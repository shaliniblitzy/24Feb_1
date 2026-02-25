"""
Script Storage, Validation, and Execution Service.

Implements Feature F-502 (Scripting Support) — a Python-native sandboxed
execution engine replacing the Groovy scripting engine from the original
Java Sonatype Nexus Repository.

**Architecture Context:**

In the Java source system, Nexus supported Groovy scripts for server-side
administrative automation.  This Python reimplementation uses Python's own
language as the scripting dialect — a natural fit since the application
server itself runs on Python.  Scripts are validated via ``ast`` (Abstract
Syntax Tree) analysis before execution and run inside a restricted sandbox
with limited builtins and no access to imports, file I/O, or subprocess
calls.

**Key Design Decisions:**

- **Storage via SystemConfig:** Scripts are stored as JSON-serialized values
  in the ``SystemConfig`` table using keys prefixed with ``scripts.``
  (e.g., ``scripts.my_admin_task``).  This reuses the existing key-value
  configuration infrastructure without requiring a new table.

- **AST-based validation:** Before execution, every script is parsed into
  an Abstract Syntax Tree.  Forbidden nodes (``import``, ``from … import``)
  are rejected, and dangerous function calls (``eval``, ``exec``,
  ``__import__``, ``open``, ``compile``, ``os.system``, ``subprocess.run``)
  are detected and blocked.

- **Restricted builtins:** Only a curated whitelist of safe Python builtins
  is available inside the sandbox.  Dangerous functions like ``open``,
  ``exec``, ``eval``, and ``__import__`` are excluded.

- **Timeout enforcement:** Execution duration is measured via
  ``time.monotonic()`` and compared against ``MAX_EXECUTION_TIME``.
  Python's ``exec()`` does not support native pre-emptive timeouts, so
  post-execution timing checks are used; true pre-emptive timeout would
  require ``threading`` or ``signal``-based approaches for long-running
  CPU-bound scripts.

- **Event emission:** Script CRUD operations and executions emit
  ``CONFIG_CHANGED`` events for audit logging (F-303) and webhook
  dispatch (F-503).

**Exports:**

- ``ScriptService``         — Main service class with full CRUD and execution
- ``ScriptError``           — Base exception for script operations
- ``ScriptValidationError`` — Raised on validation failure
- ``ScriptExecutionError``  — Raised on runtime execution failure
- ``ScriptTimeoutError``    — Raised when execution exceeds time limit
- ``MAX_SCRIPT_SIZE``       — Maximum allowed script content length (64 KB)
- ``MAX_EXECUTION_TIME``    — Maximum allowed execution duration (30 s)
- ``SCRIPT_STORAGE_PREFIX`` — Key prefix for SystemConfig storage
- ``ALLOWED_BUILTINS``      — Set of safe builtin names for the sandbox
- ``FORBIDDEN_AST_NODES``   — Set of AST node types rejected during validation
"""

from __future__ import annotations

import ast
import contextlib
import ctypes
import io
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from flask import current_app

from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType
from src.app.extensions import db
from src.app.models.system_config import SystemConfig
from src.app.utils.helpers import generate_uuid

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source system.  Provides diagnostic output for script CRUD operations,
# validation results, execution events, and error conditions.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SCRIPT_SIZE: int = 65536
"""Maximum allowed script content size in bytes (64 KB).

Prevents excessive resource usage from oversized script payloads.
"""

MAX_EXECUTION_TIME: int = 30
"""Maximum allowed script execution time in seconds.

Scripts exceeding this duration trigger a ``ScriptTimeoutError``.  Note
that Python's ``exec()`` does not natively support pre-emptive timeouts;
the check occurs after ``exec()`` returns (or raises).  Truly runaway
scripts would require ``threading.Timer`` or ``signal.alarm`` mitigation.
"""

SCRIPT_STORAGE_PREFIX: str = "scripts."
"""Key prefix used when storing scripts in the ``SystemConfig`` table.

For example, a script named ``my_task`` is stored under the key
``scripts.my_task``.
"""

ALLOWED_BUILTINS: set[str] = {
    "abs", "all", "any", "bool", "bytes", "callable", "chr", "dict",
    "dir", "divmod", "enumerate", "filter", "float", "format",
    "frozenset", "hash", "hex", "id", "int",
    "isinstance", "issubclass", "iter", "len", "list", "map", "max",
    "min", "next", "oct", "ord", "pow", "print", "range",
    "repr", "reversed", "round", "set", "slice", "sorted", "str",
    "sum", "tuple", "zip",
}
"""Set of Python builtin function names allowed inside the sandbox.

Dangerous builtins are explicitly excluded:
``open``, ``exec``, ``eval``, ``compile``, ``__import__``, ``globals``,
``locals``, ``vars``, ``delattr``, ``setattr``, ``breakpoint``,
``exit``, ``quit``, ``input``, ``memoryview``, ``super``.

Additionally, ``object``, ``getattr``, ``hasattr``, and ``type`` are excluded
to prevent sandbox escape via Python's object introspection chain.  With
``object`` and ``getattr`` available, a script can call
``object.__subclasses__()`` to enumerate all loaded classes, locate module
loaders, and achieve arbitrary code execution — bypassing AST-level checks
entirely (CWE-94).
"""

FORBIDDEN_AST_NODES: set[type] = {
    ast.Import,
    ast.ImportFrom,
}
"""Set of AST node types that are unconditionally rejected during script
validation.  ``import`` and ``from … import`` statements are forbidden
to prevent sandbox escape via arbitrary module loading.
"""

# Dangerous function names detected during AST call-site analysis.
# These are checked in _check_dangerous_calls() by inspecting ast.Call nodes.
_DANGEROUS_FUNCTION_NAMES: set[str] = {
    "eval", "exec", "__import__", "open", "compile",
    "globals", "locals", "vars", "delattr", "setattr",
    "breakpoint", "exit", "quit", "input",
}

# Dangerous attribute call patterns (module.function) that indicate
# sandbox escape attempts via standard library modules.
_DANGEROUS_ATTRIBUTE_CALLS: set[str] = {
    "os.system", "os.popen", "os.exec", "os.execl", "os.execle",
    "os.execlp", "os.execlpe", "os.execv", "os.execve", "os.execvp",
    "os.execvpe", "os.spawn", "os.spawnl", "os.spawnle",
    "subprocess.run", "subprocess.call", "subprocess.Popen",
    "subprocess.check_call", "subprocess.check_output",
    "subprocess.getoutput", "subprocess.getstatusoutput",
    "shutil.rmtree", "shutil.move",
}

# Script name validation: alphanumeric characters and underscores only.
_SCRIPT_NAME_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------


class ScriptError(Exception):
    """Base exception for all script service errors.

    Attributes:
        message: Human-readable error description.
        details: Optional dictionary with structured error context
            (e.g., validation errors, execution traceback fragments).
    """

    def __init__(self, message: str, details: dict | None = None) -> None:
        self.message: str = message
        self.details: dict = details or {}
        super().__init__(self.message)


class ScriptValidationError(ScriptError):
    """Raised when a script fails syntax parsing or safety validation.

    Carries a list of specific validation errors in ``details['errors']``.
    """

    pass


class ScriptExecutionError(ScriptError):
    """Raised when a script fails at runtime during sandboxed execution.

    The ``details`` dictionary may contain ``'exception_type'`` and
    ``'traceback'`` keys with sanitised diagnostic information.
    """

    pass


class ScriptTimeoutError(ScriptError):
    """Raised when a script exceeds ``MAX_EXECUTION_TIME``.

    The ``details`` dictionary includes ``'elapsed_seconds'`` and
    ``'limit_seconds'`` keys.
    """

    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "ScriptService",
    "ScriptError",
    "ScriptValidationError",
    "ScriptExecutionError",
    "ScriptTimeoutError",
    "MAX_SCRIPT_SIZE",
    "MAX_EXECUTION_TIME",
    "SCRIPT_STORAGE_PREFIX",
    "ALLOWED_BUILTINS",
    "FORBIDDEN_AST_NODES",
]


# ===========================================================================
# ScriptService
# ===========================================================================


class ScriptService:
    """Script storage, validation, and execution service.

    Provides full CRUD operations for named scripts stored in the
    ``SystemConfig`` table, AST-based security validation, and sandboxed
    execution within a restricted Python environment.

    **Usage:**

    .. code-block:: python

        svc = ScriptService()

        # Store a script
        meta = svc.create_script('hello', 'python', 'result = 42')

        # Execute a stored script
        outcome = svc.execute_script('hello')
        assert outcome['result'] == 42

        # Run an inline (unsaved) script
        outcome = svc.run_inline('result = args["x"] * 2', args={'x': 5})
        assert outcome['result'] == 10
    """

    def __init__(self) -> None:
        """Initialise the ScriptService with a dedicated logger."""
        self.logger: logging.Logger = logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # CRUD — Create
    # ------------------------------------------------------------------

    def create_script(
        self, name: str, script_type: str, content: str
    ) -> dict:
        """Create and persist a new named script.

        The script content is validated for syntax correctness and safety
        before storage.  A ``ScriptValidationError`` is raised if
        validation fails.

        Args:
            name: Unique script identifier.  Must match
                ``^[a-zA-Z_][a-zA-Z0-9_]*$`` (alphanumeric + underscores,
                starting with a letter or underscore).
            script_type: Freeform type label (e.g., ``'python'``,
                ``'admin'``, ``'maintenance'``).
            content: The Python source code of the script.

        Returns:
            A metadata dictionary containing ``name``, ``type``,
            ``content_length``, ``created``, and ``updated`` fields.

        Raises:
            ScriptValidationError: If the script name is invalid, content
                exceeds ``MAX_SCRIPT_SIZE``, or content fails safety
                validation.
            ScriptError: If a script with the same name already exists.
        """
        # --- Name validation ---
        if not name or not _SCRIPT_NAME_RE.match(name):
            raise ScriptValidationError(
                f"Invalid script name: '{name}'. Must match "
                f"^[a-zA-Z_][a-zA-Z0-9_]*$",
                details={"name": name},
            )

        # --- Size validation ---
        if len(content.encode("utf-8")) > MAX_SCRIPT_SIZE:
            raise ScriptValidationError(
                f"Script content exceeds maximum size of "
                f"{MAX_SCRIPT_SIZE} bytes.",
                details={"size": len(content.encode("utf-8")),
                         "limit": MAX_SCRIPT_SIZE},
            )

        # --- Safety validation ---
        self._validate_script(content)

        # --- Duplicate check ---
        storage_key: str = f"{SCRIPT_STORAGE_PREFIX}{name}"
        existing = db.session.query(SystemConfig).filter(
            SystemConfig.key == storage_key
        ).first()
        if existing is not None:
            raise ScriptError(
                f"Script '{name}' already exists.",
                details={"name": name},
            )

        # --- Build metadata payload ---
        now: datetime = datetime.now(timezone.utc)
        script_data: dict = {
            "name": name,
            "type": script_type,
            "content": content,
            "created": now.isoformat(),
            "updated": now.isoformat(),
        }

        # --- Persist to SystemConfig ---
        config_entry = SystemConfig(
            key=storage_key,
            value=json.dumps(script_data),
            category="scripts",
            description=f"Stored script: {name} (type={script_type})",
        )
        db.session.add(config_entry)
        db.session.commit()

        self.logger.info(
            "Script '%s' created (type=%s, size=%d bytes).",
            name,
            script_type,
            len(content),
        )

        # --- Emit event for audit logging / webhooks ---
        emit_event(
            EventType.CONFIG_CHANGED,
            payload={
                "key": storage_key,
                "action": "script_created",
                "script_name": name,
                "script_type": script_type,
            },
        )

        return self._build_script_metadata(
            name, script_type, content, now, now
        )

    # ------------------------------------------------------------------
    # CRUD — Read
    # ------------------------------------------------------------------

    def get_script(self, name: str) -> dict | None:
        """Retrieve a script by name, including its content.

        Args:
            name: The script identifier.

        Returns:
            A dictionary with ``name``, ``type``, ``content``,
            ``created``, and ``updated`` fields, or ``None`` if the
            script does not exist.
        """
        storage_key: str = f"{SCRIPT_STORAGE_PREFIX}{name}"
        config_entry = db.session.query(SystemConfig).filter(
            SystemConfig.key == storage_key
        ).first()

        if config_entry is None or config_entry.value is None:
            self.logger.debug("Script '%s' not found.", name)
            return None

        try:
            data: dict = json.loads(config_entry.value)
        except json.JSONDecodeError:
            self.logger.error(
                "Corrupt JSON for script '%s'; returning None.", name
            )
            return None

        return data

    def list_scripts(self) -> list[dict]:
        """List metadata for all stored scripts.

        Returns:
            A list of metadata dictionaries.  Each entry contains
            ``name``, ``type``, ``content_length``, ``created``, and
            ``updated`` fields.  Script content is **not** included to
            keep the listing lightweight.
        """
        config_entries = db.session.query(SystemConfig).filter(
            SystemConfig.key.like(f"{SCRIPT_STORAGE_PREFIX}%")
        ).all()

        results: list[dict] = []
        for entry in config_entries:
            if entry.value is None:
                continue
            try:
                data: dict = json.loads(entry.value)
            except json.JSONDecodeError:
                self.logger.warning(
                    "Skipping corrupt script entry: %s", entry.key
                )
                continue

            results.append(
                self._build_script_metadata(
                    name=data.get("name", entry.key),
                    script_type=data.get("type", "unknown"),
                    content=data.get("content", ""),
                    created=datetime.fromisoformat(data["created"])
                    if "created" in data
                    else datetime.now(timezone.utc),
                    updated=datetime.fromisoformat(data["updated"])
                    if "updated" in data
                    else datetime.now(timezone.utc),
                )
            )

        self.logger.debug("Listed %d script(s).", len(results))
        return results

    # ------------------------------------------------------------------
    # CRUD — Update
    # ------------------------------------------------------------------

    def update_script(self, name: str, content: str) -> dict:
        """Update the content of an existing script.

        Re-validates the new content before persisting.

        Args:
            name: The script identifier.
            content: The new Python source code.

        Returns:
            Updated metadata dictionary.

        Raises:
            ScriptError: If the script does not exist.
            ScriptValidationError: If the new content fails validation.
        """
        storage_key: str = f"{SCRIPT_STORAGE_PREFIX}{name}"
        config_entry = db.session.query(SystemConfig).filter(
            SystemConfig.key == storage_key
        ).first()

        if config_entry is None or config_entry.value is None:
            raise ScriptError(
                f"Script '{name}' not found.",
                details={"name": name},
            )

        # --- Size validation ---
        if len(content.encode("utf-8")) > MAX_SCRIPT_SIZE:
            raise ScriptValidationError(
                f"Script content exceeds maximum size of "
                f"{MAX_SCRIPT_SIZE} bytes.",
                details={"size": len(content.encode("utf-8")),
                         "limit": MAX_SCRIPT_SIZE},
            )

        # --- Safety validation ---
        self._validate_script(content)

        # --- Update persisted data ---
        try:
            data: dict = json.loads(config_entry.value)
        except json.JSONDecodeError:
            data = {"name": name, "type": "python"}

        now: datetime = datetime.now(timezone.utc)
        data["content"] = content
        data["updated"] = now.isoformat()

        config_entry.value = json.dumps(data)
        db.session.commit()

        self.logger.info("Script '%s' updated (size=%d bytes).", name, len(content))

        emit_event(
            EventType.CONFIG_CHANGED,
            payload={
                "key": storage_key,
                "action": "script_updated",
                "script_name": name,
            },
        )

        return self._build_script_metadata(
            name=data.get("name", name),
            script_type=data.get("type", "unknown"),
            content=content,
            created=datetime.fromisoformat(data["created"])
            if "created" in data
            else now,
            updated=now,
        )

    # ------------------------------------------------------------------
    # CRUD — Delete
    # ------------------------------------------------------------------

    def delete_script(self, name: str) -> bool:
        """Delete a script by name.

        Args:
            name: The script identifier.

        Returns:
            ``True`` if the script was deleted, ``False`` if it was not
            found.
        """
        storage_key: str = f"{SCRIPT_STORAGE_PREFIX}{name}"
        config_entry = db.session.query(SystemConfig).filter(
            SystemConfig.key == storage_key
        ).first()

        if config_entry is None:
            self.logger.debug("Script '%s' not found for deletion.", name)
            return False

        db.session.delete(config_entry)
        db.session.commit()

        self.logger.info("Script '%s' deleted.", name)

        emit_event(
            EventType.CONFIG_CHANGED,
            payload={
                "key": storage_key,
                "action": "script_deleted",
                "script_name": name,
            },
        )

        return True

    # ------------------------------------------------------------------
    # Script Validation
    # ------------------------------------------------------------------

    def _validate_script(self, content: str) -> list[str]:
        """Validate script content for syntax correctness and safety.

        Performs a three-step validation pipeline:

        1. **Syntax check** — ``ast.parse()`` to ensure valid Python.
        2. **Forbidden node check** — Walk the AST for banned constructs
           (``import``, ``from … import``).
        3. **Dangerous call check** — Inspect ``ast.Call`` nodes for
           calls to dangerous functions and attribute patterns.

        Args:
            content: The Python source code to validate.

        Returns:
            An empty list if validation passes.

        Raises:
            ScriptValidationError: If any validation errors are found.
                The ``details['errors']`` key contains the list of error
                strings.
        """
        errors: list[str] = []

        # Step 1: Syntax validation
        try:
            tree: ast.Module = ast.parse(content)
        except SyntaxError as exc:
            error_msg = (
                f"Syntax error at line {exc.lineno}: {exc.msg}"
                if exc.lineno
                else f"Syntax error: {exc.msg}"
            )
            errors.append(error_msg)
            raise ScriptValidationError(
                "Script has syntax errors.",
                details={"errors": errors},
            ) from exc

        # Step 2: Forbidden AST node check
        for node in ast.walk(tree):
            if type(node) in FORBIDDEN_AST_NODES:
                lineno = getattr(node, "lineno", "?")
                errors.append(
                    f"Forbidden statement: {type(node).__name__} "
                    f"at line {lineno}"
                )

        # Step 3: Dangerous function call check
        dangerous_call_errors: list[str] = self._check_dangerous_calls(tree)
        errors.extend(dangerous_call_errors)

        if errors:
            self.logger.warning(
                "Script validation failed with %d error(s): %s",
                len(errors),
                "; ".join(errors),
            )
            raise ScriptValidationError(
                f"Script validation failed with {len(errors)} error(s).",
                details={"errors": errors},
            )

        self.logger.debug("Script validation passed.")
        return errors

    def _check_dangerous_calls(self, tree: ast.AST) -> list[str]:
        """Detect dangerous function calls in an AST.

        Inspects all ``ast.Call`` nodes for:

        - Direct calls to dangerous builtins (e.g., ``eval(…)``,
          ``exec(…)``, ``__import__(…)``, ``open(…)``).
        - Attribute-based calls matching known dangerous patterns
          (e.g., ``os.system(…)``, ``subprocess.run(…)``).

        Args:
            tree: The parsed AST to inspect.

        Returns:
            A list of error strings describing each detected dangerous
            call.  Empty if no dangerous calls are found.
        """
        errors: list[str] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            lineno = getattr(node, "lineno", "?")

            # Check direct function name calls: eval(...), exec(...), etc.
            if isinstance(func, ast.Name):
                if func.id in _DANGEROUS_FUNCTION_NAMES:
                    errors.append(
                        f"Dangerous function call: {func.id}() "
                        f"at line {lineno}"
                    )

            # Check attribute calls: os.system(...), subprocess.run(...), etc.
            elif isinstance(func, ast.Attribute):
                # Build the dotted name by walking the value chain
                attr_name = self._resolve_attribute_name(func)
                if attr_name and attr_name in _DANGEROUS_ATTRIBUTE_CALLS:
                    errors.append(
                        f"Dangerous attribute call: {attr_name}() "
                        f"at line {lineno}"
                    )

        return errors

    @staticmethod
    def _resolve_attribute_name(node: ast.Attribute) -> str | None:
        """Resolve a dotted attribute name from an ``ast.Attribute`` node.

        Walks the ``value`` chain to reconstruct names like
        ``os.system`` or ``subprocess.run``.  Returns ``None`` if the
        chain contains non-Name / non-Attribute nodes.

        Args:
            node: An ``ast.Attribute`` node to resolve.

        Returns:
            The dotted name string, or ``None`` if resolution fails.
        """
        parts: list[str] = [node.attr]
        current: ast.expr = node.value

        while True:
            if isinstance(current, ast.Name):
                parts.append(current.id)
                break
            elif isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            else:
                return None

        parts.reverse()
        return ".".join(parts)

    # ------------------------------------------------------------------
    # Script Execution
    # ------------------------------------------------------------------

    def execute_script(
        self, name: str, args: dict[str, Any] | None = None
    ) -> dict:
        """Execute a stored script by name within a sandboxed environment.

        The script is retrieved from the database, re-validated for
        safety (defence-in-depth), and executed with restricted builtins.
        Standard output (``print()`` calls) is captured and returned.

        Scripts may communicate results by assigning to the ``result``
        variable in their execution namespace::

            # Script content:
            result = args['x'] * 2

        Args:
            name: The script identifier.
            args: Optional dictionary of arguments passed into the script
                execution namespace as ``args``.

        Returns:
            A result dictionary with keys:
            - ``name``        — Script name.
            - ``result``      — The value of ``result`` after execution.
            - ``output``      — Captured stdout output.
            - ``duration_ms`` — Execution duration in milliseconds.
            - ``status``      — ``'success'``.
            - ``execution_id``— Unique identifier for this execution run.

        Raises:
            ScriptError: If the script is not found or scripting is disabled.
            ScriptValidationError: If re-validation fails.
            ScriptExecutionError: If the script raises an exception.
            ScriptTimeoutError: If execution exceeds ``MAX_EXECUTION_TIME``.
        """
        # Enforce scripting enablement — scripts cannot be executed when
        # scripting is disabled in system configuration.
        if not self.is_scripting_enabled():
            raise ScriptError(
                "Scripting is disabled. Enable via system configuration "
                "'system.scripting.enabled' or SCRIPTING_ENABLED env var.",
                details={"name": name, "reason": "scripting_disabled"},
            )

        # Retrieve the script
        script_data: dict | None = self.get_script(name)
        if script_data is None:
            raise ScriptError(
                f"Script '{name}' not found.",
                details={"name": name},
            )

        content: str = script_data.get("content", "")
        execution_id: str = generate_uuid()

        self.logger.info(
            "Executing script '%s' (execution_id=%s).",
            name, execution_id,
        )

        # Defence-in-depth: re-validate before every execution
        self._validate_script(content)

        # Execute in sandbox
        result = self._execute_in_sandbox(
            content=content,
            script_name=name,
            args=args,
            execution_id=execution_id,
        )

        # Emit execution event
        emit_event(
            EventType.CONFIG_CHANGED,
            payload={
                "action": "script_executed",
                "script_name": name,
                "execution_id": execution_id,
                "duration_ms": result["duration_ms"],
                "status": result["status"],
            },
        )

        return result

    def run_inline(
        self, content: str, args: dict[str, Any] | None = None
    ) -> dict:
        """Execute an inline (unsaved) script within a sandboxed environment.

        Useful for one-off administrative tasks that do not need to be
        persisted.  The script is validated before execution.

        Args:
            content: The Python source code to execute.
            args: Optional arguments passed as ``args`` in the script
                namespace.

        Returns:
            A result dictionary with the same structure as
            ``execute_script()``, with ``name`` set to ``'<inline>'``.

        Raises:
            ScriptError: If scripting is disabled.
            ScriptValidationError: If the content fails validation.
            ScriptExecutionError: If the script raises an exception.
            ScriptTimeoutError: If execution exceeds ``MAX_EXECUTION_TIME``.
        """
        # Enforce scripting enablement
        if not self.is_scripting_enabled():
            raise ScriptError(
                "Scripting is disabled. Enable via system configuration "
                "'system.scripting.enabled' or SCRIPTING_ENABLED env var.",
                details={"reason": "scripting_disabled"},
            )

        # Size check
        if len(content.encode("utf-8")) > MAX_SCRIPT_SIZE:
            raise ScriptValidationError(
                f"Inline script exceeds maximum size of "
                f"{MAX_SCRIPT_SIZE} bytes.",
                details={"size": len(content.encode("utf-8")),
                         "limit": MAX_SCRIPT_SIZE},
            )

        self._validate_script(content)

        execution_id: str = generate_uuid()
        self.logger.info(
            "Executing inline script (execution_id=%s).", execution_id,
        )

        result = self._execute_in_sandbox(
            content=content,
            script_name="<inline>",
            args=args,
            execution_id=execution_id,
        )

        return result

    def _execute_in_sandbox(
        self,
        content: str,
        script_name: str,
        args: dict[str, Any] | None,
        execution_id: str,
    ) -> dict:
        """Core sandbox execution logic shared by execute_script and run_inline.

        Builds a restricted execution environment, captures stdout,
        compiles and executes the script in a **separate daemon thread**
        with a pre-emptive timeout.  If the script exceeds
        ``MAX_EXECUTION_TIME`` seconds, the thread is interrupted via
        ``PyThreadState_SetAsyncExc`` and a ``ScriptTimeoutError`` is raised.

        This approach prevents infinite loops and runaway CPU-bound scripts
        from hanging the worker process indefinitely (CWE-400 mitigation).

        Args:
            content: Python source code.
            script_name: Name for error messages and the compiled code object.
            args: Arguments dict exposed as ``args`` in the script namespace.
            execution_id: Unique execution correlation identifier.

        Returns:
            Result dictionary with ``name``, ``result``, ``output``,
            ``duration_ms``, ``status``, and ``execution_id``.

        Raises:
            ScriptExecutionError: On runtime errors.
            ScriptTimeoutError: On timeout violations.
        """
        # Build restricted builtins dict using safe attribute access
        # (we use the module-level __builtins__ reference directly)
        builtins_ref = __builtins__
        raw_builtins: dict[str, Any] = {}
        for builtin_name in ALLOWED_BUILTINS:
            if isinstance(builtins_ref, dict):
                if builtin_name in builtins_ref:
                    raw_builtins[builtin_name] = builtins_ref[builtin_name]
            else:
                obj = getattr(builtins_ref, builtin_name, None)
                if obj is not None:
                    raw_builtins[builtin_name] = obj

        # Inject a safe 'log' helper bound to the script logger
        script_logger = logging.getLogger(f"script.{script_name}")

        def safe_log(message: str) -> None:
            """Log a message from within a script execution context."""
            script_logger.info("[script:%s] %s", script_name, message)

        exec_globals: dict[str, Any] = {
            "__builtins__": raw_builtins,
            "args": args if args is not None else {},
            "result": None,
            "log": safe_log,
        }

        stdout_capture = io.StringIO()

        # Container dicts for thread communication (mutable from inner scope)
        thread_result: dict[str, Any] = {}
        thread_error: dict[str, Any] = {}

        def _run_script() -> None:
            """Execute the compiled script in the restricted globals.

            Runs inside a daemon thread so it can be interrupted on timeout.
            """
            try:
                compiled = compile(
                    content, f"<script:{script_name}>", "exec"
                )
                with contextlib.redirect_stdout(stdout_capture):
                    exec(compiled, exec_globals)  # noqa: S102 — sandboxed exec
                thread_result["success"] = True
            except Exception as exc:
                thread_error["exception"] = exc

        start_time: float = time.monotonic()

        # Run the script in a daemon thread with a timeout join
        script_thread = threading.Thread(
            target=_run_script,
            name=f"script-{execution_id[:8]}",
            daemon=True,
        )
        script_thread.start()
        script_thread.join(timeout=MAX_EXECUTION_TIME)

        elapsed: float = time.monotonic() - start_time

        # Check if the thread is still alive — pre-emptive timeout
        if script_thread.is_alive():
            self.logger.warning(
                "Script '%s' exceeded timeout (%.3fs > %ds); "
                "interrupting thread.",
                script_name, elapsed, MAX_EXECUTION_TIME,
            )

            # Attempt to force-interrupt the thread by injecting a
            # SystemExit exception into the thread's execution frame.
            # This works for Python-level code (loops, function calls)
            # but NOT for blocking C extensions or I/O syscalls.
            try:
                thread_id = script_thread.ident
                if thread_id is not None:
                    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_ulong(thread_id),
                        ctypes.py_object(SystemExit),
                    )
                    if res > 1:
                        # If it returns > 1, the call was invalid — reset it
                        ctypes.pythonapi.PyThreadState_SetAsyncExc(
                            ctypes.c_ulong(thread_id), None
                        )
            except Exception:
                self.logger.debug(
                    "Failed to async-interrupt script thread for '%s'.",
                    script_name,
                )

            raise ScriptTimeoutError(
                f"Script '{script_name}' exceeded maximum execution time "
                f"of {MAX_EXECUTION_TIME} seconds.",
                details={
                    "name": script_name,
                    "execution_id": execution_id,
                    "elapsed_seconds": round(elapsed, 3),
                    "limit_seconds": MAX_EXECUTION_TIME,
                },
            )

        # Check for exceptions raised within the thread
        if "exception" in thread_error:
            exc = thread_error["exception"]
            self.logger.error(
                "Script '%s' execution failed after %.3fs: %s",
                script_name, elapsed, str(exc),
            )
            raise ScriptExecutionError(
                f"Script execution failed: {exc}",
                details={
                    "name": script_name,
                    "execution_id": execution_id,
                    "exception_type": type(exc).__name__,
                    "elapsed_seconds": round(elapsed, 3),
                },
            ) from exc

        duration_ms: int = int(elapsed * 1000)
        output: str = stdout_capture.getvalue()

        self.logger.info(
            "Script '%s' completed in %dms (execution_id=%s).",
            script_name, duration_ms, execution_id,
        )

        return {
            "name": script_name,
            "result": exec_globals.get("result"),
            "output": output,
            "duration_ms": duration_ms,
            "status": "success",
            "execution_id": execution_id,
        }

    # ------------------------------------------------------------------
    # Helper Methods
    # ------------------------------------------------------------------

    def is_scripting_enabled(self) -> bool:
        """Check whether scripting is enabled in system configuration.

        Reads the ``system.scripting.enabled`` configuration key.
        Defaults to ``False`` for security — scripting must be explicitly
        enabled by an administrator.

        Returns:
            ``True`` if scripting is enabled, ``False`` otherwise.
        """
        # Check Flask app config first (allows override via env vars)
        try:
            app_config_value = current_app.config.get(
                "SCRIPTING_ENABLED", None
            )
            if app_config_value is not None:
                if isinstance(app_config_value, bool):
                    return app_config_value
                return str(app_config_value).lower() in (
                    "true", "1", "yes", "on"
                )
        except RuntimeError:
            # Outside of application context — fall through to DB check
            pass

        # Check database configuration
        config_entry = db.session.query(SystemConfig).filter(
            SystemConfig.key == "system.scripting.enabled"
        ).first()

        if config_entry is None or config_entry.value is None:
            return False

        return str(config_entry.value).lower() in ("true", "1", "yes", "on")

    def _build_script_metadata(
        self,
        name: str,
        script_type: str,
        content: str,
        created: datetime,
        updated: datetime,
    ) -> dict:
        """Build a standardised script metadata dictionary.

        Used by CRUD methods to return consistent response shapes
        without exposing the full script content.

        Args:
            name: Script identifier.
            script_type: Script type label.
            content: Script source code (used only for length calculation).
            created: Creation timestamp.
            updated: Last modification timestamp.

        Returns:
            A dictionary with ``name``, ``type``, ``content_length``,
            ``created``, and ``updated`` fields.
        """
        return {
            "name": name,
            "type": script_type,
            "content_length": len(content),
            "created": (
                created.isoformat()
                if isinstance(created, datetime)
                else str(created)
            ),
            "updated": (
                updated.isoformat()
                if isinstance(updated, datetime)
                else str(updated)
            ),
        }


logger.debug("ScriptService module loaded.")
