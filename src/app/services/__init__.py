"""
Business Logic Service Layer Package.

Implements the service layer (AAP Section 0.4.3 — Service Layer pattern) that
encapsulates all business logic for the Nexus Repository Flask application.
Each service class replaces a corresponding Java service component from the
original Sonatype Nexus Repository system.

**Architecture Context:**

This package sits between the API layer (``src.app.api.*``) and the data
access layer (``src.app.models.*``).  API route handlers delegate to service
classes for business logic; services interact with SQLAlchemy models and
the BlobStore abstraction for persistence.

**Service Inventory:**

+----------------------------+----------------------------------------------+
| Service Class              | Replaces (Java Source)                       |
+============================+==============================================+
| RepositoryManager          | RepositoryManagerImpl.java                   |
| UploadManager              | UploadManagerImpl.java                       |
| ProxyService               | HttpClientFacetImpl + ProxyFacetSupport      |
| GroupService               | Group facet resolution logic                 |
| SearchService              | Elasticsearch integration (F-103)            |
| BlobStoreService           | BlobStore API                                |
| CleanupService             | Cleanup evaluators (F-204)                   |
| ConfigService              | Configuration management (F-404)             |
| ScriptService              | Script plugin (F-502)                        |
| SupportZipService          | Support ZIP (F-403)                          |
+----------------------------+----------------------------------------------+

**Dependency Injection Pattern:**

Service instances are created on demand within Flask request contexts and
access the SQLAlchemy session via ``src.app.extensions.db``.  Services
receive no constructor-injected dependencies — they use the Flask app
context to access configuration and extension singletons.

Exports:
    All public service classes are re-exported from this package for
    convenient ``from src.app.services import <ServiceClass>`` usage.
"""

from src.app.services.repository_manager import (
    RepositoryManager,
    RepositoryError,
    RepositoryNotFoundError,
    RepositoryExistsError,
    InvalidRepositoryConfigError,
    InvalidStateTransitionError,
    RepositoryOfflineError,
)
from src.app.services.upload_manager import UploadManager, UploadError
from src.app.services.proxy_service import ProxyService
from src.app.services.group_service import GroupService
from src.app.services.search_service import SearchService
from src.app.services.blobstore_service import (
    BlobStoreService,
    BlobStoreError,
    BlobStoreInUseError,
    BlobStoreNotFoundError,
    BlobStoreConfigError,
)
from src.app.services.cleanup_service import CleanupService
from src.app.services.config_service import ConfigService
from src.app.services.script_service import (
    ScriptService,
    ScriptError,
    ScriptValidationError,
    ScriptExecutionError,
    ScriptTimeoutError,
)
from src.app.services.support_zip_service import SupportZipService

__all__: list[str] = [
    # Repository Management (replaces RepositoryManagerImpl.java)
    "RepositoryManager",
    "RepositoryError",
    "RepositoryNotFoundError",
    "RepositoryExistsError",
    "InvalidRepositoryConfigError",
    "InvalidStateTransitionError",
    "RepositoryOfflineError",
    # Upload Management (replaces UploadManagerImpl.java)
    "UploadManager",
    "UploadError",
    # Proxy Service (replaces HttpClientFacetImpl + ProxyFacetSupport)
    "ProxyService",
    # Group Service (replaces Group facet resolution)
    "GroupService",
    # Search Service (Elasticsearch integration — F-103)
    "SearchService",
    # BlobStore Service (replaces BlobStore API)
    "BlobStoreService",
    "BlobStoreError",
    "BlobStoreInUseError",
    "BlobStoreNotFoundError",
    "BlobStoreConfigError",
    # Cleanup Service (replaces Cleanup evaluators — F-204)
    "CleanupService",
    # Configuration Service (replaces Configuration management — F-404)
    "ConfigService",
    # Script Service (replaces Script plugin — F-502)
    "ScriptService",
    "ScriptError",
    "ScriptValidationError",
    "ScriptExecutionError",
    "ScriptTimeoutError",
    # Support ZIP Service (replaces Support ZIP — F-403)
    "SupportZipService",
]
