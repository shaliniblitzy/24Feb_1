"""
Functional tests for the Flask Binary Repository Management System.

This package contains end-to-end workflow tests that exercise complete
user scenarios through multiple application layers:

- test_artifact_lifecycle: Upload → Index → Search → Download → Delete
- test_repository_lifecycle: Create → Configure → Populate → Browse → Cleanup → Delete
- test_auth_workflow: Register → Login → Access Resource → Refresh → Logout
- test_proxy_workflow: Configure → Fetch from Upstream → Cache → Serve Cached
- test_group_repository: Create Members → Create Group → Resolve from Group

All tests in this package use @pytest.mark.functional for selective execution.
Functional tests may use a test database (not just mocks) for realistic
workflow testing. Full functional suite must complete in < 15 minutes.
"""
