# Architecture Overview

The Nexus Repository Manager is a universal binary repository manager rewritten from
Java 21 (OSGi/Karaf, Guice, JAX-RS, Shiro, Quartz, MyBatis) to **Python 3.13** with
**Flask 3.1.3**. The architecture preserves the original system's five-layer design,
dual-persistence pattern, and modular monolith deployment model while leveraging
idiomatic Python equivalents for every Java component.

This document serves as the authoritative architecture reference for developers,
operators, and architects working with the Nexus Repository Manager codebase.

---

## Table of Contents

1. [Architectural Decision Records](#architectural-decision-records)
2. [Five-Layer Architecture](#five-layer-architecture)
3. [Dual-Persistence Architecture](#dual-persistence-architecture)
4. [Module Organization](#module-organization)
5. [Technology Stack Mapping](#technology-stack-mapping)
6. [Six-Phase Startup Sequence](#six-phase-startup-sequence)
7. [Event System](#event-system)
8. [Authentication Chain](#authentication-chain)
9. [Three-Tier RBAC](#three-tier-rbac)
10. [Deployment Models](#deployment-models)
11. [Repository Lifecycle State Machine](#repository-lifecycle-state-machine)
12. [Supported Repository Formats](#supported-repository-formats)

---

## Architectural Decision Records

The following key architectural decisions govern the design and evolution of the
Nexus Repository Manager Python/Flask application.

### ADR-001: Modular Monolith Architecture

**Status:** Accepted

**Decision:** The application is a **single deployable unit** using Flask blueprints
for module boundaries. The system does not use microservices — all inter-component
communication is **in-process** via direct function calls, blinker signals, or shared
SQLAlchemy sessions.

**Rationale:**

- The original Java system was an OSGi/Karaf-based modular monolith. The Python
  rewrite preserves this deployment model for operational simplicity and performance.
- In-process communication eliminates network latency and serialization overhead
  between components.
- A single deployable unit simplifies configuration management, database migrations,
  and transactional consistency.

**Constraint:** The Flask application must remain a single deployable unit — do not
split into microservices. All modules (repositories, storage, security, admin,
integration) are packaged and deployed together.

### ADR-006: Multi-Realm Authentication (Apache Shiro Equivalent)

**Status:** Accepted

**Decision:** Replicate the Apache Shiro 2.0.0 realm-based multi-backend
authentication system using Flask-Login for session management, PyJWT for token
handling, and custom realm classes implementing the `FirstSuccessfulAuthenticator`
pattern.

**Rationale:**

- The original system supports five authentication backends (local credentials, API
  keys, JWT tokens, LDAP, SAML/OIDC) evaluated in sequence.
- Flask-Login provides the session management infrastructure; custom realm classes
  encapsulate each backend's authentication logic.

### C-001: Minimum Resource Requirements

**Status:** Accepted

**Decision:** The application requires a minimum of 4 CPU cores and 4 GB RAM for
production deployments. Gunicorn worker configuration is tuned accordingly.

### C-003: Schema Fidelity

**Status:** Accepted

**Decision:** The SQLAlchemy schema faithfully replicates the documented ER model
with 12+ entity types across 6 data domains. No schema changes are permitted beyond
what is documented.

---

## Five-Layer Architecture

The application follows a strict five-layer architecture where dependencies flow
**downward only**. No layer may import from or depend on a layer above it.

```
┌─────────────────────────────────────────────────────────────┐
│                       UI Framework                          │
│            (Static files / REST API consumers)              │
│                                                             │
│  Serves frontend assets (HTML, JS, CSS) from static/       │
│  directory. Frontend consumes REST API endpoints.           │
├─────────────────────────────────────────────────────────────┤
│                        API Layer                            │
│        (Flask blueprints, routes, REST endpoints)           │
│                                                             │
│  src/repositories/routes.py    src/security/routes.py       │
│  src/admin/routes.py           src/integration/routes.py    │
│  src/storage/routes.py                                      │
├─────────────────────────────────────────────────────────────┤
│                     Core Framework                          │
│    (Application factory, extensions, configuration,         │
│     signals, exceptions, auth chain, RBAC)                  │
│                                                             │
│  src/app.py          src/extensions.py    src/config.py     │
│  src/signals.py      src/exceptions.py    src/startup.py    │
│  src/security/auth/*                      src/security/     │
│  src/security/rbac.py                     rbac.py           │
├─────────────────────────────────────────────────────────────┤
│                    Repository Layer                         │
│   (Format handlers, repository types, search, browse)       │
│                                                             │
│  src/repositories/formats/maven.py    .../npm.py            │
│  src/repositories/formats/docker.py   .../nuget.py          │
│  src/repositories/formats/pypi.py     .../apt.py            │
│  src/repositories/formats/raw.py                            │
│  src/repositories/types/hosted.py     .../proxy.py          │
│  src/repositories/types/group.py                            │
│  src/repositories/search.py           .../browse.py         │
│  src/repositories/services.py                               │
├─────────────────────────────────────────────────────────────┤
│                      Storage Layer                          │
│        (DataStore + BlobStore dual-persistence)              │
│                                                             │
│  src/storage/datastore.py                                   │
│  src/storage/blobstore/file_blobstore.py                    │
│  src/storage/blobstore/s3_blobstore.py                      │
│  src/storage/maintenance.py       src/storage/cleanup.py    │
│  src/models/base.py   src/models/repository.py              │
│  src/models/component.py          src/models/security.py    │
│  src/models/admin.py              src/models/audit.py       │
│  src/models/storage.py                                      │
└─────────────────────────────────────────────────────────────┘
```

### Layer Dependency Rules

- **UI Framework → API Layer:** The frontend consumes REST API endpoints. It does
  not directly access any lower layer.
- **API Layer → Core Framework:** Route handlers use core services for
  authentication, authorization, configuration, and error handling.
- **API Layer → Repository Layer:** Route handlers delegate business logic to
  repository services, format handlers, and search services.
- **Core Framework → Repository Layer:** The authentication chain and RBAC evaluator
  interact with repository metadata for permission resolution.
- **Core Framework → Storage Layer:** Extensions (SQLAlchemy, migrations) interact
  directly with the storage layer for database operations.
- **Repository Layer → Storage Layer:** Format handlers and repository types use
  the DataStore for metadata persistence and the BlobStore for binary artifact
  storage.

**Cross-layer dependencies must flow strictly downward.** A module in the Storage
Layer must never import from the API Layer or Core Framework. A module in the
Repository Layer must never import from the API Layer.

---

## Dual-Persistence Architecture

The application implements a **dual-persistence pattern** separating structured
metadata from binary artifact content. This design allows independent scaling and
technology selection for each storage concern.

```
┌──────────────────┐       blob_ref       ┌──────────────────┐
│    DataStore      │ ◄──────────────────► │    BlobStore      │
│   (SQLAlchemy)    │                      │   (File / S3)     │
│                   │                      │                   │
│  • Repository     │                      │  • store(blob)    │
│  • Component      │                      │  • get(blob_id)   │
│  • Asset          │                      │  • delete(blob_id)│
│  • User           │                      │  • exists(blob_id)│
│  • Role           │                      │                   │
│  • Privilege      │                      │  Implementations: │
│  • ContentSelector│                      │  ┌──────────────┐ │
│  • TaskDefinition │                      │  │File BlobStore│ │
│  • TaskExecution  │                      │  │ (F-201)      │ │
│  • SystemConfig   │                      │  └──────────────┘ │
│  • AuditEvent     │                      │  ┌──────────────┐ │
│  • BlobStoreConfig│                      │  │ S3 BlobStore │ │
│  • CleanupPolicy  │                      │  │ (F-202)      │ │
│                   │                      │  └──────────────┘ │
└──────────────────┘                      └──────────────────┘
```

### DataStore (Metadata)

The DataStore manages all structured metadata through **SQLAlchemy 2.0.46 ORM**
with the declarative model and unit-of-work pattern.

- **Entities managed:** Repository, Component, Asset, User, Role, Privilege,
  ContentSelector, RoleAssignment, TaskDefinition, TaskExecution, SystemConfig,
  AuditEvent, BlobStoreConfig, CleanupPolicy
- **Database backends:**
  - **SQLite** — standalone deployments (single-file, zero-configuration)
  - **PostgreSQL** — clustered and production deployments (connection pooling via
    SQLAlchemy QueuePool)
- **Replaces:** MyBatis 3.5.15 (data access) + HikariCP 4.0.3 (connection pooling)
- **Migrations:** Managed by Alembic 1.14.1 (replaces Flyway 8.5.13)

### BlobStore (Binary Artifacts)

The BlobStore manages all binary artifact content through an abstract interface with
pluggable backend implementations.

- **Interface methods:** `store(blob)`, `get(blob_id)`, `delete(blob_id)`,
  `exists(blob_id)`
- **Content addressing:** Artifacts are stored using SHA-256 content-based
  addressing for deduplication
- **Soft-delete:** Blob deletion follows a soft-delete pattern — blobs are marked
  as deleted but remain recoverable until the next compaction cycle

**File BlobStore (F-201):**
- Content-addressable filesystem storage
- Configurable base directory
- Soft-delete and compaction support
- Suitable for standalone and shared-filesystem clustered deployments

**S3 BlobStore (F-202):**
- AWS S3 integration via boto3
- Server-side encryption: SSE-S3 (default) and SSE-KMS (customer-managed keys)
- IAM authentication: roles, instance profiles, explicit access keys
- Configurable bucket and key prefix
- Suitable for container-native and cloud deployments

### Separation Principle

The DataStore and BlobStore share **only blob references (IDs)** — never raw binary
data. An `Asset` record in the DataStore contains a `blob_ref` field that references
the corresponding binary content in the BlobStore. This separation enables:

- Independent scaling of metadata queries and binary downloads
- Different storage backends for different deployment models
- Atomic metadata operations without locking binary storage
- Content deduplication across multiple assets via shared blob references

---

## Module Organization

The application is organized into five Flask blueprints, each corresponding to a
feature category from the original system. All blueprints are registered during
application startup by the `create_app()` factory in `src/app.py`.

| Blueprint | Package | URL Prefix | Features |
|-----------|---------|------------|----------|
| `repositories` | `src/repositories/` | `/service/rest/v1/repositories` | F-101–F-104: Multi-format artifact support, three repository types (hosted, proxy, group), content indexing and search, browse tree navigation |
| `storage` | `src/storage/` | `/service/rest/v1/blobstores` | F-201–F-204: File and S3 BlobStore configuration, BlobStore maintenance (compaction, integrity verification), cleanup policies with format-specific retention |
| `security` | `src/security/` | `/service/rest/v1/security` | F-301–F-304: Multi-realm authentication, three-tier RBAC, SSL/TLS certificate management, JSON audit logging with SIEM integration, API key authentication |
| `admin` | `src/admin/` | `/service/rest/v1/admin` | F-401–F-404: Health check monitoring, scheduled task management, diagnostic support ZIP generation, hierarchical system configuration |
| `integration` | `src/integration/` | `/service/rest/v1` | F-501–F-504: REST API with OpenAPI documentation, server-side scripting engine, webhook event notifications with HMAC signing, plugin architecture |

### Shared Modules

In addition to the blueprints, the following shared modules provide cross-cutting
functionality:

| Module | Package | Purpose |
|--------|---------|---------|
| Models | `src/models/` | SQLAlchemy ORM models for all 12+ entity types across 6 data domains |
| Metrics | `src/metrics/` | Prometheus metric collectors, request middleware, health endpoint |
| Utilities | `src/utils/` | Hashing, pagination, request validation, outbound HTTP client |

### Package Structure

```
src/
├── __init__.py                  # Package initialization
├── app.py                       # Flask application factory (create_app)
├── config.py                    # Configuration management with env var overrides
├── exceptions.py                # Typed exception hierarchy
├── extensions.py                # Flask extension initialization
├── signals.py                   # Blinker signal definitions
├── startup.py                   # Six-phase startup orchestrator
│
├── models/                      # SQLAlchemy ORM models
│   ├── __init__.py              # Central model registry
│   ├── base.py                  # Declarative base with mixins
│   ├── repository.py            # Repository, ProxyConfig, GroupConfig
│   ├── component.py             # Component, Asset
│   ├── security.py              # User, Role, Privilege, ContentSelector
│   ├── admin.py                 # TaskDefinition, TaskExecution, SystemConfig
│   ├── audit.py                 # AuditEvent
│   └── storage.py               # BlobStoreConfig, CleanupPolicy
│
├── repositories/                # Repository management (F-101–F-104)
│   ├── __init__.py              # Blueprint registration
│   ├── routes.py                # REST API routes
│   ├── services.py              # RepositoryManager service
│   ├── search.py                # Full-text search service
│   ├── browse.py                # Browse tree navigation
│   ├── formats/                 # Format handlers
│   │   ├── __init__.py          # Format registry
│   │   ├── maven.py             # Maven repository format
│   │   ├── npm.py               # npm registry protocol
│   │   ├── docker.py            # Docker Registry HTTP API V2
│   │   ├── nuget.py             # NuGet V3 protocol
│   │   ├── pypi.py              # PyPI Simple Repository API
│   │   ├── apt.py               # APT repository
│   │   └── raw.py               # Raw repository
│   └── types/                   # Repository types
│       ├── __init__.py          # Type registry
│       ├── hosted.py            # Hosted (local storage)
│       ├── proxy.py             # Proxy (upstream caching)
│       └── group.py             # Group (virtual aggregation)
│
├── storage/                     # Storage management (F-201–F-204)
│   ├── __init__.py              # Blueprint registration
│   ├── routes.py                # REST API routes
│   ├── datastore.py             # DataStore abstraction layer
│   ├── maintenance.py           # BlobStore maintenance service
│   ├── cleanup.py               # Cleanup policy engine
│   └── blobstore/               # BlobStore implementations
│       ├── __init__.py          # BlobStore interface and factory
│       ├── file_blobstore.py    # File-based BlobStore (F-201)
│       └── s3_blobstore.py      # S3 BlobStore (F-202)
│
├── security/                    # Security (F-301–F-304)
│   ├── __init__.py              # Blueprint registration
│   ├── routes.py                # REST API routes
│   ├── rbac.py                  # Three-tier RBAC evaluator
│   ├── csel.py                  # Content Selector Expression Language
│   ├── ssl_manager.py           # SSL/TLS certificate management
│   ├── audit.py                 # Audit logging service
│   ├── decorators.py            # @requires_auth, @requires_role, etc.
│   └── auth/                    # Authentication framework
│       ├── __init__.py          # Auth package initialization
│       ├── chain.py             # Multi-realm authentication chain
│       ├── local_realm.py       # Local credential realm
│       ├── bearer_realm.py      # API key realm
│       ├── jwt_handler.py       # JWT token handler
│       ├── ldap_realm.py        # LDAP/AD realm
│       └── saml_realm.py        # SAML/OIDC realm
│
├── admin/                       # Administration (F-401–F-404)
│   ├── __init__.py              # Blueprint registration
│   ├── routes.py                # REST API routes
│   ├── health.py                # Health check service
│   ├── scheduler.py             # Task management (APScheduler)
│   ├── support_zip.py           # Support ZIP generator
│   └── system_config.py         # System configuration management
│
├── integration/                 # Integration (F-501–F-504)
│   ├── __init__.py              # Blueprint registration
│   ├── routes.py                # REST API routes
│   ├── rest_api.py              # OpenAPI spec generator
│   ├── scripting.py             # Scripting engine
│   ├── webhooks.py              # Webhook dispatcher
│   └── plugins.py               # Plugin manager
│
├── metrics/                     # Observability
│   ├── __init__.py              # Module initialization
│   ├── collectors.py            # Prometheus metric definitions
│   ├── middleware.py            # Request metric collection
│   └── health.py                # /service/metrics endpoint
│
└── utils/                       # Shared utilities
    ├── __init__.py              # Package initialization
    ├── hashing.py               # Content hash computation
    ├── pagination.py            # Cursor/offset pagination
    ├── validation.py            # Request validation helpers
    └── http_client.py           # Outbound HTTP client
```

---

## Technology Stack Mapping

The following table maps each major Java component from the original Nexus Repository
to its Python equivalent in this rewrite.

| Original (Java) | Python Equivalent | Purpose |
|-----------------|-------------------|---------|
| Jetty 12.0.5 + RESTEasy 6.2.7 | Flask 3.1.3 + Gunicorn 23.0.0 | HTTP server and REST API routing |
| Apache Shiro 2.0.0 | Flask-Login 0.6.3 + PyJWT 2.10.1 + bcrypt 4.2.1 | Authentication and authorization |
| Guice 7.0.0 | Flask app context + service registry pattern | Dependency injection |
| MyBatis 3.5.15 + HikariCP 4.0.3 | SQLAlchemy 2.0.46 (ORM + QueuePool) | ORM, data access, and connection pooling |
| Quartz 2.3.2 | APScheduler 3.11.2 | Cron-style task scheduling with persistent job store |
| Guava EventBus | blinker (Flask signals) | In-process event distribution |
| Flyway 8.5.13 | Alembic 1.14.1 (via Flask-Migrate 4.0.7) | Database schema migrations |
| Jackson 2.16.1 | marshmallow 3.25.1 | Schema-based serialization and validation |
| Swagger 2.2.20 + JAX-RS annotations | flask-smorest 0.45.0 | OpenAPI 3.x documentation generation |
| Dropwizard Metrics 4.2.25 + Prometheus 0.16.0 | prometheus-client 0.21.1 | Metrics collection and Prometheus exposition |
| BouncyCastle 1.78.1 | cryptography 44.0.0 + bcrypt 4.2.1 | PEM/X.509 certificate handling, password hashing, HMAC |
| Elasticsearch 2.4.3 (embedded) | Whoosh 2.7.4 (standalone) / elasticsearch 8.17.0 (production) | Full-text component and asset search |
| Apache HttpClient 4.5.14 | requests 2.32.3 | Outbound HTTP for proxy repository upstream fetches |
| OSGi/Karaf 4.4.4 bundles | Flask blueprints | Modular application organization |
| H2 2.3.232 / PostgreSQL JDBC 42.7.2 | SQLite (built-in) / psycopg2-binary 2.9.10 | Database drivers |
| AWS SDK for Java | boto3 1.36.7 | S3 BlobStore integration |
| SnakeYAML | PyYAML 6.0.2 | YAML configuration file parsing |
| Shiro LDAP realm | python-ldap 3.4.4 | LDAP/Active Directory authentication |

---

## Six-Phase Startup Sequence

The application follows a strict six-phase startup sequence implemented in
`src/startup.py`. Each phase must complete successfully before the next phase
begins. **No application services may begin accepting requests until all six phases
complete successfully.**

```
Phase 1: KERNEL
  └── Verify Python 3.13 runtime environment
  └── Load configuration from YAML files and environment variables
  └── Initialize structured JSON logging
  └── Create Flask application instance

Phase 2: SCHEMAS
  └── Create SQLAlchemy database engine (SQLite or PostgreSQL)
  └── Run Alembic database migrations (upgrade to head)
  └── Verify schema integrity and table existence

Phase 3: STORAGE
  └── Initialize configured BlobStore backends (File and/or S3)
  └── Verify BlobStore connectivity and write permissions
  └── Create default BlobStore if none exists

Phase 4: SECURITY
  └── Configure multi-realm authentication chain
  │     (Local → Bearer → JWT → LDAP → SAML/OIDC)
  └── Initialize RBAC evaluator with role/privilege resolution
  └── Initialize CSEL (Content Selector Expression Language) parser
  └── Create default admin user if first startup

Phase 5: CAPABILITIES
  └── Register format handlers (Maven, npm, Docker, NuGet, PyPI, APT, Raw)
  └── Load plugins from configured directories and entry points
  └── Initialize repository type handlers (Hosted, Proxy, Group)
  └── Start existing repositories (transition from STOPPED to STARTED)

Phase 6: SERVICES
  └── Start APScheduler with SQLAlchemy persistent job store
  └── Register scheduled tasks (cleanup, maintenance, health checks)
  └── Start health check monitoring
  └── Initialize Prometheus metrics collection
  └── Begin accepting HTTP requests via Gunicorn
```

### Startup Failure Handling

If any phase fails during startup:

1. The application logs the failure with full context (phase name, error details,
   stack trace)
2. All previously initialized components are cleaned up in reverse order
3. The application exits with a non-zero exit code
4. Health check endpoints return `503 Service Unavailable` during startup

This ensures the application never enters a partially-initialized state where some
services accept requests while others remain unconfigured.

---

## Event System

The application uses **blinker** (Flask's native signal library) to implement an
in-process event distribution system. This replaces the Guava EventBus from the
original Java implementation. Signals are defined in `src/signals.py` and provide
loose coupling between publishers and subscribers for cross-cutting concerns such
as audit logging, webhook dispatch, and search index updates.

### Signal Definitions

| Signal | Publisher(s) | Subscriber(s) |
|--------|-------------|---------------|
| `repository_created` | `RepositoryManager` (`src/repositories/services.py`) | `AuditService` (`src/security/audit.py`), `WebhookDispatcher` (`src/integration/webhooks.py`) |
| `repository_deleted` | `RepositoryManager` (`src/repositories/services.py`) | `AuditService`, `WebhookDispatcher`, `SearchService` (`src/repositories/search.py`) |
| `component_uploaded` | `HostedRepository` (`src/repositories/types/hosted.py`) | `SearchService`, `AuditService`, `WebhookDispatcher` |
| `component_deleted` | `CleanupEngine` (`src/storage/cleanup.py`) | `SearchService`, `AuditService` |
| `auth_success` | `AuthChain` (`src/security/auth/chain.py`) | `AuditService` |
| `auth_failure` | `AuthChain` (`src/security/auth/chain.py`) | `AuditService` |
| `config_changed` | `SystemConfig` (`src/admin/system_config.py`) | `AuditService`, `WebhookDispatcher` |
| `task_completed` | `Scheduler` (`src/admin/scheduler.py`) | `AuditService` |
| `user_modified` | `SecurityRoutes` (`src/security/routes.py`) | `AuditService`, `WebhookDispatcher` |
| `role_modified` | `SecurityRoutes` (`src/security/routes.py`) | `AuditService` |

### Signal Usage Pattern

```python
# Publisher (in src/repositories/services.py):
from src.signals import repository_created

class RepositoryManager:
    def create(self, config):
        repo = Repository(**config)
        db.session.add(repo)
        db.session.commit()
        repository_created.send(self, repository=repo)

# Subscriber (in src/security/audit.py):
from src.signals import repository_created

def on_repository_created(sender, **kwargs):
    repo = kwargs['repository']
    log_audit_event('REPOSITORY_CREATED', attributes={'name': repo.name})

repository_created.connect(on_repository_created)
```

---

## Authentication Chain

The application implements a **FirstSuccessfulAuthenticator** pattern that evaluates
multiple authentication realms in sequence. The first realm that successfully
authenticates the request terminates the chain. This replaces the Apache Shiro
2.0.0 `FirstSuccessfulModularRealmAuthenticator` from the original Java system.

### Authentication Flow

```
┌─────────────────────────────────────────────────────────┐
│                    Request Entry                         │
│  (Web UI Login / REST API Call / Format Protocol Auth)   │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│              Extract Credentials                         │
│  (Authorization header / Form POST / Session cookie)     │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│  Realm 1: Local Realm (src/security/auth/local_realm.py)│
│  • Query User model via SQLAlchemy                       │
│  • Verify bcrypt password hash                           │
│  • Check account status (active/disabled/locked)         │
├─────────────────────────────────────────────────────────┤
│  Success → Proceed to RBAC    │    Failure ↓             │
└───────────────────────────────┼──────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────┐
│  Realm 2: Bearer Realm (src/security/auth/bearer_realm.py)│
│  • Extract Bearer token from Authorization header        │
│  • Validate API key against stored tokens                │
│  • Resolve associated user account                       │
├─────────────────────────────────────────────────────────┤
│  Success → Proceed to RBAC    │    Failure ↓             │
└───────────────────────────────┼──────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────┐
│  Realm 3: JWT Realm (src/security/auth/jwt_handler.py)   │
│  • Decode JWT token with PyJWT (RS256/HS256)             │
│  • Validate expiration, issuer, and audience claims      │
│  • Extract role claims for authorization                 │
├─────────────────────────────────────────────────────────┤
│  Success → Proceed to RBAC    │    Failure ↓             │
└───────────────────────────────┼──────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────┐
│  Realm 4: LDAP Realm (src/security/auth/ldap_realm.py)   │
│  • Perform LDAP bind via python-ldap                     │
│  • Search with configurable base DN and user filter      │
│  • Map LDAP groups to application roles                  │
├─────────────────────────────────────────────────────────┤
│  Success → Proceed to RBAC    │    Failure ↓             │
└───────────────────────────────┼──────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────┐
│  Realm 5: SAML/OIDC Realm (src/security/auth/saml_realm.py)│
│  • Process SAML assertions or OIDC token exchange        │
│  • Validate signatures and claims                        │
│  • Map federated identity to local user                  │
├─────────────────────────────────────────────────────────┤
│  Success → Proceed to RBAC    │    All Failed ↓          │
└───────────────────────────────┼──────────────────────────┘
                                │
                                ▼
                   ┌──────────────────────┐
                   │  401 Unauthorized     │
                   │  (Aggregated failure  │
                   │   reasons returned)   │
                   └──────────────────────┘
```

### Key Behaviors

- **First Success Wins:** The chain stops at the first realm that successfully
  authenticates the request. Remaining realms are not evaluated.
- **Failure Aggregation:** If all realms fail, the `AuthenticationError` includes
  failure reasons from each realm for diagnostic purposes.
- **Signal Emission:** Both `auth_success` and `auth_failure` signals are emitted
  for audit logging regardless of outcome.
- **Anonymous Access:** Unauthenticated requests to protected endpoints receive
  `401 Unauthorized` — never `403 Forbidden`. The `403` status is reserved for
  authenticated users lacking required permissions.

---

## Three-Tier RBAC

After successful authentication, every protected operation is authorized through a
**three-tier Role-Based Access Control (RBAC)** model. This replaces the Shiro
permission resolution from the original Java system. The RBAC evaluator is
implemented in `src/security/rbac.py` and is invoked by the security decorators
(`@requires_auth`, `@requires_role`, `@requires_privilege`) in
`src/security/decorators.py`.

### Tier 1 — System Privileges

Global permissions that apply across the entire application:

- `nx-all` — Full administrative access to all functions
- `nx-repository-admin` — Manage all repositories
- `nx-security-all` — Manage all security settings (users, roles, privileges)
- `nx-tasks-all` — Manage all scheduled tasks
- `nx-settings` — Modify system configuration

System privileges are resolved from the user's assigned roles. A user with the
`nx-admin` role inherits the `nx-all` privilege.

### Tier 2 — Repository Permissions

Per-repository permissions scoped to specific repository instances:

- `repository-read` — Download artifacts and browse content
- `repository-write` — Upload, modify, and delete artifacts
- `repository-admin` — Manage repository configuration and lifecycle

Repository permissions are assigned through roles with repository-scoped privilege
definitions. A privilege specifies both the permission type and the target
repository (by name or wildcard pattern).

### Tier 3 — CSEL (Content Selector Expression Language)

Sub-repository path-based access control using Content Selector expressions:

- CSEL expressions define fine-grained access rules within a repository
- Expressions evaluate against component path, format, namespace, and attributes
- The CSEL parser (`src/security/csel.py`) tokenizes the expression, builds an
  AST (Abstract Syntax Tree), and evaluates it against the request context

**Example CSEL expression:**
```
format == "maven2" AND path =^ "/org/apache/"
```
This expression restricts access to Maven artifacts under the `/org/apache/`
namespace.

### Authorization Evaluation Order

1. **Resolve user roles** from role assignments
2. **Aggregate privileges** from all assigned roles
3. **Evaluate system privileges** — if the operation requires a system privilege
   and the user has it, grant access
4. **Evaluate repository permissions** — if the operation targets a specific
   repository, check repository-scoped privileges
5. **Evaluate CSEL expressions** — if repository privileges include content
   selectors, evaluate the CSEL expression against the request context
6. **Deny by default** — if no privilege grants access, return `403 Forbidden`

---

## Deployment Models

The application supports three deployment models, each combining different backend
technologies for database, blob storage, and search.

### Standalone

- **Database:** SQLite (single-file, zero-configuration)
- **BlobStore:** File BlobStore (local filesystem)
- **Search:** Whoosh (pure-Python, embedded)
- **Use case:** Development, evaluation, small teams
- **See:** [Standalone Deployment Guide](deployment/standalone.md)

### Clustered

- **Database:** PostgreSQL (shared, multi-node)
- **BlobStore:** Shared filesystem (NFS/GlusterFS)
- **Search:** Elasticsearch 8.x (shared cluster)
- **Use case:** High-availability production environments
- **See:** [Clustered Deployment Guide](deployment/clustered.md)

### Container-Native

- **Database:** PostgreSQL (managed service or pod)
- **BlobStore:** S3 BlobStore (AWS S3, MinIO, or S3-compatible)
- **Search:** Elasticsearch 8.x (managed service or pod)
- **Server:** Gunicorn with configurable worker processes
- **Use case:** Docker, Kubernetes, cloud-native environments
- **See:** [Container Deployment Guide](deployment/container.md)

### Deployment Configuration

All deployment-model-specific settings are controlled via environment variables
and YAML configuration files. The configuration loader (`src/config.py`) merges
values from:

1. `config/default.yaml` — Base defaults for all settings
2. `config/{environment}.yaml` — Environment-specific overrides
   (`development.yaml`, `production.yaml`, `testing.yaml`)
3. Environment variables — Highest priority overrides (essential for
   container-native deployments)

---

## Repository Lifecycle State Machine

Repositories follow a strict lifecycle state machine implemented in
`src/repositories/services.py`. State transitions are validated — **invalid
transitions raise `ConfigError`**.

```
                ┌──────────┐
                │   NEW    │
                └────┬─────┘
                     │ create()
                     ▼
             ┌──────────────┐
             │ INITIALIZING │
             └──┬───────┬───┘
                │       │
        success │       │ failure
                ▼       ▼
         ┌─────────┐ ┌────────┐
         │ STARTED │ │ FAILED │
         └──┬──┬───┘ └───┬────┘
            │  │          │ retry()
   stop()   │  │  start() │
            ▼  │          │
         ┌─────────┐     │
         │ STOPPED │◄────┘
         └────┬────┘
              │ delete()
              ▼
         ┌─────────┐
         │ DELETED │
         └─────────┘
```

### Valid State Transitions

| From | To | Trigger | Description |
|------|----|---------|-------------|
| `NEW` | `INITIALIZING` | `create()` | Repository creation begins |
| `INITIALIZING` | `STARTED` | Success | Initialization completes, repository is online |
| `INITIALIZING` | `FAILED` | Failure | Initialization error (storage unavailable, config invalid) |
| `STARTED` | `STOPPED` | `stop()` | Repository taken offline for maintenance |
| `STOPPED` | `STARTED` | `start()` | Repository brought back online |
| `STOPPED` | `DELETED` | `delete()` | Repository permanently removed |
| `FAILED` | `STOPPED` | `retry()` | Failed repository moved to stopped for recovery |

### Invalid Transitions

Any state transition not listed above is invalid and raises a `ConfigError`. For
example:

- `STARTED` → `DELETED` (must stop first)
- `DELETED` → `STARTED` (deleted repositories cannot be restarted)
- `NEW` → `STARTED` (must go through initialization)

---

## Supported Repository Formats

The application supports seven binary artifact formats, each with protocol-native
endpoints that comply with the format's specification. Every format supports three
repository types: **Hosted**, **Proxy**, and **Group**.

### Maven (F-101)

- **Protocol endpoints:** `/repository/{name}/**`
- **Features:** POM resolution, metadata XML generation (`maven-metadata.xml`),
  snapshot versioning, checksum verification (MD5, SHA-1, SHA-256)
- **Format handler:** `src/repositories/formats/maven.py`
- **Client compatibility:** Maven CLI, Gradle, sbt, Ivy

### npm (F-101)

- **Protocol endpoints:** `/repository/{name}/**` and `/repository/{name}/-/**`
- **Features:** Packument JSON serving, tarball storage and retrieval, scoped
  package support (`@scope/package`), full-text package search
- **Format handler:** `src/repositories/formats/npm.py`
- **Client compatibility:** npm CLI, yarn, pnpm

### Docker (F-101)

- **Protocol endpoints:** `/v2/{name}/**`
- **Features:** Docker Registry HTTP API V2 compliance, manifest operations
  (v2s2, OCI image manifest), blob upload/download (chunked and monolithic),
  tag listing, catalog API
- **Format handler:** `src/repositories/formats/docker.py`
- **Client compatibility:** Docker CLI, Podman, containerd, Buildah

### NuGet (F-101)

- **Protocol endpoints:** `/repository/{name}/index.json` and sub-resources
- **Features:** V3 service index, package registration, flat container for
  package content, full-text search
- **Format handler:** `src/repositories/formats/nuget.py`
- **Client compatibility:** dotnet CLI, NuGet CLI, Visual Studio

### PyPI (F-101)

- **Protocol endpoints:** `/repository/{name}/simple/**`
- **Features:** PEP 503 Simple Repository API, package file upload (sdist and
  wheel), package index generation
- **Format handler:** `src/repositories/formats/pypi.py`
- **Client compatibility:** pip, twine, poetry

### APT (F-101)

- **Protocol endpoints:** `/repository/{name}/**`
- **Features:** Packages and Sources index files, Release and InRelease
  (GPG-signed) metadata, `.deb` package upload and distribution
- **Format handler:** `src/repositories/formats/apt.py`
- **Client compatibility:** apt-get, aptitude

### Raw (F-101)

- **Protocol endpoints:** `/repository/{name}/**`
- **Features:** Generic binary content storage with path-based access, no
  format-specific metadata processing
- **Format handler:** `src/repositories/formats/raw.py`
- **Client compatibility:** curl, wget, any HTTP client

### Repository Types

Each format can be configured as one of three repository types:

| Type | Implementation | Behavior |
|------|---------------|----------|
| **Hosted** | `src/repositories/types/hosted.py` | Direct artifact upload and storage. Content is stored locally in the configured BlobStore. |
| **Proxy** | `src/repositories/types/proxy.py` | Caches artifacts from upstream registries (Maven Central, npmjs.com, Docker Hub, etc.). Supports TTL-based content age validation and negative caching. |
| **Group** | `src/repositories/types/group.py` | Virtual aggregation of multiple hosted and proxy repositories. Provides a single endpoint that searches across ordered member repositories. |

---

## Further Reading

- [REST API Documentation](api/README.md) — Complete API endpoint reference
- [Configuration Reference](configuration.md) — All configuration settings
- [Standalone Deployment](deployment/standalone.md) — SQLite + File BlobStore setup
- [Clustered Deployment](deployment/clustered.md) — PostgreSQL + Shared FS setup
- [Container Deployment](deployment/container.md) — Docker and Kubernetes setup
