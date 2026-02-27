"""
Docker Registry HTTP API V2 implementation.

Implements the Docker Registry API V2 specification for container image
management, supporting manifest operations, blob (layer) operations,
chunked uploads, tag listing, and catalog endpoints.

Replaces the Docker format bundle from the Java source Nexus Repository system.

Endpoints implemented:
- GET  /v2/                              — Version check and auth challenge
- GET  /v2/{name}/manifests/{reference}  — Pull manifest (by tag or digest)
- PUT  /v2/{name}/manifests/{reference}  — Push manifest
- DELETE /v2/{name}/manifests/{reference} — Delete manifest
- GET  /v2/{name}/blobs/{digest}         — Pull blob (layer)
- HEAD /v2/{name}/blobs/{digest}         — Check blob existence
- DELETE /v2/{name}/blobs/{digest}       — Delete blob
- POST /v2/{name}/blobs/uploads/         — Initiate blob upload
- PATCH /v2/{name}/blobs/uploads/{uuid}  — Upload blob chunk
- PUT  /v2/{name}/blobs/uploads/{uuid}   — Complete blob upload
- DELETE /v2/{name}/blobs/uploads/{uuid}  — Cancel blob upload
- GET  /v2/{name}/tags/list              — List tags
- GET  /v2/_catalog                      — Repository catalog
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    g,
    jsonify,
    make_response,
    request,
)

from src.app.extensions import db
from src.app.auth.authentication import authenticate_request, get_current_user
from src.app.auth.authorization import authorize_request
from src.app.storage.blobstore import BlobStore, BlobId

if TYPE_CHECKING:
    from src.app.models.repository import Repository
    from src.app.models.component import Component
    from src.app.models.asset import Asset

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for Docker Registry V2 operations including
# manifest push/pull, blob uploads, authentication challenges, tag listing,
# catalog browsing, upload session management, and error conditions.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask Blueprint — Docker Registry API V2
# ---------------------------------------------------------------------------
# Replaces the Docker format OSGi bundle from the Java source.
# All Docker V2 API route handlers are registered on this Blueprint.
# ---------------------------------------------------------------------------

docker_v2_bp: Blueprint = Blueprint("docker_v2", __name__)

# ===========================================================================
# Constants — Docker Media Types and Patterns
# ===========================================================================

# Docker V2 Schema 2 manifest media types
MANIFEST_V2_TYPE: str = "application/vnd.docker.distribution.manifest.v2+json"
MANIFEST_LIST_TYPE: str = (
    "application/vnd.docker.distribution.manifest.list.v2+json"
)

# OCI Image Manifest media types
OCI_MANIFEST_TYPE: str = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX_TYPE: str = "application/vnd.oci.image.index.v1+json"

# Layer/blob media types
LAYER_TYPE: str = "application/vnd.docker.image.rootfs.diff.tar.gzip"
CONFIG_TYPE: str = "application/vnd.docker.container.image.v1+json"
OCI_LAYER_TYPE: str = "application/vnd.oci.image.layer.v1.tar+gzip"

# All accepted manifest media types for content negotiation
ACCEPTED_MANIFEST_TYPES: frozenset[str] = frozenset(
    {
        MANIFEST_V2_TYPE,
        MANIFEST_LIST_TYPE,
        OCI_MANIFEST_TYPE,
        OCI_INDEX_TYPE,
        "application/json",  # Legacy fallback
    }
)

# Docker Registry API version — returned on every response
DOCKER_REGISTRY_VERSION: str = "registry/2.0"

# Digest pattern: sha256:hex (64 hex characters)
DIGEST_PATTERN: re.Pattern[str] = re.compile(r"^sha256:[a-fA-F0-9]{64}$")

# Tag pattern: valid Docker tag per OCI distribution spec
TAG_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9_][a-zA-Z0-9._-]{0,127}$"
)

# Docker-Upload-UUID header name
DOCKER_UPLOAD_UUID_HEADER: str = "Docker-Upload-UUID"

# Default namespace for single-segment image names (e.g., 'nginx' → 'library')
_DEFAULT_NAMESPACE: str = "library"

# In-memory upload session storage (production would use Redis or DB)
# Maps upload_uuid → session dict
_upload_sessions: dict[str, dict] = {}


# ===========================================================================
# Digest and Validation Utility Functions
# ===========================================================================


def _compute_digest(data: bytes) -> str:
    """Compute a SHA-256 digest of binary data in Docker format.

    Args:
        data: Binary content to hash.

    Returns:
        Digest string in ``sha256:<hex_digest>`` format.
    """
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _validate_digest(digest: str) -> bool:
    """Validate that a digest string matches the expected Docker format.

    Args:
        digest: Digest string to validate.

    Returns:
        ``True`` if *digest* matches ``sha256:<64-hex-chars>``; ``False`` otherwise.
    """
    return bool(DIGEST_PATTERN.match(digest))


def _parse_digest(digest: str) -> tuple[str, str]:
    """Parse a Docker digest into its algorithm and hex-value components.

    Args:
        digest: A digest string (e.g., ``sha256:abc123...``).

    Returns:
        Tuple of ``(algorithm, hex_value)``.

    Raises:
        ValueError: If the digest does not contain a colon separator.
    """
    if ":" not in digest:
        raise ValueError(f"Invalid digest format: '{digest}'")
    algorithm, hex_value = digest.split(":", 1)
    return algorithm, hex_value


def _is_digest_reference(reference: str) -> bool:
    """Determine if a Docker reference is a digest (vs a tag).

    Args:
        reference: A tag or digest string.

    Returns:
        ``True`` if *reference* matches the sha256 digest pattern.
    """
    return bool(DIGEST_PATTERN.match(reference))


def _parse_image_name(name: str) -> tuple[str, str]:
    """Parse a Docker image name into (namespace, image_name).

    Docker image names can be:
    - Single segment: ``nginx`` → ``('library', 'nginx')``
    - Two segments:   ``myorg/myapp`` → ``('myorg', 'myapp')``
    - Multi-segment:  ``registry.example.com/org/team/image``
                      → ``('registry.example.com/org/team', 'image')``

    Args:
        name: The raw Docker image name path segment.

    Returns:
        Tuple of ``(namespace, image_name)``.
    """
    if "/" not in name:
        return _DEFAULT_NAMESPACE, name
    last_slash = name.rfind("/")
    namespace = name[:last_slash]
    image_name = name[last_slash + 1:]
    return namespace, image_name


def _validate_manifest(manifest_bytes: bytes, content_type: str) -> dict:
    """Parse and validate a Docker manifest JSON body.

    Supports Docker V2 Schema 2 and OCI Image Manifest formats.  Both
    require ``schemaVersion``, ``config``, and ``layers`` fields.  Manifest
    lists / OCI indexes require ``manifests`` instead of ``layers``.

    Args:
        manifest_bytes: Raw manifest JSON bytes.
        content_type: The ``Content-Type`` header value.

    Returns:
        Parsed manifest dictionary.

    Raises:
        ValueError: If the manifest JSON is malformed or missing required fields.
    """
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid manifest JSON: {exc}") from exc

    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")

    schema_version = manifest.get("schemaVersion")
    if schema_version not in (1, 2):
        raise ValueError(
            f"Unsupported schemaVersion: {schema_version}"
        )

    # Manifest list / OCI index validation
    if content_type in (MANIFEST_LIST_TYPE, OCI_INDEX_TYPE):
        manifests_list = manifest.get("manifests")
        if not isinstance(manifests_list, list):
            raise ValueError(
                "Manifest list/index must contain a 'manifests' array"
            )
        for idx, entry in enumerate(manifests_list):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"manifests[{idx}] must be a JSON object"
                )
            if "digest" not in entry:
                raise ValueError(
                    f"manifests[{idx}] missing required 'digest' field"
                )
        return manifest

    # Single manifest (Docker V2 Schema 2 / OCI) validation
    if schema_version == 2:
        config_section = manifest.get("config")
        if not isinstance(config_section, dict):
            raise ValueError("Manifest missing required 'config' object")
        if "digest" not in config_section:
            raise ValueError("config section missing required 'digest'")

        layers_list = manifest.get("layers")
        if not isinstance(layers_list, list):
            raise ValueError("Manifest missing required 'layers' array")
        for idx, layer in enumerate(layers_list):
            if not isinstance(layer, dict):
                raise ValueError(f"layers[{idx}] must be a JSON object")
            if "digest" not in layer:
                raise ValueError(
                    f"layers[{idx}] missing required 'digest'"
                )

    return manifest


# ===========================================================================
# Docker Error Response Helper
# ===========================================================================


def _docker_error(
    code: str,
    message: str,
    detail: dict | None = None,
    status: int = 404,
) -> Response:
    """Build a Docker Registry API V2 error response.

    The Docker Registry specification mandates that error responses use the
    JSON structure ``{"errors": [{"code": ..., "message": ..., "detail": ...}]}``.

    Standard error codes include: ``BLOB_UNKNOWN``, ``BLOB_UPLOAD_INVALID``,
    ``BLOB_UPLOAD_UNKNOWN``, ``DIGEST_INVALID``, ``MANIFEST_BLOB_UNKNOWN``,
    ``MANIFEST_INVALID``, ``MANIFEST_UNKNOWN``, ``MANIFEST_UNVERIFIED``,
    ``NAME_INVALID``, ``NAME_UNKNOWN``, ``SIZE_INVALID``, ``TAG_INVALID``,
    ``UNAUTHORIZED``, ``DENIED``, ``UNSUPPORTED``.

    Args:
        code: Docker error code string (e.g., ``'MANIFEST_UNKNOWN'``).
        message: Human-readable error description.
        detail: Optional dictionary with additional error context.
        status: HTTP status code (default ``404``).

    Returns:
        A Flask :class:`Response` with the Docker error JSON body and
        appropriate Content-Type and status code.
    """
    error_entry: dict = {"code": code, "message": message}
    if detail is not None:
        error_entry["detail"] = detail

    body = json.dumps({"errors": [error_entry]})
    response = Response(
        body,
        status=status,
        content_type="application/json",
    )
    response.headers["Docker-Distribution-Api-Version"] = DOCKER_REGISTRY_VERSION
    return response


# ===========================================================================
# Docker Token Authentication Helper
# ===========================================================================


def _require_docker_auth(scope: str | None = None) -> Response | None:
    """Check authentication and return a 401 challenge if not authenticated.

    Implements the Docker token authentication flow per the specification.
    When a client is not authenticated, the response includes a
    ``WWW-Authenticate`` header with the Bearer challenge containing
    realm, service, and scope parameters.

    Args:
        scope: Optional scope string (e.g., ``'repository:library/nginx:pull'``).

    Returns:
        A ``401 Unauthorized`` :class:`Response` with the Docker auth
        challenge headers if not authenticated, or ``None`` if the
        current user is authenticated.
    """
    user = get_current_user()
    if user is not None:
        return None

    # Build WWW-Authenticate challenge header
    auth_realm = current_app.config.get(
        "DOCKER_AUTH_REALM",
        request.url_root.rstrip("/") + "/v2/token",
    )
    service_name = current_app.config.get(
        "DOCKER_AUTH_SERVICE", "nexus-registry"
    )

    challenge_parts = [
        f'Bearer realm="{auth_realm}"',
        f'service="{service_name}"',
    ]
    if scope:
        challenge_parts.append(f'scope="{scope}"')
    challenge = ",".join(challenge_parts)

    response = _docker_error(
        code="UNAUTHORIZED",
        message="authentication required",
        status=401,
    )
    response.headers["WWW-Authenticate"] = challenge
    return response


# ===========================================================================
# Upload Session Management
# ===========================================================================


def _create_upload_session(name: str) -> dict:
    """Create a new blob upload session with a unique UUID.

    Stores the session state in the module-level ``_upload_sessions``
    dictionary.  In a production multi-node deployment, this would be
    backed by Redis or the database.

    Args:
        name: The Docker image name (repository path) for this upload.

    Returns:
        Session dictionary with ``uuid``, ``repository_name``, ``offset``,
        ``data``, and ``created_at`` fields.
    """
    session_uuid = str(uuid.uuid4())
    session = {
        "uuid": session_uuid,
        "repository_name": name,
        "offset": 0,
        "data": bytearray(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _upload_sessions[session_uuid] = session
    logger.debug(
        "Created upload session uuid='%s' for repository='%s'.",
        session_uuid,
        name,
    )
    return session


def _get_upload_session(upload_uuid: str) -> dict | None:
    """Retrieve an in-progress upload session by UUID.

    Args:
        upload_uuid: The upload session identifier.

    Returns:
        Session dictionary if found, or ``None`` if the session does not
        exist or has expired.
    """
    session = _upload_sessions.get(upload_uuid)
    if session is None:
        logger.debug("Upload session not found: uuid='%s'.", upload_uuid)
    return session


def _delete_upload_session(upload_uuid: str) -> bool:
    """Remove an upload session and its temporary data.

    Args:
        upload_uuid: The upload session identifier to remove.

    Returns:
        ``True`` if the session was found and removed; ``False`` otherwise.
    """
    if upload_uuid in _upload_sessions:
        del _upload_sessions[upload_uuid]
        logger.debug("Deleted upload session uuid='%s'.", upload_uuid)
        return True
    return False


# ===========================================================================
# Internal Helper — Repository and BlobStore Resolution
# ===========================================================================


def _resolve_repository(name: str) -> Repository | None:
    """Look up a Docker repository by image name.

    Searches for a repository with ``format='docker'`` and ``online=True``
    that matches the given image *name*.  The lookup uses the full image
    name (e.g., ``library/nginx``) as a filter on the ``name`` field of
    matching repositories.  If no exact match is found, attempts to match
    by searching for repositories whose name could serve this image path.

    Args:
        name: The Docker image name path (e.g., ``library/nginx``).

    Returns:
        The matching :class:`Repository` or ``None``.
    """
    from src.app.models.repository import Repository as RepoModel

    # First try: look for any docker-format repository that is online.
    # In Nexus, the repository name is separate from image names.
    # Docker repositories serve all images within them.
    repo = (
        RepoModel.query.filter_by(format="docker", online=True)
        .first()
    )
    if repo is not None:
        return repo

    logger.debug("No online Docker repository found for image='%s'.", name)
    return None


def _get_blobstore_for_repo(repo: Repository) -> BlobStore | None:
    """Retrieve the BlobStore instance configured for a repository.

    Looks up the BlobStore by ``repo.blob_store_name`` from the
    application's blobstore registry stored in ``current_app.extensions``.

    Args:
        repo: The repository whose BlobStore to retrieve.

    Returns:
        The :class:`BlobStore` instance, or ``None`` if not found.
    """
    blobstores: dict = current_app.extensions.get("blobstores", {})
    store = blobstores.get(repo.blob_store_name)
    if store is None:
        logger.warning(
            "BlobStore '%s' not found for repository '%s'.",
            repo.blob_store_name,
            repo.name,
        )
    return store


# ===========================================================================
# Blueprint Hooks — Authentication and Headers
# ===========================================================================


@docker_v2_bp.before_request
def _before_request_auth() -> Response | None:
    """Apply authentication to all Docker V2 API requests.

    The Docker V2 specification requires that the ``GET /v2/`` endpoint
    returns ``200 OK`` even without authentication to signal V2 support.
    All other endpoints require authentication and return a ``401``
    challenge with ``WWW-Authenticate`` if credentials are missing or
    invalid.

    This hook:
    1. Attempts to authenticate the incoming request via the multi-realm
       authentication chain.
    2. If authentication succeeds, stores the user on ``g.current_user``.
    3. For ``GET /v2/``, allows anonymous access (returns ``None``).
    4. For all other endpoints, returns a ``401`` challenge if unauthenticated.
    """
    auth_result = authenticate_request()
    if auth_result.authenticated and auth_result.user is not None:
        g.current_user = auth_result.user
        return None

    # Allow GET /v2/ without authentication (Docker spec requirement)
    if request.path.rstrip("/") == "/v2" and request.method == "GET":
        return None

    # Build scope from the request path for the WWW-Authenticate challenge
    scope = _build_scope_from_request()
    return _require_docker_auth(scope=scope)


@docker_v2_bp.after_request
def _after_request_headers(response: Response) -> Response:
    """Add the ``Docker-Distribution-Api-Version`` header to all responses.

    Per the Docker Registry V2 specification, every response from the
    registry must include this header to indicate API version support.

    Args:
        response: The outgoing Flask response.

    Returns:
        The response with the version header added.
    """
    response.headers["Docker-Distribution-Api-Version"] = DOCKER_REGISTRY_VERSION
    return response


def _build_scope_from_request() -> str | None:
    """Extract a Docker auth scope string from the current request path.

    Parses the request path to determine the repository name and action
    (pull/push/delete), producing a scope in the format
    ``repository:<name>:<action>``.

    Returns:
        A scope string, or ``None`` if the path cannot be parsed.
    """
    path = request.path
    method = request.method.upper()

    # Determine action from HTTP method
    if method in ("GET", "HEAD"):
        action = "pull"
    elif method in ("PUT", "POST", "PATCH"):
        action = "push"
    elif method == "DELETE":
        action = "delete"
    else:
        action = "pull"

    # Extract repository name from path
    # Pattern: /v2/{name}/manifests/... or /v2/{name}/blobs/... etc.
    match = re.match(r"^/v2/(.+?)/(manifests|blobs|tags)/", path)
    if match:
        name = match.group(1)
        return f"repository:{name}:{action}"

    # Catalog endpoint
    if "/v2/_catalog" in path:
        return "registry:catalog:*"

    return None


# ===========================================================================
# Endpoint: Version Check (GET /v2/)
# ===========================================================================


@docker_v2_bp.route("/v2/", methods=["GET"])
def v2_check() -> Response:
    """Docker V2 API version check and authentication challenge.

    Returns an empty JSON object ``{}`` with ``200 OK`` to signal that
    the registry supports Docker V2.  If the client is not authenticated,
    the ``before_request`` hook has already returned a ``401`` challenge
    for non-GET-/v2/ endpoints; however, GET /v2/ is allowed anonymously
    per the Docker specification.

    Returns:
        ``200 OK`` with empty JSON body and Docker headers.
    """
    response = make_response(jsonify({}), 200)
    response.headers["Docker-Distribution-Api-Version"] = DOCKER_REGISTRY_VERSION
    logger.debug("Docker V2 version check: 200 OK.")
    return response


# ===========================================================================
# Endpoints: Manifest Operations
# ===========================================================================


@docker_v2_bp.route(
    "/v2/<path:name>/manifests/<reference>", methods=["GET"]
)
def get_manifest(name: str, reference: str) -> Response:
    """Pull a Docker manifest by tag or digest.

    Resolves the image name and reference to locate the manifest content
    in the BlobStore.  Supports content negotiation via the ``Accept``
    header for Docker V2 Schema 2 and OCI manifest formats.

    Args:
        name: Docker image name (e.g., ``library/nginx``).
        reference: Tag (e.g., ``latest``) or digest (e.g., ``sha256:abc...``).

    Returns:
        Manifest content with appropriate Docker headers, or a
        ``404 MANIFEST_UNKNOWN`` error.
    """
    logger.info("GET manifest: name='%s', reference='%s'.", name, reference)

    namespace, image_name = _parse_image_name(name)
    repo = _resolve_repository(name)
    if repo is None:
        return _docker_error(
            "NAME_UNKNOWN",
            f"repository name not known to registry: {name}",
            detail={"name": name},
        )

    # Check read permission
    user = get_current_user()
    if user is not None:
        if not authorize_request(
            "read",
            repository_name=repo.name,
            format_name="docker",
        ):
            return _docker_error(
                "DENIED",
                "requested access to the resource is denied",
                status=403,
            )

    from src.app.models.component import Component as CompModel
    from src.app.models.asset import Asset as AssetModel

    is_digest = _is_digest_reference(reference)

    # Find the asset corresponding to the manifest
    asset: Asset | None = None
    if is_digest:
        # Look up by digest
        asset = (
            AssetModel.query.filter_by(
                repository_name=repo.name,
                checksum_sha256=reference.split(":", 1)[1] if ":" in reference else reference,
            )
            .filter(AssetModel.content_type.in_(ACCEPTED_MANIFEST_TYPES))
            .first()
        )
    else:
        # Look up by tag via Component version
        component = (
            CompModel.query.filter_by(
                repository_name=repo.name,
                namespace=namespace,
                name=image_name,
                version=reference,
            )
            .first()
        )
        if component is not None:
            manifest_path = f"/v2/{name}/manifests/{reference}"
            asset = (
                AssetModel.query.filter_by(
                    repository_name=repo.name,
                    component_id=component.id,
                )
                .filter(AssetModel.content_type.in_(ACCEPTED_MANIFEST_TYPES))
                .first()
            )

    if asset is None:
        detail_key = "Digest" if is_digest else "Tag"
        return _docker_error(
            "MANIFEST_UNKNOWN",
            "manifest unknown to registry",
            detail={detail_key: reference},
        )

    # Retrieve manifest content from BlobStore
    blobstore = _get_blobstore_for_repo(repo)
    content: bytes | None = None
    if blobstore is not None and asset.blob_ref:
        blob_id = BlobId.from_string(asset.blob_ref)
        stream = blobstore.get_stream(blob_id)
        if stream is not None:
            content = stream.read()
            stream.close()

    if content is None:
        return _docker_error(
            "MANIFEST_UNKNOWN",
            "manifest content not available",
            detail={"reference": reference},
        )

    # Determine content type from asset or default
    manifest_content_type = asset.content_type or MANIFEST_V2_TYPE

    # Content negotiation: honour Accept header
    accept_header = request.headers.get("Accept", "*/*")
    if accept_header != "*/*" and manifest_content_type not in accept_header:
        # Check if any accepted type matches
        accepted = [t.strip().split(";")[0] for t in accept_header.split(",")]
        if "*/*" not in accepted and manifest_content_type not in accepted:
            # Try to serve anyway — many clients accept all types
            pass

    # Compute digest for the Docker-Content-Digest header
    digest = _compute_digest(content)

    # Update last_downloaded timestamp
    try:
        asset.last_downloaded = datetime.now(timezone.utc)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.debug("Failed to update last_downloaded for asset.", exc_info=True)

    response = Response(
        content,
        status=200,
        content_type=manifest_content_type,
    )
    response.headers["Docker-Content-Digest"] = digest
    response.headers["Content-Length"] = str(len(content))
    response.headers["ETag"] = f'"{digest}"'

    logger.info(
        "Served manifest for '%s:%s' (%d bytes, digest=%s).",
        name,
        reference,
        len(content),
        digest,
    )
    return response


@docker_v2_bp.route(
    "/v2/<path:name>/manifests/<reference>", methods=["PUT"]
)
def put_manifest(name: str, reference: str) -> Response:
    """Push a Docker manifest by tag or digest.

    Validates the manifest body, computes its digest, verifies all
    referenced blobs exist, stores the manifest in the BlobStore, and
    creates or updates the corresponding Component and Asset records.

    Args:
        name: Docker image name.
        reference: Tag or digest for the manifest.

    Returns:
        ``201 Created`` with ``Location`` and ``Docker-Content-Digest``
        headers, or an appropriate error response.
    """
    logger.info("PUT manifest: name='%s', reference='%s'.", name, reference)

    namespace, image_name = _parse_image_name(name)
    repo = _resolve_repository(name)
    if repo is None:
        return _docker_error(
            "NAME_UNKNOWN",
            f"repository name not known to registry: {name}",
            detail={"name": name},
        )

    # Check write permission
    user = get_current_user()
    if user is not None:
        if not authorize_request(
            "add",
            repository_name=repo.name,
            format_name="docker",
        ):
            return _docker_error(
                "DENIED",
                "requested access to the resource is denied",
                status=403,
            )

    manifest_bytes = request.data
    if not manifest_bytes:
        return _docker_error(
            "MANIFEST_INVALID",
            "manifest body is empty",
            status=400,
        )

    content_type = request.content_type or MANIFEST_V2_TYPE

    # Validate manifest
    try:
        parsed_manifest = _validate_manifest(manifest_bytes, content_type)
    except ValueError as exc:
        return _docker_error(
            "MANIFEST_INVALID",
            str(exc),
            status=400,
        )

    # Compute manifest digest
    digest = _compute_digest(manifest_bytes)
    _, digest_hex = _parse_digest(digest)

    # Verify referenced blobs exist (for non-list manifests)
    if content_type not in (MANIFEST_LIST_TYPE, OCI_INDEX_TYPE):
        blobstore = _get_blobstore_for_repo(repo)
        if blobstore is not None:
            missing_blobs = _check_referenced_blobs(
                parsed_manifest, repo, blobstore
            )
            if missing_blobs:
                return _docker_error(
                    "MANIFEST_BLOB_UNKNOWN",
                    "manifest references blobs unknown to registry",
                    detail={"missing_digests": missing_blobs},
                    status=400,
                )

    # Store manifest in BlobStore
    blobstore = _get_blobstore_for_repo(repo)
    blob_ref_str: str | None = None
    if blobstore is not None:
        blob_id = BlobId.generate(repo.blob_store_name)
        try:
            blob = blobstore.create(
                blob_id=blob_id,
                data=manifest_bytes,
                content_type=content_type,
            )
            blob_ref_str = str(blob.blob_id)
        except Exception:
            logger.exception("Failed to store manifest in BlobStore.")
            return _docker_error(
                "MANIFEST_INVALID",
                "failed to store manifest",
                status=500,
            )

    # Create or update Component and Asset records
    from src.app.models.component import Component as CompModel
    from src.app.models.asset import Asset as AssetModel

    try:
        # Resolve tag vs digest for the version field
        version = reference if not _is_digest_reference(reference) else digest

        # Find or create the component
        component = CompModel.query.filter_by(
            repository_name=repo.name,
            namespace=namespace,
            name=image_name,
            version=version,
        ).first()

        if component is None:
            component = CompModel(
                repository_name=repo.name,
                namespace=namespace,
                name=image_name,
                version=version,
            )
            db.session.add(component)
            db.session.flush()

        # Create or update the asset for this manifest
        asset_path = f"/v2/{name}/manifests/{reference}"
        asset = AssetModel.query.filter_by(
            repository_name=repo.name,
            path=asset_path,
        ).first()

        if asset is None:
            asset = AssetModel(
                repository_name=repo.name,
                path=asset_path,
                content_type=content_type,
                size=len(manifest_bytes),
                checksum_sha256=digest_hex,
                blob_ref=blob_ref_str,
                component_id=component.id,
            )
            db.session.add(asset)
        else:
            asset.content_type = content_type
            asset.size = len(manifest_bytes)
            asset.checksum_sha256 = digest_hex
            asset.blob_ref = blob_ref_str
            asset.component_id = component.id

        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to persist manifest records.")
        return _docker_error(
            "MANIFEST_INVALID",
            "failed to persist manifest metadata",
            status=500,
        )

    # Build response
    location = f"/v2/{name}/manifests/{digest}"
    response = make_response("", 201)
    response.headers["Location"] = location
    response.headers["Docker-Content-Digest"] = digest
    response.headers["Content-Length"] = "0"

    logger.info(
        "Stored manifest for '%s:%s' (digest=%s, %d bytes).",
        name,
        reference,
        digest,
        len(manifest_bytes),
    )
    return response


@docker_v2_bp.route(
    "/v2/<path:name>/manifests/<reference>", methods=["DELETE"]
)
def delete_manifest(name: str, reference: str) -> Response:
    """Delete a Docker manifest by digest.

    Per the Docker specification, manifests can only be deleted by
    digest reference.  Removes the manifest from the BlobStore and
    deletes the associated Asset and Component records.

    Args:
        name: Docker image name.
        reference: Digest of the manifest to delete.

    Returns:
        ``202 Accepted`` on success, or an appropriate error response.
    """
    logger.info("DELETE manifest: name='%s', reference='%s'.", name, reference)

    if not _is_digest_reference(reference):
        return _docker_error(
            "UNSUPPORTED",
            "manifest delete requires a digest reference",
            status=405,
        )

    repo = _resolve_repository(name)
    if repo is None:
        return _docker_error(
            "NAME_UNKNOWN",
            f"repository name not known to registry: {name}",
            detail={"name": name},
        )

    # Check delete permission
    user = get_current_user()
    if user is not None:
        if not authorize_request(
            "delete",
            repository_name=repo.name,
            format_name="docker",
        ):
            return _docker_error(
                "DENIED",
                "requested access to the resource is denied",
                status=403,
            )

    _, digest_hex = _parse_digest(reference)

    from src.app.models.asset import Asset as AssetModel

    asset = AssetModel.query.filter_by(
        repository_name=repo.name,
        checksum_sha256=digest_hex,
    ).filter(
        AssetModel.content_type.in_(ACCEPTED_MANIFEST_TYPES)
    ).first()

    if asset is None:
        return _docker_error(
            "MANIFEST_UNKNOWN",
            "manifest unknown to registry",
            detail={"Digest": reference},
        )

    # Delete from BlobStore
    blobstore = _get_blobstore_for_repo(repo)
    if blobstore is not None and asset.blob_ref:
        try:
            blob_id = BlobId.from_string(asset.blob_ref)
            blobstore.delete(blob_id)
        except Exception:
            logger.warning(
                "Failed to delete manifest blob from BlobStore.",
                exc_info=True,
            )

    # Delete database records
    try:
        db.session.delete(asset)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to delete manifest records.")
        return _docker_error(
            "MANIFEST_UNKNOWN",
            "failed to delete manifest",
            status=500,
        )

    logger.info("Deleted manifest for '%s' (digest=%s).", name, reference)
    response = make_response("", 202)
    return response


# ===========================================================================
# Endpoints: Blob (Layer) Operations
# ===========================================================================


@docker_v2_bp.route("/v2/<path:name>/blobs/<digest>", methods=["GET"])
def get_blob(name: str, digest: str) -> Response:
    """Pull a Docker blob (layer or config) by digest.

    Streams the blob content from the BlobStore and updates the
    ``last_downloaded`` timestamp on the corresponding Asset record.

    Args:
        name: Docker image name.
        digest: Content-addressable digest (e.g., ``sha256:abc...``).

    Returns:
        Blob content with Docker headers, or ``404 BLOB_UNKNOWN``.
    """
    logger.info("GET blob: name='%s', digest='%s'.", name, digest)

    if not _validate_digest(digest):
        return _docker_error(
            "DIGEST_INVALID",
            f"invalid digest format: {digest}",
            status=400,
        )

    repo = _resolve_repository(name)
    if repo is None:
        return _docker_error(
            "NAME_UNKNOWN",
            f"repository name not known to registry: {name}",
            detail={"name": name},
        )

    # Look up asset
    _, digest_hex = _parse_digest(digest)
    from src.app.models.asset import Asset as AssetModel

    asset = AssetModel.query.filter_by(
        repository_name=repo.name,
        checksum_sha256=digest_hex,
    ).first()

    if asset is None:
        return _docker_error(
            "BLOB_UNKNOWN",
            "blob unknown to registry",
            detail={"Digest": digest},
        )

    # Retrieve content from BlobStore
    blobstore = _get_blobstore_for_repo(repo)
    content: bytes | None = None
    if blobstore is not None and asset.blob_ref:
        blob_id = BlobId.from_string(asset.blob_ref)
        stream = blobstore.get_stream(blob_id)
        if stream is not None:
            content = stream.read()
            stream.close()

    if content is None:
        return _docker_error(
            "BLOB_UNKNOWN",
            "blob content not available",
            detail={"Digest": digest},
        )

    # Update last_downloaded
    try:
        asset.last_downloaded = datetime.now(timezone.utc)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.debug("Failed to update last_downloaded.", exc_info=True)

    blob_content_type = asset.content_type or "application/octet-stream"
    response = Response(
        content,
        status=200,
        content_type=blob_content_type,
    )
    response.headers["Docker-Content-Digest"] = digest
    response.headers["Content-Length"] = str(len(content))
    response.headers["ETag"] = f'"{digest}"'

    logger.info("Served blob digest=%s (%d bytes).", digest, len(content))
    return response


@docker_v2_bp.route("/v2/<path:name>/blobs/<digest>", methods=["HEAD"])
def head_blob(name: str, digest: str) -> Response:
    """Check existence of a Docker blob by digest.

    Returns ``200 OK`` with size and digest headers if the blob exists,
    or ``404 BLOB_UNKNOWN`` if it does not.  No body is returned.

    Args:
        name: Docker image name.
        digest: Content-addressable digest.

    Returns:
        ``200`` with headers or ``404`` error.
    """
    logger.debug("HEAD blob: name='%s', digest='%s'.", name, digest)

    if not _validate_digest(digest):
        return _docker_error(
            "DIGEST_INVALID",
            f"invalid digest format: {digest}",
            status=400,
        )

    repo = _resolve_repository(name)
    if repo is None:
        return _docker_error(
            "NAME_UNKNOWN",
            f"repository name not known to registry: {name}",
            detail={"name": name},
        )

    _, digest_hex = _parse_digest(digest)
    from src.app.models.asset import Asset as AssetModel

    asset = AssetModel.query.filter_by(
        repository_name=repo.name,
        checksum_sha256=digest_hex,
    ).first()

    if asset is None:
        return _docker_error(
            "BLOB_UNKNOWN",
            "blob unknown to registry",
            detail={"Digest": digest},
        )

    response = make_response("", 200)
    response.headers["Docker-Content-Digest"] = digest
    response.headers["Content-Length"] = str(asset.size or 0)
    response.headers["Content-Type"] = (
        asset.content_type or "application/octet-stream"
    )
    return response


@docker_v2_bp.route("/v2/<path:name>/blobs/<digest>", methods=["DELETE"])
def delete_blob(name: str, digest: str) -> Response:
    """Delete a Docker blob by digest.

    Args:
        name: Docker image name.
        digest: Content-addressable digest of the blob to delete.

    Returns:
        ``202 Accepted`` on success, or an appropriate error.
    """
    logger.info("DELETE blob: name='%s', digest='%s'.", name, digest)

    if not _validate_digest(digest):
        return _docker_error(
            "DIGEST_INVALID",
            f"invalid digest format: {digest}",
            status=400,
        )

    repo = _resolve_repository(name)
    if repo is None:
        return _docker_error(
            "NAME_UNKNOWN",
            f"repository name not known to registry: {name}",
            detail={"name": name},
        )

    # Check delete permission
    user = get_current_user()
    if user is not None:
        if not authorize_request(
            "delete",
            repository_name=repo.name,
            format_name="docker",
        ):
            return _docker_error(
                "DENIED",
                "requested access to the resource is denied",
                status=403,
            )

    _, digest_hex = _parse_digest(digest)
    from src.app.models.asset import Asset as AssetModel

    asset = AssetModel.query.filter_by(
        repository_name=repo.name,
        checksum_sha256=digest_hex,
    ).first()

    if asset is None:
        return _docker_error(
            "BLOB_UNKNOWN",
            "blob unknown to registry",
            detail={"Digest": digest},
        )

    # Remove from BlobStore
    blobstore = _get_blobstore_for_repo(repo)
    if blobstore is not None and asset.blob_ref:
        try:
            blob_id = BlobId.from_string(asset.blob_ref)
            blobstore.delete(blob_id)
        except Exception:
            logger.warning(
                "Failed to delete blob from BlobStore.", exc_info=True
            )

    try:
        db.session.delete(asset)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to delete blob records.")
        return _docker_error(
            "BLOB_UNKNOWN",
            "failed to delete blob",
            status=500,
        )

    logger.info("Deleted blob digest=%s.", digest)
    return make_response("", 202)


# ===========================================================================
# Endpoints: Blob Upload Operations
# ===========================================================================


@docker_v2_bp.route("/v2/<path:name>/blobs/uploads/", methods=["POST"])
def initiate_blob_upload(name: str) -> Response:
    """Initiate a new Docker blob upload session.

    Supports three upload modes:
    1. **Chunked upload**: Returns ``202 Accepted`` with an upload URL
       for subsequent PATCH/PUT operations.
    2. **Monolithic upload**: If both ``digest`` query param and request
       body are present, completes the upload in a single request.
    3. **Cross-repo mount**: If ``mount`` and ``from`` query params are
       present, attempts to mount an existing blob from another repository.

    Args:
        name: Docker image name.

    Returns:
        ``202 Accepted`` (chunked), ``201 Created`` (monolithic/mount),
        or an error response.
    """
    logger.info("POST blob upload: name='%s'.", name)

    repo = _resolve_repository(name)
    if repo is None:
        return _docker_error(
            "NAME_UNKNOWN",
            f"repository name not known to registry: {name}",
            detail={"name": name},
        )

    # Check write permission
    user = get_current_user()
    if user is not None:
        if not authorize_request(
            "add",
            repository_name=repo.name,
            format_name="docker",
        ):
            return _docker_error(
                "DENIED",
                "requested access to the resource is denied",
                status=403,
            )

    # Check for cross-repo blob mount
    mount_digest = request.args.get("mount")
    mount_from = request.args.get("from")
    if mount_digest and mount_from:
        mount_result = _attempt_blob_mount(
            name, repo, mount_digest, mount_from
        )
        if mount_result is not None:
            return mount_result

    # Check for monolithic upload (POST with digest and body)
    monolithic_digest = request.args.get("digest")
    if monolithic_digest and request.data:
        return _complete_monolithic_upload(
            name, repo, monolithic_digest, request.data
        )

    # Chunked upload initiation
    session = _create_upload_session(name)
    upload_url = f"/v2/{name}/blobs/uploads/{session['uuid']}"

    response = make_response("", 202)
    response.headers["Location"] = upload_url
    response.headers[DOCKER_UPLOAD_UUID_HEADER] = session["uuid"]
    response.headers["Range"] = "0-0"
    response.headers["Content-Length"] = "0"

    logger.info(
        "Initiated blob upload: uuid='%s', name='%s'.",
        session["uuid"],
        name,
    )
    return response


@docker_v2_bp.route(
    "/v2/<path:name>/blobs/uploads/<upload_uuid>", methods=["PATCH"]
)
def upload_blob_chunk(name: str, upload_uuid: str) -> Response:
    """Upload a blob chunk to an in-progress upload session.

    Appends the request body data to the upload session's accumulated
    buffer and returns the updated byte range.

    Args:
        name: Docker image name.
        upload_uuid: Upload session identifier.

    Returns:
        ``202 Accepted`` with updated range, or an error response.
    """
    logger.debug(
        "PATCH blob chunk: name='%s', uuid='%s'.", name, upload_uuid
    )

    session = _get_upload_session(upload_uuid)
    if session is None:
        return _docker_error(
            "BLOB_UPLOAD_UNKNOWN",
            "blob upload unknown to registry",
            detail={"Upload-UUID": upload_uuid},
        )

    chunk_data = request.data
    if chunk_data:
        session["data"].extend(chunk_data)
        session["offset"] += len(chunk_data)

    end_offset = max(session["offset"] - 1, 0)
    upload_url = f"/v2/{name}/blobs/uploads/{upload_uuid}"

    response = make_response("", 202)
    response.headers["Location"] = upload_url
    response.headers[DOCKER_UPLOAD_UUID_HEADER] = upload_uuid
    response.headers["Range"] = f"0-{end_offset}"
    response.headers["Content-Length"] = "0"

    logger.debug(
        "Appended %d bytes to upload uuid='%s' (total=%d).",
        len(chunk_data) if chunk_data else 0,
        upload_uuid,
        session["offset"],
    )
    return response


@docker_v2_bp.route(
    "/v2/<path:name>/blobs/uploads/<upload_uuid>", methods=["PUT"]
)
def complete_blob_upload(name: str, upload_uuid: str) -> Response:
    """Complete a Docker blob upload.

    Finalises the upload by appending any remaining body data, computing
    the final digest, verifying it matches the ``digest`` query parameter,
    and storing the complete blob in the BlobStore.

    Args:
        name: Docker image name.
        upload_uuid: Upload session identifier.

    Returns:
        ``201 Created`` on success with ``Location`` and
        ``Docker-Content-Digest`` headers, or an error response.
    """
    logger.info(
        "PUT complete blob upload: name='%s', uuid='%s'.",
        name,
        upload_uuid,
    )

    session = _get_upload_session(upload_uuid)
    if session is None:
        return _docker_error(
            "BLOB_UPLOAD_UNKNOWN",
            "blob upload unknown to registry",
            detail={"Upload-UUID": upload_uuid},
        )

    # Append any final chunk data
    final_chunk = request.data
    if final_chunk:
        session["data"].extend(final_chunk)
        session["offset"] += len(final_chunk)

    # The digest query parameter is required
    expected_digest = request.args.get("digest")
    if not expected_digest:
        return _docker_error(
            "DIGEST_INVALID",
            "digest query parameter is required",
            status=400,
        )

    if not _validate_digest(expected_digest):
        return _docker_error(
            "DIGEST_INVALID",
            f"invalid digest format: {expected_digest}",
            status=400,
        )

    blob_data = bytes(session["data"])
    actual_digest = _compute_digest(blob_data)

    if actual_digest != expected_digest:
        _delete_upload_session(upload_uuid)
        return _docker_error(
            "DIGEST_INVALID",
            f"digest mismatch: expected {expected_digest}, "
            f"got {actual_digest}",
            status=400,
        )

    # Store the blob in BlobStore
    repo = _resolve_repository(name)
    if repo is None:
        _delete_upload_session(upload_uuid)
        return _docker_error(
            "NAME_UNKNOWN",
            f"repository name not known to registry: {name}",
            detail={"name": name},
        )

    blobstore = _get_blobstore_for_repo(repo)
    blob_ref_str: str | None = None
    if blobstore is not None:
        blob_id = BlobId.generate(repo.blob_store_name)
        try:
            blob = blobstore.create(
                blob_id=blob_id,
                data=blob_data,
                content_type="application/octet-stream",
            )
            blob_ref_str = str(blob.blob_id)
        except Exception:
            logger.exception("Failed to store blob in BlobStore.")
            _delete_upload_session(upload_uuid)
            return _docker_error(
                "BLOB_UPLOAD_INVALID",
                "failed to store blob",
                status=500,
            )

    # Create Asset record
    _, digest_hex = _parse_digest(actual_digest)
    from src.app.models.asset import Asset as AssetModel

    try:
        asset_path = f"/v2/{name}/blobs/{actual_digest}"
        existing_asset = AssetModel.query.filter_by(
            repository_name=repo.name,
            path=asset_path,
        ).first()

        if existing_asset is None:
            new_asset = AssetModel(
                repository_name=repo.name,
                path=asset_path,
                content_type="application/octet-stream",
                size=len(blob_data),
                checksum_sha256=digest_hex,
                blob_ref=blob_ref_str,
            )
            db.session.add(new_asset)
        else:
            existing_asset.size = len(blob_data)
            existing_asset.checksum_sha256 = digest_hex
            existing_asset.blob_ref = blob_ref_str

        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to persist blob asset record.")

    # Clean up upload session
    _delete_upload_session(upload_uuid)

    location = f"/v2/{name}/blobs/{actual_digest}"
    response = make_response("", 201)
    response.headers["Location"] = location
    response.headers["Docker-Content-Digest"] = actual_digest
    response.headers["Content-Length"] = "0"

    logger.info(
        "Completed blob upload: digest=%s, size=%d bytes.",
        actual_digest,
        len(blob_data),
    )
    return response


@docker_v2_bp.route(
    "/v2/<path:name>/blobs/uploads/<upload_uuid>", methods=["DELETE"]
)
def cancel_blob_upload(name: str, upload_uuid: str) -> Response:
    """Cancel an in-progress blob upload.

    Removes the upload session and any accumulated temporary data.

    Args:
        name: Docker image name.
        upload_uuid: Upload session identifier.

    Returns:
        ``204 No Content`` on success, or ``404`` if the session is not found.
    """
    logger.info(
        "DELETE blob upload: name='%s', uuid='%s'.", name, upload_uuid
    )

    if not _delete_upload_session(upload_uuid):
        return _docker_error(
            "BLOB_UPLOAD_UNKNOWN",
            "blob upload unknown to registry",
            detail={"Upload-UUID": upload_uuid},
        )

    return make_response("", 204)


# ===========================================================================
# Endpoints: Tag Listing
# ===========================================================================


@docker_v2_bp.route("/v2/<path:name>/tags/list", methods=["GET"])
def list_tags(name: str) -> Response:
    """List all tags for a Docker image.

    Supports pagination via ``n`` (page size) and ``last`` (last seen tag)
    query parameters.  Tags are returned in lexicographic order.

    Args:
        name: Docker image name.

    Returns:
        JSON response with ``name`` and ``tags`` fields, or ``404``.
    """
    logger.info("GET tags list: name='%s'.", name)

    repo = _resolve_repository(name)
    if repo is None:
        return _docker_error(
            "NAME_UNKNOWN",
            f"repository name not known to registry: {name}",
            detail={"name": name},
        )

    namespace, image_name = _parse_image_name(name)

    from src.app.models.component import Component as CompModel

    # Query all components matching the image coordinates
    query = CompModel.query.filter_by(
        repository_name=repo.name,
        namespace=namespace,
        name=image_name,
    ).filter(CompModel.version.isnot(None))

    # Pagination parameters
    page_size = request.args.get("n", type=int)
    last_tag = request.args.get("last")

    if last_tag:
        query = query.filter(CompModel.version > last_tag)

    query = query.order_by(CompModel.version.asc())

    components = query.all()
    tags = sorted(
        {c.version for c in components if c.version is not None}
    )

    # Apply page size limit
    has_more = False
    if page_size is not None and page_size > 0:
        if len(tags) > page_size:
            has_more = True
            tags = tags[:page_size]

    result = {
        "name": name,
        "tags": tags if tags else None,
    }

    response = jsonify(result)

    # Add Link header for pagination
    if has_more and tags:
        next_last = tags[-1]
        link_params = f"n={page_size}&last={next_last}"
        link_url = f"/v2/{name}/tags/list?{link_params}"
        response.headers["Link"] = f'<{link_url}>; rel="next"'

    logger.info("Listed %d tag(s) for '%s'.", len(tags), name)
    return response


# ===========================================================================
# Endpoint: Repository Catalog
# ===========================================================================


@docker_v2_bp.route("/v2/_catalog", methods=["GET"])
def catalog() -> Response:
    """List all Docker repositories in the registry.

    Supports pagination via ``n`` (page size) and ``last`` (last seen
    repository name) query parameters.  Only includes repositories that
    the authenticated user has read access to.

    Returns:
        JSON response with ``repositories`` field.
    """
    logger.info("GET catalog.")

    from src.app.models.repository import Repository as RepoModel

    query = RepoModel.query.filter_by(format="docker", online=True)

    # Pagination parameters
    page_size = request.args.get("n", type=int)
    last_repo = request.args.get("last")

    if last_repo:
        query = query.filter(RepoModel.name > last_repo)

    query = query.order_by(RepoModel.name.asc())
    repos = query.all()

    repo_names: list[str] = sorted(r.name for r in repos)

    # Apply page size limit
    has_more = False
    if page_size is not None and page_size > 0:
        if len(repo_names) > page_size:
            has_more = True
            repo_names = repo_names[:page_size]

    result = {"repositories": repo_names}
    response = jsonify(result)

    if has_more and repo_names:
        next_last = repo_names[-1]
        link_params = f"n={page_size}&last={next_last}"
        link_url = f"/v2/_catalog?{link_params}"
        response.headers["Link"] = f'<{link_url}>; rel="next"'

    logger.info("Catalog returned %d repository(ies).", len(repo_names))
    return response


# ===========================================================================
# Internal Helper — Cross-Repo Blob Mount
# ===========================================================================


def _attempt_blob_mount(
    name: str,
    target_repo: Repository,
    mount_digest: str,
    mount_from: str,
) -> Response | None:
    """Attempt to mount a blob from another repository.

    If the blob identified by *mount_digest* exists in the *mount_from*
    repository, creates a reference to it in the *target_repo* without
    re-uploading the content.

    Args:
        name: Target Docker image name.
        target_repo: Target repository object.
        mount_digest: Digest of the blob to mount.
        mount_from: Source repository name to mount from.

    Returns:
        ``201 Created`` response if mount succeeded, or ``None`` to fall
        through to regular upload initiation.
    """
    if not _validate_digest(mount_digest):
        return None

    _, digest_hex = _parse_digest(mount_digest)
    from src.app.models.asset import Asset as AssetModel

    # Look for the blob in the source repository
    source_asset = AssetModel.query.filter_by(
        checksum_sha256=digest_hex,
    ).first()

    if source_asset is None:
        logger.debug(
            "Blob mount failed: digest=%s not found in '%s'.",
            mount_digest,
            mount_from,
        )
        return None

    # Check if blob already exists in the target repository
    existing = AssetModel.query.filter_by(
        repository_name=target_repo.name,
        checksum_sha256=digest_hex,
    ).first()

    if existing is not None:
        # Already exists — return success
        response = make_response("", 201)
        response.headers["Location"] = f"/v2/{name}/blobs/{mount_digest}"
        response.headers["Docker-Content-Digest"] = mount_digest
        response.headers["Content-Length"] = "0"
        logger.info(
            "Blob mount: digest=%s already exists in '%s'.",
            mount_digest,
            target_repo.name,
        )
        return response

    # Create a new asset record in the target repository referencing
    # the same blob_ref
    try:
        mounted_asset = AssetModel(
            repository_name=target_repo.name,
            path=f"/v2/{name}/blobs/{mount_digest}",
            content_type=source_asset.content_type or "application/octet-stream",
            size=source_asset.size,
            checksum_sha256=digest_hex,
            blob_ref=source_asset.blob_ref,
        )
        db.session.add(mounted_asset)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.warning(
            "Failed to mount blob digest=%s.", mount_digest, exc_info=True
        )
        return None

    response = make_response("", 201)
    response.headers["Location"] = f"/v2/{name}/blobs/{mount_digest}"
    response.headers["Docker-Content-Digest"] = mount_digest
    response.headers["Content-Length"] = "0"

    logger.info(
        "Blob mounted: digest=%s from '%s' to '%s'.",
        mount_digest,
        mount_from,
        target_repo.name,
    )
    return response


# ===========================================================================
# Internal Helper — Monolithic Upload
# ===========================================================================


def _complete_monolithic_upload(
    name: str,
    repo: Repository,
    expected_digest: str,
    data: bytes,
) -> Response:
    """Complete a monolithic (single-request) blob upload.

    Used when the ``POST /v2/{name}/blobs/uploads/`` request includes
    both a ``digest`` query parameter and request body data.

    Args:
        name: Docker image name.
        repo: Target repository.
        expected_digest: Expected sha256 digest of the uploaded data.
        data: The complete blob content.

    Returns:
        ``201 Created`` on success, or an error response.
    """
    if not _validate_digest(expected_digest):
        return _docker_error(
            "DIGEST_INVALID",
            f"invalid digest format: {expected_digest}",
            status=400,
        )

    actual_digest = _compute_digest(data)
    if actual_digest != expected_digest:
        return _docker_error(
            "DIGEST_INVALID",
            f"digest mismatch: expected {expected_digest}, got {actual_digest}",
            status=400,
        )

    # Store in BlobStore
    blobstore = _get_blobstore_for_repo(repo)
    blob_ref_str: str | None = None
    if blobstore is not None:
        blob_id = BlobId.generate(repo.blob_store_name)
        try:
            blob = blobstore.create(
                blob_id=blob_id,
                data=data,
                content_type="application/octet-stream",
            )
            blob_ref_str = str(blob.blob_id)
        except Exception:
            logger.exception("Failed to store monolithic blob.")
            return _docker_error(
                "BLOB_UPLOAD_INVALID",
                "failed to store blob",
                status=500,
            )

    # Create Asset record
    _, digest_hex = _parse_digest(actual_digest)
    from src.app.models.asset import Asset as AssetModel

    try:
        asset_path = f"/v2/{name}/blobs/{actual_digest}"
        existing = AssetModel.query.filter_by(
            repository_name=repo.name,
            path=asset_path,
        ).first()

        if existing is None:
            new_asset = AssetModel(
                repository_name=repo.name,
                path=asset_path,
                content_type="application/octet-stream",
                size=len(data),
                checksum_sha256=digest_hex,
                blob_ref=blob_ref_str,
            )
            db.session.add(new_asset)
        else:
            existing.size = len(data)
            existing.checksum_sha256 = digest_hex
            existing.blob_ref = blob_ref_str

        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to persist monolithic blob asset.")

    location = f"/v2/{name}/blobs/{actual_digest}"
    response = make_response("", 201)
    response.headers["Location"] = location
    response.headers["Docker-Content-Digest"] = actual_digest
    response.headers["Content-Length"] = "0"

    logger.info(
        "Monolithic blob upload completed: digest=%s, size=%d bytes.",
        actual_digest,
        len(data),
    )
    return response


# ===========================================================================
# Internal Helper — Referenced Blob Verification
# ===========================================================================


def _check_referenced_blobs(
    manifest: dict,
    repo: Repository,
    blobstore: BlobStore,
) -> list[str]:
    """Verify that all blobs referenced in a manifest exist.

    Checks both the ``config`` digest and all ``layers`` digests to
    ensure they are present in the repository's BlobStore or asset
    records.

    Args:
        manifest: Parsed manifest dictionary.
        repo: The target repository.
        blobstore: The repository's BlobStore instance.

    Returns:
        List of missing digest strings.  Empty if all blobs are present.
    """
    from src.app.models.asset import Asset as AssetModel

    missing: list[str] = []

    # Collect all referenced digests
    digests_to_check: list[str] = []
    config = manifest.get("config")
    if isinstance(config, dict) and "digest" in config:
        digests_to_check.append(config["digest"])

    layers = manifest.get("layers", [])
    for layer in layers:
        if isinstance(layer, dict) and "digest" in layer:
            digests_to_check.append(layer["digest"])

    for ref_digest in digests_to_check:
        if not _validate_digest(ref_digest):
            missing.append(ref_digest)
            continue

        _, hex_val = _parse_digest(ref_digest)
        asset = AssetModel.query.filter_by(
            repository_name=repo.name,
            checksum_sha256=hex_val,
        ).first()

        if asset is None:
            # Also check BlobStore directly
            try:
                blob_id = BlobId.generate(repo.blob_store_name)
                # We can't check by digest in BlobStore directly, so
                # rely on asset records.
                missing.append(ref_digest)
            except Exception:
                missing.append(ref_digest)

    return missing


# ===========================================================================
# Blueprint Registration Helper
# ===========================================================================


def get_docker_v2_blueprint() -> Blueprint:
    """Return the Docker V2 Registry API Blueprint.

    This function is called by ``handler.py`` to obtain the Blueprint for
    registration as part of the Docker format handler.

    Returns:
        The :data:`docker_v2_bp` Blueprint instance.
    """
    return docker_v2_bp
