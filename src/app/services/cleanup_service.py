"""
Cleanup Policy Evaluation and Execution Service (Feature F-204).

Implements the business logic for managing, evaluating, and executing cleanup
policies against repository assets.  Cleanup policies define rules for
automatic removal of stale or unwanted content from repositories based on
configurable criteria (last-download age, blob-update age, regex patterns,
pre-release version matching).

**Architecture Context:**

Replaces the Java cleanup evaluators from the original Sonatype Nexus
Repository system.  The ``CleanupPolicy`` model (defined in
``src.app.models.cleanup_policy``) stores policy definitions with criteria
JSON; this service performs the actual evaluation logic, preview, and
execution (including blob deletion and orphaned component cleanup).

**Feature Support:**

- **F-204** (Cleanup Policies): Format-specific cleanup rule execution, policy
  evaluation, asset deletion, and orphaned component removal.
- **F-303** (Audit Logging): Emits ``CLEANUP_COMPLETED`` events via Blinker
  event bus after each repository cleanup.
- **F-402** (Scheduled Tasks): The ``run_all_repository_cleanups()`` method
  serves as the APScheduler task entry point.
- **F-503** (Webhook Integration): Cleanup events feed into webhook dispatch.

**Criteria Evaluation Logic:**

All criteria in a single policy are combined with **AND** logic — an asset
must match *all* specified criteria to be selected for cleanup.  Supported
criteria keys:

+---------------------------+-------+----------------------------------------------+
| Key                       | Type  | Description                                  |
+===========================+=======+==============================================+
| ``lastDownloadedBefore``  | int   | Days since last download threshold           |
+---------------------------+-------+----------------------------------------------+
| ``lastBlobUpdatedBefore`` | int   | Days since blob was last updated threshold   |
+---------------------------+-------+----------------------------------------------+
| ``regexPattern``          | str   | Regex matching asset path                    |
+---------------------------+-------+----------------------------------------------+
| ``isPrerelease``          | bool  | Match pre-release versions only              |
+---------------------------+-------+----------------------------------------------+

**Exported API:**

- :class:`CleanupService` — primary service class with CRUD + evaluation +
  execution methods
- :data:`DEFAULT_BATCH_SIZE` — default batch size for asset deletion processing
- :data:`MAX_CLEANUP_ASSETS` — maximum assets to process in a single cleanup run
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from flask import current_app
from sqlalchemy import and_, or_

from src.app.extensions import db
from src.app.models.cleanup_policy import CleanupPolicy
from src.app.models.repository import Repository
from src.app.models.component import Component
from src.app.models.asset import Asset
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType, CleanupEventPayload
from src.app.services.blobstore_service import BlobStoreService

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for all cleanup service operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BATCH_SIZE: int = 100
"""Default number of assets processed per batch during cleanup execution.

Batching prevents out-of-memory conditions and excessive transaction lock
durations when processing repositories with large numbers of stale assets.
Can be overridden via ``CLEANUP_BATCH_SIZE`` in Flask app config.
"""

MAX_CLEANUP_ASSETS: int = 50000
"""Maximum number of assets that may be processed in a single cleanup run.

This safety limit prevents runaway cleanup operations from consuming
excessive resources.  If a policy evaluation matches more assets than this
limit, only the first ``MAX_CLEANUP_ASSETS`` are processed and a warning
is emitted.  Can be overridden via ``CLEANUP_MAX_ASSETS`` in Flask app
config.
"""

# ---------------------------------------------------------------------------
# Internal Constants
# ---------------------------------------------------------------------------

_VALID_FORMATS: frozenset[str] = frozenset({
    "maven2", "npm", "docker", "nuget", "pypi", "apt", "raw",
})
"""Valid repository format identifiers for cleanup policy ``format`` field.

The special value ``'*'`` (all formats) is also accepted but is not in this
set — it is handled separately in validation logic.
"""

_VALID_CRITERIA_KEYS: frozenset[str] = frozenset({
    "lastDownloadedBefore",
    "lastBlobUpdatedBefore",
    "regexPattern",
    "isPrerelease",
})
"""Supported criteria keys in the cleanup policy criteria JSON dict."""

_PRERELEASE_PATTERN: re.Pattern[str] = re.compile(
    r"(-SNAPSHOT|-alpha|-beta|-rc|-RC|-M\d|-dev|-preview)",
    re.IGNORECASE,
)
"""Compiled regex pattern for identifying pre-release version strings.

Matches common pre-release indicators used across package ecosystems:
    - Maven: ``-SNAPSHOT``
    - SemVer: ``-alpha``, ``-beta``, ``-rc``, ``-RC``
    - Spring: ``-M1``, ``-M2`` (milestone releases)
    - Generic: ``-dev``, ``-preview``
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "CleanupService",
    "DEFAULT_BATCH_SIZE",
    "MAX_CLEANUP_ASSETS",
]


# ===========================================================================
# CleanupService
# ===========================================================================


class CleanupService:
    """Central service for cleanup policy management and execution.

    Provides the full lifecycle for cleanup policies:

    1. **CRUD Operations** — Create, read, update, and delete cleanup policy
       definitions stored in the ``cleanup_policies`` table.
    2. **Policy Evaluation** — Evaluate cleanup criteria against repository
       assets to determine which assets match for cleanup.
    3. **Preview** — Dry-run mode that returns matched assets and projected
       impact without performing any deletions.
    4. **Execution** — Full cleanup execution with asset deletion, blob
       soft-delete, and orphaned component removal.
    5. **Scheduled Task** — ``run_all_repository_cleanups()`` provides the
       APScheduler task entry point for automated, system-wide cleanup.

    **Event Integration:**

    After each repository cleanup execution, a ``CLEANUP_COMPLETED`` event
    is emitted via the Blinker event bus for audit logging (F-303) and
    webhook dispatch (F-503).

    Usage::

        service = CleanupService()

        # Create a policy
        policy = service.create_policy(
            name="cleanup-old-snapshots",
            format_type="maven2",
            criteria={
                "lastDownloadedBefore": 30,
                "isPrerelease": True,
            },
            description="Remove Maven SNAPSHOT artifacts not downloaded in 30 days",
        )

        # Preview cleanup impact
        preview = service.preview_cleanup(policy.policy_id, "maven-snapshots")

        # Execute cleanup
        result = service.execute_cleanup("maven-snapshots")
    """

    def __init__(self) -> None:
        """Initialise the CleanupService.

        Sets up:
        - A module-scoped logger for structured logging.
        - A lazily-initialised BlobStoreService reference for blob deletion.
        """
        self.logger: logging.Logger = logging.getLogger(__name__)
        self._blobstore_service: BlobStoreService | None = None

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _get_blobstore_service(self) -> BlobStoreService:
        """Return the BlobStoreService, creating it lazily if needed.

        Lazy initialisation avoids circular import issues and ensures the
        service is only instantiated when blob operations are actually
        required (i.e., during cleanup execution, not during CRUD
        operations or previews).

        Returns:
            A :class:`BlobStoreService` instance.
        """
        if self._blobstore_service is None:
            self._blobstore_service = BlobStoreService()
            self.logger.debug("BlobStoreService lazily initialised for cleanup.")
        return self._blobstore_service

    def _get_batch_size(self) -> int:
        """Resolve the batch size for cleanup processing.

        Checks Flask app config for a ``CLEANUP_BATCH_SIZE`` override,
        falling back to :data:`DEFAULT_BATCH_SIZE`.

        Returns:
            Positive integer batch size.
        """
        try:
            return int(
                current_app.config.get("CLEANUP_BATCH_SIZE", DEFAULT_BATCH_SIZE)
            )
        except (RuntimeError, ValueError):
            # RuntimeError: outside Flask app context
            # ValueError: invalid config value
            return DEFAULT_BATCH_SIZE

    def _get_max_assets(self) -> int:
        """Resolve the maximum assets limit for cleanup processing.

        Checks Flask app config for a ``CLEANUP_MAX_ASSETS`` override,
        falling back to :data:`MAX_CLEANUP_ASSETS`.

        Returns:
            Positive integer asset limit.
        """
        try:
            return int(
                current_app.config.get("CLEANUP_MAX_ASSETS", MAX_CLEANUP_ASSETS)
            )
        except (RuntimeError, ValueError):
            return MAX_CLEANUP_ASSETS

    @staticmethod
    def _validate_criteria(criteria: dict[str, Any]) -> None:
        """Validate the structure and types of cleanup criteria.

        Ensures:
        - Only recognised criteria keys are present.
        - ``lastDownloadedBefore`` is a positive integer.
        - ``lastBlobUpdatedBefore`` is a positive integer.
        - ``regexPattern`` is a valid, compilable regular expression string.
        - ``isPrerelease`` is a boolean.

        Args:
            criteria: The criteria dictionary to validate.

        Raises:
            ValueError: If any criteria key or value is invalid.
        """
        if not isinstance(criteria, dict):
            raise ValueError("Criteria must be a dictionary.")

        unknown_keys = set(criteria.keys()) - _VALID_CRITERIA_KEYS
        if unknown_keys:
            raise ValueError(
                f"Unknown criteria keys: {', '.join(sorted(unknown_keys))}. "
                f"Valid keys: {', '.join(sorted(_VALID_CRITERIA_KEYS))}."
            )

        # Validate lastDownloadedBefore
        if "lastDownloadedBefore" in criteria:
            val = criteria["lastDownloadedBefore"]
            if not isinstance(val, int) or val <= 0:
                raise ValueError(
                    "Criteria 'lastDownloadedBefore' must be a positive integer "
                    f"(days). Got: {val!r}"
                )

        # Validate lastBlobUpdatedBefore
        if "lastBlobUpdatedBefore" in criteria:
            val = criteria["lastBlobUpdatedBefore"]
            if not isinstance(val, int) or val <= 0:
                raise ValueError(
                    "Criteria 'lastBlobUpdatedBefore' must be a positive integer "
                    f"(days). Got: {val!r}"
                )

        # Validate regexPattern
        if "regexPattern" in criteria:
            pattern = criteria["regexPattern"]
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError(
                    "Criteria 'regexPattern' must be a non-empty string."
                )
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"Criteria 'regexPattern' is not a valid regex: {exc}"
                ) from exc

        # Validate isPrerelease
        if "isPrerelease" in criteria:
            val = criteria["isPrerelease"]
            if not isinstance(val, bool):
                raise ValueError(
                    f"Criteria 'isPrerelease' must be a boolean. Got: {val!r}"
                )

    @staticmethod
    def _validate_format(format_type: str) -> None:
        """Validate a format type identifier.

        Accepts all values in :data:`_VALID_FORMATS` plus the wildcard
        ``'*'`` (matches all formats).

        Args:
            format_type: The format type to validate.

        Raises:
            ValueError: If the format type is not recognised.
        """
        if format_type != "*" and format_type not in _VALID_FORMATS:
            raise ValueError(
                f"Invalid format type: '{format_type}'. "
                f"Valid values: {', '.join(sorted(_VALID_FORMATS))}, or '*' "
                f"for all formats."
            )

    # ------------------------------------------------------------------
    # Phase 3: Policy CRUD Operations
    # ------------------------------------------------------------------

    def create_policy(
        self,
        name: str,
        format_type: str,
        criteria: dict[str, Any],
        description: str | None = None,
    ) -> CleanupPolicy:
        """Create a new cleanup policy.

        Validates the policy name for uniqueness, the format type against
        the list of supported repository formats, and the criteria
        structure for correctness before persisting the policy.

        Args:
            name:        Unique, human-readable policy name.
            format_type: Target repository format (e.g., ``'maven2'``,
                         ``'npm'``) or ``'*'`` for all formats.
            criteria:    Cleanup criteria dictionary.  Valid keys:
                         ``lastDownloadedBefore`` (int days),
                         ``lastBlobUpdatedBefore`` (int days),
                         ``regexPattern`` (str), ``isPrerelease`` (bool).
            description: Optional human-readable description.

        Returns:
            The newly created :class:`CleanupPolicy` instance.

        Raises:
            ValueError: If the name, format, or criteria are invalid, or
                if a policy with the same name already exists.
        """
        # Validate inputs
        if not name or not name.strip():
            raise ValueError("Policy name must not be empty.")

        self._validate_format(format_type)
        self._validate_criteria(criteria)

        # Check uniqueness
        existing = CleanupPolicy.query.filter_by(name=name).first()
        if existing is not None:
            raise ValueError(
                f"A cleanup policy named '{name}' already exists."
            )

        # Determine the format value to store (None for wildcard)
        stored_format: str | None = None if format_type == "*" else format_type

        # Create and persist
        policy = CleanupPolicy(
            policy_id=name,  # Use name as ID for simplicity
            name=name,
            format=stored_format,
            criteria=criteria,
            description=description,
        )
        policy.save()

        self.logger.info(
            "Created cleanup policy '%s' (format=%s, criteria_keys=%s).",
            name,
            format_type,
            list(criteria.keys()),
        )
        return policy

    def get_policy(self, policy_id: str) -> CleanupPolicy | None:
        """Retrieve a cleanup policy by its identifier.

        Args:
            policy_id: The unique policy identifier (primary key).

        Returns:
            The :class:`CleanupPolicy` instance if found, or ``None``.
        """
        policy = CleanupPolicy.query.filter_by(policy_id=policy_id).first()
        if policy is None:
            self.logger.debug("Cleanup policy '%s' not found.", policy_id)
        return policy

    def list_policies(
        self, format_type: str | None = None
    ) -> list[CleanupPolicy]:
        """List all cleanup policies, optionally filtered by format.

        When ``format_type`` is specified, returns only policies whose
        ``format`` column matches *or* policies that are format-agnostic
        (``format`` is ``None``).

        Args:
            format_type: Optional repository format to filter by.

        Returns:
            A list of :class:`CleanupPolicy` instances.
        """
        query = CleanupPolicy.query

        if format_type is not None:
            # Include format-specific policies AND format-agnostic ones
            query = query.filter(
                or_(
                    CleanupPolicy.format == format_type,
                    CleanupPolicy.format.is_(None),
                )
            )

        policies = query.order_by(CleanupPolicy.name).all()
        self.logger.debug(
            "Listed %d cleanup policies (format_filter=%s).",
            len(policies),
            format_type,
        )
        return policies

    def update_policy(
        self, policy_id: str, **kwargs: Any
    ) -> CleanupPolicy:
        """Update an existing cleanup policy.

        Supports updating ``criteria``, ``description``, ``name``, and
        ``format`` fields.  Updated criteria are validated before
        persistence.

        Args:
            policy_id: The unique policy identifier.
            **kwargs:  Keyword arguments for fields to update.  Supported
                       keys: ``criteria``, ``description``, ``name``,
                       ``format_type``.

        Returns:
            The updated :class:`CleanupPolicy` instance.

        Raises:
            ValueError: If the policy is not found or if updated criteria
                are invalid.
        """
        policy = self.get_policy(policy_id)
        if policy is None:
            raise ValueError(
                f"Cleanup policy '{policy_id}' not found."
            )

        # Validate and apply criteria update
        if "criteria" in kwargs:
            self._validate_criteria(kwargs["criteria"])
            policy.criteria = kwargs["criteria"]

        # Validate and apply format update
        if "format_type" in kwargs:
            fmt = kwargs["format_type"]
            self._validate_format(fmt)
            policy.format = None if fmt == "*" else fmt

        # Apply description update
        if "description" in kwargs:
            policy.description = kwargs["description"]

        # Apply name update (check uniqueness)
        if "name" in kwargs and kwargs["name"] != policy.name:
            new_name = kwargs["name"]
            if not new_name or not new_name.strip():
                raise ValueError("Policy name must not be empty.")
            existing = CleanupPolicy.query.filter_by(name=new_name).first()
            if existing is not None:
                raise ValueError(
                    f"A cleanup policy named '{new_name}' already exists."
                )
            policy.name = new_name

        db.session.add(policy)
        db.session.commit()

        self.logger.info(
            "Updated cleanup policy '%s' (fields=%s).",
            policy_id,
            list(kwargs.keys()),
        )
        return policy

    def delete_policy(self, policy_id: str) -> bool:
        """Delete a cleanup policy.

        Checks if any repositories still reference this policy in their
        ``cleanup_policies`` JSON array.  If references exist, a warning
        is logged but the deletion proceeds (soft policy removal).

        Args:
            policy_id: The unique policy identifier.

        Returns:
            ``True`` if the policy was found and deleted, ``False`` if
            the policy did not exist.
        """
        policy = self.get_policy(policy_id)
        if policy is None:
            self.logger.warning(
                "Attempted to delete non-existent cleanup policy '%s'.",
                policy_id,
            )
            return False

        # Check for repositories referencing this policy
        referencing_repos = self._find_referencing_repositories(policy.name)
        if referencing_repos:
            self.logger.warning(
                "Cleanup policy '%s' is still referenced by %d repository(ies): %s. "
                "Proceeding with deletion — references will become stale.",
                policy.name,
                len(referencing_repos),
                [r.name for r in referencing_repos],
            )

        policy.delete()
        self.logger.info("Deleted cleanup policy '%s'.", policy_id)
        return True

    def _find_referencing_repositories(
        self, policy_name: str
    ) -> list[Repository]:
        """Find repositories that reference a policy by name.

        Scans all repositories and checks their ``cleanup_policies`` JSON
        array for the given policy name.

        Args:
            policy_name: The policy name to search for.

        Returns:
            A list of :class:`Repository` instances that reference the
            policy.
        """
        referencing: list[Repository] = []
        repositories = Repository.query.all()
        for repo in repositories:
            repo_policies = repo.cleanup_policies
            if repo_policies and isinstance(repo_policies, list):
                if policy_name in repo_policies:
                    referencing.append(repo)
        return referencing

    # ------------------------------------------------------------------
    # Phase 4: Cleanup Evaluation Engine
    # ------------------------------------------------------------------

    def evaluate_policy(
        self,
        policy: CleanupPolicy,
        repository: Repository,
    ) -> list[Asset]:
        """Evaluate a cleanup policy against a repository's assets.

        Builds a SQLAlchemy query against the :class:`Asset` model for
        the given repository and applies all criteria filters using AND
        logic (all specified criteria must match for an asset to be
        selected).

        **Criteria evaluation order:**

        1. ``lastDownloadedBefore`` — assets not downloaded within N days
           (including assets *never* downloaded).
        2. ``lastBlobUpdatedBefore`` — assets whose blob was not updated
           within N days.
        3. ``regexPattern`` — assets whose path matches the regex.
        4. ``isPrerelease`` — assets whose component version contains
           pre-release indicators.

        For the ``regexPattern`` and ``isPrerelease`` criteria, Python-side
        filtering is used for cross-database compatibility (SQLite does not
        support native regex).

        Args:
            policy:     The cleanup policy to evaluate.
            repository: The target repository.

        Returns:
            A list of :class:`Asset` instances matching all criteria,
            limited to :data:`MAX_CLEANUP_ASSETS`.
        """
        criteria: dict[str, Any] = policy.criteria or {}
        if not criteria:
            self.logger.debug(
                "Policy '%s' has empty criteria — no assets matched.",
                policy.name,
            )
            return []

        max_assets = self._get_max_assets()
        needs_python_filter = False

        # Start building the base query for assets in this repository
        query = db.session.query(Asset).filter(
            Asset.repository_name == repository.name
        )

        # --- Criterion 1: lastDownloadedBefore ---
        if "lastDownloadedBefore" in criteria:
            days = criteria["lastDownloadedBefore"]
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            query = query.filter(
                or_(
                    Asset.last_downloaded < cutoff,
                    Asset.last_downloaded.is_(None),
                )
            )
            self.logger.debug(
                "Applied lastDownloadedBefore filter: cutoff=%s (days=%d).",
                cutoff.isoformat(),
                days,
            )

        # --- Criterion 2: lastBlobUpdatedBefore ---
        if "lastBlobUpdatedBefore" in criteria:
            days = criteria["lastBlobUpdatedBefore"]
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            query = query.filter(Asset.updated_at < cutoff)
            self.logger.debug(
                "Applied lastBlobUpdatedBefore filter: cutoff=%s (days=%d).",
                cutoff.isoformat(),
                days,
            )

        # --- Criteria 3 & 4 require Python-side filtering ---
        has_regex = "regexPattern" in criteria
        has_prerelease = criteria.get("isPrerelease", False)

        if has_regex or has_prerelease:
            needs_python_filter = True

        # If we need Python-side filtering, fetch a larger set from DB
        # then filter in Python.  Otherwise, limit at DB level.
        if needs_python_filter:
            # Fetch up to max_assets * 2 to allow for Python filtering
            # reducing the set, but cap for safety
            db_limit = min(max_assets * 2, 100000)
            candidate_assets: list[Asset] = query.limit(db_limit).all()
            matched_assets: list[Asset] = []

            # Compile regex pattern if needed
            regex_compiled: re.Pattern[str] | None = None
            if has_regex:
                try:
                    regex_compiled = re.compile(criteria["regexPattern"])
                except re.error:
                    self.logger.error(
                        "Invalid regex in policy '%s': %s",
                        policy.name,
                        criteria["regexPattern"],
                    )
                    return []

            for asset in candidate_assets:
                # Apply regexPattern filter
                if regex_compiled is not None:
                    asset_path = asset.path or ""
                    if not re.search(regex_compiled, asset_path):
                        continue

                # Apply isPrerelease filter
                if has_prerelease:
                    # Need to check component version for pre-release indicators
                    component_version = self._get_component_version(asset)
                    if component_version is None:
                        # No version info — cannot determine pre-release status;
                        # skip this asset for isPrerelease criteria
                        continue
                    if not re.search(_PRERELEASE_PATTERN, component_version):
                        continue

                matched_assets.append(asset)
                if len(matched_assets) >= max_assets:
                    break

            if len(matched_assets) >= max_assets:
                self.logger.warning(
                    "Policy '%s' matched %d+ assets in repo '%s' — "
                    "capped at MAX_CLEANUP_ASSETS=%d.",
                    policy.name,
                    len(matched_assets),
                    repository.name,
                    max_assets,
                )

            self.logger.info(
                "Policy '%s' evaluated against repo '%s': %d assets matched "
                "(Python-side filtering applied).",
                policy.name,
                repository.name,
                len(matched_assets),
            )
            return matched_assets
        else:
            # Pure DB-side filtering — apply limit directly
            matched_assets = query.limit(max_assets).all()

            if len(matched_assets) >= max_assets:
                self.logger.warning(
                    "Policy '%s' matched %d+ assets in repo '%s' — "
                    "capped at MAX_CLEANUP_ASSETS=%d.",
                    policy.name,
                    len(matched_assets),
                    repository.name,
                    max_assets,
                )

            self.logger.info(
                "Policy '%s' evaluated against repo '%s': %d assets matched.",
                policy.name,
                repository.name,
                len(matched_assets),
            )
            return matched_assets

    @staticmethod
    def _get_component_version(asset: Asset) -> str | None:
        """Extract the component version for an asset.

        Accesses the asset's related component and returns its version
        string.  Returns ``None`` if the asset has no associated component
        or the component has no version.

        Args:
            asset: The asset to extract the version from.

        Returns:
            The component version string, or ``None``.
        """
        if asset.component_id is None:
            return None
        component = Component.query.filter_by(id=asset.component_id).first()
        if component is None:
            return None
        return component.version

    def preview_cleanup(
        self,
        policy_id: str,
        repository_name: str,
    ) -> dict[str, Any]:
        """Preview cleanup impact without performing deletions.

        Evaluates the specified policy against the repository and returns
        a summary of matched assets and projected impact metrics.

        Args:
            policy_id:       The unique policy identifier.
            repository_name: The target repository name.

        Returns:
            A dictionary containing:
            - ``policy_name``: Name of the evaluated policy.
            - ``repository``: Target repository name.
            - ``matched_assets``: Number of matched assets.
            - ``matched_components``: Number of affected components.
            - ``total_size``: Total size in bytes of matched assets.
            - ``sample_assets``: Up to 20 sample assets with path and size.

        Raises:
            ValueError: If the policy or repository is not found.
        """
        policy = self.get_policy(policy_id)
        if policy is None:
            raise ValueError(f"Cleanup policy '{policy_id}' not found.")

        repository = Repository.query.filter_by(name=repository_name).first()
        if repository is None:
            raise ValueError(f"Repository '{repository_name}' not found.")

        # Check format compatibility
        if policy.format is not None and policy.format != repository.format:
            self.logger.info(
                "Policy '%s' (format=%s) does not apply to repo '%s' (format=%s).",
                policy.name,
                policy.format,
                repository.name,
                repository.format,
            )
            return {
                "policy_name": policy.name,
                "repository": repository_name,
                "matched_assets": 0,
                "matched_components": 0,
                "total_size": 0,
                "sample_assets": [],
            }

        matched_assets = self.evaluate_policy(policy, repository)

        # Compute affected components
        affected_component_ids: set[int] = set()
        for asset in matched_assets:
            if asset.component_id is not None:
                affected_component_ids.add(asset.component_id)

        total_size = sum(a.size or 0 for a in matched_assets)

        sample_assets = [
            {"path": a.path, "size": a.size}
            for a in matched_assets[:20]
        ]

        self.logger.info(
            "Preview cleanup for policy '%s' on repo '%s': "
            "%d assets, %d components, %d bytes.",
            policy.name,
            repository_name,
            len(matched_assets),
            len(affected_component_ids),
            total_size,
        )

        return {
            "policy_name": policy.name,
            "repository": repository_name,
            "matched_assets": len(matched_assets),
            "matched_components": len(affected_component_ids),
            "total_size": total_size,
            "sample_assets": sample_assets,
        }

    # ------------------------------------------------------------------
    # Phase 5: Cleanup Execution
    # ------------------------------------------------------------------

    def execute_cleanup(
        self,
        repository_name: str,
        policy_names: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute cleanup on a repository using its assigned policies.

        Loads the repository and its associated cleanup policies, evaluates
        each policy against the repository's assets, and (unless in dry-run
        mode) deletes matched assets from the database and BlobStore, then
        removes orphaned components.

        After execution, a ``CLEANUP_COMPLETED`` event is emitted via the
        Blinker event bus for audit logging and webhook dispatch.

        Args:
            repository_name: The target repository name.
            policy_names:    Optional explicit list of policy names to
                             evaluate.  If ``None``, uses the repository's
                             ``cleanup_policies`` JSON array.
            dry_run:         If ``True``, evaluate policies but do not
                             perform any deletions (preview mode).

        Returns:
            A dictionary containing execution results:
            - ``repository``: Target repository name.
            - ``policies_evaluated``: Number of policies evaluated.
            - ``total_assets_deleted``: Total assets deleted.
            - ``total_components_deleted``: Orphaned components removed.
            - ``total_space_freed``: Bytes of storage freed.
            - ``dry_run``: Whether this was a dry run.
            - ``policy_results``: Per-policy results list.
            - ``duration_ms``: Execution duration in milliseconds.

        Raises:
            ValueError: If the repository is not found.
        """
        start_time = time.monotonic()

        # Load repository
        repository = Repository.query.filter_by(name=repository_name).first()
        if repository is None:
            raise ValueError(f"Repository '{repository_name}' not found.")

        # Resolve policy names
        if policy_names is None:
            repo_policy_list = repository.cleanup_policies
            if not repo_policy_list or not isinstance(repo_policy_list, list):
                self.logger.info(
                    "Repository '%s' has no assigned cleanup policies.",
                    repository_name,
                )
                return {
                    "repository": repository_name,
                    "policies_evaluated": 0,
                    "total_assets_deleted": 0,
                    "total_components_deleted": 0,
                    "total_space_freed": 0,
                    "dry_run": dry_run,
                    "policy_results": [],
                    "duration_ms": 0,
                }
            policy_names = repo_policy_list

        total_assets_deleted: int = 0
        total_space_freed: int = 0
        total_components_deleted: int = 0
        policy_results: list[dict[str, Any]] = []

        for pname in policy_names:
            policy = self.get_policy(pname)
            if policy is None:
                self.logger.warning(
                    "Cleanup policy '%s' referenced by repo '%s' not found — skipping.",
                    pname,
                    repository_name,
                )
                policy_results.append({
                    "policy_name": pname,
                    "status": "not_found",
                    "assets_deleted": 0,
                    "space_freed": 0,
                })
                continue

            # Check format compatibility
            if policy.format is not None and policy.format != repository.format:
                self.logger.info(
                    "Policy '%s' (format=%s) skipped for repo '%s' (format=%s).",
                    policy.name,
                    policy.format,
                    repository_name,
                    repository.format,
                )
                policy_results.append({
                    "policy_name": pname,
                    "status": "format_mismatch",
                    "assets_deleted": 0,
                    "space_freed": 0,
                })
                continue

            # Evaluate policy
            matched_assets = self.evaluate_policy(policy, repository)

            if not matched_assets:
                policy_results.append({
                    "policy_name": pname,
                    "status": "no_matches",
                    "assets_deleted": 0,
                    "space_freed": 0,
                })
                continue

            assets_deleted = 0
            space_freed = 0

            if not dry_run:
                # Execute deletion in batches
                assets_deleted, space_freed = self._delete_assets_batch(
                    matched_assets, repository
                )
                total_assets_deleted += assets_deleted
                total_space_freed += space_freed

                self.logger.info(
                    "Policy '%s' cleanup on repo '%s': deleted %d assets, "
                    "freed %d bytes.",
                    pname,
                    repository_name,
                    assets_deleted,
                    space_freed,
                )
            else:
                # Dry run — compute metrics without deleting
                assets_deleted = len(matched_assets)
                space_freed = sum(a.size or 0 for a in matched_assets)
                total_assets_deleted += assets_deleted
                total_space_freed += space_freed

            policy_results.append({
                "policy_name": pname,
                "status": "completed" if not dry_run else "dry_run",
                "assets_matched": len(matched_assets),
                "assets_deleted": assets_deleted,
                "space_freed": space_freed,
            })

        # Cleanup orphaned components (only in non-dry-run mode)
        if not dry_run and total_assets_deleted > 0:
            total_components_deleted = self._cleanup_orphaned_components(
                repository_name
            )
            self.logger.info(
                "Cleaned up %d orphaned components in repo '%s'.",
                total_components_deleted,
                repository_name,
            )

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Emit cleanup completion event (only in non-dry-run mode)
        if not dry_run:
            # Determine the policy name for the event (use first policy or aggregate)
            event_policy_name = (
                policy_names[0] if len(policy_names) == 1
                else f"aggregate({len(policy_names)} policies)"
            )
            try:
                emit_event(
                    EventType.CLEANUP_COMPLETED,
                    payload={
                        "policy_name": event_policy_name,
                        "repository_name": repository_name,
                        "components_deleted": total_components_deleted,
                        "assets_deleted": total_assets_deleted,
                        "space_reclaimed_bytes": total_space_freed,
                        "duration_ms": duration_ms,
                    },
                )
            except Exception as exc:
                self.logger.error(
                    "Failed to emit CLEANUP_COMPLETED event: %s",
                    str(exc),
                    exc_info=True,
                )

        result: dict[str, Any] = {
            "repository": repository_name,
            "policies_evaluated": len(policy_names),
            "total_assets_deleted": total_assets_deleted,
            "total_components_deleted": total_components_deleted,
            "total_space_freed": total_space_freed,
            "dry_run": dry_run,
            "policy_results": policy_results,
            "duration_ms": duration_ms,
        }

        self.logger.info(
            "Cleanup execution on repo '%s': %d assets deleted, %d components "
            "deleted, %d bytes freed (dry_run=%s, duration=%dms).",
            repository_name,
            total_assets_deleted,
            total_components_deleted,
            total_space_freed,
            dry_run,
            duration_ms,
        )

        return result

    def _delete_assets_batch(
        self,
        assets: list[Asset],
        repository: Repository,
    ) -> tuple[int, int]:
        """Delete assets in batches with per-asset error handling.

        For each asset:
        1. Soft-delete the blob from the BlobStore (if blob_ref exists).
        2. Soft-delete the asset record in the database.

        Errors on individual assets are caught and logged but do not abort
        the entire batch — processing continues with the next asset.

        Args:
            assets:     List of assets to delete.
            repository: The owning repository (used to resolve BlobStore).

        Returns:
            A tuple of ``(deleted_count, freed_bytes)`` — the number of
            assets successfully deleted and the total bytes freed.
        """
        batch_size = self._get_batch_size()
        deleted_count: int = 0
        freed_bytes: int = 0

        blobstore_service = self._get_blobstore_service()

        for i in range(0, len(assets), batch_size):
            batch = assets[i:i + batch_size]
            batch_deleted = 0
            batch_freed = 0

            for asset in batch:
                try:
                    asset_size = asset.size or 0

                    # Step 1: Delete blob from BlobStore (soft-delete)
                    if asset.blob_ref:
                        try:
                            store = blobstore_service.get_store_for_repository(
                                repository.name
                            )
                            # Import BlobId for creating reference
                            from src.app.storage.blobstore import BlobId
                            blob_id = BlobId.from_string(asset.blob_ref)
                            store.delete(blob_id, soft=True)
                        except Exception as blob_exc:
                            self.logger.warning(
                                "Failed to delete blob for asset '%s' "
                                "(blob_ref=%s): %s. Continuing with DB deletion.",
                                asset.path,
                                asset.blob_ref,
                                str(blob_exc),
                            )

                    # Step 2: Soft-delete the asset record
                    asset.soft_delete()

                    batch_deleted += 1
                    batch_freed += asset_size

                except Exception as exc:
                    self.logger.error(
                        "Error deleting asset '%s' (id=%s) in repo '%s': %s",
                        asset.path,
                        asset.id,
                        repository.name,
                        str(exc),
                        exc_info=True,
                    )
                    # Rollback the session to recover from potential
                    # inconsistent state after a failed deletion
                    try:
                        db.session.rollback()
                    except Exception:
                        pass

            deleted_count += batch_deleted
            freed_bytes += batch_freed

            self.logger.debug(
                "Batch %d-%d: deleted %d/%d assets, freed %d bytes.",
                i,
                i + len(batch),
                batch_deleted,
                len(batch),
                batch_freed,
            )

        return deleted_count, freed_bytes

    def _cleanup_orphaned_components(self, repository_name: str) -> int:
        """Find and delete components with zero remaining active assets.

        After asset deletion, some components may have no remaining assets.
        This method identifies and removes those orphaned components to
        maintain data integrity.

        Uses a subquery approach for cross-database compatibility (SQLite
        and PostgreSQL).

        Args:
            repository_name: The repository to scan for orphaned components.

        Returns:
            The number of orphaned components deleted.
        """
        try:
            # Find component IDs that still have at least one active asset
            active_component_ids_subquery = (
                db.session.query(Asset.component_id)
                .filter(
                    and_(
                        Asset.component_id.isnot(None),
                        Asset.repository_name == repository_name,
                        Asset.is_deleted == False,  # noqa: E712
                    )
                )
                .distinct()
                .subquery()
            )

            # Find orphaned components — those NOT in the active set
            orphaned_components = (
                db.session.query(Component)
                .filter(
                    and_(
                        Component.repository_name == repository_name,
                        ~Component.id.in_(
                            db.session.query(
                                active_component_ids_subquery.c.component_id
                            )
                        ),
                    )
                )
                .all()
            )

            deleted_count = 0
            for component in orphaned_components:
                try:
                    db.session.delete(component)
                    deleted_count += 1
                except Exception as exc:
                    self.logger.warning(
                        "Failed to delete orphaned component %s (id=%s): %s",
                        component.name,
                        component.id,
                        str(exc),
                    )

            if deleted_count > 0:
                db.session.commit()

            self.logger.debug(
                "Orphan cleanup for repo '%s': found %d orphans, deleted %d.",
                repository_name,
                len(orphaned_components),
                deleted_count,
            )
            return deleted_count

        except Exception as exc:
            self.logger.error(
                "Error during orphan component cleanup for repo '%s': %s",
                repository_name,
                str(exc),
                exc_info=True,
            )
            db.session.rollback()
            return 0

    # ------------------------------------------------------------------
    # Phase 6: Scheduled Cleanup Task
    # ------------------------------------------------------------------

    def run_all_repository_cleanups(self) -> dict[str, Any]:
        """Execute cleanup on all repositories with assigned policies.

        This method serves as the APScheduler task entry point (Feature
        F-402).  It iterates through all repositories that have a
        non-empty ``cleanup_policies`` JSON array and executes cleanup
        for each one.

        Individual repository cleanup failures are caught and logged but
        do not prevent processing of subsequent repositories.

        Returns:
            An aggregate summary dictionary containing:
            - ``repositories_processed``: Number of repos processed.
            - ``total_assets_deleted``: Total assets deleted across all repos.
            - ``total_components_deleted``: Total orphaned components removed.
            - ``total_space_freed``: Total bytes freed across all repos.
            - ``duration_ms``: Total execution duration in milliseconds.
            - ``repository_results``: Per-repository result summaries.
            - ``errors``: List of repositories that encountered errors.
        """
        start_time = time.monotonic()
        self.logger.info("Starting scheduled cleanup across all repositories.")

        # Find all repositories with assigned cleanup policies
        all_repos = Repository.query.all()
        repos_with_policies = [
            repo for repo in all_repos
            if repo.cleanup_policies
            and isinstance(repo.cleanup_policies, list)
            and len(repo.cleanup_policies) > 0
        ]

        if not repos_with_policies:
            self.logger.info("No repositories have assigned cleanup policies.")
            return {
                "repositories_processed": 0,
                "total_assets_deleted": 0,
                "total_components_deleted": 0,
                "total_space_freed": 0,
                "duration_ms": 0,
                "repository_results": [],
                "errors": [],
            }

        total_assets_deleted: int = 0
        total_components_deleted: int = 0
        total_space_freed: int = 0
        repository_results: list[dict[str, Any]] = []
        errors: list[str] = []

        for repo in repos_with_policies:
            try:
                self.logger.info(
                    "Running cleanup for repo '%s' (%d policies).",
                    repo.name,
                    len(repo.cleanup_policies),
                )
                result = self.execute_cleanup(repo.name)
                total_assets_deleted += result["total_assets_deleted"]
                total_components_deleted += result["total_components_deleted"]
                total_space_freed += result["total_space_freed"]
                repository_results.append({
                    "repository": repo.name,
                    "result": result,
                })
            except Exception as exc:
                self.logger.error(
                    "Cleanup failed for repo '%s': %s",
                    repo.name,
                    str(exc),
                    exc_info=True,
                )
                errors.append(repo.name)
                repository_results.append({
                    "repository": repo.name,
                    "error": str(exc),
                })

        duration_ms = int((time.monotonic() - start_time) * 1000)

        summary: dict[str, Any] = {
            "repositories_processed": len(repos_with_policies),
            "total_assets_deleted": total_assets_deleted,
            "total_components_deleted": total_components_deleted,
            "total_space_freed": total_space_freed,
            "duration_ms": duration_ms,
            "repository_results": repository_results,
            "errors": errors,
        }

        self.logger.info(
            "Scheduled cleanup completed: %d repos processed, %d assets deleted, "
            "%d components deleted, %d bytes freed (%dms, %d errors).",
            len(repos_with_policies),
            total_assets_deleted,
            total_components_deleted,
            total_space_freed,
            duration_ms,
            len(errors),
        )

        return summary
