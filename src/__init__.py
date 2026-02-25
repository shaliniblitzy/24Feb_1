"""
Nexus Repository Manager - Python/Flask Implementation

A universal binary repository manager supporting Maven, npm, Docker,
NuGet, PyPI, APT, and Raw formats. Rewritten from Java 21 to Python 3.13
with Flask 3.1.3.

Architecture:
- Five-layer structure: UI Framework -> API Layer -> Core Framework
  -> Repository Layer -> Storage Layer
- Dual-persistence: DataStore (SQLAlchemy) + BlobStore (File/S3)
- Modular monolith with Flask blueprints (ADR-001)
- Event system via blinker signals
- Six-phase startup: KERNEL -> SCHEMAS -> STORAGE -> SECURITY
  -> CAPABILITIES -> SERVICES

Deployment Models:
- Standalone: SQLite + File BlobStore
- Clustered: PostgreSQL + Shared Filesystem
- Container-Native: PostgreSQL + S3 BlobStore

Supported Formats:
- Maven (POM, JAR, metadata XML, snapshot versioning)
- npm (packument, tarball, scoped packages)
- Docker (Registry HTTP API V2, manifests, blobs)
- NuGet (V3 service index, package content)
- PyPI (Simple Repository API, PEP 503)
- APT (Packages, Release, InRelease)
- Raw (generic binary storage)
"""

# Semantic version for the Nexus Repository Manager Python/Flask application.
# Follows Semantic Versioning 2.0.0 (https://semver.org/).
__version__ = '1.0.0'

# Public API surface for the src package.
# Note: create_app is intentionally NOT imported at package level to prevent
# circular import issues during application bootstrapping and testing.
# Import it directly where needed: from src.app import create_app
__all__ = ['__version__']
