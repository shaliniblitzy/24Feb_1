"""
npm format handler sub-package.

Implements npm registry protocol support (Feature F-101-RQ-004):
- package.json handling and version normalization
- Scoped package support (@scope/package)
- dist-tags management (latest, next, etc.)
- Tarball URL generation and resolution

Modules:
- ``handler.py``:   npm format handler (future checkpoint)
- ``metadata.py``:  npm package metadata utilities
"""
