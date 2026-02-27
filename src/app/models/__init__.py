"""
SQLAlchemy data models for the Nexus Repository application.

This package contains all 14 DataStore entity models reimplemented
as SQLAlchemy 2.x ORM models, replacing the MyBatis 3.5.15 mappers
from the Java source system.

All models are imported here for unified access and to ensure
proper registration with SQLAlchemy's metadata.  Importing every
model into this ``__init__.py`` guarantees that SQLAlchemy's
``MetaData`` instance (accessed via ``db.metadata``) discovers all
table definitions — a prerequisite for:

- ``db.create_all()`` in ``factory.py`` to create all tables at
  startup (development/testing).
- **Alembic** (via Flask-Migrate) to detect all tables during
  auto-generated migration scripts.

**Entity Summary (14 DataStore entities → 16 model classes):**

+---------------------+--------------------+-------------------------------+
| Model               | Source Module      | DataStore Entity              |
+=====================+====================+===============================+
| Repository          | repository.py      | REPOSITORY                    |
+---------------------+--------------------+-------------------------------+
| Component           | component.py       | COMPONENT                     |
+---------------------+--------------------+-------------------------------+
| Asset               | asset.py           | ASSET                         |
+---------------------+--------------------+-------------------------------+
| User                | user.py            | USER                          |
+---------------------+--------------------+-------------------------------+
| Role                | role.py            | ROLE                          |
+---------------------+--------------------+-------------------------------+
| RoleAssignment      | role.py            | ROLE_ASSIGNMENT               |
+---------------------+--------------------+-------------------------------+
| Privilege           | privilege.py       | PRIVILEGE                     |
+---------------------+--------------------+-------------------------------+
| ContentSelector     | content_selector.py| CONTENT_SELECTOR              |
+---------------------+--------------------+-------------------------------+
| TaskDefinition      | task.py            | TASK_DEFINITION               |
+---------------------+--------------------+-------------------------------+
| TaskExecution       | task.py            | TASK_EXECUTION                |
+---------------------+--------------------+-------------------------------+
| AuditEvent          | audit_event.py     | AUDIT_EVENT                   |
+---------------------+--------------------+-------------------------------+
| BlobStoreConfig     | blobstore_config.py| BLOBSTORE_CONFIG              |
+---------------------+--------------------+-------------------------------+
| CleanupPolicy       | cleanup_policy.py  | CLEANUP_POLICY                |
+---------------------+--------------------+-------------------------------+
| SystemConfig        | system_config.py   | SYSTEM_CONFIG                 |
+---------------------+--------------------+-------------------------------+

Additionally, the four foundational base classes and mixins from
``base.py`` are re-exported for convenience:

- ``BaseModel`` — Abstract base class with ``to_dict()``, ``save()``,
  ``delete()``, ``update()``.
- ``TimestampMixin`` — Automatic ``created_at`` / ``updated_at`` columns.
- ``SoftDeleteMixin`` — Logical (soft) deletion support.
- ``JSONAttributesMixin`` — Extensible JSON attributes column.

**Usage:**

Consumers should import models from this package rather than from
individual submodules::

    # Preferred (single import point):
    from src.app.models import Repository, Component, Asset

    # Also valid for base classes and mixins:
    from src.app.models import BaseModel, TimestampMixin

**Compatibility:**

- Python 3.12+ (AAP Section 0.7.2)
- No Java dependencies (AAP Section 0.7.2)
- Uses ``src.app`` package namespace for all internal imports
  (AAP Section 0.5.2)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Base model class and reusable mixins
# ---------------------------------------------------------------------------
# These foundational classes are defined in base.py and inherited by all
# concrete model classes below.  Re-exporting them here allows consumers
# to access the entire model hierarchy from a single import path.
# ---------------------------------------------------------------------------

from src.app.models.base import (
    BaseModel,
    JSONAttributesMixin,
    SoftDeleteMixin,
    TimestampMixin,
)

# ---------------------------------------------------------------------------
# Repository Management models (F-101, F-102, F-404)
# ---------------------------------------------------------------------------

from src.app.models.repository import Repository
from src.app.models.component import Component
from src.app.models.asset import Asset

# ---------------------------------------------------------------------------
# Security / RBAC models (F-301, F-303, F-304)
# ---------------------------------------------------------------------------

from src.app.models.user import User
from src.app.models.role import Role, RoleAssignment
from src.app.models.privilege import Privilege
from src.app.models.content_selector import ContentSelector

# ---------------------------------------------------------------------------
# Scheduling models (F-402)
# ---------------------------------------------------------------------------

from src.app.models.task import TaskDefinition, TaskExecution

# ---------------------------------------------------------------------------
# Audit Logging model (F-303)
# ---------------------------------------------------------------------------

from src.app.models.audit_event import AuditEvent

# ---------------------------------------------------------------------------
# Storage Management models (F-201, F-202)
# ---------------------------------------------------------------------------

from src.app.models.blobstore_config import BlobStoreConfig

# ---------------------------------------------------------------------------
# Cleanup Policy model (F-204)
# ---------------------------------------------------------------------------

from src.app.models.cleanup_policy import CleanupPolicy

# ---------------------------------------------------------------------------
# System Configuration model (F-404)
# ---------------------------------------------------------------------------

from src.app.models.system_config import SystemConfig

# ---------------------------------------------------------------------------
# Public API — explicit re-export list
# ---------------------------------------------------------------------------
# Contains exactly 18 names:
#   4 base classes/mixins + 14 entity model classes
#     (12 entity modules, with role.py exporting 2 and task.py exporting 2)
# ---------------------------------------------------------------------------

__all__: list[str] = [
    # Base classes and mixins (from base.py)
    "BaseModel",
    "TimestampMixin",
    "SoftDeleteMixin",
    "JSONAttributesMixin",
    # Repository Management entities
    "Repository",
    "Component",
    "Asset",
    # Security / RBAC entities
    "User",
    "Role",
    "RoleAssignment",
    "Privilege",
    "ContentSelector",
    # Scheduling entities
    "TaskDefinition",
    "TaskExecution",
    # Audit Logging entity
    "AuditEvent",
    # Storage Management entity
    "BlobStoreConfig",
    # Cleanup Policy entity
    "CleanupPolicy",
    # System Configuration entity
    "SystemConfig",
]
