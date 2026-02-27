"""
Marshmallow serialization schemas for the Nexus Repository application.

This package contains Marshmallow 3.x schema definitions for all API
request/response objects, replacing Jackson 2.16.1 JSON serialization
from the Java source system.

Each schema mirrors a corresponding SQLAlchemy model from src.app.models/
and defines field types with validation constraints, nested relationships,
load/dump transformations, and custom field validators.

Schemas are organized by entity type:
- repository.py — Repository CRUD schemas
- component.py — Component search and detail schemas
- asset.py — Asset metadata schemas
- user.py — User management schemas
- role.py — Role and privilege assignment schemas
- task.py — Task scheduling and execution schemas
- system.py — System configuration and status schemas
- search.py — Search query and result schemas
"""

# ---------------------------------------------------------------------------
# Repository schemas (F-101, F-102, F-501-RQ-001)
# ---------------------------------------------------------------------------
from src.app.schemas.repository import (
    RepositoryCreateSchema,
    RepositoryUpdateSchema,
    RepositoryResponseSchema,
)

# ---------------------------------------------------------------------------
# Component schemas (F-103, F-501-RQ-002)
# ---------------------------------------------------------------------------
from src.app.schemas.component import (
    ComponentSchema,
    ComponentSearchResultSchema,
)

# ---------------------------------------------------------------------------
# Asset schemas (F-204, F-501-RQ-002)
# ---------------------------------------------------------------------------
from src.app.schemas.asset import (
    AssetSchema,
    AssetResponseSchema,
)

# ---------------------------------------------------------------------------
# User schemas (F-301, F-501-RQ-003)
# ---------------------------------------------------------------------------
from src.app.schemas.user import (
    UserCreateSchema,
    UserUpdateSchema,
    UserResponseSchema,
)

# ---------------------------------------------------------------------------
# Role schemas (F-301, F-501-RQ-003)
# ---------------------------------------------------------------------------
from src.app.schemas.role import (
    RoleSchema,
    RoleAssignmentSchema,
)

# ---------------------------------------------------------------------------
# Task schemas (F-402, F-501-RQ-004)
# ---------------------------------------------------------------------------
from src.app.schemas.task import (
    TaskDefinitionSchema,
    TaskExecutionSchema,
    TaskCreateSchema,
)

# ---------------------------------------------------------------------------
# System schemas (F-401, F-404, F-501-RQ-004)
# ---------------------------------------------------------------------------
from src.app.schemas.system import (
    SystemConfigSchema,
    SystemStatusSchema,
    LicenseInfoSchema,
)

# ---------------------------------------------------------------------------
# Search schemas (F-103)
# ---------------------------------------------------------------------------
from src.app.schemas.search import (
    SearchQuerySchema,
    SearchResultSchema,
)

# ---------------------------------------------------------------------------
# Public API — all schema classes available via ``from src.app.schemas import *``
# ---------------------------------------------------------------------------
__all__ = [
    # Repository
    "RepositoryCreateSchema",
    "RepositoryUpdateSchema",
    "RepositoryResponseSchema",
    # Component
    "ComponentSchema",
    "ComponentSearchResultSchema",
    # Asset
    "AssetSchema",
    "AssetResponseSchema",
    # User
    "UserCreateSchema",
    "UserUpdateSchema",
    "UserResponseSchema",
    # Role
    "RoleSchema",
    "RoleAssignmentSchema",
    # Task
    "TaskDefinitionSchema",
    "TaskExecutionSchema",
    "TaskCreateSchema",
    # System
    "SystemConfigSchema",
    "SystemStatusSchema",
    "LicenseInfoSchema",
    # Search
    "SearchQuerySchema",
    "SearchResultSchema",
]
