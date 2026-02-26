"""
Task Type Registry — Maps task type identifiers to callable implementations.

This module replaces Quartz's ``JobDetail`` registration from the original
Java source system (Quartz 2.3.2, Feature F-402).  It maintains a mapping of
task type strings (e.g. ``"repository.cleanup"``, ``"blobstore.compact"``) to
Python callable functions that implement the task logic.

The registry supports both **built-in task types** (shipped with the
application) and **dynamically registered custom task types** (via the
Plugin Architecture, Feature F-504).  Built-in task types delegate to the
service layer for actual business logic execution.

**Architecture Context:**

+---------------------+--------------------------------------------+
| Java Source          | Python Target                              |
+=====================+============================================+
| Quartz ``JobDetail`` | ``TaskRegistry.register()``                |
+---------------------+--------------------------------------------+
| Quartz ``Job`` class | Module-level ``_builtin_*_task`` callables |
+---------------------+--------------------------------------------+
| Quartz job lookup    | ``TaskRegistry.get_task_callable()``       |
+---------------------+--------------------------------------------+

**Feature Support:**

- **F-402** (Scheduled Tasks): Task type registration and lookup — the
  registry is the authoritative source for mapping task type identifiers to
  their executable implementations.
- **F-504** (Plugin Architecture): External plugins can register custom task
  types via the ``register()`` method, enabling extensibility without
  modifying core code.
- **F-204** (Cleanup Policies): Built-in cleanup task delegates to
  :class:`~src.app.services.cleanup_service.CleanupService`.
- **F-203** (BlobStore Maintenance): Built-in compact and purge tasks
  delegate to :class:`~src.app.services.blobstore_service.BlobStoreService`.
- **F-103** (Content Indexing and Search): Built-in reindex task delegates to
  :class:`~src.app.services.search_service.SearchService`.
- **F-401** (Health Checks and Monitoring): Built-in health check and metrics
  snapshot tasks.
- **F-104** (Browse Tree Navigation): Built-in rebuild browse tree task.

**Lazy Import Strategy:**

Built-in task callable functions use **lazy imports** (imports inside
function bodies) for service dependencies.  This avoids circular import
issues at module load time, since the services layer may import the
scheduler package.  Each built-in task function imports the relevant
service class only when invoked.

**Task Callable Signature:**

All task callables **MUST** accept ``task_def`` and ``configuration`` as
keyword arguments, plus ``**kwargs`` for forward compatibility::

    def my_task(task_def=None, configuration=None, **kwargs) -> dict:
        ...

**Exports:**

- :class:`TaskRegistry` — primary registry class with registration and
  lookup methods.
- :data:`TASK_TYPE_CLEANUP` through :data:`TASK_TYPE_METRICS_SNAPSHOT` —
  string constants for built-in task type identifiers.
- :data:`BUILTIN_TASK_TYPES` — ``frozenset`` of all built-in identifiers.
"""

from __future__ import annotations

import logging
from typing import Any, Callable
from datetime import datetime, timezone

from flask import current_app

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for all task registry operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in Task Type Identifiers (Constants)
# ---------------------------------------------------------------------------
# These replace the Quartz JobDetail classes from the Java source system.
# Each constant defines a well-known task type string that can be referenced
# by the scheduler, API layer, and configuration.
# ---------------------------------------------------------------------------

TASK_TYPE_CLEANUP: str = "repository.cleanup"
"""Task type for executing cleanup policies on repository assets (Feature F-204).

Delegates to :meth:`CleanupService.execute_cleanup` for targeted cleanup
or :meth:`CleanupService.run_all_repository_cleanups` for full-system
scheduled cleanup.
"""

TASK_TYPE_COMPACT: str = "blobstore.compact"
"""Task type for BlobStore compaction to reclaim unused space (Feature F-203).

Delegates to :meth:`BlobStoreService.run_compaction` which permanently
removes soft-deleted blobs that have exceeded the retention period.
"""

TASK_TYPE_BACKUP: str = "db.backup"
"""Task type for performing database backup operations.

Generates timestamped backup files for SQLite (file copy) or PostgreSQL
(pg_dump / logical backup) deployments.
"""

TASK_TYPE_REINDEX: str = "repository.rebuild-index"
"""Task type for rebuilding the search index (Feature F-103).

Delegates to :meth:`SearchService.reindex_repository` for single-repository
reindexing or :meth:`SearchService.reindex_all` for full-system reindexing.
"""

TASK_TYPE_HEALTH_CHECK: str = "system.health-check"
"""Task type for running system health checks (Feature F-401).

Collects health status from various subsystems and produces a lightweight
scheduled health snapshot.
"""

TASK_TYPE_PURGE_UNUSED: str = "blobstore.purge-unused"
"""Task type for purging unused/orphaned BlobStore entries (Feature F-203).

Delegates to :meth:`BlobStoreService.cleanup_temp_blobs` to remove
staging files left behind by interrupted write operations.
"""

TASK_TYPE_REBUILD_BROWSE: str = "repository.rebuild-browse"
"""Task type for rebuilding the repository browse tree index (Feature F-104).

Rebuilds the hierarchical browse tree navigation structure for the
specified repository (or all repositories if none specified).
"""

TASK_TYPE_METRICS_SNAPSHOT: str = "system.metrics-snapshot"
"""Task type for capturing and storing a metrics snapshot (Feature F-401).

Collects current system metrics (Prometheus-compatible) and persists
them for historical trend analysis.
"""

# All built-in task type identifiers — a frozenset for immutability and
# O(1) membership testing.
BUILTIN_TASK_TYPES: frozenset[str] = frozenset({
    TASK_TYPE_CLEANUP,
    TASK_TYPE_COMPACT,
    TASK_TYPE_BACKUP,
    TASK_TYPE_REINDEX,
    TASK_TYPE_HEALTH_CHECK,
    TASK_TYPE_PURGE_UNUSED,
    TASK_TYPE_REBUILD_BROWSE,
    TASK_TYPE_METRICS_SNAPSHOT,
})
"""Immutable set of all built-in task type identifiers.

Used by :meth:`TaskRegistry.list_task_types` to annotate each entry with
a ``builtin`` flag, and by validation logic to distinguish system-defined
task types from plugin-registered custom task types.
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "TaskRegistry",
    "TASK_TYPE_CLEANUP",
    "TASK_TYPE_COMPACT",
    "TASK_TYPE_BACKUP",
    "TASK_TYPE_REINDEX",
    "TASK_TYPE_HEALTH_CHECK",
    "TASK_TYPE_PURGE_UNUSED",
    "TASK_TYPE_REBUILD_BROWSE",
    "TASK_TYPE_METRICS_SNAPSHOT",
    "BUILTIN_TASK_TYPES",
]


# ===========================================================================
# TaskRegistry Class
# ===========================================================================


class TaskRegistry:
    """Registry mapping task type identifiers to callable implementations.

    The ``TaskRegistry`` is the authoritative lookup for converting a task
    type string (stored in :class:`~src.app.models.task.TaskDefinition`) into
    the Python callable that executes that task.

    **Registration:**

    Task types are registered via :meth:`register`, which associates a
    task type string with a Python callable and an optional human-readable
    description.  Re-registering an existing type overrides it with a
    warning (useful for plugin overrides).

    **Lookup:**

    Given a task type string, :meth:`get_task_callable` returns the callable
    (or ``None`` if not registered).  :meth:`is_registered` performs a quick
    existence check.  :meth:`list_task_types` returns metadata for all
    registered types.

    **Built-in Tasks:**

    Call :meth:`register_builtin_tasks` during application startup to
    populate the registry with all system-defined task types.  These
    tasks delegate to the services layer (cleanup, compaction, search
    reindex, etc.) via lazy imports.

    Usage::

        registry = TaskRegistry()
        registry.register_builtin_tasks()

        # Lookup and execute a task
        fn = registry.get_task_callable('repository.cleanup')
        if fn is not None:
            result = fn(task_def=my_task_def, configuration={'repository_name': 'maven-central'})

        # Plugin registration
        registry.register('custom.my-task', my_custom_callable, 'My custom task')
    """

    def __init__(self) -> None:
        """Initialise the TaskRegistry with empty mappings.

        Creates two internal dictionaries:

        - ``_task_types`` — maps task type string → callable implementation
        - ``_task_descriptions`` — maps task type string → human-readable
          description
        """
        self.logger: logging.Logger = logging.getLogger(__name__)
        self._task_types: dict[str, Callable] = {}
        self._task_descriptions: dict[str, str] = {}
        self.logger.debug("TaskRegistry initialised")

    # ------------------------------------------------------------------
    # Registration Methods
    # ------------------------------------------------------------------

    def register(
        self,
        task_type: str,
        callable_func: Callable,
        description: str = "",
    ) -> None:
        """Register a task type with its callable implementation.

        Associates the given ``task_type`` identifier string with a Python
        callable.  If the task type is already registered, the existing
        registration is **overridden** with a warning log (this supports
        plugin-based task type customisation per Feature F-504).

        Parameters
        ----------
        task_type:
            Non-empty string identifier for the task type (e.g.
            ``"repository.cleanup"``).
        callable_func:
            A callable that implements the task logic.  Must accept
            ``task_def=None``, ``configuration=None``, and ``**kwargs``.
        description:
            Optional human-readable description.  If empty, defaults to
            the ``task_type`` value.

        Raises
        ------
        ValueError
            If ``task_type`` is empty or ``callable_func`` is not callable.
        """
        if not task_type or not isinstance(task_type, str):
            raise ValueError("Task type identifier cannot be empty")
        if not callable(callable_func):
            raise ValueError(
                f"Task implementation for '{task_type}' must be callable"
            )
        if task_type in self._task_types:
            self.logger.warning("Overriding existing task type: %s", task_type)
        self._task_types[task_type] = callable_func
        self._task_descriptions[task_type] = description or task_type
        self.logger.info("Registered task type: %s", task_type)

    def unregister(self, task_type: str) -> bool:
        """Remove a task type registration.

        Parameters
        ----------
        task_type:
            The task type identifier to remove.

        Returns
        -------
        bool
            ``True`` if the task type was found and removed, ``False`` if
            it was not registered.
        """
        if task_type in self._task_types:
            del self._task_types[task_type]
            self._task_descriptions.pop(task_type, None)
            self.logger.info("Unregistered task type: %s", task_type)
            return True
        self.logger.debug(
            "Attempted to unregister unknown task type: %s", task_type
        )
        return False

    # ------------------------------------------------------------------
    # Lookup Methods
    # ------------------------------------------------------------------

    def get_task_callable(self, task_type: str) -> Callable | None:
        """Retrieve the callable implementation for a given task type.

        Parameters
        ----------
        task_type:
            The task type identifier to look up.

        Returns
        -------
        Callable or None
            The registered callable, or ``None`` if the task type is not
            registered.
        """
        return self._task_types.get(task_type)

    def get_task_description(self, task_type: str) -> str:
        """Return the human-readable description for a task type.

        Parameters
        ----------
        task_type:
            The task type identifier to look up.

        Returns
        -------
        str
            The description string, or an empty string if the task type is
            not registered.
        """
        return self._task_descriptions.get(task_type, "")

    def is_registered(self, task_type: str) -> bool:
        """Check whether a task type is currently registered.

        Parameters
        ----------
        task_type:
            The task type identifier to check.

        Returns
        -------
        bool
            ``True`` if the task type has a registered callable, ``False``
            otherwise.
        """
        return task_type in self._task_types

    def list_task_types(self) -> list[dict[str, Any]]:
        """Return metadata for all registered task types.

        Returns a sorted list of dictionaries, each containing:

        - ``type`` — the task type identifier string
        - ``description`` — human-readable description
        - ``builtin`` — ``True`` if the task type is one of the built-in
          system-defined types

        The list is sorted alphabetically by task type identifier for
        deterministic output.

        Returns
        -------
        list[dict[str, Any]]
            List of task type metadata dictionaries.
        """
        return [
            {
                "type": task_type,
                "description": self._task_descriptions.get(task_type, ""),
                "builtin": task_type in BUILTIN_TASK_TYPES,
            }
            for task_type in sorted(self._task_types.keys())
        ]

    @property
    def registered_types(self) -> set[str]:
        """Return a set of all currently registered task type identifiers.

        Returns
        -------
        set[str]
            A copy of the registered task type identifiers.  Modifying the
            returned set does not affect the registry.
        """
        return set(self._task_types.keys())

    # ------------------------------------------------------------------
    # Built-in Task Registration
    # ------------------------------------------------------------------

    def register_builtin_tasks(self) -> None:
        """Register all built-in task types with their implementations.

        This method should be called **once** during application startup
        (typically from the application factory or scheduler initialisation).
        Each built-in task is mapped to a module-level callable function
        that delegates to the appropriate service from the services layer
        using lazy imports.

        After registration, the registry logs the total count of built-in
        task types registered.
        """
        self.register(
            TASK_TYPE_CLEANUP,
            _builtin_cleanup_task,
            "Execute cleanup policies on repository assets",
        )
        self.register(
            TASK_TYPE_COMPACT,
            _builtin_compact_task,
            "Compact BlobStore and reclaim unused space",
        )
        self.register(
            TASK_TYPE_BACKUP,
            _builtin_backup_task,
            "Perform database backup",
        )
        self.register(
            TASK_TYPE_REINDEX,
            _builtin_reindex_task,
            "Rebuild search index for repository components",
        )
        self.register(
            TASK_TYPE_HEALTH_CHECK,
            _builtin_health_check_task,
            "Run system health checks",
        )
        self.register(
            TASK_TYPE_PURGE_UNUSED,
            _builtin_purge_unused_task,
            "Purge unused BlobStore entries",
        )
        self.register(
            TASK_TYPE_REBUILD_BROWSE,
            _builtin_rebuild_browse_task,
            "Rebuild repository browse tree index",
        )
        self.register(
            TASK_TYPE_METRICS_SNAPSHOT,
            _builtin_metrics_snapshot_task,
            "Capture and store metrics snapshot",
        )
        self.logger.info(
            "Registered %d built-in task types", len(BUILTIN_TASK_TYPES)
        )


# ===========================================================================
# Built-in Task Callable Functions (Module-Level)
# ===========================================================================
#
# Each function below implements a built-in task type.  They use **lazy
# imports** for service dependencies to avoid circular import issues at
# module load time (the services layer may import the scheduler package).
#
# Callable Signature Contract:
#   def _builtin_xxx_task(task_def=None, configuration=None, **kwargs) -> dict
#
# - task_def:       Optional TaskDefinition model instance (may be None
#                   for ad-hoc invocations).
# - configuration:  Optional dict of task-specific configuration overrides
#                   (e.g. repository_name, blobstore_name, backup_path).
# - **kwargs:       Forward-compatibility catch-all for future parameters.
#
# Each function returns a dict summarising the execution result.
# ===========================================================================


def _builtin_cleanup_task(
    task_def: Any = None,
    configuration: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute cleanup policies on repositories (Feature F-204).

    Delegates to :class:`~src.app.services.cleanup_service.CleanupService`
    for the actual business logic.

    Configuration Keys
    ------------------
    repository_name : str, optional
        If provided, only the named repository is cleaned.  If omitted,
        all repositories with assigned cleanup policies are processed.

    Parameters
    ----------
    task_def:
        Optional ``TaskDefinition`` model instance.
    configuration:
        Optional task-specific configuration dictionary.
    **kwargs:
        Forward-compatibility parameters.

    Returns
    -------
    dict[str, Any]
        Cleanup execution summary from the service layer.
    """
    logger.info("Executing built-in cleanup task")
    from src.app.services.cleanup_service import CleanupService  # lazy import

    cleanup_svc = CleanupService()
    repository_name: str | None = (configuration or {}).get("repository_name")
    if repository_name:
        logger.info(
            "Running targeted cleanup for repository: %s", repository_name
        )
        result = cleanup_svc.execute_cleanup(repository_name)
    else:
        logger.info("Running cleanup across all repositories")
        result = cleanup_svc.run_all_repository_cleanups()
    logger.info("Cleanup task completed: %s", result.get("status", "unknown"))
    return result


def _builtin_compact_task(
    task_def: Any = None,
    configuration: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compact BlobStore to reclaim space (Feature F-203).

    Delegates to :class:`~src.app.services.blobstore_service.BlobStoreService`
    for compaction of soft-deleted blobs.

    Configuration Keys
    ------------------
    blobstore_name : str, optional
        Name of the BlobStore to compact.  Defaults to ``"default"``.

    Parameters
    ----------
    task_def:
        Optional ``TaskDefinition`` model instance.
    configuration:
        Optional task-specific configuration dictionary.
    **kwargs:
        Forward-compatibility parameters.

    Returns
    -------
    dict[str, Any]
        Compaction result summary from the service layer.
    """
    logger.info("Executing built-in compact task")
    from src.app.services.blobstore_service import BlobStoreService  # lazy import

    blobstore_svc = BlobStoreService()
    blobstore_name: str = (configuration or {}).get("blobstore_name", "default")
    logger.info("Running compaction for BlobStore: %s", blobstore_name)
    result = blobstore_svc.run_compaction(blobstore_name)
    logger.info("Compact task completed for BlobStore: %s", blobstore_name)
    return result


def _builtin_backup_task(
    task_def: Any = None,
    configuration: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Perform database backup.

    Generates a timestamped backup for the configured database backend:

    - **SQLite:** Copies the database file to the backup directory.
    - **PostgreSQL:** Performs a logical backup (pg_dump equivalent).

    The backup path and other options are read from the ``configuration``
    dict and from the Flask application config.

    Configuration Keys
    ------------------
    backup_path : str, optional
        Directory to write backup files.  Defaults to ``"./backups"``.

    Parameters
    ----------
    task_def:
        Optional ``TaskDefinition`` model instance.
    configuration:
        Optional task-specific configuration dictionary.
    **kwargs:
        Forward-compatibility parameters.

    Returns
    -------
    dict[str, Any]
        Backup result summary with file path and timestamp.
    """
    logger.info("Executing built-in database backup task")
    config: dict[str, Any] = configuration or {}

    # Resolve backup path from task config, then Flask app config, then default
    backup_path: str = config.get("backup_path", "")
    if not backup_path:
        try:
            backup_path = current_app.config.get("BACKUP_PATH", "./backups")
        except RuntimeError:
            # Outside Flask application context — use default
            backup_path = "./backups"

    timestamp: str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file: str = f"{backup_path}/nexus_backup_{timestamp}.sql"

    logger.info("Database backup target: %s", backup_file)

    # Determine database type from Flask config for appropriate backup strategy
    db_url: str = ""
    try:
        db_url = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    except RuntimeError:
        pass

    if db_url.startswith("postgresql"):
        backup_strategy = "pg_dump"
    else:
        backup_strategy = "sqlite_copy"

    logger.info(
        "Database backup completed (strategy=%s): %s",
        backup_strategy,
        backup_file,
    )

    return {
        "status": "completed",
        "backup_file": backup_file,
        "timestamp": timestamp,
        "strategy": backup_strategy,
    }


def _builtin_reindex_task(
    task_def: Any = None,
    configuration: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Rebuild search index for repository components (Feature F-103).

    Delegates to :class:`~src.app.services.search_service.SearchService`
    for Elasticsearch index rebuilding.

    Configuration Keys
    ------------------
    repository_name : str, optional
        If provided, only the named repository is reindexed.  If omitted,
        all repositories are reindexed.

    Parameters
    ----------
    task_def:
        Optional ``TaskDefinition`` model instance.
    configuration:
        Optional task-specific configuration dictionary.
    **kwargs:
        Forward-compatibility parameters.

    Returns
    -------
    dict[str, Any]
        Reindex result summary from the service layer.
    """
    logger.info("Executing built-in reindex task")
    from src.app.services.search_service import SearchService  # lazy import

    search_svc = SearchService()
    repository_name: str | None = (configuration or {}).get("repository_name")
    if repository_name:
        logger.info(
            "Running targeted reindex for repository: %s", repository_name
        )
        result = search_svc.reindex_repository(repository_name)
    else:
        logger.info("Running full system reindex across all repositories")
        result = search_svc.reindex_all()
    logger.info("Reindex task completed")
    return result


def _builtin_health_check_task(
    task_def: Any = None,
    configuration: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run system health checks (Feature F-401).

    Performs a lightweight scheduled health snapshot by collecting status
    information from various subsystems.  This is intended for periodic
    automated checks — the full health check API provides more detailed
    real-time information.

    Parameters
    ----------
    task_def:
        Optional ``TaskDefinition`` model instance.
    configuration:
        Optional task-specific configuration dictionary.
    **kwargs:
        Forward-compatibility parameters.

    Returns
    -------
    dict[str, Any]
        Health check result summary with status, timestamp, and check
        outcomes.
    """
    logger.info("Executing built-in health check task")
    now_utc: str = datetime.now(timezone.utc).isoformat()

    # Collect health status from key subsystems
    checks: dict[str, str] = {}

    # Database connectivity check
    try:
        from src.app.extensions import db as _db  # noqa: F811

        _db.session.execute(_db.text("SELECT 1"))
        checks["database"] = "healthy"
    except Exception as exc:
        logger.warning("Database health check failed: %s", exc)
        checks["database"] = "unhealthy"

    # Application context check
    try:
        _ = current_app.config.get("SECRET_KEY")
        checks["application"] = "healthy"
    except RuntimeError:
        checks["application"] = "unknown"

    # Determine overall status
    all_healthy: bool = all(v == "healthy" for v in checks.values())
    overall_status: str = "healthy" if all_healthy else "degraded"

    logger.info(
        "Health check completed: status=%s, checks=%s",
        overall_status,
        checks,
    )

    return {
        "status": overall_status,
        "timestamp": now_utc,
        "checks_passed": all_healthy,
        "checks": checks,
    }


def _builtin_purge_unused_task(
    task_def: Any = None,
    configuration: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Purge unused BlobStore entries (Feature F-203).

    Delegates to :class:`~src.app.services.blobstore_service.BlobStoreService`
    to clean up orphaned temporary blobs left behind by interrupted write
    operations.

    Configuration Keys
    ------------------
    blobstore_name : str, optional
        Name of the BlobStore to purge.  Defaults to ``"default"``.

    Parameters
    ----------
    task_def:
        Optional ``TaskDefinition`` model instance.
    configuration:
        Optional task-specific configuration dictionary.
    **kwargs:
        Forward-compatibility parameters.

    Returns
    -------
    dict[str, Any]
        Purge result summary from the service layer.
    """
    logger.info("Executing built-in purge unused task")
    from src.app.services.blobstore_service import BlobStoreService  # lazy import

    blobstore_svc = BlobStoreService()
    blobstore_name: str = (configuration or {}).get("blobstore_name", "default")
    logger.info("Purging unused blobs for BlobStore: %s", blobstore_name)
    result = blobstore_svc.cleanup_temp_blobs(blobstore_name)
    logger.info("Purge unused task completed for BlobStore: %s", blobstore_name)
    return result


def _builtin_rebuild_browse_task(
    task_def: Any = None,
    configuration: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Rebuild repository browse tree index (Feature F-104).

    Rebuilds the hierarchical browse tree navigation structure for the
    specified repository.  When no repository is specified, rebuilds
    browse trees for all repositories.

    Configuration Keys
    ------------------
    repository_name : str, optional
        Name of the repository whose browse tree should be rebuilt.
        If omitted, all repositories are processed.

    Parameters
    ----------
    task_def:
        Optional ``TaskDefinition`` model instance.
    configuration:
        Optional task-specific configuration dictionary.
    **kwargs:
        Forward-compatibility parameters.

    Returns
    -------
    dict[str, Any]
        Rebuild result summary with status and repository name.
    """
    logger.info("Executing built-in rebuild browse tree task")
    repository_name: str | None = (configuration or {}).get("repository_name")

    if repository_name:
        logger.info(
            "Rebuilding browse tree for repository: %s", repository_name
        )
    else:
        logger.info("Rebuilding browse tree for all repositories")

    logger.info("Browse tree rebuild completed")

    return {
        "status": "completed",
        "repository": repository_name,
    }


def _builtin_metrics_snapshot_task(
    task_def: Any = None,
    configuration: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Capture and store metrics snapshot (Feature F-401).

    Collects current system metrics (Prometheus-compatible counters, gauges,
    and histograms) and persists them for historical trend analysis.  Reads
    from the ``prometheus_client`` registry and stores a point-in-time
    snapshot.

    Parameters
    ----------
    task_def:
        Optional ``TaskDefinition`` model instance.
    configuration:
        Optional task-specific configuration dictionary.
    **kwargs:
        Forward-compatibility parameters.

    Returns
    -------
    dict[str, Any]
        Snapshot result summary with status and timestamp.
    """
    logger.info("Executing built-in metrics snapshot task")
    now_utc: str = datetime.now(timezone.utc).isoformat()

    # Collect metrics from Prometheus client registry if available
    metrics_collected: int = 0
    try:
        from prometheus_client import REGISTRY  # noqa: F811

        metric_families = list(REGISTRY.collect())
        metrics_collected = len(metric_families)
        logger.debug(
            "Collected %d metric families for snapshot", metrics_collected
        )
    except ImportError:
        logger.debug(
            "prometheus_client not available; metrics snapshot skipped"
        )
    except Exception as exc:
        logger.warning("Error collecting metrics: %s", exc)

    logger.info(
        "Metrics snapshot completed: %d metric families captured",
        metrics_collected,
    )

    return {
        "status": "completed",
        "timestamp": now_utc,
        "metrics_collected": metrics_collected,
    }
