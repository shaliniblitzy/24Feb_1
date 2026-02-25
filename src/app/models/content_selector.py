"""
ContentSelector SQLAlchemy Model.

Replaces the ``CONTENT_SELECTOR`` entity from the Sonatype Nexus Repository
DataStore schema (Section 6.2.1.2).  Content Selectors define expressions —
written in **CSEL** (Content Selector Expression Language) or legacy **JEXL** —
that match subsets of repository content.  They are the foundation for
**Tier 3 (sub-repository) access control** in the RBAC model.

A ``repository-content-selector`` privilege references a ContentSelector by
``name``.  When the authorization engine evaluates access, it parses the
selector's ``expression`` against the request context (format, path, etc.) to
determine whether the requesting principal may access the targeted content.

**Key Relationships:**
- Referenced by name from ``Privilege.properties["contentSelector"]`` when
  the privilege type is ``'repository-content-selector'``.
- Expression **evaluation** is implemented in
  ``src.app.auth.content_selector`` — this model only stores definitions.

**Expression Examples (CSEL):**
- ``format == "maven2" and path =^ "/org/internal/"``
- ``format == "npm" and path =^ "/@mycompany/"``
- ``format == "docker" and path =~ ".*:latest"``

**Operators:**
- ``==``  — exact equality
- ``=^``  — starts-with (prefix match)
- ``=~``  — regex match
- ``and`` / ``or`` / ``not`` — boolean combinators

Supports Feature F-301 (Role-Based Access Control).

Compatibility:
    Designed for both **SQLite** (standalone) and **PostgreSQL** (enterprise)
    deployments via portable SQLAlchemy types only.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import Column, String, Text

from src.app.extensions import db
from src.app.models.base import BaseModel, JSONAttributesMixin, TimestampMixin

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = ["ContentSelector"]


# ===========================================================================
# ContentSelector Model
# ===========================================================================


class ContentSelector(BaseModel, TimestampMixin, JSONAttributesMixin):
    """SQLAlchemy model for the ``content_selectors`` table.

    Each row represents a named expression that matches subsets of repository
    content for sub-repository-level access control (Tier 3 RBAC).

    Columns
    -------
    selector_id : str
        Unique selector identifier (primary key, max 200 chars).
    name : str
        Human-readable selector name.  Must be unique across all selectors.
        Examples: ``'all-maven-snapshots'``, ``'internal-npm-packages'``,
        ``'docker-latest-tags'``.
    type : str
        Expression language type.  Valid values:
        - ``'csel'`` — Content Selector Expression Language (modern, default).
        - ``'jexl'`` — JEXL expression language (legacy support).
    expression : str
        The selector expression in the language specified by ``type``.
    description : str | None
        Optional description of what this selector matches.

    Inherited Columns (from mixins)
    --------------------------------
    attributes : dict | None
        Extensible JSON metadata (from :class:`JSONAttributesMixin`).
    created_at : datetime
        Row creation timestamp in UTC (from :class:`TimestampMixin`).
    updated_at : datetime
        Last modification timestamp in UTC (from :class:`TimestampMixin`).

    Inherited Methods (from :class:`BaseModel`)
    --------------------------------------------
    save()
        Persist this instance to the database.
    delete()
        Remove this instance from the database.
    update(**kwargs)
        Bulk-update attributes and commit.

    Inherited Methods (from :class:`JSONAttributesMixin`)
    ------------------------------------------------------
    get_attribute(key, default=None)
        Retrieve a value from the ``attributes`` dict.
    set_attribute(key, value)
        Set a value in the ``attributes`` dict.
    remove_attribute(key)
        Remove a key from the ``attributes`` dict.
    merge_attributes(attrs)
        Merge a dict into the existing ``attributes``.
    """

    __tablename__: str = "content_selectors"

    # ------------------------------------------------------------------
    # Table Arguments — Indexes
    # ------------------------------------------------------------------
    # Index on ``type`` for efficient filtering by expression language.
    # ------------------------------------------------------------------
    __table_args__ = (
        db.Index("ix_content_selectors_type", "type"),
    )

    # ------------------------------------------------------------------
    # Columns
    # ------------------------------------------------------------------

    selector_id: str = Column(
        String(200),
        primary_key=True,
        nullable=False,
        doc="Unique content selector identifier.",
    )

    name: str = Column(
        String(255),
        nullable=False,
        unique=True,
        doc=(
            "Human-readable selector name.  Must be unique.  "
            "Used as the reference key in 'repository-content-selector' "
            "privilege properties."
        ),
    )

    type: str = Column(
        String(20),
        nullable=False,
        default="csel",
        server_default="csel",
        doc=(
            "Expression language type: 'csel' (modern, default) or "
            "'jexl' (legacy).  Determines how the expression column "
            "is parsed and evaluated."
        ),
    )

    expression: str = Column(
        Text,
        nullable=False,
        doc=(
            "Selector expression in the language specified by 'type'.  "
            "CSEL operators: == (equals), =^ (starts with), "
            "=~ (regex), and, or, not."
        ),
    )

    description: Optional[str] = Column(
        Text,
        nullable=True,
        doc="Optional description of what this selector matches.",
    )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_csel(self) -> bool:
        """Return ``True`` if this selector uses CSEL expression language.

        CSEL (Content Selector Expression Language) is the modern and
        preferred expression language for content selectors in Nexus
        Repository.
        """
        return self.type == "csel"

    @property
    def is_jexl(self) -> bool:
        """Return ``True`` if this selector uses legacy JEXL expressions.

        JEXL (Java Expression Language) support is maintained for backward
        compatibility with older configurations.  New selectors should use
        ``'csel'``.
        """
        return self.type == "jexl"

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert this ContentSelector instance to a plain dictionary.

        Extends the base ``to_dict()`` implementation by adding computed
        properties ``is_csel`` and ``is_jexl`` for consumer convenience
        and serializing ``datetime`` values to ISO-8601 strings.

        Returns:
            A dictionary containing all column values plus computed
            properties.
        """
        result: dict[str, Any] = super().to_dict()
        # Append computed boolean properties for API consumers.
        result["is_csel"] = self.is_csel
        result["is_jexl"] = self.is_jexl
        return result

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Format: ``<ContentSelector selector_id: name>``
        """
        return f"<ContentSelector {self.selector_id}: {self.name}>"


logger.debug(
    "ContentSelector model loaded — table=%s, exports=%s",
    ContentSelector.__tablename__,
    ", ".join(__all__),
)
