# Technical Specification

# 0. Agent Action Plan

## 0.1 Intent Clarification

### 0.1.1 Core Refactoring Objective

Based on the prompt, the Blitzy platform understands that the refactoring objective is to **perform a complete tech stack migration** of the Sonatype Nexus Repository backend server from its current Java/Node.js architecture to **Python 3 using Flask**, while preserving all existing functionalities of the original project.

- **Refactoring type:** Tech stack migration (Java/Node.js → Python 3 / Flask)
- **Target repository:** Same repository (greenfield implementation — the repository is currently empty, containing only `README.md`)
- **Refactoring goals with enhanced clarity:**
  - Rewrite the entire backend server layer — currently implemented in Java 21 with an embedded Jetty 12.0.5 HTTP server, RESTEasy 6.2.7 JAX-RS API framework, Apache Shiro 2.0.0 security, and OSGi/Karaf 4.4.4 module container — as a Python 3 Flask application
  - Preserve all 20 features across 5 functional categories (Repository Management, Storage Management, Security, Administration, Integration and Extensibility) as documented in the Feature Catalog (Section 2.1)
  - Maintain functional parity for all 67 testable functional requirements documented in Section 2.2
  - Replicate the comprehensive REST API surface (Feature F-501) with equivalent Flask route handlers and JSON serialization
  - Reproduce the multi-format repository support (Maven, npm, Docker, NuGet, PyPI, APT, Raw) for Hosted, Proxy, and Group repository types
  - Implement equivalent RBAC security with multi-backend authentication (local credentials, API keys, JWT tokens, LDAP/AD, SAML/OIDC)
  - Maintain the dual-database architecture (SQLite for standalone / PostgreSQL for clustered deployments) using SQLAlchemy as the ORM
  - Preserve pluggable BlobStore abstraction for both local filesystem and Amazon S3 storage backends
  - Replicate scheduled task execution, health monitoring, webhook integration, audit logging, and system configuration management
- **Implicit requirements surfaced:**
  - Maintain all public REST API contracts — endpoint paths, request/response schemas, HTTP methods, and status codes must remain functionally equivalent
  - Preserve the data model — all entity relationships (Repository, Component, Asset, User, Role, Privilege, Content Selector, Task, Audit Event, BlobStore Config, Cleanup Policy) must be faithfully represented
  - The Flask application must be deployable as a self-contained server (equivalent to the embedded Jetty model)
  - OpenAPI/Swagger documentation must be auto-generated for the new Flask API
  - All format-specific protocol handlers (Maven, npm, Docker Registry API v2, NuGet V3, PyPI PEP 503) must be reimplemented as Flask blueprints
  - The event system for audit logging and webhook dispatch must be recreated using Python equivalents (e.g., Blinker signals)

### 0.1.2 Technical Interpretation

This refactoring translates to the following technical transformation strategy:

**Architecture Mapping — Current to Target:**

| Current (Java/Node.js) | Target (Python 3 / Flask) | Transformation |
|---|---|---|
| Java 21 Runtime | Python 3.12+ | Complete language migration |
| Eclipse Jetty 12.0.5 (embedded HTTP) | Gunicorn 25.1.0 + Flask 3.1.3 | WSGI server replaces embedded servlet container |
| RESTEasy 6.2.7 (JAX-RS) | Flask Blueprints + flask-smorest | Flask routing replaces JAX-RS resource classes |
| Jackson 2.16.1 (JSON) | Marshmallow 3.x | Schema-based serialization |
| Swagger/OpenAPI 2.2.20 | flask-smorest (OpenAPI 3.x) | Auto-generated API documentation |
| Apache Shiro 2.0.0 (AuthN/AuthZ) | Flask-Login + Flask-HTTPAuth + PyJWT | Equivalent RBAC and multi-backend auth |
| OSGi/Karaf 4.4.4 (module container) | Flask Blueprints + Python packages | Module isolation via Python package structure |
| Google Guice 7.0.0 (DI) | dependency-injector or Flask app context | Python DI patterns |
| MyBatis 3.5.15 (ORM) | SQLAlchemy 2.x + Flask-SQLAlchemy 3.1.1 | Full ORM with Declarative Base |
| HikariCP 4.0.3 (connection pool) | SQLAlchemy built-in pooling | Connection pool via engine config |
| Flyway 8.5.13 (migrations) | Alembic + Flask-Migrate | Schema migration management |
| H2 2.3.232 (embedded DB) | SQLite (via SQLAlchemy) | Zero-config standalone database |
| PostgreSQL JDBC 42.7.2 | psycopg2-binary | PostgreSQL driver |
| Quartz 2.3.2 (scheduler) | APScheduler 3.x | Background task scheduling |
| Guava EventBus | Blinker (Flask signals) | Decoupled event dispatch |
| Dropwizard Metrics 4.2.25 | prometheus-flask-instrumentator | Metrics collection |
| Prometheus Client 0.16.0 | prometheus-client (Python) | Prometheus-compatible endpoints |
| SLF4J 1.7.36 + Logback 1.2.13 | Python logging module | Structured logging |
| Elasticsearch 2.4.3 | elasticsearch-py | Search client library |
| AWS SDK for Java | boto3 | S3 BlobStore integration |
| BouncyCastle 1.78.1 | cryptography (Python) | Certificate and crypto operations |
| Java-JWT 4.4.0 | PyJWT | JWT token management |
| Apache HttpClient 4.5.14 | requests / httpx | Outbound HTTP for proxy repos |
| React 18.2.0 + ExtJS 7.8.0 (UI) | Out of scope | Frontend not part of backend rewrite |
| Node.js v18.17.1 (frontend build) | Out of scope | Build toolchain not part of backend rewrite |

**Transformation Rules:**
- Every JAX-RS resource class maps to a Flask Blueprint with equivalent route handlers
- Every MyBatis mapper maps to a SQLAlchemy model with equivalent query methods
- Every Shiro realm maps to a Flask authentication backend
- Every OSGi bundle maps to a Python package/module within the Flask app
- Every Quartz scheduled task maps to an APScheduler job
- Every Guava EventBus subscriber maps to a Blinker signal receiver
- All JSON serialization via Jackson maps to Marshmallow schema definitions

## 0.2 Source Analysis

### 0.2.1 Comprehensive Source File Discovery

The repository is currently empty, containing only a single placeholder file:

```
Current Repository:
/
└── README.md (single heading: "# 24Feb_1")
```

There is **no existing source code** in the repository. The system to be rewritten is fully described in the Technical Specification document (Sections 1.1 through 9.4), which documents the Sonatype Nexus Repository platform — a universal binary repository manager implemented in Java 21 with a five-layer architecture (UI Framework, API Layer, Core Framework, Repository Layer, Storage Layer).

### 0.2.2 Source System Architecture (from Tech Spec)

The source system described in the specification is organized into the following logical component groups, each of which must be reimplemented in Python/Flask:

**API Layer Components (RESTEasy 6.2.7 → Flask Blueprints):**
- `RepositoryManagerRESTAdapter` — Repository CRUD operations REST API
- `SimpleApiResponse.java` — DTO for API responses
- REST API resource classes for repository management, component management, security management, and system management (F-501)
- Swagger/OpenAPI 2.2.20 auto-generated documentation

**Security Layer Components (Apache Shiro 2.0.0 → Flask Auth Extensions):**
- `NexusAuthenticationFilter` — Security filter chain entry point
- `FirstSuccessfulModularRealmAuthenticator` — Multi-realm authentication
- `AuthenticatingRealmImpl` — Local credential realm
- `BearerTokenRealm` — API key authentication realm
- `JwtSecurityFilter` — JWT token validation
- `SecurityComponent` — Privilege retrieval and CSEL expression evaluation

**Repository Layer Components (OSGi Bundles → Python Packages):**
- Format plugins for Maven, npm, Docker, NuGet, PyPI, APT, Raw
- `RepositoryImpl.java` — Repository lifecycle state machine
- `RepositoryManagerImpl.java` — Repository management with state transitions
- `UploadManagerImpl.java` — Artifact upload handling
- `HttpClientFacetImpl.java` — Proxy repository remote fetches
- `HttpClientManagerImpl.java` — HTTP client configuration
- `ProxyFacetSupport.java` — Proxy behavior base class
- Repository Facets for Proxy, Hosted, and Group types

**Storage Layer Components (MyBatis/HikariCP → SQLAlchemy):**
- DataStore API with MyBatis 3.5.15 mappers for all entity types
- HikariCP 4.0.3 connection pool management
- Flyway 8.5.13 schema migration scripts
- File BlobStore implementation (F-201)
- `S3BlobStore.java` — S3 storage implementation (F-202)

**Core Framework Components (Karaf/Guice → Flask App Context):**
- `Launcher.java` — System bootstrap and startup orchestration
- `NodeAccessBooter.java` — Core component initialization
- `CapabilityRegistryBooter.java` — Capability activation
- OSGi bundle lifecycle management
- Guice module wiring and dependency injection
- Guava EventBus for internal event dispatch

**Scheduling and Monitoring Components:**
- Quartz 2.3.2 task scheduling (F-402)
- `TaskLoggerHelper.java` — Per-task logging
- Dropwizard Metrics 4.2.25 + Prometheus Client 0.16.0 (F-401)
- Health check registry

**Logging and Diagnostics:**
- `LoggerFactory.java` — Structured logging
- SLF4J 1.7.36 + Logback 1.2.13 logging framework
- Support ZIP generation (F-403)

### 0.2.3 Data Model Entities (Source)

The following entities from the DataStore schema (Section 6.2.1.2) must be reimplemented as SQLAlchemy models:

| Entity | Primary Key | Key Fields | Feature Dependencies |
|---|---|---|---|
| REPOSITORY | name | format, type, blob_store_name, online, attributes | F-101, F-102, F-404 |
| COMPONENT | id | repository_name, namespace, name, version, attributes | F-101, F-103 |
| ASSET | id | component_id, path, content_type, checksum_sha1, size, last_downloaded, attributes | F-101, F-103, F-204 |
| USER | user_id | password_hash, status, email, attributes | F-301 |
| ROLE | role_id | name, description, privileges (JSON) | F-301 |
| PRIVILEGE | privilege_id | type, name, properties (JSON) | F-301 |
| CONTENT_SELECTOR | selector_id | name, type, expression | F-301 |
| ROLE_ASSIGNMENT | user_id + role_id | (junction table) | F-301 |
| TASK_DEFINITION | task_id | type, name, cron_expression, enabled, configuration (JSON) | F-402 |
| TASK_EXECUTION | execution_id | task_id, start_time, end_time, status, duration_ms | F-402 |
| SYSTEM_CONFIG | key | value, category | F-404 |
| AUDIT_EVENT | event_id | user_id, event_type, timestamp, payload (JSON) | F-303 |
| BLOBSTORE_CONFIG | blob_store_name | type, configuration (JSON), total_size | F-201, F-202 |
| CLEANUP_POLICY | policy_id | name, format, criteria (JSON) | F-204 |

### 0.2.4 Feature Inventory (All 20 Features to Reimplement)

| ID | Feature Name | Category | Priority |
|---|---|---|---|
| F-101 | Multi-Format Repository Support | Repository Management | Critical |
| F-102 | Repository Types (Hosted, Proxy, Group) | Repository Management | Critical |
| F-103 | Content Indexing and Search | Repository Management | High |
| F-104 | Browse Tree Navigation | Repository Management | Medium |
| F-201 | File BlobStore | Storage Management | Critical |
| F-202 | S3 BlobStore | Storage Management | High |
| F-203 | BlobStore Maintenance Tasks | Storage Management | High |
| F-204 | Cleanup Policies | Storage Management | High |
| F-301 | Role-Based Access Control | Security | Critical |
| F-302 | SSL/TLS Support | Security | High |
| F-303 | Audit Logging | Security | High |
| F-304 | API Key Authentication | Security | Medium |
| F-401 | Health Checks and Monitoring | Administration | High |
| F-402 | Scheduled Tasks | Administration | High |
| F-403 | Support ZIP Generation | Administration | Medium |
| F-404 | System Configuration Management | Administration | Critical |
| F-501 | REST API | Integration | Critical |
| F-502 | Scripting Support | Integration | High |
| F-503 | Webhook Integration | Integration | Medium |
| F-504 | Plugin Architecture | Integration | High |

## 0.3 Scope Boundaries

### 0.3.1 Exhaustively In Scope

**Source Transformations (All backend server logic → Python/Flask):**
- `src/app/**/*.py` — All Flask application modules (new)
- `src/app/api/**/*.py` — REST API route handlers and blueprints
- `src/app/models/**/*.py` — SQLAlchemy data models
- `src/app/services/**/*.py` — Business logic service layer
- `src/app/auth/**/*.py` — Authentication and authorization modules
- `src/app/repositories/**/*.py` — Repository management logic (Hosted, Proxy, Group)
- `src/app/formats/**/*.py` — Format-specific handlers (Maven, npm, Docker, NuGet, PyPI, APT, Raw)
- `src/app/storage/**/*.py` — BlobStore implementations (File and S3)
- `src/app/scheduler/**/*.py` — Background task scheduling (APScheduler)
- `src/app/events/**/*.py` — Event system (Blinker signals)
- `src/app/monitoring/**/*.py` — Health checks and metrics
- `src/app/search/**/*.py` — Elasticsearch integration
- `src/app/security/**/*.py` — SSL/TLS certificate management
- `src/app/config/**/*.py` — System configuration management
- `src/app/webhooks/**/*.py` — Webhook integration
- `src/app/scripts/**/*.py` — Scripting support engine
- `src/app/plugins/**/*.py` — Plugin architecture framework
- `src/app/utils/**/*.py` — Shared utilities and helpers

**Test Suite:**
- `tests/**/*test*.py` — Unit tests using pytest
- `tests/integration/**/*.py` — Integration tests
- `tests/conftest.py` — Shared test fixtures and configuration

**Configuration Files:**
- `config/*.py` — Application configuration (development, production, testing)
- `config/*.yaml` — Environment-specific settings
- `.env.example` — Environment variable template
- `logging.conf` — Logging configuration

**Documentation Updates:**
- `README.md` — Complete rewrite with Python/Flask setup and usage instructions
- `docs/api/*.md` — API documentation references
- `docs/deployment/*.md` — Deployment guide for Python/Flask stack
- `docs/architecture/*.md` — Architecture documentation

**Dependency and Build Configuration:**
- `requirements.txt` — Python dependency manifest
- `pyproject.toml` — Project metadata and build configuration
- `setup.py` or `setup.cfg` — Package installation configuration
- `Dockerfile` — Docker containerization for Flask/Gunicorn
- `docker-compose.yml` — Multi-service orchestration (app + PostgreSQL + Elasticsearch)
- `gunicorn.conf.py` — Gunicorn production server configuration

**Database Migration Scripts:**
- `migrations/**/*.py` — Alembic migration scripts for schema management

**Import and Dependency Wiring:**
- Every Python module containing imports across the application package hierarchy

### 0.3.2 Explicitly Out of Scope

| Excluded Item | Rationale |
|---|---|
| **Frontend Web UI (React 18.2.0 + ExtJS 7.8.0)** | The user request is to rewrite the server — the frontend framework is a separate concern. The REST API will serve the existing or any future UI. |
| **Rapture Bridge Framework** | Internal UI bridge layer between React and ExtJS; not part of the server backend |
| **Node.js v18.17.1 / Yarn v1.22.19 build toolchain** | Frontend build pipeline only; the Python backend does not require Node.js tooling |
| **Webpack / Babel frontend bundling** | Frontend asset pipeline; excluded from backend rewrite |
| **Advanced Security Scanning (Sonatype Nexus IQ)** | Separate product per Assumption A-005; not part of repository manager |
| **Custom Repository Format Plugin Development** | Extension framework is in scope; building custom third-party format plugins is not |
| **Infrastructure Provisioning** | Cloud infrastructure setup (AWS, Azure, GCP) is out of scope per Section 1.3.2 |
| **Maven Build System** | Java-specific; replaced by Python packaging (pip/pyproject.toml) |
| **OSGi Bundle Metadata** | Java-specific module system; replaced by Python packages |
| **Java-specific runtime artifacts** | JVM configuration, GC tuning, JFR diagnostics — not applicable to Python |
| **Groovy scripting engine** | Java-specific; Python's native scripting capabilities will be used instead |
| **Spock testing framework** | Java-specific; replaced by pytest |

## 0.4 Target Design

### 0.4.1 Refactored Structure Planning

The target architecture transforms the Java modular monolith into a Python/Flask application with an equivalent layered structure, using Flask Blueprints for module isolation and SQLAlchemy for data persistence.

```
Target:
/
├── README.md
├── pyproject.toml
├── setup.py
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── .flaskenv
├── gunicorn.conf.py
├── Dockerfile
├── docker-compose.yml
├── logging.conf
├── wsgi.py
├── run.py
├── config/
│   ├── __init__.py
│   ├── default.py
│   ├── development.py
│   ├── production.py
│   └── testing.py
├── migrations/
│   ├── env.py
│   ├── alembic.ini
│   └── versions/
│       └── 001_initial_schema.py
├── src/
│   └── app/
│       ├── __init__.py
│       ├── extensions.py
│       ├── factory.py
│       ├── api/
│       │   ├── __init__.py
│       │   ├── repositories.py
│       │   ├── components.py
│       │   ├── assets.py
│       │   ├── users.py
│       │   ├── roles.py
│       │   ├── privileges.py
│       │   ├── tasks.py
│       │   ├── system.py
│       │   ├── search.py
│       │   ├── security.py
│       │   ├── blobstores.py
│       │   ├── cleanup.py
│       │   ├── scripts.py
│       │   ├── webhooks.py
│       │   └── health.py
│       ├── models/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── repository.py
│       │   ├── component.py
│       │   ├── asset.py
│       │   ├── user.py
│       │   ├── role.py
│       │   ├── privilege.py
│       │   ├── content_selector.py
│       │   ├── task.py
│       │   ├── audit_event.py
│       │   ├── blobstore_config.py
│       │   ├── cleanup_policy.py
│       │   └── system_config.py
│       ├── schemas/
│       │   ├── __init__.py
│       │   ├── repository.py
│       │   ├── component.py
│       │   ├── asset.py
│       │   ├── user.py
│       │   ├── role.py
│       │   ├── task.py
│       │   ├── system.py
│       │   └── search.py
│       ├── services/
│       │   ├── __init__.py
│       │   ├── repository_manager.py
│       │   ├── upload_manager.py
│       │   ├── proxy_service.py
│       │   ├── group_service.py
│       │   ├── search_service.py
│       │   ├── blobstore_service.py
│       │   ├── cleanup_service.py
│       │   ├── config_service.py
│       │   ├── script_service.py
│       │   └── support_zip_service.py
│       ├── auth/
│       │   ├── __init__.py
│       │   ├── authentication.py
│       │   ├── authorization.py
│       │   ├── realms/
│       │   │   ├── __init__.py
│       │   │   ├── local_realm.py
│       │   │   ├── bearer_token_realm.py
│       │   │   ├── jwt_realm.py
│       │   │   ├── ldap_realm.py
│       │   │   └── sso_realm.py
│       │   ├── rbac.py
│       │   ├── content_selector.py
│       │   └── password_utils.py
│       ├── formats/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── maven/
│       │   │   ├── __init__.py
│       │   │   ├── handler.py
│       │   │   └── metadata.py
│       │   ├── npm/
│       │   │   ├── __init__.py
│       │   │   ├── handler.py
│       │   │   └── metadata.py
│       │   ├── docker/
│       │   │   ├── __init__.py
│       │   │   ├── handler.py
│       │   │   └── registry_v2.py
│       │   ├── nuget/
│       │   │   ├── __init__.py
│       │   │   ├── handler.py
│       │   │   └── v3_api.py
│       │   ├── pypi/
│       │   │   ├── __init__.py
│       │   │   ├── handler.py
│       │   │   └── simple_api.py
│       │   ├── apt/
│       │   │   ├── __init__.py
│       │   │   ├── handler.py
│       │   │   └── metadata.py
│       │   └── raw/
│       │       ├── __init__.py
│       │       └── handler.py
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── blobstore.py
│       │   ├── file_blobstore.py
│       │   ├── s3_blobstore.py
│       │   └── maintenance.py
│       ├── repositories/
│       │   ├── __init__.py
│       │   ├── hosted.py
│       │   ├── proxy.py
│       │   ├── group.py
│       │   ├── facets.py
│       │   └── lifecycle.py
│       ├── scheduler/
│       │   ├── __init__.py
│       │   ├── task_scheduler.py
│       │   ├── task_registry.py
│       │   └── task_logger.py
│       ├── events/
│       │   ├── __init__.py
│       │   ├── event_bus.py
│       │   ├── subscribers.py
│       │   └── event_types.py
│       ├── monitoring/
│       │   ├── __init__.py
│       │   ├── health_checks.py
│       │   ├── metrics.py
│       │   └── prometheus_exporter.py
│       ├── search/
│       │   ├── __init__.py
│       │   ├── elasticsearch_client.py
│       │   ├── index_manager.py
│       │   └── query_builder.py
│       ├── security/
│       │   ├── __init__.py
│       │   ├── ssl_manager.py
│       │   ├── certificate_store.py
│       │   └── crypto_utils.py
│       ├── webhooks/
│       │   ├── __init__.py
│       │   ├── dispatcher.py
│       │   └── payload_signer.py
│       ├── plugins/
│       │   ├── __init__.py
│       │   ├── plugin_manager.py
│       │   └── extension_points.py
│       └── utils/
│           ├── __init__.py
│           ├── helpers.py
│           ├── validators.py
│           ├── http_client.py
│           └── file_utils.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── test_models.py
│   │   ├── test_services.py
│   │   ├── test_auth.py
│   │   ├── test_formats.py
│   │   ├── test_storage.py
│   │   └── test_scheduler.py
│   ├── integration/
│   │   ├── __init__.py
│   │   ├── test_api.py
│   │   ├── test_repository_lifecycle.py
│   │   ├── test_proxy.py
│   │   ├── test_search.py
│   │   └── test_blobstore.py
│   └── fixtures/
│       ├── sample_artifacts/
│       └── test_data.py
└── docs/
    ├── api/
    │   └── openapi.yaml
    ├── architecture/
    │   └── overview.md
    └── deployment/
        └── guide.md
```

### 0.4.2 Web Search Research Conducted

- **Flask 3.1.3** (released Feb 19, 2026) — Latest stable release; supports Python 3.9+; WSGI framework with Werkzeug and Jinja2
- **Gunicorn 25.1.0** — Production WSGI HTTP server with pre-fork worker model; supports sync, gevent async, and threaded workers
- **Flask-SQLAlchemy 3.1.1** — Flask integration for SQLAlchemy with Declarative Base support
- **Flask/Gunicorn deployment patterns** — Standard production stack: Gunicorn behind Nginx, with Docker containerization
- **Python equivalent libraries** — PyJWT for JWT, boto3 for AWS, APScheduler for background tasks, Marshmallow for serialization

### 0.4.3 Design Pattern Applications

| Pattern | Java Source | Python Target | Application |
|---|---|---|---|
| **Application Factory** | `Launcher.java` + Karaf bootstrap | `factory.py` with `create_app()` | Flask app factory for testable, configurable application creation |
| **Blueprint/Module** | OSGi bundles | Flask Blueprints | Each feature area (API, formats, auth) as a registered Blueprint |
| **Repository Pattern** | DataStore API (MyBatis mappers) | SQLAlchemy models + service classes | Data access abstraction per entity |
| **Service Layer** | Format-specific service components | `services/` package | Business logic separated from route handlers |
| **Strategy Pattern** | BlobStore interface (File vs S3) | `storage/blobstore.py` abstract base | Pluggable storage backends |
| **Chain of Responsibility** | Shiro realm authentication chain | `auth/authentication.py` realm iteration | Multi-backend auth with first-successful strategy |
| **Observer Pattern** | Guava EventBus | Blinker signals | Decoupled event dispatch for audit logging and webhooks |
| **State Machine** | Repository lifecycle StateGuard | `repositories/lifecycle.py` | Repository state transitions (NEW → STARTED → STOPPED) |
| **Factory Pattern** | Repository recipes | Format handler factory | Repository creation with format-specific configuration |
| **Facade Pattern** | `RepositoryManagerImpl` | `services/repository_manager.py` | Unified interface for repository operations |

### 0.4.4 User Interface Design

The backend rewrite does not include the Web UI (React 18.2.0 / ExtJS 7.8.0), which is explicitly out of scope. The Flask backend must expose the same REST API contracts so that any existing or future frontend can integrate without modification. Key design considerations for the API surface:

- All REST API endpoints must use JSON as the primary content type (equivalent to Jackson 2.16.1 serialization)
- OpenAPI/Swagger documentation must be auto-generated and accessible at a well-known URL
- CORS headers must be configurable for frontend integration
- WebSocket support may be added for real-time event streaming if needed in the future

## 0.5 Transformation Mapping

### 0.5.1 File-by-File Transformation Plan

Since the repository is empty (greenfield), all target files are marked **CREATE**. The "Source File" column references the Java source component or tech spec section that defines the functional requirements the new file must fulfill.

**Core Application Files:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| README.md | UPDATE | README.md | Complete rewrite with Python/Flask project description, setup, and usage |
| pyproject.toml | CREATE | (new — replaces pom.xml build system) | Python project metadata, dependencies, build config |
| setup.py | CREATE | (new — replaces Maven build) | Package installation configuration |
| requirements.txt | CREATE | (new — replaces Maven dependency management) | All Python runtime dependencies |
| requirements-dev.txt | CREATE | (new) | Development and testing dependencies |
| .env.example | CREATE | (new — system config template) | Environment variable template for all config |
| .flaskenv | CREATE | (new) | Flask CLI environment settings |
| gunicorn.conf.py | CREATE | Jetty 12.0.5 config (Section 5.2.2) | Production WSGI server configuration |
| Dockerfile | CREATE | Docker Maven Plugin config (Section 3.6) | Python 3.12 + Gunicorn container image |
| docker-compose.yml | CREATE | Deployment config (Section 8.3) | Flask app + PostgreSQL + Elasticsearch services |
| logging.conf | CREATE | Logback config (SLF4J 1.7.36 + Logback 1.2.13) | Python logging with structured JSON output |
| wsgi.py | CREATE | Launcher.java (Section 6.1.6) | WSGI entry point for Gunicorn |
| run.py | CREATE | Launcher.java (Section 6.1.6) | Development server entry point |

**Configuration Module:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| config/__init__.py | CREATE | System Configuration (F-404) | Config module initialization |
| config/default.py | CREATE | System Configuration (F-404) | Base configuration with all defaults |
| config/development.py | CREATE | H2 standalone config | Development settings (SQLite, debug mode) |
| config/production.py | CREATE | PostgreSQL clustered config | Production settings (PostgreSQL, Gunicorn) |
| config/testing.py | CREATE | Test configuration | Test settings (in-memory SQLite) |

**Database Migrations:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| migrations/env.py | CREATE | Flyway 8.5.13 config | Alembic migration environment |
| migrations/alembic.ini | CREATE | Flyway config | Alembic configuration file |
| migrations/versions/001_initial_schema.py | CREATE | DataStore schema (Section 6.2.1.2) | Initial schema with all 14 entity tables |

**Application Core:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| src/app/__init__.py | CREATE | OSGi/Karaf container init | Package initialization |
| src/app/extensions.py | CREATE | Guice module wiring | Flask extension initialization (db, migrate, jwt, etc.) |
| src/app/factory.py | CREATE | Launcher.java + NodeAccessBooter.java | Flask application factory with blueprint registration |

**REST API Blueprints (replacing JAX-RS resource classes):**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| src/app/api/__init__.py | CREATE | RESTEasy 6.2.7 router | API blueprint registration |
| src/app/api/repositories.py | CREATE | RepositoryManagerRESTAdapter (F-501-RQ-001) | Repository CRUD endpoints |
| src/app/api/components.py | CREATE | Component management API (F-501-RQ-002) | Component upload/download/delete endpoints |
| src/app/api/assets.py | CREATE | Asset management (F-501-RQ-002) | Asset operations endpoints |
| src/app/api/users.py | CREATE | Security management API (F-501-RQ-003) | User CRUD endpoints |
| src/app/api/roles.py | CREATE | Security management API (F-501-RQ-003) | Role and privilege management |
| src/app/api/privileges.py | CREATE | Security management API (F-501-RQ-003) | Privilege descriptor management |
| src/app/api/tasks.py | CREATE | System management API (F-501-RQ-004) | Task scheduling and management |
| src/app/api/system.py | CREATE | System management API (F-501-RQ-004) | System configuration endpoints |
| src/app/api/search.py | CREATE | Search endpoints (F-103) | Full-text search API |
| src/app/api/security.py | CREATE | SSL/TLS management (F-302) | Certificate management API |
| src/app/api/blobstores.py | CREATE | BlobStore management (F-201, F-202) | BlobStore CRUD and configuration |
| src/app/api/cleanup.py | CREATE | Cleanup policy management (F-204) | Cleanup policy CRUD and preview |
| src/app/api/scripts.py | CREATE | Script management (F-502) | Script CRUD and execution |
| src/app/api/webhooks.py | CREATE | Webhook management (F-503) | Webhook configuration endpoints |
| src/app/api/health.py | CREATE | Health check API (F-401) | Health status and metrics endpoints |

**Data Models (replacing MyBatis 3.5.15 mappers):**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| src/app/models/__init__.py | CREATE | DataStore API | Model package initialization |
| src/app/models/base.py | CREATE | MyBatis base mapper | SQLAlchemy DeclarativeBase with common mixins |
| src/app/models/repository.py | CREATE | REPOSITORY entity (Section 6.2.1.2) | Repository model with format, type, blob_store fields |
| src/app/models/component.py | CREATE | COMPONENT entity | Component model with coordinates (namespace, name, version) |
| src/app/models/asset.py | CREATE | ASSET entity | Asset model with path, checksum, size, last_downloaded |
| src/app/models/user.py | CREATE | USER entity | User model with password_hash and status |
| src/app/models/role.py | CREATE | ROLE + ROLE_ASSIGNMENT entities | Role model with privilege aggregation |
| src/app/models/privilege.py | CREATE | PRIVILEGE entity | Privilege descriptor model |
| src/app/models/content_selector.py | CREATE | CONTENT_SELECTOR entity | CSEL expression model |
| src/app/models/task.py | CREATE | TASK_DEFINITION + TASK_EXECUTION | Task scheduling and execution models |
| src/app/models/audit_event.py | CREATE | AUDIT_EVENT entity | Audit log entry model |
| src/app/models/blobstore_config.py | CREATE | BLOBSTORE_CONFIG entity | BlobStore configuration model |
| src/app/models/cleanup_policy.py | CREATE | CLEANUP_POLICY entity | Cleanup policy rule model |
| src/app/models/system_config.py | CREATE | SYSTEM_CONFIG entity | System settings key-value model |

**Serialization Schemas (replacing Jackson 2.16.1):**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| src/app/schemas/*.py | CREATE | SimpleApiResponse.java + Jackson DTOs | Marshmallow schemas for all API request/response objects |

**Service Layer (replacing Java service components):**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| src/app/services/repository_manager.py | CREATE | RepositoryManagerImpl.java | Repository CRUD, lifecycle, and state management |
| src/app/services/upload_manager.py | CREATE | UploadManagerImpl.java | Artifact upload with format validation |
| src/app/services/proxy_service.py | CREATE | HttpClientFacetImpl.java + ProxyFacetSupport.java | Proxy fetch with caching and negative cache |
| src/app/services/group_service.py | CREATE | Group facet resolution logic | Ordered member resolution for group repositories |
| src/app/services/search_service.py | CREATE | Elasticsearch integration (F-103) | Search indexing and query execution |
| src/app/services/blobstore_service.py | CREATE | BlobStore API | Storage abstraction service |
| src/app/services/cleanup_service.py | CREATE | Cleanup evaluators (F-204) | Format-specific cleanup rule execution |
| src/app/services/config_service.py | CREATE | Configuration management (F-404) | System settings persistence and propagation |
| src/app/services/script_service.py | CREATE | Script plugin (F-502) | Script storage and execution engine |
| src/app/services/support_zip_service.py | CREATE | Support ZIP (F-403) | Diagnostic bundle generation with sanitization |

**Authentication and Authorization (replacing Apache Shiro 2.0.0):**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| src/app/auth/authentication.py | CREATE | NexusAuthenticationFilter + FirstSuccessfulModularRealmAuthenticator | Multi-realm authentication chain |
| src/app/auth/authorization.py | CREATE | SecurityComponent + RBAC framework | Three-tier authorization (system, repo, sub-repo) |
| src/app/auth/realms/local_realm.py | CREATE | AuthenticatingRealmImpl | Local username/password authentication |
| src/app/auth/realms/bearer_token_realm.py | CREATE | BearerTokenRealm | API key authentication |
| src/app/auth/realms/jwt_realm.py | CREATE | JwtSecurityFilter | JWT token validation |
| src/app/auth/realms/ldap_realm.py | CREATE | Shiro LDAP realm | LDAP/AD directory integration |
| src/app/auth/realms/sso_realm.py | CREATE | Security plugin SAML/OIDC | SSO federation support |
| src/app/auth/rbac.py | CREATE | Privilege descriptors + roles | RBAC enforcement logic |
| src/app/auth/content_selector.py | CREATE | CSEL expression evaluator | Content selector expression parsing and evaluation |
| src/app/auth/password_utils.py | CREATE | BouncyCastle credential hashing | Secure password hashing utilities |

**Format Handlers (replacing OSGi format plugin bundles):**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| src/app/formats/base.py | CREATE | Format plugin contract (F-504) | Abstract base class for format handlers |
| src/app/formats/maven/**/*.py | CREATE | Maven format bundle (F-101-RQ-001) | Maven repository protocol, POM parsing, coordinate resolution |
| src/app/formats/npm/**/*.py | CREATE | npm format bundle (F-101-RQ-004) | npm registry protocol, package.json handling |
| src/app/formats/docker/**/*.py | CREATE | Docker format bundle (F-101-RQ-005) | Docker Registry API v2, manifest and layer operations |
| src/app/formats/nuget/**/*.py | CREATE | NuGet format bundle (F-101-RQ-006) | NuGet V3 API, service index, package endpoints |
| src/app/formats/pypi/**/*.py | CREATE | PyPI format bundle (F-101-RQ-007) | PEP 503 Simple Repository API |
| src/app/formats/apt/**/*.py | CREATE | APT format bundle (F-101-RQ-003) | Debian package index, GPG signature validation |
| src/app/formats/raw/**/*.py | CREATE | Raw format bundle (F-101-RQ-002) | Arbitrary binary file handling with MIME detection |

**Storage Backends (replacing BlobStore subsystem):**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| src/app/storage/blobstore.py | CREATE | BlobStore API interface | Abstract BlobStore base class |
| src/app/storage/file_blobstore.py | CREATE | File BlobStore (F-201) | Local filesystem with atomic writes and soft-delete |
| src/app/storage/s3_blobstore.py | CREATE | S3BlobStore.java (F-202) | AWS S3 with SSE encryption and multipart uploads |
| src/app/storage/maintenance.py | CREATE | BlobStore Maintenance (F-203) | Compaction, temp cleanup, integrity checks |

**Remaining Modules:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| src/app/repositories/*.py | CREATE | Repository facets (Section 5.2.4) | Hosted, Proxy, Group repository type logic |
| src/app/scheduler/*.py | CREATE | Quartz 2.3.2 (F-402) | APScheduler-based task scheduling |
| src/app/events/*.py | CREATE | Guava EventBus (Section 5.2.8) | Blinker signal-based event system |
| src/app/monitoring/*.py | CREATE | Metrics stack (F-401) | Health checks, Prometheus metrics |
| src/app/search/*.py | CREATE | Elasticsearch 2.4.3 (F-103) | ES client, index management, queries |
| src/app/security/*.py | CREATE | SSL/TLS support (F-302) | Certificate management using cryptography lib |
| src/app/webhooks/*.py | CREATE | Webhook integration (F-503) | HTTP POST dispatch with HMAC signing |
| src/app/plugins/*.py | CREATE | Plugin architecture (F-504) | Extension point framework |
| src/app/utils/*.py | CREATE | Apache Commons + Guava utilities | Shared helpers, validators, HTTP client wrapper |

**Tests:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| tests/conftest.py | CREATE | Test configuration | Shared fixtures (test app, test DB, mock services) |
| tests/unit/test_models.py | CREATE | DataStore entity tests | SQLAlchemy model unit tests |
| tests/unit/test_services.py | CREATE | Service layer tests | Business logic unit tests |
| tests/unit/test_auth.py | CREATE | Security realm tests | Authentication and authorization tests |
| tests/unit/test_formats.py | CREATE | Format handler tests | Format-specific protocol tests |
| tests/unit/test_storage.py | CREATE | BlobStore tests | File and S3 storage backend tests |
| tests/unit/test_scheduler.py | CREATE | Quartz task tests | Scheduled task execution tests |
| tests/integration/test_api.py | CREATE | REST API integration tests | Full API endpoint integration tests |
| tests/integration/test_repository_lifecycle.py | CREATE | Repository lifecycle tests | State machine transition tests |
| tests/integration/test_proxy.py | CREATE | Proxy fetch tests | Remote artifact retrieval tests |
| tests/integration/test_search.py | CREATE | Search integration tests | Elasticsearch query tests |
| tests/integration/test_blobstore.py | CREATE | BlobStore integration tests | File and S3 storage tests |

### 0.5.2 Cross-File Dependencies

**Import Transformation Rules:**
- All internal imports use the `src.app` package hierarchy
- Flask Blueprints are registered in `src/app/factory.py` via `create_app()`
- SQLAlchemy models are imported into `src/app/models/__init__.py` for unified access
- Marshmallow schemas mirror model hierarchy in `src/app/schemas/`
- Service classes receive dependencies via Flask app context or constructor injection

**Configuration Propagation:**
- `.env` file loaded by `python-dotenv` → Flask config → Gunicorn config
- Database URL resolved from environment → SQLAlchemy engine
- Elasticsearch URL resolved from environment → ES client
- S3 credentials resolved from environment → boto3 client

### 0.5.3 One-Phase Execution

The entire refactoring and creation of this Python/Flask application will be executed by Blitzy in **ONE phase**. All files listed above — configuration, models, services, API routes, authentication, format handlers, storage backends, tests, and documentation — will be created in a single execution phase. No multi-phase splitting is applied.

## 0.6 Dependency Inventory

### 0.6.1 Key Private and Public Packages

The following table lists all key Python packages required for the Flask reimplementation, with their exact versions verified via PyPI and web search:

**Runtime Dependencies:**

| Registry | Package Name | Version | Purpose |
|---|---|---|---|
| PyPI | Flask | 3.1.3 | Core WSGI web application framework |
| PyPI | gunicorn | 25.1.0 | Production-grade WSGI HTTP server (replaces Jetty 12.0.5) |
| PyPI | Flask-SQLAlchemy | 3.1.1 | SQLAlchemy integration for Flask (replaces MyBatis 3.5.15) |
| PyPI | SQLAlchemy | 2.0.36 | Python SQL toolkit and ORM |
| PyPI | Flask-Migrate | 4.0.7 | Alembic database migration wrapper (replaces Flyway 8.5.13) |
| PyPI | alembic | 1.14.1 | Database schema migration tool |
| PyPI | marshmallow | 3.23.2 | Object serialization/deserialization (replaces Jackson 2.16.1) |
| PyPI | flask-smorest | 0.45.0 | Flask REST API with OpenAPI/Swagger docs (replaces RESTEasy + Swagger) |
| PyPI | PyJWT | 2.10.1 | JWT token generation and validation (replaces Java-JWT 4.4.0) |
| PyPI | Flask-HTTPAuth | 4.8.0 | HTTP authentication for Flask (replaces Shiro AuthN filters) |
| PyPI | Werkzeug | 3.1.3 | WSGI utilities (installed with Flask) |
| PyPI | psycopg2-binary | 2.9.10 | PostgreSQL database adapter (replaces JDBC 42.7.2) |
| PyPI | elasticsearch | 7.17.12 | Elasticsearch Python client (replaces Java ES client) |
| PyPI | boto3 | 1.36.7 | AWS SDK for Python (replaces AWS SDK for Java — S3 BlobStore) |
| PyPI | APScheduler | 3.10.4 | Background task scheduling (replaces Quartz 2.3.2) |
| PyPI | blinker | 1.9.0 | Signal/event dispatching (replaces Guava EventBus) |
| PyPI | cryptography | 44.0.0 | Cryptographic operations, cert management (replaces BouncyCastle 1.78.1) |
| PyPI | requests | 2.32.3 | HTTP client for proxy fetches (replaces Apache HttpClient 4.5.14) |
| PyPI | prometheus-client | 0.21.1 | Prometheus metrics export (replaces Prometheus Client 0.16.0) |
| PyPI | prometheus-flask-instrumentator | 7.0.0 | Flask request metrics instrumentation |
| PyPI | python-dotenv | 1.0.1 | Load environment variables from `.env` files |
| PyPI | Flask-CORS | 5.0.0 | Cross-origin resource sharing support |
| PyPI | python-ldap | 3.4.4 | LDAP directory integration (replaces Shiro LDAP realm) |
| PyPI | Jinja2 | 3.1.5 | Template engine (installed with Flask) |
| PyPI | click | 8.1.8 | CLI framework (installed with Flask) |
| PyPI | itsdangerous | 2.2.0 | Data signing (installed with Flask) |

**Development and Testing Dependencies:**

| Registry | Package Name | Version | Purpose |
|---|---|---|---|
| PyPI | pytest | 8.3.4 | Test framework (replaces JUnit 5.10.1 + Spock) |
| PyPI | pytest-cov | 6.0.0 | Test coverage reporting |
| PyPI | pytest-flask | 1.3.0 | Flask testing utilities |
| PyPI | pytest-mock | 3.14.0 | Mocking utilities (replaces Mockito 5.8.0) |
| PyPI | factory-boy | 3.3.1 | Test data factories |
| PyPI | Faker | 33.1.0 | Fake data generation for tests |
| PyPI | black | 24.10.0 | Code formatter |
| PyPI | flake8 | 7.1.1 | Linting tool |
| PyPI | mypy | 1.14.1 | Static type checking |
| PyPI | moto | 5.0.27 | AWS service mocking for boto3 tests |

### 0.6.2 Dependency Updates

**Import Refactoring:**

All internal imports will follow the `src.app` package namespace. The import hierarchy maps as follows:

| Module Area | Import Pattern | Example |
|---|---|---|
| Models | `from src.app.models.repository import Repository` | Model access in services |
| Schemas | `from src.app.schemas.repository import RepositorySchema` | Serialization in API routes |
| Services | `from src.app.services.repository_manager import RepositoryManager` | Business logic in API routes |
| Auth | `from src.app.auth.authentication import authenticate_request` | Auth in before_request hooks |
| Events | `from src.app.events.event_bus import emit_event` | Event dispatch in services |
| Storage | `from src.app.storage.blobstore import BlobStore` | Storage access in services |

**External Reference Updates:**

| File Pattern | Update Required |
|---|---|
| `requirements.txt` | All runtime dependencies with pinned versions |
| `requirements-dev.txt` | All development and testing dependencies |
| `pyproject.toml` | Project metadata, build system config, dependency declarations |
| `Dockerfile` | Python 3.12 base image, pip install, Gunicorn CMD |
| `docker-compose.yml` | Flask app service, PostgreSQL service, Elasticsearch service |
| `.env.example` | DATABASE_URL, ELASTICSEARCH_URL, S3 credentials, SECRET_KEY, JWT settings |
| `gunicorn.conf.py` | Workers, bind address, timeout, logging configuration |
| `logging.conf` | Python logging handlers, formatters, log levels |

## 0.7 Refactoring Rules

### 0.7.1 Refactoring-Specific Rules

The following rules are derived from the user's directive to "preserve all functionalities of the original project" and the constraints documented in the technical specification:

- **Maintain all public REST API contracts** — Every REST API endpoint documented in Feature F-501 must be reimplemented with equivalent paths, HTTP methods, request/response schemas, and status codes. Existing client integrations (build tools, CI/CD pipelines, automation scripts) must be able to interact with the new Flask API without modification.

- **Preserve all existing functionality** — All 20 features across 5 categories (Section 2.1) must be functionally equivalent in the Python/Flask implementation. The 67 testable functional requirements (Section 2.2) serve as the acceptance criteria.

- **Maintain the data model** — All 14 DataStore entities (Section 6.2.1.2) must be faithfully represented as SQLAlchemy models with equivalent relationships, constraints, and JSON attribute storage.

- **Preserve multi-format repository support** — All 7 repository formats (Maven, npm, Docker, NuGet, PyPI, APT, Raw) must implement their respective protocol handlers as Flask route handlers or blueprints, serving format-native protocol endpoints alongside the REST API.

- **Maintain repository type behavior** — The three repository types (Hosted, Proxy, Group) must preserve their distinct resolution strategies: Hosted serves from local BlobStore, Proxy caches from remote sources, Group aggregates ordered members.

- **Preserve the dual-database architecture** — SQLite replaces H2 for standalone deployments (zero-config); PostgreSQL remains the enterprise/clustered database. SQLAlchemy must abstract both backends through a unified ORM layer.

- **Maintain pluggable BlobStore abstraction** — The abstract BlobStore interface with File and S3 implementations must be preserved, including atomic writes (temp staging + rename-on-commit), soft-delete, metadata sidecar files, SSE encryption, and multipart uploads.

- **Preserve security model** — The multi-backend authentication chain (local, API key, JWT, LDAP, SSO) and three-tier RBAC authorization (system-wide, repository-scoped, sub-repository via CSEL) must be functionally equivalent.

- **Maintain event-driven architecture** — The event system for audit logging (F-303), webhook dispatch (F-503), configuration propagation (F-404), and cleanup triggers (F-204) must be reimplemented using Python's Blinker signal framework.

- **Preserve scheduled task execution** — One-time, recurring, and cron-based task scheduling with per-task logging must be reimplemented using APScheduler, including cluster-aware execution for multi-node deployments.

### 0.7.2 Special Instructions and Constraints

- **Python 3.12+ required** — The implementation must target Python 3.12 or newer, leveraging modern language features (type hints, match statements, dataclasses, async/await where appropriate)

- **Flask Application Factory pattern** — The application must use the Flask application factory pattern (`create_app()`) for testability and configuration flexibility

- **Blueprint-based modularization** — Each functional area must be implemented as a separate Flask Blueprint, mirroring the modular monolith architecture of the source system

- **Environment-based configuration** — All configuration must be driven by environment variables with sensible defaults, following the twelve-factor app methodology

- **Production deployment** — Gunicorn must be the production WSGI server; the Flask development server must only be used for local development

- **Docker-ready** — The application must include a Dockerfile and docker-compose.yml for containerized deployment, with services for the Flask app, PostgreSQL, and Elasticsearch

- **OpenAPI documentation** — Auto-generated OpenAPI/Swagger documentation must be accessible at `/api/docs` or equivalent

- **Structured logging** — All application logging must use Python's `logging` module with JSON-formatted output for SIEM integration compatibility

- **No Java dependencies** — The Python implementation must have zero Java/JVM dependencies; all functionality must be implemented using pure Python libraries

### 0.7.3 Performance Targets (Preserved from Source)

The following performance targets from the source system (Section 5.4.5) must be achievable in the Python/Flask implementation:

| Metric | Target |
|---|---|
| REST API Response Time | < 500ms average |
| Cached Artifact Resolution | < 200ms |
| Full-Text Search (100K components) | < 2 seconds |
| System Uptime | ≥ 99.9% |
| Storage Efficiency (deduplication) | > 2:1 ratio |

## 0.8 References

### 0.8.1 Codebase Files and Folders Searched

| Path | Type | Findings |
|---|---|---|
| `/` (root) | Folder | Repository root — contains only `README.md`; no source code, no configuration, no dependency manifests |
| `README.md` | File | Single-line file containing `# 24Feb_1`; placeholder only |

The repository is confirmed empty. All system functionality is defined exclusively by the Technical Specification document.

### 0.8.2 Technical Specification Sections Reviewed

The following tech spec sections were retrieved and analyzed to derive the comprehensive Agent Action Plan:

| Section | Title | Key Information Extracted |
|---|---|---|
| 1.1 | Executive Summary | Project identity (Sonatype Nexus Repository), editions (Open Source + Community), Java 21 migration context, business value proposition |
| 1.2 | System Overview | Five-layer architecture, modular monolith design, component diagram, deployment environments, KPIs, success criteria |
| 1.3 | Scope | In-scope features, deployment environments, data domains, Java 21 code boundaries, out-of-scope items |
| 2.1 | Feature Catalog | All 20 features across 5 categories with metadata, descriptions, dependencies, and priority levels |
| 2.2 | Functional Requirements | 67 testable requirements with acceptance criteria, priority, and complexity across 12 feature areas |
| 3.1 | Programming Languages | Java 21 as primary, Groovy for scripting, JavaScript/TypeScript for frontend, Node.js v18.17.1 |
| 3.2 | Frameworks and Libraries | Core framework stack (Karaf, Guice, Jetty, RESTEasy, Shiro, Quartz, Jackson, Metrics) |
| 3.3 | Open Source Dependencies | Complete dependency list with versions and Java 21 compatibility status |
| 3.8 | Complete Version Matrix | Consolidated component registry with all 35+ components, versions, and licenses |
| 5.1 | High-Level Architecture | System overview, five-layer model, data flow architecture, external integration points |
| 5.2 | Component Details | Detailed documentation of all major system components (10 sections) |
| 6.1 | Core Services Architecture | Service components, inter-service communication patterns, scalability design, resilience patterns, lifecycle management, observability |
| 6.2 | Database Design | Dual-database strategy, entity relationships (ER diagram), BlobStore architecture, indexing, caching, performance optimization, compliance |
| 6.3 | Integration Architecture | API design (protocols, auth methods, authorization, rate limiting, versioning), message processing (events, batch), external systems (upstream registries, cloud, identity, monitoring, dev toolchain) |
| 6.4 | Security Architecture | Authentication framework (5 methods, realm chain), RBAC authorization (3 tiers), data protection (encryption, TLS, certificates), audit logging, security control matrix |
| 7.6 | UI Screens and Use Cases | Screen inventory for Repository Management, Security Management, and Administration; user access model |

### 0.8.3 Web Research Conducted

| Search Query | Key Findings |
|---|---|
| Flask latest stable version 2024 2025 | Flask 3.1.3 released Feb 19, 2026; supports Python 3.9+; WSGI framework; latest stable release on PyPI |
| Python Flask SQLAlchemy Gunicorn latest versions 2025 | Gunicorn 25.1.0 (latest); Flask-SQLAlchemy 3.1.1 (latest); standard production deployment pattern: Gunicorn + Flask + SQLAlchemy behind Nginx |

### 0.8.4 Attachments

No attachments were provided for this project. No Figma URLs were specified. No environment files were found in `/tmp/environments_files/`.

### 0.8.5 Key Source References (from Tech Spec)

The following Java source files are referenced in the Technical Specification as the components whose functionality must be reimplemented in Python:

- `Launcher.java` — System entry point and bootstrap (→ `wsgi.py` + `factory.py`)
- `NodeAccessBooter.java` — Core component initialization (→ `factory.py`)
- `CapabilityRegistryBooter.java` — Capability activation (→ `plugins/plugin_manager.py`)
- `RepositoryImpl.java` — Repository lifecycle StateGuard (→ `repositories/lifecycle.py`)
- `RepositoryManagerImpl.java` — Repository management (→ `services/repository_manager.py`)
- `UploadManagerImpl.java` — Artifact upload handling (→ `services/upload_manager.py`)
- `HttpClientFacetImpl.java` — Proxy remote fetches (→ `services/proxy_service.py`)
- `HttpClientManagerImpl.java` — HTTP client configuration (→ `utils/http_client.py`)
- `ProxyFacetSupport.java` — Proxy behavior base (→ `repositories/proxy.py`)
- `S3BlobStore.java` — S3 storage implementation (→ `storage/s3_blobstore.py`)
- `SimpleApiResponse.java` — API DTO (→ `schemas/*.py`)
- `NexusAuthenticationFilter` — Auth entry point (→ `auth/authentication.py`)
- `FirstSuccessfulModularRealmAuthenticator` — Realm chain (→ `auth/authentication.py`)
- `AuthenticatingRealmImpl` — Local auth realm (→ `auth/realms/local_realm.py`)
- `BearerTokenRealm` — API key auth (→ `auth/realms/bearer_token_realm.py`)
- `JwtSecurityFilter` — JWT management (→ `auth/realms/jwt_realm.py`)
- `SecurityComponent` — RBAC evaluation (→ `auth/authorization.py`)
- `TaskLoggerHelper.java` — Task logging (→ `scheduler/task_logger.py`)
- `LoggerFactory.java` — Structured logging (→ `logging.conf`)
- `DefaultsCustomizer` — HTTP client defaults (→ `utils/http_client.py`)
- `RepositoriesFormMachine.js` — UI state machine (out of scope — frontend)
- `InvalidStateException.java` — State violation (→ Python exception classes)
- `BypassHttpErrorException.java` — Custom HTTP errors (→ Flask abort + custom error handlers)

