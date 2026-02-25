# Configuration Reference

The Nexus Repository Manager uses a **three-layer configuration system** that allows
settings to be defined once and overridden progressively for each deployment
environment. From lowest to highest priority:

1. **Default YAML configuration** (`config/default.yaml`) — base values that apply
   in every environment.
2. **Environment-specific YAML overrides** (`config/development.yaml`,
   `config/production.yaml`, `config/testing.yaml`) — values that differ between
   deployment environments.
3. **Environment variable overrides** — highest priority; always win over YAML
   settings, enabling runtime configuration for containers and CI/CD pipelines.

The configuration loader in `src/config.py` merges these layers at application
startup. When the Flask application factory (`create_app()`) is called, it reads the
default YAML, deep-merges the environment-specific YAML on top, and finally applies
any matching environment variables. This design follows the
[Twelve-Factor App](https://12factor.net/config) methodology, ensuring that every
setting that varies between deployments can be controlled via the process
environment.

---

## Configuration File Locations

The following files participate in the configuration system and are loaded in the
order shown below. Each successive file overrides values from previous files:

| Priority | File | Description |
|----------|------|-------------|
| 1 (lowest) | `config/default.yaml` | Base configuration defaults applied in all environments. Every configuration key used by the application is defined here with a sensible standalone-optimized default. |
| 2 | `config/development.yaml` | Development overrides — SQLite database, debug mode enabled, verbose `DEBUG`-level logging in human-readable text format, extended JWT expiration. |
| 3 | `config/production.yaml` | Production settings — PostgreSQL database, S3 BlobStore, Elasticsearch search backend, stricter pool tuning, JSON structured logging. Credentials are intentionally omitted and must be supplied via environment variables. |
| 4 | `config/testing.yaml` | Test settings — in-memory SQLite (`sqlite://`), temporary BlobStore path, disabled scheduler, disabled metrics, short JWT expiration. Optimized for fast, isolated `pytest` runs. |
| 5 (highest) | `.env` | Environment variable overrides loaded via [python-dotenv](https://pypi.org/project/python-dotenv/). Copy `.env.example` to `.env` and edit for your local environment. Variables set directly in the process environment (e.g., Kubernetes, Docker, CI/CD) take precedence over `.env`. |

> **Tip:** Run the application with `FLASK_ENV=development`, `FLASK_ENV=production`,
> or `FLASK_ENV=testing` to select the corresponding YAML override file.

---

## Application Settings

### Core Application

These settings control the Flask application runtime behavior.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `app.secret_key` | `SECRET_KEY` | string | `change-me-to-a-random-secret-key` | Cryptographic key for Flask session cookie signing and CSRF protection. **Must** be changed to a unique random value in every deployment. |
| `app.debug` | `DEBUG` | boolean | `false` | Enable Flask debug mode with interactive debugger and auto-reload. **Never** enable in production. |
| `app.testing` | `FLASK_ENV` | boolean | `false` | Enable Flask testing mode. Set automatically when `FLASK_ENV=testing`. |
| — | `FLASK_APP` | string | `src.app:create_app` | Application factory entry point for the Flask CLI and Gunicorn. |
| — | `FLASK_ENV` | string | `production` | Runtime environment name (`development` / `production` / `testing`). Controls which YAML override file is loaded. |

### Database (DataStore)

The DataStore holds all relational metadata — repositories, components, assets, users,
roles, privileges, content selectors, tasks, system configuration, and audit events.
It implements the metadata half of the **dual-persistence architecture** (see
[Architecture Overview](architecture.md)). Standalone deployments use SQLite;
clustered and production deployments use PostgreSQL.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `database.url` | `DATABASE_URL` | string | `sqlite:///nexus.db` | SQLAlchemy database connection URL. Use `sqlite:///path.db` for standalone or `postgresql://user:pass@host:5432/nexus` for production. |
| `database.echo` | `DATABASE_ECHO` | boolean | `false` | Log all SQL statements to the application logger. Useful for development debugging. |
| `database.track_modifications` | — | boolean | `false` | Flask-SQLAlchemy modification tracking. Kept `false` to avoid unnecessary overhead. |
| `database.engine_options.pool_size` | `DATABASE_POOL_SIZE` | integer | `10` | Connection pool size (PostgreSQL only; ignored for SQLite). Production default: `20`. |
| `database.engine_options.max_overflow` | `DATABASE_MAX_OVERFLOW` | integer | `20` | Maximum overflow connections beyond `pool_size`. Production default: `40`. |
| `database.engine_options.pool_timeout` | `DATABASE_POOL_TIMEOUT` | integer | `30` | Seconds to wait for a connection from the pool before raising a timeout error. |
| `database.engine_options.pool_recycle` | — | integer | `1800` | Seconds after which a connection is recycled. Prevents stale connections behind load balancers. |
| `database.engine_options.pool_pre_ping` | — | boolean | `true` | Validate connections with a lightweight ping before checkout. Handles database failovers gracefully. |

> **Note:** SQLite does not use connection pooling. When `DATABASE_URL` points to a
> SQLite database, pool settings (`pool_size`, `max_overflow`) are ignored and
> SQLAlchemy uses `NullPool` instead.

### BlobStore (Binary Storage)

The BlobStore holds all binary artifact content, separate from metadata. Two backends
are supported: **file** (local/shared filesystem) and **s3** (Amazon S3).

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `storage.blobstore_type` | `BLOBSTORE_TYPE` | string | `file` | BlobStore backend type: `file` for filesystem storage (F-201) or `s3` for Amazon S3 storage (F-202). |
| `storage.blobstore_path` | `BLOBSTORE_PATH` | string | `./data/blobs` | Base directory for the file BlobStore. Used only when `blobstore_type=file`. Supports absolute or relative paths. For clustered deployments, point to a shared filesystem (NFS/EFS). |

### S3 BlobStore Settings

These settings apply only when `BLOBSTORE_TYPE=s3`. Authentication follows the
standard AWS credential resolution order: IAM role → instance profile → explicit
access keys.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `s3.bucket` | `S3_BUCKET` | string | *(empty)* | S3 bucket name for artifact storage. **Required** when using S3 BlobStore. |
| `s3.region` | `S3_REGION` | string | `us-east-1` | AWS region where the S3 bucket resides. |
| `s3.access_key_id` | `S3_ACCESS_KEY_ID` | string | *(empty)* | AWS access key ID. Leave empty to use IAM roles or instance profiles (recommended for production). |
| `s3.secret_access_key` | `S3_SECRET_ACCESS_KEY` | string | *(empty)* | AWS secret access key. Leave empty to use IAM roles or instance profiles. |
| `s3.encryption` | `S3_ENCRYPTION` | string | `SSE-S3` | Server-side encryption mode: `SSE-S3` (Amazon-managed keys) or `SSE-KMS` (customer-managed KMS key). |
| `s3.kms_key_arn` | `S3_KMS_KEY_ARN` | string | *(empty)* | AWS KMS key ARN for SSE-KMS encryption. Required when `s3.encryption=SSE-KMS`. |
| — | `S3_PREFIX` | string | *(empty)* | Optional key prefix applied to all objects stored in the bucket. |

### JWT Authentication

JSON Web Tokens are used for API session management and cookie-based authentication,
replacing the Java-JWT (auth0) 4.4.0 library from the original implementation.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `jwt.secret` | `JWT_SECRET` | string | `change-me-to-a-random-jwt-secret` | Signing key for HS256, or path to a PEM-encoded private key for RS256. **Must** be changed to a unique random value in every deployment. |
| `jwt.algorithm` | `JWT_ALGORITHM` | string | `HS256` | JWT signing algorithm: `HS256` (symmetric HMAC) or `RS256` (asymmetric RSA). |
| `jwt.expiration_hours` | `JWT_EXPIRATION_HOURS` | integer | `24` | Token lifetime in hours before re-authentication is required. Development default: `72`. |

### LDAP Configuration

Configures the LDAP/Active Directory authentication realm. LDAP is part of the
multi-realm authentication chain: **Local → Bearer → JWT → LDAP → SAML/OIDC**. The
LDAP realm is disabled when `LDAP_URL` is empty.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `ldap.url` | `LDAP_URL` | string | *(empty)* | LDAP server URI (e.g., `ldap://ldap.example.com:389` or `ldaps://ldap.example.com:636`). Empty disables LDAP. |
| `ldap.base_dn` | `LDAP_BASE_DN` | string | *(empty)* | Base distinguished name for user searches (e.g., `ou=people,dc=example,dc=com`). |
| `ldap.user_search_filter` | `LDAP_USER_SEARCH_FILTER` | string | `(uid={0})` | LDAP filter template. `{0}` is replaced with the username at runtime. |
| `ldap.bind_dn` | `LDAP_BIND_DN` | string | *(empty)* | Distinguished name for the service account used to search for users. |
| `ldap.bind_password` | `LDAP_BIND_PASSWORD` | string | *(empty)* | Password for the LDAP service account bind. Use secret management in production. |
| `ldap.use_tls` | `LDAP_USE_TLS` | boolean | `true` | Enforce TLS for LDAP connections. Disabled by default in development and testing. |
| — | `LDAP_GROUP_BASE_DN` | string | *(empty)* | Base DN for group searches used in LDAP-to-role mapping. |
| — | `LDAP_GROUP_SEARCH_FILTER` | string | *(empty)* | LDAP filter template for resolving group memberships. |

### SAML / OIDC Configuration

Configures SAML and OpenID Connect (OIDC) SSO federation. These are the final realms
in the authentication chain. Both are disabled when their respective URLs or client IDs
are empty.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `saml.idp_metadata_url` | `SAML_IDP_METADATA_URL` | string | *(empty)* | URL of the SAML Identity Provider metadata XML. |
| `oidc.client_id` | `OIDC_CLIENT_ID` | string | *(empty)* | OAuth 2.0 client identifier for the OIDC provider. |
| `oidc.client_secret` | `OIDC_CLIENT_SECRET` | string | *(empty)* | OAuth 2.0 client secret. **Never** store in YAML files; set via environment variable or secret manager. |
| `oidc.discovery_url` | `OIDC_DISCOVERY_URL` | string | *(empty)* | OIDC well-known configuration endpoint (e.g., `https://idp.example.com/.well-known/openid-configuration`). |

### Scheduler

APScheduler 3.11.2 with `BackgroundScheduler` and `SQLAlchemyJobStore` replaces
Quartz 2.3.2 from the original Java implementation. It manages cleanup policies
(F-204), BlobStore maintenance (F-203), proxy cache invalidation, and periodic health
checks.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `scheduler.enabled` | `SCHEDULER_ENABLED` | boolean | `true` | Enable or disable the background task scheduler. Disabled in the testing environment to prevent timing interference. |
| `scheduler.jobstore_url` | `SCHEDULER_JOBSTORE_URL` | string | *(uses main DB)* | Database URL for the persistent job store. Defaults to `DATABASE_URL` when empty. A separate URL is recommended for clustered deployments to prevent duplicate task execution. |

**Default Scheduled Tasks:**

The following tasks are registered automatically when the scheduler starts. Their
schedules can be managed at runtime via the REST API
(`/service/rest/v1/admin/tasks/**`).

| Task | YAML Key | Default Cron | Description |
|------|----------|-------------|-------------|
| Cleanup | `scheduler.default_tasks.cleanup.cron` | `0 0 1 * *` | Run cleanup policies on all repositories. |
| Compaction | `scheduler.default_tasks.compaction.cron` | `0 0 2 * *` | Compact BlobStore to reclaim soft-deleted space. |
| Integrity Check | `scheduler.default_tasks.integrity_check.cron` | `0 0 3 * 0` | Verify BlobStore integrity (weekly). |

### Search

Content indexing and full-text search (F-103) for component discovery. Two backends
are supported: **Whoosh** (pure-Python, standalone) and **Elasticsearch** (production
scale).

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `search.backend` | `SEARCH_BACKEND` | string | `whoosh` | Search engine backend: `whoosh` for standalone deployments or `elasticsearch` for production. |
| `search.whoosh_index_path` | `WHOOSH_INDEX_PATH` | string | `./data/search` | Directory for Whoosh index files. Used only when `search.backend=whoosh`. |
| `search.elasticsearch_url` | `ELASTICSEARCH_URL` | string | `http://localhost:9200` | Elasticsearch cluster URL. Used only when `search.backend=elasticsearch`. |

### Metrics and Observability

Prometheus-compatible metrics are exposed at the `/service/metrics` endpoint,
replacing Dropwizard Metrics 4.2.25 and the Prometheus 0.16.0 Java client.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `metrics.enabled` | `METRICS_ENABLED` | boolean | `true` | Enable or disable Prometheus metric collection. Disabled in the testing environment to reduce overhead. |
| `metrics.endpoint` | — | string | `/service/metrics` | URL path where Prometheus metrics are served in text exposition format. |

### Logging

Structured JSON logging compatible with external SIEM ingestion. Log entries include
timestamp, level, module, message, and contextual attributes (`request_id`, `user_id`
where applicable).

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `logging.level` | `LOG_LEVEL` | string | `INFO` | Minimum log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. Development default: `DEBUG`. |
| `logging.format` | `LOG_FORMAT` | string | `json` | Log output format: `json` for structured SIEM-compatible output or `text` for human-readable console output. Development default: `text`. |

### SIEM Integration

Audit log forwarding (F-303) to external Security Information and Event Management
systems. Disabled when `SIEM_ENDPOINT` is empty.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `siem.endpoint` | `SIEM_ENDPOINT` | string | *(empty)* | URL of the SIEM ingestion endpoint (e.g., `https://siem.example.com/api/events`). Empty disables forwarding. |
| `siem.token` | `SIEM_TOKEN` | string | *(empty)* | Authentication token for the SIEM endpoint. Use secret management in production. |

### Webhooks

Webhook event notifications (F-503) with HMAC-SHA256 payload signing. Individual
webhook configurations are managed at runtime via the REST API.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| — | `WEBHOOK_SECRET` | string | *(empty)* | Default HMAC-SHA256 signing key for webhook payloads. Individual webhooks can override this with their own secret. |
| — | `WEBHOOK_TIMEOUT` | integer | `30` | HTTP request timeout in seconds for webhook delivery attempts. |

### Scripting Engine

Server-side scripting engine (F-502) for sandboxed Python script execution with
injected API context.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| — | `SCRIPTING_ENABLED` | boolean | `true` | Enable or disable the server-side scripting engine. |

### Server Settings

Bind address and port for the Flask development server. In production, these are
overridden by Gunicorn settings (see below).

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `server.host` | — | string | `0.0.0.0` | Listen address for the Flask development server. |
| `server.port` | — | integer | `8081` | Listen port. Matches the standard Nexus Repository port. |

### SSL / TLS

SSL/TLS certificate lifecycle management (F-302). In most production deployments, TLS
is terminated at a reverse proxy (Nginx, HAProxy, AWS ALB). Direct TLS termination at
Gunicorn is supported for standalone deployments.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `ssl.cert_path` | `SSL_CERT_PATH` | string | *(empty)* | Path to a PEM-encoded SSL certificate file. Empty disables application-level TLS. |
| `ssl.key_path` | `SSL_KEY_PATH` | string | *(empty)* | Path to a PEM-encoded SSL private key file. |
| `ssl.truststore_path` | `TRUSTSTORE_PATH` | string | *(empty)* | Path to a custom CA trust store (PEM bundle) for verifying upstream proxy repository connections and LDAP server certificates. |

### Repository Defaults

Default settings applied to newly created repositories when not explicitly overridden
in the repository configuration.

| YAML Key | Env Variable | Type | Default | Description |
|----------|-------------|------|---------|-------------|
| `repository_defaults.proxy.content_max_age` | — | integer | `1440` | Maximum age in minutes for cached upstream content before re-validation (24 hours). |
| `repository_defaults.proxy.metadata_max_age` | — | integer | `1440` | Maximum age in minutes for cached upstream metadata before re-validation (24 hours). |
| `repository_defaults.proxy.negative_cache_enabled` | — | boolean | `true` | Cache "not found" responses from upstream to reduce unnecessary requests. |
| `repository_defaults.proxy.negative_cache_ttl` | — | integer | `1440` | Time-to-live in minutes for negative cache entries (24 hours). |
| `repository_defaults.cleanup.retention_days` | — | integer | `30` | Default artifact retention period in days for cleanup policies. |

### Gunicorn (Production WSGI Server)

Gunicorn 23.0.0 replaces Jetty 12.0.5 as the production HTTP server. All settings
below can be overridden via environment variables and correspond to options in
`gunicorn.conf.py`. Run the server with:

```bash
gunicorn -c gunicorn.conf.py "src.app:create_app()"
```

| Env Variable | Type | Default | Description |
|-------------|------|---------|-------------|
| `GUNICORN_BIND` | string | `0.0.0.0:8081` | Listen address and port. |
| `GUNICORN_BACKLOG` | integer | `2048` | Maximum pending connections in the socket backlog queue. |
| `GUNICORN_WORKERS` | integer | `min(4, cpu*2+1)` | Number of worker processes. Match to container CPU allocation. |
| `GUNICORN_WORKER_CLASS` | string | `sync` | Worker type: `sync`, `gthread`, `gevent`, or `eventlet`. |
| `GUNICORN_WORKER_CONNECTIONS` | integer | `1000` | Maximum simultaneous clients per worker (async workers only). |
| `GUNICORN_THREADS` | integer | `1` | Threads per worker (meaningful only with the `gthread` worker class). |
| `GUNICORN_TIMEOUT` | integer | `120` | Worker silent timeout in seconds. Set high enough for large artifact transfers. |
| `GUNICORN_GRACEFUL_TIMEOUT` | integer | `30` | Graceful worker shutdown timeout in seconds. Allows in-flight requests to complete during rolling deployments. |
| `GUNICORN_KEEPALIVE` | integer | `5` | Keep-alive duration in seconds for idle connections. |
| `GUNICORN_ACCESS_LOG` | string | `-` (stdout) | Access log destination. Use `-` for stdout (container-friendly) or a file path. |
| `GUNICORN_ERROR_LOG` | string | `-` (stderr) | Error log destination. Use `-` for stderr or a file path. |
| `GUNICORN_LOG_LEVEL` | string | `info` | Gunicorn internal log level: `debug`, `info`, `warning`, `error`, `critical`. |
| `GUNICORN_LIMIT_REQUEST_LINE` | integer | `8190` | Maximum HTTP request line size in bytes. |
| `GUNICORN_LIMIT_REQUEST_FIELDS` | integer | `100` | Maximum number of HTTP header fields per request. |
| `GUNICORN_LIMIT_REQUEST_FIELD_SIZE` | integer | `8190` | Maximum size of an individual HTTP header field in bytes. |

---

## Deployment Model Configurations

The application supports three deployment models. Each model uses a different
combination of database, BlobStore, and search backends.

### Standalone Deployment (SQLite + File BlobStore)

Minimal configuration for single-server deployments with zero external dependencies.

```bash
# .env — Standalone
FLASK_ENV=production
SECRET_KEY=<generate-a-random-64-char-string>
JWT_SECRET=<generate-a-random-64-char-string>

DATABASE_URL=sqlite:///nexus.db
BLOBSTORE_TYPE=file
BLOBSTORE_PATH=./data/blobs
SEARCH_BACKEND=whoosh
WHOOSH_INDEX_PATH=./data/search
```

**Characteristics:**

- Database: SQLite file on local disk — no database server required.
- BlobStore: File-based content-addressable storage on local filesystem.
- Search: Whoosh pure-Python search engine — no Elasticsearch required.
- Best for: evaluation, small teams, single-server installations.
- Limitations: no horizontal scaling, single-writer database, no shared storage.

> See [Standalone Deployment Guide](deployment/standalone.md) for full setup
> instructions.

### Clustered Deployment (PostgreSQL + Shared Filesystem)

Multi-node deployment with a shared relational database and a shared network
filesystem for artifact storage.

```bash
# .env — Clustered
FLASK_ENV=production
SECRET_KEY=<generate-a-random-64-char-string>
JWT_SECRET=<generate-a-random-64-char-string>

DATABASE_URL=postgresql://nexus:password@db-host:5432/nexus
DATABASE_POOL_SIZE=20
DATABASE_MAX_OVERFLOW=40

BLOBSTORE_TYPE=file
BLOBSTORE_PATH=/shared/blobs

SEARCH_BACKEND=elasticsearch
ELASTICSEARCH_URL=http://es-host:9200

SCHEDULER_JOBSTORE_URL=postgresql://nexus:password@db-host:5432/nexus
```

**Characteristics:**

- Database: PostgreSQL with connection pooling for concurrent access.
- BlobStore: File-based storage on NFS or a shared filesystem (e.g., AWS EFS).
- Search: Elasticsearch 8.x cluster for production-scale indexing.
- Scheduler: Persistent job store backed by PostgreSQL prevents duplicate task
  execution across cluster nodes.
- Best for: medium to large teams, high availability, horizontal scaling.

> See [Clustered Deployment Guide](deployment/clustered.md) for full setup
> instructions.

### Container-Native Deployment (PostgreSQL + S3)

Cloud-optimized deployment using managed database and object storage services.

```bash
# .env — Container-Native
FLASK_ENV=production
SECRET_KEY=<generate-a-random-64-char-string>
JWT_SECRET=<generate-a-random-64-char-string>

DATABASE_URL=postgresql://nexus:password@rds-host:5432/nexus
DATABASE_POOL_SIZE=20
DATABASE_MAX_OVERFLOW=40

BLOBSTORE_TYPE=s3
S3_BUCKET=nexus-artifacts
S3_REGION=us-east-1
S3_ENCRYPTION=SSE-KMS
S3_KMS_KEY_ARN=arn:aws:kms:us-east-1:123456789:key/abcd-1234

SEARCH_BACKEND=elasticsearch
ELASTICSEARCH_URL=http://es-host:9200

GUNICORN_WORKERS=2
GUNICORN_BIND=0.0.0.0:8081
```

**Characteristics:**

- Database: Amazon RDS for PostgreSQL (or equivalent managed PostgreSQL).
- BlobStore: Amazon S3 with server-side encryption (SSE-S3 or SSE-KMS).
- Search: Elasticsearch 8.x (Amazon OpenSearch Service or self-managed).
- Authentication: IAM roles or instance profiles for S3 access (recommended over
  explicit access keys).
- Workers: Set `GUNICORN_WORKERS` to match the container CPU allocation.
- Best for: Kubernetes, Docker Swarm, AWS ECS, cloud-native infrastructure.

> See [Container-Native Deployment Guide](deployment/container.md) for full setup
> instructions including Dockerfile and `docker-compose.yml` reference.

---

## Security Considerations

> **⚠ Critical:** The following settings contain sensitive credentials and must be
> handled with care.

### Required Secret Rotation

| Setting | Env Variable | Risk if Unchanged |
|---------|-------------|-------------------|
| Flask Secret Key | `SECRET_KEY` | Session cookies can be forged, enabling unauthorized access. |
| JWT Signing Secret | `JWT_SECRET` | API tokens can be forged, bypassing authentication entirely. |

Both `SECRET_KEY` and `JWT_SECRET` ship with placeholder values
(`change-me-to-a-random-secret-key` / `change-me-to-a-random-jwt-secret`) in the
default configuration. **These must be changed to cryptographically random values
before any production deployment.** Generate secure values with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

### Credential Environment Variables

The following environment variables carry sensitive credentials and should **never** be
stored in plain-text YAML files or committed to version control:

- `SECRET_KEY` — Flask session signing key
- `JWT_SECRET` — JWT token signing key
- `DATABASE_URL` — May contain database username and password
- `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` — AWS credentials
- `LDAP_BIND_PASSWORD` — LDAP service account password
- `OIDC_CLIENT_SECRET` — OIDC client secret
- `SIEM_TOKEN` — SIEM ingestion authentication token
- `WEBHOOK_SECRET` — HMAC signing key for webhooks

Use a dedicated secret management system for production deployments:

- **Kubernetes:** `Secret` resources mounted as environment variables.
- **AWS:** Secrets Manager or Systems Manager Parameter Store.
- **HashiCorp Vault:** Dynamic secret injection.
- **Docker Swarm:** Docker secrets.

This follows the **zero plaintext credential storage** rule — no credential may appear
in plaintext in the database, logs, configuration files, or support ZIP bundles.

### Debug Mode Warning

**Never** set `DEBUG=true` or `FLASK_ENV=development` in production. Debug mode:

- Exposes the interactive Werkzeug debugger, which allows arbitrary code execution.
- Disables template caching and enables verbose error pages with stack traces.
- May leak sensitive configuration values in error responses.

### Audit Logging

All security-relevant events (authentication attempts, authorization decisions,
configuration changes, user/role modifications) are captured by the audit logging
service (F-303) and can be forwarded to an external SIEM via the `SIEM_ENDPOINT`
setting. See the [Security API Reference](api/security.md) for audit event details.

---

## Configuration Loading Precedence

The configuration system uses a **three-layer merge strategy** where each layer
overrides the previous one. The merge is a recursive deep-merge for nested YAML keys.

```
┌───────────────────────────────────────────┐
│  3. Environment Variables  (HIGHEST)      │  ← Always win
├───────────────────────────────────────────┤
│  2. config/{environment}.yaml             │  ← Overrides defaults
├───────────────────────────────────────────┤
│  1. config/default.yaml    (LOWEST)       │  ← Base values
└───────────────────────────────────────────┘
```

**Detailed load order:**

1. **`config/default.yaml`** — Loaded unconditionally. Contains every configuration
   key with a standalone-optimized default value. This file serves as the
   authoritative inventory of all available settings.

2. **`config/{environment}.yaml`** — Loaded based on the `FLASK_ENV` environment
   variable (or the `--config` CLI argument). Keys present in this file override
   their counterparts from `default.yaml`. Keys not present in this file retain
   their `default.yaml` values.

3. **Environment variables** — Applied last with the highest priority. Each YAML key
   has a corresponding environment variable name (documented in the tables above).
   Environment variables **always win** over any YAML file setting, regardless of
   which file defined the value.

**Example — `database.url` resolution:**

| Source | Value | Result |
|--------|-------|--------|
| `config/default.yaml` | `sqlite:///nexus.db` | Overridden ↓ |
| `config/production.yaml` | *(commented out)* | Falls through ↓ |
| `DATABASE_URL` env var | `postgresql://user:pass@host:5432/nexus` | **Winner** |

> **Tip:** To see the fully resolved configuration at runtime, check the application
> startup log. When `LOG_LEVEL=DEBUG`, the configuration loader logs all resolved
> values (with credentials redacted).

---

## Environment Variable Quick Reference

The following table lists every environment variable recognized by the application,
grouped by category. All variables listed here also appear in `.env.example`.

| Category | Variable | Required | Default |
|----------|----------|----------|---------|
| **Application** | `FLASK_APP` | No | `src.app:create_app` |
| | `FLASK_ENV` | No | `production` |
| | `SECRET_KEY` | **Yes** | — |
| | `DEBUG` | No | `false` |
| **Database** | `DATABASE_URL` | No | `sqlite:///nexus.db` |
| | `DATABASE_POOL_SIZE` | No | `10` |
| | `DATABASE_MAX_OVERFLOW` | No | `20` |
| | `DATABASE_POOL_TIMEOUT` | No | `30` |
| | `DATABASE_ECHO` | No | `false` |
| **BlobStore** | `BLOBSTORE_TYPE` | No | `file` |
| | `BLOBSTORE_PATH` | No | `./data/blobs` |
| **S3** | `S3_BUCKET` | If S3 | — |
| | `S3_REGION` | No | `us-east-1` |
| | `S3_ACCESS_KEY_ID` | No | *(IAM)* |
| | `S3_SECRET_ACCESS_KEY` | No | *(IAM)* |
| | `S3_ENCRYPTION` | No | `SSE-S3` |
| | `S3_KMS_KEY_ARN` | If KMS | — |
| | `S3_PREFIX` | No | *(empty)* |
| **JWT** | `JWT_SECRET` | **Yes** | — |
| | `JWT_ALGORITHM` | No | `HS256` |
| | `JWT_EXPIRATION_HOURS` | No | `24` |
| **LDAP** | `LDAP_URL` | No | *(empty)* |
| | `LDAP_BASE_DN` | No | *(empty)* |
| | `LDAP_USER_SEARCH_FILTER` | No | `(uid={0})` |
| | `LDAP_BIND_DN` | No | *(empty)* |
| | `LDAP_BIND_PASSWORD` | No | *(empty)* |
| | `LDAP_USE_TLS` | No | `true` |
| | `LDAP_GROUP_BASE_DN` | No | *(empty)* |
| | `LDAP_GROUP_SEARCH_FILTER` | No | *(empty)* |
| **SAML/OIDC** | `SAML_IDP_METADATA_URL` | No | *(empty)* |
| | `OIDC_CLIENT_ID` | No | *(empty)* |
| | `OIDC_CLIENT_SECRET` | No | *(empty)* |
| | `OIDC_DISCOVERY_URL` | No | *(empty)* |
| **Scheduler** | `SCHEDULER_ENABLED` | No | `true` |
| | `SCHEDULER_JOBSTORE_URL` | No | *(main DB)* |
| **Search** | `SEARCH_BACKEND` | No | `whoosh` |
| | `ELASTICSEARCH_URL` | No | `http://localhost:9200` |
| | `WHOOSH_INDEX_PATH` | No | `./data/search` |
| **Metrics** | `METRICS_ENABLED` | No | `true` |
| **Logging** | `LOG_LEVEL` | No | `INFO` |
| | `LOG_FORMAT` | No | `json` |
| **SIEM** | `SIEM_ENDPOINT` | No | *(empty)* |
| | `SIEM_TOKEN` | No | *(empty)* |
| **Webhooks** | `WEBHOOK_SECRET` | No | *(empty)* |
| | `WEBHOOK_TIMEOUT` | No | `30` |
| **Scripting** | `SCRIPTING_ENABLED` | No | `true` |
| **SSL/TLS** | `SSL_CERT_PATH` | No | *(empty)* |
| | `SSL_KEY_PATH` | No | *(empty)* |
| | `TRUSTSTORE_PATH` | No | *(empty)* |
| **Gunicorn** | `GUNICORN_BIND` | No | `0.0.0.0:8081` |
| | `GUNICORN_BACKLOG` | No | `2048` |
| | `GUNICORN_WORKERS` | No | `min(4, cpu*2+1)` |
| | `GUNICORN_WORKER_CLASS` | No | `sync` |
| | `GUNICORN_WORKER_CONNECTIONS` | No | `1000` |
| | `GUNICORN_THREADS` | No | `1` |
| | `GUNICORN_TIMEOUT` | No | `120` |
| | `GUNICORN_GRACEFUL_TIMEOUT` | No | `30` |
| | `GUNICORN_KEEPALIVE` | No | `5` |
| | `GUNICORN_ACCESS_LOG` | No | `-` |
| | `GUNICORN_ERROR_LOG` | No | `-` |
| | `GUNICORN_LOG_LEVEL` | No | `info` |
| | `GUNICORN_LIMIT_REQUEST_LINE` | No | `8190` |
| | `GUNICORN_LIMIT_REQUEST_FIELDS` | No | `100` |
| | `GUNICORN_LIMIT_REQUEST_FIELD_SIZE` | No | `8190` |
