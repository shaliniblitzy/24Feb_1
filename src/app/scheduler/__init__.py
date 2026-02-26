"""
APScheduler-based task scheduling package for the Nexus Repository Flask application.

Replaces Quartz 2.3.2 scheduled task framework from the Java source system.
Implements Feature F-402 (Scheduled Tasks) with one-time, recurring, and
cron-based task scheduling using APScheduler 3.10.4.

This package provides a unified entry point for three core scheduler components:

- **task_scheduler** — Core APScheduler integration wrapping
  :class:`~apscheduler.schedulers.background.BackgroundScheduler` in a
  Flask-friendly API.  Provides job management (add, remove, pause, resume),
  cron expression parsing via ``CronTrigger.from_crontab()``, cluster-aware
  execution for multi-node deployments via ``SQLAlchemyJobStore``, and
  automatic synchronisation between APScheduler jobs and the database-backed
  :class:`~src.app.models.task.TaskDefinition` /
  :class:`~src.app.models.task.TaskExecution` models.

- **task_registry** — Task type registry mapping task type identifiers
  (e.g. ``'repository.cleanup'``, ``'blobstore.compact'``) to callable
  implementations.  Ships with built-in task types for cleanup (F-204),
  compaction (F-203), backup, reindex (F-103), health check (F-401), purge
  unused blobs, rebuild browse tree (F-104), and metrics snapshot (F-401).
  Supports dynamically registered custom types via the Plugin Architecture
  (Feature F-504).

- **task_logger** — Per-task logging with dedicated log streams under the
  ``nexus.task.*`` logger namespace.  Manages
  :class:`~src.app.models.task.TaskExecution` records for execution status
  tracking, progress reporting, and accurate duration measurement using
  ``time.monotonic()`` to avoid NTP clock drift.

Usage::

    # In factory.py — Flask application factory
    from src.app.scheduler import TaskScheduler

    scheduler = TaskScheduler()
    scheduler.init_app(app)
    scheduler.start()

    # In API routes — task management
    from src.app.scheduler import TaskScheduler, TaskRegistry, TaskLogger

    # In plugin modules — custom task registration
    from src.app.scheduler import TaskRegistry

Architecture Context:

+------------------------------+---------------------------------------------+
| Java Source                  | Python Target                               |
+==============================+=============================================+
| Quartz 2.3.2 Scheduler      | TaskScheduler (this package)                |
+------------------------------+---------------------------------------------+
| Quartz JobDetail registry    | TaskRegistry                                |
+------------------------------+---------------------------------------------+
| TaskLoggerHelper.java        | TaskLogger                                  |
+------------------------------+---------------------------------------------+

Python 3.12+ compatible — No Java dependencies.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Convenience Re-exports
# ---------------------------------------------------------------------------
# Import the three core classes from their respective submodules so that
# consumers can use the shorter ``from src.app.scheduler import TaskScheduler``
# import form instead of the fully-qualified submodule path.
#
# Import order matters: task_registry and task_logger are imported first
# because task_scheduler depends on them internally.  Python's module loading
# handles this correctly regardless of order here, but listing dependencies
# first makes the intent explicit.
# ---------------------------------------------------------------------------

from src.app.scheduler.task_registry import TaskRegistry
from src.app.scheduler.task_logger import TaskLogger
from src.app.scheduler.task_scheduler import TaskScheduler

# ---------------------------------------------------------------------------
# Public API — __all__
# ---------------------------------------------------------------------------
# Explicitly declare the public interface of this package.  Only these three
# classes are re-exported; consumers needing constants or helper functions
# should import directly from the respective submodule.
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "TaskScheduler",
    "TaskRegistry",
    "TaskLogger",
]
