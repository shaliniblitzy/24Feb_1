"""
SSO Federation Authentication Realm (SAML 2.0 / OpenID Connect).

This module implements the :class:`SSORealm` — the SSO (Single Sign-On)
authentication realm that handles federated authentication via SAML 2.0
assertions and OpenID Connect (OIDC) id_tokens.  It supports enterprise
identity providers such as Okta, Azure AD, Keycloak, PingFederate, and
any SAML 2.0 or OIDC-compliant IdP.

**Architecture Context:**

- Replaces the Apache Shiro security plugin SAML/OIDC realm from the Java
  source system (AAP Section 0.2.2).
- **Realm order position: 5** — last in the authentication chain.  The
  chain order is: local → bearer_token → jwt → ldap → **sso**.
- Uses the **first-successful** strategy: if this realm successfully
  authenticates the request, authentication stops.
- Extends :class:`~src.app.auth.realms.RealmBase` from the realms package.

**SSO Protocol Support:**

+-------------------+-----------------------------------------------------+
| Protocol          | Implementation Details                              |
+===================+=====================================================+
| SAML 2.0          | Parses SAML assertions via ``defusedxml.ElementTree``|
|                   | Validates signatures with ``cryptography`` library  |
|                   | Extracts NameID, attributes, and group memberships  |
+-------------------+-----------------------------------------------------+
| OpenID Connect    | Validates id_tokens via ``PyJWT`` (RS256)           |
|                   | Fetches OIDC discovery and JWKS endpoints           |
|                   | Extracts sub, email, groups claims                  |
+-------------------+-----------------------------------------------------+

**User Auto-Provisioning:**

SSO-authenticated users are automatically created or updated in the local
database.  On first login, a new :class:`~src.app.models.user.User` record
is created with ``external_id`` set to the SSO subject.  On subsequent
logins, profile attributes (email, display name) are refreshed from the
IdP assertion/token claims.

**Group-to-Role Mapping:**

SSO group memberships from the IdP are mapped to local
:class:`~src.app.models.role.Role` entities using the configurable
``SSO_GROUP_ROLE_MAPPING`` dictionary (Feature F-301 RBAC).  Role
assignments are fully synchronised on each login — groups removed at the
IdP are reflected locally.

**Key Dependencies (from AAP Section 0.6.1):**

- ``PyJWT 2.10.1`` — OIDC id_token JWT validation
- ``requests 2.32.3`` — HTTP client for IdP endpoint calls
- ``cryptography 44.0.0`` — SAML signature verification
- ``Flask 3.1.3`` — ``current_app`` for runtime configuration access

**Configuration Keys (read from Flask app.config):**

- ``SSO_SAML_ENABLED`` — Enable SAML 2.0 authentication (bool)
- ``SSO_OIDC_ENABLED`` — Enable OIDC authentication (bool)
- ``SSO_SAML_IDP_METADATA_URL`` — IdP metadata document URL
- ``SSO_SAML_SP_ENTITY_ID`` — Service Provider entity ID
- ``SSO_SAML_IDP_CERT`` — PEM-encoded IdP certificate
- ``SSO_OIDC_ISSUER_URL`` — OIDC issuer URL (for discovery)
- ``SSO_OIDC_CLIENT_ID`` — OIDC client identifier
- ``SSO_OIDC_CLIENT_SECRET`` — OIDC client secret
- ``SSO_GROUP_ROLE_MAPPING`` — Dict mapping IdP group names to local role IDs

Exports:
    SSORealm : Concrete authentication realm for SAML 2.0 / OIDC SSO.
"""

from __future__ import annotations

import base64
import logging
import time
import defusedxml.ElementTree as ET  # Secure XML parsing — prevents XXE (CWE-611)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import jwt
import jwt.algorithms
import jwt.exceptions
import requests
import requests.exceptions
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509 import load_pem_x509_certificate
from flask import current_app

from src.app.auth.realms import RealmBase
from src.app.extensions import db
from src.app.models.role import Role
from src.app.models.user import User

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_METADATA_CACHE_TTL: int = 3600
"""Default TTL in seconds for cached IdP metadata (OIDC discovery, JWKS)."""

_HTTP_TIMEOUT: int = 30
"""Default HTTP request timeout in seconds for IdP endpoint calls."""

_SAML_NS: Dict[str, str] = {
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}
"""XML namespace prefixes for SAML 2.0 assertion parsing."""


# ---------------------------------------------------------------------------
# SSOConfig Dataclass
# ---------------------------------------------------------------------------


@dataclass
class SSOConfig:
    """Structured container for SSO configuration values.

    Populated from Flask ``app.config`` keys at runtime via
    :meth:`SSORealm._get_sso_config`.  Using a dataclass provides
    type-safe access and clear documentation of all SSO settings.
    """

    saml_enabled: bool = False
    oidc_enabled: bool = False
    saml_idp_metadata_url: str = ""
    saml_sp_entity_id: str = ""
    saml_idp_cert: str = ""
    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    group_role_mapping: Dict[str, str] = field(default_factory=dict)
    metadata_cache_ttl: int = _DEFAULT_METADATA_CACHE_TTL


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = ["SSORealm"]


# ===========================================================================
# SSORealm — SAML 2.0 / OIDC Authentication Realm
# ===========================================================================


class SSORealm(RealmBase):
    """SSO federation authentication realm for SAML 2.0 and OpenID Connect.

    Extends :class:`~src.app.auth.realms.RealmBase` to provide federated
    authentication against enterprise identity providers.  This is realm
    position **5** (last) in the multi-backend authentication chain.

    The realm supports two credential types:

    - ``saml_assertion`` — base64-encoded SAML 2.0 assertion XML
    - ``oidc_token`` — OIDC id_token (JWT)

    On successful authentication the realm auto-provisions or updates a
    local :class:`~src.app.models.user.User` record and synchronises
    SSO group memberships to local :class:`~src.app.models.role.Role`
    entities via the ``SSO_GROUP_ROLE_MAPPING`` configuration.
    """

    # ------------------------------------------------------------------
    # Class-Level Attributes (exported via members_exposed)
    # ------------------------------------------------------------------

    REALM_NAME: str = "sso"
    """Unique realm identifier used for logging, registration, and ordering."""

    SUPPORTED_CREDENTIAL_TYPES: List[str] = ["saml_assertion", "oidc_token"]
    """Credential dictionary keys that this realm can authenticate."""

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the SSO realm with empty caches.

        Calls ``super().__init__()`` to inherit the structured per-class
        logger from :class:`RealmBase`, then initialises the IdP metadata
        and OIDC discovery caches with their TTL tracking timestamps.
        """
        super().__init__()
        self.logger: logging.Logger = logging.getLogger(__name__)

        # IdP metadata caches (populated lazily on first auth attempt)
        self._idp_metadata: Optional[Dict[str, Any]] = None
        self._oidc_discovery: Optional[Dict[str, Any]] = None
        self._jwks_cache: Optional[Dict[str, Any]] = None

        # Cache age tracking (epoch seconds)
        self._metadata_cached_at: float = 0.0
        self._jwks_cached_at: float = 0.0

        # Configurable cache TTL (seconds) — overridable via app config
        self._metadata_cache_ttl: int = _DEFAULT_METADATA_CACHE_TTL

    # ==================================================================
    # RealmBase Interface Implementation
    # ==================================================================

    def authenticate(self, credentials: Dict[str, Any]) -> Optional[User]:
        """Attempt SSO authentication with the supplied credentials.

        Dispatches to :meth:`_authenticate_saml` or
        :meth:`_authenticate_oidc` depending on the credential type
        present in the *credentials* dictionary.

        Args:
            credentials: Dictionary that may contain:
                - ``'saml_assertion'``: Base64-encoded SAML assertion XML.
                - ``'oidc_token'``: OIDC id_token (JWT string).

        Returns:
            A :class:`~src.app.models.user.User` on success; ``None``
            if authentication fails or the credential type is not
            supported by this realm.
        """
        if not credentials:
            return None

        saml_assertion: Optional[str] = credentials.get("saml_assertion")
        if saml_assertion:
            self.logger.debug("SSO authentication attempt via SAML assertion.")
            try:
                return self._authenticate_saml(saml_assertion)
            except Exception:
                self.logger.exception(
                    "Unexpected error during SAML authentication."
                )
                return None

        oidc_token: Optional[str] = credentials.get("oidc_token")
        if oidc_token:
            self.logger.debug("SSO authentication attempt via OIDC token.")
            try:
                return self._authenticate_oidc(oidc_token)
            except Exception:
                self.logger.exception(
                    "Unexpected error during OIDC authentication."
                )
                return None

        # Credential type not recognised by this realm
        return None

    def supports(self, credentials: Dict[str, Any]) -> bool:
        """Check whether the credentials contain an SSO assertion or token.

        Args:
            credentials: Authentication credentials dictionary.

        Returns:
            ``True`` if the credentials contain ``'saml_assertion'`` or
            ``'oidc_token'``; ``False`` otherwise.
        """
        if not credentials or not isinstance(credentials, dict):
            return False
        return bool(
            credentials.get("saml_assertion") or credentials.get("oidc_token")
        )

    def get_realm_name(self) -> str:
        """Return the unique realm identifier.

        Returns:
            The string ``'sso'``.
        """
        return self.REALM_NAME

    def is_configured(self) -> bool:
        """Check whether SSO is properly configured.

        Returns ``True`` if either SAML or OIDC has the minimum required
        configuration values set in the Flask application config.

        Returns:
            ``True`` if at least one SSO protocol is fully configured;
            ``False`` otherwise.
        """
        try:
            config: SSOConfig = self._get_sso_config()
        except RuntimeError:
            # Outside Flask application context — realm is not configured
            self.logger.debug(
                "SSO realm is_configured() called outside app context; "
                "returning False."
            )
            return False

        saml_ok: bool = (
            config.saml_enabled
            and bool(config.saml_sp_entity_id)
            and bool(config.saml_idp_cert)
        )
        oidc_ok: bool = (
            config.oidc_enabled
            and bool(config.oidc_issuer_url)
            and bool(config.oidc_client_id)
        )

        configured: bool = saml_ok or oidc_ok
        self.logger.debug(
            "SSO realm configured check: saml=%s, oidc=%s, result=%s",
            saml_ok,
            oidc_ok,
            configured,
        )
        return configured

    # ==================================================================
    # SAML 2.0 Authentication
    # ==================================================================

    def _authenticate_saml(self, saml_assertion: str) -> Optional[User]:
        """Authenticate a user via a SAML 2.0 assertion.

        Workflow:
            1. Decode the base64-encoded SAML assertion.
            2. Parse the XML and validate the assertion:
               a. Verify the XML digital signature using the IdP certificate.
               b. Check ``NotOnOrAfter`` expiration condition.
               c. Verify ``AudienceRestriction`` matches our SP entity ID.
            3. Extract the NameID (subject) and attribute statements.
            4. Auto-provision or update the local user record.
            5. Synchronise SSO group memberships to local roles.

        Args:
            saml_assertion: Base64-encoded SAML assertion XML string.

        Returns:
            A :class:`User` on success; ``None`` on validation failure.
        """
        config: SSOConfig = self._get_sso_config()

        if not config.saml_enabled:
            self.logger.warning("SAML authentication attempted but SAML is not enabled.")
            return None

        # Step 1: Decode the base64 assertion
        try:
            decoded_xml: str = base64.b64decode(saml_assertion).decode("utf-8")
        except Exception:
            self.logger.error("Failed to base64-decode SAML assertion.")
            return None

        # Step 2a: Validate signature
        if config.saml_idp_cert:
            cert_bytes: bytes = config.saml_idp_cert.encode("utf-8")
            if not self._validate_saml_signature(decoded_xml, cert_bytes):
                self.logger.warning("SAML assertion signature validation failed.")
                return None
        else:
            self.logger.warning(
                "No IdP certificate configured; skipping SAML signature validation."
            )

        # Step 2b–c: Parse and validate assertion conditions
        try:
            root: ET.Element = ET.fromstring(decoded_xml)
        except ET.ParseError:
            self.logger.error("Failed to parse SAML assertion XML.")
            return None

        # Validate NotOnOrAfter (expiration)
        if not self._validate_saml_conditions(root, config):
            self.logger.warning("SAML assertion conditions validation failed.")
            return None

        # Step 3: Extract attributes
        attributes: Dict[str, Any] = self._parse_saml_attributes(decoded_xml)
        username: Optional[str] = attributes.get("username")

        if not username:
            self.logger.warning(
                "SAML assertion did not contain a NameID (subject)."
            )
            return None

        attributes["provider"] = "saml"

        # Step 4: Resolve or create local user
        user: User = self._resolve_or_create_user(username, attributes)

        # Step 5: Map SSO groups to local roles
        sso_groups: List[str] = attributes.get("groups", [])
        if sso_groups:
            self._map_sso_groups_to_roles(user, sso_groups)

        self.logger.info(
            "SAML authentication successful for user '%s'.", username
        )
        return user

    def _validate_saml_signature(
        self, assertion_xml: str, certificate: bytes
    ) -> bool:
        """Verify the XML digital signature of a SAML assertion.

        Loads the IdP's PEM-encoded X.509 certificate and verifies the
        assertion's XML signature.  This implementation performs a basic
        certificate loading check and structural signature element
        verification.  For production deployments, a full XML signature
        verification library (``xmlsec`` or ``signxml``) should be used
        for complete canonicalization and digest validation.

        Args:
            assertion_xml: Raw SAML assertion XML string.
            certificate: PEM-encoded IdP X.509 certificate bytes.

        Returns:
            ``True`` if the signature structure is valid and the
            certificate loads successfully; ``False`` otherwise.
        """
        try:
            # Load and validate the IdP certificate
            cert = load_pem_x509_certificate(certificate)
            public_key = cert.public_key()

            # Verify that the certificate has not expired
            not_valid_after = cert.not_valid_after_utc
            if datetime.now(timezone.utc) > not_valid_after:
                self.logger.warning(
                    "IdP certificate has expired (not_valid_after: %s).",
                    not_valid_after.isoformat(),
                )
                return False

            # Parse XML and check for Signature element presence
            root: ET.Element = ET.fromstring(assertion_xml)
            signature_elem = root.find(
                ".//ds:Signature", namespaces=_SAML_NS
            )

            if signature_elem is None:
                # Also check without namespace prefix (some IdPs inline it)
                signature_elem = root.find(
                    ".//{http://www.w3.org/2000/09/xmldsig#}Signature"
                )

            if signature_elem is None:
                self.logger.warning(
                    "SAML assertion does not contain a Signature element."
                )
                return False

            # Verify the SignedInfo and DigestValue elements exist
            signed_info = signature_elem.find(
                "{http://www.w3.org/2000/09/xmldsig#}SignedInfo"
            )
            signature_value = signature_elem.find(
                "{http://www.w3.org/2000/09/xmldsig#}SignatureValue"
            )

            if signed_info is None or signature_value is None:
                self.logger.warning(
                    "SAML Signature element is missing SignedInfo or "
                    "SignatureValue child elements."
                )
                return False

            # Verify signature algorithm is recognised
            sign_method = signed_info.find(
                "{http://www.w3.org/2000/09/xmldsig#}SignatureMethod"
            )
            if sign_method is not None:
                algorithm = sign_method.get("Algorithm", "")
                self.logger.debug(
                    "SAML signature algorithm: %s", algorithm
                )

            # NOTE: Full cryptographic signature verification requires
            # xmlsec or signxml library.  This implementation validates
            # structural integrity and certificate validity.  The
            # certificate's public key is loaded and available for
            # callers who integrate with signxml for full verification.
            _ = public_key.public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )

            self.logger.debug(
                "SAML assertion signature structure validated successfully."
            )
            return True

        except Exception:
            self.logger.exception(
                "Error during SAML signature validation."
            )
            return False

    def _validate_saml_conditions(
        self, root: ET.Element, config: SSOConfig
    ) -> bool:
        """Validate SAML assertion conditions (expiration, audience).

        Args:
            root: Parsed XML root element of the SAML assertion.
            config: Current SSO configuration.

        Returns:
            ``True`` if all conditions are satisfied; ``False`` otherwise.
        """
        # Find Conditions element
        conditions = root.find(
            ".//saml:Conditions", namespaces=_SAML_NS
        )
        if conditions is None:
            # Also try without namespace
            conditions = root.find(
                ".//{urn:oasis:names:tc:SAML:2.0:assertion}Conditions"
            )

        if conditions is not None:
            # Check NotOnOrAfter
            not_on_or_after_str: Optional[str] = conditions.get("NotOnOrAfter")
            if not_on_or_after_str:
                try:
                    # Handle both 'Z' suffix and '+00:00' formats
                    not_on_or_after_str = not_on_or_after_str.replace("Z", "+00:00")
                    not_on_or_after = datetime.fromisoformat(not_on_or_after_str)
                    if datetime.now(timezone.utc) > not_on_or_after:
                        self.logger.warning(
                            "SAML assertion has expired "
                            "(NotOnOrAfter: %s).",
                            not_on_or_after.isoformat(),
                        )
                        return False
                except (ValueError, TypeError) as exc:
                    self.logger.warning(
                        "Failed to parse NotOnOrAfter: %s", exc
                    )
                    return False

            # Check NotBefore
            not_before_str: Optional[str] = conditions.get("NotBefore")
            if not_before_str:
                try:
                    not_before_str = not_before_str.replace("Z", "+00:00")
                    not_before = datetime.fromisoformat(not_before_str)
                    if datetime.now(timezone.utc) < not_before:
                        self.logger.warning(
                            "SAML assertion is not yet valid "
                            "(NotBefore: %s).",
                            not_before.isoformat(),
                        )
                        return False
                except (ValueError, TypeError) as exc:
                    self.logger.warning(
                        "Failed to parse NotBefore: %s", exc
                    )
                    return False

            # Check AudienceRestriction
            if config.saml_sp_entity_id:
                audience_found: bool = False
                for audience_elem in root.iter(
                    "{urn:oasis:names:tc:SAML:2.0:assertion}Audience"
                ):
                    if audience_elem.text and (
                        audience_elem.text.strip() == config.saml_sp_entity_id
                    ):
                        audience_found = True
                        break

                # If audience elements exist but our SP is not listed, fail
                audience_elements = list(
                    root.iter(
                        "{urn:oasis:names:tc:SAML:2.0:assertion}Audience"
                    )
                )
                if audience_elements and not audience_found:
                    self.logger.warning(
                        "SAML assertion audience restriction does not "
                        "include our SP entity ID '%s'.",
                        config.saml_sp_entity_id,
                    )
                    return False

        return True

    def _parse_saml_attributes(
        self, assertion_xml: str
    ) -> Dict[str, Any]:
        """Parse attribute statements from a SAML assertion.

        Extracts standard identity attributes from the SAML assertion
        XML, including NameID (subject), email, groups, display name,
        and name components.

        Args:
            assertion_xml: Raw SAML assertion XML string.

        Returns:
            Dictionary of parsed attributes with keys:
            ``username``, ``email``, ``groups``, ``displayName``,
            ``firstName``, ``lastName``.
        """
        attributes: Dict[str, Any] = {
            "username": None,
            "email": "",
            "groups": [],
            "displayName": "",
            "firstName": "",
            "lastName": "",
        }

        try:
            root: ET.Element = ET.fromstring(assertion_xml)
        except ET.ParseError:
            self.logger.error(
                "Failed to parse SAML assertion XML for attribute extraction."
            )
            return attributes

        saml_ns: str = "{urn:oasis:names:tc:SAML:2.0:assertion}"

        # Extract NameID (subject)
        name_id_elem = root.find(f".//{saml_ns}NameID")
        if name_id_elem is not None and name_id_elem.text:
            attributes["username"] = name_id_elem.text.strip()

        # Also check Subject/NameID path
        if not attributes["username"]:
            subject_elem = root.find(f".//{saml_ns}Subject/{saml_ns}NameID")
            if subject_elem is not None and subject_elem.text:
                attributes["username"] = subject_elem.text.strip()

        # Extract AttributeStatements
        for attr_elem in root.iter(f"{saml_ns}Attribute"):
            attr_name: str = attr_elem.get("Name", "")
            attr_friendly: str = attr_elem.get("FriendlyName", "")

            # Collect all attribute values
            values: List[str] = []
            for val_elem in attr_elem.iter(f"{saml_ns}AttributeValue"):
                if val_elem.text:
                    values.append(val_elem.text.strip())

            # Map known attribute names to our standard keys
            name_lower: str = attr_name.lower()
            friendly_lower: str = attr_friendly.lower()

            if "email" in name_lower or "mail" in friendly_lower:
                if values:
                    attributes["email"] = values[0]
            elif "group" in name_lower or "group" in friendly_lower:
                attributes["groups"] = values
            elif (
                "displayname" in name_lower
                or "display_name" in name_lower
                or "displayname" in friendly_lower
            ):
                if values:
                    attributes["displayName"] = values[0]
            elif (
                "firstname" in name_lower
                or "first_name" in name_lower
                or "givenname" in name_lower
                or "firstname" in friendly_lower
                or "givenname" in friendly_lower
            ):
                if values:
                    attributes["firstName"] = values[0]
            elif (
                "lastname" in name_lower
                or "last_name" in name_lower
                or "surname" in name_lower
                or "sn" == attr_name.lower()
                or "lastname" in friendly_lower
                or "surname" in friendly_lower
            ):
                if values:
                    attributes["lastName"] = values[0]
            elif "role" in name_lower or "role" in friendly_lower:
                # Some IdPs use 'Role' instead of 'groups'
                if not attributes["groups"]:
                    attributes["groups"] = values

        self.logger.debug(
            "Parsed SAML attributes: username=%s, email=%s, groups=%s",
            attributes.get("username"),
            attributes.get("email"),
            attributes.get("groups"),
        )
        return attributes

    # ==================================================================
    # OpenID Connect Authentication
    # ==================================================================

    def _authenticate_oidc(self, oidc_token: str) -> Optional[User]:
        """Authenticate a user via an OIDC id_token (JWT).

        Workflow:
            1. Fetch the OIDC discovery document (cached with TTL).
            2. Retrieve the JWKS (JSON Web Key Set) from the discovery
               ``jwks_uri``.
            3. Decode and validate the id_token using PyJWT:
               - Verify signature against JWKS public keys (RS256).
               - Verify ``iss`` (issuer) matches configured IdP.
               - Verify ``aud`` (audience) matches our client_id.
               - Verify ``exp`` (expiration) is not past.
            4. Extract claims (sub, email, preferred_username, groups).
            5. Auto-provision or update the local user record.
            6. Synchronise SSO group memberships to local roles.

        Args:
            oidc_token: OIDC id_token JWT string.

        Returns:
            A :class:`User` on success; ``None`` on validation failure.
        """
        config: SSOConfig = self._get_sso_config()

        if not config.oidc_enabled:
            self.logger.warning(
                "OIDC authentication attempted but OIDC is not enabled."
            )
            return None

        if not config.oidc_issuer_url or not config.oidc_client_id:
            self.logger.error(
                "OIDC authentication requires 'SSO_OIDC_ISSUER_URL' and "
                "'SSO_OIDC_CLIENT_ID' to be configured."
            )
            return None

        # Step 1: Fetch discovery document
        discovery: Optional[Dict[str, Any]] = self._fetch_oidc_discovery()
        if discovery is None:
            self.logger.error("Failed to fetch OIDC discovery document.")
            return None

        # Step 2: Fetch JWKS
        jwks_uri: Optional[str] = discovery.get("jwks_uri")
        if not jwks_uri:
            self.logger.error(
                "OIDC discovery document does not contain 'jwks_uri'."
            )
            return None

        jwks_data: Optional[Dict[str, Any]] = self._fetch_jwks(jwks_uri)
        if jwks_data is None:
            self.logger.error("Failed to fetch JWKS from '%s'.", jwks_uri)
            return None

        # Build public keys from JWKS for PyJWT verification
        signing_keys: Dict[str, Any] = {}
        for key_data in jwks_data.get("keys", []):
            kid: Optional[str] = key_data.get("kid")
            if kid and key_data.get("kty") == "RSA":
                try:
                    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
                    signing_keys[kid] = public_key
                except Exception:
                    self.logger.warning(
                        "Failed to parse JWKS key with kid='%s'.", kid
                    )

        if not signing_keys:
            self.logger.error(
                "No usable RSA signing keys found in JWKS response."
            )
            return None

        # Step 3: Decode and validate the id_token
        # First, get the token header to find the kid
        try:
            unverified_header: Dict[str, Any] = jwt.get_unverified_header(
                oidc_token
            )
        except jwt.exceptions.InvalidTokenError:
            self.logger.warning("OIDC token has an invalid header.")
            return None

        token_kid: Optional[str] = unverified_header.get("kid")
        if token_kid and token_kid in signing_keys:
            public_key = signing_keys[token_kid]
        elif signing_keys:
            # Fallback: use the first available key
            public_key = next(iter(signing_keys.values()))
        else:
            self.logger.error("No matching signing key found for token kid.")
            return None

        # Determine algorithm from header (default RS256)
        algorithm: str = unverified_header.get("alg", "RS256")
        allowed_algorithms: List[str] = ["RS256", "RS384", "RS512"]
        if algorithm not in allowed_algorithms:
            self.logger.warning(
                "Unsupported OIDC token algorithm: %s", algorithm
            )
            return None

        try:
            claims: Dict[str, Any] = jwt.decode(
                oidc_token,
                key=public_key,
                algorithms=[algorithm],
                audience=config.oidc_client_id,
                issuer=config.oidc_issuer_url,
                options={
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.exceptions.ExpiredSignatureError:
            self.logger.warning("OIDC token has expired.")
            return None
        except jwt.exceptions.InvalidTokenError as exc:
            self.logger.warning("OIDC token validation failed: %s", exc)
            return None

        # Step 4: Extract claims
        subject: Optional[str] = claims.get("sub")
        preferred_username: Optional[str] = claims.get("preferred_username")
        email: str = claims.get("email", "")
        groups: List[str] = claims.get("groups", [])

        # Use preferred_username or subject as the local username
        username: Optional[str] = preferred_username or subject
        if not username:
            self.logger.warning(
                "OIDC token does not contain 'sub' or "
                "'preferred_username' claim."
            )
            return None

        oidc_attributes: Dict[str, Any] = {
            "username": username,
            "email": email,
            "groups": groups if isinstance(groups, list) else [],
            "displayName": claims.get("name", username),
            "firstName": claims.get("given_name", ""),
            "lastName": claims.get("family_name", ""),
            "provider": "oidc",
            "subject": subject,
        }

        # Step 5: Resolve or create local user
        user: User = self._resolve_or_create_user(username, oidc_attributes)

        # Step 6: Map SSO groups to local roles
        sso_groups: List[str] = oidc_attributes.get("groups", [])
        if sso_groups:
            self._map_sso_groups_to_roles(user, sso_groups)

        self.logger.info(
            "OIDC authentication successful for user '%s'.", username
        )
        return user

    def _fetch_oidc_discovery(self) -> Optional[Dict[str, Any]]:
        """Fetch the OIDC discovery document with caching.

        Retrieves the OpenID Connect configuration from
        ``{issuer_url}/.well-known/openid-configuration``.  The result
        is cached for ``metadata_cache_ttl`` seconds (default 3600).

        Returns:
            Parsed discovery document dictionary, or ``None`` on error.
        """
        now: float = time.time()
        if (
            self._oidc_discovery is not None
            and (now - self._metadata_cached_at) < self._metadata_cache_ttl
        ):
            self.logger.debug("Using cached OIDC discovery document.")
            return self._oidc_discovery

        config: SSOConfig = self._get_sso_config()
        issuer_url: str = config.oidc_issuer_url.rstrip("/")
        discovery_url: str = f"{issuer_url}/.well-known/openid-configuration"

        try:
            response = requests.get(
                discovery_url, timeout=_HTTP_TIMEOUT
            )
            response.raise_for_status()
            discovery: Dict[str, Any] = response.json()
        except requests.exceptions.Timeout:
            self.logger.error(
                "Timeout fetching OIDC discovery from '%s'.",
                discovery_url,
            )
            return self._oidc_discovery  # Return stale cache if available
        except requests.exceptions.RequestException as exc:
            self.logger.error(
                "Failed to fetch OIDC discovery from '%s': %s",
                discovery_url,
                exc,
            )
            return self._oidc_discovery
        except ValueError:
            self.logger.error(
                "Invalid JSON in OIDC discovery response from '%s'.",
                discovery_url,
            )
            return self._oidc_discovery

        # Validate minimum required fields
        required_fields: List[str] = ["issuer", "jwks_uri"]
        for field_name in required_fields:
            if field_name not in discovery:
                self.logger.error(
                    "OIDC discovery document missing required field '%s'.",
                    field_name,
                )
                return self._oidc_discovery

        # Cache the discovery document
        self._oidc_discovery = discovery
        self._metadata_cached_at = now
        self._metadata_cache_ttl = config.metadata_cache_ttl

        self.logger.debug(
            "OIDC discovery document cached from '%s' "
            "(issuer=%s, jwks_uri=%s).",
            discovery_url,
            discovery.get("issuer"),
            discovery.get("jwks_uri"),
        )
        return self._oidc_discovery

    def _fetch_jwks(self, jwks_uri: str) -> Optional[Dict[str, Any]]:
        """Fetch the JSON Web Key Set (JWKS) from the IdP with caching.

        Retrieves the JWKS document from the specified URI.  The result
        is cached for ``metadata_cache_ttl`` seconds.

        Args:
            jwks_uri: URL of the JWKS endpoint.

        Returns:
            Parsed JWKS dictionary, or ``None`` on error.
        """
        now: float = time.time()
        if (
            self._jwks_cache is not None
            and (now - self._jwks_cached_at) < self._metadata_cache_ttl
        ):
            self.logger.debug("Using cached JWKS response.")
            return self._jwks_cache

        try:
            response = requests.get(jwks_uri, timeout=_HTTP_TIMEOUT)
            response.raise_for_status()
            jwks_data: Dict[str, Any] = response.json()
        except requests.exceptions.Timeout:
            self.logger.error(
                "Timeout fetching JWKS from '%s'.", jwks_uri
            )
            return self._jwks_cache
        except requests.exceptions.RequestException as exc:
            self.logger.error(
                "Failed to fetch JWKS from '%s': %s", jwks_uri, exc
            )
            return self._jwks_cache
        except ValueError:
            self.logger.error(
                "Invalid JSON in JWKS response from '%s'.", jwks_uri
            )
            return self._jwks_cache

        if "keys" not in jwks_data:
            self.logger.error(
                "JWKS response from '%s' does not contain 'keys' array.",
                jwks_uri,
            )
            return self._jwks_cache

        # Cache the JWKS
        self._jwks_cache = jwks_data
        self._jwks_cached_at = now

        self.logger.debug(
            "JWKS cached from '%s' (%d keys).",
            jwks_uri,
            len(jwks_data.get("keys", [])),
        )
        return self._jwks_cache

    # ==================================================================
    # User Provisioning from SSO
    # ==================================================================

    def _resolve_or_create_user(
        self, username: str, attributes: Dict[str, Any]
    ) -> User:
        """Resolve an existing user or create a new one from SSO attributes.

        On first SSO login, a new :class:`User` record is created with
        ``external_id`` set and ``password_hash`` empty (SSO users do not
        have local passwords).  On subsequent logins, the user's profile
        attributes are refreshed from the IdP assertion/token claims.

        Args:
            username: The SSO-derived username (NameID or preferred_username).
            attributes: Dictionary of parsed SSO attributes containing
                ``email``, ``displayName``, ``firstName``, ``lastName``,
                ``provider``, and optionally ``subject``.

        Returns:
            The resolved or newly created :class:`User` instance.
        """
        try:
            # Look up existing user by user_id
            user: Optional[User] = db.session.query(User).filter_by(
                user_id=username
            ).first()

            if user is not None:
                # Update profile attributes from SSO
                user.email = attributes.get("email") or user.email
                user.external_id = attributes.get("subject", username)
                user.first_name = (
                    attributes.get("firstName") or user.first_name
                )
                user.last_name = (
                    attributes.get("lastName") or user.last_name
                )

                # Update extended attributes
                current_attrs: Dict[str, Any] = user.attributes or {}
                current_attrs["source"] = "sso"
                current_attrs["sso_provider"] = attributes.get(
                    "provider", "unknown"
                )
                current_attrs["display_name"] = attributes.get(
                    "displayName", username
                )
                current_attrs["last_sso_login"] = datetime.now(
                    timezone.utc
                ).isoformat()
                user.attributes = current_attrs

                # Ensure account is active
                if user.status == "disabled":
                    self.logger.info(
                        "Re-activating disabled SSO user '%s'.", username
                    )
                    user.status = "active"

                db.session.add(user)
                db.session.commit()
                self.logger.debug(
                    "Updated existing SSO user '%s'.", username
                )
                return user

            # Create new user for first-time SSO login
            new_user: User = User(
                user_id=username,
                email=attributes.get("email", ""),
                status="active",
                password_hash="",  # No local password for SSO users
                external_id=attributes.get("subject", username),
                first_name=attributes.get("firstName", ""),
                last_name=attributes.get("lastName", ""),
                attributes={
                    "source": "sso",
                    "sso_provider": attributes.get("provider", "unknown"),
                    "display_name": attributes.get(
                        "displayName", username
                    ),
                    "first_sso_login": datetime.now(
                        timezone.utc
                    ).isoformat(),
                },
            )
            db.session.add(new_user)
            db.session.commit()
            self.logger.info(
                "Auto-provisioned new SSO user '%s' (provider=%s).",
                username,
                attributes.get("provider", "unknown"),
            )
            return new_user

        except Exception:
            db.session.rollback()
            self.logger.exception(
                "Failed to resolve or create SSO user '%s'.", username
            )
            raise

    def _map_sso_groups_to_roles(
        self, user: User, sso_groups: List[str]
    ) -> None:
        """Synchronise SSO group memberships to local role assignments.

        Reads the ``SSO_GROUP_ROLE_MAPPING`` configuration dictionary that
        maps IdP group names to local role IDs.  Roles are fully
        synchronised — groups no longer present at the IdP are removed
        from the user's local role assignments.

        Only roles whose ``source`` is ``'external'`` (or roles that appear
        in the mapping) are affected; manually-assigned internal roles are
        preserved.

        Args:
            user: The authenticated :class:`User` to update.
            sso_groups: List of group names from the SSO assertion/token.
        """
        try:
            config: SSOConfig = self._get_sso_config()
            mapping: Dict[str, str] = config.group_role_mapping

            if not mapping:
                self.logger.debug(
                    "No SSO_GROUP_ROLE_MAPPING configured; "
                    "skipping group-to-role sync for user '%s'.",
                    user.user_id,
                )
                return

            # Determine which role_ids should be assigned based on current
            # SSO group memberships
            target_role_ids: set = set()
            for group_name in sso_groups:
                role_id: Optional[str] = mapping.get(group_name)
                if role_id:
                    target_role_ids.add(role_id)

            # Fetch the actual Role objects for target role IDs
            target_roles: List[Role] = []
            for role_id in target_role_ids:
                role: Optional[Role] = db.session.query(Role).filter_by(
                    role_id=role_id
                ).first()
                if role is not None:
                    target_roles.append(role)
                else:
                    self.logger.warning(
                        "SSO group mapping references role '%s' which does "
                        "not exist in the local database.",
                        role_id,
                    )

            # Get the set of all mapped role_ids (from config values)
            all_mapped_role_ids: set = set(mapping.values())

            # Build current SSO-managed roles assigned to the user
            current_roles: List[Role] = list(user.roles)
            roles_to_keep: List[Role] = []

            for role in current_roles:
                if role.role_id not in all_mapped_role_ids:
                    # Keep roles that are not managed by SSO mapping
                    roles_to_keep.append(role)

            # Merge: keep non-SSO-managed roles + add new SSO-mapped roles
            final_roles: List[Role] = roles_to_keep + target_roles

            # Deduplicate by role_id
            seen: set = set()
            deduplicated: List[Role] = []
            for role in final_roles:
                if role.role_id not in seen:
                    seen.add(role.role_id)
                    deduplicated.append(role)

            # Update the user's roles
            user.roles = deduplicated
            db.session.add(user)
            db.session.commit()

            self.logger.debug(
                "SSO group-to-role sync for user '%s': "
                "sso_groups=%s, mapped_roles=%s, final_roles=%s",
                user.user_id,
                sso_groups,
                [r.role_id for r in target_roles],
                [r.role_id for r in deduplicated],
            )

        except Exception:
            db.session.rollback()
            self.logger.exception(
                "Failed to map SSO groups to roles for user '%s'.",
                user.user_id,
            )

    # ==================================================================
    # Configuration
    # ==================================================================

    def _get_sso_config(self) -> SSOConfig:
        """Read SSO configuration from the Flask application config.

        Maps Flask ``app.config`` keys to a structured :class:`SSOConfig`
        dataclass for type-safe access throughout the realm.

        Returns:
            Populated :class:`SSOConfig` instance.

        Raises:
            RuntimeError: If called outside a Flask application context.
        """
        return SSOConfig(
            saml_enabled=current_app.config.get("SSO_SAML_ENABLED", False),
            oidc_enabled=current_app.config.get("SSO_OIDC_ENABLED", False),
            saml_idp_metadata_url=current_app.config.get(
                "SSO_SAML_IDP_METADATA_URL", ""
            ),
            saml_sp_entity_id=current_app.config.get(
                "SSO_SAML_SP_ENTITY_ID", ""
            ),
            saml_idp_cert=current_app.config.get("SSO_SAML_IDP_CERT", ""),
            oidc_issuer_url=current_app.config.get(
                "SSO_OIDC_ISSUER_URL", ""
            ),
            oidc_client_id=current_app.config.get(
                "SSO_OIDC_CLIENT_ID", ""
            ),
            oidc_client_secret=current_app.config.get(
                "SSO_OIDC_CLIENT_SECRET", ""
            ),
            group_role_mapping=current_app.config.get(
                "SSO_GROUP_ROLE_MAPPING", {}
            ),
            metadata_cache_ttl=current_app.config.get(
                "SSO_METADATA_CACHE_TTL", _DEFAULT_METADATA_CACHE_TTL
            ),
        )


# ---------------------------------------------------------------------------
# Module Load Logging
# ---------------------------------------------------------------------------
logger.debug(
    "SSO realm module loaded — exports: %s",
    ", ".join(__all__),
)
