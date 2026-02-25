"""
CleanupPolicy SQLAlchemy Model.

Defines the ``CleanupPolicy`` SQLAlchemy model that replaces the
CLEANUP_POLICY entity from the original Java DataStore schema (Section
6.2.1.2).  Cleanup policies define rules for automatic removal of stale or
unwanted content from repositories.  They are evaluated by scheduled cleanup
tasks (Feature F-402 — APScheduler) against repository assets.  Supports
Feature F-204 (Cleanup Policies).

**Architecture Context:**

Replaces the MyBatis 3.5.15 ``CLEANUP_POLICY`` mapper from the original Java
source system (Sonatype Nexus Repository).  In the Java architecture, cleanup
policies were stored via MyBatis XML mappers with JSON criteria columns; here,
SQLAlchemy's Declarative ORM with the portable ``JSON`` column type provides
equivalent functionality.

**Criteria Structure:**

The ``criteria`` JSON column supports the following optional fields, combined
with **AND** logic when multiple are present:

+---------------------------+-------+-----------------------------------------------+
| Key                       | Type  | Description                                   |
+===========================+=======+===============================================+
| ``lastDownloadedBefore``  | int   | Days since last download threshold            |
+---------------------------+-------+-----------------------------------------------+
| ``lastBlobUpdatedBefore`` | int   | Days since blob was last updated threshold    |
+---------------------------+-------+-----------------------------------------------+
| ``regexPattern``          | str   | Regex matching component version / asset path |
+---------------------------+-------+-----------------------------------------------+
| ``isPrerelease``          | bool  | Match pre-release versions only               |
+---------------------------+-------+-----------------------------------------------+

**Compatibility:**

Supports both SQLite (standalone deployments) and PostgreSQL (clustered /
enterprise deployments) via portable SQLAlchemy types.  No database-specific
types (e.g., ``JSONB``, ``ARRAY``) are used.

**Relationships:**

- Referenced by name from ``Repository.cleanup_policies`` JSON array.
- Evaluated by ``src.app.services.cleanup_service`` during scheduled cleanup
  task execution.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import Column, JSON, String, Text

from src.app.extensions import db
from src.app.models.base import BaseModel, JSONAttributesMixin, TimestampMixin

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# Provides structured logging for model-level diagnostics.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_FORMATS: tuple[str, ...] = (
    "maven2",
    "npm",
    "docker",
    "nuget",
    "pypi",
    "apt",
    "raw",
)
"""Tuple of valid repository format identifiers.

Used for documentation and optional validation of the ``format`` column.
Values correspond to the seven supported repository formats defined in AAP
Section 0.2.4 (Features F-101-RQ-001 through F-101-RQ-007).
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = ["CleanupPolicy"]


# ===========================================================================
# CleanupPolicy Model
# ===========================================================================


class CleanupPolicy(BaseModel, TimestampMixin, JSONAttributesMixin):
    """SQLAlchemy model for cleanup policy definitions.

    Cleanup policies define rules for automatic removal of stale or unwanted
    content from repositories.  They are stored in the ``cleanup_policies``
    table and evaluated by scheduled cleanup tasks (Feature F-402) against
    repository assets.

    Each policy specifies:

    - A **unique name** and optional description.
    - An optional **target format** (``None`` = applies to all formats).
    - **Criteria rules** (JSON dict) that determine which assets to clean up.

    Criteria fields are combined with **AND** logic — ALL specified criteria
    must match for an asset to be selected for cleanup.

    The actual cleanup evaluation logic resides in
    ``src.app.services.cleanup_service``; this model only stores the policy
    definition.

    Inherited Functionality:

    - ``BaseModel``: ``save()``, ``delete()``, ``update()``, ``to_dict()``
    - ``TimestampMixin``: ``created_at``, ``updated_at`` (UTC timestamps)
    - ``JSONAttributesMixin``: ``attributes``, ``get_attribute()``,
      ``set_attribute()``, ``remove_attribute()``, ``merge_attributes()``

    Example::

        policy = CleanupPolicy(
            policy_id="cleanup-snapshots",
            name="Cleanup Maven Snapshots",
            format="maven2",
            criteria={
                "lastDownloadedBefore": 30,
                "regexPattern": ".*-SNAPSHOT",
            },
            description="Remove Maven SNAPSHOT artifacts not downloaded in 30 days",
        )
        policy.save()

        assert policy.has_download_criteria is True
        assert policy.has_regex_criteria is True
        assert policy.has_age_criteria is False
        assert policy.is_format_specific is True
        assert policy.applies_to_format("maven2") is True
        assert policy.applies_to_format("npm") is False
    """

    __tablename__: str = "cleanup_policies"

    # -- Table-level constraints and indexes ---------------------------------
    __table_args__ = (
        db.Index("ix_cleanup_policies_format", "format"),
    )

    # -----------------------------------------------------------------------
    # Column Definitions
    # -----------------------------------------------------------------------
    # Maps to CLEANUP_POLICY entity from DataStore schema (Section 6.2.1.2).
    # Primary key: policy_id
    # Unique constraint: name
    # -----------------------------------------------------------------------

    policy_id: str = Column(
        String(200),
        primary_key=True,
        doc=(
            "Unique policy identifier.  Used as the primary key and "
            "referenced by Repository.cleanup_policies JSON array."
        ),
    )

    name: str = Column(
        String(255),
        nullable=False,
        unique=True,
        doc=(
            "Human-readable policy name.  Must be unique across all policies. "
            'Examples: "cleanup-snapshots", "cleanup-stale-downloads".'
        ),
    )

    format: Optional[str] = Column(
        "format",
        String(50),
        nullable=True,
        doc=(
            "Target repository format for this policy, or None for "
            "format-agnostic policies that apply to all repository formats.  "
            "Valid values: maven2, npm, docker, nuget, pypi, apt, raw."
        ),
    )

    criteria: dict = Column(
        JSON,
        nullable=False,
        default=dict,
        doc=(
            "JSON dict containing cleanup rule criteria.  All fields are "
            "optional; multiple fields are combined with AND logic.  "
            "Supported keys: lastDownloadedBefore (int days), "
            "lastBlobUpdatedBefore (int days), regexPattern (str regex), "
            "isPrerelease (bool)."
        ),
    )

    description: Optional[str] = Column(
        Text,
        nullable=True,
        doc="Optional human-readable description of the cleanup policy.",
    )

    # ``attributes`` column is provided by JSONAttributesMixin.
    # ``created_at`` column is provided by TimestampMixin.
    # ``updated_at`` column is provided by TimestampMixin.

    # -----------------------------------------------------------------------
    # Representation
    # -----------------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Overrides ``BaseModel.__repr__`` with a CleanupPolicy-specific format
        that includes both the policy_id and the human-readable name for quick
        identification during debugging and logging.

        Returns:
            String in the format ``<CleanupPolicy policy_id: name>``.
        """
        return f"<CleanupPolicy {self.policy_id}: {self.name}>"

    # -----------------------------------------------------------------------
    # Criteria Property Methods
    # -----------------------------------------------------------------------

    @property
    def has_download_criteria(self) -> bool:
        """Check if this policy has a last-downloaded threshold criterion.

        Returns ``True`` if the ``criteria`` JSON dict contains the
        ``lastDownloadedBefore`` key, which specifies the maximum number of
        days since an asset was last downloaded.  During cleanup evaluation,
        assets whose ``last_downloaded`` timestamp exceeds this threshold are
        selected for removal.

        Returns:
            ``True`` if ``lastDownloadedBefore`` is present in criteria.
        """
        if not self.criteria:
            return False
        return "lastDownloadedBefore" in self.criteria

    @property
    def has_age_criteria(self) -> bool:
        """Check if this policy has a blob-age threshold criterion.

        Returns ``True`` if the ``criteria`` JSON dict contains the
        ``lastBlobUpdatedBefore`` key, which specifies the maximum number of
        days since a blob was last updated.  During cleanup evaluation, blobs
        whose last-updated timestamp exceeds this threshold are selected for
        removal.

        Returns:
            ``True`` if ``lastBlobUpdatedBefore`` is present in criteria.
        """
        if not self.criteria:
            return False
        return "lastBlobUpdatedBefore" in self.criteria

    @property
    def has_regex_criteria(self) -> bool:
        """Check if this policy has a regex pattern matching criterion.

        Returns ``True`` if the ``criteria`` JSON dict contains the
        ``regexPattern`` key, which specifies a regex pattern for matching
        component versions or asset paths.  During cleanup evaluation, assets
        whose version or path matches the regex are selected for removal.

        Returns:
            ``True`` if ``regexPattern`` is present in criteria.
        """
        if not self.criteria:
            return False
        return "regexPattern" in self.criteria

    @property
    def has_prerelease_criteria(self) -> bool:
        """Check if this policy targets pre-release versions.

        Returns ``True`` if the ``criteria`` JSON dict contains
        ``isPrerelease`` set to ``True``.  Pre-release definition is
        format-specific:

        - **Maven**: SNAPSHOT versions (e.g., ``1.0.0-SNAPSHOT``)
        - **npm / NuGet**: Semantic versioning pre-release tags
          (e.g., ``1.0.0-beta.1``)

        Unlike other criteria properties, this checks **both** key presence
        **and** that the value is ``True`` — not just the presence of the key.

        Returns:
            ``True`` if ``isPrerelease`` is present in criteria AND is ``True``.
        """
        if not self.criteria:
            return False
        return self.criteria.get("isPrerelease", False) is True

    @property
    def is_format_specific(self) -> bool:
        """Check if this policy targets a specific repository format.

        Format-agnostic policies (``format=None``) apply to **all** repository
        formats during cleanup evaluation.  Format-specific policies only
        apply to repositories whose format matches the policy's ``format``
        value.

        Returns:
            ``True`` if ``format`` is not ``None``.
        """
        return self.format is not None

    # -----------------------------------------------------------------------
    # Format Matching
    # -----------------------------------------------------------------------

    def applies_to_format(self, format_name: str) -> bool:
        """Check if this cleanup policy applies to a given repository format.

        A policy applies to a format if either:

        1. The policy is **format-agnostic** (``format is None``) — applies to
           all formats.
        2. The policy's ``format`` matches the given *format_name*
           (case-insensitive comparison).

        Args:
            format_name: The repository format to check against this policy
                (e.g., ``"maven2"``, ``"npm"``, ``"docker"``).

        Returns:
            ``True`` if this policy applies to the specified format.
        """
        # Format-agnostic policies apply to every format
        if self.format is None:
            return True
        # Guard against empty or None format_name
        if not format_name:
            return False
        # Case-insensitive format matching
        return self.format.lower() == format_name.lower()

    # -----------------------------------------------------------------------
    # Serialization Override
    # -----------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert this model instance to a plain dictionary.

        Extends ``BaseModel.to_dict()`` with computed property values for
        convenience when serializing to API responses.  The computed properties
        provide quick introspection into the policy's criteria configuration
        without requiring the caller to parse the JSON ``criteria`` dict.

        Returns:
            Dictionary containing all mapped column values (``policy_id``,
            ``name``, ``format``, ``criteria``, ``description``,
            ``attributes``, ``created_at``, ``updated_at``) plus the
            following computed properties:

            - ``has_download_criteria`` (bool)
            - ``has_age_criteria`` (bool)
            - ``has_regex_criteria`` (bool)
            - ``has_prerelease_criteria`` (bool)
            - ``is_format_specific`` (bool)
        """
        result: dict[str, Any] = super().to_dict()
        result["has_download_criteria"] = self.has_download_criteria
        result["has_age_criteria"] = self.has_age_criteria
        result["has_regex_criteria"] = self.has_regex_criteria
        result["has_prerelease_criteria"] = self.has_prerelease_criteria
        result["is_format_specific"] = self.is_format_specific
        return result


# ---------------------------------------------------------------------------
# Module load confirmation
# ---------------------------------------------------------------------------

logger.debug(
    "CleanupPolicy model module loaded — table: %s",
    CleanupPolicy.__tablename__,
)
