"""Repository management service for CRUD operations across all repository types.

Provides the ``RepositoryService`` class for managing Hosted, Proxy, and
Group repositories across all 7 supported formats (Maven, npm, Docker,
NuGet, PyPI, APT, Raw).

Features: F-101 (Multi-Format Support), F-102 (Repository Types).

Usage::

    from src.services.repository_service import RepositoryService

    svc = RepositoryService(db_session=session)
    repo = svc.create_repository(config)
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPOSITORY_FORMATS = ("maven", "npm", "docker", "nuget", "pypi", "apt", "raw")
REPOSITORY_TYPES = ("hosted", "proxy", "group")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RepositoryNotFoundError(LookupError):
    """Requested repository does not exist."""


class RepositoryConflictError(ValueError):
    """Operation would create a duplicate or circular reference."""


class InvalidRepositoryConfigError(ValueError):
    """Repository configuration is invalid."""


# ---------------------------------------------------------------------------
# RepositoryService
# ---------------------------------------------------------------------------

class RepositoryService:
    """Manage binary repository CRUD operations and lifecycle.

    Parameters
    ----------
    db_session : object or None
        SQLAlchemy-compatible session for persistence operations.
    storage_service : object or None
        Storage backend service for BlobStore management.
    """

    def __init__(
        self,
        db_session: Any = None,
        storage_service: Any = None,
    ) -> None:
        self.db_session = db_session
        self.session = db_session  # alias for fixture wiring
        self.storage_service = storage_service
        # In-memory store for unit-test isolation (keyed by repo ID)
        self._repos: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_name(name: str) -> None:
        """Validate repository name.

        Raises
        ------
        ValueError
            If the name is empty or exceeds 255 characters.
        """
        if not name:
            raise ValueError("name required")
        if len(name) > 255:
            raise ValueError("name exceeds maximum length of 255 characters")

    @staticmethod
    def _validate_type(repo_type: str) -> None:
        """Validate repository type.

        Raises
        ------
        ValueError
            If the type is not one of ``hosted``, ``proxy``, ``group``.
        """
        if repo_type not in REPOSITORY_TYPES:
            raise ValueError(
                f"invalid type '{repo_type}'; must be one of {REPOSITORY_TYPES}"
            )

    @staticmethod
    def _validate_format(repo_format: str) -> None:
        """Validate repository format.

        Raises
        ------
        ValueError
            If the format is not in the supported list.
        """
        if repo_format not in REPOSITORY_FORMATS:
            raise ValueError(
                f"unsupported format '{repo_format}'; "
                f"must be one of {REPOSITORY_FORMATS}"
            )

    def _validate_proxy_config(self, config: Dict[str, Any]) -> None:
        """Validate proxy-specific configuration.

        Raises
        ------
        ValueError
            If ``proxy.remote_url`` is missing.
        """
        proxy = config.get("proxy", {})
        if not proxy.get("remote_url"):
            raise ValueError("missing remote_url for proxy repository")

    def _validate_group_config(self, config: Dict[str, Any]) -> None:
        """Validate group-specific configuration and detect circular refs.

        Raises
        ------
        ValueError
            If a circular member reference is detected.
        """
        group = config.get("group", {})
        member_names = group.get("member_names", [])
        repo_name = config.get("name", "")
        if repo_name in member_names:
            raise ValueError("circular reference: group cannot include itself")
        # Check for cross-circular references in existing repos
        for member in member_names:
            member_repo = self._repos.get(member) or self._find_by_name(member)
            if member_repo and isinstance(member_repo, dict):
                member_group = member_repo.get("group", {})
                if repo_name in member_group.get("member_names", []):
                    raise ValueError("circular reference detected")

    def _find_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Look up a repository by name in the in-memory store."""
        for repo in self._repos.values():
            if repo.get("name") == name:
                return repo
        return None

    # ------------------------------------------------------------------
    # CRUD Operations
    # ------------------------------------------------------------------

    def create_repository(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new repository from a configuration dictionary.

        Parameters
        ----------
        config : dict
            Repository configuration with at minimum ``name``, ``type``,
            and ``format`` keys.

        Returns
        -------
        dict
            The persisted repository configuration with an ``id`` assigned.

        Raises
        ------
        ValueError
            If validation fails (name, type, format, proxy URL, circular refs).
        RuntimeError
            If a database commit error occurs.
        """
        self._validate_name(config.get("name", ""))
        self._validate_type(config.get("type", ""))
        self._validate_format(config.get("format", ""))

        # Check duplicate name
        if self._find_by_name(config["name"]):
            raise ValueError(f"duplicate: repository '{config['name']}' exists")

        # Type-specific validation
        repo_type = config["type"]
        if repo_type == "proxy":
            self._validate_proxy_config(config)
        elif repo_type == "group":
            self._validate_group_config(config)

        # Assign ID and persist
        repo = copy.deepcopy(config)
        if "id" not in repo:
            repo["id"] = str(uuid.uuid4())
        self._repos[repo["id"]] = repo

        # Persist via db session if available
        if self.db_session:
            try:
                self.db_session.add(repo)
                self.db_session.commit()
            except Exception as exc:
                self.db_session.rollback()
                raise RuntimeError(f"db commit failed: {exc}") from exc

        return repo

    def get_repository(self, repo_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a repository by its unique ID.

        Parameters
        ----------
        repo_id : str
            Repository identifier.

        Returns
        -------
        dict or None
            Repository configuration, or ``None`` if not found.
        """
        return self._repos.get(repo_id)

    def get_repository_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Retrieve a repository by its unique name.

        Parameters
        ----------
        name : str
            Repository name.

        Returns
        -------
        dict or None
            Repository configuration, or ``None`` if not found.
        """
        return self._find_by_name(name)

    def list_repositories(self, **filters: Any) -> List[Dict[str, Any]]:
        """List all repositories, optionally filtered.

        Parameters
        ----------
        **filters
            Optional keyword filters (``type``, ``format``, ``online``).

        Returns
        -------
        list[dict]
            List of repository configuration dicts.
        """
        repos = list(self._repos.values())
        for key, value in filters.items():
            repos = [r for r in repos if r.get(key) == value]
        return repos

    def update_repository(
        self, repo_id: str, updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update an existing repository's configuration.

        Parameters
        ----------
        repo_id : str
            Repository identifier.
        updates : dict
            Fields to update.

        Returns
        -------
        dict
            The updated repository configuration.

        Raises
        ------
        LookupError
            If the repository is not found.
        """
        repo = self._repos.get(repo_id)
        if repo is None:
            raise LookupError(f"not found: repository '{repo_id}'")
        repo.update(updates)
        if self.db_session:
            self.db_session.commit()
        return repo

    def delete_repository(self, repo_id: str) -> bool:
        """Delete a repository by ID.

        Parameters
        ----------
        repo_id : str
            Repository identifier.

        Returns
        -------
        bool
            ``True`` if the repository was deleted.

        Raises
        ------
        LookupError
            If the repository is not found.
        """
        if repo_id not in self._repos:
            raise LookupError(f"not found: repository '{repo_id}'")
        del self._repos[repo_id]
        if self.db_session:
            self.db_session.commit()
        return True

    def validate_repository_config(self, config: Dict[str, Any]) -> bool:
        """Validate a repository configuration without persisting.

        Parameters
        ----------
        config : dict
            Repository configuration to validate.

        Returns
        -------
        bool
            ``True`` if valid.

        Raises
        ------
        ValueError
            If any validation rule fails.
        """
        self._validate_name(config.get("name", ""))
        self._validate_type(config.get("type", ""))
        self._validate_format(config.get("format", ""))
        if config.get("type") == "proxy":
            self._validate_proxy_config(config)
        if config.get("type") == "group":
            self._validate_group_config(config)
        return True

    def get_repository_formats(self) -> List[str]:
        """Return the list of supported repository formats.

        Returns
        -------
        list[str]
            All supported format identifiers.
        """
        return list(REPOSITORY_FORMATS)
