"""
APT/Debian Format Handler Sub-Package.

Implements the APT/Debian repository format (F-101-RQ-003) for Debian
package (.deb) management, including:
- .deb package upload, download, and validation
- Debian control metadata extraction
- APT repository metadata generation (Packages, Release, InRelease, Sources)
- GPG signature generation and validation
- Distribution/Component/Architecture hierarchy support
"""

from src.app.formats.apt.handler import AptFormatHandler
from src.app.formats.apt.metadata import AptMetadataManager

__all__: list[str] = [
    "AptFormatHandler",
    "AptMetadataManager",
]
