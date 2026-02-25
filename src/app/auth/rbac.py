"""
RBAC (Role-Based Access Control) Enforcement Logic.

This module implements the low-level privilege evaluation engine for the Nexus
Repository Manager's three-tier authorization model (Feature F-301).  It
replaces the privilege descriptor matching system from Apache Shiro 2.0.0
in the original Java source.

**Three-Tier Authorization Model:**

    ┌────────────────────────────────────────────────────────────┐
    │ Tier 1 — System-wide:                                     │
    │   • 'application' type  (e.g., user management, settings) │
    │   • 'wildcard' type     (e.g., nx-all — super-admin)      │
    ├────────────────────────────────────────────────────────────┤
    │ Tier 2 — Repository-scoped:                               │
    │   • 'repository-admin' type  (repo admin operations)      │
    │   • 'repository-view' type   (read access to content)     │
    ├────────────────────────────────────────────────────────────┤
    │ Tier 3 — Sub-repository:                                  │
    │   • 'repository-content-selector' type (CSEL expressions) │
    └────────────────────────────────────────────────────────────┘

**Architecture Notes:**

- This module is a **pure logic engine**: it does NOT interact with the Flask
  request context.  The ``authorization.py`` module wraps this engine with
  Flask-specific hooks (``before_request``, decorators, etc.).
- Role-to-privilege resolution traverses the User → Role → Privilege chain
  using SQLAlchemy relationships and query operations.
- Privilege matching supports wildcard (``*``) patterns for format and
  repository fields, with case-insensitive comparison.
- The action hierarchy (``ACTION_HIERARCHY``) ensures that higher-level
  actions implicitly grant lower-level ones (e.g., ``admin`` implies all).

**Privilege Types:**

- ``'wildcard'``                    — unrestricted access (e.g., ``nx-all``)
- ``'application'``                 — system-wide admin privileges
- ``'repository-admin'``            — repository administration
- ``'repository-view'``             — repository content access
- ``'repository-content-selector'`` — sub-repository access via CSEL

**Cross-Module Usage:**

- Consumed by ``src.app.auth.authorization`` for Flask integration.
- Uses models from ``src.app.models.privilege``, ``src.app.models.role``,
  ``src.app.models.user``, and ``src.app.models.content_selector``.
- Uses ``db.session`` from ``src.app.extensions`` for query operations.

Exports:
    RBACEnforcer    : Core RBAC evaluation engine class.
    ACTION_HIERARCHY : Mapping of action names to their implied action sets.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from typing import List, Optional, Set, Tuple

from src.app.extensions import db
from src.app.models.content_selector import ContentSelector
from src.app.models.privilege import Privilege
from src.app.models.role import Role
from src.app.models.user import User

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# Provides structured logging for privilege resolution, permission check
# results, and authorization decisions.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "RBACEnforcer",
    "ACTION_HIERARCHY",
]

# ---------------------------------------------------------------------------
# Action Hierarchy Constant
# ---------------------------------------------------------------------------
# Defines the implication graph for actions.  A higher-level action
# implicitly grants all actions in its set.  For example, possessing the
# ``admin`` action implies ``browse``, ``read``, ``edit``, ``delete``,
# ``add``, and ``admin``.  The ``read`` action implies ``browse``.
#
# This hierarchy mirrors the Shiro 2.0.0 WildcardPermission action
# resolution from the Java source.
# ---------------------------------------------------------------------------

ACTION_HIERARCHY: dict[str, set[str]] = {
    "admin": {"browse", "read", "edit", "delete", "add", "admin"},
    "edit": {"browse", "read", "edit"},
    "delete": {"delete"},
    "add": {"add"},
    "read": {"browse", "read"},
    "browse": {"browse"},
}
"""Mapping of action names to the sets of actions they implicitly grant.

Each key is an action level, and its value is the set of actions that are
considered *granted* when the key action is possessed.  This enables
hierarchical permission checks — e.g., a privilege granting ``'admin'``
automatically satisfies checks for ``'read'``, ``'browse'``, ``'edit'``,
``'delete'``, and ``'add'``.

Members exposed:
    admin  : Grants all actions (browse, read, edit, delete, add, admin).
    edit   : Grants browse, read, and edit.
    delete : Grants only delete.
    add    : Grants only add.
    read   : Grants browse and read.
    browse : Grants only browse.
"""


# ===========================================================================
# Private Helper Functions
# ===========================================================================


def _matches_pattern(pattern: str, value: str) -> bool:
    """Determine if *value* matches the wildcard *pattern*.

    Implements the privilege property matching logic used throughout the RBAC
    system.  Supports:

    - ``'*'`` — matches any value unconditionally.
    - Exact match — case-insensitive string comparison.
    - Glob patterns — delegated to :func:`fnmatch.fnmatch` for patterns
      containing ``*``, ``?``, ``[``, ``]`` characters.

    Args:
        pattern: The pattern from the privilege properties (e.g., ``'maven2'``,
            ``'*'``, ``'docker-*'``).
        value: The actual value to test against the pattern (e.g., a
            repository name or format name).

    Returns:
        ``True`` if *value* matches *pattern*; ``False`` otherwise.
    """
    # Wildcard '*' matches everything.
    if pattern == "*":
        return True

    # Fast path: exact case-insensitive match.
    if pattern.lower() == value.lower():
        return True

    # Glob pattern matching (e.g., 'docker-*', 'maven?') using fnmatch.
    # fnmatch is case-insensitive on Windows but case-sensitive on Unix;
    # normalise both to lowercase for consistent cross-platform behaviour.
    if any(ch in pattern for ch in ("*", "?", "[", "]")):
        return fnmatch.fnmatch(value.lower(), pattern.lower())

    return False


def _action_permitted(allowed_actions: List[str], requested_action: str) -> bool:
    """Check if *requested_action* is permitted by the *allowed_actions* list.

    Uses the ``ACTION_HIERARCHY`` to expand each allowed action into its set
    of implied actions.  The check succeeds if *requested_action* is in the
    union of all implied action sets.

    Args:
        allowed_actions: List of action strings from a privilege's properties
            (e.g., ``['read', 'browse']``).  A ``'*'`` entry grants all
            actions unconditionally.
        requested_action: The specific action being requested (e.g.,
            ``'read'``, ``'delete'``).

    Returns:
        ``True`` if the requested action is permitted; ``False`` otherwise.
    """
    # Empty actions list means no action restriction (typically for
    # repository-admin privileges that grant all actions implicitly).
    if not allowed_actions:
        return True

    # Wildcard '*' in the actions list grants every action.
    if "*" in allowed_actions:
        return True

    # Normalise the requested action to lowercase for comparison.
    requested_lower: str = requested_action.lower()

    # Build the effective set of granted actions by expanding each allowed
    # action through the hierarchy.
    effective_actions: set[str] = set()
    for action in allowed_actions:
        action_lower: str = action.lower()
        # Look up the hierarchy for implied actions; if the action is not
        # in the hierarchy, include it as-is (direct match).
        implied: set[str] = ACTION_HIERARCHY.get(action_lower, {action_lower})
        effective_actions.update(implied)

    return requested_lower in effective_actions


# Compiled regex for validating privilege type strings (used in debug logging).
_PRIVILEGE_TYPE_RE: re.Pattern[str] = re.compile(
    r"^(application|wildcard|repository-admin|repository-view"
    r"|repository-content-selector)$"
)


# ===========================================================================
# RBACEnforcer Class
# ===========================================================================


class RBACEnforcer:
    """Core RBAC evaluation engine for the Nexus Repository Manager.

    This class encapsulates the complete role-privilege resolution and
    permission-checking pipeline.  It operates as a stateless service
    (except for the logger) and may be instantiated once and reused across
    the application lifecycle.

    **Usage in authorization.py:**

    .. code-block:: python

        enforcer = RBACEnforcer()

        # Tier 1 — system-wide check
        if enforcer.check_system_permission(privileges, 'users', 'create'):
            allow_access()

        # Tier 2 — repository-scoped check
        if enforcer.check_repository_permission(
            privileges, 'my-maven-repo', 'maven2', 'read'
        ):
            allow_access()

        # Tier 3 — sub-repository CSEL check
        selectors = enforcer.check_content_selector_permission(
            privileges, 'my-npm-repo', 'npm', 'read'
        )
        for name, expression in selectors:
            if evaluate_csel(expression, asset_path):
                allow_access()

    **Thread Safety:**
    This class is safe for concurrent use from multiple WSGI worker threads
    because it maintains no mutable state.  All data is retrieved fresh from
    the database for each authorization evaluation.
    """

    def __init__(self) -> None:
        """Initialise the RBAC enforcer.

        Sets up the module-level logger for diagnostic and audit logging of
        privilege resolution and permission check results.
        """
        self.logger: logging.Logger = logging.getLogger(__name__)
        self.logger.debug("RBACEnforcer initialised.")

    # -------------------------------------------------------------------
    # Phase 3: Role-Privilege Resolution
    # -------------------------------------------------------------------

    def get_user_roles(self, user: User) -> List[Role]:
        """Retrieve all roles assigned to a user.

        Traverses the User → Role many-to-many relationship (via the
        ``role_assignments`` junction table) to collect all roles, including
        both ``'internal'`` (locally-created) and ``'external'``
        (LDAP/SSO-mapped) roles.

        Args:
            user: The :class:`User` instance whose roles are to be resolved.

        Returns:
            A list of :class:`Role` objects assigned to the user.  Returns
            an empty list if the user has no role assignments or if the
            relationship cannot be loaded.
        """
        if user is None:
            self.logger.warning("get_user_roles called with None user.")
            return []

        try:
            # User.roles is a dynamic relationship (lazy='dynamic'),
            # so we call .all() to materialise the query.
            roles: List[Role] = list(user.roles.all())
            self.logger.debug(
                "Resolved %d role(s) for user '%s': [%s]",
                len(roles),
                user.user_id,
                ", ".join(r.role_id for r in roles),
            )
            return roles
        except Exception as exc:
            self.logger.error(
                "Failed to resolve roles for user '%s': %s",
                user.user_id,
                str(exc),
                exc_info=True,
            )
            return []

    def get_role_privilege_ids(self, roles: List[Role]) -> Set[str]:
        """Extract all privilege IDs from a collection of roles.

        Iterates over each role's ``privilege_list`` property (which
        deserialises the ``privileges`` JSON column into a Python list of
        strings) and aggregates all privilege IDs into a deduplicated set.

        Args:
            roles: A list of :class:`Role` objects.

        Returns:
            A set of unique privilege ID strings extracted from all roles.
            Returns an empty set if *roles* is empty or all roles have
            empty privilege lists.
        """
        if not roles:
            return set()

        privilege_ids: Set[str] = set()
        for role in roles:
            try:
                role_privs: List[str] = role.privilege_list
                privilege_ids.update(role_privs)
                self.logger.debug(
                    "Role '%s' (source='%s') contributes %d privilege(s): [%s]",
                    role.role_id,
                    role.source,
                    len(role_privs),
                    ", ".join(role_privs),
                )
            except Exception as exc:
                self.logger.error(
                    "Error extracting privileges from role '%s': %s",
                    role.role_id,
                    str(exc),
                    exc_info=True,
                )

        self.logger.debug(
            "Aggregated %d unique privilege ID(s) from %d role(s).",
            len(privilege_ids),
            len(roles),
        )
        return privilege_ids

    def resolve_privileges(self, privilege_ids: Set[str]) -> List[Privilege]:
        """Load full Privilege objects from the database by their IDs.

        Performs a batch query using ``Privilege.query.filter(in_(...))`` to
        efficiently load all requested privileges in a single round-trip.

        Args:
            privilege_ids: A set of privilege ID strings to resolve.

        Returns:
            A list of :class:`Privilege` objects.  Privilege IDs that do not
            exist in the database are silently skipped (logged at DEBUG level
            for diagnostics).
        """
        if not privilege_ids:
            return []

        try:
            privileges: List[Privilege] = Privilege.query.filter(
                Privilege.privilege_id.in_(privilege_ids)
            ).all()

            # Log any IDs that were requested but not found in the DB.
            resolved_ids: set[str] = {p.privilege_id for p in privileges}
            missing_ids: set[str] = privilege_ids - resolved_ids
            if missing_ids:
                self.logger.debug(
                    "Privilege IDs not found in database: [%s]",
                    ", ".join(sorted(missing_ids)),
                )

            self.logger.debug(
                "Resolved %d privilege(s) from %d requested ID(s).",
                len(privileges),
                len(privilege_ids),
            )
            return privileges
        except Exception as exc:
            self.logger.error(
                "Failed to resolve privileges from database: %s",
                str(exc),
                exc_info=True,
            )
            return []

    # -------------------------------------------------------------------
    # Phase 4: System-Level Permission Checking (Tier 1)
    # -------------------------------------------------------------------

    def check_system_permission(
        self,
        privileges: List[Privilege],
        domain: str,
        action: str,
    ) -> bool:
        """Evaluate Tier 1 (system-wide) permission.

        Checks ``'wildcard'`` and ``'application'`` type privileges to
        determine if the supplied set of privileges grants the requested
        *action* on the specified *domain*.

        **Privilege types evaluated:**

        - ``'wildcard'`` (e.g., ``nx-all``): Grants all access
          unconditionally.  If any wildcard privilege is present, this
          method immediately returns ``True``.
        - ``'application'``: Matches against the ``domain`` and ``actions``
          fields in the privilege's ``properties`` JSON.  Example
          properties: ``{"domain": "users", "actions": ["create", "read"]}``.

        Args:
            privileges: The full list of :class:`Privilege` objects resolved
                for the current user.
            domain: The system domain being accessed (e.g., ``'users'``,
                ``'settings'``, ``'repositories'``).
            action: The action being performed (e.g., ``'create'``,
                ``'read'``, ``'update'``, ``'delete'``).

        Returns:
            ``True`` if the user's privileges grant the requested
            system-level permission; ``False`` otherwise.
        """
        if not privileges:
            return False

        for priv in privileges:
            # Tier 1 — Wildcard: grants everything unconditionally.
            if priv.is_wildcard:
                self.logger.debug(
                    "System permission GRANTED via wildcard privilege '%s' "
                    "for domain='%s', action='%s'.",
                    priv.privilege_id,
                    domain,
                    action,
                )
                return True

            # Tier 1 — Application: check domain and action match.
            if priv.type == "application":
                props: dict = priv.properties or {}
                priv_domain: str = props.get("domain", "")
                priv_actions: list = props.get("actions", [])

                if _matches_pattern(priv_domain, domain) and _action_permitted(
                    priv_actions, action
                ):
                    self.logger.debug(
                        "System permission GRANTED via application privilege "
                        "'%s' for domain='%s', action='%s'.",
                        priv.privilege_id,
                        domain,
                        action,
                    )
                    return True

        self.logger.debug(
            "System permission DENIED for domain='%s', action='%s' "
            "(%d privilege(s) evaluated).",
            domain,
            action,
            len(privileges),
        )
        return False

    # -------------------------------------------------------------------
    # Phase 5: Repository-Level Permission Checking (Tier 2)
    # -------------------------------------------------------------------

    def check_repository_permission(
        self,
        privileges: List[Privilege],
        repository_name: str,
        format_name: str,
        action: str,
    ) -> bool:
        """Evaluate Tier 2 (repository-scoped) permission.

        Checks ``'wildcard'``, ``'repository-admin'``, and
        ``'repository-view'`` type privileges against the supplied
        (repository, format, action) tuple.

        **Privilege types evaluated:**

        - ``'wildcard'``: Grants all access unconditionally.
        - ``'repository-admin'``: Matches on ``format`` and ``repository``
          fields.  Admin privileges grant all actions implicitly (no action
          restriction).  Properties example:
          ``{"format": "maven2", "repository": "*"}``.
        - ``'repository-view'``: Matches on ``format``, ``repository``,
          and ``actions``.  Properties example:
          ``{"format": "maven2", "repository": "*", "actions": ["read", "browse"]}``.

        Args:
            privileges: The full list of :class:`Privilege` objects resolved
                for the current user.
            repository_name: The repository being accessed (e.g.,
                ``'maven-central'``).
            format_name: The repository format (e.g., ``'maven2'``,
                ``'npm'``, ``'docker'``).
            action: The action being performed (e.g., ``'read'``,
                ``'browse'``, ``'edit'``, ``'delete'``).

        Returns:
            ``True`` if the user's privileges grant the requested
            repository-level permission; ``False`` otherwise.
        """
        if not privileges:
            return False

        for priv in privileges:
            # Wildcard privilege grants everything.
            if priv.is_wildcard:
                self.logger.debug(
                    "Repository permission GRANTED via wildcard privilege "
                    "'%s' for repo='%s', format='%s', action='%s'.",
                    priv.privilege_id,
                    repository_name,
                    format_name,
                    action,
                )
                return True

            # Repository-admin: matches format + repository, grants all actions.
            if priv.type == "repository-admin":
                props: dict = priv.properties or {}
                priv_format: str = props.get("format", "")
                priv_repo: str = props.get("repository", "")

                if _matches_pattern(priv_format, format_name) and _matches_pattern(
                    priv_repo, repository_name
                ):
                    self.logger.debug(
                        "Repository permission GRANTED via repository-admin "
                        "privilege '%s' for repo='%s', format='%s', "
                        "action='%s'.",
                        priv.privilege_id,
                        repository_name,
                        format_name,
                        action,
                    )
                    return True

            # Repository-view: matches format + repository + action.
            if priv.type == "repository-view":
                props = priv.properties or {}
                priv_format = props.get("format", "")
                priv_repo = props.get("repository", "")
                priv_actions: list = props.get("actions", [])

                if (
                    _matches_pattern(priv_format, format_name)
                    and _matches_pattern(priv_repo, repository_name)
                    and _action_permitted(priv_actions, action)
                ):
                    self.logger.debug(
                        "Repository permission GRANTED via repository-view "
                        "privilege '%s' for repo='%s', format='%s', "
                        "action='%s'.",
                        priv.privilege_id,
                        repository_name,
                        format_name,
                        action,
                    )
                    return True

        self.logger.debug(
            "Repository permission DENIED for repo='%s', format='%s', "
            "action='%s' (%d privilege(s) evaluated).",
            repository_name,
            format_name,
            action,
            len(privileges),
        )
        return False

    # -------------------------------------------------------------------
    # Phase 6: Content Selector Permission Checking (Tier 3)
    # -------------------------------------------------------------------

    def check_content_selector_permission(
        self,
        privileges: List[Privilege],
        repository_name: str,
        format_name: str,
        action: str,
    ) -> List[Tuple[str, str]]:
        """Evaluate Tier 3 (sub-repository) content selector privileges.

        Finds all ``'repository-content-selector'`` type privileges that
        match the supplied (repository, format, action) tuple, resolves the
        referenced content selector expressions from the database, and
        returns them for downstream CSEL evaluation.

        **This method does NOT evaluate CSEL expressions** — it only resolves
        which selectors should be evaluated.  The actual expression evaluation
        is handled by ``src.app.auth.content_selector``.

        **Privilege properties example:**

        .. code-block:: json

            {
                "format": "npm",
                "repository": "*",
                "contentSelector": "my-selector",
                "actions": ["read"]
            }

        Args:
            privileges: The full list of :class:`Privilege` objects resolved
                for the current user.
            repository_name: The repository being accessed.
            format_name: The repository format.
            action: The action being performed.

        Returns:
            A list of ``(content_selector_name, expression)`` tuples for
            privileges that match the repository/format/action criteria.
            Returns an empty list if no matching content selector privileges
            are found or if the referenced selectors do not exist in the
            database.
        """
        if not privileges:
            return []

        matching_selector_names: list[str] = []

        for priv in privileges:
            if priv.type != "repository-content-selector":
                continue

            props: dict = priv.properties or {}
            priv_format: str = props.get("format", "")
            priv_repo: str = props.get("repository", "")
            priv_actions: list = props.get("actions", [])
            selector_name: str = props.get("contentSelector", "")

            # Validate that a content selector name is actually specified.
            if not selector_name:
                self.logger.warning(
                    "Content-selector privilege '%s' is missing "
                    "'contentSelector' in properties; skipping.",
                    priv.privilege_id,
                )
                continue

            # Match format, repository, and action.
            if (
                _matches_pattern(priv_format, format_name)
                and _matches_pattern(priv_repo, repository_name)
                and _action_permitted(priv_actions, action)
            ):
                matching_selector_names.append(selector_name)
                self.logger.debug(
                    "Content-selector privilege '%s' matches "
                    "repo='%s', format='%s', action='%s' → "
                    "selector='%s'.",
                    priv.privilege_id,
                    repository_name,
                    format_name,
                    action,
                    selector_name,
                )

        if not matching_selector_names:
            self.logger.debug(
                "No content-selector privileges matched for repo='%s', "
                "format='%s', action='%s'.",
                repository_name,
                format_name,
                action,
            )
            return []

        # Resolve content selector expressions from the database.
        result: List[Tuple[str, str]] = []
        try:
            # Batch-query all matching selectors by name.
            selectors: list[ContentSelector] = ContentSelector.query.filter(
                ContentSelector.name.in_(matching_selector_names)
            ).all()

            # Build a lookup map for efficient resolution.
            selector_map: dict[str, ContentSelector] = {
                cs.name: cs for cs in selectors
            }

            for name in matching_selector_names:
                cs: ContentSelector | None = selector_map.get(name)
                if cs is not None:
                    result.append((cs.name, cs.expression))
                    self.logger.debug(
                        "Resolved content selector '%s' (id='%s') → "
                        "expression='%s'.",
                        cs.name,
                        cs.selector_id,
                        cs.expression,
                    )
                else:
                    self.logger.warning(
                        "Content selector '%s' referenced by privilege but "
                        "not found in the database.",
                        name,
                    )
        except Exception as exc:
            self.logger.error(
                "Failed to resolve content selectors from database: %s",
                str(exc),
                exc_info=True,
            )

        self.logger.debug(
            "Resolved %d content selector(s) for repo='%s', format='%s', "
            "action='%s'.",
            len(result),
            repository_name,
            format_name,
            action,
        )
        return result

    # -------------------------------------------------------------------
    # Phase 8: Aggregate Permission Check
    # -------------------------------------------------------------------

    def is_permitted(
        self,
        user: User,
        repository_name: Optional[str] = None,
        format_name: Optional[str] = None,
        action: str = "read",
    ) -> bool:
        """High-level convenience method combining all RBAC tiers.

        Performs the complete role-privilege resolution pipeline and checks
        all applicable authorization tiers:

        1. Resolve all roles assigned to the user.
        2. Extract privilege IDs from those roles.
        3. Load full privilege objects from the database.
        4. **Tier 1**: Check system-level permissions first.
        5. **Tier 2**: If not granted at system level and a repository context
           is provided, check repository-level permissions.
        6. Return the result.

        .. note::
            **Tier 3** (content selector) evaluation is NOT performed by this
            method because it requires an ``asset_path`` for CSEL expression
            evaluation.  Tier 3 checks are handled by
            ``src.app.auth.authorization`` which calls
            :meth:`check_content_selector_permission` separately.

        Args:
            user: The :class:`User` requesting access.
            repository_name: The target repository name, or ``None`` for
                system-level checks only.
            format_name: The repository format (e.g., ``'maven2'``), or
                ``None``.
            action: The requested action (default: ``'read'``).

        Returns:
            ``True`` if the user is permitted; ``False`` otherwise.
        """
        if user is None:
            self.logger.warning("is_permitted called with None user.")
            return False

        self.logger.debug(
            "Evaluating permission for user='%s', repo='%s', "
            "format='%s', action='%s'.",
            user.user_id,
            repository_name,
            format_name,
            action,
        )

        # Step 1: Resolve user roles.
        roles: List[Role] = self.get_user_roles(user)
        if not roles:
            self.logger.debug(
                "User '%s' has no roles; permission DENIED.", user.user_id
            )
            return False

        # Step 2: Extract privilege IDs from roles.
        privilege_ids: Set[str] = self.get_role_privilege_ids(roles)
        if not privilege_ids:
            self.logger.debug(
                "User '%s' roles contain no privilege IDs; permission DENIED.",
                user.user_id,
            )
            return False

        # Step 3: Load full privilege objects.
        privileges: List[Privilege] = self.resolve_privileges(privilege_ids)
        if not privileges:
            self.logger.debug(
                "No privileges resolved for user '%s'; permission DENIED.",
                user.user_id,
            )
            return False

        # Step 4: Tier 1 — system-level check.
        # For system-level checks (no specific repository), use the action
        # as the domain.  For repository-level checks, 'repositories' serves
        # as the implied system domain.
        system_domain: str = "repositories" if repository_name else action
        if self.check_system_permission(privileges, system_domain, action):
            self.logger.debug(
                "Permission GRANTED for user='%s' via Tier 1 (system).",
                user.user_id,
            )
            return True

        # Step 5: Tier 2 — repository-level check (only if repo context given).
        if repository_name and format_name:
            if self.check_repository_permission(
                privileges, repository_name, format_name, action
            ):
                self.logger.debug(
                    "Permission GRANTED for user='%s' via Tier 2 (repository).",
                    user.user_id,
                )
                return True

        self.logger.debug(
            "Permission DENIED for user='%s' after evaluating all tiers.",
            user.user_id,
        )
        return False


# ---------------------------------------------------------------------------
# Module Initialization Logging
# ---------------------------------------------------------------------------
logger.debug(
    "RBAC enforcement module loaded — exports: %s",
    ", ".join(__all__),
)
