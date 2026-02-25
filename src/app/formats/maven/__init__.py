"""
Maven format handler sub-package.

Implements Maven repository protocol support (Feature F-101-RQ-001):
- GAV (GroupId:ArtifactId:Version) coordinate resolution
- maven-metadata.xml generation and merging at group, artifact, and SNAPSHOT levels
- POM parsing and dependency resolution
- MD5/SHA-1 checksum sidecar file generation

Modules:
- ``handler.py``:   Maven format handler (future checkpoint)
- ``metadata.py``:  Maven metadata.xml generation and merging utilities
"""
