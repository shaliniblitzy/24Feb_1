"""
BlobStore Maintenance Tasks (Feature F-203).

This module implements the BlobStore maintenance service that orchestrates
four categories of background maintenance operations across all configured
BlobStore backends:

1. **Compaction** — Permanently removes soft-deleted blobs that have exceeded
   the retention period (default 30 days).  Replaces the Java Quartz 2.3.2
   ``CompactBlobStoreTask`` scheduled task.

2. **Temporary File Cleanup** — Removes orphaned temporary / staging files
   left behind by interrupted write operations.

3. **Integrity Verification** — Read-only checksum validation that detects
   data corruption by comparing stored checksums against recomputed values.

4. **Statistics Collection** — Gathers storage usage metrics (total size,
   blob count, available space) and persists them to the database.

Each operation is fault-tolerant: individual blob or store failures are
logged and recorded in the result dictionary but **never** crash the
caller or the APScheduler (Feature F-402) scheduler thread.

**Architecture Context:**
Replaces Java BlobStore maintenance task implementations that executed
via Quartz 2.3.2 in the original Sonatype Nexus Repository system.

**Event Integration:**
Maintenance completion events are dispatched through the Blinker-based
event bus (``src.app.events.event_bus``) to support audit logging
(Feature F-303) and monitoring (Feature F-401) subscribers.

**Scheduling Integration:**
The ``get_*_task()`` methods return callables suitable for direct
registration with APScheduler 3.10.4 (Feature F-402).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from src.app.extensions import db
from src.app.storage.blobstore import BlobStore, BlobStoreMetrics
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType
from src.app.utils.helpers import format_file_size

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source system.  All maintenance operations emit structured log messages at
# appropriate severity levels: INFO for normal operations, WARNING for
# anomalies, ERROR for failures.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-Level Constants
# ---------------------------------------------------------------------------

DEFAULT_SOFT_DELETE_RETENTION_DAYS: int = 30
"""Keep soft-deleted blobs for 30 days before permanent removal during
compaction.  After this period, ``compact()`` will permanently delete the
blob data and metadata from the storage backend."""

DEFAULT_TEMP_FILE_MAX_AGE_HOURS: int = 24
"""Clean up temporary / staging files older than 24 hours.  These files
are typically left behind by interrupted write operations (e.g. failed
artifact uploads, aborted multipart S3 uploads)."""

INTEGRITY_CHECK_BATCH_SIZE: int = 100
"""Number of blobs to verify per batch during integrity checks.  Controls
memory usage and allows progress reporting between batches."""


# ===========================================================================
# BlobStoreMaintenanceService
# ===========================================================================


class BlobStoreMaintenanceService:
    """Central service orchestrating all maintenance operations across
    configured BlobStore backends.

    This class is the single entry point for all BlobStore maintenance tasks.
    It maintains a registry of ``BlobStore`` instances by name and provides
    methods that can be invoked directly or wrapped as APScheduler jobs.

    **Lifecycle:**
    1. Instantiate (optionally with a pre-populated registry).
    2. Register BlobStore instances via :meth:`register_blobstore`.
    3. Invoke maintenance methods directly or obtain task callables via
       the ``get_*_task()`` helpers for APScheduler registration.

    **Fault Tolerance:**
    Every public method catches and logs exceptions without re-raising,
    ensuring that scheduler threads and callers are never interrupted by
    maintenance failures.  Individual blob failures within a batch operation
    are recorded in the result dictionary and logged, but processing
    continues to the next blob.
    """

    def __init__(
        self,
        blobstore_registry: dict[str, BlobStore] | None = None,
    ) -> None:
        """Initialise the maintenance service.

        Args:
            blobstore_registry: Optional pre-populated mapping of
                BlobStore name → BlobStore instance.  Defaults to an
                empty registry that can be populated later via
                :meth:`register_blobstore`.
        """
        self._registry: dict[str, BlobStore] = blobstore_registry or {}
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.BlobStoreMaintenanceService"
        )
        self.logger.debug(
            "BlobStoreMaintenanceService initialised with %d registered store(s).",
            len(self._registry),
        )

    # ------------------------------------------------------------------
    # Registry Management
    # ------------------------------------------------------------------

    def register_blobstore(self, name: str, blobstore: BlobStore) -> None:
        """Register a BlobStore instance for maintenance operations.

        Overwrites any existing registration for the same *name*.

        Args:
            name:      Unique name identifying the BlobStore.
            blobstore: The ``BlobStore`` instance to register.
        """
        self._registry[name] = blobstore
        self.logger.info(
            "Registered BlobStore '%s' (type=%s) for maintenance.",
            name,
            getattr(blobstore, "store_type", "unknown"),
        )

    def get_blobstore(self, name: str) -> BlobStore | None:
        """Retrieve a registered BlobStore by name.

        Args:
            name: The BlobStore name to look up.

        Returns:
            The ``BlobStore`` instance, or ``None`` if no store is
            registered under the given name.
        """
        return self._registry.get(name)

    # ------------------------------------------------------------------
    # Compaction Task — Remove Soft-Deleted Blobs (Feature F-203)
    # ------------------------------------------------------------------

    def compact(
        self,
        blob_store_name: str,
        retention_days: int = DEFAULT_SOFT_DELETE_RETENTION_DAYS,
    ) -> dict[str, Any]:
        """Remove soft-deleted blobs beyond the retention period for one
        BlobStore.

        This is the **primary maintenance operation**.  It invokes the
        concrete ``BlobStore.compact()`` method which permanently removes
        blobs that have been soft-deleted for longer than the configured
        retention period.

        After compaction the ``BlobStoreConfig`` model is updated with
        fresh storage statistics, and a ``BLOBSTORE_COMPACTED`` event is
        emitted for audit logging and monitoring subscribers.

        Args:
            blob_store_name: Name of the target BlobStore.
            retention_days:  Retention period in days (informational —
                the concrete BlobStore implementation controls the actual
                retention check internally).

        Returns:
            Result dictionary with compaction statistics.
        """
        start_time: float = time.time()
        result: dict[str, Any] = {
            "blob_store_name": blob_store_name,
            "blobs_removed": 0,
            "space_reclaimed_bytes": 0,
            "space_reclaimed_formatted": format_file_size(0),
            "duration_ms": 0,
            "retention_days": retention_days,
            "status": "error",
        }

        try:
            blobstore: BlobStore | None = self.get_blobstore(blob_store_name)
            if blobstore is None:
                self.logger.warning(
                    "Compaction skipped — BlobStore '%s' not found in registry.",
                    blob_store_name,
                )
                result["status"] = "skipped"
                result["error"] = f"BlobStore '{blob_store_name}' not registered"
                return result

            # Capture pre-compaction metrics for space-reclaimed calculation
            pre_metrics: BlobStoreMetrics = blobstore.get_metrics()
            pre_total_size: int = pre_metrics.total_size_bytes

            # Execute compaction — returns count of blobs permanently removed
            blobs_removed: int = blobstore.compact()

            # Capture post-compaction metrics
            post_metrics: BlobStoreMetrics = blobstore.get_metrics()
            post_total_size: int = post_metrics.total_size_bytes

            # Calculate space reclaimed (non-negative guard)
            space_reclaimed: int = max(0, pre_total_size - post_total_size)

            elapsed_ms: int = int((time.time() - start_time) * 1000)

            result.update({
                "blobs_removed": blobs_removed,
                "space_reclaimed_bytes": space_reclaimed,
                "space_reclaimed_formatted": format_file_size(space_reclaimed),
                "duration_ms": elapsed_ms,
                "status": "ok",
            })

            # Update BlobStoreConfig model with latest statistics
            self._update_blobstore_config(
                blob_store_name,
                post_metrics.total_size_bytes,
                post_metrics.blob_count,
                post_metrics.available_space_bytes if post_metrics.available_space_bytes >= 0 else None,
            )

            # Emit compaction completion event for audit logging (F-303)
            # and monitoring (F-401)
            emit_event(
                EventType.BLOBSTORE_COMPACTED,
                payload={
                    "blob_store_name": blob_store_name,
                    "operation": "compact",
                    "blobs_removed": blobs_removed,
                    "space_reclaimed_bytes": space_reclaimed,
                    "duration_ms": elapsed_ms,
                },
            )

            self.logger.info(
                "Compaction completed for BlobStore '%s': "
                "removed=%d blobs, reclaimed=%s, duration=%dms",
                blob_store_name,
                blobs_removed,
                format_file_size(space_reclaimed),
                elapsed_ms,
            )

        except Exception as exc:
            elapsed_ms = int((time.time() - start_time) * 1000)
            result["duration_ms"] = elapsed_ms
            result["error"] = str(exc)
            self.logger.error(
                "Compaction failed for BlobStore '%s': %s",
                blob_store_name,
                str(exc),
                exc_info=True,
            )

        return result

    def compact_all(
        self,
        retention_days: int = DEFAULT_SOFT_DELETE_RETENTION_DAYS,
    ) -> list[dict[str, Any]]:
        """Run compaction on ALL registered BlobStores.

        Iterates through every registered BlobStore and invokes
        :meth:`compact` on each.  Individual store failures do not
        prevent compaction of remaining stores.

        Args:
            retention_days: Retention period in days passed to each
                individual compaction call.

        Returns:
            List of result dictionaries, one per registered BlobStore.
        """
        results: list[dict[str, Any]] = []
        store_names: list[str] = list(self._registry.keys())

        self.logger.info(
            "Starting compaction for %d BlobStore(s).",
            len(store_names),
        )

        for name in store_names:
            result: dict[str, Any] = self.compact(name, retention_days)
            results.append(result)

        # Summary logging
        total_removed: int = sum(
            r.get("blobs_removed", 0) for r in results
        )
        total_reclaimed: int = sum(
            r.get("space_reclaimed_bytes", 0) for r in results
        )
        self.logger.info(
            "Compaction completed for all stores: "
            "total_removed=%d blobs, total_reclaimed=%s",
            total_removed,
            format_file_size(total_reclaimed),
        )

        return results

    # ------------------------------------------------------------------
    # Temporary File Cleanup (Feature F-203)
    # ------------------------------------------------------------------

    def cleanup_temp_files(
        self,
        blob_store_name: str,
        max_age_hours: int = DEFAULT_TEMP_FILE_MAX_AGE_HOURS,
    ) -> dict[str, Any]:
        """Remove orphaned temporary files for a specific BlobStore.

        Temporary files are staging artifacts left behind by interrupted
        write operations:
        - **File BlobStore**: temp files in the staging directory
        - **S3 BlobStore**: incomplete multipart uploads

        The method checks if the concrete BlobStore implementation
        provides a ``cleanup_temp()`` method and invokes it if available.
        If the method is not implemented, the operation is skipped with
        a warning.

        Args:
            blob_store_name: Name of the target BlobStore.
            max_age_hours:   Maximum age in hours — temp files older
                than this threshold are removed.

        Returns:
            Result dictionary with cleanup statistics.
        """
        start_time: float = time.time()
        result: dict[str, Any] = {
            "blob_store_name": blob_store_name,
            "files_removed": 0,
            "space_reclaimed_bytes": 0,
            "space_reclaimed_formatted": format_file_size(0),
            "duration_ms": 0,
            "max_age_hours": max_age_hours,
            "status": "error",
        }

        try:
            blobstore: BlobStore | None = self.get_blobstore(blob_store_name)
            if blobstore is None:
                self.logger.warning(
                    "Temp cleanup skipped — BlobStore '%s' not found in registry.",
                    blob_store_name,
                )
                result["status"] = "skipped"
                result["error"] = f"BlobStore '{blob_store_name}' not registered"
                return result

            # Determine cutoff time for temp files
            cutoff: datetime = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

            # Check if the concrete BlobStore supports cleanup_temp()
            if hasattr(blobstore, "cleanup_temp") and callable(
                getattr(blobstore, "cleanup_temp")
            ):
                cleanup_result: Any = blobstore.cleanup_temp(cutoff)  # type: ignore[attr-defined]

                # Handle various return types from concrete implementations
                files_removed: int = 0
                space_reclaimed: int = 0

                if isinstance(cleanup_result, dict):
                    files_removed = cleanup_result.get("files_removed", 0)
                    space_reclaimed = cleanup_result.get("space_reclaimed_bytes", 0)
                elif isinstance(cleanup_result, (int, float)):
                    files_removed = int(cleanup_result)
                elif isinstance(cleanup_result, tuple) and len(cleanup_result) >= 2:
                    files_removed = int(cleanup_result[0])
                    space_reclaimed = int(cleanup_result[1])

                elapsed_ms: int = int((time.time() - start_time) * 1000)
                result.update({
                    "files_removed": files_removed,
                    "space_reclaimed_bytes": space_reclaimed,
                    "space_reclaimed_formatted": format_file_size(space_reclaimed),
                    "duration_ms": elapsed_ms,
                    "status": "ok",
                })

                self.logger.info(
                    "Temp cleanup completed for BlobStore '%s': "
                    "files_removed=%d, reclaimed=%s, duration=%dms",
                    blob_store_name,
                    files_removed,
                    format_file_size(space_reclaimed),
                    elapsed_ms,
                )
            else:
                elapsed_ms = int((time.time() - start_time) * 1000)
                result["duration_ms"] = elapsed_ms
                result["status"] = "skipped"
                result["message"] = (
                    f"BlobStore '{blob_store_name}' does not implement cleanup_temp()"
                )
                self.logger.warning(
                    "Temp cleanup skipped for BlobStore '%s': "
                    "cleanup_temp() not implemented by backend.",
                    blob_store_name,
                )

        except Exception as exc:
            elapsed_ms = int((time.time() - start_time) * 1000)
            result["duration_ms"] = elapsed_ms
            result["error"] = str(exc)
            self.logger.error(
                "Temp cleanup failed for BlobStore '%s': %s",
                blob_store_name,
                str(exc),
                exc_info=True,
            )

        return result

    def cleanup_all_temp_files(
        self,
        max_age_hours: int = DEFAULT_TEMP_FILE_MAX_AGE_HOURS,
    ) -> list[dict[str, Any]]:
        """Run temporary file cleanup on ALL registered BlobStores.

        Iterates through every registered BlobStore and invokes
        :meth:`cleanup_temp_files` on each.  Individual store failures
        do not prevent cleanup of remaining stores.

        Args:
            max_age_hours: Maximum age in hours for temp file eligibility.

        Returns:
            List of result dictionaries, one per registered BlobStore.
        """
        results: list[dict[str, Any]] = []
        store_names: list[str] = list(self._registry.keys())

        self.logger.info(
            "Starting temp file cleanup for %d BlobStore(s).",
            len(store_names),
        )

        for name in store_names:
            result: dict[str, Any] = self.cleanup_temp_files(name, max_age_hours)
            results.append(result)

        total_removed: int = sum(
            r.get("files_removed", 0) for r in results
        )
        self.logger.info(
            "Temp cleanup completed for all stores: total_files_removed=%d",
            total_removed,
        )

        return results

    # ------------------------------------------------------------------
    # Integrity Verification — Checksum Validation (Feature F-203)
    # ------------------------------------------------------------------

    def verify_integrity(
        self,
        blob_store_name: str,
        batch_size: int = INTEGRITY_CHECK_BATCH_SIZE,
        max_errors: int = 100,
    ) -> dict[str, Any]:
        """Verify data integrity by validating stored checksums against
        recomputed values.

        This is a **read-only** operation — it NEVER deletes or modifies
        any blob data.  Each blob's stored SHA-1 checksum is compared
        against a freshly computed checksum of the actual blob content.
        Mismatches are recorded and logged.

        Processing stops early if the number of corrupted blobs reaches
        *max_errors* to avoid excessive processing time.

        Args:
            blob_store_name: Name of the target BlobStore.
            batch_size:      Number of blobs to process per batch
                (controls progress reporting granularity).
            max_errors:      Maximum number of corruption errors before
                stopping early.

        Returns:
            Result dictionary with integrity verification statistics.
        """
        start_time: float = time.time()
        result: dict[str, Any] = {
            "blob_store_name": blob_store_name,
            "blobs_verified": 0,
            "blobs_ok": 0,
            "blobs_corrupted": 0,
            "corrupted_blob_ids": [],
            "duration_ms": 0,
            "status": "error",
        }

        try:
            blobstore: BlobStore | None = self.get_blobstore(blob_store_name)
            if blobstore is None:
                self.logger.warning(
                    "Integrity check skipped — BlobStore '%s' not found.",
                    blob_store_name,
                )
                result["status"] = "skipped"
                result["error"] = f"BlobStore '{blob_store_name}' not registered"
                return result

            blobs_verified: int = 0
            blobs_ok: int = 0
            blobs_corrupted: int = 0
            corrupted_ids: list[str] = []
            stopped_early: bool = False

            # Iterate through all non-deleted blobs in the store
            blob_iterator = blobstore.list_blobs(include_deleted=False)

            batch_count: int = 0
            for blob in blob_iterator:
                blobs_verified += 1
                batch_count += 1

                try:
                    # Read the blob content stream for checksum computation
                    stream = blobstore.get_stream(blob.blob_id)
                    if stream is None:
                        # Blob metadata exists but content is missing
                        blobs_corrupted += 1
                        blob_id_str: str = str(blob.blob_id)
                        corrupted_ids.append(blob_id_str)
                        self.logger.warning(
                            "Integrity check: blob '%s' — content missing "
                            "(metadata exists but stream is None).",
                            blob_id_str,
                        )
                    else:
                        try:
                            # Compute actual checksums from blob content
                            actual_checksums: dict[str, str] = blobstore.compute_checksums(stream)

                            # Compare stored SHA-1 against computed SHA-1
                            stored_sha1: str = blob.attributes.sha1
                            computed_sha1: str = actual_checksums.get("sha1", "")

                            if stored_sha1 and computed_sha1:
                                if stored_sha1.lower() != computed_sha1.lower():
                                    blobs_corrupted += 1
                                    blob_id_str = str(blob.blob_id)
                                    corrupted_ids.append(blob_id_str)
                                    self.logger.warning(
                                        "Integrity check: blob '%s' — "
                                        "SHA-1 mismatch: stored=%s, computed=%s",
                                        blob_id_str,
                                        stored_sha1,
                                        computed_sha1,
                                    )
                                else:
                                    blobs_ok += 1
                            else:
                                # No stored checksum to compare — count as OK
                                blobs_ok += 1
                        finally:
                            # Ensure stream is closed to prevent resource leaks
                            if hasattr(stream, "close"):
                                stream.close()

                except Exception as blob_exc:
                    # Individual blob failure — log and continue
                    blobs_corrupted += 1
                    blob_id_str = str(blob.blob_id)
                    corrupted_ids.append(blob_id_str)
                    self.logger.error(
                        "Integrity check: error verifying blob '%s': %s",
                        blob_id_str,
                        str(blob_exc),
                    )

                # Check early termination condition
                if blobs_corrupted >= max_errors:
                    stopped_early = True
                    self.logger.warning(
                        "Integrity check stopped early for BlobStore '%s': "
                        "reached max_errors=%d after verifying %d blobs.",
                        blob_store_name,
                        max_errors,
                        blobs_verified,
                    )
                    break

                # Log progress at batch boundaries
                if batch_count >= batch_size:
                    self.logger.info(
                        "Integrity check progress for '%s': "
                        "verified=%d, ok=%d, corrupted=%d",
                        blob_store_name,
                        blobs_verified,
                        blobs_ok,
                        blobs_corrupted,
                    )
                    batch_count = 0

            elapsed_ms: int = int((time.time() - start_time) * 1000)

            # Determine final status
            if blobs_corrupted > 0:
                status: str = "corrupted"
            else:
                status = "ok"

            result.update({
                "blobs_verified": blobs_verified,
                "blobs_ok": blobs_ok,
                "blobs_corrupted": blobs_corrupted,
                "corrupted_blob_ids": corrupted_ids,
                "duration_ms": elapsed_ms,
                "status": status,
                "stopped_early": stopped_early,
            })

            if blobs_corrupted > 0:
                self.logger.error(
                    "Integrity check for BlobStore '%s': "
                    "CORRUPTED — verified=%d, ok=%d, corrupted=%d, duration=%dms",
                    blob_store_name,
                    blobs_verified,
                    blobs_ok,
                    blobs_corrupted,
                    elapsed_ms,
                )
            else:
                self.logger.info(
                    "Integrity check for BlobStore '%s': "
                    "OK — verified=%d, corrupted=0, duration=%dms",
                    blob_store_name,
                    blobs_verified,
                    elapsed_ms,
                )

        except Exception as exc:
            elapsed_ms = int((time.time() - start_time) * 1000)
            result["duration_ms"] = elapsed_ms
            result["error"] = str(exc)
            self.logger.error(
                "Integrity check failed for BlobStore '%s': %s",
                blob_store_name,
                str(exc),
                exc_info=True,
            )

        return result

    # ------------------------------------------------------------------
    # Statistics Collection (Feature F-203 / F-401)
    # ------------------------------------------------------------------

    def collect_statistics(self, blob_store_name: str) -> dict[str, Any]:
        """Gather storage usage statistics for a specific BlobStore.

        Queries the concrete BlobStore backend for current metrics and
        persists the updated statistics to the ``BlobStoreConfig`` model
        in the database.

        Args:
            blob_store_name: Name of the target BlobStore.

        Returns:
            Dictionary containing formatted storage statistics.
        """
        start_time: float = time.time()
        result: dict[str, Any] = {
            "blob_store_name": blob_store_name,
            "type": "unknown",
            "total_size_bytes": 0,
            "total_size_formatted": format_file_size(0),
            "blob_count": 0,
            "available_space_bytes": 0,
            "available_space_formatted": format_file_size(0),
            "utilization_percent": 0.0,
            "duration_ms": 0,
            "status": "error",
        }

        try:
            blobstore: BlobStore | None = self.get_blobstore(blob_store_name)
            if blobstore is None:
                self.logger.warning(
                    "Statistics collection skipped — BlobStore '%s' not found.",
                    blob_store_name,
                )
                result["status"] = "skipped"
                result["error"] = f"BlobStore '{blob_store_name}' not registered"
                return result

            # Retrieve current metrics from the BlobStore backend
            metrics: BlobStoreMetrics = blobstore.get_metrics()

            total_size: int = metrics.total_size_bytes
            blob_count: int = metrics.blob_count
            available_space: int = metrics.available_space_bytes

            # Calculate utilization percentage
            utilization: float = 0.0
            if metrics.total_space_bytes > 0:
                utilization = round(
                    (total_size / metrics.total_space_bytes) * 100.0, 2
                )
            elif available_space > 0:
                total_capacity: int = total_size + available_space
                if total_capacity > 0:
                    utilization = round(
                        (total_size / total_capacity) * 100.0, 2
                    )

            # Resolve BlobStore type from config model or metrics
            store_type: str = metrics.blob_store_type

            # Update BlobStoreConfig model in the database
            self._update_blobstore_config(
                blob_store_name,
                total_size,
                blob_count,
                available_space if available_space >= 0 else None,
            )

            elapsed_ms: int = int((time.time() - start_time) * 1000)

            result.update({
                "type": store_type,
                "total_size_bytes": total_size,
                "total_size_formatted": format_file_size(total_size),
                "blob_count": blob_count,
                "available_space_bytes": available_space,
                "available_space_formatted": format_file_size(
                    max(0, available_space)
                ),
                "utilization_percent": utilization,
                "duration_ms": elapsed_ms,
                "status": "ok",
                "metrics": metrics.to_dict(),
            })

            self.logger.info(
                "Statistics collected for BlobStore '%s': "
                "total_size=%s, blob_count=%d, available=%s, "
                "utilization=%.1f%%, duration=%dms",
                blob_store_name,
                format_file_size(total_size),
                blob_count,
                format_file_size(max(0, available_space)),
                utilization,
                elapsed_ms,
            )

        except Exception as exc:
            elapsed_ms = int((time.time() - start_time) * 1000)
            result["duration_ms"] = elapsed_ms
            result["error"] = str(exc)
            self.logger.error(
                "Statistics collection failed for BlobStore '%s': %s",
                blob_store_name,
                str(exc),
                exc_info=True,
            )

        return result

    def collect_all_statistics(self) -> list[dict[str, Any]]:
        """Collect storage statistics from ALL registered BlobStores.

        Iterates through every registered BlobStore and invokes
        :meth:`collect_statistics` on each.  Individual store failures
        do not prevent statistics collection for remaining stores.

        Returns:
            List of statistics dictionaries, one per registered BlobStore.
        """
        results: list[dict[str, Any]] = []
        store_names: list[str] = list(self._registry.keys())

        self.logger.info(
            "Starting statistics collection for %d BlobStore(s).",
            len(store_names),
        )

        for name in store_names:
            result: dict[str, Any] = self.collect_statistics(name)
            results.append(result)

        self.logger.info(
            "Statistics collection completed for %d BlobStore(s).",
            len(results),
        )

        return results

    # ------------------------------------------------------------------
    # Scheduled Task Registration Helpers (Feature F-402)
    # ------------------------------------------------------------------

    def get_compaction_task(self) -> callable:
        """Return a callable suitable for APScheduler job registration
        that executes compaction on all registered BlobStores.

        The returned callable wraps :meth:`compact_all` with logging
        and comprehensive error handling.

        Task type identifier: ``'blobstore.compact'``

        Returns:
            A zero-argument callable for APScheduler.
        """
        service_ref = self

        def _compaction_task() -> list[dict[str, Any]]:
            """Scheduled compaction task — removes soft-deleted blobs
            past the retention period across all BlobStores."""
            task_logger = logging.getLogger(
                f"{__name__}.task.blobstore.compact"
            )
            task_logger.info("Scheduled compaction task started.")
            start_time: float = time.time()
            try:
                results: list[dict[str, Any]] = service_ref.compact_all()
                elapsed: int = int((time.time() - start_time) * 1000)
                task_logger.info(
                    "Scheduled compaction task completed in %dms. "
                    "Stores processed: %d",
                    elapsed,
                    len(results),
                )
                return results
            except Exception as exc:
                elapsed = int((time.time() - start_time) * 1000)
                task_logger.error(
                    "Scheduled compaction task failed after %dms: %s",
                    elapsed,
                    str(exc),
                    exc_info=True,
                )
                return []

        _compaction_task.__name__ = "blobstore.compact"
        _compaction_task.__qualname__ = "blobstore.compact"
        return _compaction_task

    def get_temp_cleanup_task(self) -> callable:
        """Return a callable suitable for APScheduler job registration
        that executes temporary file cleanup on all registered BlobStores.

        Task type identifier: ``'blobstore.cleanup_temp'``

        Returns:
            A zero-argument callable for APScheduler.
        """
        service_ref = self

        def _temp_cleanup_task() -> list[dict[str, Any]]:
            """Scheduled temp cleanup task — removes orphaned temporary
            files across all BlobStores."""
            task_logger = logging.getLogger(
                f"{__name__}.task.blobstore.cleanup_temp"
            )
            task_logger.info("Scheduled temp cleanup task started.")
            start_time: float = time.time()
            try:
                results: list[dict[str, Any]] = service_ref.cleanup_all_temp_files()
                elapsed: int = int((time.time() - start_time) * 1000)
                task_logger.info(
                    "Scheduled temp cleanup task completed in %dms. "
                    "Stores processed: %d",
                    elapsed,
                    len(results),
                )
                return results
            except Exception as exc:
                elapsed = int((time.time() - start_time) * 1000)
                task_logger.error(
                    "Scheduled temp cleanup task failed after %dms: %s",
                    elapsed,
                    str(exc),
                    exc_info=True,
                )
                return []

        _temp_cleanup_task.__name__ = "blobstore.cleanup_temp"
        _temp_cleanup_task.__qualname__ = "blobstore.cleanup_temp"
        return _temp_cleanup_task

    def get_integrity_check_task(self) -> callable:
        """Return a callable suitable for APScheduler job registration
        that executes integrity verification on all registered BlobStores.

        Task type identifier: ``'blobstore.integrity_check'``

        Returns:
            A zero-argument callable for APScheduler.
        """
        service_ref = self

        def _integrity_check_task() -> list[dict[str, Any]]:
            """Scheduled integrity check task — validates blob checksums
            across all BlobStores."""
            task_logger = logging.getLogger(
                f"{__name__}.task.blobstore.integrity_check"
            )
            task_logger.info("Scheduled integrity check task started.")
            start_time: float = time.time()
            results: list[dict[str, Any]] = []
            try:
                for name in list(service_ref._registry.keys()):
                    result: dict[str, Any] = service_ref.verify_integrity(name)
                    results.append(result)
                elapsed: int = int((time.time() - start_time) * 1000)
                task_logger.info(
                    "Scheduled integrity check task completed in %dms. "
                    "Stores processed: %d",
                    elapsed,
                    len(results),
                )
                return results
            except Exception as exc:
                elapsed = int((time.time() - start_time) * 1000)
                task_logger.error(
                    "Scheduled integrity check task failed after %dms: %s",
                    elapsed,
                    str(exc),
                    exc_info=True,
                )
                return results

        _integrity_check_task.__name__ = "blobstore.integrity_check"
        _integrity_check_task.__qualname__ = "blobstore.integrity_check"
        return _integrity_check_task

    def get_statistics_task(self) -> callable:
        """Return a callable suitable for APScheduler job registration
        that collects storage statistics from all registered BlobStores.

        Task type identifier: ``'blobstore.collect_stats'``

        Returns:
            A zero-argument callable for APScheduler.
        """
        service_ref = self

        def _statistics_task() -> list[dict[str, Any]]:
            """Scheduled statistics collection task — gathers storage
            metrics from all BlobStores."""
            task_logger = logging.getLogger(
                f"{__name__}.task.blobstore.collect_stats"
            )
            task_logger.info("Scheduled statistics collection task started.")
            start_time: float = time.time()
            try:
                results: list[dict[str, Any]] = service_ref.collect_all_statistics()
                elapsed: int = int((time.time() - start_time) * 1000)
                task_logger.info(
                    "Scheduled statistics task completed in %dms. "
                    "Stores processed: %d",
                    elapsed,
                    len(results),
                )
                return results
            except Exception as exc:
                elapsed = int((time.time() - start_time) * 1000)
                task_logger.error(
                    "Scheduled statistics task failed after %dms: %s",
                    elapsed,
                    str(exc),
                    exc_info=True,
                )
                return []

        _statistics_task.__name__ = "blobstore.collect_stats"
        _statistics_task.__qualname__ = "blobstore.collect_stats"
        return _statistics_task

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _update_blobstore_config(
        self,
        blob_store_name: str,
        total_size: int,
        blob_count: int,
        available_space: Optional[int] = None,
    ) -> None:
        """Persist updated storage statistics to the BlobStoreConfig model.

        Queries the database for the ``BlobStoreConfig`` record matching
        *blob_store_name* and calls its ``update_stats()`` method.

        Args:
            blob_store_name: Name of the BlobStore to update.
            total_size:      Updated total storage consumed in bytes.
            blob_count:      Updated total number of blobs.
            available_space: Updated available space in bytes
                (``None`` to leave unchanged — common for S3 backends).
        """
        try:
            config: BlobStoreConfig | None = (
                db.session.query(BlobStoreConfig)
                .filter_by(blob_store_name=blob_store_name)
                .first()
            )

            if config is not None:
                config.update_stats(
                    total_size=total_size,
                    blob_count=blob_count,
                    available_space=available_space,
                )
                self.logger.debug(
                    "Updated BlobStoreConfig for '%s': "
                    "total_size=%d, blob_count=%d, available_space=%s",
                    blob_store_name,
                    total_size,
                    blob_count,
                    available_space,
                )
            else:
                self.logger.warning(
                    "BlobStoreConfig record not found for '%s' — "
                    "skipping statistics persistence.",
                    blob_store_name,
                )
        except Exception as exc:
            # Statistics persistence failure must not crash maintenance
            self.logger.error(
                "Failed to update BlobStoreConfig for '%s': %s",
                blob_store_name,
                str(exc),
                exc_info=True,
            )
            try:
                db.session.rollback()
            except Exception:
                pass


# ===========================================================================
# Module-Level Factory and Singleton
# ===========================================================================


def create_maintenance_service(
    blobstore_registry: dict[str, BlobStore] | None = None,
) -> BlobStoreMaintenanceService:
    """Factory function to create a new ``BlobStoreMaintenanceService``.

    Provides a clean entry point for creating service instances with
    pre-populated BlobStore registries.  Useful in testing scenarios
    where a fresh service instance is needed for each test.

    Args:
        blobstore_registry: Optional mapping of BlobStore name →
            BlobStore instance.  Defaults to an empty registry.

    Returns:
        A newly created ``BlobStoreMaintenanceService`` instance.
    """
    return BlobStoreMaintenanceService(blobstore_registry=blobstore_registry)


# Module-level singleton instance — registered with the scheduler during
# application bootstrap.  External modules import this instance for
# direct invocation or scheduler registration.
maintenance_service: BlobStoreMaintenanceService = BlobStoreMaintenanceService()

logger.debug("BlobStore maintenance module loaded. Singleton service created.")
