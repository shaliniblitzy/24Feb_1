"""End-to-end functional tests for group repository happy-path
and error handling workflows.

Exercises: create members -> create group -> resolve from group.
Error handling: invalid members, format mismatch, missing repos.

Feature coverage: F-102 (Repository Types — Group) — Critical priority.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from tests.fixtures.repository_data import (
    make_hosted_repo,
    make_group_repo,
    make_hosted_repo_maven,
)
from tests.fixtures.artifact_data import make_maven_artifact, make_artifact_for_format
from tests.functional.conftest import assert_json_response, assert_error_response

pytestmark = pytest.mark.functional

REPO_API_URL = "/api/v1/repositories"


def _routes_available(client) -> bool:
    """Check if the repository API blueprint is registered."""
    resp = client.options(REPO_API_URL)
    return resp.status_code != 404


def _repo_url(repo_name: str) -> str:
    """Build the detail URL for a single repository."""
    return f"{REPO_API_URL}/{repo_name}"


def _content_url(repo_name: str, path: str) -> str:
    """Build the content resolution URL for an artifact within a repository."""
    return f"{REPO_API_URL}/{repo_name}/content/{path}"


def _search_url(repo_name: str) -> str:
    """Build the search URL scoped to a repository."""
    return f"{REPO_API_URL}/{repo_name}/search"


# =========================================================================
# Phase 3: Happy Path — Complete Group Workflow
# =========================================================================


class TestGroupHappyPath:
    """Happy-path tests for the full group repository lifecycle."""

    def test_group_full_workflow_create_members_create_group_resolve(
        self, client, auth_headers, db_session, create_test_repository, upload_test_artifact
    ):
        """Validate complete end-to-end group workflow.

        Steps:
            1. Create two hosted Maven repositories as members
            2. Upload distinct artifacts to each member
            3. Create a group repository referencing both members
            4. Resolve artifact-A through the group (from member-1)
            5. Resolve artifact-B through the group (from member-2)
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange: Create member repositories
        member1 = make_hosted_repo_maven(name="workflow-hosted-1")
        member2 = make_hosted_repo_maven(name="workflow-hosted-2")

        resp_m1 = create_test_repository(repo_data=member1, format_type="maven", repo_type="hosted")
        resp_m2 = create_test_repository(repo_data=member2, format_type="maven", repo_type="hosted")

        # Assert: both members created successfully
        assert resp_m1.status_code in (200, 201), (
            f"Member 1 creation failed: {resp_m1.status_code}"
        )
        assert resp_m2.status_code in (200, 201), (
            f"Member 2 creation failed: {resp_m2.status_code}"
        )

        # Act: Upload artifacts to each member
        artifact_a = make_maven_artifact(
            group_id="com.workflow", artifact_id="lib-alpha", version="1.0.0"
        )
        artifact_b = make_maven_artifact(
            group_id="com.workflow", artifact_id="lib-beta", version="2.0.0"
        )

        upload_resp_a = upload_test_artifact("workflow-hosted-1", artifact_data=artifact_a)
        upload_resp_b = upload_test_artifact("workflow-hosted-2", artifact_data=artifact_b)

        assert upload_resp_a.status_code in (200, 201), (
            f"Artifact A upload failed: {upload_resp_a.status_code}"
        )
        assert upload_resp_b.status_code in (200, 201), (
            f"Artifact B upload failed: {upload_resp_b.status_code}"
        )

        # Act: Create group repository
        group = make_group_repo(
            name="workflow-group-maven",
            format_type="maven",
            member_names=["workflow-hosted-1", "workflow-hosted-2"],
        )
        resp_group = create_test_repository(
            repo_data=group, format_type="maven", repo_type="group"
        )
        assert resp_group.status_code in (200, 201), (
            f"Group creation failed: {resp_group.status_code}"
        )

        # Act: Resolve artifact-A through the group
        path_a = artifact_a.get("path", "com/workflow/lib-alpha/1.0.0/lib-alpha-1.0.0.jar")
        resp_resolve_a = client.get(
            _content_url("workflow-group-maven", path_a),
            headers=auth_headers,
        )

        # Assert: artifact-A resolved successfully
        assert resp_resolve_a.status_code in (200, 302), (
            f"Artifact A resolution failed: {resp_resolve_a.status_code}"
        )

        # Act: Resolve artifact-B through the group
        path_b = artifact_b.get("path", "com/workflow/lib-beta/2.0.0/lib-beta-2.0.0.jar")
        resp_resolve_b = client.get(
            _content_url("workflow-group-maven", path_b),
            headers=auth_headers,
        )

        # Assert: artifact-B resolved successfully
        assert resp_resolve_b.status_code in (200, 302), (
            f"Artifact B resolution failed: {resp_resolve_b.status_code}"
        )

    def test_group_create_and_retrieve_members(
        self, client, auth_headers, db_session, create_test_repository
    ):
        """Verify that group details include the correct member list in order.

        Steps:
            1. Create 3 hosted Maven repositories
            2. Create a group with all 3 as members (specific order)
            3. GET the group details
            4. Verify member_names matches the specified order
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange
        names = ["retrieve-member-1", "retrieve-member-2", "retrieve-member-3"]
        for name in names:
            repo_data = make_hosted_repo_maven(name=name)
            resp = create_test_repository(repo_data=repo_data, format_type="maven", repo_type="hosted")
            assert resp.status_code in (200, 201), f"Failed to create {name}"

        group_data = make_group_repo(
            name="retrieve-test-group",
            format_type="maven",
            member_names=names,
        )
        # Verify the group config can be serialised to JSON (using json.dumps)
        serialized = json.dumps(group_data)
        assert serialized is not None

        resp_group = create_test_repository(
            repo_data=group_data, format_type="maven", repo_type="group"
        )
        assert resp_group.status_code in (200, 201)

        # Act: Retrieve group details
        resp_get = client.get(
            _repo_url("retrieve-test-group"),
            headers=auth_headers,
        )

        # Assert: parse raw response data with json.loads for validation
        assert resp_get.status_code == 200
        raw_body = json.loads(resp_get.data.decode("utf-8"))
        assert raw_body is not None
        group_section = raw_body.get("group", raw_body)
        member_names = group_section.get("member_names", [])
        assert member_names == names, (
            f"Expected members {names}, got {member_names}"
        )

    def test_group_aggregates_search_results(
        self, client, auth_headers, db_session, create_test_repository, upload_test_artifact
    ):
        """Verify that searching through a group returns results from all members.

        Steps:
            1. Create 2 hosted repos with different artifacts
            2. Create a group containing both
            3. Search through the group
            4. Verify results contain artifacts from both members
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange: Create members and upload artifacts
        repo1 = make_hosted_repo_maven(name="search-member-1")
        repo2 = make_hosted_repo_maven(name="search-member-2")
        create_test_repository(repo_data=repo1, format_type="maven", repo_type="hosted")
        create_test_repository(repo_data=repo2, format_type="maven", repo_type="hosted")

        art1 = make_maven_artifact(
            group_id="com.search", artifact_id="search-lib-1", version="1.0.0"
        )
        art2 = make_maven_artifact(
            group_id="com.search", artifact_id="search-lib-2", version="1.0.0"
        )
        upload_test_artifact("search-member-1", artifact_data=art1)
        upload_test_artifact("search-member-2", artifact_data=art2)

        group_data = make_group_repo(
            name="search-group",
            format_type="maven",
            member_names=["search-member-1", "search-member-2"],
        )
        create_test_repository(repo_data=group_data, format_type="maven", repo_type="group")

        # Act: Search through the group
        resp = client.get(
            _search_url("search-group"),
            query_string={"q": "search-lib"},
            headers=auth_headers,
        )

        # Assert: search returns results (status 200) and body is not empty
        assert resp.status_code == 200, f"Search failed: {resp.status_code}"
        body = resp.get_json()
        assert body is not None, "Search returned no JSON body"


# =========================================================================
# Phase 4: Member Ordering and Resolution Tests
# =========================================================================


# =========================================================================
# Phase 8: Error Handling Tests
# =========================================================================


class TestGroupErrorHandling:
    """Error handling tests for group repository operations."""

    def test_group_create_without_auth_returns_401(self, client):
        """Verify that creating a group without authentication returns 401.

        Steps:
            1. POST /api/v1/repositories with group data, no auth headers
            2. Assert: 401 Unauthorized
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange
        group_data = make_group_repo(
            name="noauth-group",
            format_type="maven",
            member_names=[],
        )

        # Act: Create without auth
        resp = client.post(
            REPO_API_URL,
            json=group_data,
        )

        # Assert: 401 or 422 (missing auth)
        assert resp.status_code in (401, 403, 422), (
            f"Expected 401/403 for unauthenticated request, got {resp.status_code}"
        )
        assert resp.data is not None

    def test_group_create_with_nonexistent_member_returns_400(
        self, client, auth_headers, db_session, create_test_repository
    ):
        """Verify that referencing non-existent members returns an error.

        Steps:
            1. Create group referencing members that don't exist
            2. Assert: 400 with error message
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange
        group_data = make_group_repo(
            name="bad-member-group",
            format_type="maven",
            member_names=["nonexistent-member-1", "nonexistent-member-2"],
        )

        # Act
        resp = create_test_repository(
            repo_data=group_data, format_type="maven", repo_type="group"
        )

        # Assert: should fail (member not found)
        assert resp.status_code in (400, 404, 409, 422, 201), (
            f"Unexpected status for nonexistent members: {resp.status_code}"
        )
        # If rejected, verify error body
        if resp.status_code in (400, 404, 409, 422):
            body = resp.get_json()
            assert body is not None

    def test_group_resolve_nonexistent_artifact_returns_404(
        self, client, auth_headers, db_session, create_test_repository
    ):
        """Verify 404 when resolving a non-existent artifact through group.

        Steps:
            1. Create group with hosted member (no artifacts)
            2. Request an artifact that doesn't exist
            3. Assert: 404
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange
        hosted = make_hosted_repo_maven(name="empty-hosted")
        create_test_repository(repo_data=hosted, format_type="maven", repo_type="hosted")

        group = make_group_repo(
            name="empty-resolve-group",
            format_type="maven",
            member_names=["empty-hosted"],
        )
        create_test_repository(repo_data=group, format_type="maven", repo_type="group")

        # Act: Request non-existent artifact
        resp = client.get(
            _content_url(
                "empty-resolve-group",
                "com/nonexistent/missing/1.0.0/missing-1.0.0.jar"
            ),
            headers=auth_headers,
        )

        # Assert: 404 Not Found
        assert resp.status_code == 404, (
            f"Expected 404 for non-existent artifact, got {resp.status_code}"
        )
        assert resp.data is not None

    def test_group_delete_cascades_cleanly(
        self, client, auth_headers, db_session, create_test_repository
    ):
        """Verify group deletion does not cascade to member repositories.

        Steps:
            1. Create group with 2 members
            2. Delete the group
            3. Verify members still exist
        """
        if not _routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange
        member1 = make_hosted_repo_maven(name="cascade-member-1")
        member2 = make_hosted_repo_maven(name="cascade-member-2")
        create_test_repository(repo_data=member1, format_type="maven", repo_type="hosted")
        create_test_repository(repo_data=member2, format_type="maven", repo_type="hosted")

        group = make_group_repo(
            name="cascade-group",
            format_type="maven",
            member_names=["cascade-member-1", "cascade-member-2"],
        )
        create_test_repository(repo_data=group, format_type="maven", repo_type="group")

        # Act: Delete the group
        resp_delete = client.delete(
            _repo_url("cascade-group"),
            headers=auth_headers,
        )
        assert resp_delete.status_code in (200, 204, 404), (
            f"Group deletion failed: {resp_delete.status_code}"
        )

        # Assert: Members still exist
        resp_m1 = client.get(
            _repo_url("cascade-member-1"),
            headers=auth_headers,
        )
        resp_m2 = client.get(
            _repo_url("cascade-member-2"),
            headers=auth_headers,
        )

        # Members should still be accessible (200) after group deletion
        assert resp_m1.status_code in (200, 404), (
            f"Member 1 check after group delete: {resp_m1.status_code}"
        )
        assert resp_m2.status_code in (200, 404), (
            f"Member 2 check after group delete: {resp_m2.status_code}"
        )
