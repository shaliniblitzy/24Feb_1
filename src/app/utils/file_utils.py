"""
File system utilities for the Nexus Repository Flask application.

Provides safe, robust file operations used primarily by the BlobStore
implementations (file_blobstore.py, s3_blobstore.py) and other modules
that interact with the filesystem. Implements the atomic file write
pattern (temp file + rename) which is critical to the BlobStore's data
integrity guarantees (Feature F-201).

The atomic write pattern replaces the Java equivalent using
``Files.move()`` with ``ATOMIC_MOVE`` option from ``java.nio.file``,
and the temp file management replaces ``java.io.File.createTempFile()``.

Key capabilities:
- Atomic file writes via temp-file-plus-rename pattern
- Streaming atomic write context manager for BlobStore blob persistence
- Secure temporary file and directory creation / cleanup
- Path sanitization to prevent directory traversal attacks
- File-based locking for concurrent BlobStore access
- Recursive directory size calculation for BlobStore reporting

All operations target Python 3.12+ and use modern type hints.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import logging
import time
from pathlib import Path
from typing import BinaryIO, Generator
from contextlib import contextmanager

# Conditional import for Unix file locking — graceful fallback on platforms
# where ``fcntl`` is unavailable (e.g. Windows).
try:
    import fcntl

    _FCNTL_AVAILABLE = True
except ImportError:  # pragma: no cover — platform-dependent
    _FCNTL_AVAILABLE = False

# ---------------------------------------------------------------------------
# Module-level logger (replaces SLF4J 1.7.36 from the Java source)
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default POSIX directory permissions (rwxr-xr-x).
DEFAULT_DIR_PERMISSIONS: int = 0o755

#: Default POSIX file permissions (rw-r--r--).
DEFAULT_FILE_PERMISSIONS: int = 0o644

#: Prefix used for temporary files created by this module.
TEMP_FILE_PREFIX: str = "nexus_tmp_"

#: Suffix used for temporary files created by this module.
TEMP_FILE_SUFFIX: str = ".tmp"

#: Default chunk size (8 KB) for reading/writing operations.
CHUNK_SIZE: int = 8192


# ===================================================================
# Atomic File Write (Critical for BlobStore — Feature F-201)
# ===================================================================

def atomic_write(
    target_path: str | Path,
    data: bytes | BinaryIO,
    permissions: int = DEFAULT_FILE_PERMISSIONS,
) -> None:
    """Write *data* to *target_path* atomically.

    **CRITICAL OPERATION** — used by File BlobStore for blob persistence.

    Algorithm:
    1. Ensure the target directory exists.
    2. Create a temporary file **in the same directory** as the target
       (required for atomic rename on the same filesystem).
    3. Write the data to the temp file.
    4. ``fsync`` the temp file to flush data to disk.
    5. Set file permissions.
    6. Atomically rename the temp file to the target path via
       ``os.replace()``, which is atomic on POSIX when source and target
       are on the same filesystem.
    7. If any step fails, clean up the temp file and re-raise.

    Parameters
    ----------
    target_path:
        Destination file path.
    data:
        Either raw ``bytes`` or a file-like ``BinaryIO`` object.
    permissions:
        POSIX permission bits to set on the final file.

    Raises
    ------
    OSError:
        If any underlying I/O operation fails.
    """
    target = Path(target_path)
    target_dir = target.parent

    # Step 1 — ensure parent directory exists
    ensure_directory(target_dir)

    # Step 2 — create temp file in the *same* directory
    fd: int = -1
    tmp_path: str = ""
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=TEMP_FILE_PREFIX,
            suffix=TEMP_FILE_SUFFIX,
            dir=str(target_dir),
        )

        # Step 3 — write data
        if isinstance(data, bytes):
            os.write(fd, data)
        else:
            # BinaryIO / file-like object — read in chunks
            while True:
                chunk = data.read(CHUNK_SIZE)
                if not chunk:
                    break
                os.write(fd, chunk)

        # Step 4 — flush to disk
        os.fsync(fd)

        # Close the file descriptor before rename
        os.close(fd)
        fd = -1  # mark closed so finally doesn't double-close

        # Step 5 — set permissions
        os.chmod(tmp_path, permissions)

        # Step 6 — atomic rename
        os.replace(tmp_path, str(target))
        tmp_path = ""  # mark consumed so finally doesn't delete

        logger.debug("Atomic write completed: %s", target)

    finally:
        # Guarantee cleanup on any failure
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def atomic_write_text(
    target_path: str | Path,
    content: str,
    encoding: str = "utf-8",
    permissions: int = DEFAULT_FILE_PERMISSIONS,
) -> None:
    """Convenience wrapper that writes a text string atomically.

    Encodes *content* to bytes using *encoding* and delegates to
    :func:`atomic_write`.

    Parameters
    ----------
    target_path:
        Destination file path.
    content:
        Text content to write.
    encoding:
        Character encoding (default ``utf-8``).
    permissions:
        POSIX permission bits to set on the final file.
    """
    atomic_write(target_path, content.encode(encoding), permissions=permissions)


# ===================================================================
# Context Manager for Streaming Atomic Writes
# ===================================================================

@contextmanager
def atomic_write_context(
    target_path: str | Path,
) -> Generator[BinaryIO, None, None]:
    """Context manager for streaming atomic writes.

    Usage::

        with atomic_write_context('/path/to/blob') as f:
            f.write(chunk1)
            f.write(chunk2)
        # File is atomically placed at target on successful exit.

    On successful context exit the temp file is fsynced, closed, and
    atomically renamed to the target.  On exception the temp file is
    removed and the exception is re-raised.

    Yields
    ------
    BinaryIO
        Writable file handle for the temporary staging file.
    """
    target = Path(target_path)
    target_dir = target.parent

    ensure_directory(target_dir)

    fd, tmp_path = tempfile.mkstemp(
        prefix=TEMP_FILE_PREFIX,
        suffix=TEMP_FILE_SUFFIX,
        dir=str(target_dir),
    )

    # Wrap the raw fd in a Python file object for convenience
    tmp_file = os.fdopen(fd, "wb")

    try:
        yield tmp_file

        # Successful exit — commit
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
        tmp_file.close()

        os.chmod(tmp_path, DEFAULT_FILE_PERMISSIONS)
        os.replace(tmp_path, str(target))
        tmp_path = ""  # consumed
        logger.debug("Atomic write context committed: %s", target)

    except BaseException:
        # Failure path — clean up temp file
        if not tmp_file.closed:
            tmp_file.close()
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


# ===================================================================
# Directory Operations
# ===================================================================

def ensure_directory(
    path: str | Path,
    permissions: int = DEFAULT_DIR_PERMISSIONS,
) -> Path:
    """Create a directory (and parents) if it does not already exist.

    Parameters
    ----------
    path:
        Target directory path.
    permissions:
        POSIX permission bits to set on newly created directories.

    Returns
    -------
    Path
        The (now-existing) directory as a :class:`~pathlib.Path`.
    """
    dir_path = Path(path)

    if not dir_path.is_dir():
        os.makedirs(str(dir_path), mode=permissions, exist_ok=True)
        # os.makedirs may be affected by umask — apply explicit chmod
        try:
            os.chmod(str(dir_path), permissions)
        except OSError:
            pass  # best-effort on permission setting
        logger.debug("Directory ensured: %s", dir_path)

    return dir_path


def remove_directory(path: str | Path, force: bool = False) -> bool:
    """Remove a directory.

    Parameters
    ----------
    path:
        Target directory path.
    force:
        If ``True`` remove recursively including contents via
        ``shutil.rmtree()``.  If ``False`` only remove an empty
        directory via ``os.rmdir()``.

    Returns
    -------
    bool
        ``True`` if the directory was removed, ``False`` if it did not
        exist.
    """
    dir_path = Path(path)

    if not dir_path.exists():
        return False

    try:
        if force:
            shutil.rmtree(str(dir_path))
        else:
            os.rmdir(str(dir_path))
        logger.debug("Directory removed: %s (force=%s)", dir_path, force)
        return True
    except PermissionError as exc:
        logger.warning(
            "Permission denied removing directory %s: %s", dir_path, exc
        )
        return False
    except OSError as exc:
        logger.error("Error removing directory %s: %s", dir_path, exc)
        return False


# ===================================================================
# File Size and Info Operations
# ===================================================================

def get_file_size(path: str | Path) -> int:
    """Return the size of a file in bytes.

    Raises
    ------
    FileNotFoundError
        If *path* does not point to an existing file.
    """
    file_path = Path(path)

    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    return os.path.getsize(str(file_path))


def get_directory_size(path: str | Path) -> int:
    """Calculate the total size of all files in a directory recursively.

    Symlinks are **skipped** to prevent infinite loops.

    Parameters
    ----------
    path:
        Root directory to measure.

    Returns
    -------
    int
        Total size in bytes of all regular files under *path*.
    """
    total_size: int = 0
    root_dir = str(Path(path))

    for dirpath, _dirnames, filenames in os.walk(root_dir, followlinks=False):
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            # Skip symlinks to prevent double-counting or infinite loops
            if not os.path.islink(filepath):
                try:
                    total_size += os.path.getsize(filepath)
                except OSError:
                    # File may have been removed between walk and stat
                    pass

    return total_size


def file_exists(path: str | Path) -> bool:
    """Check whether *path* points to an existing regular file."""
    return Path(path).is_file()


def directory_exists(path: str | Path) -> bool:
    """Check whether *path* points to an existing directory."""
    return Path(path).is_dir()


# ===================================================================
# Recursive Directory & File Operations
# ===================================================================

def list_files(
    path: str | Path,
    pattern: str = "*",
    recursive: bool = False,
) -> list[Path]:
    """List files matching a glob pattern in a directory.

    Parameters
    ----------
    path:
        Directory to search.
    pattern:
        Glob pattern (e.g. ``'*.py'``).
    recursive:
        If ``True`` use :meth:`Path.rglob`; otherwise
        :meth:`Path.glob`.

    Returns
    -------
    list[Path]
        Sorted list of matching **file** paths (directories excluded).
    """
    dir_path = Path(path)

    if not dir_path.is_dir():
        return []

    if recursive:
        matches = dir_path.rglob(pattern)
    else:
        matches = dir_path.glob(pattern)

    return sorted(p for p in matches if p.is_file())


def copy_file(
    source: str | Path,
    target: str | Path,
    preserve_metadata: bool = True,
) -> None:
    """Copy a file from *source* to *target*.

    Parameters
    ----------
    source:
        Path to the source file.
    target:
        Destination path (parent directories are created automatically).
    preserve_metadata:
        If ``True`` use ``shutil.copy2`` (preserves timestamps);
        otherwise use ``shutil.copy``.
    """
    target_path = Path(target)
    ensure_directory(target_path.parent)

    if preserve_metadata:
        shutil.copy2(str(source), str(target_path))
    else:
        shutil.copy(str(source), str(target_path))


def move_file(source: str | Path, target: str | Path) -> None:
    """Move / rename a file from *source* to *target*.

    Handles cross-filesystem moves transparently via ``shutil.move``.
    Parent directories of *target* are created automatically.
    """
    target_path = Path(target)
    ensure_directory(target_path.parent)
    shutil.move(str(source), str(target_path))
    logger.debug("File moved: %s -> %s", source, target_path)


# ===================================================================
# Temporary File Management
# ===================================================================

def create_temp_file(
    prefix: str = TEMP_FILE_PREFIX,
    suffix: str = TEMP_FILE_SUFFIX,
    directory: str | Path | None = None,
) -> Path:
    """Create a secure temporary file and return its path.

    Uses ``tempfile.mkstemp`` which creates the file atomically to
    prevent race conditions (replaces ``java.io.File.createTempFile``).

    The caller is responsible for deleting the file when done.

    Parameters
    ----------
    prefix:
        Filename prefix.
    suffix:
        Filename suffix.
    directory:
        Directory to place the temp file in; uses the system default
        if ``None``.

    Returns
    -------
    Path
        Absolute path to the newly created temporary file.
    """
    dir_str: str | None = str(directory) if directory is not None else None
    fd, tmp_path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=dir_str)
    os.close(fd)
    return Path(tmp_path)


def create_temp_directory(
    prefix: str = TEMP_FILE_PREFIX,
    directory: str | Path | None = None,
) -> Path:
    """Create a secure temporary directory and return its path.

    Uses ``tempfile.mkdtemp`` to prevent race conditions.

    The caller is responsible for removing the directory when done.

    Parameters
    ----------
    prefix:
        Directory name prefix.
    directory:
        Parent directory; uses the system default if ``None``.

    Returns
    -------
    Path
        Absolute path to the newly created temporary directory.
    """
    dir_str: str | None = str(directory) if directory is not None else None
    tmp_dir = tempfile.mkdtemp(prefix=prefix, dir=dir_str)
    return Path(tmp_dir)


@contextmanager
def temp_file_context(
    prefix: str = TEMP_FILE_PREFIX,
    suffix: str = TEMP_FILE_SUFFIX,
    directory: str | Path | None = None,
) -> Generator[Path, None, None]:
    """Context manager that creates a temporary file and auto-cleans on exit.

    Usage::

        with temp_file_context() as tmp_path:
            tmp_path.write_bytes(b'data')
        # tmp_path is automatically deleted here

    Yields
    ------
    Path
        Path to the temporary file.
    """
    tmp_path = create_temp_file(prefix=prefix, suffix=suffix, directory=directory)
    try:
        yield tmp_path
    finally:
        try:
            if tmp_path.exists():
                os.unlink(str(tmp_path))
        except OSError as exc:
            logger.warning("Failed to clean up temp file %s: %s", tmp_path, exc)


# ===================================================================
# Path Sanitization (SECURITY CRITICAL)
# ===================================================================

def sanitize_path(path: str) -> str:
    """Sanitize a filesystem path to prevent directory traversal attacks.

    **SECURITY CRITICAL** — used by BlobStore and asset API routes.

    Processing steps:
    1. Strip leading / trailing whitespace.
    2. Remove null bytes and control characters.
    3. Normalize backslashes to forward slashes.
    4. Apply ``os.path.normpath`` to collapse ``./`` and redundant
       separators.
    5. Verify no ``..`` components remain — reject if found.
    6. Strip leading slashes so the result is always relative.

    Parameters
    ----------
    path:
        Raw untrusted path string.

    Returns
    -------
    str
        Sanitized relative path with forward-slash separators.

    Raises
    ------
    ValueError
        If the path contains traversal sequences that cannot be safely
        removed.
    """
    if not path:
        return ""

    # Step 1 — strip whitespace
    sanitized = path.strip()

    # Step 2 — remove null bytes and control characters (ASCII 0–31)
    sanitized = "".join(ch for ch in sanitized if ord(ch) >= 32)

    # Step 3 — normalise separators to forward slashes
    sanitized = sanitized.replace("\\", "/")

    # Step 4 — normalise with os.path.normpath (collapses ./ and //)
    sanitized = os.path.normpath(sanitized)

    # Normalize back to forward-slash after normpath (which may use os.sep)
    sanitized = sanitized.replace(os.sep, "/")

    # Step 5 — reject any remaining traversal components
    parts = sanitized.split("/")
    if ".." in parts:
        raise ValueError(
            f"Path contains directory traversal sequences: {path!r}"
        )

    # Step 6 — strip leading slashes to ensure a relative result
    sanitized = sanitized.lstrip("/")

    # Collapse to empty string rather than "."
    if sanitized == ".":
        sanitized = ""

    return sanitized


def safe_join(base: str | Path, *paths: str) -> Path:
    """Safely join path components, preventing directory traversal.

    Resolves the joined path and verifies it is still contained within
    *base*.

    Parameters
    ----------
    base:
        Trusted base directory path.
    *paths:
        Untrusted path components to append.

    Returns
    -------
    Path
        Resolved absolute path that is guaranteed to be under *base*.

    Raises
    ------
    ValueError
        If the resolved path escapes the base directory.
    """
    base_resolved = Path(base).resolve()

    # Join all components
    joined = base_resolved
    for p in paths:
        # Use os.path.join so that an absolute path in *paths* doesn't
        # silently replace the base.
        joined = Path(os.path.join(str(joined), p))

    joined_resolved = joined.resolve()

    # Ensure the resolved path starts with the base directory
    try:
        joined_resolved.relative_to(base_resolved)
    except ValueError:
        raise ValueError(
            f"Path escapes base directory: resolved={joined_resolved}, "
            f"base={base_resolved}"
        )

    return joined_resolved


# ===================================================================
# File Locking (for concurrent BlobStore access)
# ===================================================================

@contextmanager
def file_lock(
    path: str | Path,
    exclusive: bool = True,
    timeout: float = 30.0,
) -> Generator[None, None, None]:
    """File-based lock using ``fcntl.flock`` on Unix.

    Parameters
    ----------
    path:
        Path to the lock file.  The file is created if it does not
        exist.
    exclusive:
        ``True`` for an exclusive (write) lock (``LOCK_EX``);
        ``False`` for a shared (read) lock (``LOCK_SH``).
    timeout:
        Maximum seconds to wait for the lock.  Raises ``TimeoutError``
        on expiry.

    Yields
    ------
    None
        Control returns to the caller while the lock is held.

    Raises
    ------
    TimeoutError
        If the lock cannot be acquired within *timeout* seconds.
    OSError
        If ``fcntl`` is unavailable on the current platform (Windows).
    """
    if not _FCNTL_AVAILABLE:
        # Fallback: on platforms without fcntl, yield immediately.
        # Documented limitation — true locking requires Unix.
        logger.warning(
            "fcntl unavailable on this platform; file_lock is a no-op for %s",
            path,
        )
        yield
        return

    lock_path = Path(path)
    ensure_directory(lock_path.parent)

    lock_fd = open(str(lock_path), "a+")  # noqa: SIM115
    lock_flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH

    deadline = time.monotonic() + timeout
    acquired = False

    try:
        while True:
            try:
                fcntl.flock(lock_fd.fileno(), lock_flag | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                # Lock is held by another process — retry after a short sleep
                if time.monotonic() >= deadline:
                    logger.error(
                        "Timeout acquiring file lock on %s after %.1fs",
                        lock_path,
                        timeout,
                    )
                    raise TimeoutError(
                        f"Could not acquire file lock on {lock_path} "
                        f"within {timeout}s"
                    )
                time.sleep(0.05)

        yield

    finally:
        if acquired:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        lock_fd.close()
