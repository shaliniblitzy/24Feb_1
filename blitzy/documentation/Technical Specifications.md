# Technical Specification

# 0. Agent Action Plan

## 0.1 Intent Clarification

### 0.1.1 Core Testing Objective

Based on the provided requirements, the Blitzy platform understands that the testing objective is to **create a comprehensive new test suite for a Python 3 Flask application** that is being rewritten from an existing Node.js server. The user's request — repeated for emphasis — is to rewrite a Node.js server in Python 3 using Flask while preserving all functionalities of the original project. This is a complete platform migration from a JavaScript/Node.js runtime to a Python/Flask runtime.

**Request Categorization:** Add new tests (greenfield test creation for migrated Flask application)

The user's core requirement translates to the following testing imperatives:

- **Functional Parity Verification** — Every feature present in the original Node.js server must be validated in the new Flask implementation through corresponding test cases, ensuring zero regression during migration
- **Flask API Route Testing** — All REST API endpoints rewritten from Node.js Express routes (or equivalent) to Flask route handlers must be tested for correct HTTP methods, status codes, request/response payloads, and error handling
- **Data Layer Validation** — Database interactions, ORM models, and data access patterns migrated from Node.js (e.g., Mongoose, Sequelize, Knex) to Python equivalents (e.g., SQLAlchemy, Flask-SQLAlchemy) must be verified for correctness
- **Authentication and Authorization Testing** — Security mechanisms (JWT, session management, RBAC) rewritten in Flask must be validated for behavioral equivalence
- **Middleware and Hook Testing** — Node.js middleware patterns (Express middleware) translated to Flask before/after request decorators and error handlers must be tested
- **Integration Testing** — End-to-end request lifecycle through the Flask application must be validated using Flask's test client
- **Edge Case and Error Handling** — Boundary conditions, invalid inputs, and error scenarios must be covered to ensure the Flask rewrite handles failures gracefully

**Implicit Testing Needs Surfaced:**

- Configuration management tests (environment variables, Flask config objects)
- Request validation and serialization/deserialization tests
- CORS handling tests (if applicable in the original server)
- File upload/download handling tests
- WebSocket or real-time communication tests (if present in original)
- Background task/scheduled job tests (if present in original)
- Health check and monitoring endpoint tests
- Database migration and schema validation tests

### 0.1.2 Special Instructions and Constraints

**Critical Directives Identified:**

- The user explicitly requests "preserving all functionalities of the original project" — this is the paramount constraint and means tests must validate complete functional parity
- The migration is from Node.js to Python 3 with Flask — no original Node.js test files exist in the repository to reference or port
- The repository is currently empty (contains only a `README.md`), meaning all test infrastructure, configuration, and test files must be created from scratch
- No specific testing framework or coverage targets were specified by the user; industry best practices for Flask testing with pytest will be applied

**Testing Convention Requirements:**

- Use pytest as the primary testing framework (Flask's officially recommended test runner)
- Follow Flask's documented testing patterns using `app.test_client()` fixtures
- Organize tests in a `tests/` directory with subdirectories for unit, integration, and functional tests
- Use `conftest.py` for shared fixtures, following pytest conventions
- Apply the application factory pattern (`create_app()`) to support testing configuration

**Web Search Requirements Documented:**

- Flask 3.1.x testing best practices — Completed
- pytest compatibility with Python 3.12+ and Flask 3.x — Completed
- pytest-flask plugin features and compatibility — Completed
- Mocking strategies for Flask database and external service dependencies — Completed

### 0.1.3 Technical Interpretation

These testing requirements translate to the following technical test implementation strategy:

- To **validate REST API endpoints**, we will create unit and functional test files for each route blueprint in the Flask application, using Flask's `test_client()` to issue HTTP requests and assert response codes, headers, and JSON payloads
- To **verify data layer correctness**, we will create model and repository test files that validate ORM mappings, CRUD operations, query correctness, and transaction behavior using an in-memory SQLite database or test-specific database fixtures
- To **test authentication and security**, we will create dedicated security test files that validate JWT token generation/validation, session management, role-based access control, and protected endpoint behavior
- To **validate middleware behavior**, we will create test files covering Flask `before_request`, `after_request`, and `errorhandler` decorators to ensure request lifecycle hooks function correctly
- To **ensure integration correctness**, we will create integration tests that exercise complete request lifecycles through multiple application layers (route → service → data access → response)
- To **cover configuration management**, we will create configuration test files that validate environment-specific settings, Flask config loading, and secret management
- To **test background operations**, we will create tests for any scheduled tasks, async operations, or background job processing present in the migrated application

### 0.1.4 Coverage Requirements Interpretation

**Explicit Coverage Targets:** None specified by the user.

**Implicit Coverage Expectations Based on Best Practices:**

- Flask/Python industry standard for well-tested applications: ≥ 80% line coverage
- Critical path coverage (authentication, data mutations, core business logic): 100%
- Route handler coverage: 100% of all defined endpoints
- Error handler coverage: all registered Flask error handlers tested
- Model/schema coverage: all ORM model classes and their relationships
- The original Node.js project's feature set represents the coverage boundary — every feature must have corresponding test validation

To achieve comprehensive testing, coverage should include:

- All Flask route handlers across every blueprint
- All service layer functions implementing business logic
- All data access layer functions (models, queries, transactions)
- All authentication and authorization code paths
- All error handling and exception translation logic
- All configuration loading and validation paths
- All utility and helper functions

## 0.2 Test Discovery and Analysis

### 0.2.1 Existing Test Infrastructure Assessment

Repository analysis reveals that the codebase is a **greenfield repository** with no existing source code, test files, or infrastructure of any kind. The repository contains only a single `README.md` file with the content `# 24Feb_1`. Consequently:

- **No existing test files** found matching patterns: `*test*`, `*spec*`, `test_*`, `spec_*`, `*_test.*`, `*_spec.*`
- **No testing framework** detected in any dependency manifest (no `package.json`, `requirements.txt`, `pyproject.toml`, `setup.py`, or `setup.cfg` exists)
- **No test configuration files** found (no `pytest.ini`, `conftest.py`, `tox.ini`, `.coveragerc`, `jest.config.*`, or equivalent)
- **No test data fixtures** or factory files present
- **No CI/CD configuration** referencing test execution

Since the user's requirement is a Node.js-to-Flask rewrite, the original Node.js codebase is not present in this repository. The technical specification document serves as the authoritative feature definition, describing a comprehensive binary repository management system with 20 features across 5 categories (Repository Management, Storage Management, Security, Administration, Integration & Extensibility).

**Planned Testing Framework Selection:**

- **Current testing framework:** pytest (to be installed) — Flask's officially recommended test runner per Flask Documentation 3.1.x
- **Test runner configuration location:** `pytest.ini` or `pyproject.toml` `[tool.pytest]` section at project root
- **Coverage tools:** pytest-cov (to be installed) — integrates coverage.py with pytest
- **Mock/stub libraries:** pytest-mock (to be installed) — thin pytest wrapper around `unittest.mock`
- **Test data fixtures:** Custom pytest fixtures in `tests/conftest.py` using Flask's application factory pattern

### 0.2.2 Web Search Research Conducted

The following research was conducted to inform the testing strategy:

- **Flask testing best practices** — Flask 3.1.x documentation confirms pytest as the recommended framework, emphasizing the application factory pattern with `create_app()`, `app.test_client()` for HTTP testing, and `conftest.py` for fixture organization. Tests should be located in a `tests/` folder with functions prefixed `test_` in modules prefixed `test_`.

- **pytest-flask plugin capabilities** — The pytest-flask plugin (version 1.3.0) provides built-in fixtures including `client`, `config`, and `live_server`. It has confirmed compatibility with Flask 3.0+ after removing the deprecated `request_ctx` fixture. It supports Python 3.10, 3.11, and 3.12.

- **Mocking strategies for Flask applications** — Best practices confirm using `unittest.mock.patch` and `pytest-mock` for isolating external dependencies such as APIs and databases. Three proven approaches for database testing: real PostgreSQL via Docker/Testcontainers, in-memory SQLite for speed, and mock database interactions for pure unit testing.

- **pytest version compatibility** — pytest 8.x supports Python 3.8+ with confirmed Python 3.13 support. pytest 9.x introduces native TOML configuration under `[tool.pytest]` in `pyproject.toml`. All modern Flask testing tools are fully compatible with Python 3.12.

- **Flask test client patterns** — Flask's `test_client()` provides methods matching HTTP verbs (`get()`, `post()`, `put()`, `delete()`, `patch()`), accepts `path`, `query_string`, `headers`, `data`, and `json` parameters. Session testing is supported via `client.session_transaction()` context manager.

### 0.2.3 Feature-Driven Test Target Mapping

Since the repository is empty but the technical specification defines the full feature catalog, the following features from the tech spec form the test target universe for the Flask rewrite:

| Feature Category | Feature ID | Feature Name | Test Priority |
|---|---|---|---|
| Repository Management | F-101 | Multi-Format Support (Maven, npm, Docker, NuGet, PyPI, APT, Raw) | Critical |
| Repository Management | F-102 | Repository Types (Hosted, Proxy, Group) | Critical |
| Repository Management | F-103 | Content Indexing and Search | High |
| Repository Management | F-104 | Browse Tree Navigation | Medium |
| Storage Management | F-201 | File BlobStore | Critical |
| Storage Management | F-202 | S3 BlobStore | High |
| Storage Management | F-203 | Maintenance Tasks | Medium |
| Storage Management | F-204 | Cleanup Policies | Medium |
| Security | F-301 | Role-Based Access Control (RBAC) | Critical |
| Security | F-302 | SSL/TLS Management | High |
| Security | F-303 | Audit Logging | High |
| Security | F-304 | API Key Authentication | Critical |
| Administration | F-401 | Health Checks and Monitoring | High |
| Administration | F-402 | Scheduled Tasks | Medium |
| Administration | F-403 | Support ZIP Generation | Low |
| Administration | F-404 | System Configuration | Medium |
| Integration | F-501 | REST API | Critical |
| Integration | F-502 | Scripting Engine | Medium |
| Integration | F-503 | Webhooks | Medium |
| Integration | F-504 | Plugin Architecture | Low |

## 0.3 Testing Scope Analysis

### 0.3.1 Test Target Identification

**Primary Code to Be Tested:**

The Flask application being created from scratch will follow a modular structure derived from the technical specification's five-layer architecture. Each module below requires dedicated test coverage:

- **Module:** Application Factory at `src/app.py` — requires unit tests for configuration loading, blueprint registration, extension initialization, and error handler setup
- **Module:** Repository Management Service at `src/services/repository_service.py` — requires unit and integration tests for CRUD operations across all repository types (Hosted, Proxy, Group)
- **Module:** Storage Service at `src/services/storage_service.py` — requires unit tests for file BlobStore and S3 BlobStore operations (upload, download, delete, metadata retrieval)
- **Module:** Security Service at `src/services/security_service.py` — requires unit tests for authentication (JWT, API Key), authorization (RBAC), and session management
- **Module:** Search Service at `src/services/search_service.py` — requires unit tests for content indexing and full-text search across components
- **Module:** Scheduling Service at `src/services/scheduler_service.py` — requires unit tests for task scheduling, execution, and cleanup policy enforcement
- **Module:** REST API Controllers at `src/api/` — requires functional tests for every route handler across all blueprints
- **Module:** Data Models at `src/models/` — requires unit tests for ORM model definitions, relationships, and validation
- **Module:** Configuration at `src/config.py` — requires unit tests for environment-based configuration loading

**Functions Requiring Test Categories:**

| Module / Function Group | Unit Tests | Integration Tests | Edge Case Tests | Error Tests |
|---|---|---|---|---|
| `create_app()` factory | ✓ | ✓ | ✓ | ✓ |
| Repository CRUD operations | ✓ | ✓ | ✓ | ✓ |
| Artifact upload/download | ✓ | ✓ | ✓ | ✓ |
| Proxy fetch and caching | ✓ | ✓ | ✓ | ✓ |
| Group repository aggregation | ✓ | ✓ | ✓ | ✓ |
| User authentication (JWT/API Key) | ✓ | ✓ | ✓ | ✓ |
| RBAC privilege evaluation | ✓ | — | ✓ | ✓ |
| Search indexing and querying | ✓ | ✓ | ✓ | ✓ |
| BlobStore file operations | ✓ | ✓ | ✓ | ✓ |
| S3 BlobStore operations | ✓ | ✓ | ✓ | ✓ |
| Scheduled task execution | ✓ | ✓ | ✓ | ✓ |
| Health check endpoints | ✓ | — | — | ✓ |
| Audit logging | ✓ | ✓ | — | ✓ |
| Webhook dispatch | ✓ | ✓ | ✓ | ✓ |
| Configuration loading | ✓ | — | ✓ | ✓ |

**Existing Test File Mapping:**

| Source File | Existing Test File | Test Categories Present |
|---|---|---|
| src/app.py | None — to be created | N/A |
| src/api/*.py | None — to be created | N/A |
| src/services/*.py | None — to be created | N/A |
| src/models/*.py | None — to be created | N/A |
| src/config.py | None — to be created | N/A |

### 0.3.2 Dependencies Requiring Mocking

**External Services to Mock:**

- **S3-compatible object storage** — All S3 BlobStore operations must be mocked using `moto` (AWS mock library) or `unittest.mock.patch` to avoid real cloud interactions during testing
- **Upstream proxy registries** — Remote registry endpoints (Maven Central, npmjs.org, Docker Hub, PyPI, NuGet Gallery) must be mocked with `responses` or `requests-mock` for proxy fetch tests
- **Elasticsearch/search engine** — Search indexing and query operations must be mocked to test search service logic without a running search cluster
- **SMTP/notification services** — Any email or webhook dispatch must be mocked
- **External identity providers** — LDAP, SAML, or OAuth providers must be mocked for authentication tests

**Database Interactions to Stub:**

- SQLAlchemy session and query objects must be mocked for pure unit tests
- An in-memory SQLite database should be used for integration tests requiring real SQL execution
- Database migration state (Alembic/Flask-Migrate) must be verified in a test database context

**File System Operations to Virtualize:**

- Local file BlobStore operations (file creation, reading, deletion) should use temporary directories via `tmp_path` fixture or `tempfile`
- Support ZIP generation should use in-memory buffers
- Configuration file loading should use mocked file contents

### 0.3.3 Version Compatibility Research

Based on Python 3.12 (the target runtime) and Flask 3.1.x, the recommended testing stack is:

| Tool | Recommended Version | Compatibility Rationale |
|---|---|---|
| pytest | 8.3.x | Full Python 3.12/3.13 support; native TOML config; stable latest release |
| pytest-flask | 1.3.0 | Flask 3.0+ compatibility confirmed; removed deprecated `request_ctx`; supports Python 3.10–3.12 |
| pytest-cov | 6.0.x | Latest coverage.py integration; supports Python 3.12 |
| pytest-mock | 3.14.x | Thin wrapper around `unittest.mock`; fully compatible with Python 3.12 |
| Flask | 3.1.x | Latest stable; requires Python ≥ 3.9, Werkzeug ≥ 3.1 |
| responses | 0.25.x | HTTP request mocking; Python 3.12 compatible |
| moto | 5.0.x | AWS service mocking (S3); Python 3.12 compatible |
| factory-boy | 3.3.x | Test data factories; Python 3.12 compatible |
| Faker | 30.x | Synthetic test data generation; Python 3.12 compatible |

**No version conflicts identified.** All listed packages have confirmed compatibility with Python 3.12 and Flask 3.1.x.

## 0.4 Test Implementation Design

### 0.4.1 Test Strategy Selection

**Test Types to Implement:**

- **Unit Tests** — Focus on isolated components: individual service methods, model validations, utility functions, and configuration loaders. Each function is tested in isolation with all external dependencies mocked. This tier forms the base of the testing pyramid and accounts for the majority of test cases.

- **Integration Tests** — Cover component interactions: Flask route handlers calling service layer methods which in turn interact with database models via SQLAlchemy. These tests use Flask's `test_client()` to issue real HTTP requests through the application stack with an in-memory SQLite test database.

- **Edge Case Tests** — Address boundary conditions: maximum file sizes, empty payloads, Unicode characters in repository names, concurrent access patterns, invalid format combinations, expired tokens, overflow conditions, and configuration boundary values.

- **Error Handling Tests** — Verify failure scenarios: authentication failures (invalid/expired JWT, revoked API keys), authorization denials (insufficient RBAC privileges), storage errors (disk full, S3 connection failures), database errors (constraint violations, connection timeouts), malformed requests (invalid JSON, missing required fields), and HTTP error responses (400, 401, 403, 404, 409, 500).

### 0.4.2 Test Case Blueprint

```
Component: Application Factory (src/app.py)
Test Categories:
- Happy path: App creation with valid config, blueprint registration, extension init
- Edge cases: Missing environment variables, conflicting configurations
- Error cases: Invalid database URI, missing required extensions
- Configuration: Testing, development, and production config profiles
```

```
Component: Repository Management Service (src/services/repository_service.py)
Test Categories:
- Happy path: Create/read/update/delete for Hosted, Proxy, and Group repositories
- Edge cases: Duplicate repository names, maximum name length, special characters
- Error cases: Non-existent repository access, invalid repository type, circular group references
- Integration: Multi-format artifact operations (Maven, npm, Docker, NuGet, PyPI, APT, Raw)
```

```
Component: Storage Service (src/services/storage_service.py)
Test Categories:
- Happy path: File upload, download, delete for File BlobStore and S3 BlobStore
- Edge cases: Zero-byte files, maximum file size, concurrent uploads, metadata limits
- Error cases: Storage backend unavailable, permission denied, corrupted data detection
- Performance boundaries: Large file streaming, concurrent read/write operations
```

```
Component: Security Service (src/services/security_service.py)
Test Categories:
- Happy path: JWT generation/validation, API key auth, RBAC privilege checks, session creation
- Edge cases: Token at expiration boundary, multiple concurrent sessions, role hierarchy edge cases
- Error cases: Invalid credentials, expired tokens, revoked API keys, insufficient privileges
- Integration: Full authentication chain from request to authorization decision
```

```
Component: REST API Controllers (src/api/)
Test Categories:
- Happy path: All CRUD endpoints return correct status codes and payloads
- Edge cases: Empty collections, pagination boundaries, query parameter limits
- Error cases: Invalid input validation, unauthorized access, resource conflicts
- Content negotiation: JSON request/response handling, file upload multipart handling
```

```
Component: Search Service (src/services/search_service.py)
Test Categories:
- Happy path: Index creation, document indexing, full-text search, filtered search
- Edge cases: Empty search results, special characters in queries, very long queries
- Error cases: Search backend unavailable, index corruption, timeout scenarios
```

```
Component: Data Models (src/models/)
Test Categories:
- Happy path: Model instantiation, field validation, relationship traversal
- Edge cases: Optional field boundaries, enum value boundaries, cascading operations
- Error cases: Constraint violations, invalid field values, orphaned relationships
```

### 0.4.3 Existing Test Extension Strategy

Since the repository is empty, there are no existing tests to extend, refactor, or fix. All tests will be newly created. However, the following design principles ensure future extensibility:

- **Tests to create following reference patterns:** All tests will follow the Flask documentation's recommended pattern using `conftest.py` fixtures with the application factory pattern, enabling straightforward extension as new features are added
- **Shared fixture design:** Common fixtures (`app`, `client`, `db_session`, `auth_headers`) will be defined in the root `tests/conftest.py` to be inherited by all test subdirectories
- **Parametrized test design:** Tests for multi-format support (7 formats) and multi-repository-type operations (3 types) will use `@pytest.mark.parametrize` to maximize coverage with minimal code duplication

### 0.4.4 Test Data and Fixtures Design

**Required Test Data Structures:**

- Repository fixtures: Sample repository configurations for each type (Hosted, Proxy, Group) and each format (Maven, npm, Docker, NuGet, PyPI, APT, Raw)
- User fixtures: Test users with varying RBAC roles (admin, developer, readonly, anonymous)
- Artifact fixtures: Sample binary artifacts for upload/download testing (small text files, medium binaries)
- Configuration fixtures: Environment-specific configuration dictionaries for testing, development, and production profiles

**Fixture Organization Strategy:**

```
tests/
├── conftest.py              # Root fixtures: app, client, db, auth
├── fixtures/
│   ├── __init__.py
│   ├── repository_data.py   # Repository factory functions
│   ├── user_data.py         # User/role factory functions
│   ├── artifact_data.py     # Sample artifact generators
│   └── config_data.py       # Configuration dictionaries
```

**Mock Object Specifications:**

- `MockS3Client` — Simulates boto3 S3 client with `put_object`, `get_object`, `delete_object`, `list_objects_v2`
- `MockSearchEngine` — Simulates search backend with `index`, `search`, `delete_index`
- `MockProxyClient` — Simulates upstream registry HTTP responses with configurable status codes and payloads
- `MockEmailService` — Captures sent notifications for assertion without actual dispatch

**Test Database/State Management Approach:**

- Use SQLAlchemy with SQLite in-memory database (`sqlite:///:memory:`) for integration tests
- Apply `db.create_all()` in a session-scoped fixture and `db.session.rollback()` after each test for isolation
- Use `factory_boy` factories integrated with SQLAlchemy for generating test data consistently

## 0.5 Test File Transformation Mapping

### 0.5.1 File-by-File Test Plan

All test files are new creations since the repository contains no existing tests or source code. The following table maps every test file to be created, its source file (the production code it validates), and its purpose.

| Target Test File | Transformation | Source File/Test | Purpose/Changes |
|---|---|---|---|
| tests/conftest.py | CREATE | src/app.py | Root test configuration: app factory fixture, test client, database session, authentication helpers |
| tests/unit/conftest.py | CREATE | N/A | Unit test-scoped fixtures with fully mocked dependencies |
| tests/unit/test_app_factory.py | CREATE | src/app.py | Validate application factory, blueprint registration, extension initialization, config loading |
| tests/unit/test_config.py | CREATE | src/config.py | Validate configuration classes for testing, development, production environments |
| tests/unit/services/conftest.py | CREATE | N/A | Service-level unit test fixtures with mocked repositories and clients |
| tests/unit/services/test_repository_service.py | CREATE | src/services/repository_service.py | Unit tests for repository CRUD, type validation, format handling across all 7 formats |
| tests/unit/services/test_storage_service.py | CREATE | src/services/storage_service.py | Unit tests for File BlobStore and S3 BlobStore operations with mocked backends |
| tests/unit/services/test_security_service.py | CREATE | src/services/security_service.py | Unit tests for JWT generation/validation, API key auth, RBAC privilege evaluation |
| tests/unit/services/test_search_service.py | CREATE | src/services/search_service.py | Unit tests for content indexing, full-text search, filtered queries with mocked engine |
| tests/unit/services/test_scheduler_service.py | CREATE | src/services/scheduler_service.py | Unit tests for task scheduling, execution tracking, cleanup policy enforcement |
| tests/unit/services/test_audit_service.py | CREATE | src/services/audit_service.py | Unit tests for audit log creation, retrieval, filtering, and retention |
| tests/unit/services/test_webhook_service.py | CREATE | src/services/webhook_service.py | Unit tests for webhook registration, dispatch, retry logic, and payload formatting |
| tests/unit/services/test_health_service.py | CREATE | src/services/health_service.py | Unit tests for health check aggregation, component status reporting |
| tests/unit/models/conftest.py | CREATE | N/A | Model test fixtures with in-memory database |
| tests/unit/models/test_repository_model.py | CREATE | src/models/repository.py | Model validation, field constraints, relationship integrity for Repository entities |
| tests/unit/models/test_user_model.py | CREATE | src/models/user.py | User model validation, password hashing, role assignment, API key generation |
| tests/unit/models/test_asset_model.py | CREATE | src/models/asset.py | Asset/component model validation, BlobStore references, metadata constraints |
| tests/unit/models/test_blobstore_model.py | CREATE | src/models/blobstore.py | BlobStore configuration model validation, type-specific settings |
| tests/unit/models/test_task_model.py | CREATE | src/models/task.py | Scheduled task model validation, cron expression parsing, state transitions |
| tests/unit/models/test_security_model.py | CREATE | src/models/security.py | Role, privilege, and content selector model validation and relationships |
| tests/unit/utils/test_format_utils.py | CREATE | src/utils/format_utils.py | Format detection, content type mapping, coordinate parsing for each repository format |
| tests/unit/utils/test_crypto_utils.py | CREATE | src/utils/crypto_utils.py | Hash computation (SHA-1, SHA-256, MD5), checksum validation, encryption utilities |
| tests/unit/utils/test_validation_utils.py | CREATE | src/utils/validation_utils.py | Input validation, name sanitization, path normalization, size limit enforcement |
| tests/integration/conftest.py | CREATE | N/A | Integration test fixtures with test database, test client, and controlled dependencies |
| tests/integration/api/test_repository_api.py | CREATE | src/api/repository_routes.py | Full HTTP lifecycle tests for repository CRUD endpoints (POST/GET/PUT/DELETE) |
| tests/integration/api/test_asset_api.py | CREATE | src/api/asset_routes.py | Full HTTP lifecycle tests for artifact upload, download, search, and deletion |
| tests/integration/api/test_auth_api.py | CREATE | src/api/auth_routes.py | Full HTTP lifecycle tests for login, token refresh, API key management, session handling |
| tests/integration/api/test_user_api.py | CREATE | src/api/user_routes.py | Full HTTP lifecycle tests for user CRUD, role assignment, password management |
| tests/integration/api/test_search_api.py | CREATE | src/api/search_routes.py | Full HTTP lifecycle tests for search queries, faceted search, keyword search |
| tests/integration/api/test_admin_api.py | CREATE | src/api/admin_routes.py | Full HTTP lifecycle tests for system configuration, health checks, support ZIP |
| tests/integration/api/test_task_api.py | CREATE | src/api/task_routes.py | Full HTTP lifecycle tests for scheduled task management endpoints |
| tests/integration/api/test_blobstore_api.py | CREATE | src/api/blobstore_routes.py | Full HTTP lifecycle tests for BlobStore configuration and management |
| tests/integration/services/test_repository_integration.py | CREATE | src/services/repository_service.py | Repository service with real database: create, configure, lifecycle transitions |
| tests/integration/services/test_proxy_integration.py | CREATE | src/services/repository_service.py | Proxy repository fetch, cache, and upstream registry interaction with mocked HTTP |
| tests/integration/services/test_storage_integration.py | CREATE | src/services/storage_service.py | Storage service with temp file system: file operations, deduplication, cleanup |
| tests/integration/services/test_auth_integration.py | CREATE | src/services/security_service.py | Authentication chain: login → token → protected endpoint → authorization decision |
| tests/integration/services/test_search_integration.py | CREATE | src/services/search_service.py | Search indexing pipeline: artifact upload → index update → search retrieval |
| tests/functional/test_artifact_lifecycle.py | CREATE | src/api/, src/services/ | End-to-end artifact lifecycle: upload → index → search → download → delete |
| tests/functional/test_repository_lifecycle.py | CREATE | src/api/, src/services/ | End-to-end repository lifecycle: create → configure → populate → browse → cleanup → delete |
| tests/functional/test_auth_workflow.py | CREATE | src/api/, src/services/ | End-to-end auth workflow: register → login → access resource → refresh → logout |
| tests/functional/test_proxy_workflow.py | CREATE | src/api/, src/services/ | End-to-end proxy workflow: configure → fetch from upstream → cache → serve cached |
| tests/functional/test_group_repository.py | CREATE | src/api/, src/services/ | End-to-end group repository: create members → create group → resolve from group |
| tests/fixtures/__init__.py | CREATE | N/A | Package init for test fixtures module |
| tests/fixtures/repository_data.py | CREATE | N/A | Factory functions for repository configuration test data (all types and formats) |
| tests/fixtures/user_data.py | CREATE | N/A | Factory functions for user entities with various RBAC roles |
| tests/fixtures/artifact_data.py | CREATE | N/A | Sample artifact generators for each supported format |
| tests/fixtures/config_data.py | CREATE | N/A | Test configuration dictionaries for all environment profiles |
| tests/mocks/__init__.py | CREATE | N/A | Package init for mock objects module |
| tests/mocks/mock_s3_client.py | CREATE | N/A | Mock S3 client for BlobStore operations without AWS connectivity |
| tests/mocks/mock_search_engine.py | CREATE | N/A | Mock search backend for indexing and query operations |
| tests/mocks/mock_proxy_client.py | CREATE | N/A | Mock HTTP client for upstream proxy registry responses |
| tests/mocks/mock_email_service.py | CREATE | N/A | Mock notification service for webhook and alert testing |

### 0.5.2 New Test Files Detail

**Unit Test Files:**

- `tests/unit/test_app_factory.py` — Application factory validation
  - Test categories: happy path (valid configs), edge cases (missing env vars), errors (invalid DB URI)
  - Mock dependencies: database engine, extension initializers
  - Assertions focus: blueprint count, config values, extension presence

- `tests/unit/services/test_repository_service.py` — Repository management service
  - Test categories: CRUD for all 3 types × 7 formats = 21+ scenario groups
  - Mock dependencies: SQLAlchemy session, storage backend, search engine
  - Assertions focus: return values, state transitions, validation errors

- `tests/unit/services/test_storage_service.py` — Storage backend operations
  - Test categories: upload/download/delete for File BlobStore and S3 BlobStore
  - Mock dependencies: filesystem operations (`os`, `shutil`), boto3 S3 client
  - Assertions focus: file integrity (hash verification), metadata correctness, error propagation

- `tests/unit/services/test_security_service.py` — Authentication and authorization
  - Test categories: JWT lifecycle, API key management, RBAC evaluation
  - Mock dependencies: user repository, token store, time functions
  - Assertions focus: token payload correctness, expiry behavior, privilege matrix

- `tests/unit/models/test_repository_model.py` — Repository data model
  - Test categories: instantiation, validation, relationships, serialization
  - Mock dependencies: none (pure model tests with in-memory DB)
  - Assertions focus: field constraints, defaults, cascading behaviors

**Integration Test Files:**

- `tests/integration/api/test_repository_api.py` — Repository REST API
  - Integration points: route handler → service → model → database
  - Test data requirements: pre-seeded repository configurations via fixtures

- `tests/integration/api/test_asset_api.py` — Artifact management REST API
  - Integration points: route handler → storage service → BlobStore → database
  - Test data requirements: binary artifact payloads, repository contexts

- `tests/integration/services/test_auth_integration.py` — Authentication chain
  - Integration points: login endpoint → security service → user model → JWT
  - Test data requirements: pre-seeded users with various roles

**Functional Test Files:**

- `tests/functional/test_artifact_lifecycle.py` — Full artifact lifecycle
  - Test data requirements: repository configurations, artifact payloads, user credentials

**Fixture Files:**

- `tests/fixtures/repository_data.py` — Repository configuration factories
  - Fixture types: `make_hosted_repo()`, `make_proxy_repo()`, `make_group_repo()` with format parameterization

- `tests/fixtures/user_data.py` — User and role factories
  - Fixture types: `make_admin_user()`, `make_developer_user()`, `make_readonly_user()`, `make_anonymous_user()`

### 0.5.3 Test Configuration Updates

- `pyproject.toml` — Add `[tool.pytest]` section with test paths, markers, and options:
  - `testpaths = ["tests"]`
  - `python_files = ["test_*.py"]`
  - `python_functions = ["test_*"]`
  - `addopts = ["-ra", "-q", "--strict-markers"]`
  - Custom markers: `unit`, `integration`, `functional`, `slow`

- `.coveragerc` or `pyproject.toml [tool.coverage]` — Configure coverage settings:
  - `source = ["src"]`
  - `omit = ["tests/*", "*/migrations/*"]`
  - `fail_under = 80`
  - Branch coverage enabled

- `pytest.ini` (alternative) — Not needed if using `pyproject.toml`

### 0.5.4 Cross-File Test Dependencies

**Shared Fixtures (tests/conftest.py):**

- `app` — Flask application instance created via factory with test config
- `client` — Flask test client bound to the app fixture
- `db` — SQLAlchemy database instance with test schema
- `db_session` — Database session with per-test rollback
- `auth_headers` — Pre-built Authorization headers with valid JWT for authenticated requests
- `admin_user` / `developer_user` / `readonly_user` — Pre-seeded user fixtures

**Mock Objects (tests/mocks/):**

- `mock_s3_client.py` — Used by storage service tests and integration tests involving S3 BlobStore
- `mock_search_engine.py` — Used by search service tests and artifact lifecycle tests
- `mock_proxy_client.py` — Used by proxy integration tests and proxy workflow tests

**Test Utilities (tests/helpers/):**

- `test_helpers.py` — Common assertion helpers (e.g., `assert_json_response()`, `assert_pagination()`)
- `auth_helpers.py` — Token generation shortcuts for test authentication

**Import Updates Required:**

- All test files import from `src.*` modules — requires `src` to be on the Python path (configured via `pyproject.toml` or `conftest.py` path manipulation)
- All integration test files import shared fixtures from `tests/conftest.py` via pytest's automatic fixture discovery
- Mock objects are imported explicitly from `tests/mocks.*` in unit tests

## 0.6 Dependency Inventory

### 0.6.1 Testing Dependencies

The following table lists all key testing packages relevant to this testing exercise. All versions have been verified for compatibility with Python 3.12 and Flask 3.1.x through web search research.

| Registry | Package Name | Version | Purpose |
|---|---|---|---|
| pip | pytest | 8.3.4 | Primary testing framework; test discovery, execution, fixtures, assertions |
| pip | pytest-flask | 1.3.0 | Flask-specific test fixtures (client, live_server, config); automatic request context |
| pip | pytest-cov | 6.0.0 | Coverage measurement plugin; integrates coverage.py with pytest reporting |
| pip | pytest-mock | 3.14.0 | Thin wrapper around unittest.mock providing `mocker` fixture for patching |
| pip | pytest-xdist | 3.5.0 | Parallel test execution across multiple CPU cores for faster test runs |
| pip | pytest-timeout | 2.3.1 | Per-test timeout enforcement to prevent hanging tests |
| pip | coverage | 7.6.9 | Underlying coverage measurement engine used by pytest-cov |
| pip | responses | 0.25.6 | HTTP request mocking library for testing external API calls (proxy fetch) |
| pip | moto | 5.0.24 | AWS service mocking (S3 BlobStore operations) without real AWS credentials |
| pip | factory-boy | 3.3.1 | Test data factory library with SQLAlchemy integration for consistent fixtures |
| pip | Faker | 33.1.0 | Synthetic test data generation (usernames, emails, file contents, etc.) |
| pip | freezegun | 1.4.0 | Time-freezing utility for testing JWT expiration, scheduled tasks, audit timestamps |
| pip | Flask | 3.1.0 | Target web framework (production dependency required for test client) |
| pip | Flask-SQLAlchemy | 3.1.1 | SQLAlchemy integration for Flask (production dependency required by model tests) |
| pip | Flask-JWT-Extended | 4.7.1 | JWT authentication extension (production dependency required by auth tests) |
| pip | Flask-Migrate | 4.0.7 | Database migration via Alembic (production dependency for schema tests) |
| pip | SQLAlchemy | 2.0.36 | ORM engine (production dependency required by database tests) |
| pip | boto3 | 1.35.81 | AWS SDK for S3 operations (production dependency mocked in tests) |

### 0.6.2 Import Updates

Since this is a greenfield project, there are no existing imports to update. However, the following import conventions will be established across all test files:

**Standard Test Imports Pattern:**

- All test files will import from the `src` package namespace:
  - `from src.app import create_app`
  - `from src.services.repository_service import RepositoryService`
  - `from src.models.repository import Repository`

- All test files will import pytest and relevant plugins:
  - `import pytest`
  - `from unittest.mock import patch, MagicMock`

- Fixture files will import from factory-boy and Faker:
  - `import factory`
  - `from faker import Faker`

**Import Path Configuration:**

The `pyproject.toml` must include the appropriate package configuration to ensure `src` is importable in the test context. This can be achieved via:

- Setting `pythonpath = ["src"]` in `[tool.pytest]` section
- Or using a `src` layout with proper `__init__.py` files and editable install (`pip install -e .`)

## 0.7 Coverage and Quality Targets

### 0.7.1 Coverage Metrics

**Current Coverage:** 0% — No source code or tests exist in the repository.

**Target Coverage:** ≥ 80% line coverage based on Python/Flask industry best practices and the tech spec's quality metrics (which specify ≥80% line coverage and ≥70% branch coverage for backend code).

**Coverage Gaps to Address:**

Since this is a greenfield project, all code will be written alongside its tests. The following coverage priorities are established to ensure critical paths are tested first:

| Component | Target Coverage | Priority | Focus Areas |
|---|---|---|---|
| REST API route handlers (`src/api/`) | ≥ 95% | Critical | All HTTP methods, status codes, content negotiation, error responses |
| Security service (`src/services/security_service.py`) | 100% | Critical | JWT lifecycle, API key validation, RBAC privilege checks, session management |
| Repository service (`src/services/repository_service.py`) | ≥ 90% | Critical | CRUD operations, type-specific logic, format handling, lifecycle state transitions |
| Storage service (`src/services/storage_service.py`) | ≥ 90% | Critical | File/S3 BlobStore operations, deduplication, integrity checks |
| Data models (`src/models/`) | ≥ 85% | High | Field validation, relationships, constraints, serialization |
| Search service (`src/services/search_service.py`) | ≥ 85% | High | Indexing, querying, filtering, result pagination |
| Configuration (`src/config.py`) | 100% | High | All environment profiles, required variable validation |
| Utility functions (`src/utils/`) | ≥ 90% | High | Format detection, crypto operations, input validation |
| Scheduler service (`src/services/scheduler_service.py`) | ≥ 80% | Medium | Task scheduling, execution, error handling |
| Health and admin services | ≥ 80% | Medium | Health check aggregation, system info, support ZIP |
| Application factory (`src/app.py`) | ≥ 85% | High | Blueprint registration, middleware setup, error handler registration |

**Per-File Coverage Enforcement:**

The `.coveragerc` or `pyproject.toml [tool.coverage]` configuration will enforce:
- `fail_under = 80` — Build fails if overall coverage drops below 80%
- Branch coverage enabled — ensures conditional logic paths are tested
- Per-file minimum not enforced globally but documented above as guidance

### 0.7.2 Test Quality Criteria

**Assertion Density Expectations:**

- Minimum 2 assertions per test function (verifying both the return value and the side effect or state change)
- API tests must assert: HTTP status code, response content type, response body structure, and key business values
- Security tests must assert: token validity, user identity correctness, privilege evaluation result, and error messages

**Test Isolation Requirements:**

- Every unit test must run independently with no shared mutable state between tests
- Database tests must use per-test transaction rollback to ensure test isolation
- File system tests must use `tmp_path` fixtures for temporary file operations
- Time-dependent tests must use `freezegun` to control clock behavior
- Network-dependent tests must use `responses` or `moto` mocks — no real network calls permitted in unit or integration tests

**Performance Constraints for Test Execution:**

| Test Tier | Maximum Duration | Execution Context |
|---|---|---|
| Individual unit test | < 500ms | Local development, CI/CD |
| Unit test suite (all) | < 2 minutes | CI/CD pipeline |
| Individual integration test | < 2 seconds | CI/CD pipeline |
| Integration test suite (all) | < 10 minutes | CI/CD pipeline |
| Functional test suite (all) | < 15 minutes | CI/CD pipeline, nightly |
| Full test pyramid | < 30 minutes | Pre-release validation |

**Maintainability Standards:**

- Test function names must clearly describe the scenario: `test_{component}_{scenario}_{expected_result}` (e.g., `test_create_repository_with_duplicate_name_returns_409`)
- Test files must not exceed 500 lines; split into multiple files if needed
- Shared setup logic must be extracted into fixtures (never duplicated across test functions)
- Magic numbers and string literals must be replaced with named constants or fixture values
- Docstrings are recommended for complex test scenarios explaining the setup, action, and expected outcome

**Following Repository Test Patterns and Conventions:**

Since the repository is new, the following conventions are established as the baseline pattern for all test code:

- **Arrange-Act-Assert (AAA)** pattern for all test functions
- **pytest fixtures** over setup/teardown methods
- **`@pytest.mark.parametrize`** for data-driven tests with multiple input scenarios
- **`@pytest.mark.slow`** marker for tests exceeding 5 seconds
- **`@pytest.mark.integration`** marker for tests requiring database or external service
- **Plain `assert` statements** over unittest-style assertion methods

## 0.8 Scope Boundaries

### 0.8.1 Exhaustively In Scope

**New Test Files:**

- `tests/unit/test_app_factory.py` — Application factory unit tests
- `tests/unit/test_config.py` — Configuration validation unit tests
- `tests/unit/services/test_repository_service.py` — Repository management unit tests
- `tests/unit/services/test_storage_service.py` — Storage operations unit tests
- `tests/unit/services/test_security_service.py` — Authentication and authorization unit tests
- `tests/unit/services/test_search_service.py` — Search engine unit tests
- `tests/unit/services/test_scheduler_service.py` — Task scheduling unit tests
- `tests/unit/services/test_audit_service.py` — Audit logging unit tests
- `tests/unit/services/test_webhook_service.py` — Webhook dispatch unit tests
- `tests/unit/services/test_health_service.py` — Health check unit tests
- `tests/unit/models/test_repository_model.py` — Repository model unit tests
- `tests/unit/models/test_user_model.py` — User model unit tests
- `tests/unit/models/test_asset_model.py` — Asset/component model unit tests
- `tests/unit/models/test_blobstore_model.py` — BlobStore model unit tests
- `tests/unit/models/test_task_model.py` — Task model unit tests
- `tests/unit/models/test_security_model.py` — Role/privilege model unit tests
- `tests/unit/utils/test_format_utils.py` — Format utility unit tests
- `tests/unit/utils/test_crypto_utils.py` — Cryptographic utility unit tests
- `tests/unit/utils/test_validation_utils.py` — Input validation utility unit tests
- `tests/integration/api/test_repository_api.py` — Repository endpoint integration tests
- `tests/integration/api/test_asset_api.py` — Asset management endpoint integration tests
- `tests/integration/api/test_auth_api.py` — Authentication endpoint integration tests
- `tests/integration/api/test_user_api.py` — User management endpoint integration tests
- `tests/integration/api/test_search_api.py` — Search endpoint integration tests
- `tests/integration/api/test_admin_api.py` — Admin endpoint integration tests
- `tests/integration/api/test_task_api.py` — Task management endpoint integration tests
- `tests/integration/api/test_blobstore_api.py` — BlobStore endpoint integration tests
- `tests/integration/services/test_repository_integration.py` — Repository service integration tests
- `tests/integration/services/test_proxy_integration.py` — Proxy fetch integration tests
- `tests/integration/services/test_storage_integration.py` — Storage service integration tests
- `tests/integration/services/test_auth_integration.py` — Auth chain integration tests
- `tests/integration/services/test_search_integration.py` — Search pipeline integration tests
- `tests/functional/test_artifact_lifecycle.py` — Artifact lifecycle functional tests
- `tests/functional/test_repository_lifecycle.py` — Repository lifecycle functional tests
- `tests/functional/test_auth_workflow.py` — Authentication workflow functional tests
- `tests/functional/test_proxy_workflow.py` — Proxy fetch workflow functional tests
- `tests/functional/test_group_repository.py` — Group repository resolution functional tests

**Test Infrastructure Files:**

- `tests/__init__.py` — Package init for tests module
- `tests/conftest.py` — Root pytest configuration with shared fixtures
- `tests/unit/__init__.py` — Package init for unit tests
- `tests/unit/conftest.py` — Unit test scoped fixtures
- `tests/unit/services/__init__.py` — Package init for service unit tests
- `tests/unit/services/conftest.py` — Service unit test fixtures
- `tests/unit/models/__init__.py` — Package init for model unit tests
- `tests/unit/models/conftest.py` — Model unit test fixtures
- `tests/unit/utils/__init__.py` — Package init for utility unit tests
- `tests/integration/__init__.py` — Package init for integration tests
- `tests/integration/conftest.py` — Integration test scoped fixtures
- `tests/integration/api/__init__.py` — Package init for API integration tests
- `tests/integration/services/__init__.py` — Package init for service integration tests
- `tests/functional/__init__.py` — Package init for functional tests
- `tests/functional/conftest.py` — Functional test scoped fixtures

**Test Data and Mock Files:**

- `tests/fixtures/__init__.py` — Package init for fixtures module
- `tests/fixtures/repository_data.py` — Repository test data factories
- `tests/fixtures/user_data.py` — User/role test data factories
- `tests/fixtures/artifact_data.py` — Artifact test data generators
- `tests/fixtures/config_data.py` — Configuration test data
- `tests/mocks/__init__.py` — Package init for mocks module
- `tests/mocks/mock_s3_client.py` — S3 client mock
- `tests/mocks/mock_search_engine.py` — Search engine mock
- `tests/mocks/mock_proxy_client.py` — Proxy HTTP client mock
- `tests/mocks/mock_email_service.py` — Email/notification service mock

**Test Configuration Files:**

- `pyproject.toml` — pytest configuration (`[tool.pytest]`), coverage configuration (`[tool.coverage]`)
- `.coveragerc` (optional) — Coverage settings if not in pyproject.toml

### 0.8.2 Explicitly Out of Scope

- **Source code implementation** — The actual Flask application source code (`src/`) is out of scope for this testing plan; test files define what needs to be tested, not the implementation itself
- **Frontend or UI testing** — The tech spec mentions React/ExtJS UI components, but the user's request is specifically for the Node.js server (backend) rewrite to Flask; no frontend test files are included
- **Performance and load testing** — While the tech spec defines performance benchmarks (≥20% throughput, ≥15% latency reduction), dedicated load testing tools (Locust, k6) and their configuration are out of scope for this unit/integration testing plan
- **Infrastructure provisioning** — Docker, Kubernetes, or cloud infrastructure setup for test environments is out of scope
- **CI/CD pipeline configuration** — While test execution commands are documented, the actual CI/CD pipeline files (GitHub Actions, GitLab CI, etc.) are not part of this test creation scope
- **Database migration scripts** — Alembic migration files themselves are out of scope; only migration validation tests are included
- **Third-party library internal testing** — Tests should not retest Flask, SQLAlchemy, pytest, or other dependency internals
- **Original Node.js codebase** — No Node.js source code exists in the repository; tests are designed against the Flask application specification
- **Security penetration testing** — OWASP-style penetration tests and vulnerability scanning are out of scope
- **Browser-based E2E testing** — Selenium, Playwright, or Cypress browser tests are out of scope
- **API documentation testing** — Swagger/OpenAPI spec validation is out of scope for this test creation plan

## 0.9 Execution Parameters

### 0.9.1 Testing-Specific Instructions

**Test Execution Commands:**

- Run all tests:
  ```bash
  python -m pytest tests/ -v --tb=short
  ```

- Run only unit tests:
  ```bash
  python -m pytest tests/unit/ -v --tb=short -m "not integration"
  ```

- Run only integration tests:
  ```bash
  python -m pytest tests/integration/ -v --tb=short -m integration
  ```

- Run only functional tests:
  ```bash
  python -m pytest tests/functional/ -v --tb=short -m functional
  ```

**Coverage Measurement Command:**

```bash
python -m pytest tests/ --cov=src --cov-report=term-missing --cov-report=html --cov-branch --cov-fail-under=80
```

**Single Test Execution Pattern:**

```bash
python -m pytest tests/unit/services/test_repository_service.py::test_create_hosted_repository_success -v
```

**Debug Mode Execution:**

```bash
python -m pytest tests/ -v --tb=long -s --pdb
```

**Parallel Execution Command:**

```bash
python -m pytest tests/ -n auto --dist worksteal
```

**Specific Test Patterns to Follow:**

- All test functions use the `test_` prefix
- All test modules use the `test_` prefix
- Pytest markers are used for categorization: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.functional`, `@pytest.mark.slow`
- Parametrized tests use `@pytest.mark.parametrize` with descriptive IDs
- Fixtures follow the scope hierarchy: `session` → `module` → `class` → `function`

**Excluded Test Categories:**

- No performance/load test category (out of scope per section 0.8)
- No browser-based E2E test category (out of scope per section 0.8)

**Environment Setup Requirements for Tests:**

- Python 3.12 virtual environment with all dependencies from `pyproject.toml` installed
- `FLASK_ENV=testing` environment variable set
- `DATABASE_URL=sqlite:///:memory:` for unit and integration tests (or overridable for CI)
- `SECRET_KEY=test-secret-key-not-for-production` for Flask session and JWT signing
- `S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` not required (mocked via `moto`)
- `SEARCH_ENGINE_URL` not required (mocked in tests)
- No external services required to be running for test execution

## 0.10 Special Instructions for Testing

### 0.10.1 Testing-Specific Requirements

The following special instructions govern the testing approach for this Node.js-to-Flask rewrite project:

- **Preserve all functionalities:** The user explicitly stated "preserving all functionalities of the original project." Every feature defined in the technical specification's feature catalog (F-101 through F-504, encompassing 20 features across 5 categories) must have corresponding test coverage in the Flask rewrite. Tests serve as the verification mechanism for functional parity.

- **Greenfield test creation:** Since the repository is empty and no original Node.js tests exist to reference, all test files, fixtures, configuration, and infrastructure must be created from scratch. There are no existing patterns to follow within the repository; Flask's official testing documentation and pytest conventions serve as the authoritative style guide.

- **Application factory pattern mandatory:** All tests must use Flask's application factory pattern (`create_app()`) to enable testing configuration injection. The `app` fixture in `conftest.py` must create the application with `TESTING=True` and an in-memory SQLite database.

- **DO NOT modify source code unless absolutely necessary for testability:** Tests are designed to validate the Flask application as specified. If a source module lacks testability (e.g., hard-coded dependencies without dependency injection), document the required refactoring as a note within the test file rather than modifying the source.

- **Follow pytest conventions exclusively:** Do not use `unittest.TestCase` classes or `setUp/tearDown` methods. All tests must use pytest's functional approach with fixtures, plain `assert` statements, and `@pytest.mark` decorators.

- **Ensure all tests can run independently and in parallel:** Every test function must be fully self-contained with no dependencies on execution order. Database state must be isolated per test via transaction rollback. Temporary files must use unique paths. This enables parallel execution with `pytest-xdist`.

- **Maintain backward compatibility in test utilities:** Shared fixtures and helper functions in `tests/conftest.py` and `tests/fixtures/` must provide stable interfaces that do not break existing tests when new tests are added.

- **Match consistent naming conventions:** Test files mirror source files: `src/services/repository_service.py` → `tests/unit/services/test_repository_service.py`. Test functions follow the pattern: `test_{method_or_feature}_{scenario}_{expected_outcome}`.

- **Mock all external dependencies:** No test may make real network calls, access real cloud storage, or depend on external services. All external interactions must be mocked using `responses`, `moto`, `unittest.mock`, or custom mock classes in `tests/mocks/`.

- **Multi-format repository testing:** Given that the system supports 7 repository formats (Maven, npm, Docker, NuGet, PyPI, APT, Raw), format-specific behavior must be tested via parametrized tests that exercise each format's unique coordinate system, content types, and metadata handling.

- **Security testing rigor:** Authentication and authorization tests must cover all 5 authentication methods referenced in the tech spec (username/password, JWT bearer token, API key, session-based, anonymous access) and the 3-tier RBAC model (global roles, repository-specific permissions, content selectors).

- **No test file left pending:** Every test file listed in section 0.5 must be fully defined with complete test cases. No file may be deferred to future work or marked as a placeholder.

