# Administration API Reference

This document provides the complete REST API reference for all administration endpoints in the Nexus Repository Manager. It covers the following feature areas:

- **F-401 — Health Check Monitoring:** Real-time system status and component health checks
- **F-402 — Scheduled Task Management:** CRUD operations for cron-style scheduled tasks with execution history
- **F-403 — Support ZIP Generation:** Diagnostic bundle generation with automatic credential redaction
- **F-404 — System Configuration Management:** Hierarchical key-value configuration store with type validation

All administration endpoints require administrative privileges (`nx-admin` role) unless explicitly noted otherwise. Endpoints are implemented in `src/admin/routes.py` with service logic distributed across:

- `src/admin/health.py` — Health check service
- `src/admin/scheduler.py` — Scheduled task management service
- `src/admin/support_zip.py` — Support ZIP generation service
- `src/admin/system_config.py` — System configuration management service

> **Base Path:** All REST API endpoints use the `/service/rest/v1/` prefix to maintain backward compatibility with existing Nexus Repository API consumers.

---

## Health Check Monitoring (F-401)

Health checks provide real-time system status monitoring. The health service performs individual component checks (database, BlobStore, scheduler, search) and aggregates them into an overall system status.

> **Availability Guarantee:** The health check endpoint is designed to return health status for all system components — including database connectivity, BlobStore availability, and scheduler state — even when other application endpoints are experiencing failures. This makes it suitable for use as a load balancer health probe.

### GET /service/rest/v1/status/check

Returns the aggregated health status of all system components.

**Authentication:** Not required (public endpoint for load balancer health probes)

**Response:**

```json
{
  "status": "HEALTHY",
  "checks": {
    "database": {
      "status": "HEALTHY",
      "details": {
        "engine": "postgresql",
        "pool_size": 10,
        "active_connections": 3
      }
    },
    "blobstore": {
      "status": "HEALTHY",
      "details": {
        "type": "s3",
        "bucket": "nexus-artifacts",
        "accessible": true
      }
    },
    "scheduler": {
      "status": "HEALTHY",
      "details": {
        "running": true,
        "pending_jobs": 5
      }
    },
    "search": {
      "status": "HEALTHY",
      "details": {
        "engine": "whoosh",
        "index_size": 1024,
        "documents": 5000
      }
    }
  },
  "timestamp": "2026-02-24T10:30:00Z"
}
```

**Status Values:**

| Status | HTTP Code | Description |
|--------|-----------|-------------|
| `HEALTHY` | 200 | All components operational |
| `DEGRADED` | 200 | Some non-critical components unavailable |
| `UNHEALTHY` | 503 | Critical components unavailable |

**Component Checks:**

| Component | Description | Critical |
|-----------|-------------|----------|
| **Database** | Verify SQLAlchemy engine connectivity and connection pool status | Yes |
| **BlobStore** | Verify file system access or S3 bucket connectivity | Yes |
| **Scheduler** | Verify APScheduler is running and responsive | No |
| **Search** | Verify Whoosh/Elasticsearch index accessibility | No |

- A component marked as **Critical** will cause the overall status to be `UNHEALTHY` if it fails.
- A non-critical component failure results in a `DEGRADED` overall status.

### GET /service/rest/v1/status

Returns a simple status indicator for basic availability checks.

**Authentication:** Not required

**Response:** `200 OK`

```json
{
  "status": "AVAILABLE",
  "startup_phase": "SERVICES",
  "edition": "OSS"
}
```

**Status Values:**

| Status | Description |
|--------|-------------|
| `AVAILABLE` | Application is fully started and serving requests |
| `STARTING` | Application is in a startup phase (not yet ready) |
| `UNAVAILABLE` | Application is shutting down or in a failed state |

**Startup Phases:**

The application progresses through six startup phases in strict order. The `startup_phase` field indicates the last completed phase:

| Phase | Description |
|-------|-------------|
| `KERNEL` | Runtime environment verified |
| `SCHEMAS` | Database migrations applied |
| `STORAGE` | BlobStore backends initialized |
| `SECURITY` | Authentication chain and RBAC configured |
| `CAPABILITIES` | Format handlers and plugins registered |
| `SERVICES` | Scheduler and health monitoring started (fully operational) |

---

## Scheduled Task Management (F-402)

Task management provides CRUD operations for scheduled tasks with cron-style scheduling. The scheduler is backed by APScheduler 3.11.2 with `SQLAlchemyJobStore` for persistent job storage, replacing the original Quartz 2.3.2 implementation.

### GET /service/rest/v1/admin/tasks

List all configured task definitions.

**Authentication:** Required — `nx-admin` role or `tasks-read` privilege

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `type` | string | — | Filter by task type |
| `enabled` | boolean | — | Filter by enabled status |

**Response:** `200 OK`

```json
{
  "items": [
    {
      "id": "cleanup-maven-releases",
      "type": "repository.cleanup",
      "name": "Cleanup Maven Releases",
      "schedule": "0 0 3 * * ?",
      "enabled": true,
      "properties": {
        "repository_name": "maven-releases",
        "policy_name": "cleanup-old-releases"
      },
      "last_run": "2026-02-24T03:00:00Z",
      "last_status": "SUCCESS",
      "next_run": "2026-02-25T03:00:00Z",
      "created_at": "2026-01-15T08:00:00Z",
      "updated_at": "2026-02-20T12:00:00Z"
    }
  ]
}
```

### POST /service/rest/v1/admin/tasks

Create a new scheduled task.

**Authentication:** Required — `nx-admin` role or `tasks-create` privilege

**Request Body:**

```json
{
  "type": "repository.cleanup",
  "name": "Cleanup Docker Snapshots",
  "schedule": "0 0 4 * * ?",
  "enabled": true,
  "properties": {
    "repository_name": "docker-snapshots",
    "policy_name": "cleanup-snapshots"
  }
}
```

**Response:** `201 Created`

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "type": "repository.cleanup",
  "name": "Cleanup Docker Snapshots",
  "schedule": "0 0 4 * * ?",
  "enabled": true,
  "properties": {
    "repository_name": "docker-snapshots",
    "policy_name": "cleanup-snapshots"
  },
  "next_run": "2026-02-25T04:00:00Z",
  "created_at": "2026-02-24T10:30:00Z",
  "updated_at": "2026-02-24T10:30:00Z"
}
```

**Task Types:**

| Type | Description |
|------|-------------|
| `repository.cleanup` | Execute cleanup policy on repositories (F-204) |
| `blobstore.compact` | BlobStore compaction — reclaim soft-deleted space (F-203) |
| `blobstore.integrity` | BlobStore integrity verification — checksum validation (F-203) |
| `repository.rebuild-index` | Rebuild search index for repository |

**Schedule Format:**

Cron-style expressions using 6 fields:

```
second minute hour day_of_month month day_of_week
```

Examples:

| Expression | Meaning |
|-----------|---------|
| `0 0 3 * * ?` | Every day at 3:00 AM |
| `0 0 */6 * * ?` | Every 6 hours |
| `0 30 2 ? * MON-FRI` | Weekdays at 2:30 AM |
| `0 0 0 1 * ?` | First day of each month at midnight |

### GET /service/rest/v1/admin/tasks/{task_id}

Get a specific task definition.

**Authentication:** Required — `nx-admin` role or `tasks-read` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | string | Unique task identifier (UUID) |

**Response:** `200 OK` — Returns the full task definition object (same schema as list item).

**Error Responses:**

| HTTP Code | Condition |
|-----------|-----------|
| 404 | Task with specified `task_id` not found |

### PUT /service/rest/v1/admin/tasks/{task_id}

Update a task definition.

**Authentication:** Required — `nx-admin` role or `tasks-update` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | string | Unique task identifier (UUID) |

**Request Body:**

```json
{
  "name": "Cleanup Docker Snapshots (Updated)",
  "schedule": "0 0 2 * * ?",
  "enabled": false,
  "properties": {
    "repository_name": "docker-snapshots",
    "policy_name": "cleanup-snapshots-v2"
  }
}
```

**Response:** `200 OK` — Returns the updated task definition object.

**Error Responses:**

| HTTP Code | Condition |
|-----------|-----------|
| 404 | Task with specified `task_id` not found |
| 400 | Invalid request body or cron expression |

### DELETE /service/rest/v1/admin/tasks/{task_id}

Delete a task definition. This also removes any pending executions and execution history for the task.

**Authentication:** Required — `nx-admin` role or `tasks-delete` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | string | Unique task identifier (UUID) |

**Response:** `204 No Content`

**Error Responses:**

| HTTP Code | Condition |
|-----------|-----------|
| 404 | Task with specified `task_id` not found |
| 409 | Task is currently running and cannot be deleted |

### POST /service/rest/v1/admin/tasks/{task_id}/run

Trigger immediate execution of a task, regardless of its cron schedule.

**Authentication:** Required — `nx-admin` role or `tasks-run` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | string | Unique task identifier (UUID) |

**Response:** `200 OK`

```json
{
  "execution_id": "exec-a1b2c3d4",
  "task_id": "cleanup-maven-releases",
  "status": "RUNNING",
  "start_time": "2026-02-24T10:30:00Z"
}
```

**Error Responses:**

| HTTP Code | Condition |
|-----------|-----------|
| 404 | Task with specified `task_id` not found |
| 409 | Task is already running |

### GET /service/rest/v1/admin/tasks/{task_id}/executions

List execution history for a specific task.

**Authentication:** Required — `nx-admin` role or `tasks-read` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | string | Unique task identifier (UUID) |

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | integer | `1` | Page number (1-based) |
| `per_page` | integer | `25` | Items per page (max 100) |
| `status` | string | — | Filter by execution status |

**Response:** `200 OK`

```json
{
  "items": [
    {
      "id": "exec-a1b2c3d4",
      "task_id": "cleanup-maven-releases",
      "start_time": "2026-02-24T03:00:00Z",
      "end_time": "2026-02-24T03:15:22Z",
      "status": "SUCCESS",
      "result": {
        "components_deleted": 42,
        "space_reclaimed_bytes": 1073741824
      }
    },
    {
      "id": "exec-e5f6g7h8",
      "task_id": "cleanup-maven-releases",
      "start_time": "2026-02-23T03:00:00Z",
      "end_time": "2026-02-23T03:12:05Z",
      "status": "SUCCESS",
      "result": {
        "components_deleted": 18,
        "space_reclaimed_bytes": 536870912
      }
    }
  ],
  "page": 1,
  "per_page": 25,
  "total": 2
}
```

**Execution Statuses:**

| Status | Description |
|--------|-------------|
| `WAITING` | Scheduled but not yet started |
| `RUNNING` | Currently executing |
| `SUCCESS` | Completed successfully |
| `FAILED` | Completed with errors |
| `CANCELLED` | Cancelled by administrator |

---

## Support ZIP Generation (F-403)

The support ZIP endpoint generates a diagnostic bundle containing system information, configuration, logs, and metrics for troubleshooting. **All credential data is automatically redacted** before inclusion in the bundle to ensure that sensitive information is never exposed.

### GET /service/rest/v1/admin/support/supportzip

Generate and download a diagnostic support ZIP bundle.

**Authentication:** Required — `nx-admin` role

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `include_logs` | boolean | `true` | Include application logs |
| `include_metrics` | boolean | `true` | Include metrics snapshot |
| `include_config` | boolean | `true` | Include configuration (redacted) |
| `log_lines` | integer | `10000` | Maximum log lines to include |

**Response:** `200 OK`

- **Content-Type:** `application/zip`
- **Content-Disposition:** `attachment; filename="support-YYYY-MM-DD-HHmmss.zip"`

**ZIP Bundle Contents:**

| File | Description |
|------|-------------|
| `system-info.json` | Python version, OS details, memory usage, CPU count, disk usage |
| `configuration.json` | Application configuration with **all credentials redacted** |
| `thread-dump.txt` | Current thread information and stack traces |
| `metrics-snapshot.json` | Current Prometheus metrics values |
| `log/application.log` | Recent application log entries (limited by `log_lines`) |
| `database-info.json` | Database engine, schema version, connection pool stats, table sizes |
| `blobstore-info.json` | BlobStore type, configuration, disk/S3 usage statistics |
| `task-history.json` | Recent scheduled task execution history |

**Credential Redaction:**

The following fields are automatically replaced with `****` in the support bundle to comply with security requirements:

- Database passwords
- S3 secret access keys
- JWT secrets
- LDAP bind passwords
- API keys
- Certificate private keys
- OIDC client secrets
- SIEM authentication tokens
- SMTP passwords
- Encryption keys

> **Security Note:** The redaction engine scans all included configuration and environment data. Any key matching common credential patterns (e.g., `*_password`, `*_secret`, `*_key`, `*_token`) is automatically redacted regardless of context.

---

## System Configuration Management (F-404)

Hierarchical key-value configuration store with type validation and change notifications. Configuration changes emit `config_changed` blinker signals for audit logging and webhook notifications. The configuration system supports environment variable overrides for container-native deployments.

### GET /service/rest/v1/admin/system/config

List all system configuration entries.

**Authentication:** Required — `nx-admin` role or `settings-read` privilege

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prefix` | string | — | Filter by key prefix (e.g., `nexus.proxy`) |
| `type` | string | — | Filter by value type |

**Response:** `200 OK`

```json
{
  "items": [
    {
      "key": "nexus.proxy.timeout",
      "value": "60",
      "type": "integer",
      "description": "Proxy repository upstream fetch timeout in seconds",
      "updated_at": "2026-02-20T12:00:00Z",
      "updated_by": "admin"
    },
    {
      "key": "nexus.cleanup.retention_days",
      "value": "30",
      "type": "integer",
      "description": "Default retention period for cleanup policies",
      "updated_at": "2026-01-15T08:00:00Z",
      "updated_by": "system"
    },
    {
      "key": "nexus.security.anonymous_access",
      "value": "false",
      "type": "boolean",
      "description": "Allow anonymous read access to public repositories",
      "updated_at": "2026-02-01T10:00:00Z",
      "updated_by": "admin"
    }
  ]
}
```

### GET /service/rest/v1/admin/system/config/{key}

Get a specific configuration entry.

**Authentication:** Required — `nx-admin` role or `settings-read` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | string | Configuration key in dot-notation (e.g., `nexus.proxy.timeout`) |

**Response:** `200 OK`

```json
{
  "key": "nexus.proxy.timeout",
  "value": "60",
  "type": "integer",
  "description": "Proxy repository upstream fetch timeout in seconds",
  "updated_at": "2026-02-20T12:00:00Z",
  "updated_by": "admin"
}
```

**Error Responses:**

| HTTP Code | Condition |
|-----------|-----------|
| 404 | Configuration key not found |

### PUT /service/rest/v1/admin/system/config/{key}

Create or update a configuration entry. Emits a `config_changed` signal upon successful update for audit logging and webhook notifications.

**Authentication:** Required — `nx-admin` role or `settings-update` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | string | Configuration key in dot-notation |

**Request Body:**

```json
{
  "value": "90",
  "description": "Updated timeout for slow upstream registries"
}
```

**Response:** `200 OK`

```json
{
  "key": "nexus.proxy.timeout",
  "value": "90",
  "type": "integer",
  "description": "Updated timeout for slow upstream registries",
  "updated_at": "2026-02-24T10:30:00Z",
  "updated_by": "admin"
}
```

**Error Responses:**

| HTTP Code | Condition |
|-----------|-----------|
| 400 | Value does not match the expected type for this key |
| 403 | Insufficient privileges to modify configuration |

### DELETE /service/rest/v1/admin/system/config/{key}

Reset a configuration entry to its default value. If no default is defined, the entry is removed. Emits a `config_changed` signal upon successful reset.

**Authentication:** Required — `nx-admin` role or `settings-update` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | string | Configuration key in dot-notation |

**Response:** `200 OK` — Returns the configuration entry with its default value restored.

**Error Responses:**

| HTTP Code | Condition |
|-----------|-----------|
| 404 | Configuration key not found |

**Configuration Types:**

| Type | Validation | Example Key |
|------|-----------|-------------|
| `string` | Non-empty string | `nexus.ui.title` |
| `integer` | Valid integer value | `nexus.proxy.timeout` |
| `boolean` | `true` or `false` | `nexus.security.anonymous_access` |
| `json` | Valid JSON object or array | `nexus.blobstore.quotas` |

**Common Configuration Keys:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `nexus.proxy.timeout` | integer | `60` | Upstream proxy fetch timeout (seconds) |
| `nexus.proxy.retries` | integer | `3` | Number of retry attempts for upstream fetches |
| `nexus.cleanup.retention_days` | integer | `30` | Default cleanup retention period (days) |
| `nexus.security.anonymous_access` | boolean | `false` | Allow anonymous read access |
| `nexus.security.session_timeout` | integer | `1800` | Session timeout (seconds) |
| `nexus.ui.title` | string | `Nexus Repository Manager` | Browser window title |
| `nexus.blobstore.quotas` | json | `{}` | BlobStore quota configuration |

---

## Cleanup Policy Management

Cleanup policies define format-specific retention rules for scheduled repository cleanup (related to F-204). Policies are administered through the admin API and executed by scheduled tasks (F-402).

### GET /service/rest/v1/admin/cleanup-policies

List all cleanup policies.

**Authentication:** Required — `nx-admin` role

**Response:** `200 OK`

```json
{
  "items": [
    {
      "id": "cleanup-snapshots",
      "name": "Remove Old Snapshots",
      "format": "maven2",
      "criteria": {
        "last_downloaded_before": 30,
        "is_prerelease": true,
        "regex": ".*-SNAPSHOT"
      },
      "notes": "Remove Maven snapshots not downloaded in 30 days"
    },
    {
      "id": "cleanup-docker-old",
      "name": "Remove Old Docker Tags",
      "format": "docker",
      "criteria": {
        "last_downloaded_before": 90,
        "last_blob_updated_before": 90
      },
      "notes": "Remove Docker images not accessed in 90 days"
    }
  ]
}
```

### POST /service/rest/v1/admin/cleanup-policies

Create a new cleanup policy.

**Authentication:** Required — `nx-admin` role

**Request Body:**

```json
{
  "name": "Remove Old npm Packages",
  "format": "npm",
  "criteria": {
    "last_downloaded_before": 60,
    "is_prerelease": true
  },
  "notes": "Remove pre-release npm packages not downloaded in 60 days"
}
```

**Response:** `201 Created` — Returns the created cleanup policy with its generated `id`.

### GET /service/rest/v1/admin/cleanup-policies/{id}

Get a specific cleanup policy.

**Authentication:** Required — `nx-admin` role

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Cleanup policy identifier |

**Response:** `200 OK` — Returns the full cleanup policy object.

### PUT /service/rest/v1/admin/cleanup-policies/{id}

Update an existing cleanup policy.

**Authentication:** Required — `nx-admin` role

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Cleanup policy identifier |

**Request Body:**

```json
{
  "name": "Remove Old npm Packages (Updated)",
  "criteria": {
    "last_downloaded_before": 45,
    "is_prerelease": true,
    "regex": ".*-beta.*"
  },
  "notes": "Updated to include beta pattern matching"
}
```

**Response:** `200 OK` — Returns the updated cleanup policy object.

### DELETE /service/rest/v1/admin/cleanup-policies/{id}

Delete a cleanup policy. Tasks referencing this policy will fail on their next execution.

**Authentication:** Required — `nx-admin` role

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Cleanup policy identifier |

**Response:** `204 No Content`

**Error Responses:**

| HTTP Code | Condition |
|-----------|-----------|
| 404 | Cleanup policy not found |

**Cleanup Criteria:**

| Criterion | Type | Description |
|-----------|------|-------------|
| `last_downloaded_before` | integer | Days since the component was last downloaded |
| `last_blob_updated_before` | integer | Days since the blob content was last updated |
| `is_prerelease` | boolean | Match pre-release/snapshot components only |
| `regex` | string | Regex pattern matched against component name/version |

**Supported Formats:**

Cleanup policies can target any supported repository format:

| Format | Pre-release Detection |
|--------|----------------------|
| `maven2` | `-SNAPSHOT` suffix |
| `npm` | SemVer pre-release tag |
| `docker` | Tag pattern matching |
| `nuget` | SemVer pre-release tag |
| `pypi` | PEP 440 pre-release identifiers |
| `apt` | Debian version epoch |
| `raw` | Not applicable (no version semantics) |

---

## Error Responses

All administration API endpoints return errors in a consistent JSON format:

```json
{
  "error": {
    "type": "ClientError",
    "message": "Task with id 'nonexistent-id' not found",
    "status": 404
  }
}
```

**Common Error Types:**

| Type | HTTP Code | Description |
|------|-----------|-------------|
| `ClientError` | 400 | Invalid request body, parameters, or cron expression |
| `AuthenticationError` | 401 | Missing or invalid authentication credentials |
| `AuthorizationError` | 403 | Insufficient privileges for the requested operation |
| `NotFoundError` | 404 | Requested resource does not exist |
| `ConflictError` | 409 | Resource state conflict (e.g., deleting a running task) |
| `SystemError` | 500 | Internal server error |

**Authentication Errors:**

Unauthenticated requests to protected endpoints receive `401 Unauthorized` with a `WWW-Authenticate` header. Per security rules, unauthenticated requests never receive `403 Forbidden` — authentication is always evaluated before authorization.

---

## Data Models

The following SQLAlchemy models back the administration API endpoints. These are defined in `src/models/admin.py`.

### TaskDefinition

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary Key | Unique task identifier |
| `type` | string | Not Null | Task type (`repository.cleanup`, `blobstore.compact`, `blobstore.integrity`, `repository.rebuild-index`) |
| `name` | string | Not Null | Human-readable task name |
| `schedule` | string | Not Null | Cron expression for scheduling (6-field format) |
| `enabled` | boolean | Not Null, Default `true` | Whether the task is active |
| `properties` | JSON | Nullable | Task-specific configuration parameters |
| `created_at` | datetime | Not Null, Auto | Creation timestamp (UTC) |
| `updated_at` | datetime | Not Null, Auto | Last modification timestamp (UTC) |

### TaskExecution

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary Key | Unique execution identifier |
| `task_id` | string (UUID) | Foreign Key → TaskDefinition.id | Reference to the parent task definition |
| `start_time` | datetime | Not Null | Execution start time (UTC) |
| `end_time` | datetime | Nullable | Execution end time (null if currently running) |
| `status` | string | Not Null | Execution status: `WAITING`, `RUNNING`, `SUCCESS`, `FAILED`, `CANCELLED` |
| `result` | JSON | Nullable | Execution result details (task-type-specific) |

### SystemConfig

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary Key | Unique configuration entry identifier |
| `key` | string | Unique, Not Null | Configuration key in dot-notation (e.g., `nexus.proxy.timeout`) |
| `value` | string | Not Null | Configuration value (stored as string, parsed by type) |
| `type` | string | Not Null | Value type: `string`, `integer`, `boolean`, `json` |
| `description` | string | Nullable | Human-readable description of the setting |

### CleanupPolicy

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary Key | Unique cleanup policy identifier |
| `name` | string | Not Null | Human-readable policy name |
| `format` | string | Not Null | Target repository format (`maven2`, `npm`, `docker`, `nuget`, `pypi`, `apt`, `raw`) |
| `criteria` | JSON | Not Null | Retention rule criteria |
| `notes` | string | Nullable | Administrative notes about the policy |

---

## Related Documentation

- [API Index](README.md) — Overview of all available API endpoints
- [Repository Management API](repositories.md) — Repository CRUD, format-native protocols, search, and browse
- [Security Management API](security.md) — Users, roles, privileges, certificates, and authentication
- [Architecture Overview](../architecture.md) — System architecture, startup sequence, and scheduler internals
- [Configuration Reference](../configuration.md) — YAML configuration file format and all available settings
