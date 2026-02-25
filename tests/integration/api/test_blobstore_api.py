"""Full HTTP lifecycle integration tests for BlobStore configuration and
management REST API endpoints.

POST/GET/PUT/DELETE on /api/v1/blobstores for File and S3 BlobStore types.
AAP §0.5.1, §0.5.2, §0.4.2, §0.7.1, §0.10.1.  Features F-201, F-202.
"""
import json
from datetime import datetime, timezone as _tz
from unittest.mock import patch, MagicMock

import pytest

from tests.fixtures.config_data import (
    make_blobstore_config,
    make_config_with_s3_storage,
    make_config_with_file_storage,
)
from tests.mocks.mock_s3_client import MockS3Client

# ---------------------------------------------------------------------------
# Import real source modules (no shim fallback).
# If source modules are not yet created, all tests in this file are skipped.
# ---------------------------------------------------------------------------
pytest.importorskip(
    "src.api.blobstore_routes",
    reason="BlobStore routes module not yet created",
)
pytest.importorskip(
    "src.models.blobstore",
    reason="BlobStore model module not yet created",
)

pytestmark = pytest.mark.integration
BASE = "/api/v1/blobstores"


# ── Helpers ─────────────────────────────────────────────────────────────
def _post(client, auth_headers, payload):
    """POST helper to create a blobstore and return the raw response."""
    return client.post(BASE, data=json.dumps(payload),
                       headers=auth_headers,
                       content_type="application/json")


# =========================================================================
# CRUD Happy Path
# =========================================================================
class TestBlobStoreCRUD:
    """CRUD operations for File and S3 BlobStore configurations."""

    def test_create_file_blobstore_returns_201(self, client, auth_headers):
        payload = make_blobstore_config(store_type="file", name="test-file-store")
        response = _post(client, auth_headers, payload)
        assert response.status_code == 201
        data = response.get_json()
        assert data is not None
        assert data["name"] == "test-file-store"
        assert data["type"] == "file"

    def test_create_s3_blobstore_returns_201(self, client, auth_headers):
        payload = make_blobstore_config(store_type="s3", name="test-s3-store")
        response = _post(client, auth_headers, payload)
        assert response.status_code == 201
        data = response.get_json()
        assert data is not None
        assert data["name"] == "test-s3-store"
        assert data["type"] == "s3"
        assert "bucket" in data

    def test_list_blobstores_returns_200(self, client, auth_headers, db_session):
        _post(client, auth_headers,
              make_blobstore_config(store_type="file", name="list-file"))
        _post(client, auth_headers,
              make_blobstore_config(store_type="s3", name="list-s3"))
        response = client.get(BASE, headers=auth_headers)
        assert response.status_code == 200
        data = response.get_json()
        assert data is not None
        items = data if isinstance(data, list) else data.get("items", [])
        assert len(items) >= 2

    def test_get_blobstore_by_name_returns_200(self, client, auth_headers,
                                               db_session):
        _post(client, auth_headers,
              make_blobstore_config(store_type="file", name="get-store"))
        response = client.get(f"{BASE}/get-store", headers=auth_headers)
        assert response.status_code == 200
        data = response.get_json()
        assert data is not None
        assert data["name"] == "get-store"
        assert data["type"] == "file"

    def test_update_blobstore_config_returns_200(self, client, auth_headers,
                                                 db_session):
        _post(client, auth_headers,
              make_blobstore_config(store_type="file", name="upd-store",
                                   path="/tmp/orig"))
        updated = make_blobstore_config(store_type="file", name="upd-store",
                                        path="/tmp/updated")
        response = client.put(
            f"{BASE}/upd-store", data=json.dumps(updated),
            headers=auth_headers, content_type="application/json")
        assert response.status_code == 200
        data = response.get_json()
        assert data is not None
        assert data["path"] == "/tmp/updated"

    def test_delete_blobstore_returns_204(self, client, auth_headers,
                                         db_session):
        _post(client, auth_headers,
              make_blobstore_config(store_type="file", name="del-store"))
        response = client.delete(f"{BASE}/del-store", headers=auth_headers)
        assert response.status_code == 204
        verify = client.get(f"{BASE}/del-store", headers=auth_headers)
        assert verify.status_code == 404


# =========================================================================
# Type-Specific Tests
# =========================================================================
class TestBlobStoreTypeSpecific:
    """Type-specific configuration for File and S3 stores."""

    def test_create_file_blobstore_with_custom_path(self, client,
                                                    auth_headers,
                                                    tmp_storage):
        custom = str(tmp_storage / "custom-data")
        payload = make_blobstore_config(store_type="file",
                                        name="custom-path-store",
                                        path=custom)
        response = _post(client, auth_headers, payload)
        assert response.status_code == 201
        data = response.get_json()
        assert data is not None
        assert data["path"] == custom

    def test_create_s3_blobstore_with_prefix(self, client, auth_headers):
        payload = make_blobstore_config(store_type="s3",
                                        name="prefixed-s3",
                                        prefix="artifacts/v2/")
        response = _post(client, auth_headers, payload)
        assert response.status_code == 201
        data = response.get_json()
        assert data is not None
        assert data.get("prefix") == "artifacts/v2/"

    def test_list_blobstores_filtered_by_type(self, client, auth_headers,
                                              db_session):
        _post(client, auth_headers,
              make_blobstore_config(store_type="file", name="filter-file"))
        _post(client, auth_headers,
              make_blobstore_config(store_type="s3", name="filter-s3"))
        response = client.get(f"{BASE}?type=file", headers=auth_headers)
        assert response.status_code == 200
        data = response.get_json()
        items = data if isinstance(data, list) else data.get("items", [])
        for item in items:
            assert item["type"] == "file"

    def test_get_blobstore_quota_info(self, client, auth_headers, db_session):
        _post(client, auth_headers,
              make_blobstore_config(store_type="file", name="quota-store"))
        response = client.get(f"{BASE}/quota-store/status",
                              headers=auth_headers)
        assert response.status_code == 200
        data = response.get_json()
        assert data is not None
        assert ("total_size" in data or "totalSize" in data
                or "object_count" in data or "objectCount" in data)


# =========================================================================
# Edge Case Tests
# =========================================================================
class TestBlobStoreEdgeCases:
    """Edge case tests for BlobStore management."""

    def test_create_blobstore_with_duplicate_name_returns_409(
            self, client, auth_headers, db_session):
        payload = make_blobstore_config(store_type="file", name="default")
        _post(client, auth_headers, payload)
        dup = make_blobstore_config(store_type="file", name="default")
        response = _post(client, auth_headers, dup)
        assert response.status_code == 409
        data = response.get_json()
        assert data is not None
        assert "error" in data or "message" in data

    def test_get_nonexistent_blobstore_returns_404(self, client,
                                                   auth_headers):
        response = client.get(f"{BASE}/nonexistent-store",
                              headers=auth_headers)
        assert response.status_code == 404
        data = response.get_json()
        assert data is not None
        assert "error" in data or "message" in data

    def test_delete_blobstore_in_use_returns_409(self, client, auth_headers,
                                                 db_session):
        _post(client, auth_headers,
              make_blobstore_config(store_type="file", name="in-use-store"))
        with patch(
            "src.services.storage_service.StorageService.is_blobstore_in_use",
            return_value=True,
        ):
            response = client.delete(f"{BASE}/in-use-store",
                                     headers=auth_headers)
        assert response.status_code == 409
        data = response.get_json()
        assert data is not None
        assert "error" in data or "message" in data

    def test_create_blobstore_with_invalid_type_returns_400(
            self, client, auth_headers):
        payload = {"name": "bad-type", "type": "invalid", "path": "/tmp/bad"}
        response = _post(client, auth_headers, payload)
        assert response.status_code == 400
        data = response.get_json()
        assert data is not None
        assert "error" in data or "message" in data

    def test_create_s3_blobstore_missing_bucket_returns_400(
            self, client, auth_headers):
        payload = {"name": "no-bucket-s3", "type": "s3",
                   "region": "us-east-1",
                   "endpoint_url": "http://localhost:4566"}
        response = _post(client, auth_headers, payload)
        assert response.status_code == 400
        data = response.get_json()
        assert data is not None
        assert "error" in data or "message" in data

    def test_update_blobstore_type_not_allowed_returns_400(
            self, client, auth_headers, db_session):
        _post(client, auth_headers,
              make_blobstore_config(store_type="file", name="immutable-type"))
        change = make_blobstore_config(store_type="s3", name="immutable-type")
        response = client.put(
            f"{BASE}/immutable-type", data=json.dumps(change),
            headers=auth_headers, content_type="application/json")
        assert response.status_code == 400
        data = response.get_json()
        assert data is not None
        assert "error" in data or "message" in data


# =========================================================================
# Security and Authorization Tests
# =========================================================================
class TestBlobStoreSecurity:
    """Security and authorization tests for BlobStore endpoints."""

    def test_create_blobstore_without_auth_returns_401(self, client):
        payload = make_blobstore_config(store_type="file", name="unauth")
        response = client.post(BASE, data=json.dumps(payload),
                               content_type="application/json")
        assert response.status_code == 401
        data = response.get_json()
        assert data is not None

    def test_create_blobstore_with_readonly_user_returns_403(
            self, client, readonly_auth_headers):
        payload = make_blobstore_config(store_type="file", name="ro-store")
        response = client.post(BASE, data=json.dumps(payload),
                               headers=readonly_auth_headers,
                               content_type="application/json")
        assert response.status_code == 403
        data = response.get_json()
        assert data is not None
        assert "error" in data or "message" in data

    def test_delete_blobstore_requires_admin(self, client, auth_headers,
                                             developer_auth_headers,
                                             db_session):
        _post(client, auth_headers,
              make_blobstore_config(store_type="file", name="dev-del"))
        response = client.delete(f"{BASE}/dev-del",
                                 headers=developer_auth_headers)
        assert response.status_code == 403
        data = response.get_json()
        assert data is not None

    def test_list_blobstores_with_api_key_returns_200(
            self, client, auth_headers, api_key_headers, db_session):
        _post(client, auth_headers,
              make_blobstore_config(store_type="file", name="apikey-store"))
        response = client.get(BASE, headers=api_key_headers)
        assert response.status_code in (200, 403)
        data = response.get_json()
        assert data is not None


# =========================================================================
# Error Handling Tests
# =========================================================================
class TestBlobStoreErrorHandling:
    """Error handling tests for malformed requests and missing resources."""

    def test_create_blobstore_with_invalid_json_returns_400(
            self, client, auth_headers):
        response = client.post(BASE, data="not-valid-json{{{",
                               headers=auth_headers,
                               content_type="application/json")
        assert response.status_code == 400
        data = response.get_json()
        assert data is not None

    def test_create_blobstore_missing_name_returns_400(self, client,
                                                       auth_headers):
        payload = {"type": "file", "path": "/tmp/no-name"}
        response = _post(client, auth_headers, payload)
        assert response.status_code == 400
        data = response.get_json()
        assert data is not None
        assert "error" in data or "message" in data

    def test_update_nonexistent_blobstore_returns_404(self, client,
                                                      auth_headers):
        payload = make_blobstore_config(store_type="file",
                                        name="nonexistent",
                                        path="/tmp/nowhere")
        response = client.put(
            f"{BASE}/nonexistent", data=json.dumps(payload),
            headers=auth_headers, content_type="application/json")
        assert response.status_code == 404
        data = response.get_json()
        assert data is not None
        assert "error" in data or "message" in data
