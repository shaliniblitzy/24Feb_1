"""
Marshmallow serialization schemas for the Nexus Repository Flask application.

Replaces Jackson 2.16.1 JSON DTOs from the Java source. Provides request/response
serialization for all REST API endpoints using Marshmallow 3.x.

Modules:
- ``repository.py``:  Repository CRUD request/response schemas
- ``asset.py``:       Asset metadata schemas
- ``system.py``:      System configuration schemas
- ``component.py``:   Component schemas (future checkpoint)
- ``user.py``:        User schemas (future checkpoint)
- ``role.py``:        Role schemas (future checkpoint)
- ``task.py``:        Task schemas (future checkpoint)
- ``search.py``:      Search query/result schemas (future checkpoint)
"""

from src.app.schemas.repository import (  # noqa: F401
    RepositoryCreateSchema,
    RepositoryUpdateSchema,
    RepositoryResponseSchema,
)
from src.app.schemas.asset import AssetSchema  # noqa: F401
from src.app.schemas.system import SystemConfigSchema  # noqa: F401
from src.app.schemas.role import (  # noqa: F401
    RoleSchema,
    RoleAssignmentSchema,
)
from src.app.schemas.task import (  # noqa: F401
    TaskCreateSchema,
    TaskDefinitionSchema,
    TaskExecutionSchema,
)
from src.app.schemas.search import (  # noqa: F401
    SearchQuerySchema,
    SearchResultSchema,
)

