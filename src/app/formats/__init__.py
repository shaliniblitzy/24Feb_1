"""
Format handler package for the Nexus Repository Flask application.

Implements format-specific protocol handlers for all 7 supported repository
formats (Feature F-101). Each format is a sub-package containing a handler
and metadata utilities. Replaces OSGi format plugin bundles from the Java source.

Sub-packages:
- ``maven/``:  Maven repository protocol (GAV coordinates, POM, metadata.xml)
- ``npm/``:    npm registry protocol (package.json, scoped packages)
- ``docker/``: Docker Registry API v2 (manifests, layers, tags)
- ``nuget/``:  NuGet V3 API (service index, package content)
- ``pypi/``:   PyPI PEP 503 Simple Repository API
- ``apt/``:    Debian APT repository (Packages index, GPG signatures)
- ``raw/``:    Raw binary file handling with MIME detection
"""
