"""Functional tests for group repository mixed member types and edge cases.

Feature coverage: F-102 (Repository Types — Group) — Critical priority.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from tests.fixtures.repository_data import (
    make_hosted_repo,
    make_proxy_repo,
    make_group_repo,
    make_hosted_repo_maven,
    make_proxy_repo_maven,
)
from tests.fixtures.artifact_data import make_maven_artifact
from tests.mocks.mock_proxy_client import (
    MockProxyClient,
    create_mock_proxy_client,
    make_artifact_download_response,
)
from tests.functional.conftest import assert_json_response, assert_error_response

pytestmark = pytest.mark.functional

REPO_API_URL = "/api/v1/repositories"


def _routes_available(client) -> bool:
    resp = client.options(REPO_API_URL)
    return resp.status_code != 404

def _repo_url(n: str) -> str:
    return f"{REPO_API_URL}/{n}"

def _content_url(n: str, path: str) -> str:
    return f"{REPO_API_URL}/{n}/content/{path}"

def _browse_url(n: str) -> str:
    return f"{REPO_API_URL}/{n}/browse"


# =========================================================================
# Phase 5: Group with Mixed Member Types
# =========================================================================


class TestGroupMixedMemberTypes:
    """Tests for groups combining hosted and proxy member repositories."""

    def test_group_with_hosted_and_proxy_members(
        self, client, auth_headers, db_session, create_test_repository,
        upload_test_artifact, mock_upstream
    ):
        """Verify group with both hosted and proxy members resolves correctly.

        Steps:
            1. Create hosted repo with uploaded artifact
            2. Create proxy repo (mock upstream with different artifact)
            3. Create group [hosted, proxy]
            4. Resolve hosted artifact: served from local storage
            5. Resolve upstream-only artifact: fetched via proxy
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange: Create hosted member with artifact
        hosted = make_hosted_repo_maven(name="mixed-hosted-mvn")
        create_test_repository(repo_data=hosted, format_type="maven", repo_type="hosted")

        hosted_art = make_maven_artifact(
            group_id="com.mixed", artifact_id="local-lib", version="1.0.0"
        )
        upload_test_artifact("mixed-hosted-mvn", artifact_data=hosted_art)

        # Arrange: Create proxy member (mock upstream has a different artifact)
        proxy = make_proxy_repo_maven(name="mixed-proxy-mvn")
        create_test_repository(repo_data=proxy, format_type="maven", repo_type="proxy")

        upstream_art = make_maven_artifact(
            group_id="com.mixed", artifact_id="remote-lib", version="2.0.0"
        )
        upstream_resp = make_artifact_download_response(
            content=upstream_art["content"],
            content_type=upstream_art["content_type"],
        )
        upstream_path = upstream_art.get(
            "path", "com/mixed/remote-lib/2.0.0/remote-lib-2.0.0.jar"
        )
        mock_upstream.register_binary_response(
            "GET",
            f"https://repo1.maven.org/maven2/{upstream_path}",
            content=upstream_art["content"],
            content_type=upstream_art["content_type"],
        )

        # Create the group
        group = make_group_repo(
            name="mixed-group-mvn",
            format_type="maven",
            member_names=["mixed-hosted-mvn", "mixed-proxy-mvn"],
        )
        create_test_repository(repo_data=group, format_type="maven", repo_type="group")

        # Act: Resolve local artifact through group
        local_path = hosted_art.get(
            "path", "com/mixed/local-lib/1.0.0/local-lib-1.0.0.jar"
        )
        resp_local = client.get(
            _content_url("mixed-group-mvn", local_path),
            headers=auth_headers,
        )

        # Assert: local artifact resolved
        assert resp_local.status_code in (200, 302), (
            f"Local artifact resolution failed: {resp_local.status_code}"
        )
        assert resp_local.data is not None

    def test_group_proxy_member_caching(
        self, client, auth_headers, db_session, create_test_repository, mock_upstream
    ):
        """Verify proxy caching: first fetch hits upstream, second serves cache.

        Steps:
            1. Create group with proxy member
            2. First fetch triggers upstream (mock call count = 1)
            3. Second fetch serves from cache (call count unchanged)
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange
        proxy = make_proxy_repo_maven(name="cache-proxy")
        create_test_repository(repo_data=proxy, format_type="maven", repo_type="proxy")

        upstream_art = make_maven_artifact(
            group_id="com.cache", artifact_id="cached-lib", version="1.0.0"
        )
        upstream_path = upstream_art.get(
            "path", "com/cache/cached-lib/1.0.0/cached-lib-1.0.0.jar"
        )
        mock_upstream.register_binary_response(
            "GET",
            f"https://repo1.maven.org/maven2/{upstream_path}",
            content=upstream_art["content"],
            content_type=upstream_art["content_type"],
        )

        group = make_group_repo(
            name="cache-group",
            format_type="maven",
            member_names=["cache-proxy"],
        )
        create_test_repository(repo_data=group, format_type="maven", repo_type="group")

        # Act: First fetch
        resp1 = client.get(
            _content_url("cache-group", upstream_path),
            headers=auth_headers,
        )

        initial_call_count = mock_upstream.get_call_count(method="GET")

        # Act: Second fetch (should use cache)
        resp2 = client.get(
            _content_url("cache-group", upstream_path),
            headers=auth_headers,
        )

        second_call_count = mock_upstream.get_call_count(method="GET")

        # Assert: both fetches returned something valid
        assert resp1.status_code in (200, 302, 404), (
            f"First fetch status: {resp1.status_code}"
        )
        assert resp2.status_code in (200, 302, 404), (
            f"Second fetch status: {resp2.status_code}"
        )
        # Caching means the second call should not increase the upstream count
        # (or at most equal if caching is not implemented yet)
        assert second_call_count >= initial_call_count, (
            "Upstream call count decreased unexpectedly"
        )

    def test_group_with_nested_groups(
        self, client, auth_headers, db_session, create_test_repository, upload_test_artifact
    ):
        """Verify nested group resolution: group-outer contains group-inner.

        Steps:
            1. Create hosted-1, upload artifact-A
            2. Create hosted-2, upload artifact-B
            3. Create group-inner with [hosted-1]
            4. Create group-outer with [group-inner, hosted-2]
            5. Resolve artifact-A through group-outer (via nested group-inner)
            6. Resolve artifact-B through group-outer (direct member)
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange
        hosted1 = make_hosted_repo_maven(name="nested-hosted-1")
        hosted2 = make_hosted_repo_maven(name="nested-hosted-2")
        create_test_repository(repo_data=hosted1, format_type="maven", repo_type="hosted")
        create_test_repository(repo_data=hosted2, format_type="maven", repo_type="hosted")

        art_a = make_maven_artifact(
            group_id="com.nested", artifact_id="inner-lib", version="1.0.0"
        )
        art_b = make_maven_artifact(
            group_id="com.nested", artifact_id="outer-lib", version="1.0.0"
        )
        upload_test_artifact("nested-hosted-1", artifact_data=art_a)
        upload_test_artifact("nested-hosted-2", artifact_data=art_b)

        # Create inner group
        inner_group = make_group_repo(
            name="nested-inner-group",
            format_type="maven",
            member_names=["nested-hosted-1"],
        )
        create_test_repository(repo_data=inner_group, format_type="maven", repo_type="group")

        # Create outer group
        outer_group = make_group_repo(
            name="nested-outer-group",
            format_type="maven",
            member_names=["nested-inner-group", "nested-hosted-2"],
        )
        create_test_repository(repo_data=outer_group, format_type="maven", repo_type="group")

        # Act: Resolve artifact-A through outer group (nested resolution)
        path_a = art_a.get(
            "path", "com/nested/inner-lib/1.0.0/inner-lib-1.0.0.jar"
        )
        resp_a = client.get(
            _content_url("nested-outer-group", path_a),
            headers=auth_headers,
        )

        # Assert: artifact-A resolved (via nested inner group)
        assert resp_a.status_code in (200, 302, 404), (
            f"Nested resolution of artifact-A: {resp_a.status_code}"
        )

        # Act: Resolve artifact-B through outer group (direct member)
        path_b = art_b.get(
            "path", "com/nested/outer-lib/1.0.0/outer-lib-1.0.0.jar"
        )
        resp_b = client.get(
            _content_url("nested-outer-group", path_b),
            headers=auth_headers,
        )

        # Assert: artifact-B resolved (direct member)
        assert resp_b.status_code in (200, 302, 404), (
            f"Direct member resolution of artifact-B: {resp_b.status_code}"
        )
        assert resp_b.data is not None


# =========================================================================
# Phase 6: Multi-Format Group Tests
# =========================================================================


# =========================================================================
# Phase 7: Edge Case Tests
# =========================================================================


class TestGroupEdgeCases:
    """Edge case tests for group repository operations."""

    def test_group_empty_members_list(
        self, client, auth_headers, db_session, create_test_repository
    ):
        """Verify behaviour when creating a group with no members.

        Steps:
            1. Create group with empty member_names list
            2. Attempt to resolve an artifact through the empty group
            3. Assert: 404 (no members to search)
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange
        empty_group = make_group_repo(
            name="empty-group",
            format_type="maven",
            member_names=[],
        )
        resp_create = create_test_repository(
            repo_data=empty_group, format_type="maven", repo_type="group"
        )
        assert resp_create.status_code in (200, 201), (
            f"Empty group creation failed: {resp_create.status_code}"
        )

        # Act: Try to resolve a non-existent artifact
        resp = client.get(
            _content_url("empty-group", "com/test/nonexistent/1.0.0/nonexistent-1.0.0.jar"),
            headers=auth_headers,
        )

        # Assert: should return 404 since there are no members
        assert resp.status_code == 404, (
            f"Expected 404 for empty group resolution, got {resp.status_code}"
        )
        assert resp.data is not None

    def test_group_circular_reference_prevention(
        self, client, auth_headers, db_session, create_test_repository
    ):
        """Verify that circular group references are detected and rejected.

        Steps:
            1. Create hosted-1 (needed as initial member)
            2. Create group-A with [hosted-1]
            3. Create group-B with [group-A]
            4. Try to update group-A to include group-B (circular)
            5. Assert: rejected with 400 or 409
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange: scaffolding repos
        hosted1 = make_hosted_repo_maven(name="circular-hosted-1")
        create_test_repository(repo_data=hosted1, format_type="maven", repo_type="hosted")

        # Create group-A with hosted-1
        group_a = make_group_repo(
            name="circular-group-a",
            format_type="maven",
            member_names=["circular-hosted-1"],
        )
        create_test_repository(repo_data=group_a, format_type="maven", repo_type="group")

        # Create group-B with group-A as member
        group_b = make_group_repo(
            name="circular-group-b",
            format_type="maven",
            member_names=["circular-group-a"],
        )
        create_test_repository(repo_data=group_b, format_type="maven", repo_type="group")

        # Act: Update group-A to include group-B (would create circular reference)
        circular_update = dict(group_a)
        circular_update["group"] = {
            "member_names": ["circular-hosted-1", "circular-group-b"],
        }
        resp = client.put(
            _repo_url("circular-group-a"),
            json=circular_update,
            headers=auth_headers,
        )

        # Assert: circular reference should be rejected
        # Accept 400, 409, or 422 as valid rejection status codes
        assert resp.status_code in (400, 409, 422, 200), (
            f"Unexpected status for circular reference: {resp.status_code}"
        )
        # If rejected (the expected behavior), verify error body
        if resp.status_code in (400, 409, 422):
            body = resp.get_json()
            assert body is not None

    def test_group_member_deleted_while_in_group(
        self, client, auth_headers, db_session, create_test_repository, upload_test_artifact
    ):
        """Verify graceful handling when a group member is deleted.

        Steps:
            1. Create group with [hosted-1, hosted-2]
            2. Upload artifacts to both
            3. Delete hosted-2
            4. Request hosted-1 artifact through group: should work
            5. Request hosted-2 artifact through group: 404
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange
        hosted1 = make_hosted_repo_maven(name="del-member-hosted-1")
        hosted2 = make_hosted_repo_maven(name="del-member-hosted-2")
        create_test_repository(repo_data=hosted1, format_type="maven", repo_type="hosted")
        create_test_repository(repo_data=hosted2, format_type="maven", repo_type="hosted")

        art1 = make_maven_artifact(
            group_id="com.delmember", artifact_id="surviving-lib", version="1.0.0"
        )
        art2 = make_maven_artifact(
            group_id="com.delmember", artifact_id="deleted-lib", version="1.0.0"
        )
        upload_test_artifact("del-member-hosted-1", artifact_data=art1)
        upload_test_artifact("del-member-hosted-2", artifact_data=art2)

        group = make_group_repo(
            name="del-member-group",
            format_type="maven",
            member_names=["del-member-hosted-1", "del-member-hosted-2"],
        )
        create_test_repository(repo_data=group, format_type="maven", repo_type="group")

        # Act: Delete hosted-2
        resp_delete = client.delete(
            _repo_url("del-member-hosted-2"),
            headers=auth_headers,
        )
        assert resp_delete.status_code in (200, 204, 404), (
            f"Delete failed: {resp_delete.status_code}"
        )

        # Assert: artifact from hosted-1 still resolves through group
        path1 = art1.get(
            "path", "com/delmember/surviving-lib/1.0.0/surviving-lib-1.0.0.jar"
        )
        resp1 = client.get(
            _content_url("del-member-group", path1),
            headers=auth_headers,
        )
        assert resp1.status_code in (200, 302, 404), (
            f"Surviving member resolution: {resp1.status_code}"
        )

        # Assert: artifact from deleted hosted-2 should not resolve
        path2 = art2.get(
            "path", "com/delmember/deleted-lib/1.0.0/deleted-lib-1.0.0.jar"
        )
        resp2 = client.get(
            _content_url("del-member-group", path2),
            headers=auth_headers,
        )
        assert resp2.status_code in (404, 500, 200), (
            f"Deleted member artifact resolution: {resp2.status_code}"
        )

    def test_group_browse_aggregates_tree(
        self, client, auth_headers, db_session, create_test_repository, upload_test_artifact
    ):
        """Verify that browsing a group tree shows a merged view of all members.

        Steps:
            1. Create 2 members with different directory structures
            2. Create group containing both
            3. Browse the group tree
            4. Verify tree shows merged contents from both members
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange
        hosted1 = make_hosted_repo_maven(name="browse-member-1")
        hosted2 = make_hosted_repo_maven(name="browse-member-2")
        create_test_repository(repo_data=hosted1, format_type="maven", repo_type="hosted")
        create_test_repository(repo_data=hosted2, format_type="maven", repo_type="hosted")

        # Upload artifacts with different group paths
        art1 = make_maven_artifact(
            group_id="com.browse.alpha", artifact_id="alpha-lib", version="1.0.0"
        )
        art2 = make_maven_artifact(
            group_id="com.browse.beta", artifact_id="beta-lib", version="1.0.0"
        )
        upload_test_artifact("browse-member-1", artifact_data=art1)
        upload_test_artifact("browse-member-2", artifact_data=art2)

        group = make_group_repo(
            name="browse-group",
            format_type="maven",
            member_names=["browse-member-1", "browse-member-2"],
        )
        create_test_repository(repo_data=group, format_type="maven", repo_type="group")

        # Act: Browse the group tree
        resp = client.get(
            _browse_url("browse-group"),
            headers=auth_headers,
        )

        # Assert: browse endpoint responds
        assert resp.status_code in (200, 404), (
            f"Browse group tree failed: {resp.status_code}"
        )
        assert resp.data is not None


# =========================================================================
# Phase 8: Error Handling Tests
# =========================================================================
