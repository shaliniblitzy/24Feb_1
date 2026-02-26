"""
Docker Registry API V2 Format Handler Sub-Package.

Implements the Docker container image repository format (F-101-RQ-005) for
the Nexus Repository Flask application.  Supports the full Docker Registry
HTTP API V2 specification including:

- Manifest operations (push, pull, delete by tag or digest)
- Blob (layer) operations with chunked upload support
- Cross-repository blob mounting
- Tag listing and repository catalog
- Docker token authentication challenge (``WWW-Authenticate`` header)
- Content-addressable storage via SHA-256 digests
- Docker-Distribution-Api-Version header on all responses

**Replaces:** The Docker format bundle OSGi plugin from the original
Sonatype Nexus Repository Java source system.

**Protocol Endpoints (15+):**

- ``GET  /v2/``                              — Version check / auth challenge
- ``GET  /v2/{name}/manifests/{reference}``  — Pull manifest
- ``PUT  /v2/{name}/manifests/{reference}``  — Push manifest
- ``DELETE /v2/{name}/manifests/{reference}`` — Delete manifest
- ``GET  /v2/{name}/blobs/{digest}``         — Pull blob
- ``HEAD /v2/{name}/blobs/{digest}``         — Check blob existence
- ``DELETE /v2/{name}/blobs/{digest}``       — Delete blob
- ``POST /v2/{name}/blobs/uploads/``         — Initiate blob upload
- ``PATCH /v2/{name}/blobs/uploads/{uuid}``  — Upload blob chunk
- ``PUT  /v2/{name}/blobs/uploads/{uuid}``   — Complete blob upload
- ``DELETE /v2/{name}/blobs/uploads/{uuid}``  — Cancel blob upload
- ``GET  /v2/{name}/tags/list``              — List tags
- ``GET  /v2/_catalog``                      — Repository catalog

Exports:
    docker_v2_bp : Flask Blueprint containing all Docker V2 route handlers.
"""

from src.app.formats.docker.registry_v2 import docker_v2_bp

__all__: list[str] = [
    "docker_v2_bp",
]
