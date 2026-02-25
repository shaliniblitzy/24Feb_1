"""
Authentication and authorization package for the Nexus Repository Flask application.

Replaces Apache Shiro 2.0.0 security framework from the Java source. Implements
multi-backend authentication (local credentials, API keys, JWT, LDAP/AD, SAML/OIDC)
and three-tier RBAC authorization (system-wide, repository-scoped, sub-repository
via content selectors).

Modules:
- ``password_utils.py``:  Secure password hashing and API key generation
- ``authentication.py``:  Multi-realm authentication chain (future checkpoint)
- ``authorization.py``:   RBAC enforcement logic (future checkpoint)
- ``rbac.py``:            Role-based access control (future checkpoint)
- ``content_selector.py``: CSEL expression evaluation (future checkpoint)
- ``realms/``:            Authentication realm implementations (future checkpoint)
"""
