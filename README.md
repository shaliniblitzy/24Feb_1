# Nexus Repository Manager (Python/Flask)

A universal binary repository manager rewritten from Java 21 to Python 3.13/Flask 3.1.3. Nexus Repository Manager provides a single source of truth for all software components, supporting **Maven**, **npm**, **Docker**, **NuGet**, **PyPI**, **APT**, and **Raw** artifact formats with three repository types — Hosted, Proxy, and Group.

---

## Features

Nexus Repository Manager delivers **20 features** across **5 categories**, faithfully preserving every capability of the original Java-based system.

### Repository Management (F-101 – F-104)

| ID | Feature | Description |
|----|---------|-------------|
| F-101 | **Multi-Format Artifact Support** | Store and serve artifacts in seven formats: Maven (JAR/POM), npm (tarballs/packuments), Docker (images/manifests), NuGet (packages), PyPI (wheels/sdists), APT (.deb packages), and Raw (generic binaries) |
| F-102 | **Three Repository Types** | **Hosted** repositories for internal artifacts, **Proxy** repositories that cache upstream registries (Maven Central, npmjs.com, Docker Hub, nuget.org, pypi.org), and **Group** repositories that aggregate multiple repositories into a single virtual endpoint |
| F-103 | **Content Indexing & Search** | Full-text search across all component metadata powered by Whoosh (standalone) or Elasticsearch (production), with format-aware query parsing and paginated results |
| F-104 | **Browse Tree Navigation** | Hierarchical component and asset traversal with namespace-based tree structure for intuitive artifact discovery |

### Storage Management (F-201 – F-204)

| ID | Feature | Description |
|----|---------|-------------|
| F-201 | **File-Based BlobStore** | Content-addressable filesystem storage with soft-delete support, configurable base directory, and blob deduplication via SHA-256 hashing |
| F-202 | **S3 BlobStore** | Amazon S3 integration via boto3 with server-side encryption (SSE-S3 and SSE-KMS with customer-managed keys), IAM role-based authentication, and configurable bucket/prefix |
| F-203 | **Maintenance Operations** | BlobStore compaction (reclaim soft-deleted space), integrity verification (checksum validation), and orphan blob detection with scheduled execution |
| F-204 | **Cleanup Policies** | Format-specific retention rules with configurable criteria (age, usage, regex patterns) and scheduled policy execution for automated artifact lifecycle management |

### Security (F-301 – F-304)

| ID | Feature | Description |
|----|---------|-------------|
| F-301 | **Three-Tier RBAC** | Role-Based Access Control with system-wide privileges, repository-scoped permissions, and sub-repository path filtering via Content Selector Expression Language (CSEL) |
| F-302 | **SSL/TLS Certificate Management** | PEM and PKCS12 certificate import/export, trust store management, certificate chain validation, and TLS 1.2+ enforcement for all connections |
| F-303 | **JSON Audit Logging** | Structured JSON event capture for all security-relevant operations (authentication, authorization, configuration changes) with SIEM endpoint forwarding |
| F-304 | **API Key Authentication** | Bearer token-based authentication for CI/CD pipeline integration, with token generation, rotation, and revocation capabilities |

### Administration (F-401 – F-404)

| ID | Feature | Description |
|----|---------|-------------|
| F-401 | **Health Check Monitoring** | Individual component checks (database, BlobStore, scheduler) with aggregated system status exposed at `/service/rest/v1/status/check` |
| F-402 | **Scheduled Task Management** | Cron-style, interval, and one-off task scheduling via APScheduler with persistent SQLAlchemy job stores and execution history tracking |
| F-403 | **Support ZIP Generation** | Diagnostic bundle creation with system info, configuration (credential-redacted), logs, metrics snapshot, and thread dump — downloadable as a single ZIP archive |
| F-404 | **System Configuration** | Hierarchical key-value configuration store with type validation, change notification via signals, and REST API management |

### Integration & Extensibility (F-501 – F-504)

| ID | Feature | Description |
|----|---------|-------------|
| F-501 | **REST API with OpenAPI** | Comprehensive REST API at `/service/rest/v1/**` with auto-generated OpenAPI 3.x documentation via flask-smorest |
| F-502 | **Scripting Engine** | Sandboxed Python script execution with injected API context for repository access, security checks, and structured logging |
| F-503 | **Webhook Notifications** | HMAC-SHA256 signed HTTP POST event notifications triggered by repository, component, and security events via blinker signals |
| F-504 | **Plugin Architecture** | Dynamic format handler registration via entry points or configured directories for extending supported artifact formats |

---

## Architecture Overview

Nexus Repository Manager follows a **modular monolith** pattern (ADR-001) implemented as a single deployable Flask application using blueprint-based modularity.

### Five-Layer Architecture

```
┌─────────────────────────────────────┐
│         UI Framework Layer          │  ← Static files, CORS, frontend assets
├─────────────────────────────────────┤
│           API Layer                 │  ← Flask blueprints, REST routes, OpenAPI
├─────────────────────────────────────┤
│        Core Framework Layer         │  ← Auth chain, RBAC, signals, scheduler
├─────────────────────────────────────┤
│        Repository Layer             │  ← Format handlers, repository types, search
├─────────────────────────────────────┤
│         Storage Layer               │  ← DataStore (SQLAlchemy) + BlobStore (File/S3)
└─────────────────────────────────────┘
```

Cross-layer dependencies flow strictly downward: **API → Core → Repository → Storage**.

### Dual-Persistence Pattern

- **DataStore** — Metadata persistence via SQLAlchemy ORM with support for SQLite (standalone) and PostgreSQL (clustered/production). Manages all entity types: repositories, components, assets, users, roles, privileges, tasks, audit events, and configuration.
- **BlobStore** — Binary artifact persistence via a pluggable backend interface. Supports file-based storage (content-addressable filesystem) and S3 storage (boto3 with SSE encryption). The two systems share only blob reference IDs, never raw data.

### Six-Phase Startup Sequence

The application factory (`create_app()`) orchestrates startup in strict order:

1. **KERNEL** — Verify Python runtime, load configuration, initialize Flask extensions
2. **SCHEMAS** — Run Alembic database migrations, verify schema version
3. **STORAGE** — Initialize BlobStore backends (File and/or S3), verify connectivity
4. **SECURITY** — Configure multi-realm authentication chain (Local, Bearer, JWT, LDAP) and RBAC evaluator
5. **CAPABILITIES** — Register format handlers (Maven, npm, Docker, NuGet, PyPI, APT, Raw) and plugins
6. **SERVICES** — Start APScheduler background tasks, enable health monitoring, begin accepting requests

---

## Technology Stack

### Core Framework

| Component | Version | Purpose | Replaces (Java) |
|-----------|---------|---------|-----------------|
| Python | 3.13 | Runtime language | Java 21 |
| Flask | 3.1.3 | Web framework, routing, blueprints | Jetty 12.0.5 + RESTEasy 6.2.7 |
| Gunicorn | 23.0.0 | Production WSGI HTTP server | Jetty embedded server |
| flask-smorest | 0.45.0 | REST API with OpenAPI 3.x docs | Swagger 2.2.20 |
| blinker | ≥1.9 | Signal/event system | Guava EventBus |

### Database & ORM

| Component | Version | Purpose | Replaces (Java) |
|-----------|---------|---------|-----------------|
| SQLAlchemy | 2.0.46 | ORM, connection pooling, unit of work | MyBatis 3.5.15 + HikariCP 4.0.3 |
| Flask-SQLAlchemy | 3.1.1 | Flask integration for SQLAlchemy | Guice-MyBatis bindings |
| Alembic | 1.14.1 | Database migration management | Flyway 8.5.13 |
| Flask-Migrate | 4.0.7 | Flask CLI for Alembic migrations | Maven Flyway plugin |
| psycopg2-binary | 2.9.10 | PostgreSQL adapter | PostgreSQL JDBC 42.7.2 |

### Authentication & Security

| Component | Version | Purpose | Replaces (Java) |
|-----------|---------|---------|-----------------|
| Flask-Login | 0.6.3 | Session management | Apache Shiro 2.0.0 sessions |
| PyJWT | 2.10.1 | JWT token handling | Java-JWT 4.4.0 |
| bcrypt | 4.2.1 | Password hashing | BouncyCastle 1.78.1 |
| cryptography | 44.0.0 | Certificates, HMAC, encryption | BouncyCastle 1.78.1 |
| python-ldap | 3.4.4 | LDAP/AD integration | Shiro LDAP realm |

### Additional Components

| Component | Version | Purpose |
|-----------|---------|---------|
| APScheduler | 3.11.2 | Task scheduling with persistent job store |
| Whoosh | 2.7.4 | Full-text search (standalone) |
| elasticsearch | 8.17.0 | Search client (production) |
| boto3 | 1.36.7 | AWS S3 integration |
| marshmallow | 3.25.1 | Schema serialization/validation |
| prometheus-client | 0.21.1 | Metrics exposition |
| requests | 2.32.3 | HTTP client for proxy upstreams |

---

## Quick Start

### Prerequisites

- **Python 3.13+** with pip
- **PostgreSQL 16** (optional — SQLite is used by default for standalone mode)
- **Elasticsearch 8.x** (optional — Whoosh is used by default for standalone search)

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd nexus-repository

# Create and activate a virtual environment
python3.13 -m venv venv
source venv/bin/activate

# Install production dependencies
pip install -r requirements.txt

# Copy and configure environment variables
cp .env.example .env
# Edit .env with your settings (DATABASE_URL, SECRET_KEY, etc.)

# Run database migrations
flask db upgrade

# Start the development server
flask run --host=0.0.0.0 --port=8081
```

### Docker Quick Start

```bash
# Start the full stack (app + PostgreSQL + Elasticsearch)
docker-compose up

# The application will be available at http://localhost:8081
# API documentation at http://localhost:8081/swagger.json
```

### Verify Installation

```bash
# Health check
curl http://localhost:8081/service/rest/v1/status/check

# Prometheus metrics
curl http://localhost:8081/service/metrics
```

---

## Deployment Models

Nexus Repository Manager supports three deployment configurations:

### Standalone

Best for development and small teams.

| Component | Technology |
|-----------|-----------|
| Database | SQLite (file-based, zero-config) |
| BlobStore | File-based (local filesystem) |
| Search | Whoosh (pure-Python, embedded) |

```bash
# .env configuration
DATABASE_URL=sqlite:///nexus.db
BLOBSTORE_TYPE=file
BLOBSTORE_PATH=./data/blobs
SEARCH_BACKEND=whoosh
```

### Clustered

Best for high-availability production environments.

| Component | Technology |
|-----------|-----------|
| Database | PostgreSQL 16 (shared across nodes) |
| BlobStore | Shared filesystem (NFS/GlusterFS) |
| Search | Elasticsearch 8.x (external cluster) |

```bash
# .env configuration
DATABASE_URL=postgresql://nexus:password@db-host:5432/nexus
BLOBSTORE_TYPE=file
BLOBSTORE_PATH=/shared/blobs
SEARCH_BACKEND=elasticsearch
ELASTICSEARCH_URL=http://es-host:9200
```

### Container-Native

Best for Kubernetes and cloud-native deployments.

| Component | Technology |
|-----------|-----------|
| Database | PostgreSQL 16 (managed — RDS, Cloud SQL) |
| BlobStore | Amazon S3 with SSE-S3/SSE-KMS encryption |
| Search | Elasticsearch 8.x (managed — OpenSearch) |

```bash
# .env configuration
DATABASE_URL=postgresql://nexus:password@rds-endpoint:5432/nexus
BLOBSTORE_TYPE=s3
S3_BUCKET=nexus-artifacts
S3_REGION=us-east-1
S3_ENCRYPTION=SSE-KMS
S3_KMS_KEY_ARN=arn:aws:kms:us-east-1:123456789:key/abc-def
SEARCH_BACKEND=elasticsearch
ELASTICSEARCH_URL=https://opensearch-endpoint:443
```

---

## Configuration

### Configuration Files

Environment-specific YAML configuration files are located in the [`config/`](config/) directory:

| File | Purpose |
|------|---------|
| [`config/default.yaml`](config/default.yaml) | Default values for all settings |
| [`config/development.yaml`](config/development.yaml) | Development environment overrides |
| [`config/production.yaml`](config/production.yaml) | Production environment settings |
| [`config/testing.yaml`](config/testing.yaml) | Test environment configuration |

### Environment Variable Overrides

Every configuration setting supports environment variable overrides for container-native deployments. Key variables include:

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | Database connection string | `sqlite:///nexus.db` |
| `SECRET_KEY` | Flask secret key for sessions | *(required)* |
| `JWT_SECRET` | JWT signing secret | *(required)* |
| `BLOBSTORE_TYPE` | Storage backend (`file` or `s3`) | `file` |
| `BLOBSTORE_PATH` | File BlobStore directory | `./data/blobs` |
| `S3_BUCKET` | S3 bucket name | *(required for S3)* |
| `SEARCH_BACKEND` | Search engine (`whoosh` or `elasticsearch`) | `whoosh` |
| `ELASTICSEARCH_URL` | Elasticsearch connection URL | `http://localhost:9200` |
| `LOG_LEVEL` | Application log level | `INFO` |
| `LOG_FORMAT` | Log output format (`json` or `text`) | `json` |

See [`.env.example`](.env.example) for the complete variable reference with descriptions and accepted values.

---

## API Documentation

### REST API

All management endpoints are available under `/service/rest/v1/`:

| Endpoint Prefix | Module | Description |
|-----------------|--------|-------------|
| `/service/rest/v1/repositories` | Repository Management | Repository CRUD, listing, and status management |
| `/service/rest/v1/components` | Repository Management | Component metadata queries |
| `/service/rest/v1/assets` | Repository Management | Asset metadata and download |
| `/service/rest/v1/search` | Repository Management | Full-text search across components |
| `/service/rest/v1/security/users` | Security | User account management |
| `/service/rest/v1/security/roles` | Security | Role definition and assignment |
| `/service/rest/v1/security/privileges` | Security | Privilege configuration |
| `/service/rest/v1/security/certificates` | Security | SSL/TLS certificate management |
| `/service/rest/v1/status` | Administration | Health checks and system status |
| `/service/rest/v1/admin/tasks` | Administration | Scheduled task management |
| `/service/rest/v1/admin/system/config` | Administration | System configuration |
| `/service/rest/v1/admin/support/supportzip` | Administration | Diagnostic bundle download |
| `/service/rest/v1/blobstores` | Storage | BlobStore configuration |

### OpenAPI Documentation

Interactive API documentation is auto-generated via flask-smorest and available at:

- **OpenAPI Spec:** `GET /swagger.json`

### Format-Native Protocol Endpoints

Build tools interact directly with format-specific endpoints:

| Format | Endpoint Pattern | Client Tools |
|--------|-----------------|-------------|
| **Maven** | `/repository/{repo_name}/...` | `mvn`, `gradle` |
| **npm** | `/repository/{repo_name}/-/...` | `npm`, `yarn` |
| **Docker** | `/v2/{repo_name}/...` | `docker pull/push` |
| **NuGet** | `/repository/{repo_name}/index.json` | `dotnet nuget`, `nuget.exe` |
| **PyPI** | `/repository/{repo_name}/simple/...` | `pip`, `twine` |
| **APT** | `/repository/{repo_name}/dists/...` | `apt-get` |
| **Raw** | `/repository/{repo_name}/...` | `curl`, `wget` |

See [`docs/api/`](docs/api/) for detailed endpoint reference documentation.

---

## Testing

The test suite follows a multi-tier testing pyramid with unit, integration, and end-to-end tests.

```bash
# Activate the virtual environment
source venv/bin/activate

# Install development dependencies
pip install -r requirements-dev.txt

# Run all tests
pytest

# Run unit tests only
pytest tests/unit/

# Run integration tests
pytest tests/integration/

# Run end-to-end tests
pytest tests/e2e/

# Run with coverage report
pytest --cov=src

# Run tests by marker
pytest -m unit          # Unit tests (fast, no external deps)
pytest -m integration   # Integration tests (database, BlobStore)
pytest -m e2e           # End-to-end tests (full lifecycle)
```

### Coverage Targets

| Metric | Target |
|--------|--------|
| Line coverage | ≥ 80% |
| Branch coverage | ≥ 70% |

Coverage is enforced by pytest-cov and configured in [`pytest.ini`](pytest.ini).

---

## Project Structure

```
nexus-repository/
├── src/                          # Application source code
│   ├── __init__.py
│   ├── app.py                    # Flask application factory (create_app)
│   ├── config.py                 # Configuration loader with env var overrides
│   ├── extensions.py             # Flask extension initialization
│   ├── exceptions.py             # Typed exception hierarchy
│   ├── signals.py                # Blinker signal definitions
│   ├── startup.py                # Six-phase startup orchestrator
│   ├── repositories/             # Repository management (F-101 – F-104)
│   │   ├── routes.py             # REST API routes
│   │   ├── services.py           # RepositoryManager service
│   │   ├── search.py             # Full-text search service
│   │   ├── browse.py             # Browse tree navigation
│   │   ├── formats/              # Format handlers
│   │   │   ├── maven.py          # Maven repository format
│   │   │   ├── npm.py            # npm registry protocol
│   │   │   ├── docker.py         # Docker Registry HTTP API V2
│   │   │   ├── nuget.py          # NuGet V3 protocol
│   │   │   ├── pypi.py           # PyPI Simple Repository API
│   │   │   ├── apt.py            # APT repository handler
│   │   │   └── raw.py            # Raw content handler
│   │   └── types/                # Repository types
│   │       ├── hosted.py         # Direct artifact storage
│   │       ├── proxy.py          # Upstream caching proxy
│   │       └── group.py          # Virtual aggregation
│   ├── storage/                  # Storage management (F-201 – F-204)
│   │   ├── routes.py             # REST API routes
│   │   ├── datastore.py          # DataStore abstraction
│   │   ├── maintenance.py        # Compaction, integrity verification
│   │   ├── cleanup.py            # Cleanup policy engine
│   │   └── blobstore/            # BlobStore backends
│   │       ├── __init__.py       # Abstract base class
│   │       ├── file_blobstore.py # File-based storage
│   │       └── s3_blobstore.py   # Amazon S3 storage
│   ├── security/                 # Security framework (F-301 – F-304)
│   │   ├── routes.py             # REST API routes
│   │   ├── rbac.py               # Three-tier RBAC evaluator
│   │   ├── csel.py               # Content Selector Expression Language
│   │   ├── ssl_manager.py        # Certificate lifecycle management
│   │   ├── audit.py              # JSON audit logging service
│   │   ├── decorators.py         # @requires_auth, @requires_role
│   │   └── auth/                 # Authentication realms
│   │       ├── chain.py          # Multi-realm authenticator
│   │       ├── local_realm.py    # Username/password (bcrypt)
│   │       ├── bearer_realm.py   # API key tokens
│   │       ├── jwt_handler.py    # JWT generation/validation
│   │       ├── ldap_realm.py     # LDAP/Active Directory
│   │       └── saml_realm.py     # SAML/OpenID Connect SSO
│   ├── admin/                    # Administration (F-401 – F-404)
│   │   ├── routes.py             # REST API routes
│   │   ├── health.py             # Health check service
│   │   ├── scheduler.py          # APScheduler task management
│   │   ├── support_zip.py        # Diagnostic bundle generator
│   │   └── system_config.py      # Configuration management
│   ├── integration/              # Integration & extensibility (F-501 – F-504)
│   │   ├── routes.py             # REST API routes
│   │   ├── rest_api.py           # OpenAPI spec generation
│   │   ├── scripting.py          # Sandboxed script execution
│   │   ├── webhooks.py           # HMAC-signed event dispatch
│   │   └── plugins.py            # Format plugin architecture
│   ├── metrics/                  # Prometheus observability
│   │   ├── collectors.py         # Metric definitions
│   │   ├── middleware.py         # Request tracking middleware
│   │   └── health.py             # /service/metrics endpoint
│   ├── models/                   # SQLAlchemy ORM models
│   │   ├── base.py               # Declarative base, mixins
│   │   ├── repository.py         # Repository, ProxyConfig, GroupConfig
│   │   ├── component.py          # Component, Asset
│   │   ├── security.py           # User, Role, Privilege, ContentSelector
│   │   ├── admin.py              # TaskDefinition, TaskExecution, SystemConfig
│   │   ├── audit.py              # AuditEvent
│   │   └── storage.py            # BlobStoreConfig, CleanupPolicy
│   └── utils/                    # Shared utilities
│       ├── hashing.py            # SHA-1, SHA-256, MD5 computation
│       ├── pagination.py         # Cursor and offset pagination
│       ├── validation.py         # Request validation helpers
│       └── http_client.py        # Outbound HTTP client
├── tests/                        # Test suite
│   ├── conftest.py               # Shared pytest fixtures
│   ├── unit/                     # Unit tests
│   ├── integration/              # Integration tests
│   └── e2e/                      # End-to-end tests
├── config/                       # YAML configuration files
│   ├── default.yaml
│   ├── development.yaml
│   ├── production.yaml
│   └── testing.yaml
├── migrations/                   # Alembic database migrations
│   ├── env.py
│   └── versions/
├── docs/                         # Documentation
│   ├── architecture.md
│   ├── configuration.md
│   └── api/
│       ├── repositories.md
│       ├── security.md
│       └── admin.md
├── pyproject.toml                # PEP 621 project metadata
├── requirements.txt              # Pinned production dependencies
├── requirements-dev.txt          # Development and testing dependencies
├── Dockerfile                    # Container image build
├── docker-compose.yml            # Multi-service dev environment
├── gunicorn.conf.py              # Production WSGI server config
├── alembic.ini                   # Migration configuration
├── pytest.ini                    # Test runner configuration
├── .env.example                  # Environment variable template
└── .flake8                       # Linting configuration
```

---

## License

> **Note:** License information has not yet been specified for this project. Please add the appropriate license before distributing this software.
