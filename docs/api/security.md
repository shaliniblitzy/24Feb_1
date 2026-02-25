# Security Management API Reference

This document provides a comprehensive reference for all security management REST API endpoints in the Nexus Repository Manager Python/Flask application.

## Overview

The security management module covers the following features:

- **F-301 — Role-Based Access Control (RBAC):** Three-tier RBAC with system-wide, repository-scoped, and Content Selector Expression Language (CSEL) sub-repository privileges
- **F-302 — SSL/TLS Certificate Management:** PEM/PKCS12 certificate import/export and trust store lifecycle management
- **F-303 — Audit Logging:** JSON-serialized audit event capture with SIEM integration for all security-relevant operations
- **F-304 — API Key Authentication:** Bearer token generation and management for CI/CD pipeline integration

All endpoints are served under the `/service/rest/v1/security/` base path.

### Implementation

The security framework replaces Apache Shiro 2.0.0 from the original Java implementation, using the following Python libraries:

| Component | Library | Replaces |
|-----------|---------|----------|
| Session management | Flask-Login 0.6.3 | Shiro session handling |
| JWT tokens | PyJWT 2.10.1 | Java-JWT (auth0) 4.4.0 |
| Password hashing | bcrypt 4.2.1 | BouncyCastle 1.78.1 |
| Certificate handling | cryptography 44.0.0 | BouncyCastle 1.78.1 |
| LDAP integration | python-ldap 3.4.4 | Shiro LDAP realm |

**Source Files:**

- Route handlers: `src/security/routes.py`
- Authentication chain: `src/security/auth/chain.py`
- RBAC evaluator: `src/security/rbac.py`
- CSEL parser: `src/security/csel.py`
- SSL manager: `src/security/ssl_manager.py`
- Audit service: `src/security/audit.py`
- Route decorators: `src/security/decorators.py`

### Common Response Codes

| Code | Description |
|------|-------------|
| `200` | Success |
| `201` | Resource created |
| `204` | Success with no content (delete operations) |
| `400` | Bad request — validation error |
| `401` | Unauthorized — authentication required or failed |
| `403` | Forbidden — insufficient privileges |
| `404` | Resource not found |
| `409` | Conflict — resource already exists |
| `500` | Internal server error |

> **Important:** Per the security rules, authentication is ALWAYS evaluated before authorization. Unauthenticated requests to protected endpoints receive `401 Unauthorized` — never `403 Forbidden`.

---

## User Management

Manage user accounts including creation, updates, password management, and soft-deletion. All user modifications emit the `user_modified` blinker signal, which triggers audit logging and webhook notifications.

### List Users

```
GET /service/rest/v1/security/users
```

List all user accounts in the system.

**Authentication:** Required — `nx-admin` role or `users-read` privilege

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `page` | integer | No | Page number (default: 1) |
| `per_page` | integer | No | Items per page (default: 20, max: 100) |
| `status` | string | No | Filter by status (ACTIVE, DISABLED, LOCKED, CHANGE_PASSWORD) |

**Response:**

```json
{
  "items": [
    {
      "id": "user-001",
      "username": "admin",
      "email": "admin@example.com",
      "status": "ACTIVE",
      "roles": ["nx-admin"],
      "created_at": "2026-01-01T00:00:00Z",
      "updated_at": "2026-01-01T00:00:00Z",
      "last_login": "2026-02-24T09:00:00Z"
    }
  ],
  "total": 42,
  "page": 1,
  "per_page": 20
}
```

> **Note:** The `password_hash` field is never included in API responses. Plaintext passwords are never stored or logged.

### Create User

```
POST /service/rest/v1/security/users
```

Create a new user account.

**Authentication:** Required — `nx-admin` role or `users-create` privilege

**Request Body:**

```json
{
  "username": "developer1",
  "password": "SecureP@ss123",
  "email": "dev1@example.com",
  "status": "ACTIVE",
  "roles": ["nx-developer"]
}
```

**Password Rules:**

- Minimum 8 characters
- Must include at least one uppercase letter, one lowercase letter, one digit, and one special character
- Password is hashed with bcrypt before storage (replaces BouncyCastle 1.78.1)
- Plaintext password is NEVER stored or logged (per zero plaintext credential storage policy)

**Response:** `201 Created`

```json
{
  "id": "user-002",
  "username": "developer1",
  "email": "dev1@example.com",
  "status": "ACTIVE",
  "roles": ["nx-developer"],
  "created_at": "2026-02-24T10:30:00Z",
  "updated_at": "2026-02-24T10:30:00Z"
}
```

**Error Responses:**

| Code | Condition |
|------|-----------|
| `400` | Missing required fields or invalid password |
| `409` | Username already exists |

### Get User

```
GET /service/rest/v1/security/users/{username}
```

Retrieve a specific user by username.

**Authentication:** Required — `nx-admin` role, `users-read` privilege, or the user themselves

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `username` | string | The username to retrieve |

**Response:** `200 OK`

```json
{
  "id": "user-001",
  "username": "admin",
  "email": "admin@example.com",
  "status": "ACTIVE",
  "roles": ["nx-admin"],
  "attributes": {},
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-02-20T14:00:00Z",
  "last_login": "2026-02-24T09:00:00Z"
}
```

### Update User

```
PUT /service/rest/v1/security/users/{username}
```

Update user details. Cannot be used to change passwords (use the dedicated password endpoint).

**Authentication:** Required — `nx-admin` role or `users-update` privilege

**Request Body:**

```json
{
  "email": "updated@example.com",
  "status": "ACTIVE",
  "roles": ["nx-admin", "nx-developer"]
}
```

**Response:** `200 OK` with updated user object.

### Delete User

```
DELETE /service/rest/v1/security/users/{username}
```

Delete a user account (soft-delete). The user record is retained for audit trail purposes but the account is deactivated.

**Authentication:** Required — `nx-admin` role or `users-delete` privilege

**Response:** `204 No Content`

### Change Password

```
PUT /service/rest/v1/security/users/{username}/password
```

Change a user's password. Users can change their own password; administrators can change any user's password.

**Authentication:** Required — the user themselves or `nx-admin` role

**Request Body:**

```json
{
  "current_password": "OldP@ss123",
  "new_password": "NewSecureP@ss456"
}
```

> **Note:** The `current_password` field is required when users change their own password. Administrators may omit it when resetting another user's password.

**Response:** `204 No Content`

**Error Responses:**

| Code | Condition |
|------|-----------|
| `400` | New password does not meet complexity requirements |
| `401` | Current password is incorrect |

### User Status Values

| Status | Description |
|--------|-------------|
| `ACTIVE` | User can authenticate normally |
| `DISABLED` | User exists but cannot authenticate |
| `LOCKED` | User temporarily locked due to excessive failed login attempts |
| `CHANGE_PASSWORD` | User must change password on next login |

---

## Role Management

Manage roles that aggregate privileges into assignable units. Roles support nesting — a role can contain references to other roles. All role modifications emit the `role_modified` blinker signal for audit logging.

### List Roles

```
GET /service/rest/v1/security/roles
```

List all roles in the system.

**Authentication:** Required — `nx-admin` role or `roles-read` privilege

**Response:**

```json
{
  "items": [
    {
      "id": "role-001",
      "name": "nx-admin",
      "description": "Administrator role with full system access",
      "privileges": ["nx-all"],
      "roles": []
    },
    {
      "id": "role-002",
      "name": "nx-developer",
      "description": "Developer role with repository read/write access",
      "privileges": ["nx-repository-view-*", "nx-repository-edit-*"],
      "roles": []
    }
  ]
}
```

### Create Role

```
POST /service/rest/v1/security/roles
```

Create a new role.

**Authentication:** Required — `nx-admin` role or `roles-create` privilege

**Request Body:**

```json
{
  "name": "maven-deployer",
  "description": "Can deploy artifacts to Maven repositories",
  "privileges": [
    "nx-repository-view-maven2-*-read",
    "nx-repository-view-maven2-*-add",
    "nx-repository-view-maven2-*-edit"
  ],
  "roles": []
}
```

**Response:** `201 Created` with the created role object.

**Error Responses:**

| Code | Condition |
|------|-----------|
| `400` | Missing required fields or invalid privilege references |
| `409` | Role name already exists |

### Get Role

```
GET /service/rest/v1/security/roles/{role_id}
```

Retrieve a specific role by ID.

**Authentication:** Required — `nx-admin` role or `roles-read` privilege

**Response:** `200 OK` with the role object.

### Update Role

```
PUT /service/rest/v1/security/roles/{role_id}
```

Update an existing role.

**Authentication:** Required — `nx-admin` role or `roles-update` privilege

**Request Body:**

```json
{
  "description": "Updated description",
  "privileges": ["nx-repository-view-maven2-*-read"],
  "roles": []
}
```

**Response:** `200 OK` with the updated role object.

### Delete Role

```
DELETE /service/rest/v1/security/roles/{role_id}
```

Delete a role. Built-in default roles cannot be deleted.

**Authentication:** Required — `nx-admin` role or `roles-delete` privilege

**Response:** `204 No Content`

### Default Roles

The following roles are seeded during the SECURITY phase of the application startup sequence:

| Role | Description |
|------|-------------|
| `nx-admin` | Full system administrator access — all privileges |
| `nx-anonymous` | Read-only anonymous access to public repositories |

---

## Privilege Management

Privileges define granular permissions that are assigned to roles. The Nexus Repository Manager implements a three-tier RBAC model:

1. **System Privileges (Tier 1)** — Global administrative permissions controlling access to system-wide functions such as user management, configuration, and scheduling
2. **Repository Privileges (Tier 2)** — Per-repository read/write/admin permissions scoped to a specific repository format and repository name
3. **CSEL Privileges (Tier 3)** — Sub-repository path-based access control using Content Selector Expression Language (CSEL) expressions for fine-grained component-level permissions

### List Privileges

```
GET /service/rest/v1/security/privileges
```

List all defined privileges.

**Authentication:** Required — `nx-admin` role or `privileges-read` privilege

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `type` | string | No | Filter by privilege type |

**Response:**

```json
{
  "items": [
    {
      "id": "priv-001",
      "type": "repository-view",
      "name": "nx-repository-view-maven2-releases-read",
      "description": "Read access to maven2 releases repository",
      "properties": {
        "format": "maven2",
        "repository": "releases",
        "actions": ["READ"]
      }
    },
    {
      "id": "priv-002",
      "type": "repository-content-selector",
      "name": "nx-csel-maven-org-example",
      "description": "Access to org.example namespace in Maven repos",
      "properties": {
        "content_selector": "maven-org-example",
        "repository": "*",
        "actions": ["READ", "ADD"]
      }
    }
  ]
}
```

### Create Privilege

```
POST /service/rest/v1/security/privileges
```

Create a new privilege.

**Authentication:** Required — `nx-admin` role or `privileges-create` privilege

**Request Body:**

```json
{
  "type": "repository-view",
  "name": "nx-repository-view-npm-releases-read",
  "description": "Read access to npm releases repository",
  "properties": {
    "format": "npm",
    "repository": "npm-releases",
    "actions": ["READ"]
  }
}
```

**Response:** `201 Created` with the created privilege object.

### Update Privilege

```
PUT /service/rest/v1/security/privileges/{privilege_id}
```

Update an existing privilege.

**Authentication:** Required — `nx-admin` role or `privileges-update` privilege

**Response:** `200 OK` with the updated privilege object.

### Delete Privilege

```
DELETE /service/rest/v1/security/privileges/{privilege_id}
```

Delete a privilege. Built-in privileges cannot be deleted.

**Authentication:** Required — `nx-admin` role or `privileges-delete` privilege

**Response:** `204 No Content`

### Privilege Types

| Type | Description | Properties |
|------|-------------|------------|
| `application` | System-wide application permissions | `actions` |
| `repository-admin` | Repository administration | `format`, `repository`, `actions` |
| `repository-view` | Repository content access | `format`, `repository`, `actions` |
| `repository-content-selector` | Sub-repository CSEL-based access | `content_selector`, `repository`, `actions` |
| `script` | Script execution permissions | `script_name`, `actions` |

### Actions

| Action | Description |
|--------|-------------|
| `READ` | View and download content |
| `ADD` | Upload and create new content |
| `EDIT` | Modify existing content |
| `DELETE` | Remove content |
| `ASSOCIATE` | Associate components with each other |
| `DISASSOCIATE` | Remove component associations |
| `ALL` | Grants all of the above actions |

---

## Content Selector Management

Content Selectors define CSEL (Content Selector Expression Language) expressions for sub-repository access control (Tier 3 of the RBAC model). CSEL expressions are parsed and evaluated by `src/security/csel.py` against component path and attribute data at request time.

### List Content Selectors

```
GET /service/rest/v1/security/content-selectors
```

List all content selectors.

**Authentication:** Required — `nx-admin` role

**Response:**

```json
{
  "items": [
    {
      "id": "cs-001",
      "name": "maven-org-example",
      "type": "csel",
      "description": "Components in the org.example Maven namespace",
      "expression": "format == \"maven2\" and path =^ \"/org/example/\""
    }
  ]
}
```

### Create Content Selector

```
POST /service/rest/v1/security/content-selectors
```

Create a new content selector.

**Authentication:** Required — `nx-admin` role

**Request Body:**

```json
{
  "name": "docker-production",
  "type": "csel",
  "description": "Production Docker images only",
  "expression": "format == \"docker\" and path =^ \"/production/\""
}
```

**Response:** `201 Created` with the created content selector object.

**Error Responses:**

| Code | Condition |
|------|-----------|
| `400` | Invalid CSEL expression syntax |
| `409` | Content selector name already exists |

### Update Content Selector

```
PUT /service/rest/v1/security/content-selectors/{id}
```

Update an existing content selector expression or metadata.

**Authentication:** Required — `nx-admin` role

**Request Body:**

```json
{
  "description": "Updated description",
  "expression": "format == \"docker\" and path =^ \"/prod/\""
}
```

**Response:** `200 OK` with the updated content selector object.

### Delete Content Selector

```
DELETE /service/rest/v1/security/content-selectors/{id}
```

Delete a content selector. Content selectors currently referenced by privileges cannot be deleted.

**Authentication:** Required — `nx-admin` role

**Response:** `204 No Content`

### CSEL Expression Syntax

The Content Selector Expression Language provides a declarative query syntax for matching components and assets within repositories.

**Operators:**

| Operator | Description | Example |
|----------|-------------|---------|
| `==` | Equals | `format == "maven2"` |
| `!=` | Not equals | `format != "raw"` |
| `=^` | Starts with | `path =^ "/org/example/"` |
| `=~` | Regex match | `path =~ ".*-SNAPSHOT"` |
| `and` | Logical AND | `format == "maven2" and path =^ "/com/"` |
| `or` | Logical OR | `format == "npm" or format == "pypi"` |
| `not` | Logical NOT | `not path =^ "/internal/"` |

**Available Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `format` | string | Repository format — one of: `maven2`, `npm`, `docker`, `nuget`, `pypi`, `apt`, `raw` |
| `path` | string | Component or asset path within the repository |
| `name` | string | Component name |
| `group` | string | Component group or namespace |
| `version` | string | Component version string |

**Example Expressions:**

```
# Match all Maven artifacts under org.example
format == "maven2" and path =^ "/org/example/"

# Match all SNAPSHOT versions across all formats
path =~ ".*-SNAPSHOT"

# Match Docker images in production namespace but not internal ones
format == "docker" and path =^ "/production/" and not path =^ "/production/internal/"

# Match npm or PyPI packages
format == "npm" or format == "pypi"
```

---

## SSL/TLS Certificate Management (F-302)

Certificate lifecycle management for PEM and PKCS12 import/export and trust store management. Uses the Python `cryptography` library (replaces BouncyCastle 1.78.1 from the original Java implementation). Implemented in `src/security/ssl_manager.py`.

Trust store certificates are used when proxy repositories connect to upstream registries over TLS, enabling support for self-signed certificates and enterprise CA hierarchies.

### List Certificates

```
GET /service/rest/v1/security/certificates
```

List all certificates in the application trust store.

**Authentication:** Required — `nx-admin` role

**Response:**

```json
{
  "items": [
    {
      "id": "cert-001",
      "subject_dn": "CN=upstream-registry.example.com",
      "issuer_dn": "CN=Enterprise CA",
      "serial_number": "1234567890",
      "not_before": "2025-01-01T00:00:00Z",
      "not_after": "2027-01-01T00:00:00Z",
      "fingerprint_sha256": "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89",
      "pem": "-----BEGIN CERTIFICATE-----\nMIID...\n-----END CERTIFICATE-----"
    }
  ]
}
```

### Import Certificate

```
POST /service/rest/v1/security/certificates
```

Import a certificate into the application trust store.

**Authentication:** Required — `nx-admin` role

**Request Body (PEM):**

```json
{
  "pem": "-----BEGIN CERTIFICATE-----\nMIID...\n-----END CERTIFICATE-----"
}
```

**Request Body (PKCS12):**

```json
{
  "pkcs12_base64": "<base64-encoded-pkcs12-bundle>",
  "password": "bundle-password"
}
```

**Supported Formats:**

| Format | Description |
|--------|-------------|
| PEM | PEM-encoded X.509 certificates (single or chain) |
| PKCS12 | PKCS#12 bundles with optional password protection |

**Response:** `201 Created` with the imported certificate details.

**Error Responses:**

| Code | Condition |
|------|-----------|
| `400` | Invalid certificate format or expired certificate |
| `409` | Certificate with the same fingerprint already exists |

### Delete Certificate

```
DELETE /service/rest/v1/security/certificates/{id}
```

Remove a certificate from the trust store.

**Authentication:** Required — `nx-admin` role

**Response:** `204 No Content`

### Retrieve Remote Certificate

```
POST /service/rest/v1/security/certificates/retrieve
```

Retrieve and display a remote server's SSL/TLS certificate chain without importing it. This allows administrators to inspect certificates from upstream registries before deciding to trust them.

**Authentication:** Required — `nx-admin` role

**Request Body:**

```json
{
  "host": "upstream-registry.example.com",
  "port": 443
}
```

**Response:** `200 OK`

```json
{
  "certificates": [
    {
      "subject_dn": "CN=upstream-registry.example.com",
      "issuer_dn": "CN=Enterprise CA",
      "serial_number": "1234567890",
      "not_before": "2025-01-01T00:00:00Z",
      "not_after": "2027-01-01T00:00:00Z",
      "fingerprint_sha256": "AB:CD:EF:...",
      "pem": "-----BEGIN CERTIFICATE-----\nMIID...\n-----END CERTIFICATE-----"
    }
  ]
}
```

**Use Case:** Administrators can inspect and then import certificates from upstream registries that use self-signed or enterprise CA certificates, enabling proxy repositories to connect securely over TLS.

---

## API Key Authentication (F-304)

API keys enable non-interactive authentication for CI/CD pipelines and automated tooling. Keys are passed via the `Authorization: Bearer <api-key>` HTTP header and validated by the Bearer Realm (`src/security/auth/bearer_realm.py`) in the authentication chain.

### List API Keys

```
GET /service/rest/v1/security/users/{username}/api-keys
```

List API keys for a user. Key values are partially masked for security.

**Authentication:** Required — the user themselves or `nx-admin` role

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `username` | string | The username whose API keys to list |

**Response:**

```json
{
  "items": [
    {
      "id": "key-001",
      "name": "jenkins-deploy-key",
      "created_at": "2026-01-15T10:00:00Z",
      "last_used": "2026-02-24T08:30:00Z",
      "expires_at": "2027-01-15T10:00:00Z",
      "key_preview": "nxrm-****-****-abcd"
    }
  ]
}
```

### Create API Key

```
POST /service/rest/v1/security/users/{username}/api-keys
```

Generate a new API key for a user.

**Authentication:** Required — the user themselves or `nx-admin` role

**Request Body:**

```json
{
  "name": "github-actions-key",
  "expiration_days": 365
}
```

**Response:** `201 Created`

```json
{
  "id": "key-002",
  "name": "github-actions-key",
  "key": "nxrm-a1b2c3d4-e5f6g7h8-i9j0k1l2",
  "created_at": "2026-02-24T10:30:00Z",
  "expires_at": "2027-02-24T10:30:00Z"
}
```

> **IMPORTANT:** The full API key value is returned ONLY at creation time. It cannot be retrieved later. Store it securely in your CI/CD system's secret management.

### Revoke API Key

```
DELETE /service/rest/v1/security/users/{username}/api-keys/{key_id}
```

Revoke (permanently delete) an API key. The key will no longer be accepted for authentication.

**Authentication:** Required — the user themselves or `nx-admin` role

**Response:** `204 No Content`

### Usage Examples

**Using an API key with curl:**

```bash
curl -H "Authorization: Bearer nxrm-a1b2c3d4-e5f6g7h8-i9j0k1l2" \
  https://nexus.example.com/service/rest/v1/repositories
```

**Maven `settings.xml` configuration:**

```xml
<server>
  <id>nexus</id>
  <username>_api_key</username>
  <password>nxrm-a1b2c3d4-e5f6g7h8-i9j0k1l2</password>
</server>
```

**npm `.npmrc` configuration:**

```
//nexus.example.com/repository/npm-hosted/:_authToken=nxrm-a1b2c3d4-e5f6g7h8-i9j0k1l2
```

**Docker login:**

```bash
echo "nxrm-a1b2c3d4-e5f6g7h8-i9j0k1l2" | docker login nexus.example.com -u _api_key --password-stdin
```

**pip configuration:**

```
pip install --index-url https://_api_key:nxrm-a1b2c3d4-e5f6g7h8-i9j0k1l2@nexus.example.com/repository/pypi-hosted/simple/ package-name
```

---

## Audit Logging (F-303)

JSON-serialized audit events for all security-relevant operations. Events are captured via blinker signals (replacing Guava EventBus) and persisted to the `AuditEvent` model in the database. The audit service (`src/security/audit.py`) subscribes to application signals and serializes event data for persistence and optional SIEM forwarding.

### Query Audit Events

```
GET /service/rest/v1/security/audit
```

Query audit log events with filtering and pagination.

**Authentication:** Required — `nx-admin` role or `audit-read` privilege

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `event_type` | string | No | Filter by event type (see table below) |
| `user_id` | string | No | Filter by the user who triggered the event |
| `source_ip` | string | No | Filter by client IP address |
| `from_date` | datetime | No | Start of date range (ISO 8601 format) |
| `to_date` | datetime | No | End of date range (ISO 8601 format) |
| `page` | integer | No | Page number (default: 1) |
| `per_page` | integer | No | Items per page (default: 20, max: 100) |

**Response:**

```json
{
  "items": [
    {
      "id": "evt-001",
      "timestamp": "2026-02-24T10:30:00Z",
      "event_type": "auth_success",
      "user_id": "admin",
      "source_ip": "192.168.1.100",
      "attributes": {
        "realm": "local",
        "method": "basic_auth"
      },
      "payload": {}
    },
    {
      "id": "evt-002",
      "timestamp": "2026-02-24T10:31:00Z",
      "event_type": "user_modified",
      "user_id": "admin",
      "source_ip": "192.168.1.100",
      "attributes": {
        "action": "create",
        "target_user": "developer1"
      },
      "payload": {
        "username": "developer1",
        "roles": ["nx-developer"]
      }
    }
  ],
  "total": 1542,
  "page": 1,
  "per_page": 20
}
```

### Audit Event Types

The following event types are captured through the blinker signal system defined in `src/signals.py`:

| Event Type | Trigger | Signal Source |
|------------|---------|---------------|
| `auth_success` | Successful authentication | `auth_success` signal |
| `auth_failure` | Failed authentication attempt | `auth_failure` signal |
| `repository_created` | New repository created | `repository_created` signal |
| `repository_deleted` | Repository deleted | `repository_deleted` signal |
| `component_uploaded` | Artifact uploaded to a repository | `component_uploaded` signal |
| `component_deleted` | Artifact deleted (manual or cleanup) | `component_deleted` signal |
| `user_modified` | User created, updated, or deleted | `user_modified` signal |
| `role_modified` | Role created, updated, or deleted | `role_modified` signal |
| `config_changed` | System configuration value modified | `config_changed` signal |
| `task_completed` | Scheduled task execution completed | `task_completed` signal |

### SIEM Integration

Audit events can be forwarded to external Security Information and Event Management (SIEM) systems in real-time. Configure the following settings in the application configuration:

| Setting | Description |
|---------|-------------|
| `SIEM_ENDPOINT` | URL of the SIEM ingestion endpoint |
| `SIEM_TOKEN` | Authentication token for the SIEM endpoint |
| `SIEM_ENABLED` | Enable or disable SIEM forwarding (default: `false`) |
| `SIEM_BATCH_SIZE` | Number of events to batch before forwarding (default: `10`) |
| `SIEM_FLUSH_INTERVAL` | Maximum interval between SIEM flushes in seconds (default: `30`) |

See [Configuration Reference](../configuration.md) for complete configuration details.

---

## Data Models

The following SQLAlchemy ORM models back the security management endpoints. Models are defined in `src/models/security.py` and `src/models/audit.py` with common mixins from `src/models/base.py` providing `created_at`, `updated_at`, and soft-delete functionality.

### User

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary key | Unique user identifier |
| `username` | string | Unique, not null | Login username |
| `password_hash` | string | Not null | bcrypt password hash (never exposed via API) |
| `email` | string | | User email address |
| `status` | string | Not null, default: ACTIVE | ACTIVE / DISABLED / LOCKED / CHANGE_PASSWORD |
| `attributes` | JSON | | Additional user properties |
| `created_at` | datetime | Auto-set | Creation timestamp (from TimestampMixin) |
| `updated_at` | datetime | Auto-updated | Last modification timestamp (from TimestampMixin) |

### Role

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary key | Unique role identifier |
| `name` | string | Unique, not null | Role name |
| `description` | string | | Human-readable description |
| `privileges` | JSON | | List of privilege name references |

### Privilege

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary key | Unique privilege identifier |
| `type` | string | Not null | Privilege type (application / repository-admin / repository-view / repository-content-selector / script) |
| `name` | string | Not null | Privilege name |
| `properties` | JSON | | Type-specific properties (format, repository, actions, etc.) |

### ContentSelector

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary key | Unique selector identifier |
| `name` | string | Unique, not null | Selector name |
| `type` | string | Not null, default: csel | Expression type |
| `expression` | string | Not null | CSEL expression string |

### RoleAssignment

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `user_id` | string (FK → User) | Composite PK | Reference to user |
| `role_id` | string (FK → Role) | Composite PK | Reference to role |

### AuditEvent

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary key | Unique event identifier |
| `timestamp` | datetime | Not null, indexed | Event timestamp |
| `event_type` | string | Not null, indexed | Event type category |
| `user_id` | string | Indexed | User who triggered the event |
| `source_ip` | string | | Client IP address |
| `attributes` | JSON | | Event metadata (realm, method, action, etc.) |
| `payload` | JSON | | Event-specific data payload |

---

## Authentication Chain

The authentication chain implements the **FirstSuccessfulAuthenticator** pattern, replacing Apache Shiro 2.0.0 from the original Java implementation. The chain is orchestrated by `src/security/auth/chain.py`.

### Authentication Realms

Realms are evaluated in the following order. The first realm that successfully authenticates the request wins:

| Order | Realm | Module | Authentication Method |
|-------|-------|--------|----------------------|
| 1 | **Local Realm** | `src/security/auth/local_realm.py` | Username/password verification against bcrypt hash stored in database |
| 2 | **Bearer Realm** | `src/security/auth/bearer_realm.py` | API key validation from `Authorization: Bearer` header |
| 3 | **JWT Realm** | `src/security/auth/jwt_handler.py` | JWT token decode and claim validation (PyJWT with HS256/RS256 signing) |
| 4 | **LDAP Realm** | `src/security/auth/ldap_realm.py` | LDAP bind authentication via python-ldap with configurable base DN and group mapping |
| 5 | **SAML/OIDC Realm** | `src/security/auth/saml_realm.py` | SAML assertion processing and OpenID Connect token exchange for SSO |

### Authentication Rules

- Realms are evaluated in strict order (1 through 5); the first successful authentication result is accepted
- If ALL realms fail to authenticate the request, a `401 Unauthorized` response is returned with aggregated failure reasons
- Authentication is ALWAYS evaluated before authorization — unauthenticated requests never receive `403 Forbidden`
- Credentials are extracted from three sources: HTTP `Authorization` header, form POST data, and session cookies

### Route Protection Decorators

Flask route decorators defined in `src/security/decorators.py` enforce authentication and authorization:

| Decorator | Purpose | Example |
|-----------|---------|---------|
| `@requires_auth` | Requires any authenticated user | `@requires_auth` |
| `@requires_role(name)` | Requires a specific role | `@requires_role("nx-admin")` |
| `@requires_privilege(type, resource)` | Requires a specific privilege on a resource | `@requires_privilege("repository-view", "maven-releases")` |

### RBAC Evaluation Flow

After successful authentication, the RBAC evaluator (`src/security/rbac.py`) determines authorization:

1. **Resolve Roles** — Collect all roles assigned to the authenticated user (including nested role inheritance)
2. **Aggregate Privileges** — Gather all privileges from all resolved roles
3. **Evaluate System Privileges (Tier 1)** — Check for system-wide administrative privileges
4. **Evaluate Repository Privileges (Tier 2)** — Check for repository-scoped read/write/admin permissions
5. **Evaluate CSEL Privileges (Tier 3)** — If repository privileges match, evaluate CSEL expressions against the requested component path and attributes

If the user does not hold sufficient privileges at any tier, a `403 Forbidden` response is returned.

---

## Error Responses

All security endpoints return structured error responses following a consistent format:

```json
{
  "error": {
    "type": "AuthenticationError",
    "message": "Invalid credentials provided",
    "status": 401,
    "details": {}
  }
}
```

### Security-Specific Error Types

| Error Type | HTTP Code | Description |
|------------|-----------|-------------|
| `ClientError` | 400 | Invalid request data or validation failure |
| `AuthenticationError` | 401 | Authentication failed — invalid or missing credentials |
| `AuthorizationError` | 403 | Authenticated but insufficient privileges |
| `NotFoundError` | 404 | Requested user, role, privilege, or resource not found |
| `ConflictError` | 409 | Resource already exists (duplicate username, role name, etc.) |
| `SystemError` | 500 | Internal server error |

---

## Related Documentation

- [API Index](README.md) — Overview of all available API endpoints
- [Repository Management API](repositories.md) — Repository CRUD, format-specific endpoints, search, and browse APIs
- [Administration API](admin.md) — Health checks, scheduled tasks, system configuration, and support ZIP
- [Architecture Overview](../architecture.md) — Detailed authentication chain diagram and RBAC layer architecture
- [Configuration Reference](../configuration.md) — LDAP, SAML/OIDC, JWT, SIEM, and all security-related configuration settings
