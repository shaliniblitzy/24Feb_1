"""
Diagnostic Bundle (Support ZIP) Generation Service — Feature F-403.

This module implements the ``SupportZipService`` class that generates a
comprehensive diagnostic ZIP bundle for troubleshooting and support
purposes.  It replaces the Java ``SupportZipGenerator`` and related
components from the original Sonatype Nexus Repository source system.

**Collected Diagnostic Sections:**

+--------------------+-----------------------------------------------------+
| ZIP Path           | Contents                                            |
+====================+=====================================================+
| info/system-info   | Python version, platform, runtime, environment      |
+--------------------+-----------------------------------------------------+
| config/            | System and Flask configuration (sanitized)          |
+--------------------+-----------------------------------------------------+
| log/               | Application log files (truncated to MAX_LOG_SIZE)   |
+--------------------+-----------------------------------------------------+
| info/thread-dump   | Python thread dump (replaces Java jstack/JFR)       |
+--------------------+-----------------------------------------------------+
| metrics/           | Prometheus metrics + runtime memory/uptime metrics  |
+--------------------+-----------------------------------------------------+
| info/database-info | Database type, table row counts                     |
+--------------------+-----------------------------------------------------+
| info/repositories  | All repository definitions with component counts    |
+--------------------+-----------------------------------------------------+
| info/blobstores    | BlobStore configs (S3 credentials masked)           |
+--------------------+-----------------------------------------------------+
| info/tasks         | Scheduled task definitions and recent executions    |
+--------------------+-----------------------------------------------------+

**Security:**

All sensitive configuration values (passwords, secrets, API keys, tokens,
AWS credentials) are replaced with ``'*****'`` before inclusion in the ZIP.
Database URLs with embedded credentials are masked as well.  This ensures
that diagnostic bundles are safe to share with support teams.

**Architecture Context:**

- Replaces ``SupportZipGenerator`` from the Java source system.
- Uses Python standard library ``zipfile`` with ``ZIP_DEFLATED`` compression.
- Returns ``(BytesIO, filename)`` tuple for streaming response in the API.
- All collectors are resilient — individual failures are logged and skipped
  without aborting the entire ZIP generation.

Exports:
    SupportZipService : Main service class with ``generate_support_zip()``
    SENSITIVE_KEYS    : List of key patterns used for config sanitization
    MAX_LOG_SIZE      : Maximum bytes per log file to include (10 MB)
    ZIP_FILENAME_FORMAT : Filename template for generated ZIPs
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import sys
import threading
import time
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse, urlunparse

from flask import current_app
from sqlalchemy import text

from src.app.extensions import db
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.models.repository import Repository
from src.app.models.system_config import SystemConfig
from src.app.models.task import TaskDefinition, TaskExecution
from src.app.utils.helpers import format_file_size, iso_now, safe_json_dumps

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# Provides structured logging for ZIP generation diagnostics.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SENSITIVE_KEYS: list[str] = [
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
    "secret_key",
    "credential",
    "aws_secret",
    "s3_secret",
    "smtp_password",
    "ldap_password",
]
"""Substrings (case-insensitive) that identify a configuration key as
sensitive.  Any config key whose lowercase representation contains one
of these patterns will have its value replaced with ``'*****'``.
"""

MAX_LOG_SIZE: int = 10 * 1024 * 1024  # 10 MB
"""Maximum number of bytes to include per log file.  Files exceeding this
limit are tail-truncated so that only the most recent portion is included
in the diagnostic bundle.
"""

ZIP_FILENAME_FORMAT: str = "support-{timestamp}.zip"
"""Filename template for generated support ZIPs.  The ``{timestamp}``
placeholder is replaced with a ``YYYYMMDD-HHmmss`` UTC timestamp.
"""

# Mask string used to replace sensitive values
_MASK: str = "*****"

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "SupportZipService",
    "SENSITIVE_KEYS",
    "MAX_LOG_SIZE",
    "ZIP_FILENAME_FORMAT",
]


# ===========================================================================
# SupportZipService
# ===========================================================================


class SupportZipService:
    """Diagnostic bundle (support ZIP) generation service.

    Collects system information, sanitized configuration, log files, thread
    dumps, Prometheus metrics, and database/repository/blobstore/task
    metadata into an in-memory ZIP archive.

    Usage::

        service = SupportZipService()
        buffer, filename = service.generate_support_zip()
        # Send buffer as a streaming response from the API handler.
    """

    def __init__(self) -> None:
        """Initialise the service with a dedicated logger instance."""
        self.logger: logging.Logger = logging.getLogger(__name__)

    # -----------------------------------------------------------------------
    # Main Entry Point
    # -----------------------------------------------------------------------

    def generate_support_zip(
        self,
        include_logs: bool = True,
        include_config: bool = True,
        include_system_info: bool = True,
        include_thread_dump: bool = True,
        include_metrics: bool = True,
        include_jmx: bool = False,
        limit_file_sizes: bool = True,
    ) -> tuple[BinaryIO, str]:
        """Generate a complete diagnostic support ZIP bundle.

        Each optional section is collected independently so that a failure
        in one collector does not prevent the remaining sections from being
        included.

        Parameters:
            include_logs: Collect application log files.
            include_config: Collect system and Flask configuration
                (sanitized).
            include_system_info: Collect Python/platform/runtime details.
            include_thread_dump: Collect a Python thread dump.
            include_metrics: Collect Prometheus and runtime metrics.
            include_jmx: Reserved for forward compatibility (no-op).
            limit_file_sizes: Truncate log files exceeding
                :data:`MAX_LOG_SIZE`.

        Returns:
            A ``(buffer, filename)`` tuple where *buffer* is a
            :class:`io.BytesIO` positioned at offset 0 containing the ZIP
            data, and *filename* is the suggested download filename.
        """
        start_time = time.time()
        self.logger.info("Starting support ZIP generation.")

        buffer = io.BytesIO()

        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            # 1. System information
            if include_system_info:
                self._safe_collect("system_info", self._collect_system_info, zf)

            # 2. Configuration
            if include_config:
                self._safe_collect("configuration", self._collect_configuration, zf)

            # 3. Log files
            if include_logs:
                self._safe_collect(
                    "log_files",
                    self._collect_log_files,
                    zf,
                    limit_size=limit_file_sizes,
                )

            # 4. Thread dump
            if include_thread_dump:
                self._safe_collect("thread_dump", self._collect_thread_dump, zf)

            # 5. Metrics
            if include_metrics:
                self._safe_collect("metrics", self._collect_metrics, zf)

            # Always-included collectors
            self._safe_collect("database_info", self._collect_database_info, zf)
            self._safe_collect("repository_info", self._collect_repository_info, zf)
            self._safe_collect("blobstore_info", self._collect_blobstore_info, zf)
            self._safe_collect("task_info", self._collect_task_info, zf)

        # Generate filename with UTC timestamp
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = ZIP_FILENAME_FORMAT.format(timestamp=ts)

        # Rewind the buffer so callers can read from the beginning
        buffer.seek(0)

        elapsed = time.time() - start_time
        self.logger.info(
            "Support ZIP generation complete: %s (%.2f seconds).",
            filename,
            elapsed,
        )

        return buffer, filename

    # -----------------------------------------------------------------------
    # Collector — System Information
    # -----------------------------------------------------------------------

    def _collect_system_info(self, zf: zipfile.ZipFile) -> None:
        """Collect Python, platform, and runtime information.

        Writes ``info/system-info.json`` into the archive.
        """
        info: dict = {
            "generated_at": iso_now(),
            "application": {
                "name": "Nexus Repository Manager",
                "version": current_app.config.get("APP_VERSION", "1.0.0"),
                "edition": current_app.config.get("APP_EDITION", "OSS"),
            },
            "python": {
                "version": sys.version,
                "implementation": platform.python_implementation(),
                "executable": sys.executable,
            },
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "hostname": platform.node(),
            },
            "runtime": {
                "pid": os.getpid(),
                "cwd": os.getcwd(),
                "thread_count": threading.active_count(),
            },
            "environment": {
                "FLASK_ENV": os.environ.get("FLASK_ENV", "production"),
                "DATABASE_URL": _sanitize_url(
                    os.environ.get("DATABASE_URL", "")
                ),
                "ELASTICSEARCH_URL": os.environ.get("ELASTICSEARCH_URL", ""),
            },
        }

        zf.writestr("info/system-info.json", safe_json_dumps(info, pretty=True))
        self.logger.debug("Collected system information.")

    # -----------------------------------------------------------------------
    # Collector — Configuration
    # -----------------------------------------------------------------------

    def _collect_configuration(self, zf: zipfile.ZipFile) -> None:
        """Collect sanitized system and Flask configuration.

        Writes ``config/system-config.json`` and ``config/flask-config.json``
        into the archive.  **All sensitive values are masked.**
        """
        # --- System configuration from the database ---
        try:
            configs = SystemConfig.query.all()
        except Exception:
            configs = []
            self.logger.warning(
                "Could not query SystemConfig table; writing empty config."
            )

        grouped: dict[str, dict[str, str]] = {}
        for cfg in configs:
            category = cfg.category or "general"
            if category not in grouped:
                grouped[category] = {}
            sanitized_value = self._sanitize_config_value(
                cfg.key, cfg.value if cfg.value is not None else ""
            )
            grouped[category][cfg.key] = sanitized_value

        system_config_data = {
            "generated_at": iso_now(),
            "categories": grouped,
            "total_entries": len(configs),
        }
        zf.writestr(
            "config/system-config.json",
            safe_json_dumps(system_config_data, pretty=True),
        )

        # --- Flask app configuration (sanitized) ---
        flask_config: dict[str, str] = {}
        try:
            for key in sorted(current_app.config.keys()):
                raw = current_app.config[key]
                # Attempt to make the value JSON-serialisable
                try:
                    json.dumps(raw)
                    value_str = str(raw)
                except (TypeError, ValueError, OverflowError):
                    value_str = repr(raw)
                flask_config[key] = self._sanitize_config_value(key, value_str)
        except Exception:
            self.logger.warning("Could not serialize Flask config.", exc_info=True)

        flask_config_data = {
            "generated_at": iso_now(),
            "config": flask_config,
        }
        zf.writestr(
            "config/flask-config.json",
            safe_json_dumps(flask_config_data, pretty=True),
        )

        self.logger.debug(
            "Collected configuration: %d system entries.", len(configs)
        )

    def _sanitize_config_value(self, key: str, value: str) -> str:
        """Mask the *value* if *key* matches any sensitive pattern.

        A key is considered sensitive when its **lowercase** representation
        contains any substring from :data:`SENSITIVE_KEYS`.  URLs with
        embedded credentials (e.g. ``postgres://user:pass@host``) are also
        masked via :func:`_sanitize_url`.

        Parameters:
            key: Configuration key name.
            value: Raw configuration value string.

        Returns:
            The original *value* if safe, or ``'*****'`` if sensitive.
        """
        key_lower = key.lower()
        for pattern in SENSITIVE_KEYS:
            if pattern in key_lower:
                return _MASK
        # Detect URL-style values that may embed passwords
        if value and "://" in value and "@" in value:
            return _sanitize_url(value)
        return value

    # -----------------------------------------------------------------------
    # Collector — Log Files
    # -----------------------------------------------------------------------

    def _collect_log_files(
        self,
        zf: zipfile.ZipFile,
        limit_size: bool = True,
    ) -> None:
        """Collect application log files into ``log/`` within the archive.

        Files exceeding :data:`MAX_LOG_SIZE` are tail-truncated when
        *limit_size* is ``True``.  Missing log directories are handled
        gracefully without raising.
        """
        log_dir_str: str = current_app.config.get("LOG_DIR", "./logs")
        log_dir = Path(log_dir_str)

        if not log_dir.exists() or not log_dir.is_dir():
            self.logger.debug(
                "Log directory '%s' does not exist; skipping log collection.",
                log_dir,
            )
            return

        log_files_collected = 0
        for log_path in sorted(log_dir.glob("*.log")):
            try:
                file_size = log_path.stat().st_size
                if limit_size and file_size > MAX_LOG_SIZE:
                    # Tail: include only the last MAX_LOG_SIZE bytes
                    with open(log_path, "rb") as fh:
                        fh.seek(file_size - MAX_LOG_SIZE)
                        data = fh.read()
                    self.logger.debug(
                        "Log file '%s' truncated from %s to %s.",
                        log_path.name,
                        format_file_size(file_size),
                        format_file_size(len(data)),
                    )
                else:
                    data = log_path.read_bytes()

                zf.writestr(f"log/{log_path.name}", data)
                log_files_collected += 1
            except Exception:
                self.logger.warning(
                    "Failed to read log file '%s'.", log_path.name, exc_info=True
                )

        self.logger.debug("Collected %d log file(s).", log_files_collected)

    # -----------------------------------------------------------------------
    # Collector — Thread Dump
    # -----------------------------------------------------------------------

    def _collect_thread_dump(self, zf: zipfile.ZipFile) -> None:
        """Collect a Python thread dump similar to Java ``jstack`` output.

        Writes ``info/thread-dump.txt`` into the archive.
        """
        lines: list[str] = []
        lines.append(f"Thread Dump — {iso_now()}")
        lines.append(f"Active threads: {threading.active_count()}")
        lines.append("=" * 72)

        # Map thread IDs to stack frames
        frames = sys._current_frames()

        for thread_obj in threading.enumerate():
            tid = thread_obj.ident
            lines.append("")
            lines.append(
                f'Thread: "{thread_obj.name}" '
                f"(id={tid}, daemon={thread_obj.daemon}, "
                f"alive={thread_obj.is_alive()})"
            )
            lines.append("-" * 60)
            if tid is not None and tid in frames:
                stack = traceback.format_stack(frames[tid])
                for frame_line in stack:
                    lines.append(frame_line.rstrip())
            else:
                lines.append("  (no stack trace available)")

        lines.append("")
        lines.append("=" * 72)
        lines.append(f"End of thread dump — {len(threading.enumerate())} threads")

        zf.writestr("info/thread-dump.txt", "\n".join(lines))
        self.logger.debug("Collected thread dump.")

    # -----------------------------------------------------------------------
    # Collector — Metrics
    # -----------------------------------------------------------------------

    def _collect_metrics(self, zf: zipfile.ZipFile) -> None:
        """Collect Prometheus and runtime metrics.

        Writes ``metrics/prometheus.txt`` (if prometheus_client is
        available) and ``metrics/runtime-metrics.json`` into the archive.
        """
        # Prometheus metrics (optional — graceful degradation)
        try:
            from prometheus_client import generate_latest  # noqa: F811

            metrics_text = generate_latest().decode("utf-8")
            zf.writestr("metrics/prometheus.txt", metrics_text)
            self.logger.debug("Collected Prometheus metrics.")
        except ImportError:
            self.logger.debug(
                "prometheus_client not available; skipping Prometheus metrics."
            )
        except Exception:
            self.logger.warning(
                "Failed to collect Prometheus metrics.", exc_info=True
            )

        # Runtime metrics
        runtime_metrics: dict = {
            "generated_at": iso_now(),
            "pid": os.getpid(),
            "thread_count": threading.active_count(),
        }

        # Attempt to read process memory info from /proc on Linux
        try:
            proc_status_path = f"/proc/{os.getpid()}/status"
            if os.path.exists(proc_status_path):
                with open(proc_status_path, "r") as fh:
                    for line in fh:
                        if line.startswith("VmRSS:"):
                            runtime_metrics["memory_rss_kb"] = int(
                                line.split()[1]
                            )
                        elif line.startswith("VmSize:"):
                            runtime_metrics["memory_virtual_kb"] = int(
                                line.split()[1]
                            )
                        elif line.startswith("VmPeak:"):
                            runtime_metrics["memory_peak_kb"] = int(
                                line.split()[1]
                            )
        except Exception:
            self.logger.debug(
                "Could not read /proc status for memory info.", exc_info=True
            )

        # Attempt to read uptime from Flask config
        try:
            app_start_time = current_app.config.get("APP_START_TIME")
            if app_start_time:
                if isinstance(app_start_time, (int, float)):
                    uptime_seconds = time.time() - app_start_time
                    runtime_metrics["uptime_seconds"] = round(uptime_seconds, 2)
        except Exception:
            pass

        zf.writestr(
            "metrics/runtime-metrics.json",
            safe_json_dumps(runtime_metrics, pretty=True),
        )
        self.logger.debug("Collected runtime metrics.")

    # -----------------------------------------------------------------------
    # Collector — Database Information
    # -----------------------------------------------------------------------

    def _collect_database_info(self, zf: zipfile.ZipFile) -> None:
        """Collect database type and table row counts.

        Writes ``info/database-info.json`` into the archive.
        """
        db_url_raw = str(db.engine.url) if db.engine else ""
        db_url_sanitized = _sanitize_url(db_url_raw)

        # Determine database type from the dialect name
        db_type = "unknown"
        try:
            db_type = db.engine.dialect.name if db.engine else "unknown"
        except Exception:
            pass

        # Collect row counts — use model queries for simple tables and
        # raw SQL for tables that may not have a dedicated model import.
        table_counts: dict[str, int | str] = {}
        try:
            table_counts["repositories"] = Repository.query.count()
        except Exception as exc:
            table_counts["repositories"] = f"error: {exc}"

        for table_name in ("component", "asset", "user"):
            try:
                count = db.session.execute(
                    text(f"SELECT COUNT(*) FROM {table_name}")
                ).scalar()
                table_counts[f"{table_name}s"] = count if count is not None else 0
            except Exception:
                table_counts[f"{table_name}s"] = "error: table may not exist"

        try:
            table_counts["tasks"] = TaskDefinition.query.count()
        except Exception as exc:
            table_counts["tasks"] = f"error: {exc}"

        db_info: dict = {
            "generated_at": iso_now(),
            "database_type": db_type,
            "database_url": db_url_sanitized,
            "table_counts": table_counts,
        }

        zf.writestr(
            "info/database-info.json",
            safe_json_dumps(db_info, pretty=True),
        )
        self.logger.debug("Collected database information.")

    # -----------------------------------------------------------------------
    # Collector — Repository Information
    # -----------------------------------------------------------------------

    def _collect_repository_info(self, zf: zipfile.ZipFile) -> None:
        """Collect all repository definitions.

        Writes ``info/repositories.json`` into the archive.
        """
        repos_data: list[dict] = []
        try:
            repos = Repository.query.all()
            for repo in repos:
                repos_data.append(
                    {
                        "name": repo.name,
                        "format": repo.format,
                        "type": repo.type,
                        "online": repo.online,
                        "component_count": repo.component_count,
                    }
                )
        except Exception:
            self.logger.warning(
                "Failed to query repositories.", exc_info=True
            )

        repo_info: dict = {
            "generated_at": iso_now(),
            "total_repositories": len(repos_data),
            "repositories": repos_data,
        }

        zf.writestr(
            "info/repositories.json",
            safe_json_dumps(repo_info, pretty=True),
        )
        self.logger.debug(
            "Collected repository information: %d repositories.", len(repos_data)
        )

    # -----------------------------------------------------------------------
    # Collector — BlobStore Information
    # -----------------------------------------------------------------------

    def _collect_blobstore_info(self, zf: zipfile.ZipFile) -> None:
        """Collect all BlobStore configurations with S3 credentials masked.

        Writes ``info/blobstores.json`` into the archive.
        """
        blobstores_data: list[dict] = []
        try:
            blobstores = BlobStoreConfig.query.all()
            for bs in blobstores:
                sanitized_config = self._sanitize_blobstore_configuration(
                    bs.configuration
                )
                blobstores_data.append(
                    {
                        "name": bs.blob_store_name,
                        "type": bs.type,
                        "total_size": bs.total_size,
                        "total_size_human": format_file_size(
                            bs.total_size or 0
                        ),
                        "blob_count": bs.blob_count,
                        "available_space": bs.available_space,
                        "available_space_human": (
                            format_file_size(bs.available_space)
                            if bs.available_space is not None
                            else None
                        ),
                        "configuration": sanitized_config,
                    }
                )
        except Exception:
            self.logger.warning(
                "Failed to query BlobStore configs.", exc_info=True
            )

        bs_info: dict = {
            "generated_at": iso_now(),
            "total_blobstores": len(blobstores_data),
            "blobstores": blobstores_data,
        }

        zf.writestr(
            "info/blobstores.json",
            safe_json_dumps(bs_info, pretty=True),
        )
        self.logger.debug(
            "Collected BlobStore information: %d blobstores.",
            len(blobstores_data),
        )

    # -----------------------------------------------------------------------
    # Collector — Task Information
    # -----------------------------------------------------------------------

    def _collect_task_info(self, zf: zipfile.ZipFile) -> None:
        """Collect scheduled task definitions and recent executions.

        Writes ``info/tasks.json`` into the archive.
        """
        tasks_data: list[dict] = []
        try:
            task_defs = TaskDefinition.query.all()
            for td in task_defs:
                # Gather last 10 execution records for this task
                recent_executions: list[dict] = []
                try:
                    execs = (
                        TaskExecution.query.filter(
                            TaskExecution.task_id == td.task_id
                        )
                        .order_by(TaskExecution.start_time.desc())
                        .limit(10)
                        .all()
                    )
                    for ex in execs:
                        recent_executions.append(
                            {
                                "status": ex.status,
                                "start_time": (
                                    ex.start_time.isoformat()
                                    if ex.start_time
                                    else None
                                ),
                                "end_time": (
                                    ex.end_time.isoformat()
                                    if ex.end_time
                                    else None
                                ),
                                "duration_ms": ex.duration_ms,
                            }
                        )
                except Exception:
                    self.logger.debug(
                        "Could not fetch executions for task '%s'.",
                        td.task_id,
                        exc_info=True,
                    )

                tasks_data.append(
                    {
                        "task_id": td.task_id,
                        "type": td.type,
                        "name": td.name,
                        "enabled": td.enabled,
                        "cron_expression": td.cron_expression,
                        "recent_executions": recent_executions,
                    }
                )
        except Exception:
            self.logger.warning(
                "Failed to query task definitions.", exc_info=True
            )

        task_info: dict = {
            "generated_at": iso_now(),
            "total_tasks": len(tasks_data),
            "tasks": tasks_data,
        }

        zf.writestr(
            "info/tasks.json",
            safe_json_dumps(task_info, pretty=True),
        )
        self.logger.debug(
            "Collected task information: %d tasks.", len(tasks_data)
        )

    # -----------------------------------------------------------------------
    # Private Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _sanitize_blobstore_configuration(
        configuration: dict | None,
    ) -> dict:
        """Return a copy of *configuration* with S3 credentials masked.

        Keys containing sensitive substrings (``secret``, ``access_key``,
        ``password``, ``credential``) are replaced with ``'*****'``.
        Nested dicts are traversed recursively.
        """
        if not configuration:
            return {}

        sanitized: dict = {}
        for key, value in configuration.items():
            key_lower = key.lower()
            is_sensitive = any(p in key_lower for p in SENSITIVE_KEYS)
            if is_sensitive and isinstance(value, str):
                sanitized[key] = _MASK
            elif isinstance(value, dict):
                sanitized[key] = SupportZipService._sanitize_blobstore_configuration(
                    value
                )
            else:
                sanitized[key] = value
        return sanitized

    def _safe_collect(
        self,
        name: str,
        collector_fn,
        zf: zipfile.ZipFile,
        **kwargs,
    ) -> None:
        """Invoke *collector_fn* with error isolation.

        If the collector raises any exception the error is logged and the
        ZIP generation continues with the remaining collectors.
        """
        try:
            collector_fn(zf, **kwargs)
        except Exception:
            self.logger.warning(
                "Collector '%s' failed; skipping section.", name, exc_info=True
            )


# ---------------------------------------------------------------------------
# Module-Level URL Sanitization Helper
# ---------------------------------------------------------------------------


def _sanitize_url(url: str) -> str:
    """Mask the password component of a URL.

    ``postgres://user:secret@host:5432/db``
    → ``postgres://user:****@host:5432/db``

    Empty or non-URL strings are returned unchanged.
    """
    if not url or "://" not in url:
        return url
    try:
        parsed = urlparse(url)
        if parsed.password:
            # Reconstruct with masked password
            netloc = parsed.hostname or ""
            if parsed.username:
                netloc = f"{parsed.username}:****@{netloc}"
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return urlunparse(
                (
                    parsed.scheme,
                    netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
    except Exception:
        pass
    return url
