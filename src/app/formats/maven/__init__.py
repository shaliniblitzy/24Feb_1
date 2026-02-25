"""
Maven repository format handler sub-package.

Implements the Maven2 repository format (F-101-RQ-001) — the most complex
format handler in the Nexus Repository system. Supports:
- Maven2 repository layout convention for artifact storage
- POM XML parsing and validation
- Maven coordinate extraction (groupId, artifactId, version, classifier, extension)
- maven-metadata.xml generation and merging for group repositories
- SNAPSHOT versioning support
- Checksum sidecar file handling (.md5, .sha1, .sha256, .sha512)

The format_name is 'maven2' (NOT 'maven') to match the original Java
implementation and the Repository model's format enum.
"""

from src.app.formats.maven.handler import MavenFormatHandler
from src.app.formats.maven.metadata import (
    generate_artifact_metadata,
    generate_group_metadata,
    generate_snapshot_metadata,
    merge_metadata,
)

__all__ = [
    "MavenFormatHandler",
    "generate_group_metadata",
    "generate_artifact_metadata",
    "generate_snapshot_metadata",
    "merge_metadata",
]
