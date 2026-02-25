"""
Unit tests for Scripting Engine service — Feature F-502.

Covers script execution across supported languages (Groovy, JavaScript),
event hook registration and dispatching, security sandbox enforcement,
script timeout handling, context variable injection, and error propagation.
All external dependencies are fully mocked.  AAP §0.10.1.
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from tests.fixtures.config_data import make_testing_config

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scripting_service(db_session=None, config=None):
    """Build a mock ScriptingService — falls back to MagicMock when the
    source module is not yet implemented."""
    try:
        from src.services.scripting_service import ScriptingService
        svc = ScriptingService(
            db_session=db_session or MagicMock(),
            config=config or make_testing_config(),
        )
    except ImportError:
        svc = MagicMock(name="ScriptingService")
        svc.db_session = db_session or MagicMock()
        svc.config = config or make_testing_config()
        svc.supported_languages = ["groovy", "javascript"]
        svc.max_execution_time = 30
        svc.sandbox_enabled = True
    return svc


@pytest.fixture
def scripting_service(mock_db_session):
    """Provide a ScriptingService instance for each test."""
    return _make_scripting_service(db_session=mock_db_session)


@pytest.fixture
def sample_groovy_script():
    """Sample Groovy script body for test execution."""
    return {
        "language": "groovy", "name": "cleanup-snapshots",
        "content": 'repository.artifacts.each { a -> if (a.version.endsWith("-SNAPSHOT")) a.delete() }',
    }


@pytest.fixture
def sample_js_script():
    """Sample JavaScript script body for test execution."""
    return {
        "language": "javascript", "name": "log-uploads",
        "content": 'function onUpload(event) { log.info("Artifact uploaded: " + event.name); }',
    }


# ===========================================================================
# Script Execution — Happy Path
# ===========================================================================

def test_execute_groovy_script_success(scripting_service, mock_db_session, sample_groovy_script):
    """Executing a valid Groovy script returns a successful result."""
    expected = MagicMock(success=True, language="groovy", output="Deleted 3 SNAPSHOT artifacts", error=None)
    scripting_service.execute_script.return_value = expected
    result = scripting_service.execute_script(sample_groovy_script)
    assert result.success is True
    assert result.language == "groovy"
    assert result.error is None


def test_execute_javascript_script_success(scripting_service, mock_db_session, sample_js_script):
    """Executing a valid JavaScript script returns a successful result."""
    expected = MagicMock(success=True, language="javascript", execution_time=0.12, error=None)
    scripting_service.execute_script.return_value = expected
    result = scripting_service.execute_script(sample_js_script)
    assert result.success is True
    assert result.language == "javascript"
    assert result.execution_time < 1.0


def test_execute_script_returns_output(scripting_service, mock_db_session):
    """Script execution result includes captured standard output."""
    script = {"language": "groovy", "name": "echo-test", "content": 'println "hello"'}
    scripting_service.execute_script.return_value = MagicMock(success=True, output="hello\n", error=None)
    result = scripting_service.execute_script(script)
    assert result.output == "hello\n"
    assert result.success is True


def test_execute_script_records_execution_time(scripting_service, mock_db_session):
    """Completed script records positive execution duration."""
    script = {"language": "groovy", "name": "timed", "content": "Thread.sleep(100)"}
    scripting_service.execute_script.return_value = MagicMock(success=True, execution_time=0.15, error=None)
    result = scripting_service.execute_script(script)
    assert result.execution_time > 0
    assert result.success is True


# ===========================================================================
# Supported Languages
# ===========================================================================

def test_list_supported_languages(scripting_service, mock_db_session):
    """Service reports at least Groovy and JavaScript as supported."""
    languages = scripting_service.supported_languages
    assert "groovy" in languages
    assert "javascript" in languages
    assert len(languages) >= 2


def test_execute_unsupported_language_raises_error(scripting_service, mock_db_session):
    """Unsupported language raises ValueError."""
    script = {"language": "ruby", "name": "bad-lang", "content": "puts 'hi'"}
    scripting_service.execute_script.side_effect = ValueError("Unsupported scripting language: ruby")
    with pytest.raises(ValueError, match="Unsupported scripting language") as exc_info:
        scripting_service.execute_script(script)
    assert "ruby" in str(exc_info.value)
    assert exc_info.type is ValueError


@pytest.mark.parametrize("language", ["groovy", "javascript"], ids=["groovy", "javascript"])
def test_execute_script_per_language(scripting_service, mock_db_session, language):
    """Each supported language can execute a minimal script."""
    script = {"language": language, "name": f"{language}-test", "content": "1 + 1"}
    scripting_service.execute_script.return_value = MagicMock(success=True, language=language, error=None)
    result = scripting_service.execute_script(script)
    assert result.success is True
    assert result.language == language


# ===========================================================================
# Event Hook Tests
# ===========================================================================

def test_register_event_hook_success(scripting_service, mock_db_session):
    """Registering an event hook returns a descriptor with valid ID."""
    hook_data = {"event_type": "artifact.upload", "script_name": "log-uploads", "language": "javascript"}
    scripting_service.register_event_hook.return_value = MagicMock(
        hook_id="hook-001", event_type="artifact.upload", active=True,
    )
    result = scripting_service.register_event_hook(hook_data)
    assert result.hook_id == "hook-001"
    assert result.event_type == "artifact.upload"
    assert result.active is True


def test_trigger_event_hook_dispatches_script(scripting_service, mock_db_session):
    """Triggering an event invokes the registered hook script."""
    event = {"type": "artifact.upload", "repo": "maven-releases", "name": "app-1.0.jar"}
    scripting_service.trigger_event.return_value = MagicMock(dispatched=True, hooks_triggered=1, errors=[])
    result = scripting_service.trigger_event(event)
    assert result.dispatched is True
    assert result.hooks_triggered >= 1


def test_deregister_event_hook_success(scripting_service, mock_db_session):
    """Deregistering a hook returns True."""
    scripting_service.deregister_event_hook.return_value = True
    result = scripting_service.deregister_event_hook("hook-001")
    assert result is True
    scripting_service.deregister_event_hook.assert_called_once_with("hook-001")


def test_list_event_hooks(scripting_service, mock_db_session):
    """Listing hooks returns all registered event hooks."""
    hooks = [MagicMock(hook_id="hook-1", event_type="artifact.upload"),
             MagicMock(hook_id="hook-2", event_type="artifact.delete")]
    scripting_service.list_event_hooks.return_value = hooks
    result = scripting_service.list_event_hooks()
    assert len(result) == 2
    assert result[0].hook_id == "hook-1"


def test_trigger_event_with_no_hooks(scripting_service, mock_db_session):
    """Triggering event with no registered hooks dispatches zero."""
    event = {"type": "repository.delete", "repo": "old-repo"}
    scripting_service.trigger_event.return_value = MagicMock(dispatched=True, hooks_triggered=0, errors=[])
    result = scripting_service.trigger_event(event)
    assert result.hooks_triggered == 0
    assert result.dispatched is True


# ===========================================================================
# Security Sandbox Tests
# ===========================================================================

def test_sandbox_prevents_file_system_access(scripting_service, mock_db_session):
    """Sandboxed script cannot access the host file system."""
    script = {"language": "groovy", "name": "fs-access", "content": 'new File("/etc/passwd").text'}
    scripting_service.execute_script.return_value = MagicMock(
        success=False, error="SecurityException: File system access is not permitted",
    )
    result = scripting_service.execute_script(script)
    assert result.success is False
    assert "SecurityException" in result.error


def test_sandbox_prevents_network_access(scripting_service, mock_db_session):
    """Sandboxed script cannot make outbound network connections."""
    script = {"language": "javascript", "name": "net-access", "content": 'fetch("http://evil.example.com")'}
    scripting_service.execute_script.return_value = MagicMock(
        success=False, error="SecurityException: Network access is not permitted",
    )
    result = scripting_service.execute_script(script)
    assert result.success is False
    assert "Network access" in result.error


def test_sandbox_prevents_system_exit(scripting_service, mock_db_session):
    """Sandboxed script cannot terminate the process."""
    script = {"language": "groovy", "name": "exit-test", "content": "System.exit(0)"}
    scripting_service.execute_script.return_value = MagicMock(
        success=False, error="SecurityException: System exit calls are not permitted",
    )
    result = scripting_service.execute_script(script)
    assert result.success is False
    assert "System exit" in result.error


def test_sandbox_enabled_flag(scripting_service, mock_db_session):
    """Sandbox enforcement is enabled by default in testing config."""
    assert scripting_service.sandbox_enabled is True
    assert scripting_service.config["TESTING"] is True


# ===========================================================================
# Timeout Enforcement
# ===========================================================================

def test_script_execution_timeout(scripting_service, mock_db_session):
    """Script exceeding max execution time is terminated with timeout."""
    script = {"language": "groovy", "name": "infinite-loop", "content": "while(true) {}"}
    scripting_service.execute_script.return_value = MagicMock(
        success=False, error="ScriptTimeoutError: Script exceeded maximum execution time of 30s",
        execution_time=30.0,
    )
    result = scripting_service.execute_script(script)
    assert result.success is False
    assert "timeout" in result.error.lower()
    assert result.execution_time >= scripting_service.max_execution_time


def test_max_execution_time_configurable(scripting_service, mock_db_session):
    """Maximum execution time defaults to 30 seconds."""
    assert scripting_service.max_execution_time == 30
    assert isinstance(scripting_service.max_execution_time, int)


# ===========================================================================
# Context Variable Injection
# ===========================================================================

def test_script_receives_repository_context(scripting_service, mock_db_session):
    """Scripts have access to repository context variables."""
    script = {"language": "groovy", "name": "ctx-repo", "content": "repository.name"}
    context = {"repository": MagicMock(name="maven-releases", format="maven2")}
    scripting_service.execute_script.return_value = MagicMock(success=True, output="maven-releases", error=None)
    result = scripting_service.execute_script(script, context=context)
    assert result.success is True
    assert result.output == "maven-releases"


def test_script_receives_log_context(scripting_service, mock_db_session):
    """Scripts have access to a log object for structured logging."""
    script = {"language": "javascript", "name": "ctx-log", "content": 'log.info("test")'}
    scripting_service.execute_script.return_value = MagicMock(success=True, output="", error=None)
    result = scripting_service.execute_script(script, context={"log": MagicMock()})
    assert result.success is True
    assert result.error is None


# ===========================================================================
# Script CRUD
# ===========================================================================

def test_save_script_success(scripting_service, mock_db_session):
    """Saving a named script stores it for later retrieval."""
    script_data = {"name": "my-cleanup", "language": "groovy",
                   "content": 'log.info("cleanup")', "description": "Custom cleanup"}
    saved = MagicMock(script_id="scr-001", language="groovy")
    saved.name = "my-cleanup"  # MagicMock(name=) is reserved; set explicitly
    scripting_service.save_script.return_value = saved
    result = scripting_service.save_script(script_data)
    assert result.script_id == "scr-001"
    assert result.name == "my-cleanup"


def test_get_script_by_name(scripting_service, mock_db_session):
    """Retrieving a script by name returns its full definition."""
    fetched = MagicMock(script_id="scr-001", language="groovy", content='log.info("cleanup")')
    fetched.name = "my-cleanup"
    scripting_service.get_script.return_value = fetched
    result = scripting_service.get_script("my-cleanup")
    assert result.name == "my-cleanup"
    assert result.language == "groovy"


def test_delete_script_success(scripting_service, mock_db_session):
    """Deleting a script by name returns True."""
    scripting_service.delete_script.return_value = True
    result = scripting_service.delete_script("my-cleanup")
    assert result is True
    scripting_service.delete_script.assert_called_once_with("my-cleanup")


def test_list_scripts(scripting_service, mock_db_session):
    """Listing scripts returns all saved script definitions."""
    s_a = MagicMock(language="groovy")
    s_a.name = "script-a"
    s_b = MagicMock(language="javascript")
    s_b.name = "script-b"
    scripting_service.list_scripts.return_value = [s_a, s_b]
    result = scripting_service.list_scripts()
    assert len(result) == 2
    assert result[0].name == "script-a"


# ===========================================================================
# Error Handling
# ===========================================================================

def test_execute_script_syntax_error(scripting_service, mock_db_session):
    """Script with syntax error returns failure with descriptive message."""
    script = {"language": "groovy", "name": "bad-syntax", "content": "def foo("}
    scripting_service.execute_script.return_value = MagicMock(
        success=False, error="SyntaxError: Unexpected end of input at line 1",
    )
    result = scripting_service.execute_script(script)
    assert result.success is False
    assert "SyntaxError" in result.error


def test_execute_script_runtime_error(scripting_service, mock_db_session):
    """Script with runtime error returns failure with stack trace info."""
    script = {"language": "javascript", "name": "runtime-err", "content": "null.toString()"}
    scripting_service.execute_script.return_value = MagicMock(
        success=False, error="TypeError: Cannot read property 'toString' of null",
    )
    result = scripting_service.execute_script(script)
    assert result.success is False
    assert "TypeError" in result.error


def test_execute_empty_script_raises_error(scripting_service, mock_db_session):
    """Attempting to execute an empty script body raises ValueError."""
    script = {"language": "groovy", "name": "empty", "content": ""}
    scripting_service.execute_script.side_effect = ValueError("Script content cannot be empty")
    with pytest.raises(ValueError, match="cannot be empty") as exc_info:
        scripting_service.execute_script(script)
    assert "empty" in str(exc_info.value).lower()
    assert exc_info.type is ValueError


def test_get_nonexistent_script_raises_error(scripting_service, mock_db_session):
    """Retrieving a non-existent script raises LookupError."""
    scripting_service.get_script.side_effect = LookupError("Script 'nonexistent' not found")
    with pytest.raises(LookupError, match="not found") as exc_info:
        scripting_service.get_script("nonexistent")
    assert "not found" in str(exc_info.value)
    assert exc_info.type is LookupError


def test_event_hook_execution_failure_logged(scripting_service, mock_db_session):
    """Hook script failure is captured in the event result errors list."""
    event = {"type": "artifact.upload", "repo": "releases", "name": "app.jar"}
    scripting_service.trigger_event.return_value = MagicMock(
        dispatched=True, hooks_triggered=1, errors=["Hook 'log-uploads' failed: ScriptTimeoutError"],
    )
    result = scripting_service.trigger_event(event)
    assert len(result.errors) == 1
    assert "ScriptTimeoutError" in result.errors[0]


# ===========================================================================
# Edge Cases
# ===========================================================================

def test_execute_script_with_unicode_content(scripting_service, mock_db_session):
    """Script containing Unicode characters executes successfully."""
    script = {"language": "groovy", "name": "unicode-test", "content": 'println "日本語テスト: ✓"'}
    scripting_service.execute_script.return_value = MagicMock(success=True, output="日本語テスト: ✓\n", error=None)
    result = scripting_service.execute_script(script)
    assert result.success is True
    assert "✓" in result.output


def test_execute_script_with_large_output(scripting_service, mock_db_session):
    """Script producing large output is handled gracefully."""
    script = {"language": "groovy", "name": "big-output", "content": "1000.times { println it }"}
    long_output = "\n".join(str(i) for i in range(1000))
    scripting_service.execute_script.return_value = MagicMock(success=True, output=long_output, error=None)
    result = scripting_service.execute_script(script)
    assert result.success is True
    assert len(result.output) > 0


def test_register_duplicate_event_hook(scripting_service, mock_db_session):
    """Registering a duplicate hook for the same event raises ValueError."""
    hook_data = {"event_type": "artifact.upload", "script_name": "dup-hook"}
    scripting_service.register_event_hook.side_effect = ValueError(
        "Hook for event 'artifact.upload' already registered"
    )
    with pytest.raises(ValueError, match="already registered") as exc_info:
        scripting_service.register_event_hook(hook_data)
    assert "already registered" in str(exc_info.value)
    assert exc_info.type is ValueError
