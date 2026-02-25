"""
SQLAlchemy Declarative Base Model and Common Mixins.

This module provides the foundational base model class and reusable mixins that
all SQLAlchemy models in the application inherit from.  It is the **most
foundational file** in the ``src.app.models`` package — every other model file
depends on this module.

**Architecture Context:**
Replaces MyBatis 3.5.15 base mapper patterns from the original Java source
system (Sonatype Nexus Repository).  In the Java architecture, MyBatis XML
mappers defined SQL mappings for each entity; here, SQLAlchemy's Declarative
ORM with a shared abstract base class and Python mixins provides equivalent
functionality with far less boilerplate.

**Mixins Provided:**

+-------------------------+---------------------------------------------------+
| Mixin                   | Purpose                                           |
+=========================+===================================================+
| ``TimestampMixin``      | Automatic ``created_at`` / ``updated_at`` columns |
+-------------------------+---------------------------------------------------+
| ``SoftDeleteMixin``     | Logical (soft) deletion with audit trail          |
+-------------------------+---------------------------------------------------+
| ``JSONAttributesMixin`` | Extensible JSON attribute column for metadata     |
+-------------------------+---------------------------------------------------+

**Base Class:**

``BaseModel(db.Model)`` — Abstract base class with convenience methods
(``to_dict``, ``save``, ``delete``, ``update``) available to all models.

**Compatibility:**
Designed to support **both** SQLite (standalone deployments) and PostgreSQL
(clustered / enterprise deployments) via portable SQLAlchemy types.  No
database-specific types (e.g., ``JSONB``, ``ARRAY``) are used.

**Circular Import Safety:**
This module imports ONLY from ``src.app.extensions`` (for the ``db`` instance)
and standard library / SQLAlchemy packages.  It has **zero** imports from other
model files, which prevents circular import issues.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Boolean, Column, DateTime, JSON, Text, func, inspect
from sqlalchemy.orm import declared_attr

from src.app.extensions import db

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "BaseModel",
    "TimestampMixin",
    "SoftDeleteMixin",
    "JSONAttributesMixin",
]


# ===========================================================================
# TimestampMixin
# ===========================================================================


class TimestampMixin:
    """Mixin that adds automatic ``created_at`` and ``updated_at`` columns.

    Every model that includes this mixin will automatically track when a row
    was first created and when it was last modified.  Both columns use
    UTC-aware datetimes and portable ``func.now()`` server defaults so they
    work correctly on both SQLite and PostgreSQL.

    Usage::

        class MyModel(BaseModel, TimestampMixin):
            __tablename__ = "my_table"
            id = Column(Integer, primary_key=True)
    """

    @declared_attr
    def created_at(cls):  # noqa: N805 — declared_attr convention
        """Timestamp of record creation (UTC).

        Set once when the row is first inserted.  Never modified afterwards.

        - ``default``: Python-side default via ``datetime.now(timezone.utc)``
        - ``server_default``: Database-side default via ``func.now()``
          (compatible with both SQLite and PostgreSQL)
        """
        return Column(
            DateTime,
            nullable=False,
            default=lambda: datetime.now(timezone.utc),
            server_default=func.now(),
            index=True,
        )

    @declared_attr
    def updated_at(cls):  # noqa: N805
        """Timestamp of last modification (UTC).

        Automatically updated every time the row is modified via SQLAlchemy's
        ``onupdate`` hook.

        - ``default``: Python-side default via ``datetime.now(timezone.utc)``
        - ``server_default``: Database-side default via ``func.now()``
        - ``onupdate``: Python-side auto-update on modification
        """
        return Column(
            DateTime,
            nullable=False,
            default=lambda: datetime.now(timezone.utc),
            server_default=func.now(),
            onupdate=lambda: datetime.now(timezone.utc),
        )


# ===========================================================================
# SoftDeleteMixin
# ===========================================================================


class SoftDeleteMixin:
    """Mixin that enables soft-deletion (logical deletion).

    Models that include this mixin are *never* physically removed from the
    database.  Instead, they are flagged as deleted and timestamped.  This
    supports:

    - **Audit trail**: Deleted records remain available for compliance queries.
    - **Data recovery**: Accidentally deleted records can be restored.
    - **Referential integrity**: Foreign-key references are not broken.

    Corresponds to the soft-delete / audit-trail requirement in AAP Section
    0.7.1.

    Usage::

        class MyModel(BaseModel, SoftDeleteMixin):
            __tablename__ = "my_table"
            id = Column(Integer, primary_key=True)

        instance = MyModel.query.get(1)
        instance.soft_delete()   # logically deletes
        instance.restore()       # reverts deletion
        active = MyModel.query_active()  # excludes soft-deleted rows
    """

    @declared_attr
    def deleted_at(cls):  # noqa: N805
        """Timestamp of soft-deletion (UTC), or ``None`` if not deleted."""
        return Column(
            DateTime,
            nullable=True,
            default=None,
        )

    @declared_attr
    def is_deleted(cls):  # noqa: N805
        """Boolean flag indicating whether the record has been soft-deleted.

        Indexed for efficient filtering in queries that exclude deleted rows.
        """
        return Column(
            Boolean,
            nullable=False,
            default=False,
            server_default="0",
            index=True,
        )

    # -- Instance methods ----------------------------------------------------

    def soft_delete(self) -> None:
        """Mark this record as soft-deleted.

        Sets ``is_deleted`` to ``True`` and records the deletion timestamp in
        ``deleted_at``.  The caller is responsible for committing the session
        (or can use ``save()`` if the model also inherits ``BaseModel``).
        """
        self.is_deleted = True
        self.deleted_at = datetime.now(timezone.utc)
        db.session.add(self)
        db.session.commit()
        logger.debug(
            "Soft-deleted %s (deleted_at=%s).",
            repr(self),
            self.deleted_at.isoformat() if self.deleted_at else "N/A",
        )

    def restore(self) -> None:
        """Restore a previously soft-deleted record.

        Clears the ``is_deleted`` flag and the ``deleted_at`` timestamp.  The
        caller is responsible for committing the session.
        """
        self.is_deleted = False
        self.deleted_at = None
        db.session.add(self)
        db.session.commit()
        logger.debug("Restored %s from soft-deletion.", repr(self))

    # -- Properties ----------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Return ``True`` if this record has **not** been soft-deleted."""
        return not self.is_deleted

    # -- Class methods -------------------------------------------------------

    @classmethod
    def query_active(cls) -> Any:
        """Return a query filtered to only non-deleted (active) records.

        This is a convenience class method equivalent to::

            MyModel.query.filter_by(is_deleted=False)

        Returns:
            A SQLAlchemy query scoped to active rows.
        """
        return cls.query.filter_by(is_deleted=False)


# ===========================================================================
# JSONAttributesMixin
# ===========================================================================


class JSONAttributesMixin:
    """Mixin that adds an extensible ``attributes`` JSON column.

    The ``attributes`` column is the primary extensibility mechanism described
    in AAP Section 0.2.3.  It provides flexible key-value storage used for:

    - **Format-specific metadata**: Maven GAV coordinates, npm scope, Docker
      manifest digest, etc.
    - **Type-specific configuration**: Proxy settings, group membership order,
      hosted write policies.
    - **User-defined properties**: Custom tags and labels attached to entities.

    Replaces the ``attributes`` JSON fields that appear across multiple Java
    entity types in the original DataStore schema.

    The column uses SQLAlchemy's portable ``JSON`` type, which automatically
    maps to:
    - ``TEXT`` with JSON emulation on SQLite
    - Native ``json`` / ``jsonb`` on PostgreSQL

    Usage::

        class MyModel(BaseModel, JSONAttributesMixin):
            __tablename__ = "my_table"
            id = Column(Integer, primary_key=True)

        instance = MyModel()
        instance.set_attribute("format", "maven2")
        value = instance.get_attribute("format")  # "maven2"
        instance.merge_attributes({"group_id": "com.example", "version": "1.0"})
        instance.remove_attribute("version")
    """

    @declared_attr
    def attributes(cls):  # noqa: N805
        """JSON column for extensible metadata.

        Defaults to an empty dict ``{}`` rather than ``None`` so that callers
        can always safely access ``self.attributes[key]`` without a null check.
        """
        return Column(
            JSON,
            nullable=True,
            default=dict,
        )

    # -- Instance methods ----------------------------------------------------

    def get_attribute(self, key: str, default: Any = None) -> Any:
        """Safely retrieve a value from the ``attributes`` dict.

        Args:
            key: The attribute key to look up.
            default: Value to return if the key is missing or attributes is
                ``None``.  Defaults to ``None``.

        Returns:
            The stored value, or *default* if not found.
        """
        if self.attributes is None:
            return default
        return self.attributes.get(key, default)

    def set_attribute(self, key: str, value: Any) -> None:
        """Set a value in the ``attributes`` dict.

        Creates the dict if ``attributes`` is currently ``None``.

        Args:
            key: The attribute key to set.
            value: The value to store (must be JSON-serializable).
        """
        if self.attributes is None:
            self.attributes = {}
        # SQLAlchemy JSON mutation tracking requires reassignment of the
        # top-level value so that the ORM marks the column as dirty.
        attrs = dict(self.attributes)
        attrs[key] = value
        self.attributes = attrs

    def remove_attribute(self, key: str) -> None:
        """Remove a key from the ``attributes`` dict, if present.

        No-op if the key does not exist or if ``attributes`` is ``None``.

        Args:
            key: The attribute key to remove.
        """
        if self.attributes is None:
            return
        if key in self.attributes:
            attrs = dict(self.attributes)
            del attrs[key]
            self.attributes = attrs

    def merge_attributes(self, attrs: dict) -> None:
        """Merge the given dict into the existing ``attributes``.

        Existing keys are overwritten; new keys are added.  If ``attributes``
        is currently ``None``, a new dict is created from *attrs*.

        Args:
            attrs: Dictionary of attributes to merge.
        """
        if not isinstance(attrs, dict):
            raise TypeError(
                f"merge_attributes expects a dict, got {type(attrs).__name__}"
            )
        if self.attributes is None:
            self.attributes = dict(attrs)
        else:
            merged = dict(self.attributes)
            merged.update(attrs)
            self.attributes = merged


# ===========================================================================
# BaseModel
# ===========================================================================


class BaseModel(db.Model):
    """Abstract base class for all SQLAlchemy models in the application.

    Provides convenience methods (``to_dict``, ``save``, ``delete``,
    ``update``) that are available to every concrete model.

    Concrete models inherit from ``BaseModel`` and optionally include one or
    more of the mixins defined in this module::

        class Repository(BaseModel, TimestampMixin, SoftDeleteMixin,
                         JSONAttributesMixin):
            __tablename__ = "repository"
            name = Column(String(200), primary_key=True)
            ...

    **Important:** This class is marked ``__abstract__ = True`` so that
    SQLAlchemy does **not** attempt to create a table for it.
    """

    __abstract__: bool = True
    __allow_unmapped__: bool = True

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert this model instance to a plain dictionary.

        Iterates over all mapped columns and copies their values into a dict.
        ``datetime`` values are serialized to ISO-8601 strings for JSON
        compatibility.

        Returns:
            A dictionary mapping column names to their current values.
        """
        result: dict[str, Any] = {}
        mapper = inspect(self.__class__)
        for column in mapper.columns:
            value: Any = getattr(self, column.key)
            if isinstance(value, datetime):
                value = value.isoformat()
            result[column.key] = value
        return result

    # -- Session convenience methods -----------------------------------------

    def save(self) -> "BaseModel":
        """Add this instance to the session and commit.

        Returns:
            ``self`` for method chaining.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On any database-level error.
        """
        db.session.add(self)
        db.session.commit()
        return self

    def delete(self) -> None:
        """Remove this instance from the database and commit.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On any database-level error.
        """
        db.session.delete(self)
        db.session.commit()

    def update(self, **kwargs: Any) -> "BaseModel":
        """Bulk-update attributes on this instance and commit.

        Only attributes that already exist on the model are set; unknown keys
        are silently ignored.

        Args:
            **kwargs: Keyword arguments mapping attribute names to new values.

        Returns:
            ``self`` for method chaining.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On any database-level error.
        """
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        db.session.commit()
        return self

    # -- Representation ------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Includes the class name and the values of all primary-key columns for
        quick identification during debugging.
        """
        mapper = inspect(self.__class__)
        pk_cols = [col.key for col in mapper.primary_key]
        pk_values = ", ".join(
            f"{col}={getattr(self, col, '?')!r}" for col in pk_cols
        )
        return f"<{self.__class__.__name__}({pk_values})>"


logger.debug(
    "Base model module loaded — exports: %s",
    ", ".join(__all__),
)
