"""
Unit tests for the Flask application factory (``src/app.py``).

Validates ``create_app()`` for configuration loading, blueprint registration,
extension initialization, error handler setup, edge cases, and error paths.
Tests follow AAA pattern and are marked ``@pytest.mark.unit`` via conftest.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from flask import Flask, Blueprint

from src.app import create_app
from tests.fixtures.config_data import (
    make_testing_config,
    make_development_config,
    make_production_config,
    make_config_with_missing_required_fields,
    make_config_with_invalid_database_uri,
    make_config_with_conflicting_settings,
)

# Apply @pytest.mark.unit to every test in this module
pytestmark = pytest.mark.unit


EXPECTED_BLUEPRINT_CONFIGS = [
    ("repository", "src.api.repository_routes", "repository_bp", "/api/v1/repositories"),
    ("asset", "src.api.asset_routes", "asset_bp", "/api/v1/assets"),
    ("auth", "src.api.auth_routes", "auth_bp", "/api/v1/auth"),
    ("user", "src.api.user_routes", "user_bp", "/api/v1/users"),
    ("search", "src.api.search_routes", "search_bp", "/api/v1/search"),
    ("admin", "src.api.admin_routes", "admin_bp", "/api/v1/admin"),
    ("task", "src.api.task_routes", "task_bp", "/api/v1/tasks"),
    ("blobstore", "src.api.blobstore_routes", "blobstore_bp", "/api/v1/blobstores"),
]

EXPECTED_BLUEPRINT_NAMES = [cfg[0] for cfg in EXPECTED_BLUEPRINT_CONFIGS]


def _build_mock_blueprint_modules() -> dict:
    """Return {module_path: MagicMock} with real Blueprint attrs for sys.modules patching."""
    mock_modules: dict = {}
    for bp_name, module_path, attr_name, _url_prefix in EXPECTED_BLUEPRINT_CONFIGS:
        bp = Blueprint(bp_name, __name__)
        mock_mod = MagicMock()
        setattr(mock_mod, attr_name, bp)
        mock_modules[module_path] = mock_mod
    return mock_modules


# ===========================================================================
# Happy Path Tests — App Creation with Valid Config
# ===========================================================================


class TestCreateAppHappyPath:
    """Happy-path tests for ``create_app()`` with valid configurations."""

    def test_create_app_returns_flask_instance(self) -> None:
        """create_app() returns a Flask instance with testing mode active."""
        config = make_testing_config()
        app = create_app(config)
        assert isinstance(app, Flask)
        assert app.testing is True

    def test_create_app_with_testing_config_applies_all_values(self) -> None:
        """Testing config sets TESTING, DB URI, and SECRET_KEY correctly."""
        config = make_testing_config()
        app = create_app(config)
        assert app.config["TESTING"] is True
        assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
        assert app.config["SECRET_KEY"] == "test-secret-key-not-for-production"

    def test_create_app_with_development_config(self) -> None:
        """Development config enables DEBUG and disables TESTING."""
        config = make_development_config()
        app = create_app(config)
        assert app.config["DEBUG"] is True
        assert app.config["TESTING"] is False

    def test_create_app_with_production_config(self) -> None:
        """Production config disables both DEBUG and TESTING."""
        # Override DB URI to avoid psycopg2 dependency in unit tests
        config = make_production_config(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
        app = create_app(config)
        assert app.config["DEBUG"] is False
        assert app.config["TESTING"] is False


# ===========================================================================
# Blueprint Registration Tests
# ===========================================================================


class TestBlueprintRegistration:
    """Tests verifying blueprint registration behaviour in create_app()."""

    def test_create_app_handles_missing_blueprint_modules_gracefully(self) -> None:
        """App is created successfully even when blueprint modules are absent."""
        app = create_app(make_testing_config())
        assert isinstance(app, Flask)
        assert isinstance(app.blueprints, dict)

    @pytest.mark.parametrize(
        "bp_name,module_path,attr_name,url_prefix",
        EXPECTED_BLUEPRINT_CONFIGS,
        ids=EXPECTED_BLUEPRINT_NAMES,
    )
    def test_create_app_registers_individual_blueprint(
        self, bp_name: str, module_path: str, attr_name: str, url_prefix: str,
    ) -> None:
        """Each blueprint is registered when its module is importable."""
        bp = Blueprint(bp_name, __name__)
        mock_mod = MagicMock()
        setattr(mock_mod, attr_name, bp)
        with patch.dict("sys.modules", {module_path: mock_mod}):
            app = create_app(make_testing_config())
        assert bp_name in app.blueprints
        assert app.blueprints[bp_name] is bp

    def test_create_app_registers_all_expected_blueprints(self) -> None:
        """All 8 expected blueprints are registered when modules exist."""
        mock_modules = _build_mock_blueprint_modules()
        with patch.dict("sys.modules", mock_modules):
            app = create_app(make_testing_config())
        for bp_name in EXPECTED_BLUEPRINT_NAMES:
            assert bp_name in app.blueprints, f"'{bp_name}' missing"
        assert len(app.blueprints) >= len(EXPECTED_BLUEPRINT_NAMES)


# ===========================================================================
# Extension Initialization Tests
# ===========================================================================


class TestExtensionInitialization:
    """Tests verifying Flask extension initialization."""

    def test_create_app_initializes_sqlalchemy(self) -> None:
        """SQLAlchemy is initialized and present in app.extensions."""
        app = create_app(make_testing_config())
        assert "sqlalchemy" in app.extensions
        assert app.extensions["sqlalchemy"] is not None

    def test_create_app_initializes_jwt(self) -> None:
        """Flask-JWT-Extended is initialized and present in app.extensions."""
        app = create_app(make_testing_config())
        assert "flask-jwt-extended" in app.extensions
        assert app.extensions["flask-jwt-extended"] is not None

    def test_create_app_initializes_migrate(self) -> None:
        """Flask-Migrate is initialized and present in app.extensions."""
        app = create_app(make_testing_config())
        assert "migrate" in app.extensions
        assert app.extensions["migrate"] is not None

    def test_create_app_sets_sqlalchemy_tracking_false(self) -> None:
        """SQLALCHEMY_TRACK_MODIFICATIONS is False for performance."""
        app = create_app(make_testing_config())
        assert app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] is False
        assert "sqlalchemy" in app.extensions


# ===========================================================================
# Error Handler Tests
# ===========================================================================


class TestErrorHandlers:
    """Tests verifying HTTP error handler registration."""

    def test_create_app_handles_404_errors(self) -> None:
        """Requests to non-existent routes return 404 JSON response."""
        app = create_app(make_testing_config())
        with app.test_client() as client:
            response = client.get("/this-route-does-not-exist")
        assert response.status_code == 404
        json_data = response.get_json()
        assert json_data is not None
        assert json_data["error"] == "Not Found"

    def test_create_app_handles_405_method_not_allowed(self) -> None:
        """Sending wrong HTTP method returns 405 JSON response."""
        app = create_app(make_testing_config())

        @app.route("/test-get-only", methods=["GET"])
        def get_only_route():
            return "OK"

        with app.test_client() as client:
            response = client.post("/test-get-only")
        assert response.status_code == 405
        json_data = response.get_json()
        assert json_data is not None
        assert json_data["error"] == "Method Not Allowed"

    def test_create_app_handles_500_errors(self) -> None:
        """Unhandled exceptions return 500 JSON when propagation disabled."""
        config = make_testing_config(PROPAGATE_EXCEPTIONS=False)
        app = create_app(config)

        @app.route("/test-500-trigger")
        def trigger_internal_error():
            raise RuntimeError("Intentional test error")

        with app.test_client() as client:
            response = client.get("/test-500-trigger")
        assert response.status_code == 500
        json_data = response.get_json()
        assert json_data is not None
        assert json_data["error"] == "Internal Server Error"

    def test_create_app_404_response_has_json_content_type(self) -> None:
        """404 error responses use application/json content type."""
        app = create_app(make_testing_config())
        with app.test_client() as client:
            response = client.get("/nonexistent")
        assert response.status_code == 404
        assert response.content_type == "application/json"


# ===========================================================================
# Edge Case Tests
# ===========================================================================


class TestEdgeCases:
    """Edge case tests for create_app() boundary conditions."""

    def test_create_app_with_missing_environment_variables(self) -> None:
        """App uses env-var defaults when config omits SECRET_KEY and DB URI."""
        config = make_config_with_missing_required_fields()
        app = create_app(config)
        assert isinstance(app, Flask)
        assert app.config["TESTING"] is True
        assert app.config["SECRET_KEY"] is not None
        assert len(app.config["SECRET_KEY"]) > 0

    def test_create_app_with_conflicting_configurations(self) -> None:
        """App is created with conflicting settings applied as-is."""
        config = make_config_with_conflicting_settings()
        app = create_app(config)
        assert app.config["TESTING"] is True
        assert app.config["DEBUG"] is False

    def test_create_app_with_custom_config_override(self) -> None:
        """Custom configuration values override base config correctly."""
        custom_secret = "custom-override-secret-key"
        config = make_testing_config(SECRET_KEY=custom_secret, LOG_LEVEL="ERROR")
        app = create_app(config)
        assert app.config["SECRET_KEY"] == custom_secret
        assert app.config["LOG_LEVEL"] == "ERROR"

    def test_create_app_with_none_config_uses_defaults(self) -> None:
        """Passing None as config uses environment variable defaults."""
        app = create_app(None)
        assert isinstance(app, Flask)
        assert "sqlalchemy" in app.extensions

    def test_create_app_with_object_config(self) -> None:
        """Config can be provided as an object with attributes."""

        class TestConfig:
            TESTING = True
            SECRET_KEY = "object-config-secret"
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SQLALCHEMY_TRACK_MODIFICATIONS = False

        app = create_app(TestConfig)
        assert app.config["TESTING"] is True
        assert app.config["SECRET_KEY"] == "object-config-secret"


# ===========================================================================
# Error Case Tests
# ===========================================================================


class TestErrorCases:
    """Tests for error conditions during app creation."""

    def test_create_app_with_invalid_database_uri_raises_error(self) -> None:
        """Invalid database URI causes an exception during extension init."""
        config = make_config_with_invalid_database_uri()
        with pytest.raises(Exception) as exc_info:
            create_app(config)
        assert exc_info.value is not None
        error_msg = str(exc_info.value).lower()
        assert "invalid" in error_msg or "can't load" in error_msg

    def test_create_app_without_secret_key_uses_fallback(self) -> None:
        """App uses a fallback SECRET_KEY when none is provided in config."""
        config = make_testing_config()
        del config["SECRET_KEY"]
        app = create_app(config)
        assert isinstance(app, Flask)
        assert app.config["SECRET_KEY"] is not None
        assert len(app.config["SECRET_KEY"]) > 0


# ===========================================================================
# Configuration Loading Tests
# ===========================================================================


class TestConfigurationLoading:
    """Tests for configuration loading behaviour across profiles."""

    @pytest.mark.parametrize(
        "env_name,config_factory,expected_testing,expected_debug",
        [
            ("testing", make_testing_config, True, True),
            ("development", make_development_config, False, True),
            ("production", make_production_config, False, False),
        ],
        ids=["testing", "development", "production"],
    )
    def test_create_app_loads_config_profile_correctly(
        self, env_name: str, config_factory, expected_testing: bool,
        expected_debug: bool,
    ) -> None:
        """Each environment profile sets TESTING and DEBUG appropriately."""
        # Override DB URI to sqlite for all profiles to avoid psycopg2 dep
        config = config_factory(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
        app = create_app(config)
        assert app.config["TESTING"] is expected_testing, f"{env_name}: TESTING"
        assert app.config["DEBUG"] is expected_debug, f"{env_name}: DEBUG"

    def test_create_app_config_values_are_correct_types(self) -> None:
        """Configuration values are the correct Python types."""
        app = create_app(make_testing_config())
        assert isinstance(app.config["TESTING"], bool)
        assert isinstance(app.config["SECRET_KEY"], str)
        assert isinstance(app.config["SQLALCHEMY_DATABASE_URI"], str)
        assert isinstance(app.config["SQLALCHEMY_TRACK_MODIFICATIONS"], bool)

    def test_create_app_testing_config_has_required_keys(self) -> None:
        """Testing configuration includes all required keys for the app."""
        required_keys = [
            "TESTING", "SECRET_KEY",
            "SQLALCHEMY_DATABASE_URI", "SQLALCHEMY_TRACK_MODIFICATIONS",
        ]
        app = create_app(make_testing_config())
        for key in required_keys:
            assert key in app.config, f"Required key '{key}' missing"
        assert len(required_keys) == 4

    def test_create_app_production_config_has_secure_settings(self) -> None:
        """Production config includes security-hardened settings."""
        config = make_production_config(SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
        app = create_app(config)
        assert app.config["SESSION_COOKIE_SECURE"] is True
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
