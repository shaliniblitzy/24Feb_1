# Nexus Repository Manager — Documentation

This directory contains comprehensive documentation for the **Nexus Repository Manager** Python/Flask application, covering architecture, API references, configuration, and deployment guides.

The Nexus Repository Manager is a universal binary repository manager — originally implemented in Java 21 with OSGi/Karaf, Guice, JAX-RS, Shiro, Quartz, and MyBatis — rewritten as a Python 3.13 application powered by Flask 3.1.3. Every documented capability of the original system is preserved in this rewrite.

---

## Documentation Index

### Architecture & Design

- [Architecture Overview](architecture.md) — Five-layer architecture, dual-persistence pattern (DataStore + BlobStore), modular monolith design (ADR-001), six-phase startup sequence (KERNEL → SCHEMAS → STORAGE → SECURITY → CAPABILITIES → SERVICES), blinker event system, multi-realm authentication chain, and three-tier RBAC model

### API Reference

- [API Documentation Index](api/README.md) — Overview of all REST API endpoints, authentication methods, common request/response patterns, pagination, and error handling conventions
  - [Repository Management API](api/repositories.md) — Repository CRUD, format-native protocols (Maven, npm, Docker, NuGet, PyPI, APT, Raw), component and asset management, content search, and browse tree navigation (F-101–F-104)
  - [Security Management API](api/security.md) — User, role, and privilege management, Content Selector Expression Language (CSEL), SSL/TLS certificate lifecycle, JSON-serialized audit logging with SIEM integration, and API key authentication for CI/CD pipelines (F-301–F-304)
  - [Administration API](api/admin.md) — Health check monitoring with component and system checks, scheduled task management with cron-style scheduling, diagnostic support ZIP generation with credential redaction, and hierarchical system configuration management (F-401–F-404)

### Configuration

- [Configuration Reference](configuration.md) — All YAML settings, environment variable overrides, deployment model-specific configurations, and configuration loading precedence (default YAML → environment YAML → environment variables)

### Deployment Guides

- [Standalone Deployment](deployment/standalone.md) — Single-server deployment with SQLite + File BlobStore + Whoosh search engine
- [Clustered Deployment](deployment/clustered.md) — Multi-node high-availability deployment with PostgreSQL 16 + Shared Filesystem (NFS/GlusterFS) + Elasticsearch 8.x
- [Container-Native Deployment](deployment/container.md) — Docker/Kubernetes deployment with PostgreSQL (RDS/CloudSQL) + S3 BlobStore (SSE-S3/SSE-KMS encryption) + Elasticsearch 8.x

---

## Quick Navigation

| Document | Description |
|----------|-------------|
| [Architecture](architecture.md) | System design, five-layer architecture, technology stack mapping |
| [API Reference](api/README.md) | REST API endpoints, authentication, protocols |
| [Configuration](configuration.md) | Settings, environment variables, deployment profiles |
| [Standalone Deploy](deployment/standalone.md) | SQLite + File BlobStore setup |
| [Clustered Deploy](deployment/clustered.md) | PostgreSQL + Shared Filesystem setup |
| [Container Deploy](deployment/container.md) | Docker + Kubernetes + S3 setup |

---

## Project Overview

**Nexus Repository Manager** is a universal binary repository manager that stores, organizes, and distributes software artifacts across the entire development lifecycle.

### Technology Stack

This application is a complete rewrite from Java 21 to Python 3.13, replacing every major Java component with an idiomatic Python equivalent:

| Original (Java) | Python Equivalent |
|-----------------|-------------------|
| Jetty 12.0.5 + RESTEasy 6.2.7 | Flask 3.1.3 + Gunicorn 23.0.0 |
| Apache Shiro 2.0.0 | Flask-Login 0.6.3 + PyJWT 2.10.1 + bcrypt 4.2.1 |
| Guice 7.0.0 | Flask application context + service registry |
| MyBatis 3.5.15 + HikariCP 4.0.3 | SQLAlchemy 2.0.46 |
| Quartz 2.3.2 | APScheduler 3.11.2 |
| Guava EventBus | blinker (Flask signals) |
| Flyway 8.5.13 | Alembic 1.14.1 |
| Jackson 2.16.1 | marshmallow 3.25.1 |
| Swagger 2.2.20 | flask-smorest 0.45.0 |
| Dropwizard Metrics 4.2.25 | prometheus-client 0.21.1 |
| BouncyCastle 1.78.1 | cryptography 44.0.0 |
| Elasticsearch 2.4.3 | Whoosh 2.7.4 / elasticsearch 8.17.0 |

### Supported Repository Formats

Seven repository formats are supported, each with full format-native protocol handling:

| Format | Protocol | Client Tools |
|--------|----------|-------------|
| **Maven** | Maven Repository Layout | Maven CLI, Gradle |
| **npm** | npm Registry Protocol | npm, yarn |
| **Docker** | Docker Registry HTTP API V2 | docker CLI |
| **NuGet** | NuGet V3 Protocol | dotnet CLI, NuGet |
| **PyPI** | PEP 503 Simple Repository API | pip, twine |
| **APT** | Debian Repository Format | apt-get |
| **Raw** | Generic HTTP | curl, wget |

### Repository Types

- **Hosted** — Local artifact storage with direct upload capability
- **Proxy** — Remote repository cache with configurable TTL and negative caching
- **Group** — Virtual aggregation of multiple repositories with ordered member traversal

### Deployment Models

- **Standalone** — SQLite + File BlobStore + Whoosh (single-server, evaluation)
- **Clustered** — PostgreSQL + Shared Filesystem + Elasticsearch (enterprise HA)
- **Container-Native** — PostgreSQL + S3 + Elasticsearch (cloud-native, Docker/K8s)

### Feature Summary

The application delivers 20 features across 5 categories:

| Category | Features | IDs |
|----------|----------|-----|
| **Repository Management** | Multi-format artifact support, three repository types (Hosted/Proxy/Group), content indexing and search, browse tree navigation | F-101, F-102, F-103, F-104 |
| **Storage Management** | File-based BlobStore, S3 BlobStore with SSE-S3/SSE-KMS encryption, BlobStore maintenance (compaction, integrity verification), configurable cleanup policies with format-specific retention rules | F-201, F-202, F-203, F-204 |
| **Security** | Three-tier RBAC with Content Selector Expression Language (CSEL), SSL/TLS certificate lifecycle management, JSON-serialized audit logging with SIEM integration, API key authentication for CI/CD pipelines | F-301, F-302, F-303, F-304 |
| **Administration** | Health check monitoring, scheduled task management with cron-style scheduling, diagnostic support ZIP generation with credential redaction, hierarchical system configuration management | F-401, F-402, F-403, F-404 |
| **Integration & Extensibility** | Comprehensive REST API with OpenAPI documentation, server-side scripting engine, webhook event notifications with HMAC signing, plugin architecture for format extensibility | F-501, F-502, F-503, F-504 |

---

## Getting Started

Choose your starting point based on your role:

- **New developers** — Start with the [Architecture Overview](architecture.md) to understand the five-layer design, dual-persistence pattern, and module organization
- **API consumers** — Start with the [API Reference](api/README.md) for endpoint documentation, authentication methods, and request/response schemas
- **System administrators** — Start with the appropriate deployment guide ([Standalone](deployment/standalone.md), [Clustered](deployment/clustered.md), or [Container-Native](deployment/container.md)) and the [Configuration Reference](configuration.md) for all available settings
- **Quick setup** — See the project [README](../README.md) at the repository root for installation instructions and quickstart commands

---

## Related Resources

- **Project README** — See [../README.md](../README.md) for quickstart installation, dependency setup, and development instructions
- **OpenAPI Documentation** — When the application is running, the full OpenAPI 3.x specification is available at `/swagger.json` (generated by flask-smorest)
- **Prometheus Metrics** — Application metrics are exposed at `/service/metrics` in Prometheus text exposition format, covering request latency, authentication attempts, blob operations, active repositories, and scheduler executions
- **Health Check Endpoint** — System health status is available at `/service/rest/v1/status/check`, reporting component status for the database, BlobStore, scheduler, and search engine
