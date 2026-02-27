# Sonatype Nexus Repository — Python/Flask Backend

![Python](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.1.3-green?logo=flask&logoColor=white)
![License](https://img.shields.io/badge/License-EPL--2.0-orange)
![Build](https://img.shields.io/badge/build-passing-brightgreen)

A **Python 3 / Flask** reimplementation of the [Sonatype Nexus Repository Manager](https://www.sonatype.com/products/sonatype-nexus-repository) backend server. This project is a universal binary repository manager that provides a single source of truth for all software components, supporting **Maven**, **npm**, **Docker**, **NuGet**, **PyPI**, **APT**, and **Raw** repository formats across **Hosted**, **Proxy**, and **Group** repository types.

---

## Table of Contents

- [Features](#features)
- [Technology Stack](#technology-stack)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Application](#running-the-application)
- [Project Structure](#project-structure)
- [Testing](#testing)
- [Deployment](#deployment)
- [API Documentation](#api-documentation)
- [Architecture](#architecture)
- [License](#license)
- [Contributing](#contributing)

---

## Features

### Repository Management

| Feature | Description |
|---|---|
| **Multi-Format Repository Support** | Native protocol support for Maven, npm, Docker, NuGet, PyPI, APT, and Raw artifact formats |
| **Repository Types** | Hosted repositories for internal artifacts, Proxy repositories for caching remote sources, and Group repositories for aggregating multiple repositories under a single URL |
| **Content Indexing & Search** | Full-text search across all components and assets powered by Elasticsearch integration |
| **Browse Tree Navigation** | Hierarchical tree-based browsing of repository contents with format-aware path resolution |

### Storage Management

| Feature | Description |
|---|---|
| **File BlobStore** | Local filesystem storage backend with atomic writes (temp staging + rename-on-commit), soft-delete, and metadata sidecar files |
| **S3 BlobStore** | Amazon S3 storage backend with server-side encryption (SSE), multipart uploads, and configurable regions |
| **BlobStore Maintenance Tasks** | Automated compaction, temporary file cleanup, and integrity verification for BlobStore data |
| **Cleanup Policies** | Configurable format-specific cleanup rules for automated removal of unused or outdated components |

### Security

| Feature | Description |
|---|---|
| **Role-Based Access Control (RBAC)** | Three-tier authorization model — system-wide privileges, repository-scoped permissions, and sub-repository access via Content Selector Expressions (CSEL) |
| **SSL/TLS Support** | Certificate management, keystore/truststore operations, and encrypted communications using the `cryptography` library |
| **Audit Logging** | Comprehensive event-driven audit trail capturing all administrative and security-relevant actions |
| **API Key Authentication** | Token-based authentication for CI/CD pipeline integrations and programmatic access |

### Administration

| Feature | Description |
|---|---|
| **Health Checks & Monitoring** | System health endpoints and Prometheus-compatible metrics for infrastructure monitoring |
| **Scheduled Tasks** | One-time, recurring, and cron-based background task scheduling with per-task logging |
| **Support ZIP Generation** | Diagnostic bundle generation with automatic sanitization of sensitive data |
| **System Configuration Management** | Centralized configuration store with runtime propagation and environment-based overrides |

### Integration & Extensibility

| Feature | Description |
|---|---|
| **REST API** | Comprehensive JSON REST API for all management operations with auto-generated OpenAPI 3.x documentation |
| **Scripting Support** | Script storage and execution engine for administrative automation |
| **Webhook Integration** | HTTP POST event dispatch with HMAC payload signing for external system notifications |
| **Plugin Architecture** | Extension point framework for custom functionality and format plugins |

---

## Technology Stack

| Component | Technology | Version |
|---|---|---|
| **Runtime** | Python | 3.12+ |
| **Web Framework** | Flask | 3.1.3 |
| **Production Server** | Gunicorn | 25.1.0 |
| **ORM** | SQLAlchemy | 2.0.36 |
| **Flask ORM Integration** | Flask-SQLAlchemy | 3.1.1 |
| **Database (Standalone)** | SQLite | (built-in) |
| **Database (Clustered)** | PostgreSQL | 16+ |
| **Database Migrations** | Alembic via Flask-Migrate | 4.0.7 |
| **Serialization** | Marshmallow | 3.26.2 |
| **API Documentation** | flask-smorest (OpenAPI 3.x) | 0.45.0 |
| **HTTP Authentication** | Flask-HTTPAuth | 4.8.0 |
| **JWT Tokens** | PyJWT | 2.10.1 |
| **Cryptography** | cryptography | 46.0.5 |
| **Search Engine Client** | elasticsearch-py | 7.17.12 |
| **Task Scheduling** | APScheduler | 3.10.4 |
| **Event Dispatch** | Blinker | 1.9.0 |
| **AWS S3 Integration** | boto3 | 1.36.7 |
| **Metrics** | prometheus-client | 0.21.1 |
| **HTTP Client** | requests | 2.32.4 |
| **LDAP Integration** | python-ldap | 3.4.5 |
| **CORS** | Flask-CORS | 6.0.0 |
| **Environment Config** | python-dotenv | 1.0.1 |

---

## Prerequisites

- **Python 3.12+** — [Download](https://www.python.org/downloads/)
- **pip** — Python package installer (bundled with Python 3.12+)
- **virtualenv** or **venv** — Python virtual environment tool
- **PostgreSQL 16+** *(optional)* — Required only for clustered/production deployments; SQLite is used by default for standalone mode
- **Elasticsearch 7.17.x** *(optional)* — Required for full-text search functionality; the application starts without it but search features will be unavailable

---

## Installation

```bash
# 1. Clone the repository
git clone <repository-url>
cd nexus-repository

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

# 3. Install runtime dependencies
pip install -r requirements.txt

# 4. Install development and testing dependencies
pip install -r requirements-dev.txt

# 5. Copy the environment template and configure
cp .env.example .env
# Edit .env to set SECRET_KEY, DATABASE_URL, and other settings

# 6. Initialize the database (run Alembic migrations)
flask db upgrade

# 7. Start the development server
python run.py
```

The application will be available at `http://localhost:5000` in development mode.

---

## Configuration

This application follows the [twelve-factor app](https://12factor.net/) methodology. All configuration is driven by **environment variables** with sensible defaults.

### Environment Files

| File | Purpose | Version Controlled |
|---|---|---|
| `.env.example` | Template listing all available environment variables with defaults | Yes |
| `.env` | Your local configuration (copy from `.env.example`) | **No** (gitignored) |
| `.flaskenv` | Flask CLI defaults (`FLASK_APP`, `FLASK_DEBUG`) | Yes |

### Configuration Profiles

| Profile | Database | Debug | Use Case |
|---|---|---|---|
| `development` | SQLite (`nexus.db`) | Enabled | Local development |
| `production` | PostgreSQL | Disabled | Production deployment |
| `testing` | SQLite (in-memory) | Disabled | Automated test execution |

Set the active profile via the `FLASK_CONFIG` environment variable:

```bash
export FLASK_CONFIG=development  # or production, testing
```

### Key Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | *(required)* | Flask session encryption key |
| `DATABASE_URL` | `sqlite:///nexus.db` | Database connection string |
| `ELASTICSEARCH_URL` | `http://localhost:9200` | Elasticsearch cluster URL |
| `JWT_SECRET_KEY` | *(required)* | JWT token signing key |
| `BLOBSTORE_TYPE` | `file` | Storage backend: `file` or `s3` |
| `BLOBSTORE_PATH` | `./data/blobs` | Local BlobStore directory path |
| `S3_BUCKET` | *(empty)* | S3 bucket name for S3 BlobStore |
| `S3_REGION` | `us-east-1` | AWS region for S3 BlobStore |
| `LOG_LEVEL` | `INFO` | Application log level |
| `LOG_FORMAT` | `json` | Log output format: `json` or `standard` |

Refer to [`.env.example`](.env.example) for the complete list of configuration variables.

---

## Running the Application

### Development Mode

```bash
# Using the run.py entry point (recommended for development)
python run.py

# Or using the Flask CLI
flask run
```

The development server starts on `http://localhost:5000` with debug mode and auto-reload enabled.

### Production Mode

```bash
# Using Gunicorn with the production configuration
gunicorn -c gunicorn.conf.py wsgi:app
```

Gunicorn binds to `0.0.0.0:8000` by default with worker processes automatically scaled to your CPU count.

### Docker Mode

```bash
# Start all services (Flask app + PostgreSQL + Elasticsearch)
docker-compose up -d

# View logs
docker-compose logs -f app

# Stop all services
docker-compose down
```

The application is available at `http://localhost:8081` when running via Docker Compose.

### API Documentation

Once the application is running, auto-generated **OpenAPI/Swagger** documentation is accessible at:

```
http://localhost:5000/api/docs/swagger-ui    # Swagger UI (Development)
http://localhost:5000/api/docs/openapi.json  # Raw OpenAPI JSON spec (Development)
http://localhost:8081/api/docs/swagger-ui    # Swagger UI (Docker)
http://localhost:8081/api/docs/openapi.json  # Raw OpenAPI JSON spec (Docker)
```

---

## Project Structure

```
nexus-repository/
├── README.md                    # Project documentation (this file)
├── pyproject.toml               # Project metadata and build configuration
├── setup.py                     # Package installation configuration
├── requirements.txt             # Python runtime dependencies
├── requirements-dev.txt         # Development and testing dependencies
├── .env.example                 # Environment variable template
├── .flaskenv                    # Flask CLI environment settings
├── gunicorn.conf.py             # Gunicorn production server configuration
├── Dockerfile                   # Container image definition
├── docker-compose.yml           # Multi-service orchestration
├── logging.conf                 # Python logging configuration
├── wsgi.py                      # WSGI entry point for Gunicorn
├── run.py                       # Development server entry point
│
├── config/                      # Application configuration profiles
│   ├── __init__.py
│   ├── default.py               # Base configuration with all defaults
│   ├── development.py           # Development settings (SQLite, debug)
│   ├── production.py            # Production settings (PostgreSQL)
│   └── testing.py               # Test settings (in-memory SQLite)
│
├── migrations/                  # Alembic database migration scripts
│   ├── env.py                   # Migration environment configuration
│   ├── alembic.ini              # Alembic settings
│   └── versions/                # Versioned migration scripts
│       └── 001_initial_schema.py
│
├── src/app/                     # Main application package
│   ├── __init__.py              # Package initialization
│   ├── extensions.py            # Flask extension instances (db, migrate, jwt)
│   ├── factory.py               # Application factory (create_app)
│   ├── api/                     # REST API route handlers (Flask Blueprints)
│   │   ├── repositories.py      # Repository CRUD endpoints
│   │   ├── components.py        # Component upload/download endpoints
│   │   ├── assets.py            # Asset operations endpoints
│   │   ├── users.py             # User management endpoints
│   │   ├── roles.py             # Role and privilege management
│   │   ├── tasks.py             # Task scheduling endpoints
│   │   ├── system.py            # System configuration endpoints
│   │   ├── search.py            # Full-text search endpoints
│   │   ├── blobstores.py        # BlobStore management endpoints
│   │   ├── health.py            # Health check and metrics endpoints
│   │   └── ...                  # Additional API modules
│   ├── models/                  # SQLAlchemy data models
│   │   ├── repository.py        # Repository entity
│   │   ├── component.py         # Component entity
│   │   ├── asset.py             # Asset entity
│   │   ├── user.py              # User entity
│   │   ├── role.py              # Role and role assignment entities
│   │   └── ...                  # Additional model modules
│   ├── schemas/                 # Marshmallow serialization schemas
│   ├── services/                # Business logic service layer
│   ├── auth/                    # Authentication and authorization
│   │   ├── authentication.py    # Multi-realm authentication chain
│   │   ├── authorization.py     # Three-tier RBAC enforcement
│   │   └── realms/              # Auth backend implementations
│   ├── formats/                 # Format-specific protocol handlers
│   │   ├── maven/               # Maven repository protocol
│   │   ├── npm/                 # npm registry protocol
│   │   ├── docker/              # Docker Registry API v2
│   │   ├── nuget/               # NuGet V3 API
│   │   ├── pypi/                # PEP 503 Simple Repository API
│   │   ├── apt/                 # Debian package index
│   │   └── raw/                 # Arbitrary binary file handling
│   ├── storage/                 # BlobStore implementations
│   │   ├── file_blobstore.py    # Local filesystem backend
│   │   └── s3_blobstore.py      # Amazon S3 backend
│   ├── repositories/            # Repository type logic (Hosted/Proxy/Group)
│   ├── scheduler/               # APScheduler task scheduling
│   ├── events/                  # Blinker signal-based event system
│   ├── monitoring/              # Health checks and Prometheus metrics
│   ├── search/                  # Elasticsearch client and queries
│   ├── security/                # SSL/TLS and certificate management
│   ├── webhooks/                # Webhook dispatch with HMAC signing
│   ├── plugins/                 # Plugin architecture framework
│   └── utils/                   # Shared utilities and helpers
│
├── tests/                       # Test suite
│   ├── conftest.py              # Shared test fixtures and configuration
│   ├── unit/                    # Unit tests
│   │   ├── test_models.py       # SQLAlchemy model tests
│   │   ├── test_services.py     # Business logic tests
│   │   ├── test_auth.py         # Authentication and authorization tests
│   │   ├── test_formats.py      # Format handler tests
│   │   ├── test_storage.py      # BlobStore backend tests
│   │   └── test_scheduler.py    # Scheduled task tests
│   ├── integration/             # Integration tests
│   │   ├── test_api.py          # REST API endpoint tests
│   │   ├── test_repository_lifecycle.py
│   │   ├── test_proxy.py        # Proxy fetch tests
│   │   ├── test_search.py       # Elasticsearch integration tests
│   │   └── test_blobstore.py    # Storage backend tests
│   └── fixtures/                # Test data and sample artifacts
│
└── docs/                        # Documentation
    ├── api/
    │   └── openapi.yaml         # OpenAPI specification
    ├── architecture/
    │   └── overview.md          # Architecture documentation
    └── deployment/
        └── guide.md             # Deployment guide
```

### Directory Overview

| Directory | Description |
|---|---|
| `config/` | Environment-specific configuration classes loaded by the application factory |
| `migrations/` | Alembic database migration scripts for schema versioning |
| `src/app/` | Main application package containing all Flask blueprints, models, services, and modules |
| `tests/` | Comprehensive test suite with unit tests, integration tests, and shared fixtures |
| `docs/` | Project documentation including OpenAPI specs, architecture overview, and deployment guide |

---

## Testing

The test suite uses [pytest](https://docs.pytest.org/) with supporting libraries for Flask testing, mocking, and test data generation.

### Run All Tests

```bash
# Activate the virtual environment
source venv/bin/activate

# Run the full test suite
pytest

# Run with verbose output
pytest -v --tb=short
```

### Run with Coverage

```bash
# Generate a coverage report
pytest --cov=src --cov-report=term-missing

# Generate an HTML coverage report
pytest --cov=src --cov-report=html
```

### Test Categories

| Category | Path | Description |
|---|---|---|
| **Unit Tests** | `tests/unit/` | Isolated tests for models, services, authentication, format handlers, storage backends, and scheduler |
| **Integration Tests** | `tests/integration/` | End-to-end tests for API endpoints, repository lifecycle, proxy fetching, search, and BlobStore operations |

### Test Utilities

- **pytest-flask** — Flask application and test client fixtures
- **pytest-mock** — Convenient mocking utilities
- **factory-boy** — Declarative test data factories for SQLAlchemy models
- **Faker** — Realistic fake data generation for test scenarios
- **moto** — AWS service mocking for S3 BlobStore tests

---

## Deployment

### Docker Deployment (Recommended)

The project includes a production-ready `Dockerfile` and `docker-compose.yml` for containerized deployment.

```bash
# Build and start all services
docker-compose up -d --build

# Services started:
#   - nexus-app          : Flask/Gunicorn on port 8081
#   - nexus-postgres     : PostgreSQL 16 on port 5432
#   - nexus-elasticsearch: Elasticsearch 7.17.12 on port 9200

# Verify the application is running
curl http://localhost:8081/api/v1/health

# Stop all services
docker-compose down
```

### Manual Deployment

For non-Docker deployments, use Gunicorn behind a reverse proxy (e.g., Nginx):

```bash
# Install dependencies
pip install -r requirements.txt

# Set production environment variables
export FLASK_CONFIG=production
export DATABASE_URL=postgresql://user:pass@db-host:5432/nexus
export SECRET_KEY=your-secret-key

# Run database migrations
flask db upgrade

# Start Gunicorn
gunicorn -c gunicorn.conf.py wsgi:app
```

For detailed deployment instructions, see [`docs/deployment/guide.md`](docs/deployment/guide.md).

---

## API Documentation

Auto-generated **OpenAPI 3.x / Swagger** documentation is available at `/api/docs/swagger-ui` (interactive Swagger UI) and `/api/docs/openapi.json` (raw JSON specification) when the application is running. The documentation is generated by [flask-smorest](https://flask-smorest.readthedocs.io/) from the registered Flask Blueprints.

The static OpenAPI specification is also available at [`docs/api/openapi.yaml`](docs/api/openapi.yaml).

### API Endpoint Groups

| Endpoint Group | Base Path | Description |
|---|---|---|
| **Repositories** | `/api/v1/repositories` | Repository CRUD and lifecycle management |
| **Components** | `/api/v1/components` | Component upload, download, and deletion |
| **Assets** | `/api/v1/assets` | Asset metadata and content operations |
| **Users** | `/api/v1/security/users` | User account management |
| **Roles** | `/api/v1/security/roles` | Role and privilege assignment |
| **Tasks** | `/api/v1/tasks` | Scheduled task management and execution |
| **System** | `/api/v1/system` | System configuration and status |
| **Search** | `/api/v1/search` | Full-text and keyword search |
| **Security** | `/api/v1/security` | Certificate and SSL/TLS management |
| **BlobStores** | `/api/v1/blobstores` | Storage backend configuration |
| **Cleanup** | `/api/v1/cleanup` | Cleanup policy management and preview |
| **Scripts** | `/api/v1/scripts` | Script CRUD and execution |
| **Webhooks** | `/api/v1/webhooks` | Webhook configuration and management |
| **Health** | `/api/v1/health` | Health checks and system status |

---

## Architecture

This application implements a **layered architecture** with clear separation of concerns, mirroring the modular monolith design of the original system:

```
┌─────────────────────────────────────────────────────────┐
│                   API Layer                              │
│         (Flask Blueprints — REST endpoints)              │
├─────────────────────────────────────────────────────────┤
│                 Service Layer                            │
│      (Business logic, validation, orchestration)        │
├─────────────────────────────────────────────────────────┤
│               Repository Layer                          │
│     (Hosted / Proxy / Group type resolution)            │
├─────────────────────────────────────────────────────────┤
│                Storage Layer                             │
│       (BlobStore abstraction — File / S3)               │
├─────────────────────────────────────────────────────────┤
│                  Data Layer                              │
│   (SQLAlchemy models — SQLite / PostgreSQL)             │
└─────────────────────────────────────────────────────────┘
```

### Key Design Patterns

| Pattern | Implementation |
|---|---|
| **Application Factory** | `create_app()` in `src/app/factory.py` for testable, configurable app creation |
| **Blueprint Modules** | Each functional area as a separate Flask Blueprint |
| **Repository Pattern** | SQLAlchemy models with service-layer data access abstraction |
| **Strategy Pattern** | Pluggable BlobStore backends (File vs. S3) via abstract base class |
| **Chain of Responsibility** | Multi-realm authentication with first-successful strategy |
| **Observer Pattern** | Blinker signals for decoupled event dispatch (audit logging, webhooks) |
| **State Machine** | Repository lifecycle transitions (NEW → STARTED → STOPPED) |

For detailed architecture documentation, see [`docs/architecture/overview.md`](docs/architecture/overview.md).

---

## License

This project is licensed under the **Eclipse Public License 2.0 (EPL-2.0)**. See the [LICENSE](LICENSE) file for details.

---

## Contributing

Contributions are welcome! To get started:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Install development dependencies (`pip install -r requirements-dev.txt`)
4. Make your changes and add tests
5. Run the test suite (`pytest`)
6. Run the linter and formatter:
   ```bash
   black src/ tests/
   flake8 src/ tests/
   mypy src/
   ```
7. Commit your changes (`git commit -m 'Add your feature'`)
8. Push to the branch (`git push origin feature/your-feature`)
9. Open a Pull Request

Please ensure all tests pass and code style checks are clean before submitting.
