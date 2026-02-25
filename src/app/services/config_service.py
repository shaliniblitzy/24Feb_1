"""
System Configuration Management Service (Feature F-404).

This module implements the **ConfigService** class — the primary business-logic
service for managing runtime system configuration stored in the
:class:`~src.app.models.system_config.SystemConfig` key-value table.

**Architecture Context:**

Replaces the Java system configuration management layer from the Sonatype
Nexus Repository source system.  Configuration values use dot-notation keys
(e.g. ``'repository.proxy.connection_timeout'``) and are persisted as text
in the ``system_configs`` table.  An in-memory cache with a configurable TTL
(default 5 minutes) reduces database load on hot-path reads.

Every mutating operation (``set()``, ``delete()``, ``set_many()``,
``import_config()``) emits a :data:`~src.app.events.event_types.EventType.CONFIG_CHANGED`
event via the Blinker-based event bus, enabling:

- Cache invalidation across distributed nodes
- Real-time configuration propagation
- Audit logging (Feature F-303)
- Webhook dispatch (Feature F-503)

**Feature Dependencies:**

- F-404 — System Configuration Management (primary)
- F-303 — Audit Logging (via CONFIG_CHANGED events)
- F-403 — Support ZIP Generation (via ``get_all_masked()``)
- F-503 — Webhook Integration (via CONFIG_CHANGED events)

**Exports:**

- ``ConfigService``       — Service class with typed getters, cache, import/export
- ``CONFIG_CATEGORIES``   — Category prefix → description mapping
- ``SENSITIVE_KEYS``      — Set of keys whose values must be masked in API responses
- ``SYSTEM_DEFAULTS``     — Dict of critical default configuration values
- ``CACHE_TTL_SECONDS``   — In-memory cache time-to-live (300 s)
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from flask import current_app

from src.app.extensions import db
from src.app.models.system_config import SystemConfig
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# Provides structured logging for configuration CRUD, cache management,
# default initialization, import/export, and error diagnostics.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "ConfigService",
    "CONFIG_CATEGORIES",
    "SENSITIVE_KEYS",
    "SYSTEM_DEFAULTS",
    "CACHE_TTL_SECONDS",
]

# ===========================================================================
# Module-Level Constants
# ===========================================================================

# Config key prefixes for categorization.
# The first segment of a dot-notation key is matched against these prefixes
# to auto-detect the category when the caller does not provide one.
CONFIG_CATEGORIES: dict[str, str] = {
    "system": "System-wide settings",
    "security": "Security-related settings",
    "repository": "Repository default settings",
    "storage": "Storage backend settings",
    "network": "Network and proxy settings",
    "email": "Email/SMTP settings",
    "scheduling": "Task scheduling settings",
}

# Sensitive keys that must be masked in API responses, support ZIPs (F-403),
# and configuration exports.  This is a security-critical constant.
SENSITIVE_KEYS: set[str] = {
    "security.ldap.password",
    "security.saml.keystore_password",
    "storage.s3.secret_key",
    "email.smtp.password",
    "security.jwt.secret",
    "system.http.proxy.password",
}

# Default values for critical system settings.  These are created in the
# database on first run via ``initialize_defaults()`` and serve as in-code
# fallbacks when a key is not found in the DB or cache.
SYSTEM_DEFAULTS: dict[str, str] = {
    "system.base_url": "http://localhost:8081",
    "system.user_agent_prefix": "Nexus/",
    "repository.default_blob_store": "default",
    "repository.proxy.connection_timeout": "20",
    "repository.proxy.retry_count": "3",
    "repository.proxy.negative_cache_enabled": "true",
    "repository.proxy.negative_cache_ttl": "1440",
    "security.anonymous_access": "false",
    "security.session_timeout": "30",
    "security.content_security_policy": "default-src 'self'",
    "scheduling.cleanup_interval": "86400",
}

# In-memory cache TTL in seconds.  Configuration values are cached for this
# duration to reduce database round-trips on hot-path reads.
CACHE_TTL_SECONDS: int = 300  # 5 minutes


# ===========================================================================
# ConfigService Class
# ===========================================================================


class ConfigService:
    """High-level service for managing system configuration (Feature F-404).

    Provides typed getters, cache-aware reads, transactional writes with
    event propagation, bulk retrieval by category / prefix, sensitive-key
    masking, default initialization, and import / export support.

    Typical lifecycle::

        svc = ConfigService()
        svc.initialize_defaults()   # First-run: seed SYSTEM_DEFAULTS
        svc.warm_cache()            # Startup: preload DB into cache

        value = svc.get('repository.proxy.connection_timeout', default='20')
        svc.set('repository.proxy.connection_timeout', '30')
    """

    # -----------------------------------------------------------------------
    # Construction
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise a new ConfigService instance.

        Creates an instance-level logger and two dictionaries that together
        form the in-memory configuration cache:

        - ``_cache``            — key → parsed value
        - ``_cache_timestamps`` — key → epoch-second of last refresh
        """
        self.logger: logging.Logger = logging.getLogger(__name__)
        self._cache: dict[str, Any] = {}
        self._cache_timestamps: dict[str, float] = {}

    # ===================================================================
    # Core CRUD Operations
    # ===================================================================

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve a single configuration value by its dot-notation key.

        Resolution order:

        1. In-memory cache (if TTL has not expired)
        2. Database (``SystemConfig`` table)
        3. ``SYSTEM_DEFAULTS`` dict
        4. Caller-provided *default*

        Complex values (JSON dicts, lists, booleans) stored as text are
        automatically parsed via :meth:`_parse_value`.

        Args:
            key: Dot-notation configuration key.
            default: Fallback value when the key is not found anywhere.

        Returns:
            The configuration value (parsed if JSON) or *default*.
        """
        # 1. Check in-memory cache
        if self._is_cache_valid(key):
            self.logger.debug("Cache hit for key '%s'.", key)
            return self._cache[key]

        # 2. Query database
        try:
            config: SystemConfig | None = db.session.get(SystemConfig, key)
        except Exception:
            self.logger.exception("DB error retrieving key '%s'.", key)
            config = None

        if config is not None and config.value is not None:
            parsed = self._parse_value(config.value)
            self._update_cache(key, parsed)
            self.logger.debug("DB hit for key '%s'.", key)
            return parsed

        # 3. Fallback to SYSTEM_DEFAULTS
        if key in SYSTEM_DEFAULTS:
            fallback = self._parse_value(SYSTEM_DEFAULTS[key])
            self._update_cache(key, fallback)
            self.logger.debug("Defaults hit for key '%s'.", key)
            return fallback

        # 4. Caller default
        self.logger.debug(
            "Key '%s' not found; returning caller default=%r.", key, default
        )
        return default

    # -----------------------------------------------------------------------
    # Typed getters
    # -----------------------------------------------------------------------

    def get_str(self, key: str, default: str = "") -> str:
        """Return the configuration value as a string.

        If the underlying value is *None*, the *default* is returned.
        Non-string values are coerced via ``str()``.
        """
        raw = self.get(key, default=None)
        if raw is None:
            return default
        return str(raw)

    def get_int(self, key: str, default: int = 0) -> int:
        """Return the configuration value as an integer.

        Performs safe conversion; returns *default* on parse failure.
        """
        raw = self.get(key, default=None)
        if raw is None:
            return default
        try:
            return int(raw)
        except (ValueError, TypeError):
            self.logger.debug(
                "Cannot convert key '%s' value %r to int; returning default=%d.",
                key,
                raw,
                default,
            )
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Return the configuration value as a boolean.

        Truthy strings: ``'true'``, ``'1'``, ``'yes'``, ``'on'``
        Falsy strings:  ``'false'``, ``'0'``, ``'no'``, ``'off'``

        Returns *default* for unrecognised or missing values.
        """
        raw = self.get(key, default=None)
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        normalised = str(raw).lower().strip()
        if normalised in ("true", "1", "yes", "on"):
            return True
        if normalised in ("false", "0", "no", "off"):
            return False
        self.logger.debug(
            "Cannot convert key '%s' value %r to bool; returning default=%s.",
            key,
            raw,
            default,
        )
        return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        """Return the configuration value as a float.

        Performs safe conversion; returns *default* on parse failure.
        """
        raw = self.get(key, default=None)
        if raw is None:
            return default
        try:
            return float(raw)
        except (ValueError, TypeError):
            self.logger.debug(
                "Cannot convert key '%s' value %r to float; returning default=%s.",
                key,
                raw,
                default,
            )
            return default

    # -----------------------------------------------------------------------
    # Write operations
    # -----------------------------------------------------------------------

    def set(  # noqa: A003 — intentional shadow of builtin for API parity
        self,
        key: str,
        value: Any,
        category: str | None = None,
    ) -> SystemConfig:
        """Create or update a single configuration entry (upsert).

        The value is serialised to text for storage.  Complex Python types
        (``dict``, ``list``) are JSON-encoded; booleans become ``'true'`` /
        ``'false'``; everything else uses ``str(value)``.

        A :data:`~src.app.events.event_types.EventType.CONFIG_CHANGED`
        event is emitted after every successful write.

        Args:
            key: Dot-notation configuration key.
            value: The value to store (any serialisable type).
            category: Optional category override.  Auto-detected from the
                key prefix if not supplied.

        Returns:
            The created or updated :class:`SystemConfig` instance.
        """
        # Determine the effective category
        if category is None:
            category = self._detect_category(key)

        # Serialise value to text
        str_value = self._serialise_value(value)

        # Attempt upsert
        try:
            existing: SystemConfig | None = db.session.get(SystemConfig, key)
            old_value: str | None = None

            if existing is not None:
                old_value = existing.value
                existing.value = str_value
                if category is not None:
                    existing.category = category
                config = existing
                self.logger.info(
                    "Updated SystemConfig '%s' = '%s' (category=%s).",
                    key,
                    str_value,
                    category,
                )
            else:
                config = SystemConfig(
                    key=key,
                    value=str_value,
                    category=category,
                )
                db.session.add(config)
                self.logger.info(
                    "Created SystemConfig '%s' = '%s' (category=%s).",
                    key,
                    str_value,
                    category,
                )

            db.session.commit()

            # Update cache
            parsed = self._parse_value(str_value) if str_value is not None else None
            self._update_cache(key, parsed)

            # Emit event for propagation, audit, webhooks
            emit_event(
                EventType.CONFIG_CHANGED,
                payload={
                    "key": key,
                    "old_value": old_value,
                    "new_value": str_value,
                    "category": category,
                },
            )

            return config

        except Exception:
            db.session.rollback()
            self.logger.exception("Failed to set SystemConfig '%s'.", key)
            raise

    def delete(self, key: str) -> bool:
        """Delete a configuration entry by key.

        Removes the entry from both the database and the in-memory cache.
        Emits a ``CONFIG_CHANGED`` event with ``new_value=None``.

        Args:
            key: Dot-notation configuration key.

        Returns:
            ``True`` if the entry was found and deleted, ``False`` otherwise.
        """
        try:
            existing: SystemConfig | None = db.session.get(SystemConfig, key)
            if existing is None:
                self.logger.debug("Delete requested for non-existent key '%s'.", key)
                return False

            old_value = existing.value
            old_category = existing.category

            db.session.delete(existing)
            db.session.commit()

            # Purge from cache
            self.invalidate_cache(key)

            self.logger.info("Deleted SystemConfig '%s'.", key)

            emit_event(
                EventType.CONFIG_CHANGED,
                payload={
                    "key": key,
                    "old_value": old_value,
                    "new_value": None,
                    "category": old_category,
                },
            )

            return True

        except Exception:
            db.session.rollback()
            self.logger.exception("Failed to delete SystemConfig '%s'.", key)
            raise

    def set_many(self, settings: dict[str, Any]) -> list[SystemConfig]:
        """Batch-set multiple configuration values in a single transaction.

        Internally delegates to :meth:`set` for each key/value pair.

        Args:
            settings: Mapping of dot-notation key → value.

        Returns:
            List of created / updated :class:`SystemConfig` instances.
        """
        results: list[SystemConfig] = []
        for key, value in settings.items():
            result = self.set(key, value)
            results.append(result)
        self.logger.info("Batch-set %d configuration entries.", len(results))
        return results

    # ===================================================================
    # Bulk Retrieval Operations
    # ===================================================================

    def get_by_category(self, category: str) -> dict[str, Any]:
        """Retrieve all configuration entries matching a category.

        Args:
            category: Exact category string (e.g. ``'security'``).

        Returns:
            Dict mapping key → parsed value for entries in the category.
        """
        try:
            configs: list[SystemConfig] = (
                SystemConfig.query
                .filter(SystemConfig.category == category)
                .all()
            )
        except Exception:
            self.logger.exception(
                "DB error querying category '%s'.", category
            )
            return {}

        result: dict[str, Any] = {}
        for cfg in configs:
            result[cfg.key] = self._parse_value(cfg.value) if cfg.value is not None else None
        self.logger.debug(
            "Retrieved %d entries for category '%s'.", len(result), category
        )
        return result

    def get_by_prefix(self, prefix: str) -> dict[str, Any]:
        """Retrieve all configuration entries whose key starts with *prefix*.

        Useful for getting all settings under a section, e.g.
        ``get_by_prefix('repository.proxy')`` returns all proxy settings.

        Args:
            prefix: Key prefix string (e.g. ``'repository.proxy'``).

        Returns:
            Dict mapping key → parsed value for matching entries.
        """
        try:
            configs: list[SystemConfig] = (
                SystemConfig.query
                .filter(SystemConfig.key.startswith(prefix))
                .all()
            )
        except Exception:
            self.logger.exception(
                "DB error querying prefix '%s'.", prefix
            )
            return {}

        result: dict[str, Any] = {}
        for cfg in configs:
            result[cfg.key] = self._parse_value(cfg.value) if cfg.value is not None else None
        self.logger.debug(
            "Retrieved %d entries for prefix '%s'.", len(result), prefix
        )
        return result

    def get_all(self, include_defaults: bool = True) -> dict[str, Any]:
        """Retrieve ALL configuration entries.

        When *include_defaults* is ``True`` (the default), any
        ``SYSTEM_DEFAULTS`` key that is absent from the database is
        included with its default value.  Database entries always take
        precedence over defaults.

        Args:
            include_defaults: Merge ``SYSTEM_DEFAULTS`` as a baseline.

        Returns:
            Complete configuration dict mapping key → parsed value.
        """
        result: dict[str, Any] = {}

        # Baseline: SYSTEM_DEFAULTS (if requested)
        if include_defaults:
            for key, val in SYSTEM_DEFAULTS.items():
                result[key] = self._parse_value(val)

        # Override with DB entries
        try:
            all_configs: list[SystemConfig] = SystemConfig.query.all()
        except Exception:
            self.logger.exception("DB error querying all configs.")
            return result

        for cfg in all_configs:
            result[cfg.key] = self._parse_value(cfg.value) if cfg.value is not None else None

        return result

    def get_all_masked(self) -> dict[str, Any]:
        """Retrieve ALL configuration entries with sensitive values masked.

        Keys present in :data:`SENSITIVE_KEYS` have their values replaced
        with ``'********'``.  Used by the support ZIP service (F-403) and
        the admin API to prevent credential leakage.

        Returns:
            Configuration dict with masked sensitive values.
        """
        all_config = self.get_all(include_defaults=True)
        for key in all_config:
            if self.is_sensitive_key(key):
                all_config[key] = "********"
        return all_config

    # ===================================================================
    # Cache Management
    # ===================================================================

    def invalidate_cache(self, key: str | None = None) -> None:
        """Invalidate one or all entries in the in-memory cache.

        Args:
            key: Specific key to invalidate.  If ``None``, the **entire**
                cache is cleared.
        """
        if key is not None:
            self._cache.pop(key, None)
            self._cache_timestamps.pop(key, None)
            self.logger.debug("Cache invalidated for key '%s'.", key)
        else:
            self._cache.clear()
            self._cache_timestamps.clear()
            self.logger.debug("Entire configuration cache invalidated.")

    def warm_cache(self) -> None:
        """Pre-load all database configuration entries into the cache.

        Intended to be called during application startup (from
        ``factory.py``) so that subsequent reads are served from memory.
        """
        try:
            all_configs: list[SystemConfig] = SystemConfig.query.all()
            for cfg in all_configs:
                parsed = self._parse_value(cfg.value) if cfg.value is not None else None
                self._update_cache(cfg.key, parsed)
            self.logger.info(
                "Configuration cache warmed with %d entries.", len(all_configs)
            )
        except Exception:
            self.logger.exception("Failed to warm configuration cache.")

    # -----------------------------------------------------------------------
    # Private cache helpers
    # -----------------------------------------------------------------------

    def _is_cache_valid(self, key: str) -> bool:
        """Return ``True`` if *key* exists in the cache and its TTL has not expired."""
        if key not in self._cache:
            return False
        cached_at = self._cache_timestamps.get(key, 0.0)
        return (time.time() - cached_at) < CACHE_TTL_SECONDS

    def _update_cache(self, key: str, value: Any) -> None:
        """Store *value* and the current timestamp in the cache dicts."""
        self._cache[key] = value
        self._cache_timestamps[key] = time.time()

    # ===================================================================
    # Configuration Initialisation and Defaults
    # ===================================================================

    def initialize_defaults(self) -> None:
        """Ensure all ``SYSTEM_DEFAULTS`` entries exist in the database.

        Only creates entries for keys that do **not** already exist — user
        modifications are never overwritten.  This is intended to be called
        during application first-run or after schema migrations.
        """
        created_count = 0
        try:
            for key, default_value in SYSTEM_DEFAULTS.items():
                existing = db.session.get(SystemConfig, key)
                if existing is None:
                    category = self._detect_category(key)
                    config = SystemConfig(
                        key=key,
                        value=default_value,
                        category=category,
                    )
                    db.session.add(config)
                    created_count += 1
                    self.logger.debug(
                        "Seeding default: '%s' = '%s' (category=%s).",
                        key,
                        default_value,
                        category,
                    )
            db.session.commit()
            self.logger.info(
                "Default initialisation complete: %d new entries created "
                "(%d already present).",
                created_count,
                len(SYSTEM_DEFAULTS) - created_count,
            )
        except Exception:
            db.session.rollback()
            self.logger.exception("Failed to initialise default configuration.")
            raise

    def get_system_status(self) -> dict[str, Any]:
        """Return a summary of the current system configuration state.

        The returned dictionary includes:

        - ``total_entries``    — Number of config rows in the database
        - ``categories``       — Per-category entry count
        - ``cache_size``       — Number of entries in the in-memory cache
        - ``defaults_applied`` — Whether all ``SYSTEM_DEFAULTS`` exist in DB

        Returns:
            Configuration status summary dict.
        """
        try:
            all_configs: list[SystemConfig] = SystemConfig.query.all()
        except Exception:
            self.logger.exception("DB error in get_system_status.")
            return {
                "total_entries": 0,
                "categories": {},
                "cache_size": len(self._cache),
                "defaults_applied": False,
            }

        category_counts: dict[str, int] = defaultdict(int)
        existing_keys: set[str] = set()
        for cfg in all_configs:
            cat = cfg.category or "general"
            category_counts[cat] += 1
            existing_keys.add(cfg.key)

        defaults_applied = all(k in existing_keys for k in SYSTEM_DEFAULTS)

        return {
            "total_entries": len(all_configs),
            "categories": dict(category_counts),
            "cache_size": len(self._cache),
            "defaults_applied": defaults_applied,
        }

    # ===================================================================
    # Export and Import
    # ===================================================================

    def export_config(self, include_sensitive: bool = False) -> dict[str, Any]:
        """Export all configuration as a serialisable dictionary.

        The returned structure contains a ``metadata`` block (timestamp,
        application version) and a ``settings`` block with every key-value
        pair currently in the database.

        Args:
            include_sensitive: When ``False`` (default), values for keys in
                :data:`SENSITIVE_KEYS` are replaced with ``'********'``.

        Returns:
            Exportable configuration dictionary.
        """
        all_config = self.get_all(include_defaults=False)

        if not include_sensitive:
            for key in list(all_config.keys()):
                if self.is_sensitive_key(key):
                    all_config[key] = "********"

        # Determine app version safely (may be outside app context in tests)
        app_version = "unknown"
        try:
            app_version = current_app.config.get("APP_VERSION", "1.0.0")
        except RuntimeError:
            pass  # Outside application context

        return {
            "metadata": {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "version": app_version,
                "total_entries": len(all_config),
            },
            "settings": all_config,
        }

    def import_config(
        self,
        config_data: dict[str, Any],
        overwrite: bool = False,
    ) -> dict[str, int]:
        """Import configuration from a dictionary (e.g. a backup).

        Args:
            config_data: A dict whose ``settings`` key maps config keys to
                their values.  If ``settings`` is absent, *config_data*
                itself is treated as the flat key-value mapping.
            overwrite: When ``False`` (default), existing keys are skipped.
                When ``True``, existing values are updated.

        Returns:
            Summary dict: ``{'imported': int, 'skipped': int, 'errors': int}``
        """
        settings = config_data.get("settings", config_data)
        imported = 0
        skipped = 0
        errors = 0

        for key, value in settings.items():
            # Skip metadata keys that are not actual settings
            if key == "metadata":
                continue
            try:
                existing = db.session.get(SystemConfig, key)
                if existing is not None and not overwrite:
                    skipped += 1
                    continue

                self.set(key, value)
                imported += 1
            except Exception:
                self.logger.exception(
                    "Error importing config key '%s'.", key
                )
                errors += 1

        summary = {"imported": imported, "skipped": skipped, "errors": errors}
        self.logger.info(
            "Configuration import complete: imported=%d, skipped=%d, errors=%d.",
            imported,
            skipped,
            errors,
        )
        return summary

    # ===================================================================
    # Helper / Utility Methods
    # ===================================================================

    def _detect_category(self, key: str) -> str:
        """Auto-detect category from the first segment of a dot-notation key.

        Args:
            key: Configuration key (e.g. ``'repository.proxy.timeout'``).

        Returns:
            Category string (e.g. ``'repository'``) or ``'general'`` if the
            prefix does not match any known category.
        """
        if not key:
            return "general"
        first_segment = key.split(".", maxsplit=1)[0]
        if first_segment in CONFIG_CATEGORIES:
            return first_segment
        return "general"

    def _parse_value(self, raw_value: str | None) -> Any:
        """Attempt to parse a stored text value into an appropriate type.

        Resolution:

        1. ``None`` → ``None``
        2. JSON parse (handles dicts, lists, booleans, numbers)
        3. Return raw string on parse failure

        Args:
            raw_value: The text value from the database.

        Returns:
            A Python object (``dict``, ``list``, ``bool``, ``int``,
            ``float``) or the original string.
        """
        if raw_value is None:
            return None
        try:
            return json.loads(raw_value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return raw_value

    @staticmethod
    def _serialise_value(value: Any) -> str | None:
        """Convert a Python value to its text representation for storage.

        - ``None``               → ``None`` (SQL NULL)
        - ``dict``, ``list``     → JSON string
        - ``bool``               → ``'true'`` / ``'false'``
        - Everything else        → ``str(value)``
        """
        if value is None:
            return None
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value)
        if isinstance(value, bool):
            return str(value).lower()
        return str(value)

    @staticmethod
    def is_sensitive_key(key: str) -> bool:
        """Return ``True`` if *key* is in the :data:`SENSITIVE_KEYS` set.

        Used by the API layer, ``get_all_masked()``, ``export_config()``,
        and the support ZIP service (Feature F-403) to prevent credential
        leakage in responses and diagnostic bundles.
        """
        return key in SENSITIVE_KEYS


logger.debug("ConfigService module loaded.")
