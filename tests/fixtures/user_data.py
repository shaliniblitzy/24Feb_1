"""
User and role test data factories for the Flask Binary Repository Management System.

Provides factory functions for generating user entities with various RBAC roles,
covering all 5 authentication methods (username/password, JWT bearer token, API key,
session-based, anonymous access) and the 3-tier RBAC model (global roles,
repository-specific permissions, content selectors).

All factory functions follow the ``make_*`` naming convention and accept ``**overrides``
for flexible customization. Uses factory-boy and Faker with seeded instances
for deterministic, reproducible test data generation.

Usage examples::

    from tests.fixtures.user_data import (
        make_admin_user, make_developer_user, make_user_set,
        make_jwt_token_data, make_api_key_data,
    )

    admin = make_admin_user()
    dev = make_developer_user(username='custom-dev')
    all_users = make_user_set()
    token = make_jwt_token_data(user_data=admin)
"""

from __future__ import annotations

import hashlib
import secrets
import datetime
import uuid
from typing import Dict, List, Optional, Any

import factory
import factory.alchemy
from faker import Faker

# ---------------------------------------------------------------------------
# Seeded Faker instance — ensures deterministic output across test runs
# ---------------------------------------------------------------------------
fake = Faker()
Faker.seed(67890)

# ---------------------------------------------------------------------------
# Role constants — global user role identifiers
# ---------------------------------------------------------------------------
ROLE_ADMIN: str = "admin"
ROLE_DEVELOPER: str = "developer"
ROLE_READONLY: str = "readonly"
ROLE_ANONYMOUS: str = "anonymous"

# ---------------------------------------------------------------------------
# Privilege constants — granular permission identifiers matching Nexus-style
# naming conventions used by the binary repository management system.
# ---------------------------------------------------------------------------
PRIV_ALL: str = "nx-all"
PRIV_REPO_READ: str = "nx-repository-view"
PRIV_REPO_WRITE: str = "nx-repository-edit"
PRIV_REPO_ADMIN: str = "nx-repository-admin"
PRIV_SEARCH: str = "nx-search-read"
PRIV_COMPONENT_UPLOAD: str = "nx-component-upload"
PRIV_API_KEY_ALL: str = "nx-apikey-all"

# Default test password — used as plaintext input for login and as the basis
# for pre-computed hashes stored alongside user data for direct DB seeding.
DEFAULT_PASSWORD: str = "TestPassword123!"

# Internal list of all defined privilege constants for max-privilege scenarios
_ALL_PRIVILEGES: List[str] = [
    PRIV_ALL,
    PRIV_REPO_READ,
    PRIV_REPO_WRITE,
    PRIV_REPO_ADMIN,
    PRIV_SEARCH,
    PRIV_COMPONENT_UPLOAD,
    PRIV_API_KEY_ALL,
]

# Internal list of assignable roles for random user generation
_ASSIGNABLE_ROLES: List[str] = [
    ROLE_ADMIN,
    ROLE_DEVELOPER,
    ROLE_READONLY,
]


# ---------------------------------------------------------------------------
# Helper: compute a SHA-256 hash of the default password for direct DB seeding
# ---------------------------------------------------------------------------
def _compute_password_hash(password: str = DEFAULT_PASSWORD) -> str:
    """Return a SHA-256 hex digest of *password* prefixed with ``sha256$``.

    This format is suitable for direct database seeding in tests, bypassing
    the application's hashing layer.  It is **not** intended for production
    use — real applications should use bcrypt / argon2 / werkzeug's
    ``generate_password_hash``.
    """
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return f"sha256${digest}"


# =========================================================================
# Phase 2 — Base User Factory
# =========================================================================


def make_user_base(
    username: Optional[str] = None,
    email: Optional[str] = None,
    role: str = ROLE_DEVELOPER,
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate a base user configuration dictionary with common fields.

    Parameters
    ----------
    username:
        Explicit username.  Defaults to a Faker-generated lowercase username.
    email:
        Explicit email address.  Defaults to a Faker-generated email.
    role:
        User role string (one of the ``ROLE_*`` constants).
    **overrides:
        Arbitrary key/value pairs merged into the returned dict, allowing
        callers to customise any field without modifying the factory.

    Returns
    -------
    Dict[str, Any]
        A dictionary representing the user entity suitable for test assertions
        or direct database seeding.
    """
    now = datetime.datetime.now(datetime.timezone.utc)

    user_data: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "username": (username or fake.user_name()).lower().replace(" ", "_"),
        "email": email or fake.email(),
        "first_name": fake.first_name(),
        "last_name": fake.last_name(),
        "role": role,
        "status": "active",
        "password": DEFAULT_PASSWORD,
        "password_hash": _compute_password_hash(DEFAULT_PASSWORD),
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "last_login": None,
    }

    # Merge caller-provided overrides (last-write-wins)
    user_data.update(overrides)
    return user_data


# =========================================================================
# Phase 3 — Role-Specific User Factories
# =========================================================================


def make_admin_user(
    username: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Create an admin user with full system privileges.

    The admin user receives the ``PRIV_ALL`` wildcard privilege and the
    ``nx-admin`` global role, granting unrestricted access to every resource
    in the binary repository management system.

    Parameters
    ----------
    username:
        Explicit username.  Defaults to ``admin-<random>``.
    **overrides:
        Additional fields merged into the returned dict.

    Returns
    -------
    Dict[str, Any]
        Complete admin user configuration dictionary.
    """
    effective_username = username or f"admin-{fake.word()}"

    admin_fields: Dict[str, Any] = {
        "privileges": [PRIV_ALL],
        "is_admin": True,
        "global_roles": ["nx-admin"],
    }
    admin_fields.update(overrides)

    return make_user_base(
        username=effective_username,
        role=ROLE_ADMIN,
        **admin_fields,
    )


def make_developer_user(
    username: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Create a developer user with read/write and upload privileges.

    Developers can browse repositories, upload components, and search —
    but cannot administer repositories or manage system configuration.

    Parameters
    ----------
    username:
        Explicit username.  Defaults to ``dev-<random>``.
    **overrides:
        Additional fields merged into the returned dict.

    Returns
    -------
    Dict[str, Any]
        Complete developer user configuration dictionary.
    """
    effective_username = username or f"dev-{fake.word()}"

    dev_fields: Dict[str, Any] = {
        "privileges": [
            PRIV_REPO_READ,
            PRIV_REPO_WRITE,
            PRIV_SEARCH,
            PRIV_COMPONENT_UPLOAD,
        ],
        "is_admin": False,
        "global_roles": ["nx-developer"],
    }
    dev_fields.update(overrides)

    return make_user_base(
        username=effective_username,
        role=ROLE_DEVELOPER,
        **dev_fields,
    )


def make_readonly_user(
    username: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Create a read-only user with browse and search privileges only.

    Read-only users can view repository contents and execute searches but
    cannot upload, modify, or delete any data.

    Parameters
    ----------
    username:
        Explicit username.  Defaults to ``readonly-<random>``.
    **overrides:
        Additional fields merged into the returned dict.

    Returns
    -------
    Dict[str, Any]
        Complete read-only user configuration dictionary.
    """
    effective_username = username or f"readonly-{fake.word()}"

    ro_fields: Dict[str, Any] = {
        "privileges": [PRIV_REPO_READ, PRIV_SEARCH],
        "is_admin": False,
        "global_roles": ["nx-readonly"],
    }
    ro_fields.update(overrides)

    return make_user_base(
        username=effective_username,
        role=ROLE_READONLY,
        **ro_fields,
    )


def make_anonymous_user() -> Dict[str, Any]:
    """Create a minimal anonymous/unauthenticated user representation.

    Anonymous users are not logged in — they have no credentials, no email,
    and only the basic ``PRIV_REPO_READ`` privilege to allow browsing of
    public repositories.

    Returns
    -------
    Dict[str, Any]
        Minimal anonymous user configuration dictionary.
    """
    return {
        "id": str(uuid.uuid4()),
        "username": "anonymous",
        "email": None,
        "first_name": None,
        "last_name": None,
        "role": ROLE_ANONYMOUS,
        "status": "active",
        "password": None,
        "password_hash": None,
        "privileges": [PRIV_REPO_READ],
        "is_admin": False,
        "is_authenticated": False,
        "global_roles": [],
        "created_at": None,
        "updated_at": None,
        "last_login": None,
    }


# =========================================================================
# Phase 4 — Authentication Data Factories
# =========================================================================


def make_jwt_token_data(
    user_data: Optional[Dict[str, Any]] = None,
    expired: bool = False,
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate JWT token claim data for authentication testing.

    This produces the *claim payload* that would be embedded in a signed JWT.
    It does **not** produce the signed token itself — signing is done by
    the application or by conftest.py fixtures.

    Parameters
    ----------
    user_data:
        A user dictionary (e.g. from ``make_admin_user()``).  If ``None``,
        a new developer user is generated automatically.
    expired:
        When ``True`` the ``expires_delta`` is set to a negative timedelta,
        simulating an expired token for rejection tests.
    **overrides:
        Additional fields merged into the returned dict.

    Returns
    -------
    Dict[str, Any]
        Dictionary containing JWT claim fields.
    """
    if user_data is None:
        user_data = make_developer_user()

    if expired:
        expires_delta = datetime.timedelta(seconds=-1)
    else:
        expires_delta = datetime.timedelta(hours=1)

    token_data: Dict[str, Any] = {
        "identity": user_data.get("id", str(uuid.uuid4())),
        "claims": {
            "role": user_data.get("role", ROLE_DEVELOPER),
            "privileges": user_data.get("privileges", []),
            "username": user_data.get("username", "unknown"),
        },
        "expires_delta": expires_delta,
        "token_type": "access",
        "fresh": True,
    }

    token_data.update(overrides)
    return token_data


def make_api_key_data(
    user_id: Optional[str] = None,
    name: Optional[str] = None,
    expired: bool = False,
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate API key test data for key-based authentication testing.

    Parameters
    ----------
    user_id:
        Owner user ID.  Defaults to a new UUID.
    name:
        Human-readable key name.  Defaults to a Faker-generated label.
    expired:
        When ``True`` the ``expires_at`` timestamp is set in the past and
        ``is_active`` is ``False``.
    **overrides:
        Additional fields merged into the returned dict.

    Returns
    -------
    Dict[str, Any]
        Dictionary containing API key fields.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    key_value = secrets.token_hex(32)  # 64-char hex API key

    if expired:
        expires_at = (now - datetime.timedelta(days=1)).isoformat()
        is_active = False
    else:
        expires_at = (now + datetime.timedelta(days=90)).isoformat()
        is_active = True

    api_key: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "key": key_value,
        "key_prefix": key_value[:8],
        "name": name or f"api-key-{fake.word()}",
        "user_id": user_id or str(uuid.uuid4()),
        "created_at": now.isoformat(),
        "expires_at": expires_at,
        "is_active": is_active,
        "last_used": None,
    }

    api_key.update(overrides)
    return api_key


def make_session_data(
    user_data: Optional[Dict[str, Any]] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate session test data for session-based authentication testing.

    Parameters
    ----------
    user_data:
        A user dictionary.  If ``None``, a new developer user is generated.
    **overrides:
        Additional fields merged into the returned dict.

    Returns
    -------
    Dict[str, Any]
        Dictionary containing session fields.
    """
    if user_data is None:
        user_data = make_developer_user()

    now = datetime.datetime.now(datetime.timezone.utc)

    session: Dict[str, Any] = {
        "session_id": secrets.token_hex(16),
        "user_id": user_data.get("id", str(uuid.uuid4())),
        "username": user_data.get("username", "unknown"),
        "created_at": now.isoformat(),
        "expires_at": (now + datetime.timedelta(hours=1)).isoformat(),
        "ip_address": fake.ipv4(),
        "user_agent": fake.user_agent(),
        "is_active": True,
    }

    session.update(overrides)
    return session


def make_login_credentials(
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, str]:
    """Generate login credential payload for authentication endpoint tests.

    Parameters
    ----------
    username:
        Explicit username.  Defaults to a Faker-generated value.
    password:
        Explicit password.  Defaults to ``DEFAULT_PASSWORD``.

    Returns
    -------
    Dict[str, str]
        Simple ``{"username": ..., "password": ...}`` dictionary.
    """
    return {
        "username": username or fake.user_name(),
        "password": password or DEFAULT_PASSWORD,
    }


def make_invalid_credentials() -> Dict[str, str]:
    """Generate intentionally wrong credentials for error-path testing.

    Returns
    -------
    Dict[str, str]
        Credentials guaranteed to not match any seeded test user.
    """
    return {
        "username": "nonexistent-user",
        "password": "wrong-password",
    }


# =========================================================================
# Phase 5 — RBAC and Permission Data Factories
# =========================================================================


def make_role_data(
    role_id: Optional[str] = None,
    name: Optional[str] = None,
    privileges: Optional[List[str]] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate a role definition dictionary for RBAC testing.

    Parameters
    ----------
    role_id:
        Explicit role ID.  Defaults to a new UUID.
    name:
        Role name (e.g. ``nx-admin``).  Defaults to ``role-<word>``.
    privileges:
        List of privilege strings assigned to this role.
    **overrides:
        Additional fields merged into the returned dict.

    Returns
    -------
    Dict[str, Any]
        Role definition dictionary.
    """
    role: Dict[str, Any] = {
        "id": role_id or str(uuid.uuid4()),
        "name": name or f"role-{fake.word()}",
        "description": fake.sentence(),
        "privileges": privileges if privileges is not None else [PRIV_REPO_READ],
        "source": "default",
    }

    role.update(overrides)
    return role


def make_privilege_data(
    privilege_id: Optional[str] = None,
    name: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate a privilege definition dictionary for permission testing.

    Parameters
    ----------
    privilege_id:
        Explicit privilege ID.  Defaults to a new UUID.
    name:
        Privilege name (e.g. ``nx-repository-view``).
    **overrides:
        Additional fields merged into the returned dict.

    Returns
    -------
    Dict[str, Any]
        Privilege definition dictionary.
    """
    priv: Dict[str, Any] = {
        "id": privilege_id or str(uuid.uuid4()),
        "name": name or f"nx-{fake.word()}-read",
        "description": fake.sentence(),
        "actions": ["READ", "BROWSE"],
        "domain": fake.random_element(["repository", "component", "system"]),
    }

    priv.update(overrides)
    return priv


def make_content_selector_data(
    name: Optional[str] = None,
    expression: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate a content selector definition for fine-grained access control.

    Content selectors form the third tier of the RBAC model, providing
    path-level or format-level filtering within repository permissions.

    Parameters
    ----------
    name:
        Selector name.  Defaults to ``selector-<word>``.
    expression:
        CSEL expression.  Defaults to a Maven path filter.
    **overrides:
        Additional fields merged into the returned dict.

    Returns
    -------
    Dict[str, Any]
        Content selector definition dictionary.
    """
    selector: Dict[str, Any] = {
        "name": name or f"selector-{fake.word()}",
        "description": fake.sentence(),
        "expression": expression or 'format == "maven2" and path =^ "/com/example"',
        "type": "csel",
    }

    selector.update(overrides)
    return selector


def make_repo_permission_data(
    repo_name: Optional[str] = None,
    role_name: Optional[str] = None,
    privileges: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Generate repository-specific permission assignment data.

    This represents the second tier of the RBAC model — permissions scoped
    to a specific repository rather than applied globally.

    Parameters
    ----------
    repo_name:
        Target repository name.  Defaults to ``repo-<word>``.
    role_name:
        Role to assign within this repository scope.
    privileges:
        Specific privileges for this repository.

    Returns
    -------
    Dict[str, Any]
        Repository permission assignment dictionary.
    """
    return {
        "repository_name": repo_name or f"repo-{fake.word()}",
        "role_name": role_name or "nx-developer",
        "privileges": privileges if privileges is not None else [
            PRIV_REPO_READ,
            PRIV_REPO_WRITE,
        ],
        "content_selectors": [],
    }


# =========================================================================
# Phase 6 — User Collection Generators
# =========================================================================


def make_user_set() -> Dict[str, Dict[str, Any]]:
    """Generate a complete set of users — one per role.

    Convenient for tests that need to assert different behaviour per role
    in a single parametrised or sequential test.

    Returns
    -------
    Dict[str, Dict[str, Any]]
        Keys are role labels: ``admin``, ``developer``, ``readonly``,
        ``anonymous``.
    """
    return {
        "admin": make_admin_user(),
        "developer": make_developer_user(),
        "readonly": make_readonly_user(),
        "anonymous": make_anonymous_user(),
    }


def make_users_with_roles(count: int = 5) -> List[Dict[str, Any]]:
    """Generate *count* users with randomly assigned roles.

    Useful for bulk-user testing (e.g. pagination, listing endpoints).

    Parameters
    ----------
    count:
        Number of users to generate.  Defaults to 5.

    Returns
    -------
    List[Dict[str, Any]]
        List of user configuration dictionaries.
    """
    role_factory_map = {
        ROLE_ADMIN: make_admin_user,
        ROLE_DEVELOPER: make_developer_user,
        ROLE_READONLY: make_readonly_user,
    }

    users: List[Dict[str, Any]] = []
    for _ in range(count):
        role = fake.random_element(_ASSIGNABLE_ROLES)
        user = role_factory_map[role]()
        users.append(user)

    return users


# =========================================================================
# Phase 7 — Edge Case User Data
# =========================================================================


def make_user_with_expired_token() -> Dict[str, Any]:
    """Generate a user paired with an expired JWT token.

    Returns
    -------
    Dict[str, Any]
        Dictionary with ``user`` and ``token`` keys.  The ``token`` data
        has its ``expires_delta`` set to a negative value for expiry testing.
    """
    user = make_developer_user()
    token = make_jwt_token_data(user_data=user, expired=True)
    return {
        "user": user,
        "token": token,
    }


def make_user_with_revoked_api_key() -> Dict[str, Any]:
    """Generate a user with an API key marked as revoked / inactive.

    Returns
    -------
    Dict[str, Any]
        Dictionary with ``user`` and ``api_key`` keys.  The API key has
        ``is_active`` set to ``False``.
    """
    user = make_developer_user()
    api_key = make_api_key_data(
        user_id=user["id"],
        name="revoked-key",
        expired=False,
        is_active=False,
    )
    return {
        "user": user,
        "api_key": api_key,
    }


def make_user_with_multiple_sessions(
    session_count: int = 3,
) -> Dict[str, Any]:
    """Generate a user with multiple concurrent sessions.

    Useful for testing concurrent session limits, session listing, and
    session invalidation behaviour.

    Parameters
    ----------
    session_count:
        Number of concurrent sessions to generate.  Defaults to 3.

    Returns
    -------
    Dict[str, Any]
        Dictionary with ``user`` and ``sessions`` keys.
    """
    user = make_developer_user()
    sessions = [make_session_data(user_data=user) for _ in range(session_count)]
    return {
        "user": user,
        "sessions": sessions,
    }


def make_user_with_max_privileges() -> Dict[str, Any]:
    """Generate a user with every defined privilege assigned.

    Useful for testing privilege accumulation and the upper boundary of
    the authorisation matrix.

    Returns
    -------
    Dict[str, Any]
        Admin-level user with the complete privilege set.
    """
    return make_admin_user(
        privileges=list(_ALL_PRIVILEGES),
        global_roles=["nx-admin", "nx-developer", "nx-readonly"],
    )


def make_user_with_no_privileges() -> Dict[str, Any]:
    """Generate a user with an empty privilege list.

    Useful for testing minimum-privilege / zero-access scenarios where
    every authorised action should be denied.

    Returns
    -------
    Dict[str, Any]
        User configuration with ``privileges`` set to an empty list.
    """
    return make_developer_user(
        privileges=[],
        global_roles=[],
    )


# =========================================================================
# Phase 8 — factory-boy Model Factory (Optional SQLAlchemy Integration)
# =========================================================================

try:
    from src.models.user import User as _UserModel  # type: ignore[import-untyped]

    class UserFactory(factory.alchemy.SQLAlchemyModelFactory):
        """ORM-integrated user factory for database-backed tests.

        The ``Meta.sqlalchemy_session`` is intentionally set to ``None`` and
        must be overridden by the test fixture (e.g. via
        ``UserFactory._meta.sqlalchemy_session = db_session``).
        """

        class Meta:
            model = _UserModel
            sqlalchemy_session = None
            sqlalchemy_session_persistence = "commit"

        username = factory.Sequence(lambda n: f"user-{n}")
        email = factory.LazyAttribute(lambda obj: f"{obj.username}@test.example.com")
        role = factory.Faker("random_element", elements=[ROLE_ADMIN, ROLE_DEVELOPER, ROLE_READONLY])
        status = "active"
        password_hash = factory.LazyAttribute(
            lambda _obj: _compute_password_hash(DEFAULT_PASSWORD)
        )

except ImportError:
    # The source models package may not exist yet in a greenfield project.
    # Provide a placeholder class so that ``from tests.fixtures.user_data
    # import UserFactory`` does not raise an ImportError at import time.
    class UserFactory:  # type: ignore[no-redef]
        """Placeholder UserFactory — the ``src.models.user.User`` model is not
        available.  This stub allows the fixture module to be imported without
        error.  Tests relying on ``UserFactory`` with a real ORM session
        should skip when the model is absent.
        """

        class Meta:
            model = None
            sqlalchemy_session = None

        username = None
        email = None
        role = ROLE_DEVELOPER
        status = "active"
        password_hash = None

        def __init_subclass__(cls, **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)

        @classmethod
        def create(cls, **kwargs: Any) -> Dict[str, Any]:
            """Fallback: returns a plain dict via ``make_user_base``."""
            return make_user_base(**kwargs)

        @classmethod
        def build(cls, **kwargs: Any) -> Dict[str, Any]:
            """Fallback: returns a plain dict via ``make_user_base``."""
            return make_user_base(**kwargs)

