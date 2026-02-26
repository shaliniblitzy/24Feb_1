"""
Docker format handler sub-package for Nexus Repository.

Implements the Docker Registry HTTP API V2 for container image management,
supporting manifest operations, blob (layer) operations, chunked uploads,
tag listing, and catalog endpoints.

Supports Docker V2 Schema 2 and OCI Image Manifest formats.

Feature: F-101-RQ-005

**Replaces:** The Docker format bundle OSGi plugin from the original
Sonatype Nexus Repository Java source system (OSGi/Karaf 4.4.4 module
container).

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
    DockerFormatHandler: The format handler class for Docker container images.
        Extends FormatHandler base class with Docker-specific coordinate
        extraction, manifest validation, and metadata generation.
    docker_v2_bp: Flask Blueprint containing all Docker V2 route handlers.
    MANIFEST_V2_TYPE: Docker V2 Schema 2 manifest media type constant
        (``'application/vnd.docker.distribution.manifest.v2+json'``).
    OCI_MANIFEST_TYPE: OCI Image Manifest media type constant
        (``'application/vnd.oci.image.manifest.v1+json'``).
    DOCKER_REGISTRY_VERSION: Docker Registry API version string
        (``'registry/2.0'``).
"""

from src.app.formats.docker.handler import DockerFormatHandler
from src.app.formats.docker.registry_v2 import (
    docker_v2_bp,
    MANIFEST_V2_TYPE,
    OCI_MANIFEST_TYPE,
    DOCKER_REGISTRY_VERSION,
)

__all__: list[str] = [
    "DockerFormatHandler",
    "docker_v2_bp",
    "MANIFEST_V2_TYPE",
    "OCI_MANIFEST_TYPE",
    "DOCKER_REGISTRY_VERSION",
]
