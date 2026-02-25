"""
BlobStore Management Service (Features F-201, F-202, F-203).

Provides the business logic layer for managing BlobStore configurations —
CRUD operations on blobstore instances, storage allocation, runtime status
monitoring, BlobStore lifecycle management, and maintenance orchestration.

This service sits between the REST API layer (``src.app.api.blobstores``) and
the storage abstraction layer (``src.app.storage``).  The actual blob
read/write operations are handled by concrete BlobStore implementations
(``FileBlobStore``, ``S3BlobStore``); this service manages configuration,
lifecycle, and orchestration.

**Architecture Context:**

Replaces the Java BlobStore management layer from the original Sonatype Nexus
Repository system.  Implements the Strategy pattern (AAP Section 0.4.3) —
the BlobStore interface with pluggable File and S3 backends is configured
and instantiated through this service.

**Feature Support:**

- **F-201** (File BlobStore): Local filesystem storage with atomic writes and
  soft-delete.  File-type BlobStore configurations require a ``path`` key.
- **F-202** (S3 BlobStore): Amazon S3 storage with SSE encryption and
  multipart uploads.  S3-type configurations require ``bucket`` and ``region``
  keys.
- **F-203** (BlobStore Maintenance Tasks): Compaction, temporary file cleanup,
  and integrity verification — delegated to
  :class:`~src.app.storage.maintenance.BlobStoreMaintenanceService`.

**Event Integration:**

Emits events through the Blinker-based event bus for:
- ``BLOBSTORE_COMPACTED`` after compaction completes (audit logging F-303,
  monitoring F-401).
- Configuration change events after create/update/delete operations for
  webhook dispatch (F-503) and audit logging.

**Exported API:**

- :class:`BlobStoreService` — primary service class
- :class:`BlobStoreError` — base exception
- :class:`BlobStoreInUseError` — deletion rejected (dependent repositories)
- :class:`BlobStoreNotFoundError` — BlobStore not found
- :class:`BlobStoreConfigError` — invalid configuration
- :data:`BLOBSTORE_TYPES` — valid BlobStore type identifiers
- :data:`DEFAULT_BLOBSTORE_NAME` — default BlobStore name constant
"""

from __future__ import annotations

import copy
import logging
import re
from datetime import datetime, timezone
from typing import Any

from flask import current_app

from src.app.extensions import db
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.models.repository import Repository
from src.app.storage import create_blobstore, create_blobstore_from_model
from src.app.storage.blobstore import BlobStore, BlobStoreMetrics
from src.app.storage.maintenance import BlobStoreMaintenanceService
from src.app.utils.helpers import generate_uuid

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for all BlobStore management operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BLOBSTORE_TYPES: set[str] = {"file", "s3"}
"""Valid BlobStore backend type identifiers.  ``'file'`` for local filesystem
(Feature F-201) and ``'s3'`` for Amazon S3 (Feature F-202)."""

DEFAULT_BLOBSTORE_NAME: str = "default"
"""The system default BlobStore name.  This BlobStore is created automatically
during application startup if it does not already exist, and it cannot be
deleted."""

# -- Internal constants (not exported) --------------------------------------

_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
"""Validation regex for BlobStore names: must start with alphanumeric and
contain only alphanumeric characters, hyphens, and underscores."""

_NAME_MAX_LENGTH: int = 200
"""Maximum allowed length for BlobStore names (matches DB column width)."""

_SENSITIVE_CONFIG_KEYS: frozenset[str] = frozenset({
    "secretAccessKey",
    "accessKeyId",
    "secret_access_key",
    "access_key_id",
})
"""Top-level configuration keys that must be masked in status responses
to prevent leaking S3 credentials through API responses or logs."""

_CREDENTIAL_MASK: str = "********"
"""Replacement value for masked credentials."""


# ---------------------------------------------------------------------------
# Custom Exception Classes
# ---------------------------------------------------------------------------


class BlobStoreError(Exception):
    """Base exception for all BlobStore service errors.

    All BlobStore-related exceptions inherit from this class, allowing
    consumers to catch any service error with a single ``except`` clause.

    Attributes:
        message:        Human-readable error description.
        blobstore_name: Name of the BlobStore involved (if applicable).
    """

    def __init__(
        self,
        message: str,
        blobstore_name: str | None = None,
    ) -> None:
        self.message: str = message
        self.blobstore_name: str | None = blobstore_name
        super().__init__(message)


class BlobStoreInUseError(BlobStoreError):
    """Raised when attempting to delete a BlobStore that has dependent
    repositories.

    Repositories must be migrated to a different BlobStore before the
    current one can be safely removed.

    Attributes:
        message:        Human-readable error description.
        blobstore_name: Name of the BlobStore that is still in use.
    """
    pass


class BlobStoreNotFoundError(BlobStoreError):
    """Raised when a requested BlobStore configuration does not exist.

    Attributes:
        message:        Human-readable error description.
        blobstore_name: Name of the BlobStore that was not found.
    """
    pass


class BlobStoreConfigError(BlobStoreError):
    """Raised for invalid BlobStore configuration values.

    This includes unsupported types, missing required configuration keys,
    invalid name formats, and duplicate names.

    Attributes:
        message:        Human-readable error description.
        blobstore_name: Name of the BlobStore with the invalid config.
    """
    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "BlobStoreService",
    "BlobStoreError",
    "BlobStoreInUseError",
    "BlobStoreNotFoundError",
    "BlobStoreConfigError",
    "BLOBSTORE_TYPES",
    "DEFAULT_BLOBSTORE_NAME",
]


# ===========================================================================
# BlobStoreService
# ===========================================================================


class BlobStoreService:
    """Central service for BlobStore configuration management and lifecycle.

    This service manages the full lifecycle of BlobStore backends:

    1. **CRUD Operations** — Create, read, update, and delete BlobStore
       configurations persisted in the database via
       :class:`~src.app.models.blobstore_config.BlobStoreConfig`.
    2. **Runtime Lifecycle** — Initialise, cache, and shut down runtime
       :class:`~src.app.storage.blobstore.BlobStore` instances for active
       storage backends.
    3. **Metrics & Status** — Collect and expose storage usage statistics
       for health monitoring (Feature F-401) and the administration API.
    4. **Maintenance** — Delegate compaction, integrity checks, and temp
       cleanup to :class:`~src.app.storage.maintenance.BlobStoreMaintenanceService`
       (Feature F-203).
    5. **Default Setup** — Ensure the ``'default'`` BlobStore exists at
       application startup.

    **Thread Safety:**
    Active store instances are cached in ``_active_stores`` (a ``dict``).
    In the standard Flask deployment model (single-threaded dev server or
    Gunicorn with sync workers), concurrent access to this cache is not
    expected.  For async or multi-threaded workers, the caller is responsible
    for external synchronisation.

    Usage::

        service = BlobStoreService()
        service.ensure_default_blobstore()
        service.initialize_all_stores()

        config = service.create_blobstore(
            name='s3-production',
            store_type='s3',
            configuration={
                'bucket': 'nexus-blobs',
                'region': 'us-east-1',
            },
        )

        store = service.get_active_store('s3-production')
    """

    def __init__(self) -> None:
        """Initialise the BlobStoreService.

        Sets up:
        - A module-scoped logger for structured logging.
        - An empty runtime cache for active :class:`BlobStore` instances.
        - A lazily-initialised maintenance service reference.
        """
        self.logger: logging.Logger = logging.getLogger(__name__)
        self._active_stores: dict[str, BlobStore] = {}
        self._maintenance_service: BlobStoreMaintenanceService | None = None

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _get_maintenance_service(self) -> BlobStoreMaintenanceService:
        """Return the maintenance service, creating it lazily if needed.

        The maintenance service is initialised with the current active
        stores registry so that it can perform operations on any
        registered BlobStore.

        Returns:
            A :class:`BlobStoreMaintenanceService` instance with all
            currently active stores registered.
        """
        if self._maintenance_service is None:
            self._maintenance_service = BlobStoreMaintenanceService(
                blobstore_registry=dict(self._active_stores),
            )
            self.logger.debug(
                "BlobStoreMaintenanceService initialised with %d store(s).",
                len(self._active_stores),
            )
        return self._maintenance_service

    def _sync_maintenance_registry(self) -> None:
        """Synchronise the maintenance service's BlobStore registry with
        the current active stores cache.

        Called after any change to ``_active_stores`` (add, remove,
        reinitialise) to ensure the maintenance service has an up-to-date
        view of available backends.
        """
        if self._maintenance_service is not None:
            for name, store in self._active_stores.items():
                self._maintenance_service.register_blobstore(name, store)

    @staticmethod
    def _validate_name(name: str) -> None:
        """Validate a BlobStore name for format compliance.

        Args:
            name: The candidate BlobStore name.

        Raises:
            BlobStoreConfigError: If the name is empty, too long, or
                contains invalid characters.
        """
        if not name or not name.strip():
            raise BlobStoreConfigError(
                "BlobStore name must not be empty.",
                blobstore_name=name,
            )

        if len(name) > _NAME_MAX_LENGTH:
            raise BlobStoreConfigError(
                f"BlobStore name exceeds maximum length of "
                f"{_NAME_MAX_LENGTH} characters.",
                blobstore_name=name,
            )

        if not _NAME_PATTERN.match(name):
            raise BlobStoreConfigError(
                f"BlobStore name '{name}' is invalid. Names must start "
                f"with an alphanumeric character and contain only "
                f"alphanumeric characters, hyphens, and underscores.",
                blobstore_name=name,
            )

    @staticmethod
    def _validate_type(store_type: str) -> None:
        """Validate a BlobStore type identifier.

        Args:
            store_type: The candidate type string.

        Raises:
            BlobStoreConfigError: If the type is not in
                :data:`BLOBSTORE_TYPES`.
        """
        if store_type not in BLOBSTORE_TYPES:
            raise BlobStoreConfigError(
                f"Unsupported BlobStore type: '{store_type}'. "
                f"Supported types are: {', '.join(sorted(BLOBSTORE_TYPES))}.",
            )

    @staticmethod
    def _validate_file_config(configuration: dict[str, Any]) -> None:
        """Validate configuration for a ``file``-type BlobStore.

        Required keys:
            - ``path``: Directory path for blob storage.

        Args:
            configuration: The configuration dictionary.

        Raises:
            BlobStoreConfigError: If required keys are missing or invalid.
        """
        path: Any = configuration.get("path")
        if not path or not isinstance(path, str) or not path.strip():
            raise BlobStoreConfigError(
                "File BlobStore configuration requires a non-empty 'path' "
                "key specifying the directory path for blob storage.",
            )

    @staticmethod
    def _validate_s3_config(configuration: dict[str, Any]) -> None:
        """Validate configuration for an ``s3``-type BlobStore.

        Required keys:
            - ``bucket``: S3 bucket name.
            - ``region``: AWS region identifier.

        Optional keys:
            - ``prefix``, ``access_key_id``/``accessKeyId``,
              ``secret_access_key``/``secretAccessKey``, ``endpoint``,
              ``encryption``, ``force_path_style``/``forcePathStyle``.

        Args:
            configuration: The configuration dictionary.

        Raises:
            BlobStoreConfigError: If required keys are missing or invalid.
        """
        bucket: Any = configuration.get("bucket")
        if not bucket or not isinstance(bucket, str) or not bucket.strip():
            raise BlobStoreConfigError(
                "S3 BlobStore configuration requires a non-empty 'bucket' "
                "key specifying the S3 bucket name.",
            )

        region: Any = configuration.get("region")
        if not region or not isinstance(region, str) or not region.strip():
            raise BlobStoreConfigError(
                "S3 BlobStore configuration requires a non-empty 'region' "
                "key specifying the AWS region.",
            )

    def _validate_configuration(
        self,
        store_type: str,
        configuration: dict[str, Any],
    ) -> None:
        """Validate configuration based on the BlobStore type.

        Dispatches to type-specific validators:
        - ``'file'`` → :meth:`_validate_file_config`
        - ``'s3'`` → :meth:`_validate_s3_config`

        Args:
            store_type: The BlobStore type identifier.
            configuration: The configuration dictionary.

        Raises:
            BlobStoreConfigError: If the configuration is invalid for
                the given type.
        """
        if not isinstance(configuration, dict):
            raise BlobStoreConfigError(
                "BlobStore configuration must be a dictionary.",
            )

        if store_type == "file":
            self._validate_file_config(configuration)
        elif store_type == "s3":
            self._validate_s3_config(configuration)

    @staticmethod
    def _mask_sensitive_config(
        configuration: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Create a deep copy of configuration with sensitive keys masked.

        Masks S3 credential fields (access keys, secret keys) and nested
        encryption keys to prevent leaking secrets through API responses
        or log output.

        Args:
            configuration: Raw configuration dictionary (may be ``None``).

        Returns:
            A deep-copied dictionary with sensitive values replaced by
            ``_CREDENTIAL_MASK``, or ``None`` if input was ``None``.
        """
        if configuration is None:
            return None

        masked: dict[str, Any] = copy.deepcopy(configuration)

        # Mask top-level sensitive keys
        for key in _SENSITIVE_CONFIG_KEYS:
            if key in masked and masked[key]:
                masked[key] = _CREDENTIAL_MASK

        # Mask nested encryption key if present (S3 SSE)
        encryption: Any = masked.get("encryption")
        if isinstance(encryption, dict) and encryption.get("key") is not None:
            encryption["key"] = _CREDENTIAL_MASK

        return masked

    # ------------------------------------------------------------------
    # Phase 4: BlobStore CRUD Operations
    # ------------------------------------------------------------------

    def create_blobstore(
        self,
        name: str,
        store_type: str,
        configuration: dict[str, Any],
    ) -> BlobStoreConfig:
        """Create a new BlobStore configuration and initialise its runtime
        backend instance.

        Validates the name, type, and type-specific configuration before
        persisting to the database and starting the runtime BlobStore.

        Args:
            name:          Unique BlobStore name (alphanumeric, hyphens,
                           underscores; max 200 chars).
            store_type:    Backend type — ``'file'`` or ``'s3'``.
            configuration: Type-specific configuration dictionary.

        Returns:
            The newly created :class:`BlobStoreConfig` model instance.

        Raises:
            BlobStoreConfigError: If the name, type, or configuration is
                invalid, or if a BlobStore with the given name already
                exists.
        """
        # -- Step 1: Validate inputs ----------------------------------------
        self._validate_name(name)
        self._validate_type(store_type)
        self._validate_configuration(store_type, configuration)

        # -- Step 2: Check uniqueness ---------------------------------------
        existing: BlobStoreConfig | None = db.session.query(
            BlobStoreConfig
        ).filter_by(blob_store_name=name).first()
        if existing is not None:
            raise BlobStoreConfigError(
                f"A BlobStore named '{name}' already exists.",
                blobstore_name=name,
            )

        # -- Step 3: Persist the configuration ------------------------------
        config = BlobStoreConfig(
            blob_store_name=name,
            type=store_type,
            configuration=configuration,
            total_size=0,
        )
        db.session.add(config)
        db.session.commit()

        self.logger.info(
            "BlobStore configuration created: name='%s', type='%s'.",
            name,
            store_type,
        )

        # -- Step 4: Initialise the runtime BlobStore instance --------------
        try:
            store: BlobStore = create_blobstore_from_model(config)
            store.start()
            self._active_stores[name] = store
            self._sync_maintenance_registry()

            self.logger.info(
                "Runtime BlobStore '%s' initialised and cached.", name,
            )
        except Exception as exc:
            # Log but don't roll back the DB config — the store can be
            # reinitialised later via get_active_store().
            self.logger.warning(
                "BlobStore '%s' config saved but runtime initialisation "
                "failed: %s.  Will retry on next access.",
                name,
                str(exc),
            )

        # -- Step 5: Emit creation event for audit logging (F-303) ----------
        operation_id: str = generate_uuid()
        emit_event(
            EventType.CONFIG_CHANGED,
            payload={
                "operation_id": operation_id,
                "operation": "blobstore.created",
                "blobstore_name": name,
                "store_type": store_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        return config

    def get_blobstore(self, name: str) -> BlobStoreConfig | None:
        """Retrieve a BlobStore configuration by name.

        Args:
            name: The unique BlobStore name to look up.

        Returns:
            The :class:`BlobStoreConfig` model instance, or ``None`` if
            no BlobStore with the given name exists.
        """
        return db.session.query(
            BlobStoreConfig
        ).filter_by(blob_store_name=name).first()

    def get_blobstore_or_raise(self, name: str) -> BlobStoreConfig:
        """Retrieve a BlobStore configuration by name, raising if not found.

        Args:
            name: The unique BlobStore name to look up.

        Returns:
            The :class:`BlobStoreConfig` model instance.

        Raises:
            BlobStoreNotFoundError: If no BlobStore with the given name
                exists.
        """
        config: BlobStoreConfig | None = self.get_blobstore(name)
        if config is None:
            raise BlobStoreNotFoundError(
                f"BlobStore '{name}' not found.",
                blobstore_name=name,
            )
        return config

    def list_blobstores(self) -> list[BlobStoreConfig]:
        """Return all BlobStore configurations ordered by name.

        Returns:
            A list of :class:`BlobStoreConfig` instances ordered
            alphabetically by ``blob_store_name``.
        """
        return (
            db.session.query(BlobStoreConfig)
            .order_by(BlobStoreConfig.blob_store_name)
            .all()
        )

    def update_blobstore(
        self,
        name: str,
        configuration: dict[str, Any],
    ) -> BlobStoreConfig:
        """Update the configuration of an existing BlobStore.

        Validates the new configuration against the existing store type,
        updates the database record, and reinitialises the runtime
        BlobStore instance.

        Args:
            name:          Name of the BlobStore to update.
            configuration: New configuration dictionary (replaces existing).

        Returns:
            The updated :class:`BlobStoreConfig` model instance.

        Raises:
            BlobStoreNotFoundError: If no BlobStore with the given name
                exists.
            BlobStoreConfigError: If the new configuration is invalid for
                the store's type.
        """
        # -- Step 1: Retrieve and validate ----------------------------------
        config: BlobStoreConfig = self.get_blobstore_or_raise(name)
        self._validate_configuration(config.type, configuration)

        # -- Step 2: Capture old config for event payload -------------------
        old_configuration: dict[str, Any] = (
            copy.deepcopy(config.configuration) if config.configuration else {}
        )

        # -- Step 3: Update the configuration in the database ---------------
        config.configuration = configuration
        db.session.add(config)
        db.session.commit()

        self.logger.info(
            "BlobStore configuration updated: name='%s', type='%s'.",
            name,
            config.type,
        )

        # -- Step 4: Reinitialise runtime BlobStore if it was active --------
        if name in self._active_stores:
            try:
                old_store: BlobStore = self._active_stores[name]
                try:
                    old_store.stop()
                except Exception as stop_exc:
                    self.logger.warning(
                        "Error stopping old BlobStore '%s' during update: %s",
                        name,
                        str(stop_exc),
                    )

                new_store: BlobStore = create_blobstore_from_model(config)
                new_store.start()
                self._active_stores[name] = new_store
                self._sync_maintenance_registry()

                self.logger.info(
                    "Runtime BlobStore '%s' reinitialised after config update.",
                    name,
                )
            except Exception as exc:
                self.logger.warning(
                    "BlobStore '%s' config updated but runtime "
                    "reinitialisation failed: %s.  Will retry on next access.",
                    name,
                    str(exc),
                )
                # Remove stale entry so next access triggers fresh init
                self._active_stores.pop(name, None)

        # -- Step 5: Emit update event for audit logging (F-303) ------------
        operation_id: str = generate_uuid()
        emit_event(
            EventType.CONFIG_CHANGED,
            payload={
                "operation_id": operation_id,
                "operation": "blobstore.updated",
                "blobstore_name": name,
                "store_type": config.type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        return config

    def delete_blobstore(self, name: str) -> bool:
        """Delete a BlobStore configuration and shut down its runtime
        instance.

        **Safety Checks:**
        1. The default BlobStore (``DEFAULT_BLOBSTORE_NAME``) cannot be
           deleted — it is the system fallback.
        2. A BlobStore that has dependent repositories cannot be deleted.
           Repositories must be migrated to another BlobStore first.

        Args:
            name: Name of the BlobStore to delete.

        Returns:
            ``True`` if the BlobStore was successfully deleted.

        Raises:
            BlobStoreConfigError: If attempting to delete the default
                BlobStore.
            BlobStoreNotFoundError: If no BlobStore with the given name
                exists.
            BlobStoreInUseError: If repositories depend on this BlobStore.
        """
        # -- Step 1: Prevent deletion of the default BlobStore ---------------
        if name == DEFAULT_BLOBSTORE_NAME:
            raise BlobStoreConfigError(
                f"Cannot delete the default BlobStore '{DEFAULT_BLOBSTORE_NAME}'. "
                f"The default BlobStore is the system fallback and must always "
                f"exist.",
                blobstore_name=name,
            )

        # -- Step 2: Verify the BlobStore exists ----------------------------
        config: BlobStoreConfig = self.get_blobstore_or_raise(name)

        # -- Step 3: Check for dependent repositories -----------------------
        dependent_repos = Repository.query.filter_by(
            blob_store_name=name
        ).all()
        if dependent_repos:
            repo_names: list[str] = [
                getattr(r, "name", str(r)) for r in dependent_repos
            ]
            raise BlobStoreInUseError(
                f"BlobStore '{name}' is in use by "
                f"{len(dependent_repos)} repository(ies): "
                f"{', '.join(repo_names[:5])}"
                f"{'...' if len(repo_names) > 5 else ''}. "
                f"Migrate repositories before deleting.",
                blobstore_name=name,
            )

        # -- Step 4: Shut down runtime BlobStore instance -------------------
        if name in self._active_stores:
            try:
                self._active_stores[name].stop()
                self.logger.info(
                    "Runtime BlobStore '%s' stopped for deletion.", name,
                )
            except Exception as exc:
                self.logger.warning(
                    "Error stopping BlobStore '%s' during deletion: %s. "
                    "Proceeding with config removal.",
                    name,
                    str(exc),
                )
            finally:
                self._active_stores.pop(name, None)

        # -- Step 5: Remove from database -----------------------------------
        db.session.delete(config)
        db.session.commit()

        self.logger.info(
            "BlobStore configuration deleted: name='%s', type='%s'.",
            name,
            config.type,
        )

        # -- Step 6: Emit deletion event for audit logging (F-303) ----------
        operation_id: str = generate_uuid()
        emit_event(
            EventType.CONFIG_CHANGED,
            payload={
                "operation_id": operation_id,
                "operation": "blobstore.deleted",
                "blobstore_name": name,
                "store_type": config.type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        return True

    # ------------------------------------------------------------------
    # Phase 5: Runtime BlobStore Access
    # ------------------------------------------------------------------

    def get_active_store(self, name: str) -> BlobStore:
        """Get or create the runtime BlobStore instance for a given name.

        Checks the in-memory cache first.  If not cached, loads the
        :class:`BlobStoreConfig` from the database, creates a new
        :class:`BlobStore` instance via the factory, starts it, and
        caches it for subsequent access.

        Args:
            name: The BlobStore name to retrieve.

        Returns:
            A started :class:`BlobStore` instance ready for I/O.

        Raises:
            BlobStoreNotFoundError: If no BlobStore configuration with
                the given name exists.
        """
        # Fast path: return cached instance
        if name in self._active_stores:
            return self._active_stores[name]

        # Slow path: load from DB and initialise
        config: BlobStoreConfig = self.get_blobstore_or_raise(name)

        store: BlobStore = create_blobstore_from_model(config)
        store.start()
        self._active_stores[name] = store
        self._sync_maintenance_registry()

        self.logger.info(
            "Runtime BlobStore '%s' (type=%s) initialised on demand.",
            name,
            config.type,
        )
        return store

    def get_store_for_repository(self, repository_name: str) -> BlobStore:
        """Get the BlobStore instance assigned to a specific repository.

        Resolves the BlobStore name from the repository's
        ``blob_store_name`` column and delegates to
        :meth:`get_active_store`.

        This is a convenience method used by upload_manager,
        proxy_service, and other services that need storage access for a
        specific repository.

        Args:
            repository_name: The repository name (primary key).

        Returns:
            A started :class:`BlobStore` instance for the repository's
            configured backend.

        Raises:
            BlobStoreNotFoundError: If the repository does not exist or
                its assigned BlobStore configuration cannot be found.
        """
        repo: Repository | None = Repository.query.filter_by(
            name=repository_name
        ).first()
        if repo is None:
            raise BlobStoreNotFoundError(
                f"Repository '{repository_name}' not found. Cannot resolve "
                f"BlobStore assignment.",
                blobstore_name=None,
            )

        blob_store_name: str = repo.blob_store_name
        if not blob_store_name:
            raise BlobStoreNotFoundError(
                f"Repository '{repository_name}' has no assigned BlobStore.",
                blobstore_name=None,
            )

        return self.get_active_store(blob_store_name)

    def initialize_all_stores(self) -> None:
        """Load all BlobStore configurations and initialise runtime
        instances.

        Called during application startup (from ``factory.py``).  Each
        configured BlobStore is instantiated via the factory, started,
        and cached.  Failures for individual stores are logged but do
        not prevent other stores from initialising.
        """
        configs: list[BlobStoreConfig] = (
            db.session.query(BlobStoreConfig)
            .order_by(BlobStoreConfig.blob_store_name)
            .all()
        )

        self.logger.info(
            "Initialising %d BlobStore(s) from database configuration.",
            len(configs),
        )

        for config in configs:
            name: str = config.blob_store_name
            try:
                store: BlobStore = create_blobstore_from_model(config)
                store.start()
                self._active_stores[name] = store

                self.logger.info(
                    "BlobStore '%s' (type=%s) initialised successfully.",
                    name,
                    config.type,
                )
            except Exception as exc:
                self.logger.error(
                    "Failed to initialise BlobStore '%s' (type=%s): %s. "
                    "Will retry on first access.",
                    name,
                    config.type,
                    str(exc),
                    exc_info=True,
                )

        # Sync the maintenance service with newly initialised stores
        self._sync_maintenance_registry()

        self.logger.info(
            "BlobStore initialisation complete: %d of %d store(s) active.",
            len(self._active_stores),
            len(configs),
        )

    def shutdown_all_stores(self) -> None:
        """Gracefully shut down all active BlobStore instances.

        Iterates through all cached :class:`BlobStore` instances and
        calls their ``stop()`` method to close connections, flush pending
        writes, and release resources.  Called during application
        shutdown.

        Failures for individual stores are logged but do not prevent
        shutdown of remaining stores.
        """
        store_names: list[str] = list(self._active_stores.keys())

        self.logger.info(
            "Shutting down %d active BlobStore(s).", len(store_names),
        )

        for name in store_names:
            try:
                self._active_stores[name].stop()
                self.logger.info(
                    "BlobStore '%s' stopped gracefully.", name,
                )
            except Exception as exc:
                self.logger.error(
                    "Error stopping BlobStore '%s': %s",
                    name,
                    str(exc),
                    exc_info=True,
                )

        self._active_stores.clear()
        self._maintenance_service = None

        self.logger.info("All BlobStore instances shut down.")

    # ------------------------------------------------------------------
    # Phase 6: BlobStore Metrics and Status
    # ------------------------------------------------------------------

    def get_blobstore_status(self, name: str) -> dict[str, Any]:
        """Return status and metrics for a specific BlobStore.

        Combines the database configuration with live runtime metrics
        (if the store is active).  Sensitive configuration fields (S3
        credentials) are automatically masked.

        Args:
            name: The BlobStore name.

        Returns:
            A dictionary containing BlobStore status information::

                {
                    'name': str,
                    'type': str,
                    'available': bool,
                    'metrics': {
                        'total_size': int,
                        'blob_count': int,
                        'available_space': int,
                    },
                    'configuration': dict,  # masked
                }

        Raises:
            BlobStoreNotFoundError: If the BlobStore does not exist.
        """
        config: BlobStoreConfig = self.get_blobstore_or_raise(name)

        # Attempt to get live metrics from the runtime store
        available: bool = False
        metrics_dict: dict[str, Any] = {
            "total_size": config.total_size or 0,
            "blob_count": config.blob_count or 0,
            "available_space": config.available_space if config.available_space is not None else -1,
        }

        if name in self._active_stores:
            try:
                store: BlobStore = self._active_stores[name]
                runtime_metrics: BlobStoreMetrics = store.get_metrics()
                metrics_dict = {
                    "total_size": runtime_metrics.total_size_bytes,
                    "blob_count": runtime_metrics.blob_count,
                    "available_space": runtime_metrics.available_space_bytes,
                }
                available = True
            except Exception as exc:
                self.logger.warning(
                    "Failed to get live metrics for BlobStore '%s': %s. "
                    "Falling back to database values.",
                    name,
                    str(exc),
                )

        return {
            "name": name,
            "type": config.type,
            "available": available,
            "metrics": metrics_dict,
            "configuration": self._mask_sensitive_config(
                config.configuration,
            ),
        }

    def get_all_blobstore_status(self) -> list[dict[str, Any]]:
        """Return status for all configured BlobStores.

        Iterates over all :class:`BlobStoreConfig` entries and calls
        :meth:`get_blobstore_status` for each.

        Returns:
            A list of status dictionaries, one per configured BlobStore,
            ordered alphabetically by name.
        """
        configs: list[BlobStoreConfig] = self.list_blobstores()
        results: list[dict[str, Any]] = []

        for config in configs:
            try:
                status: dict[str, Any] = self.get_blobstore_status(
                    config.blob_store_name,
                )
                results.append(status)
            except Exception as exc:
                self.logger.warning(
                    "Error retrieving status for BlobStore '%s': %s",
                    config.blob_store_name,
                    str(exc),
                )
                # Include an error entry so the caller knows about the store
                results.append({
                    "name": config.blob_store_name,
                    "type": config.type,
                    "available": False,
                    "metrics": {
                        "total_size": 0,
                        "blob_count": 0,
                        "available_space": -1,
                    },
                    "configuration": None,
                    "error": str(exc),
                })

        return results

    def get_blobstore_metrics(self, name: str) -> BlobStoreMetrics:
        """Get metrics from the runtime BlobStore instance.

        Returns the :class:`BlobStoreMetrics` dataclass from the storage
        layer, providing detailed statistics including soft-deleted blob
        counts, space warnings, and usage percentages.

        Args:
            name: The BlobStore name.

        Returns:
            A :class:`BlobStoreMetrics` instance with current statistics.

        Raises:
            BlobStoreNotFoundError: If the BlobStore does not exist or
                its runtime instance is not available.
        """
        store: BlobStore = self.get_active_store(name)
        return store.get_metrics()

    def update_blobstore_size(self, name: str, size_delta: int) -> None:
        """Update the ``total_size`` in BlobStoreConfig after blob operations.

        Performs an atomic increment or decrement on the stored
        ``total_size`` value.  This method is typically called by the
        storage layer after successful blob create or delete operations.

        Args:
            name:       The BlobStore name.
            size_delta: Number of bytes to add (positive) or subtract
                        (negative) from the current total size.

        Raises:
            BlobStoreNotFoundError: If the BlobStore does not exist.
        """
        config: BlobStoreConfig = self.get_blobstore_or_raise(name)
        config.total_size = max(0, (config.total_size or 0) + size_delta)
        db.session.add(config)
        db.session.commit()

        self.logger.debug(
            "BlobStore '%s' size updated: delta=%+d, new_total=%d.",
            name,
            size_delta,
            config.total_size,
        )

    # ------------------------------------------------------------------
    # Phase 7: BlobStore Maintenance Integration (Feature F-203)
    # ------------------------------------------------------------------

    def run_compaction(self, name: str) -> dict[str, Any]:
        """Trigger compaction on a specific BlobStore.

        Delegates to
        :meth:`~src.app.storage.maintenance.BlobStoreMaintenanceService.compact`
        which permanently removes soft-deleted blobs that have exceeded
        the retention period.

        After compaction, a ``BLOBSTORE_COMPACTED`` event is emitted for
        audit logging (F-303) and monitoring (F-401).

        Args:
            name: The BlobStore name to compact.

        Returns:
            Compaction summary dictionary::

                {
                    'blobs_removed': int,
                    'space_reclaimed': int,
                    ...
                }
        """
        self.logger.info("Starting compaction for BlobStore '%s'.", name)

        # Ensure the store is active so maintenance can operate on it
        self.get_active_store(name)

        maintenance: BlobStoreMaintenanceService = (
            self._get_maintenance_service()
        )
        result: dict[str, Any] = maintenance.compact(name)

        # The maintenance service already emits BLOBSTORE_COMPACTED events
        # internally, but we emit an additional service-level event with
        # the operation correlation ID for request tracing.
        operation_id: str = generate_uuid()
        emit_event(
            EventType.BLOBSTORE_COMPACTED,
            payload={
                "operation_id": operation_id,
                "blobstore_name": name,
                "blobs_removed": result.get("blobs_removed", 0),
                "space_reclaimed_bytes": result.get(
                    "space_reclaimed_bytes", 0,
                ),
                "duration_ms": result.get("duration_ms", 0),
                "status": result.get("status", "unknown"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        self.logger.info(
            "Compaction completed for BlobStore '%s': removed=%d blobs, "
            "reclaimed=%d bytes.",
            name,
            result.get("blobs_removed", 0),
            result.get("space_reclaimed_bytes", 0),
        )

        return result

    def run_integrity_check(self, name: str) -> dict[str, Any]:
        """Run an integrity check on a specific BlobStore.

        Delegates to
        :meth:`~src.app.storage.maintenance.BlobStoreMaintenanceService.verify_integrity`
        which validates stored checksums against recomputed values to
        detect data corruption.

        This is a **read-only** operation — no data is modified or
        deleted.

        Args:
            name: The BlobStore name to check.

        Returns:
            Integrity check summary dictionary::

                {
                    'blobs_checked': int,
                    'errors_found': int,
                    'details': [...],
                    ...
                }
        """
        self.logger.info(
            "Starting integrity check for BlobStore '%s'.", name,
        )

        # Ensure the store is active so maintenance can operate on it
        self.get_active_store(name)

        maintenance: BlobStoreMaintenanceService = (
            self._get_maintenance_service()
        )
        result: dict[str, Any] = maintenance.verify_integrity(name)

        self.logger.info(
            "Integrity check completed for BlobStore '%s': "
            "checked=%d, errors=%d.",
            name,
            result.get("blobs_checked", 0),
            result.get("errors_found", 0),
        )

        return result

    def cleanup_temp_blobs(self, name: str) -> dict[str, Any]:
        """Clean up orphaned temporary blobs for a specific BlobStore.

        Delegates to
        :meth:`~src.app.storage.maintenance.BlobStoreMaintenanceService.cleanup_temp_files`
        which removes staging files left behind by interrupted write
        operations.

        Args:
            name: The BlobStore name to clean up.

        Returns:
            Cleanup summary dictionary::

                {
                    'temp_blobs_removed': int,
                    ...
                }
        """
        self.logger.info(
            "Starting temp blob cleanup for BlobStore '%s'.", name,
        )

        # Ensure the store is active so maintenance can operate on it
        self.get_active_store(name)

        maintenance: BlobStoreMaintenanceService = (
            self._get_maintenance_service()
        )
        result: dict[str, Any] = maintenance.cleanup_temp_files(name)

        # Normalize the result key to match our API contract
        temp_blobs_removed: int = result.get("files_removed", 0)
        result["temp_blobs_removed"] = temp_blobs_removed

        self.logger.info(
            "Temp blob cleanup completed for BlobStore '%s': removed=%d.",
            name,
            temp_blobs_removed,
        )

        return result

    # ------------------------------------------------------------------
    # Phase 8: Default BlobStore Setup
    # ------------------------------------------------------------------

    def ensure_default_blobstore(self) -> None:
        """Ensure the default BlobStore exists.

        Called during application initialisation (from ``factory.py``).
        If a BlobStore named :data:`DEFAULT_BLOBSTORE_NAME` does not yet
        exist, a ``file``-type BlobStore is created using the path from
        ``BLOBSTORE_DEFAULT_PATH`` in the Flask app configuration
        (defaulting to ``'./data/blobs'``).

        If the default BlobStore already exists, this method is a no-op.
        """
        existing: BlobStoreConfig | None = self.get_blobstore(
            DEFAULT_BLOBSTORE_NAME,
        )
        if existing is not None:
            self.logger.info(
                "Default BlobStore '%s' already exists (type=%s).",
                DEFAULT_BLOBSTORE_NAME,
                existing.type,
            )
            return

        default_path: str = current_app.config.get(
            "BLOBSTORE_DEFAULT_PATH", "./data/blobs",
        )

        self.logger.info(
            "Creating default BlobStore '%s' with path '%s'.",
            DEFAULT_BLOBSTORE_NAME,
            default_path,
        )

        self.create_blobstore(
            name=DEFAULT_BLOBSTORE_NAME,
            store_type="file",
            configuration={"path": default_path},
        )

        self.logger.info(
            "Default BlobStore '%s' created successfully.",
            DEFAULT_BLOBSTORE_NAME,
        )


# ---------------------------------------------------------------------------
# Module load logging
# ---------------------------------------------------------------------------
logger.debug(
    "BlobStoreService module loaded — BLOBSTORE_TYPES=%s, "
    "DEFAULT_BLOBSTORE_NAME='%s'.",
    BLOBSTORE_TYPES,
    DEFAULT_BLOBSTORE_NAME,
)
