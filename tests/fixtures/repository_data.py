"""
Repository configuration test data factories for the Flask Binary Repository
Management System.

Provides factory functions for generating repository configuration test data
covering all 3 repository types (Hosted, Proxy, Group) × 7 formats (Maven,
npm, Docker, NuGet, PyPI, APT, Raw).  Every ``make_*`` function returns a
plain ``dict`` and accepts ``**overrides`` for flexible customisation.

Deterministic output is guaranteed by seeding the Faker instance used for
synthetic data generation.

Usage examples::

    from tests.fixtures.repository_data import (
        make_hosted_repo_maven,
        make_proxy_repo_npm,
        make_group_repo_with_members,
        make_all_format_repos,
    )

    maven_repo = make_hosted_repo_maven()
    npm_proxy  = make_proxy_repo_npm()
    group_set  = make_group_repo_with_members(format_type='docker')
    all_hosted = make_all_format_repos(repo_type='hosted')
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import factory
import factory.fuzzy
from faker import Faker

# ---------------------------------------------------------------------------
# Deterministic Faker instance (seeded for reproducible test runs)
# ---------------------------------------------------------------------------
fake = Faker()
Faker.seed(54321)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPOSITORY_FORMATS: List[str] = [
    "maven",
    "npm",
    "docker",
    "nuget",
    "pypi",
    "apt",
    "raw",
]
"""All seven supported repository formats."""

REPOSITORY_TYPES: List[str] = [
    "hosted",
    "proxy",
    "group",
]
"""All three supported repository types."""

FORMAT_CONTENT_TYPES: Dict[str, str] = {
    "maven": "application/java-archive",
    "npm": "application/gzip",
    "docker": "application/vnd.docker.distribution.manifest.v2+json",
    "nuget": "application/zip",
    "pypi": "application/gzip",
    "apt": "application/vnd.debian.binary-package",
    "raw": "application/octet-stream",
}
"""Primary content-type string associated with each repository format."""

DEFAULT_REMOTE_URLS: Dict[str, str] = {
    "maven": "https://repo1.maven.org/maven2/",
    "npm": "https://registry.npmjs.org/",
    "docker": "https://registry-1.docker.io",
    "nuget": "https://api.nuget.org/v3/index.json",
    "pypi": "https://pypi.org/",
    "apt": "http://archive.ubuntu.com/ubuntu/",
    "raw": "https://raw.example.com/public/",
}
"""Default upstream remote URLs for each repository format (proxy repos)."""

# Internal sequence counter for unique repository name generation.
_sequence_counter: int = 0


def _next_sequence() -> int:
    """Return a monotonically-increasing integer for unique naming."""
    global _sequence_counter
    _sequence_counter += 1
    return _sequence_counter


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# Phase 2 – Base Repository Factory
# ============================================================================


def make_repository_base(
    name: Optional[str] = None,
    format_type: str = "maven",
    repo_type: str = "hosted",
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate a base repository configuration dict with common fields.

    Parameters
    ----------
    name:
        Human-readable repository name.  Auto-generated when *None*.
    format_type:
        One of :data:`REPOSITORY_FORMATS`.
    repo_type:
        One of :data:`REPOSITORY_TYPES`.
    **overrides:
        Arbitrary key/value pairs merged into the returned dict, allowing
        callers to override any default value.

    Returns
    -------
    Dict[str, Any]
        A dictionary representing the repository configuration.
    """
    seq = _next_sequence()
    generated_name = name or f"test-{format_type}-{repo_type}-{seq}-{fake.slug()}"

    now_iso = _utcnow_iso()

    base: Dict[str, Any] = {
        "id": str(fake.uuid4()),
        "name": generated_name,
        "format": format_type,
        "type": repo_type,
        "online": True,
        "description": fake.sentence(nb_words=8),
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_modified": fake.date_time_this_year(tzinfo=timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


# ============================================================================
# Phase 3 – Hosted Repository Factories
# ============================================================================


def make_hosted_repo(
    name: Optional[str] = None,
    format_type: str = "maven",
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate a complete *hosted* repository configuration dict.

    Hosted repositories store locally-published artefacts.  This function
    populates storage, cleanup, and component configuration blocks in
    addition to the common fields.

    Parameters
    ----------
    name:
        Repository name. Auto-generated when *None*.
    format_type:
        One of :data:`REPOSITORY_FORMATS`.
    **overrides:
        Merged into the returned dict.
    """
    repo = make_repository_base(
        name=name, format_type=format_type, repo_type="hosted", **overrides
    )
    repo.setdefault(
        "storage",
        {
            "blob_store_name": "default",
            "strict_content_type_validation": True,
            "write_policy": "ALLOW_ONCE",
        },
    )
    repo.setdefault("cleanup", {"policy_names": []})
    repo.setdefault("component", {"proprietary_components": True})
    return repo


def make_hosted_repo_maven(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for a Maven hosted repository.

    Sets Maven-specific defaults: ``version_policy``, ``layout_policy``,
    and ``content_disposition``.
    """
    defaults: Dict[str, Any] = {
        "maven": {
            "version_policy": "MIXED",
            "layout_policy": "STRICT",
            "content_disposition": "ATTACHMENT",
        },
    }
    defaults.update(overrides)
    return make_hosted_repo(format_type="maven", **defaults)


def make_hosted_repo_npm(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for an npm hosted repository."""
    return make_hosted_repo(format_type="npm", **overrides)


def make_hosted_repo_docker(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for a Docker hosted repository.

    Sets Docker-specific defaults: ``http_port``, ``https_port``,
    ``force_basic_auth``, and ``v1_enabled``.
    """
    defaults: Dict[str, Any] = {
        "docker": {
            "http_port": 8082,
            "https_port": 8083,
            "force_basic_auth": True,
            "v1_enabled": False,
        },
    }
    defaults.update(overrides)
    return make_hosted_repo(format_type="docker", **defaults)


def make_hosted_repo_nuget(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for a NuGet hosted repository."""
    return make_hosted_repo(format_type="nuget", **overrides)


def make_hosted_repo_pypi(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for a PyPI hosted repository."""
    return make_hosted_repo(format_type="pypi", **overrides)


def make_hosted_repo_apt(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for an APT hosted repository.

    Sets APT-specific defaults: ``distribution``, ``flat``, and
    ``gpg_key`` (a 40-character hex string generated by Faker).
    """
    defaults: Dict[str, Any] = {
        "apt": {
            "distribution": "bionic",
            "flat": False,
            "gpg_key": fake.hexify(text="^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ ^^^^"),
        },
    }
    defaults.update(overrides)
    return make_hosted_repo(format_type="apt", **defaults)


def make_hosted_repo_raw(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for a Raw hosted repository."""
    return make_hosted_repo(format_type="raw", **overrides)


# ============================================================================
# Phase 4 – Proxy Repository Factories
# ============================================================================


def make_proxy_repo(
    name: Optional[str] = None,
    format_type: str = "maven",
    remote_url: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate a complete *proxy* repository configuration dict.

    Proxy repositories cache artefacts fetched from an upstream remote
    registry.  When *remote_url* is ``None`` the format-specific default
    from :data:`DEFAULT_REMOTE_URLS` is used.

    Parameters
    ----------
    name:
        Repository name. Auto-generated when *None*.
    format_type:
        One of :data:`REPOSITORY_FORMATS`.
    remote_url:
        The upstream URL to proxy.  Falls back to the format default.
    **overrides:
        Merged into the returned dict.
    """
    resolved_remote_url = remote_url or DEFAULT_REMOTE_URLS.get(
        format_type, "https://example.com/remote/"
    )

    repo = make_repository_base(
        name=name, format_type=format_type, repo_type="proxy", **overrides
    )
    repo.setdefault(
        "proxy",
        {
            "remote_url": resolved_remote_url,
            "content_max_age": 1440,
            "metadata_max_age": 1440,
        },
    )
    repo.setdefault(
        "http_client",
        {
            "blocked": False,
            "auto_block": True,
            "connection": {
                "retries": 0,
                "timeout": 60,
            },
        },
    )
    repo.setdefault(
        "negative_cache",
        {
            "enabled": True,
            "time_to_live": 1440,
        },
    )
    repo.setdefault(
        "storage",
        {
            "blob_store_name": "default",
            "strict_content_type_validation": True,
        },
    )
    return repo


def make_proxy_repo_maven(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for a Maven Central proxy repository."""
    return make_proxy_repo(
        format_type="maven",
        remote_url="https://repo1.maven.org/maven2/",
        **overrides,
    )


def make_proxy_repo_npm(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for an npmjs proxy repository."""
    return make_proxy_repo(
        format_type="npm",
        remote_url="https://registry.npmjs.org/",
        **overrides,
    )


def make_proxy_repo_docker(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for a Docker Hub proxy repository."""
    return make_proxy_repo(
        format_type="docker",
        remote_url="https://registry-1.docker.io",
        **overrides,
    )


def make_proxy_repo_nuget(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for a NuGet Gallery proxy repository."""
    return make_proxy_repo(
        format_type="nuget",
        remote_url="https://api.nuget.org/v3/index.json",
        **overrides,
    )


def make_proxy_repo_pypi(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for a PyPI proxy repository."""
    return make_proxy_repo(
        format_type="pypi",
        remote_url="https://pypi.org/",
        **overrides,
    )


def make_proxy_repo_apt(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for an Ubuntu APT proxy repository."""
    return make_proxy_repo(
        format_type="apt",
        remote_url="http://archive.ubuntu.com/ubuntu/",
        **overrides,
    )


def make_proxy_repo_raw(**overrides: Any) -> Dict[str, Any]:
    """Convenience factory for a Raw proxy repository with a generic URL."""
    return make_proxy_repo(
        format_type="raw",
        remote_url="https://raw.example.com/public/",
        **overrides,
    )


# ============================================================================
# Phase 5 – Group Repository Factories
# ============================================================================


def make_group_repo(
    name: Optional[str] = None,
    format_type: str = "maven",
    member_names: Optional[List[str]] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate a *group* repository configuration dict.

    Group repositories aggregate multiple member repositories (hosted and
    proxy) under a single virtual endpoint.

    Parameters
    ----------
    name:
        Repository name. Auto-generated when *None*.
    format_type:
        One of :data:`REPOSITORY_FORMATS`.
    member_names:
        List of repository names that this group aggregates.  Defaults
        to an empty list.
    **overrides:
        Merged into the returned dict.
    """
    repo = make_repository_base(
        name=name, format_type=format_type, repo_type="group", **overrides
    )
    repo.setdefault(
        "group",
        {
            "member_names": list(member_names) if member_names else [],
        },
    )
    return repo


def make_group_repo_with_members(
    format_type: str = "maven",
    hosted_count: int = 1,
    proxy_count: int = 1,
) -> Dict[str, Any]:
    """Generate a complete group repository setup with member repos.

    Creates *hosted_count* hosted repos and *proxy_count* proxy repos,
    then builds a group repo that references all of them.

    Returns
    -------
    Dict[str, Any]
        ``{"group": <group config dict>, "members": [<member dicts>]}``
    """
    members: List[Dict[str, Any]] = []

    for _ in range(hosted_count):
        members.append(make_hosted_repo(format_type=format_type))

    for _ in range(proxy_count):
        members.append(make_proxy_repo(format_type=format_type))

    member_names = [m["name"] for m in members]

    group = make_group_repo(
        format_type=format_type,
        member_names=member_names,
    )
    return {"group": group, "members": members}


# ============================================================================
# Phase 6 – Parametrised Data Generators
# ============================================================================


def make_all_format_repos(repo_type: str = "hosted") -> List[Dict[str, Any]]:
    """Generate one repository config for each of the 7 supported formats.

    Useful for ``@pytest.mark.parametrize`` data sources.

    Parameters
    ----------
    repo_type:
        The repository type to generate (``'hosted'``, ``'proxy'``, or
        ``'group'``).

    Returns
    -------
    List[Dict[str, Any]]
        Seven repository configuration dicts.
    """
    repos: List[Dict[str, Any]] = []
    for fmt in REPOSITORY_FORMATS:
        if repo_type == "hosted":
            repos.append(make_hosted_repo(format_type=fmt))
        elif repo_type == "proxy":
            repos.append(make_proxy_repo(format_type=fmt))
        elif repo_type == "group":
            repos.append(make_group_repo(format_type=fmt))
        else:
            repos.append(make_repository_base(format_type=fmt, repo_type=repo_type))
    return repos


def make_all_type_repos(format_type: str = "maven") -> List[Dict[str, Any]]:
    """Generate one repository of each type for the given format.

    Returns
    -------
    List[Dict[str, Any]]
        Three repository configuration dicts (hosted, proxy, group).
    """
    return [
        make_hosted_repo(format_type=format_type),
        make_proxy_repo(format_type=format_type),
        make_group_repo(format_type=format_type),
    ]


def get_repo_format_ids() -> List[str]:
    """Return format ID strings suitable for ``pytest.mark.parametrize`` *ids*."""
    return list(REPOSITORY_FORMATS)


def get_repo_type_ids() -> List[str]:
    """Return type ID strings suitable for ``pytest.mark.parametrize`` *ids*."""
    return list(REPOSITORY_TYPES)


# ============================================================================
# Phase 7 – Edge-Case Repository Data
# ============================================================================


def make_repo_with_duplicate_name(
    name: str = "duplicate-test",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return two repository configs that share the same name.

    This is used to test conflict / uniqueness-constraint behaviour.

    Returns
    -------
    Tuple[Dict[str, Any], Dict[str, Any]]
        A pair of repository configuration dicts with identical ``name``.
    """
    repo_a = make_hosted_repo(name=name, format_type="maven")
    repo_b = make_hosted_repo(name=name, format_type="maven")
    return repo_a, repo_b


def make_repo_with_max_name_length(length: int = 255) -> Dict[str, Any]:
    """Return a repository config whose name is *length* characters long.

    Useful for boundary-value testing of name length constraints.
    """
    long_name = "r" * length
    return make_hosted_repo(name=long_name, format_type="maven")


def make_repo_with_special_characters() -> Dict[str, Any]:
    """Return a repository config with special / unusual characters in its name.

    The name contains Unicode, spaces, dots, hyphens, and underscores to
    exercise input sanitisation / validation logic.
    """
    special_name = "tëst repo.special-chars_ünîcödé 日本語"
    return make_hosted_repo(name=special_name, format_type="raw")


def make_repo_with_invalid_type() -> Dict[str, Any]:
    """Return a repository config with an invalid ``type`` value.

    The ``type`` field is intentionally set to a value that does not
    belong to :data:`REPOSITORY_TYPES` so that validation / error-
    handling code can be exercised.
    """
    return make_repository_base(
        format_type="maven",
        repo_type="invalid_type_xyz",
    )


def make_circular_group_reference() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return two group repository configs that reference each other.

    This is an intentionally invalid configuration used to verify that
    the system correctly detects and rejects circular group membership.

    Returns
    -------
    Tuple[Dict[str, Any], Dict[str, Any]]
        Two group configs where ``group_a`` lists ``group_b`` as a member
        and vice-versa.
    """
    group_a = make_group_repo(
        name="circular-group-a",
        format_type="maven",
        member_names=["circular-group-b"],
    )
    group_b = make_group_repo(
        name="circular-group-b",
        format_type="maven",
        member_names=["circular-group-a"],
    )
    return group_a, group_b


# ============================================================================
# Phase 8 – factory-boy SQLAlchemy Model Factory
# ============================================================================

try:
    from src.models.repository import Repository as _RepositoryModel

    class RepositoryFactory(factory.alchemy.SQLAlchemyModelFactory):
        """SQLAlchemy-integrated factory for ``Repository`` ORM instances.

        The ``Meta.sqlalchemy_session`` attribute is ``None`` by default; it
        must be overridden by the consuming test fixture so that the factory
        writes into the correct database session::

            @pytest.fixture
            def repository_factory(db_session):
                RepositoryFactory._meta.sqlalchemy_session = db_session
                return RepositoryFactory
        """

        class Meta:
            model = _RepositoryModel
            sqlalchemy_session = None
            sqlalchemy_session_persistence = "commit"

        name = factory.Sequence(lambda n: f"factory-repo-{n}")
        format = factory.fuzzy.FuzzyChoice(REPOSITORY_FORMATS)
        type = factory.fuzzy.FuzzyChoice(REPOSITORY_TYPES)
        online = True
        description = factory.LazyAttribute(lambda _: fake.sentence(nb_words=6))

except ImportError:
    # The Repository model may not exist yet (greenfield project).
    # Provide a lightweight stand-in so that consumers can still import
    # this module without error.  Tests that require ORM instances should
    # skip when the model is unavailable.

    class RepositoryFactory:  # type: ignore[no-redef]
        """Placeholder ``RepositoryFactory`` — ORM model not yet available.

        This stub is loaded when ``src.models.repository.Repository`` cannot
        be imported.  It provides a working dict-based fallback (via
        ``make_repository_base``) so that tests relying on
        ``RepositoryFactory.create()`` or ``RepositoryFactory.build()``
        continue to function without a real ORM session.  Tests that
        require actual ORM instances should skip when the model is absent.
        """

        class Meta:
            model = None
            sqlalchemy_session = None
            sqlalchemy_session_persistence = "commit"

        name: Any = None
        format: Any = None
        type: Any = None
        online: bool = True
        description: Any = None

        def __init_subclass__(cls, **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)

        @classmethod
        def create(cls, **kwargs: Any) -> Dict[str, Any]:
            """Fallback: returns a plain dict via ``make_repository_base``."""
            return make_repository_base(**kwargs)

        @classmethod
        def build(cls, **kwargs: Any) -> Dict[str, Any]:
            """Fallback: returns a plain dict via ``make_repository_base``."""
            return make_repository_base(**kwargs)
