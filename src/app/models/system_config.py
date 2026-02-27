"""
SystemConfig SQLAlchemy Model — Key-Value Configuration Store.

This module defines the ``SystemConfig`` model that provides a persistent
key-value store for runtime system configuration.  It replaces the
``SYSTEM_CONFIG`` entity from the original Java DataStore schema
(Section 6.2.1.2) and supports Feature F-404 (System Configuration
Management).

**Architecture Context:**

In the Java Sonatype Nexus Repository, system configuration was managed
through MyBatis 3.5.15 mappers operating on the ``SYSTEM_CONFIG`` table
with columns ``key``, ``value``, and ``category``.  This Python
implementation provides equivalent functionality using SQLAlchemy 2.0.36
with the Flask-SQLAlchemy 3.1.1 integration, supporting both SQLite
(standalone deployments) and PostgreSQL (clustered / enterprise
deployments).

**Key Design Decisions:**

- ``key`` is the primary key, using dot-notation for natural namespacing
  (e.g., ``'security.anonymousAccess'``, ``'http.proxy.host'``).
- ``value`` is always stored as ``Text`` — type conversion is handled by
  property accessors (``as_bool``, ``as_int``, ``as_list``).
- No ``SoftDeleteMixin`` — configuration entries are hard-deleted when
  removed, as historical config state is tracked via ``AuditEvent`` (F-303).
- No ``JSONAttributesMixin`` — this is a pure key-value table; the
  ``value`` column handles structured data via JSON string encoding.
- Category grouping enables admin UI organization and bulk retrieval.
- Class methods (``get_value``, ``set_value``, ``get_by_category``) provide
  convenient data-access patterns that downstream services consume.

**Usage Examples::

    # Retrieve a config value with a default fallback
    base_url = SystemConfig.get_value('system.baseUrl', default='http://localhost:8081')

    # Store a boolean config
    SystemConfig.set_value('security.anonymousAccess', True, category='security')

    # Store a list config as JSON
    SystemConfig.set_value('security.realms',
        ['NexusAuthenticatingRealm', 'LdapRealm'],
        category='security')

    # Read type-converted values
    config = SystemConfig.query.get('security.anonymousAccess')
    if config and config.as_bool:
        allow_anonymous()

    # Retrieve all configs in a category
    security_configs = SystemConfig.get_by_category('security')

**Compatibility:**

Designed to work identically on SQLite and PostgreSQL.  All column types
(``String``, ``Text``) are portable across both databases.  The ``Text``
column for ``value`` has no maximum length constraint, supporting
arbitrarily large configuration payloads (e.g., serialized JSON objects).
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from sqlalchemy import Column, String, Text

from src.app.extensions import db
from src.app.models.base import BaseModel, TimestampMixin

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 structured logging from the Java source.
# Used for tracing configuration reads/writes at DEBUG level and
# recording significant configuration changes at INFO level.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = ["SystemConfig"]


# ===========================================================================
# SystemConfig Model
# ===========================================================================


class SystemConfig(BaseModel, TimestampMixin):
    """Persistent key-value store for runtime system configuration.

    Each row represents a single configuration setting identified by a
    dot-notation ``key`` (e.g., ``'security.anonymousAccess'``).  The
    ``value`` is always stored as text; type-safe accessors (``as_bool``,
    ``as_int``, ``as_list``) provide convenient conversion.

    The optional ``category`` column enables logical grouping for the
    admin UI and bulk retrieval operations (e.g., all ``'security'``
    settings, all ``'http'`` proxy settings).

    **Table:** ``system_configs``

    **Primary Key:** ``key`` (String, dot-notation)

    **Indexes:**

    - ``ix_system_configs_category`` on ``category`` — accelerates
      ``get_by_category()`` queries and admin UI category filtering.

    **Inherited Columns (via TimestampMixin):**

    - ``created_at`` — UTC timestamp of initial creation.
    - ``updated_at`` — UTC timestamp of last modification.

    **Inherited Methods (via BaseModel):**

    - ``save()`` — Persist changes to the database.
    - ``delete()`` — Hard-delete this configuration entry.
    - ``update(**kwargs)`` — Bulk-update attributes and commit.

    Attributes:
        key: Configuration key in dot-notation (primary key).
            Examples: ``'security.anonymousAccess'``,
            ``'http.proxy.host'``, ``'system.baseUrl'``.
        value: Configuration value stored as text.  Boolean values use
            ``'true'``/``'false'``; integers use string representation
            (e.g., ``'8080'``); lists use JSON arrays (e.g.,
            ``'["NexusAuthenticatingRealm", "LdapRealm"]'``).
        category: Logical grouping identifier (e.g., ``'security'``,
            ``'http'``, ``'system'``, ``'email'``, ``'repository'``).
        description: Human-readable description of the configuration key.
    """

    __tablename__: str = "system_configs"

    __table_args__ = (
        db.Index("ix_system_configs_category", "category"),
    )

    # -- Columns -------------------------------------------------------------

    key: str = Column(
        String(500),
        primary_key=True,
        nullable=False,
        doc=(
            "Configuration key in dot-notation. Serves as the natural "
            "primary key providing hierarchical namespacing without "
            "requiring a surrogate id column."
        ),
    )

    value: Optional[str] = Column(
        Text,
        nullable=True,
        doc=(
            "Configuration value stored as text. Type conversion is "
            "handled by property accessors (as_bool, as_int, as_list). "
            "Supports arbitrary-length content including serialized JSON."
        ),
    )

    category: Optional[str] = Column(
        String(100),
        nullable=True,
        doc=(
            "Logical grouping category for admin UI organization. "
            "Examples: 'security', 'http', 'system', 'email', "
            "'repository', 'cleanup', 'scheduling'."
        ),
    )

    description: Optional[str] = Column(
        Text,
        nullable=True,
        doc=(
            "Human-readable description explaining the purpose and "
            "acceptable values of this configuration key."
        ),
    )

    # -- String Representation -----------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Shows both the key and current value for quick identification
        during debugging and log inspection.

        Returns:
            A string in the format ``<SystemConfig key=value>``.
        """
        return f"<SystemConfig {self.key}={self.value}>"

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert this configuration entry to a plain dictionary.

        Extends the base ``BaseModel.to_dict()`` implementation to ensure
        all SystemConfig-specific columns are included with proper
        datetime serialization for ``created_at`` and ``updated_at``.

        Returns:
            A dictionary containing all column values with datetime
            fields serialized to ISO-8601 strings.

        Example::

            config = SystemConfig.query.get('system.baseUrl')
            data = config.to_dict()
            # {
            #     'key': 'system.baseUrl',
            #     'value': 'http://localhost:8081',
            #     'category': 'system',
            #     'description': 'Base URL for link generation',
            #     'created_at': '2024-01-15T10:30:00+00:00',
            #     'updated_at': '2024-01-15T10:30:00+00:00'
            # }
        """
        return super().to_dict()

    # -- Type Conversion Properties ------------------------------------------

    @property
    def as_bool(self) -> Optional[bool]:
        """Convert the stored value to a Python boolean.

        Recognizes common truthy/falsy string representations used in
        configuration systems.  Returns ``None`` if the value cannot be
        interpreted as a boolean.

        Truthy values: ``'true'``, ``'1'``, ``'yes'``, ``'on'``
        Falsy values:  ``'false'``, ``'0'``, ``'no'``, ``'off'``

        Returns:
            ``True`` for truthy strings, ``False`` for falsy strings,
            ``None`` if the value is ``None`` or unrecognized.

        Example::

            config = SystemConfig(key='security.anonymousAccess',
                                  value='true')
            assert config.as_bool is True
        """
        if self.value is None:
            return None
        normalized: str = str(self.value).lower().strip()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
        logger.debug(
            "Cannot convert SystemConfig '%s' value '%s' to bool.",
            self.key,
            self.value,
        )
        return None

    @property
    def as_int(self) -> Optional[int]:
        """Convert the stored value to a Python integer.

        Returns ``None`` if the value is ``None`` or cannot be parsed
        as an integer.

        Returns:
            The integer representation of the value, or ``None`` on
            conversion failure.

        Example::

            config = SystemConfig(key='http.proxy.port', value='8080')
            assert config.as_int == 8080
        """
        if self.value is None:
            return None
        try:
            return int(self.value)
        except (ValueError, TypeError):
            logger.debug(
                "Cannot convert SystemConfig '%s' value '%s' to int.",
                self.key,
                self.value,
            )
            return None

    @property
    def as_list(self) -> List[Any]:
        """Parse the stored value as a JSON array.

        Configuration values that represent ordered collections (e.g.,
        active authentication realm names) are stored as JSON-encoded
        arrays.  This property parses them back into Python lists.

        Returns an empty list if the value is ``None``, not valid JSON,
        or not a JSON array.

        Returns:
            A Python list parsed from the JSON value, or an empty list
            on failure.

        Example::

            config = SystemConfig(
                key='security.realms',
                value='["NexusAuthenticatingRealm", "LdapRealm"]'
            )
            assert config.as_list == [
                "NexusAuthenticatingRealm", "LdapRealm"
            ]
        """
        if self.value is None:
            return []
        try:
            parsed: Any = json.loads(self.value)
            if isinstance(parsed, list):
                return parsed
            logger.debug(
                "SystemConfig '%s' value is valid JSON but not an array "
                "(type=%s). Returning empty list.",
                self.key,
                type(parsed).__name__,
            )
            return []
        except (json.JSONDecodeError, TypeError):
            logger.debug(
                "Cannot parse SystemConfig '%s' value as JSON list.",
                self.key,
            )
            return []

    # -- Class Methods -------------------------------------------------------

    @classmethod
    def get_value(cls, key: str, default: Any = None) -> Optional[Any]:
        """Retrieve a configuration value by its key with a default fallback.

        This is the primary read accessor for configuration data.  It
        performs a direct primary-key lookup and returns the raw string
        value, or the provided default if the key does not exist or has
        a ``None`` value.

        Args:
            key: The dot-notation configuration key to look up.
            default: The fallback value to return if the key is not found
                or its value is ``None``.  Defaults to ``None``.

        Returns:
            The stored configuration value as a string, or *default* if
            the key is not present or has a ``None`` value.

        Example::

            base_url = SystemConfig.get_value(
                'system.baseUrl',
                default='http://localhost:8081'
            )
        """
        config: Optional[SystemConfig] = (
            db.session.query(cls).filter_by(key=key).first()
        )
        if config is None:
            logger.debug(
                "SystemConfig key '%s' not found; returning default=%r.",
                key,
                default,
            )
            return default
        if config.value is None:
            return default
        return config.value

    @classmethod
    def set_value(
        cls,
        key: str,
        value: Any,
        category: Optional[str] = None,
        description: Optional[str] = None,
    ) -> SystemConfig:
        """Create or update a configuration entry (upsert).

        If an entry with the given key already exists, its value is
        updated.  If ``category`` or ``description`` are provided, those
        fields are also updated on the existing entry.  If no entry
        exists, a new one is created.

        Values are automatically serialized to their string
        representation:

        - ``list`` → JSON array string (via ``json.dumps``)
        - ``bool`` → ``'true'`` / ``'false'``
        - ``None`` → stored as SQL ``NULL``
        - All other types → ``str(value)``

        Args:
            key: The dot-notation configuration key.
            value: The configuration value.  Will be serialized to text.
            category: Optional logical grouping category.  If ``None``
                and updating an existing entry, the existing category is
                preserved.
            description: Optional human-readable description.  If
                ``None`` and updating an existing entry, the existing
                description is preserved.

        Returns:
            The created or updated ``SystemConfig`` instance.

        Example::

            # Store a boolean setting
            SystemConfig.set_value(
                'security.anonymousAccess', True,
                category='security',
                description='Enable anonymous access to public repos'
            )

            # Store a list setting
            SystemConfig.set_value(
                'security.realms',
                ['NexusAuthenticatingRealm', 'LdapRealm'],
                category='security'
            )
        """
        # Serialize the value to its string representation
        str_value: Optional[str] = cls._serialize_value(value)

        # Attempt to find an existing entry
        existing: Optional[SystemConfig] = (
            db.session.query(cls).filter_by(key=key).first()
        )

        if existing is not None:
            # Update existing entry
            existing.value = str_value
            if category is not None:
                existing.category = category
            if description is not None:
                existing.description = description
            config: SystemConfig = db.session.merge(existing)
            db.session.commit()
            logger.info(
                "Updated SystemConfig '%s' = '%s' (category=%s).",
                key,
                str_value,
                existing.category,
            )
            return config

        # Create new entry
        config = cls(
            key=key,
            value=str_value,
            category=category,
            description=description,
        )
        db.session.add(config)
        db.session.commit()
        logger.info(
            "Created SystemConfig '%s' = '%s' (category=%s).",
            key,
            str_value,
            category,
        )
        return config

    @classmethod
    def get_by_category(cls, category: str) -> List[SystemConfig]:
        """Retrieve all configuration entries belonging to a category.

        Performs an exact match on the ``category`` column using the
        ``ix_system_configs_category`` index for efficient lookups.
        Results are ordered by key for consistent display in admin UIs.

        Args:
            category: The category name to filter by (e.g., ``'security'``,
                ``'http'``, ``'system'``).

        Returns:
            A list of ``SystemConfig`` instances matching the given
            category, ordered by key.  Returns an empty list if no
            entries are found.

        Example::

            security_configs = SystemConfig.get_by_category('security')
            for cfg in security_configs:
                print(f"{cfg.key} = {cfg.value}")
        """
        configs: List[SystemConfig] = (
            db.session.query(cls)
            .filter_by(category=category)
            .order_by(cls.key)
            .all()
        )
        logger.debug(
            "Retrieved %d SystemConfig entries for category '%s'.",
            len(configs),
            category,
        )
        return configs

    # -- Private Helpers -----------------------------------------------------

    @staticmethod
    def _serialize_value(value: Any) -> Optional[str]:
        """Serialize a Python value to its text representation for storage.

        Handles the following type conversions:

        - ``None`` → ``None`` (stored as SQL ``NULL``)
        - ``list`` or ``tuple`` → JSON array string
        - ``dict`` → JSON object string
        - ``bool`` → ``'true'`` / ``'false'`` (lowercase)
        - All other types → ``str(value)``

        Args:
            value: The value to serialize.

        Returns:
            The string representation of the value, or ``None``.
        """
        if value is None:
            return None
        if isinstance(value, (list, tuple, dict)):
            return json.dumps(value)
        if isinstance(value, bool):
            return str(value).lower()
        return str(value)


logger.debug("SystemConfig model module loaded.")
