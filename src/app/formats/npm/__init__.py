"""
npm repository format handler sub-package.

Implements the npm registry format (F-101-RQ-004) for Node.js/JavaScript
package management. Supports:
- npm registry protocol for package publish and install
- Scoped packages (@scope/package-name)
- Package document (full metadata) and abbreviated (corgi) document generation
- dist-tags management (latest, next, etc.)
- Tarball upload/download with integrity hash verification (sha512 SRI format)
- npm-specific headers (npm-session, npm-command)
- Semver pre-release version detection

The format_name is 'npm' matching the Repository model's format enum.
"""

from src.app.formats.npm.handler import NpmFormatHandler
from src.app.formats.npm.metadata import (
    generate_package_document,
    generate_abbreviated_document,
    generate_version_metadata,
    merge_package_documents,
)

__all__: list[str] = [
    "NpmFormatHandler",
    "generate_package_document",
    "generate_abbreviated_document",
    "generate_version_metadata",
    "merge_package_documents",
]
