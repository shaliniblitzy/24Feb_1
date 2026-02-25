"""
PyPI repository format handler sub-package.

Implements the PyPI repository format (F-101-RQ-007) for Python package
distribution. Supports:
- PEP 503 Simple Repository API for package discovery and install
- Python package upload (sdist .tar.gz, bdist .whl) and download
- PyPI metadata extraction from PKG-INFO or METADATA within packages
- PEP 440 version handling and pre-release detection
- PEP 427 wheel filename validation
- Normalized package names per PEP 503
- Legacy upload API (POST to /) with multipart form data

The format_name is 'pypi' matching the Repository model's format enum.
"""

from src.app.formats.pypi.handler import PypiFormatHandler
from src.app.formats.pypi.simple_api import (
    generate_package_page,
    generate_simple_index,
)

__all__: list[str] = [
    "PypiFormatHandler",
    "generate_simple_index",
    "generate_package_page",
]
