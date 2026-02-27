"""
NuGet repository format handler sub-package.

Implements the NuGet V3 repository format (F-101-RQ-006) for .NET
package management. Supports:
- NuGet V3 API protocol (Service Index, Package Content, Search, Registrations)
- .nupkg package upload/download with .nuspec metadata extraction
- SemVer pre-release version detection (e.g., 1.0.0-beta, 2.0.0-rc.1)
- NuGet V3 protocol pagination and version resolution
- Package publish endpoint (PUT /api/v2/package)
- Catalog resource for package change tracking

The format_name is 'nuget' matching the Repository model's format enum.
"""

from src.app.formats.nuget.handler import NugetFormatHandler
from src.app.formats.nuget.v3_api import (
    generate_registration_index,
    generate_search_response,
    generate_service_index,
)

__all__: list[str] = [
    "NugetFormatHandler",
    "generate_service_index",
    "generate_registration_index",
    "generate_search_response",
]
