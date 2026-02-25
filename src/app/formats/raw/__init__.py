"""
Raw repository format handler sub-package.

Implements the Raw format (F-101-RQ-002) for arbitrary binary file handling
with automatic MIME type detection.  The Raw format is the simplest format
handler — it does not impose any coordinate structure or metadata conventions.

Supports Hosted, Proxy, and Group repository types for the ``'raw'`` format.
"""

from src.app.formats.raw.handler import RawFormatHandler

__all__: list[str] = ["RawFormatHandler"]
