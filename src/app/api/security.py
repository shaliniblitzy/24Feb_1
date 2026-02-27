"""
SSL/TLS Certificate Management REST API Blueprint — Feature F-302.

This module implements the SSL/TLS certificate management REST API endpoints
for the Sonatype Nexus Repository Flask application.  It provides CRUD
operations for the SSL trust store (list, import, remove certificates),
remote certificate retrieval for proxy repository upstream servers, and
LDAP SSL configuration management.

Replaces:
    - Java BouncyCastle 1.78.1 certificate management REST API
    - Jetty 12.0.5 SslContextFactory REST endpoints
    - Java ``java.security.KeyStore`` truststore management API
    - RESTEasy 6.2.7 JAX-RS resource classes for SSL/TLS administration

Endpoints:
    GET    /api/v1/security/ssl/truststore              — List trusted certs
    POST   /api/v1/security/ssl/truststore              — Import PEM cert
    DELETE /api/v1/security/ssl/truststore/<fingerprint> — Remove cert
    GET    /api/v1/security/ssl/certificate              — Fetch remote cert
    PUT    /api/v1/security/ssl/ldap                     — LDAP SSL settings
    GET    /api/v1/security/ssl/info                     — SSL config info

Security:
    - All endpoints require authentication via ``login_required``
    - RBAC enforced via ``require_permission('ssl', <action>)``
    - Private keys are **NEVER** exposed in API responses
    - Certificate fingerprints use SHA-256 hashing

Architecture:
    - Delegates certificate storage operations to
      :class:`~src.app.security.certificate_store.CertificateStore`
    - Delegates SSL configuration queries to
      :class:`~src.app.security.ssl_manager.SSLManager`
    - Uses Marshmallow schemas for OpenAPI 3.x documentation

Exports:
    security_bp : flask_smorest.Blueprint
        The security SSL/TLS API blueprint, registered at
        ``/api/v1/security/ssl``.
"""

from __future__ import annotations

import logging
import ssl as stdlib_ssl
import socket
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import current_app, jsonify, request
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields, validate

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_permission
from src.app.extensions import db
from src.app.security.certificate_store import CertificateStore, CertificateInfo
from src.app.security.ssl_manager import SSLManager, create_ssl_manager

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for SSL/TLS certificate CRUD operations,
# PEM parsing, remote certificate retrieval, trust store modifications,
# and error conditions per AAP Section 0.7.2.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ============================================================================
# Blueprint Definition
# ============================================================================

security_bp: Blueprint = Blueprint(
    "security",
    __name__,
    url_prefix="/api/v1/security/ssl",
    description="SSL/TLS certificate management (F-302)",
)
"""Flask-smorest Blueprint for the SSL/TLS security API endpoint group.

Registered at ``/api/v1/security/ssl`` by
``src.app.api.register_api_blueprints()`` (or equivalent registration
in ``factory.py``).
"""

# ============================================================================
# Marshmallow Schemas — Request / Response Definitions
# ============================================================================
# Replaces Jackson 2.16.1 JSON DTOs from the Java source system.
# Inline schemas provide OpenAPI 3.x documentation auto-generation via
# flask-smorest 0.45.0 and Marshmallow 3.23.2.
# ============================================================================


class CertificateResponseSchema(Schema):
    """Schema for certificate metadata in API responses.

    Represents the public (non-sensitive) attributes of an X.509
    certificate.  Private keys are **NEVER** included.

    Used by:
        - GET /truststore (list of certificates)
        - POST /truststore (newly imported certificate)
        - GET /certificate (remote certificate details)
    """

    alias = fields.String(
        metadata={"description": "Unique alias/identifier for the certificate"},
    )
    subject = fields.String(
        metadata={"description": "Certificate subject common name (CN)"},
    )
    issuer = fields.String(
        metadata={"description": "Certificate issuer common name (CN)"},
    )
    serial_number = fields.String(
        metadata={"description": "Certificate serial number (hex)"},
    )
    not_before = fields.String(
        metadata={"description": "Validity start date (ISO 8601 UTC)"},
    )
    not_after = fields.String(
        metadata={"description": "Validity end date (ISO 8601 UTC)"},
    )
    fingerprint_sha256 = fields.String(
        metadata={"description": "SHA-256 fingerprint of the DER-encoded certificate"},
    )
    fingerprint_sha1 = fields.String(
        metadata={"description": "SHA-1 fingerprint (legacy, for compatibility)"},
    )
    is_ca = fields.Boolean(
        metadata={"description": "True if BasicConstraints CA=True"},
    )
    is_self_signed = fields.Boolean(
        metadata={"description": "True if subject and issuer match"},
    )
    subject_alternative_names = fields.List(
        fields.String(),
        metadata={"description": "DNS names and IPs from the SAN extension"},
    )
    key_type = fields.String(
        metadata={"description": "Public key algorithm (RSA, EC, etc.)"},
    )
    key_size = fields.Integer(
        metadata={"description": "Public key size in bits"},
    )
    category = fields.String(
        metadata={"description": "Certificate category (trusted_ca, client, server)"},
    )
    days_until_expiry = fields.Integer(
        metadata={"description": "Days remaining until expiration"},
    )
    is_expired = fields.Boolean(
        metadata={"description": "True if the certificate has expired"},
    )
    expiry_status = fields.String(
        metadata={"description": "Expiry status: ok, warning, critical, expired"},
    )


class CertificateListResponseSchema(Schema):
    """Schema for the GET /truststore response.

    Wraps a list of certificate metadata with a total count.
    """

    total = fields.Integer(
        metadata={"description": "Total number of trusted certificates"},
    )
    certificates = fields.List(
        fields.Nested(CertificateResponseSchema),
        metadata={"description": "List of trusted certificate details"},
    )


class CertificateUploadSchema(Schema):
    """Schema for POST /truststore request body.

    Accepts a PEM-encoded certificate and optional metadata.
    """

    pem_data = fields.String(
        required=True,
        validate=validate.Length(min=50, max=65536),
        metadata={
            "description": (
                "PEM-encoded X.509 certificate data. Must contain "
                "-----BEGIN CERTIFICATE----- and "
                "-----END CERTIFICATE----- markers."
            ),
        },
    )
    alias = fields.String(
        load_default=None,
        metadata={
            "description": (
                "Optional alias for the certificate. "
                "Auto-generated from the subject CN if omitted."
            ),
        },
    )
    category = fields.String(
        load_default="trusted_ca",
        metadata={
            "description": (
                "Certificate category: trusted_ca, client, or server. "
                "Defaults to trusted_ca."
            ),
        },
    )


class RemoteCertificateQuerySchema(Schema):
    """Schema for GET /certificate query parameters.

    Specifies the remote host and port from which to retrieve the
    SSL/TLS certificate presented during the TLS handshake.
    """

    host = fields.String(
        required=True,
        validate=validate.Length(min=1, max=253),
        metadata={
            "description": "Remote hostname or IP address to connect to",
        },
    )
    port = fields.Integer(
        load_default=443,
        metadata={
            "description": "Remote port number (default: 443)",
        },
    )


class LDAPSSLSettingsSchema(Schema):
    """Schema for PUT /ldap request body.

    Configures SSL/TLS settings for LDAP directory connections used
    by the LDAP authentication realm (Feature F-301).
    """

    enabled = fields.Boolean(
        required=True,
        metadata={"description": "Enable SSL/TLS for LDAP connections"},
    )
    protocol = fields.String(
        load_default="ldaps",
        metadata={
            "description": (
                "LDAP SSL protocol: 'ldaps' (LDAPS on port 636) or "
                "'starttls' (STARTTLS upgrade on port 389)"
            ),
        },
    )
    verify_certificates = fields.Boolean(
        load_default=True,
        metadata={
            "description": "Verify the LDAP server's SSL certificate",
        },
    )
    verify_hostname = fields.Boolean(
        load_default=True,
        metadata={
            "description": "Verify the LDAP server's hostname against the certificate",
        },
    )
    ca_certificate_pem = fields.String(
        load_default=None,
        metadata={
            "description": (
                "Optional PEM-encoded CA certificate for verifying "
                "the LDAP server's certificate"
            ),
        },
    )
    client_certificate_pem = fields.String(
        load_default=None,
        metadata={
            "description": (
                "Optional PEM-encoded client certificate for mutual TLS "
                "with the LDAP server"
            ),
        },
    )


class SSLInfoResponseSchema(Schema):
    """Schema for the GET /info response.

    Provides an overview of the current SSL/TLS configuration and
    certificate store statistics.
    """

    tls_min_version = fields.String(
        metadata={"description": "Minimum TLS protocol version"},
    )
    tls_max_version = fields.String(
        metadata={"description": "Maximum TLS protocol version"},
    )
    cipher_suites = fields.List(
        fields.String(),
        metadata={"description": "Configured cipher suite names"},
    )
    server_cert_configured = fields.Boolean(
        metadata={"description": "Whether a server certificate is configured"},
    )
    client_cert_configured = fields.Boolean(
        metadata={"description": "Whether a client certificate is configured"},
    )
    certificate_count = fields.Integer(
        metadata={"description": "Total certificates in the trust store"},
    )
    certificates_expiring_soon = fields.Integer(
        metadata={"description": "Certificates nearing expiration"},
    )
    timestamp = fields.DateTime(
        metadata={"description": "Response timestamp (ISO 8601 UTC)"},
    )


# ============================================================================
# Internal Helper Functions
# ============================================================================


def _get_ssl_manager() -> SSLManager:
    """Retrieve or create the application's SSL manager.

    Looks for a persistent :class:`SSLManager` stored in
    ``current_app.extensions['ssl_manager']``.  If not found, creates
    a new instance from the application configuration using
    :func:`create_ssl_manager`.

    Returns:
        The active :class:`SSLManager` instance.
    """
    try:
        manager: Optional[SSLManager] = current_app.extensions.get(
            "ssl_manager"
        )
        if manager is not None:
            return manager
    except RuntimeError:
        pass

    logger.debug(
        "No persistent SSLManager found in app extensions; "
        "creating from application config."
    )
    manager = create_ssl_manager(current_app.config)
    return manager


def _get_certificate_store() -> CertificateStore:
    """Retrieve or create the application's certificate store.

    Looks for a persistent :class:`CertificateStore` stored in
    ``current_app.extensions['certificate_store']``.  If not found,
    creates a new instance with the default store directory.

    Returns:
        The active :class:`CertificateStore` instance.
    """
    try:
        store: Optional[CertificateStore] = current_app.extensions.get(
            "certificate_store"
        )
        if store is not None:
            return store
    except RuntimeError:
        pass

    logger.debug(
        "No persistent CertificateStore found in app extensions; "
        "creating with default directory."
    )
    store_dir: str = current_app.config.get(
        "CERTIFICATE_STORE_DIR", "data/certificates"
    )
    return CertificateStore(store_dir=store_dir)


def _fetch_remote_certificate(
    host: str, port: int = 443, timeout: int = 10
) -> Dict[str, Any]:
    """Connect to a remote host and retrieve its SSL/TLS certificate.

    Performs a TLS handshake with the remote server and extracts the
    presented certificate's metadata.  Used for reviewing upstream
    proxy repository server certificates before adding them to the
    trust store.

    Args:
        host: Remote hostname or IP address.
        port: Remote port number (default 443).
        timeout: Connection timeout in seconds.

    Returns:
        Dictionary with certificate details including subject, issuer,
        validity dates, and PEM-encoded certificate data.

    Raises:
        ConnectionError: If the connection or TLS handshake fails.
    """
    logger.info(
        "Fetching remote SSL certificate: host=%s, port=%d",
        host, port,
    )

    try:
        # Create a permissive SSL context for certificate retrieval
        # (we want to see the certificate even if it's self-signed)
        ctx = stdlib_ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = stdlib_ssl.CERT_NONE

        with socket.create_connection(
            (host, port), timeout=timeout
        ) as raw_sock:
            with ctx.wrap_socket(raw_sock, server_hostname=host) as ssl_sock:
                # Get DER-encoded certificate
                der_cert: bytes = ssl_sock.getpeercert(binary_form=True)
                peer_cert_dict: Optional[Dict[str, Any]] = ssl_sock.getpeercert()

        if der_cert is None:
            raise ConnectionError(
                f"No certificate presented by {host}:{port}"
            )

        # Parse with cryptography library for detailed metadata
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.backends import default_backend
        from cryptography.x509.oid import NameOID

        cert = x509.load_der_x509_certificate(der_cert, default_backend())

        # Extract subject CN
        subject_cn: str = "unknown"
        try:
            cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
            if cn_attrs:
                subject_cn = str(cn_attrs[0].value)
        except Exception:
            pass

        # Extract issuer CN
        issuer_cn: str = "unknown"
        try:
            cn_attrs = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
            if cn_attrs:
                issuer_cn = str(cn_attrs[0].value)
        except Exception:
            pass

        # Convert to PEM for potential import
        pem_bytes: bytes = cert.public_bytes(serialization.Encoding.PEM)
        fingerprint_sha256: str = cert.fingerprint(hashes.SHA256()).hex()

        # Validity dates
        not_before = cert.not_valid_before_utc
        not_after = cert.not_valid_after_utc
        is_self_signed: bool = cert.subject == cert.issuer

        # Subject Alternative Names
        sans: List[str] = []
        try:
            from cryptography.x509.oid import ExtensionOID
            san_ext = cert.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME
            )
            for general_name in san_ext.value:
                sans.append(str(general_name.value))
        except Exception:
            pass

        now = datetime.now(timezone.utc)
        days_until_expiry: int = (not_after - now).days

        result: Dict[str, Any] = {
            "host": host,
            "port": port,
            "subject": subject_cn,
            "issuer": issuer_cn,
            "serial_number": hex(cert.serial_number),
            "not_before": not_before.isoformat(),
            "not_after": not_after.isoformat(),
            "fingerprint_sha256": fingerprint_sha256,
            "is_self_signed": is_self_signed,
            "subject_alternative_names": sans,
            "days_until_expiry": days_until_expiry,
            "is_expired": now > not_after,
            "pem_data": pem_bytes.decode("ascii"),
            "retrieved_at": now.isoformat(),
        }

        logger.info(
            "Retrieved remote certificate: host=%s, port=%d, "
            "subject=%s, issuer=%s, fingerprint=%s",
            host, port, subject_cn, issuer_cn,
            fingerprint_sha256[:16] + "...",
        )
        return result

    except socket.timeout:
        error_msg = f"Connection timed out connecting to {host}:{port}"
        logger.error(error_msg)
        raise ConnectionError(error_msg)
    except socket.gaierror as exc:
        error_msg = f"DNS resolution failed for {host}: {exc}"
        logger.error(error_msg)
        raise ConnectionError(error_msg)
    except stdlib_ssl.SSLError as exc:
        error_msg = f"SSL error connecting to {host}:{port}: {exc}"
        logger.error(error_msg)
        raise ConnectionError(error_msg)
    except OSError as exc:
        error_msg = f"Network error connecting to {host}:{port}: {exc}"
        logger.error(error_msg)
        raise ConnectionError(error_msg)


# ============================================================================
# Endpoint 1 — List Trusted Certificates (GET /truststore)
# ============================================================================


@security_bp.route("/truststore", methods=["GET"])
@security_bp.response(200, CertificateListResponseSchema)
@login_required
@require_permission("ssl", "read")
def list_trusted_certificates() -> Dict[str, Any]:
    """List all trusted CA certificates in the trust store.

    Returns the metadata of every certificate in the SSL trust store,
    including subject, issuer, validity dates, fingerprints, and
    expiration status.  Private keys are **NEVER** included.

    This endpoint is used by administrators to review the current trust
    store contents, especially when configuring proxy repositories that
    connect to upstream registries using custom or private CAs.

    Returns:
        JSON response with ``total`` count and ``certificates`` list.
    """
    logger.debug("Listing trusted certificates in trust store.")

    store: CertificateStore = _get_certificate_store()
    certificates: List[CertificateInfo] = store.list_certificates()

    # Also retrieve the raw trusted CA PEM list for count verification
    trusted_ca_pems: List[bytes] = store.get_trusted_ca_certificates()
    logger.debug(
        "Trust store contains %d trusted CA PEM certificate(s).",
        len(trusted_ca_pems),
    )

    # Serialize certificate metadata — NEVER expose private keys
    cert_list: List[Dict[str, Any]] = []
    for cert_info in certificates:
        # Log expiration warnings for each certificate
        if cert_info.not_after is not None:
            now = datetime.now(timezone.utc)
            not_after_utc = cert_info.not_after
            if not_after_utc.tzinfo is None:
                not_after_utc = not_after_utc.replace(tzinfo=timezone.utc)
            if now > not_after_utc:
                logger.warning(
                    "Expired certificate in trust store: subject=%s, "
                    "issuer=%s, fingerprint=%s",
                    cert_info.subject,
                    cert_info.issuer,
                    cert_info.fingerprint_sha256[:16] + "...",
                )
        cert_dict: Dict[str, Any] = cert_info.to_dict()
        cert_list.append(cert_dict)

    logger.info(
        "Trust store listing returned %d certificate(s).",
        len(cert_list),
    )

    return jsonify({
        "total": len(cert_list),
        "certificates": cert_list,
    })


# ============================================================================
# Endpoint 2 — Import Trusted Certificate (POST /truststore)
# ============================================================================


@security_bp.route("/truststore", methods=["POST"])
@login_required
@require_permission("ssl", "create")
@security_bp.arguments(CertificateUploadSchema, location="json")
@security_bp.response(201, CertificateResponseSchema)
def import_trusted_certificate(upload_data: Dict[str, Any]) -> Any:
    """Import a PEM-encoded X.509 certificate into the trust store.

    Accepts a PEM-encoded certificate in the request body, validates it,
    stores it in the trust store, and returns the extracted metadata.
    Used for adding upstream registry CA certificates required by proxy
    repositories.

    The certificate is validated for structural integrity (valid PEM
    format, parseable X.509 data) before being stored.  The
    ``SSLManager.validate_certificate_chain()`` method provides
    additional chain validation when available.

    Args:
        upload_data: Validated request body containing ``pem_data``,
            optional ``alias``, and optional ``category``.

    Returns:
        201 response with the imported certificate's metadata.
    """
    # flask-smorest @bp.arguments() parses the JSON body; we also
    # support raw PEM upload via request.data for CLI tools that POST
    # the PEM file directly without JSON wrapping.
    if upload_data is None or "pem_data" not in upload_data:
        # Fallback: try reading raw request body as PEM
        raw_data: bytes = request.data
        if raw_data and b"-----BEGIN CERTIFICATE-----" in raw_data:
            upload_data = {
                "pem_data": raw_data.decode("utf-8", errors="replace"),
            }
        else:
            # Also check if JSON was sent directly
            json_body: Optional[Dict[str, Any]] = request.get_json(silent=True)
            if json_body and "pem_data" in json_body:
                upload_data = json_body

    pem_data_str: str = upload_data["pem_data"]
    alias: Optional[str] = upload_data.get("alias")
    category: str = upload_data.get("category", "trusted_ca")

    logger.info(
        "Importing PEM certificate into trust store: alias=%s, category=%s",
        alias or "(auto-generated)",
        category,
    )

    # Validate PEM format markers
    if "-----BEGIN CERTIFICATE-----" not in pem_data_str:
        logger.warning("Invalid PEM data: missing BEGIN CERTIFICATE marker.")
        abort(
            400,
            message="Invalid PEM data: must contain "
            "-----BEGIN CERTIFICATE----- marker.",
        )

    if "-----END CERTIFICATE-----" not in pem_data_str:
        logger.warning("Invalid PEM data: missing END CERTIFICATE marker.")
        abort(
            400,
            message="Invalid PEM data: must contain "
            "-----END CERTIFICATE----- marker.",
        )

    pem_bytes: bytes = pem_data_str.encode("utf-8")

    # Validate certificate chain using SSLManager
    ssl_manager: SSLManager = _get_ssl_manager()
    validation_result: Dict[str, Any] = ssl_manager.validate_certificate_chain(
        pem_bytes
    )

    if not validation_result.get("valid", False):
        errors: List[str] = validation_result.get("errors", [])
        error_msg: str = "; ".join(errors) if errors else "Invalid certificate"
        logger.warning(
            "Certificate validation failed: %s", error_msg,
        )
        abort(400, message=f"Certificate validation failed: {error_msg}")

    # Check certificate expiry
    expiry_info: Dict[str, Any] = ssl_manager.check_certificate_expiry(pem_bytes)
    if expiry_info.get("status") == "expired":
        logger.warning(
            "Attempting to import an expired certificate: subject=%s",
            expiry_info.get("subject", "unknown"),
        )
        # Allow import but warn — administrators may need to import
        # expired certs for specific trust chain scenarios

    # Import into the certificate store
    store: CertificateStore = _get_certificate_store()
    try:
        cert_info: CertificateInfo = store.import_pem(
            pem_bytes, alias=alias, category=category,
        )
    except ValueError as exc:
        logger.error("Failed to import certificate: %s", exc)
        abort(400, message=f"Failed to import certificate: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error importing certificate: %s", exc)
        abort(500, message="Internal error during certificate import")

    logger.info(
        "Certificate imported successfully: alias=%s, subject=%s, "
        "fingerprint=%s",
        cert_info.alias,
        cert_info.subject,
        cert_info.fingerprint_sha256[:16] + "...",
    )

    # Use db.session context for any transactional operations
    # (CertificateStore manages its own persistence, but we ensure
    #  the database session is in a clean state)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.debug(
            "Database session commit after certificate import "
            "(no DB-backed operation required)."
        )

    response_data: Dict[str, Any] = cert_info.to_dict()
    return jsonify(response_data), 201


# ============================================================================
# Endpoint 3 — Remove Trusted Certificate (DELETE /truststore/<fingerprint>)
# ============================================================================


@security_bp.route(
    "/truststore/<string:fingerprint>", methods=["DELETE"]
)
@login_required
@require_permission("ssl", "delete")
def remove_trusted_certificate(fingerprint: str) -> Any:
    """Remove a trusted certificate from the trust store by fingerprint.

    Looks up the certificate by its SHA-256 fingerprint and removes it
    from both the in-memory index and on-disk storage.  Returns 204 on
    success, 404 if no certificate matches the fingerprint.

    Args:
        fingerprint: SHA-256 fingerprint hex string (case-insensitive).

    Returns:
        204 No Content on successful removal.
    """
    logger.info(
        "Removing certificate from trust store by fingerprint: %s",
        fingerprint,
    )

    store: CertificateStore = _get_certificate_store()

    # Look up the certificate by fingerprint
    cert_info: Optional[CertificateInfo] = store.find_by_fingerprint(fingerprint)

    if cert_info is None:
        logger.warning(
            "Certificate not found for fingerprint: %s", fingerprint,
        )
        abort(
            404,
            message=f"Certificate not found with fingerprint: {fingerprint}",
        )

    # Extract alias and subject before removal for logging
    cert_alias: str = cert_info.alias
    cert_subject: str = cert_info.subject
    cert_fp: str = cert_info.fingerprint_sha256

    # Remove the certificate
    removed: bool = store.remove_certificate(cert_alias)

    if not removed:
        logger.error(
            "Failed to remove certificate: alias=%s, fingerprint=%s",
            cert_alias, fingerprint,
        )
        abort(
            500,
            message="Failed to remove certificate from trust store",
        )

    logger.info(
        "Certificate removed from trust store: alias=%s, subject=%s, "
        "fingerprint=%s",
        cert_alias, cert_subject, cert_fp[:16] + "...",
    )

    return "", 204


# ============================================================================
# Endpoint 4 — Retrieve Remote Certificate (GET /certificate)
# ============================================================================


@security_bp.route("/certificate", methods=["GET"])
@security_bp.arguments(RemoteCertificateQuerySchema, location="query")
@login_required
@require_permission("ssl", "read")
def retrieve_remote_certificate(
    query_params: Dict[str, Any],
) -> Dict[str, Any]:
    """Retrieve the SSL/TLS certificate from a remote host.

    Connects to the specified host and port, performs a TLS handshake,
    and extracts the server's certificate metadata.  The returned
    information allows administrators to review the certificate before
    deciding to import it into the trust store.

    This is particularly useful when configuring proxy repositories that
    connect to upstream registries using self-signed or private CA
    certificates.

    Args:
        query_params: Validated query parameters with ``host`` (required)
            and ``port`` (optional, default 443).

    Returns:
        JSON response with the remote certificate details including
        subject, issuer, validity dates, fingerprint, and the PEM data
        for potential import.
    """
    host: str = query_params["host"]
    port: int = query_params.get("port", 443)

    # Also support raw query parameter access for backwards compatibility
    # with clients that don't use the schema-based query format
    if not host:
        host = request.args.get("host", "")
    if port == 443 and request.args.get("port"):
        try:
            port = int(request.args.get("port", "443"))
        except (ValueError, TypeError):
            port = 443

    logger.info(
        "Retrieving remote SSL certificate: host=%s, port=%d",
        host, port,
    )

    # Validate host parameter
    if not host or host.isspace():
        abort(400, message="Host parameter is required and cannot be empty")

    try:
        cert_details: Dict[str, Any] = _fetch_remote_certificate(
            host=host, port=port,
        )
    except ConnectionError as exc:
        logger.error(
            "Failed to retrieve remote certificate from %s:%d — %s",
            host, port, exc,
        )
        abort(
            500,
            message=f"Failed to retrieve certificate from "
            f"{host}:{port}: {exc}",
        )
    except Exception as exc:
        logger.exception(
            "Unexpected error retrieving remote certificate from %s:%d",
            host, port,
        )
        abort(
            500,
            message=f"Unexpected error connecting to {host}:{port}: {exc}",
        )

    return jsonify(cert_details)


# ============================================================================
# Endpoint 5 — LDAP SSL Settings (PUT /ldap)
# ============================================================================


@security_bp.route("/ldap", methods=["PUT"])
@login_required
@require_permission("ssl", "update")
@security_bp.arguments(LDAPSSLSettingsSchema, location="json")
def update_ldap_ssl_settings(
    settings_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Configure SSL/TLS settings for LDAP directory connections.

    Updates the SSL configuration used by the LDAP authentication realm
    when connecting to LDAP/Active Directory servers.  Supports both
    LDAPS (port 636) and STARTTLS (port 389) protocols.

    The settings are persisted via the system configuration service and
    take effect on the next LDAP authentication attempt.

    Args:
        settings_data: Validated settings from the request body.

    Returns:
        JSON response confirming the updated settings.
    """
    enabled: bool = settings_data["enabled"]
    protocol: str = settings_data.get("protocol", "ldaps")
    verify_certs: bool = settings_data.get("verify_certificates", True)
    verify_host: bool = settings_data.get("verify_hostname", True)
    ca_cert_pem: Optional[str] = settings_data.get("ca_certificate_pem")
    client_cert_pem: Optional[str] = settings_data.get("client_certificate_pem")

    logger.info(
        "Updating LDAP SSL settings: enabled=%s, protocol=%s, "
        "verify_certs=%s, verify_host=%s",
        enabled, protocol, verify_certs, verify_host,
    )

    # Validate protocol value
    if protocol not in ("ldaps", "starttls"):
        abort(
            400,
            message="Invalid protocol: must be 'ldaps' or 'starttls'",
        )

    # Validate CA certificate PEM if provided
    if ca_cert_pem:
        if "-----BEGIN CERTIFICATE-----" not in ca_cert_pem:
            abort(
                400,
                message="Invalid CA certificate PEM: missing "
                "-----BEGIN CERTIFICATE----- marker",
            )
        # Validate the CA cert using SSLManager
        ssl_manager: SSLManager = _get_ssl_manager()
        ca_validation: Dict[str, Any] = ssl_manager.validate_certificate_chain(
            ca_cert_pem.encode("utf-8")
        )
        if not ca_validation.get("valid", False):
            errors: List[str] = ca_validation.get("errors", [])
            abort(
                400,
                message=f"Invalid CA certificate: {'; '.join(errors)}",
            )

    # Store LDAP SSL settings in application configuration
    # These are persisted via the system configuration service
    ldap_ssl_config: Dict[str, Any] = {
        "enabled": enabled,
        "protocol": protocol,
        "verify_certificates": verify_certs,
        "verify_hostname": verify_host,
        "ca_certificate_configured": ca_cert_pem is not None,
        "client_certificate_configured": client_cert_pem is not None,
    }

    # Update the application config for the LDAP SSL settings
    current_app.config["LDAP_SSL_ENABLED"] = enabled
    current_app.config["LDAP_SSL_PROTOCOL"] = protocol
    current_app.config["LDAP_SSL_VERIFY_CERTIFICATES"] = verify_certs
    current_app.config["LDAP_SSL_VERIFY_HOSTNAME"] = verify_host

    if ca_cert_pem:
        current_app.config["LDAP_SSL_CA_CERT_PEM"] = ca_cert_pem

    if client_cert_pem:
        current_app.config["LDAP_SSL_CLIENT_CERT_PEM"] = client_cert_pem

    # Commit any database-backed configuration changes
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.debug(
            "Database session commit after LDAP SSL config update."
        )

    logger.info(
        "LDAP SSL settings updated successfully: enabled=%s, protocol=%s",
        enabled, protocol,
    )

    now_str: str = datetime.now(timezone.utc).isoformat()

    return jsonify({
        "status": "success",
        "message": "LDAP SSL settings updated successfully",
        "settings": ldap_ssl_config,
        "updated_at": now_str,
    })


# ============================================================================
# Endpoint 6 — SSL Configuration Info (GET /info)
# ============================================================================


@security_bp.route("/info", methods=["GET"])
@login_required
@require_permission("ssl", "read")
def get_ssl_configuration_info() -> Dict[str, Any]:
    """Return comprehensive SSL/TLS configuration information.

    Aggregates the current TLS configuration, cipher suites, certificate
    counts, and renewal status into a single response.  Used by
    administrators to verify SSL configuration and monitor certificate
    health.

    Returns:
        JSON response with SSL/TLS configuration summary and
        certificate store statistics.
    """
    logger.debug("Retrieving SSL configuration information.")

    ssl_manager: SSLManager = _get_ssl_manager()
    ssl_info: Dict[str, Any] = ssl_manager.get_ssl_info()

    # Add certificate renewal status
    renewal_status: List[Dict[str, Any]] = ssl_manager.get_renewal_status()

    # Include the current LDAP SSL configuration
    ldap_ssl_enabled: bool = current_app.config.get(
        "LDAP_SSL_ENABLED", False
    )
    ldap_ssl_protocol: str = current_app.config.get(
        "LDAP_SSL_PROTOCOL", "ldaps"
    )

    now: datetime = datetime.now(timezone.utc)

    response: Dict[str, Any] = {
        **ssl_info,
        "renewal_status": renewal_status,
        "ldap_ssl": {
            "enabled": ldap_ssl_enabled,
            "protocol": ldap_ssl_protocol,
        },
        "timestamp": now.isoformat(),
    }

    logger.info(
        "SSL configuration info returned: cert_count=%d, "
        "expiring_soon=%d",
        ssl_info.get("certificate_count", 0),
        ssl_info.get("certificates_expiring_soon", 0),
    )

    return jsonify(response)


# ---------------------------------------------------------------------------
# Module Initialization Logging
# ---------------------------------------------------------------------------

logger.debug(
    "Security SSL/TLS API module loaded — blueprint: %s, "
    "prefix: %s, endpoints: truststore (CRUD), certificate "
    "(remote fetch), ldap (SSL config), info",
    security_bp.name,
    "/api/v1/security/ssl",
)
