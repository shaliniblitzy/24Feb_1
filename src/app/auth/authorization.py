"""
Three-Tier RBAC Authorization Engine.

Replaces ``SecurityComponent`` privilege retrieval and CSEL expression
evaluation from the Java source system (Apache Shiro 2.0.0).  This module
implements the complete three-tier RBAC authorization model documented in
AAP Section 0.7.1 (Feature F-301):

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

**Privilege Evaluation Order:**
    System-wide → Repository-scoped → Content Selector.
    First grant wins — once any tier grants access, evaluation stops.

**Architecture Context:**

- Replaces the ``SecurityComponent`` from the Java source, which evaluated
  Apache Shiro realm permissions and CSEL expressions.
- Uses ``RBACEnforcer`` (from ``src.app.auth.rbac``) for low-level
  privilege matching against the three tiers.
- Uses ``ContentSelectorEvaluator`` (from ``src.app.auth.content_selector``)
  for Tier 3 CSEL expression parsing and evaluation.
- Emits Blinker signals (``authz_granted``, ``authz_denied``) for audit
  logging (Feature F-303) and webhook dispatch (Feature F-503).
- Caches effective privileges on the Flask ``g`` request context to avoid
  redundant database queries within a single request.

**Cross-Module Usage:**

- Imported and re-exported by ``src.app.auth.__init__.py``.
- The ``require_permission`` and ``require_repository_permission`` decorators
  are applied to Flask route handlers in ``src.app.api.*`` modules.
- The ``authorize_request`` convenience function is used for inline
  authorization checks within service-layer methods.

Exports:
    AuthorizationEngine            : Main authorization orchestrator class.
    authorize_request              : Module-level convenience function.
    require_permission             : Decorator for system-wide permission checks.
    require_repository_permission  : Decorator for repository-scoped checks.
    authz_granted                  : Blinker signal emitted on access grant.
    authz_denied                   : Blinker signal emitted on access denial.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import List, Optional, Set

from flask import abort, current_app, g, request

from src.app.auth.content_selector import ContentSelectorEvaluator
from src.app.auth.rbac import RBACEnforcer
from src.app.extensions import db, event_signals
from src.app.models.content_selector import ContentSelector
from src.app.models.privilege import Privilege
from src.app.models.role import Role
from src.app.models.user import User

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# Provides structured logging for authorization decisions and privilege
# resolution diagnostics per AAP Section 0.7.2.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "AuthorizationEngine",
    "authorize_request",
    "require_permission",
    "require_repository_permission",
    "authz_granted",
    "authz_denied",
]

# ---------------------------------------------------------------------------
# Authorization Event Signals (Blinker)
# ---------------------------------------------------------------------------
# Replaces Guava EventBus authorization event subscribers from the Java
# source.  These signals are consumed by audit logging subscribers
# (Feature F-303) and webhook dispatchers (Feature F-503) for recording
# every authorization decision.
#
# Signal payloads include:
#   - user_id   : The authenticated user's identifier.
#   - permission : The permission/action that was evaluated.
#   - context   : Additional context (repository_name, format, etc.).
# ---------------------------------------------------------------------------

authz_granted = event_signals.signal("authz-granted")
"""Blinker signal emitted when an authorization check succeeds.

Subscribers receive keyword arguments:
    ``user_id`` (str), ``permission`` (str), ``context`` (dict).
"""

authz_denied = event_signals.signal("authz-denied")
"""Blinker signal emitted when an authorization check fails.

Subscribers receive keyword arguments:
    ``user_id`` (str), ``permission`` (str), ``context`` (dict).
"""

# ---------------------------------------------------------------------------
# Cache key constants for Flask g context
# ---------------------------------------------------------------------------
_G_PRIVILEGES_CACHE: str = "_effective_privileges"
_G_PRIVILEGES_USER_ID: str = "_effective_privileges_user_id"


# ===========================================================================
# AuthorizationEngine Class
# ===========================================================================


class AuthorizationEngine:
    """Three-tier RBAC authorization orchestrator.

    Replaces ``SecurityComponent`` from the Java source system (Apache
    Shiro 2.0.0).  Provides the complete authorization pipeline for the
    Nexus Repository Manager:

    1. Resolve the user's effective privileges from their assigned roles.
    2. Evaluate the requested action against three RBAC tiers.
    3. Emit audit signals for every authorization decision.

    The engine delegates low-level privilege matching to :class:`RBACEnforcer`
    and CSEL expression evaluation to :class:`ContentSelectorEvaluator`,
    keeping this class focused on orchestration and Flask integration.

    **Thread Safety:**
    Instances are lightweight and stateless (aside from the logger and
    sub-engines).  They may be instantiated per-request or shared across
    requests.  Per-request caching is handled via the Flask ``g`` context.

    Usage::

        engine = AuthorizationEngine()

        # Full authorization check with context
        if engine.authorize_request(user, "read", repository_name="my-repo"):
            allow_access()

        # Tier-specific checks
        if engine.check_system_privilege(user, "users", "create"):
            allow_user_creation()

        if engine.check_repository_privilege(user, "my-repo", "maven2", "read"):
            serve_artifact()
    """

    def __init__(self) -> None:
        """Initialise the authorization engine.

        Creates instances of the RBAC enforcer and CSEL evaluator that
        are used for all subsequent authorization evaluations.  Also
        configures the module-level logger for diagnostic output.
        """
        self.rbac: RBACEnforcer = RBACEnforcer()
        self.csel_evaluator: ContentSelectorEvaluator = ContentSelectorEvaluator()
        self.logger: logging.Logger = logging.getLogger(__name__)
        self.logger.debug("AuthorizationEngine initialised.")

    # -------------------------------------------------------------------
    # Main Authorization Entry Point
    # -------------------------------------------------------------------

    def authorize_request(
        self, user: User, permission: str, **context
    ) -> bool:
        """Main authorization entry point for route handlers and services.

        Extracts authorization parameters from the supplied ``**context``
        keyword arguments and delegates to :meth:`is_permitted` for
        three-tier evaluation.  Emits Blinker signals for audit logging
        regardless of the outcome.

        Args:
            user: The authenticated :class:`User` requesting access.
            permission: A high-level permission descriptor (e.g.,
                ``'read'``, ``'admin'``, ``'users:create'``).
            **context: Additional authorization context.  Recognised keys:

                - ``repository_name`` (str): Target repository name.
                - ``format_name`` (str): Repository format (default ``'*'``).
                - ``action`` (str): Specific action (defaults to *permission*).
                - ``asset_path`` (str): Asset path for Tier 3 CSEL checks.

        Returns:
            ``True`` if the user is authorised; ``False`` otherwise.
        """
        if user is None:
            self.logger.warning(
                "authorize_request called with None user for "
                "permission='%s'.",
                permission,
            )
            return False

        repository_name: Optional[str] = context.get("repository_name")
        format_name: Optional[str] = context.get("format_name", "*")
        action: str = context.get("action", permission)
        asset_path: Optional[str] = context.get("asset_path")

        self.logger.debug(
            "authorize_request: user='%s', permission='%s', "
            "repo='%s', format='%s', action='%s', path='%s'.",
            user.user_id,
            permission,
            repository_name,
            format_name,
            action,
            asset_path,
        )

        result: bool = self.is_permitted(
            user,
            repository_name=repository_name,
            format_name=format_name,
            action=action,
            asset_path=asset_path,
        )

        if result:
            self._emit_granted(user, permission, context)
        else:
            self._emit_denied(user, permission, context)

        return result

    # -------------------------------------------------------------------
    # Privilege Resolution
    # -------------------------------------------------------------------

    def get_effective_privileges(self, user: User) -> List[Privilege]:
        """Collect all privilege objects from all roles assigned to the user.

        Traverses the User → Role → Privilege chain:
        1. Resolve all roles assigned to the user.
        2. Aggregate all privilege IDs from those roles.
        3. Batch-load full :class:`Privilege` objects from the database.

        Results are cached on the Flask ``g`` context for the current
        request lifecycle to avoid redundant database queries.  The cache
        is keyed by ``user.user_id`` so that multiple authorization checks
        for the same user within a single request share the resolved
        privilege set.

        Args:
            user: The :class:`User` whose effective privileges are needed.

        Returns:
            A list of :class:`Privilege` objects.  May be empty if the
            user has no role assignments or all roles have empty privilege
            lists.
        """
        if user is None:
            self.logger.warning(
                "get_effective_privileges called with None user."
            )
            return []

        # -- Check per-request cache on Flask g context --------------------
        try:
            cached: Optional[List[Privilege]] = getattr(
                g, _G_PRIVILEGES_CACHE, None
            )
            if cached is not None:
                cached_uid: Optional[str] = getattr(
                    g, _G_PRIVILEGES_USER_ID, None
                )
                if cached_uid == user.user_id:
                    self.logger.debug(
                        "Returning cached effective privileges for user='%s' "
                        "(%d privilege(s)).",
                        user.user_id,
                        len(cached),
                    )
                    return cached
        except RuntimeError:
            # Outside of Flask request context — skip cache lookup.
            pass

        # -- Resolve fresh from database -----------------------------------
        roles: List[Role] = self._resolve_user_roles(user)
        privilege_ids: Set[str] = self._resolve_role_privileges(roles)
        privileges: List[Privilege] = self._load_privileges(privilege_ids)

        self.logger.debug(
            "Resolved %d effective privilege(s) for user='%s' "
            "from %d role(s).",
            len(privileges),
            user.user_id,
            len(roles),
        )

        # -- Store in per-request cache ------------------------------------
        try:
            setattr(g, _G_PRIVILEGES_CACHE, privileges)
            setattr(g, _G_PRIVILEGES_USER_ID, user.user_id)
        except RuntimeError:
            # Outside of Flask request context — skip caching.
            pass

        return privileges

    # -------------------------------------------------------------------
    # Tier 1: System-Wide Privilege Check
    # -------------------------------------------------------------------

    def check_system_privilege(
        self, user: User, domain: str, action: str
    ) -> bool:
        """Evaluate Tier 1 (system-wide) authorisation.

        Checks whether the user holds an ``'application'`` or ``'wildcard'``
        privilege that grants the requested *action* on the specified
        *domain*.

        The ``nx-all`` wildcard privilege bypasses all checks and grants
        unrestricted access.

        Args:
            user: The authenticated :class:`User`.
            domain: The system domain (e.g., ``'users'``, ``'tasks'``,
                ``'settings'``, ``'repositories'``).
            action: The action being performed (e.g., ``'create'``,
                ``'read'``, ``'update'``, ``'delete'``, ``'run'``).

        Returns:
            ``True`` if a Tier 1 privilege grants the requested action.
        """
        if user is None:
            return False

        privileges: List[Privilege] = self.get_effective_privileges(user)
        result: bool = self.rbac.check_system_permission(
            privileges, domain, action
        )

        self.logger.debug(
            "Tier 1 check: user='%s', domain='%s', action='%s' → %s.",
            user.user_id,
            domain,
            action,
            "GRANTED" if result else "DENIED",
        )
        return result

    # -------------------------------------------------------------------
    # Tier 2: Repository-Scoped Privilege Check
    # -------------------------------------------------------------------

    def check_repository_privilege(
        self,
        user: User,
        repository_name: str,
        format_name: str,
        action: str,
    ) -> bool:
        """Evaluate Tier 2 (repository-scoped) authorisation.

        Checks ``'repository-admin'`` and ``'repository-view'`` type
        privileges against the supplied (repository, format, action) tuple.
        Supports wildcard (``'*'``) patterns in format and repository
        privilege properties.

        Args:
            user: The authenticated :class:`User`.
            repository_name: The target repository (e.g.,
                ``'maven-central'``, ``'docker-hosted'``).
            format_name: The repository format (e.g., ``'maven2'``,
                ``'npm'``, ``'docker'``).
            action: The action being performed (``'browse'``, ``'read'``,
                ``'edit'``, ``'delete'``, ``'add'``, ``'admin'``).

        Returns:
            ``True`` if a Tier 2 privilege grants the requested action.
        """
        if user is None:
            return False

        privileges: List[Privilege] = self.get_effective_privileges(user)
        result: bool = self.rbac.check_repository_permission(
            privileges, repository_name, format_name, action
        )

        self.logger.debug(
            "Tier 2 check: user='%s', repo='%s', format='%s', "
            "action='%s' → %s.",
            user.user_id,
            repository_name,
            format_name,
            action,
            "GRANTED" if result else "DENIED",
        )
        return result

    # -------------------------------------------------------------------
    # Tier 3: Sub-Repository (Content Selector) Privilege Check
    # -------------------------------------------------------------------

    def check_content_selector_privilege(
        self,
        user: User,
        repository_name: str,
        format_name: str,
        asset_path: str,
        action: str,
    ) -> bool:
        """Evaluate Tier 3 (sub-repository) authorisation via CSEL.

        Finds all ``'repository-content-selector'`` privileges matching
        the (repository, format, action) tuple, resolves their linked
        Content Selector expressions from the database, and evaluates
        each expression against the *asset_path* context.

        Access is granted if **any** content selector expression matches.

        Args:
            user: The authenticated :class:`User`.
            repository_name: The target repository name.
            format_name: The repository format identifier.
            asset_path: The asset path within the repository to evaluate
                against CSEL expressions.
            action: The action being performed.

        Returns:
            ``True`` if any Tier 3 content selector privilege grants
            access to the specified asset path.
        """
        if user is None:
            return False

        privileges: List[Privilege] = self.get_effective_privileges(user)

        # Resolve matching content selector (name, expression) tuples
        # from RBACEnforcer.  This does NOT evaluate the expressions —
        # it only resolves which selectors to evaluate.
        selectors = self.rbac.check_content_selector_permission(
            privileges, repository_name, format_name, action
        )

        if not selectors:
            self.logger.debug(
                "Tier 3 check: no matching content selector privileges "
                "for user='%s', repo='%s', format='%s', action='%s'.",
                user.user_id,
                repository_name,
                format_name,
                action,
            )
            return False

        # Build the CSEL evaluation context from the asset path.
        csel_context: dict = {
            "format": format_name,
            "path": asset_path,
        }

        # Evaluate each matched selector expression.
        for selector_name, expression in selectors:
            try:
                if self.csel_evaluator.evaluate(expression, csel_context):
                    self.logger.debug(
                        "Tier 3 check: content selector '%s' MATCHED "
                        "for user='%s', path='%s'.",
                        selector_name,
                        user.user_id,
                        asset_path,
                    )
                    return True
            except Exception as exc:
                self.logger.error(
                    "Error evaluating content selector '%s' for "
                    "user='%s': %s",
                    selector_name,
                    user.user_id,
                    str(exc),
                    exc_info=True,
                )
                # Continue to next selector rather than failing hard.
                continue

        self.logger.debug(
            "Tier 3 check: no content selectors matched for user='%s', "
            "repo='%s', path='%s' (%d selector(s) evaluated).",
            user.user_id,
            repository_name,
            asset_path,
            len(selectors),
        )
        return False

    # -------------------------------------------------------------------
    # Composite Authorization — All Three Tiers
    # -------------------------------------------------------------------

    def is_permitted(
        self,
        user: User,
        repository_name: Optional[str] = None,
        format_name: Optional[str] = None,
        action: str = "read",
        asset_path: Optional[str] = None,
    ) -> bool:
        """High-level authorization checking all three RBAC tiers in order.

        **Evaluation order** (first grant wins):

        1. **Tier 1 — System-wide**: Check for ``'wildcard'`` (nx-all)
           and ``'application'`` privileges.  If granted, return
           immediately.
        2. **Tier 2 — Repository-scoped**: Check ``'repository-admin'``
           and ``'repository-view'`` privileges against the target
           repository.  If granted, return immediately.
        3. **Tier 3 — Content Selector**: Only evaluated when
           *asset_path* is provided AND Tiers 1 & 2 did not grant
           access.  Evaluates CSEL expressions against the asset path.

        Returns ``True`` if **any** tier grants access.  Logs the
        authorization decision for audit purposes.

        Args:
            user: The authenticated :class:`User`.
            repository_name: Target repository name, or ``None`` for
                system-level checks only.
            format_name: Repository format, or ``None``.
            action: The requested action (default: ``'read'``).
            asset_path: Asset path for Tier 3 CSEL evaluation, or
                ``None`` to skip Tier 3.

        Returns:
            ``True`` if the user is permitted; ``False`` otherwise.
        """
        if user is None:
            self.logger.warning("is_permitted called with None user.")
            return False

        privileges: List[Privilege] = self.get_effective_privileges(user)
        if not privileges:
            self.logger.debug(
                "User '%s' has no effective privileges; permission DENIED.",
                user.user_id,
            )
            return False

        # -- Tier 1: System-wide -----------------------------------------
        # For system-level checks (no specific repository), use the action
        # as the domain.  For repository-level checks, 'repositories'
        # serves as the implied system domain — mirroring the Java source
        # SecurityComponent behaviour.
        system_domain: str = "repositories" if repository_name else action
        if self.rbac.check_system_permission(
            privileges, system_domain, action
        ):
            self.logger.info(
                "Permission GRANTED for user='%s' via Tier 1 (system), "
                "domain='%s', action='%s'.",
                user.user_id,
                system_domain,
                action,
            )
            return True

        # -- Tier 2: Repository-scoped -----------------------------------
        if repository_name and format_name:
            if self.rbac.check_repository_permission(
                privileges, repository_name, format_name, action
            ):
                self.logger.info(
                    "Permission GRANTED for user='%s' via Tier 2 "
                    "(repository), repo='%s', format='%s', action='%s'.",
                    user.user_id,
                    repository_name,
                    format_name,
                    action,
                )
                return True

        # -- Tier 3: Content Selector (only if asset_path provided) ------
        if repository_name and format_name and asset_path:
            selectors = self.rbac.check_content_selector_permission(
                privileges, repository_name, format_name, action
            )
            if selectors:
                csel_context: dict = {
                    "format": format_name,
                    "path": asset_path,
                }
                for selector_name, expression in selectors:
                    try:
                        if self.csel_evaluator.evaluate(
                            expression, csel_context
                        ):
                            self.logger.info(
                                "Permission GRANTED for user='%s' via "
                                "Tier 3 (content selector '%s'), "
                                "repo='%s', path='%s'.",
                                user.user_id,
                                selector_name,
                                repository_name,
                                asset_path,
                            )
                            return True
                    except Exception as exc:
                        self.logger.error(
                            "Error evaluating content selector '%s' for "
                            "user='%s': %s",
                            selector_name,
                            user.user_id,
                            str(exc),
                            exc_info=True,
                        )
                        continue

        # -- All tiers exhausted — DENY ----------------------------------
        self.logger.debug(
            "Permission DENIED for user='%s' after evaluating all tiers. "
            "repo='%s', format='%s', action='%s', path='%s'.",
            user.user_id,
            repository_name,
            format_name,
            action,
            asset_path,
        )
        return False

    # -------------------------------------------------------------------
    # Private Helpers — Role & Privilege Resolution
    # -------------------------------------------------------------------

    def _resolve_user_roles(self, user: User) -> List[Role]:
        """Fetch all roles assigned to the user.

        Delegates to :meth:`RBACEnforcer.get_user_roles` which traverses
        the User → Role many-to-many relationship via the
        ``role_assignments`` junction table.

        Includes both ``'internal'`` (locally-created) and ``'external'``
        (LDAP/SSO-mapped) source roles.

        Args:
            user: The :class:`User` whose roles are to be resolved.

        Returns:
            A list of :class:`Role` objects.  May be empty.
        """
        return self.rbac.get_user_roles(user)

    def _resolve_role_privileges(self, roles: List[Role]) -> Set[str]:
        """Aggregate all privilege IDs from all roles.

        Delegates to :meth:`RBACEnforcer.get_role_privilege_ids` which
        iterates each role's ``privilege_list`` property to collect and
        deduplicate all privilege IDs.

        Args:
            roles: A list of :class:`Role` objects.

        Returns:
            A deduplicated set of privilege ID strings.
        """
        return self.rbac.get_role_privilege_ids(roles)

    def _load_privileges(self, privilege_ids: Set[str]) -> List[Privilege]:
        """Batch-load Privilege objects from the database by their IDs.

        Delegates to :meth:`RBACEnforcer.resolve_privileges` which
        performs a single ``Privilege.query.filter(in_(...))`` query for
        efficient batch loading.

        Args:
            privilege_ids: A set of privilege ID strings to resolve.

        Returns:
            A list of :class:`Privilege` objects.  IDs not found in the
            database are silently skipped.
        """
        return self.rbac.resolve_privileges(privilege_ids)

    # -------------------------------------------------------------------
    # Private Helpers — Signal Emission
    # -------------------------------------------------------------------

    def _emit_granted(
        self, user: User, permission: str, context: dict
    ) -> None:
        """Emit the ``authz-granted`` signal for audit logging.

        Sends the signal with keyword arguments that audit subscribers
        (Feature F-303) can use to record the authorization event.

        Args:
            user: The user who was granted access.
            permission: The permission descriptor that was evaluated.
            context: Additional authorization context.
        """
        try:
            authz_granted.send(
                self,
                user_id=user.user_id,
                permission=permission,
                context=context,
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to emit authz-granted signal for user='%s': %s",
                user.user_id,
                str(exc),
            )

    def _emit_denied(
        self, user: User, permission: str, context: dict
    ) -> None:
        """Emit the ``authz-denied`` signal for audit logging.

        Sends the signal with keyword arguments that audit subscribers
        (Feature F-303) can use to record the denied access event.

        Args:
            user: The user who was denied access.
            permission: The permission descriptor that was evaluated.
            context: Additional authorization context.
        """
        try:
            authz_denied.send(
                self,
                user_id=user.user_id,
                permission=permission,
                context=context,
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to emit authz-denied signal for user='%s': %s",
                user.user_id,
                str(exc),
            )


# ===========================================================================
# Module-Level Convenience Functions and Decorators
# ===========================================================================


def authorize_request(permission: str, **context) -> bool:
    """Convenience function for authorization checks in route handlers.

    Retrieves the current user from the Flask ``g`` context and delegates
    to :meth:`AuthorizationEngine.authorize_request`.  Returns ``False``
    if no authenticated user is available on ``g.current_user``.

    This function is designed for inline authorization checks within
    service methods and route handlers where the full decorator pattern
    is not appropriate.

    Args:
        permission: A high-level permission descriptor (e.g., ``'read'``).
        **context: Additional authorization context (see
            :meth:`AuthorizationEngine.authorize_request` for recognised
            keys).

    Returns:
        ``True`` if the current user is authorised; ``False`` otherwise.

    Example::

        @app.route('/api/v1/repositories')
        def list_repositories():
            if not authorize_request('read', repository_name='*'):
                abort(403)
            return jsonify(repositories)
    """
    user: Optional[User] = getattr(g, "current_user", None)
    if user is None:
        logger.debug(
            "authorize_request: no current_user on g context; "
            "returning False for permission='%s'.",
            permission,
        )
        return False

    engine: AuthorizationEngine = AuthorizationEngine()
    return engine.authorize_request(user, permission, **context)


def require_permission(domain: str, action: str):
    """Decorator to require a specific system-wide (Tier 1) permission.

    Checks whether the current user (from ``g.current_user``) holds a
    system-wide privilege matching the given *domain* and *action*.
    Aborts with ``401`` if no user is authenticated, or ``403`` if the
    user lacks the required permission.

    Uses :meth:`AuthorizationEngine.check_system_privilege` for the
    authorization evaluation.

    Args:
        domain: The system domain (e.g., ``'users'``, ``'tasks'``,
            ``'settings'``).
        action: The action to check (e.g., ``'create'``, ``'read'``,
            ``'update'``, ``'delete'``).

    Returns:
        A decorator function that wraps a Flask route handler.

    Example::

        @app.route('/api/v1/users', methods=['POST'])
        @require_permission('users', 'create')
        def create_user():
            ...
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user: Optional[User] = getattr(g, "current_user", None)
            if user is None:
                logger.warning(
                    "require_permission(%s, %s): no authenticated user; "
                    "aborting 401.",
                    domain,
                    action,
                )
                abort(401, description="Authentication required")

            engine: AuthorizationEngine = AuthorizationEngine()
            if not engine.check_system_privilege(user, domain, action):
                logger.warning(
                    "require_permission(%s, %s): user='%s' lacks "
                    "privilege; aborting 403.",
                    domain,
                    action,
                    user.user_id,
                )
                # Emit denied signal for audit trail.
                try:
                    authz_denied.send(
                        None,
                        user_id=user.user_id,
                        permission=f"{domain}:{action}",
                        context={"endpoint": request.endpoint},
                    )
                except Exception:
                    pass
                abort(
                    403,
                    description=(
                        f"Insufficient privileges: {domain}:{action}"
                    ),
                )

            return f(*args, **kwargs)

        return decorated_function

    return decorator


def require_repository_permission(action: str):
    """Decorator to require a repository-scoped (Tier 2) permission.

    Repository name and format are extracted from the decorated route's
    keyword arguments (``repository_name`` and ``format_name``).  If
    ``format_name`` is not present in the route parameters, ``'*'`` is
    used as the default (matching any format).

    Aborts with ``401`` if no user is authenticated, or ``403`` if the
    user lacks the required repository-level permission.

    Uses :meth:`AuthorizationEngine.check_repository_privilege` for the
    authorization evaluation.

    Args:
        action: The action to check (e.g., ``'read'``, ``'browse'``,
            ``'edit'``, ``'delete'``, ``'add'``, ``'admin'``).

    Returns:
        A decorator function that wraps a Flask route handler.

    Example::

        @app.route('/api/v1/repositories/<repository_name>/components')
        @require_repository_permission('read')
        def list_components(repository_name):
            ...
    """

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user: Optional[User] = getattr(g, "current_user", None)
            repository_name: Optional[str] = kwargs.get("repository_name")
            format_name: str = kwargs.get("format_name", "*")

            if user is None:
                logger.warning(
                    "require_repository_permission(%s): no authenticated "
                    "user; aborting 401.",
                    action,
                )
                abort(401, description="Authentication required")

            engine: AuthorizationEngine = AuthorizationEngine()
            if not engine.check_repository_privilege(
                user, repository_name, format_name, action
            ):
                logger.warning(
                    "require_repository_permission(%s): user='%s' lacks "
                    "privilege for repo='%s'; aborting 403.",
                    action,
                    user.user_id,
                    repository_name,
                )
                # Emit denied signal for audit trail.
                try:
                    authz_denied.send(
                        None,
                        user_id=user.user_id,
                        permission=f"{repository_name}:{action}",
                        context={
                            "endpoint": request.endpoint,
                            "format": format_name,
                        },
                    )
                except Exception:
                    pass
                abort(
                    403,
                    description=(
                        f"Insufficient repository privileges: "
                        f"{repository_name}:{action}"
                    ),
                )

            return f(*args, **kwargs)

        return decorated_function

    return decorator


# ---------------------------------------------------------------------------
# Module Initialization Logging
# ---------------------------------------------------------------------------
logger.debug(
    "Authorization engine module loaded — exports: %s",
    ", ".join(__all__),
)
