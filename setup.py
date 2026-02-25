"""
setup.py - Package Installation Configuration

Sonatype Nexus Repository Manager - Python/Flask Backend

This setup.py provides backward compatibility with older Python packaging tools
that do not support PEP 621 (pyproject.toml-only configuration). It reads runtime
dependencies from requirements.txt, development dependencies from
requirements-dev.txt, and the project long description from README.md.

This file replaces the Maven pom.xml build system from the original Java source.
For the modern PEP 621 equivalent, see pyproject.toml.
"""

import os
from setuptools import setup, find_packages

# ---------------------------------------------------------------------------
# Path helpers — resolve all paths relative to this file's directory so that
# ``python setup.py ...`` works correctly regardless of the caller's cwd.
# ---------------------------------------------------------------------------
HERE = os.path.abspath(os.path.dirname(__file__))


def _read_file(filename: str) -> str:
    """Read and return the full text content of a file relative to setup.py.

    Args:
        filename: Name of the file to read, relative to the project root.

    Returns:
        The file contents as a string, or an empty string if the file does
        not exist (graceful degradation during development or CI builds
        where ancillary files may not yet be present).
    """
    filepath = os.path.join(HERE, filename)
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def _parse_requirements(filename: str) -> list[str]:
    """Parse a pip-style requirements file into a list of dependency strings.

    Handles the following pip requirements-file conventions:
    - Blank lines and whitespace-only lines are skipped.
    - Lines beginning with ``#`` (comments) are skipped.
    - Lines beginning with ``-r`` (recursive includes) are skipped because
      setuptools does not support file inclusion directives; the referenced
      file's contents are expected to appear in ``install_requires`` instead.
    - Lines beginning with ``-`` followed by other pip flags (e.g.
      ``--index-url``, ``-e``, ``-f``) are skipped.
    - Inline comments (``package==1.0  # explanation``) are stripped.
    - Leading/trailing whitespace is stripped from each entry.

    Args:
        filename: Name of the requirements file, relative to the project root.

    Returns:
        A list of PEP 508 dependency specifier strings suitable for use in
        ``install_requires`` or ``extras_require``.
    """
    content = _read_file(filename)
    if not content:
        return []

    requirements: list[str] = []
    for line in content.splitlines():
        # Strip leading/trailing whitespace
        line = line.strip()

        # Skip empty lines
        if not line:
            continue

        # Skip comments
        if line.startswith("#"):
            continue

        # Skip pip directives (-r, -e, -f, --index-url, etc.)
        if line.startswith("-"):
            continue

        # Strip inline comments (e.g. "Flask==3.1.3  # core framework")
        if " #" in line:
            line = line[: line.index(" #")].strip()

        # Only include non-empty entries after processing
        if line:
            requirements.append(line)

    return requirements


# ---------------------------------------------------------------------------
# Read project metadata sources
# ---------------------------------------------------------------------------

# Long description from README.md for PyPI and package index display
long_description = _read_file("README.md")

# Runtime dependencies from the pinned requirements manifest
install_requires = _parse_requirements("requirements.txt")

# Development and testing dependencies from the dev requirements manifest
dev_requires = _parse_requirements("requirements-dev.txt")


# ---------------------------------------------------------------------------
# Package setup
# ---------------------------------------------------------------------------
setup(
    # -----------------------------------------------------------------------
    # Core Package Identity
    # Mirrors the [project] section in pyproject.toml for backward compat.
    # -----------------------------------------------------------------------
    name="nexus-repository",
    version="1.0.0",
    description="Sonatype Nexus Repository Manager - Python/Flask Backend",
    long_description=long_description,
    long_description_content_type="text/markdown",

    # -----------------------------------------------------------------------
    # Author and Project Links
    # -----------------------------------------------------------------------
    author="Nexus Repository Team",
    url="https://github.com/sonatype/nexus-repository",
    project_urls={
        "Documentation": "https://github.com/sonatype/nexus-repository/tree/main/docs",
        "Source": "https://github.com/sonatype/nexus-repository",
        "Bug Tracker": "https://github.com/sonatype/nexus-repository/issues",
    },

    # -----------------------------------------------------------------------
    # Python Version Requirement
    # The project targets Python 3.12+ per AAP Section 0.7.2 to leverage
    # modern features: type hints, match statements, dataclasses, async/await.
    # -----------------------------------------------------------------------
    python_requires=">=3.12",

    # -----------------------------------------------------------------------
    # Package Discovery
    # Uses the src-layout pattern: all application source code lives under
    # the ``src/`` directory.  ``find_packages(where="src")`` automatically
    # discovers ``app`` and all nested sub-packages (app.api, app.models,
    # app.services, app.auth, app.formats, app.storage, etc.).
    # -----------------------------------------------------------------------
    packages=find_packages(where="src"),
    package_dir={"": "src"},

    # -----------------------------------------------------------------------
    # Runtime Dependencies
    # Read from requirements.txt — contains all pinned (==) runtime packages
    # matching AAP Section 0.6.1.  Replaces the Maven dependency management
    # from the original Java source's pom.xml.
    # -----------------------------------------------------------------------
    install_requires=install_requires,

    # -----------------------------------------------------------------------
    # Optional / Extra Dependency Groups
    #
    # ``dev``      — Development and testing tools.  Install with:
    #                pip install nexus-repository[dev]
    # ``postgres`` — PostgreSQL adapter for clustered deployments.
    # ``s3``       — AWS S3 SDK for S3 BlobStore backend.
    # ``ldap``     — LDAP/AD directory integration for enterprise auth.
    # ``all``      — Convenience group that installs every optional extra.
    # -----------------------------------------------------------------------
    extras_require={
        "dev": dev_requires,
        "postgres": [
            "psycopg2-binary==2.9.10",
        ],
        "s3": [
            "boto3==1.36.7",
        ],
        "ldap": [
            "python-ldap==3.4.4",
        ],
        "all": [
            "psycopg2-binary==2.9.10",
            "boto3==1.36.7",
            "python-ldap==3.4.4",
        ],
    },

    # -----------------------------------------------------------------------
    # Console Script Entry Points
    # Entry points will be added when the CLI module is implemented.
    # -----------------------------------------------------------------------
    entry_points={},

    # -----------------------------------------------------------------------
    # Package Classifiers (PyPI Trove Classifiers)
    # Describe the project for discovery on PyPI and other package indices.
    # -----------------------------------------------------------------------
    classifiers=[
        "Development Status :: 4 - Beta",
        "Framework :: Flask",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: Eclipse Public License 2.0 (EPL-2.0)",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.12",
        "Topic :: Software Development :: Libraries :: Application Frameworks",
        "Topic :: System :: Software Distribution",
    ],

    # -----------------------------------------------------------------------
    # Additional Package Metadata
    # -----------------------------------------------------------------------
    license="EPL-2.0",
    keywords=[
        "nexus",
        "repository",
        "artifact",
        "maven",
        "npm",
        "docker",
        "nuget",
        "pypi",
        "apt",
        "flask",
    ],
    zip_safe=False,
    include_package_data=True,
)
