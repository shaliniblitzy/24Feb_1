# Repository Management API Reference

This document provides the complete API reference for all repository management REST API endpoints in the Nexus Repository Manager Python/Flask application. It covers the following features:

- **F-101 — Multi-Format Artifact Support:** Seven repository formats (Maven, npm, Docker, NuGet, PyPI, APT, Raw) with format-native protocol endpoints
- **F-102 — Three Repository Types:** Hosted (local storage), Proxy (upstream cache with negative caching), Group (virtual aggregation)
- **F-103 — Content Indexing and Search:** Full-text search across all indexed components and assets
- **F-104 — Browse Tree Navigation:** Hierarchical tree traversal of repository contents

**Endpoint Prefixes:**

| Category | Base Path | Description |
|----------|-----------|-------------|
| Repository Management | `/service/rest/v1/repositories/` | CRUD operations for repository configuration |
| Component Management | `/service/rest/v1/components/` | Component metadata and lifecycle |
| Asset Management | `/service/rest/v1/assets/` | Asset metadata and downloads |
| Content Search | `/service/rest/v1/search/` | Full-text search queries |
| Browse Navigation | `/service/rest/v1/browse/` | Hierarchical content browsing |
| BlobStore Management | `/service/rest/v1/blobstores/` | Binary storage backend configuration |
| Format-Native Protocols | `/repository/{repo_name}/` | Format-specific artifact access |
| Docker Registry API | `/v2/{repo_name}/` | Docker Registry HTTP API V2 |

**Implementation References:**

- Routes: `src/repositories/routes.py`
- Services: `src/repositories/services.py`
- Format Handlers: `src/repositories/formats/` (maven, npm, docker, nuget, pypi, apt, raw)
- Type Implementations: `src/repositories/types/` (hosted, proxy, group)
- Search Service: `src/repositories/search.py`
- Browse Service: `src/repositories/browse.py`

**Supported Formats:** Maven (`maven2`), npm (`npm`), Docker (`docker`), NuGet (`nuget`), PyPI (`pypi`), APT (`apt`), Raw (`raw`)

**Supported Repository Types:** `hosted`, `proxy`, `group`

---

## Table of Contents

1. [Repository Management (CRUD)](#repository-management)
2. [Repository Lifecycle](#repository-lifecycle)
3. [Maven Repository Protocol](#maven-repository-protocol-f-101)
4. [npm Registry Protocol](#npm-registry-protocol-f-101)
5. [Docker Registry HTTP API V2](#docker-registry-http-api-v2-f-101)
6. [NuGet V3 Protocol](#nuget-v3-protocol-f-101)
7. [PyPI Simple Repository API](#pypi-simple-repository-api-f-101)
8. [APT Repository Protocol](#apt-repository-protocol-f-101)
9. [Raw Repository Protocol](#raw-repository-protocol-f-101)
10. [Repository Types (F-102)](#repository-types-f-102)
11. [Component Management](#component-management)
12. [Asset Management](#asset-management)
13. [Content Search (F-103)](#content-search-f-103)
14. [Browse Tree Navigation (F-104)](#browse-tree-navigation-f-104)
15. [BlobStore Configuration](#blobstore-configuration)
16. [Data Models](#data-models)
17. [Error Responses](#error-responses)
18. [Related Documentation](#related-documentation)

---

## Repository Management

REST API endpoints for repository CRUD operations. All management endpoints require authentication and appropriate privileges.

### List All Repositories

```
GET /service/rest/v1/repositories
```

**Authentication:** Required — `nx-admin` role or `repositories-read` privilege

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `format` | string | No | Filter by format (`maven2`, `npm`, `docker`, `nuget`, `pypi`, `apt`, `raw`) |
| `type` | string | No | Filter by type (`hosted`, `proxy`, `group`) |

**Response: `200 OK`**

```json
{
  "items": [
    {
      "name": "maven-central",
      "format": "maven2",
      "type": "proxy",
      "url": "https://nexus.example.com/repository/maven-central",
      "online": true,
      "status": "STARTED",
      "attributes": {}
    },
    {
      "name": "maven-releases",
      "format": "maven2",
      "type": "hosted",
      "url": "https://nexus.example.com/repository/maven-releases",
      "online": true,
      "status": "STARTED",
      "attributes": {}
    }
  ]
}
```

---

### Create a Repository

```
POST /service/rest/v1/repositories/{format}/{type}
```

**Authentication:** Required — `nx-admin` role or `repositories-create` privilege

**Path Parameters:**

| Parameter | Type | Description | Values |
|-----------|------|-------------|--------|
| `format` | string | Repository format | `maven2`, `npm`, `docker`, `nuget`, `pypi`, `apt`, `raw` |
| `type` | string | Repository type | `hosted`, `proxy`, `group` |

**Hosted Repository Request Body:**

```json
{
  "name": "maven-releases",
  "online": true,
  "storage": {
    "blobstore_name": "default",
    "write_policy": "ALLOW_ONCE"
  },
  "maven": {
    "version_policy": "RELEASE",
    "layout_policy": "STRICT"
  }
}
```

**Proxy Repository Request Body:**

```json
{
  "name": "maven-central",
  "online": true,
  "proxy": {
    "remote_url": "https://repo1.maven.org/maven2/",
    "content_max_age": 1440,
    "metadata_max_age": 1440
  },
  "negative_cache": {
    "enabled": true,
    "time_to_live": 1440
  },
  "storage": {
    "blobstore_name": "default"
  },
  "http_client": {
    "connection_timeout": 20,
    "socket_timeout": 20,
    "auto_block": true
  }
}
```

**Group Repository Request Body:**

```json
{
  "name": "maven-public",
  "online": true,
  "group": {
    "member_names": ["maven-releases", "maven-snapshots", "maven-central"]
  },
  "storage": {
    "blobstore_name": "default"
  }
}
```

**Response: `201 Created`**

Returns the created repository configuration.

**Write Policies:**

| Policy | Description |
|--------|-------------|
| `ALLOW` | Allow any writes and overwrites of existing artifacts |
| `ALLOW_ONCE` | Allow initial deploy only; subsequent uploads to the same coordinate are rejected |
| `DENY` | Read-only repository; all write operations are denied |

---

### Get Repository Configuration

```
GET /service/rest/v1/repositories/{name}
```

**Authentication:** Required — `nx-admin` role or `repositories-read` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Repository name |

**Response: `200 OK`**

Returns the full repository configuration object including format-specific and type-specific attributes.

**Response: `404 Not Found`** — Repository does not exist.

---

### Update Repository Configuration

```
PUT /service/rest/v1/repositories/{name}
```

**Authentication:** Required — `nx-admin` role or `repositories-update` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Repository name |

**Request Body:** Same structure as the corresponding create request for the repository's format and type. The `name` field cannot be changed.

**Response: `200 OK`** — Repository configuration updated.

**Response: `404 Not Found`** — Repository does not exist.

---

### Delete a Repository

```
DELETE /service/rest/v1/repositories/{name}
```

**Authentication:** Required — `nx-admin` role or `repositories-delete` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Repository name |

**Response: `204 No Content`** — Repository deleted.

**Response: `404 Not Found`** — Repository does not exist.

**Response: `400 Bad Request`** — Repository is a member of a group repository and cannot be deleted until removed from the group.

Deletion transitions the repository through `STOPPED` → `DELETED` states. The `repository_deleted` signal is emitted, triggering audit logging, webhook notifications, and search index cleanup. Binary artifacts in the BlobStore follow the soft-delete pattern and are reclaimed during the next compaction cycle.

---

## Repository Lifecycle

Repositories transition through a well-defined lifecycle state machine. State transitions are validated, and invalid transitions raise a `ConfigError` (HTTP 500).

**State Transition Diagram:**

```
NEW → INITIALIZING → STARTED → STOPPED → DELETED
                  ↘         ↗
                   FAILED
```

**States:**

| State | Description |
|-------|-------------|
| `NEW` | Repository configuration created but not yet initialized |
| `INITIALIZING` | Storage backend and search indexing are being set up |
| `STARTED` | Repository is online and actively serving requests |
| `STOPPED` | Repository is offline; all read/write requests are rejected |
| `FAILED` | Initialization or operation failed; can retry → `INITIALIZING` |
| `DELETED` | Repository and its associated data have been removed |

**Valid State Transitions:**

| From | To | Trigger |
|------|----|---------|
| `NEW` | `INITIALIZING` | Repository creation initiated |
| `INITIALIZING` | `STARTED` | Initialization completed successfully |
| `INITIALIZING` | `FAILED` | Initialization error occurred |
| `STARTED` | `STOPPED` | Administrator stops repository |
| `STOPPED` | `STARTED` | Administrator starts repository |
| `STOPPED` | `DELETED` | Administrator deletes repository |
| `FAILED` | `INITIALIZING` | Administrator retries initialization |

**Invalid state transitions raise `ConfigError` (HTTP 500).**

**Lifecycle Signals:**

| Signal | Emitted When | Subscribers |
|--------|-------------|-------------|
| `repository_created` | Transition to `STARTED` | Audit logging, webhooks |
| `repository_deleted` | Transition to `DELETED` | Audit logging, webhooks, search index cleanup |

---

## Maven Repository Protocol (F-101)

Maven repositories follow the standard Maven repository layout for POM resolution, JAR download, metadata XML generation, snapshot versioning, and checksum verification.

**Endpoint Base:** `/repository/{repo_name}/`

**Implementation:** `src/repositories/formats/maven.py`

### Download Artifact

```
GET /repository/{repo_name}/{path}
```

Download a Maven artifact by its full path (JAR, POM, metadata XML, checksums).

**Authentication:** Required — `read` permission on the repository

**Examples:**

```
GET /repository/maven-central/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar
GET /repository/maven-central/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.pom
GET /repository/maven-central/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar.sha256
GET /repository/maven-central/org/apache/commons/commons-lang3/maven-metadata.xml
```

**Response: `200 OK`** — Binary content with appropriate `Content-Type` header.

**Response: `404 Not Found`** — Artifact does not exist.

For proxy repositories, a cache miss triggers an upstream fetch from the configured `remote_url`. The fetched artifact is cached locally in the BlobStore for subsequent requests.

### Upload Artifact

```
PUT /repository/{repo_name}/{path}
```

Upload a Maven artifact to a hosted repository.

**Authentication:** Required — `write` permission on the repository

**Request Body:** Binary artifact content with `Content-Type` header.

**Response: `201 Created`** — Artifact uploaded and indexed.

**Response: `400 Bad Request`** — Write policy violation (e.g., overwrite rejected by `ALLOW_ONCE`).

**Response: `405 Method Not Allowed`** — Target is a proxy or group repository.

### Maven-Specific Features

- **POM Resolution:** Automatic POM file parsing and dependency metadata extraction for search indexing
- **Metadata XML Generation:** `maven-metadata.xml` is auto-generated for group-level and version-level listings, aggregating version information across all deployed artifacts
- **Snapshot Versioning:** Timestamped snapshot versions (e.g., `1.0-20260224.100000-1`) with `maven-metadata.xml` tracking the latest snapshot build
- **Checksum Verification:** SHA-1, SHA-256, and MD5 checksums are generated automatically on upload and verified on download; checksum files (`.sha1`, `.sha256`, `.md5`) are served alongside artifacts
- **Version Policies:** `RELEASE` (no snapshots allowed), `SNAPSHOT` (only snapshots allowed), `MIXED` (both release and snapshot artifacts)
- **Layout Policies:** `STRICT` (enforce Maven 2 standard layout), `PERMISSIVE` (allow non-standard paths)

---

## npm Registry Protocol (F-101)

npm repositories implement the npm registry protocol for Node.js package management, including packument retrieval, tarball download, package publishing, and search.

**Endpoint Base:** `/repository/{repo_name}/`

**Implementation:** `src/repositories/formats/npm.py`

### Get Packument

```
GET /repository/{repo_name}/{package_name}
```

Retrieve the packument (package document) containing all version metadata for a package.

**Authentication:** Required — `read` permission on the repository

**Examples:**

```
GET /repository/npm-proxy/lodash
GET /repository/npm-proxy/@angular/core
```

**Response: `200 OK`** — npm packument JSON with version history, dist-tags, and tarball URLs.

### Download Tarball

```
GET /repository/{repo_name}/-/{tarball_filename}
```

Download a package tarball.

**Example:**

```
GET /repository/npm-proxy/-/lodash-4.17.21.tgz
```

**Response: `200 OK`** — Binary tarball content.

### Publish Package

```
PUT /repository/{repo_name}/{package_name}
```

Publish a new package or version to a hosted repository.

**Authentication:** Required — `write` permission on the repository

**Request Body:** npm publish payload containing packument metadata and tarball data.

**Response: `201 Created`** — Package published.

**Response: `405 Method Not Allowed`** — Target is a proxy or group repository.

### Search Packages

```
GET /repository/{repo_name}/-/v1/search
```

Search for packages using the npm search API.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `text` | string | Full-text search query |
| `size` | integer | Maximum number of results to return |
| `from` | integer | Offset for pagination |

**Response: `200 OK`**

```json
{
  "objects": [
    {
      "package": {
        "name": "lodash",
        "version": "4.17.21",
        "description": "Lodash modular utilities.",
        "keywords": ["modules", "stdlib"],
        "links": {
          "npm": "https://www.npmjs.com/package/lodash"
        }
      },
      "score": { "final": 0.95 }
    }
  ],
  "total": 1,
  "time": "2026-02-24T10:00:00Z"
}
```

### npm-Specific Features

- **Scoped Packages:** Full support for `@scope/package` naming convention
- **Packument Caching:** Complete package metadata cached for proxy repositories with configurable TTL
- **dist-tags:** Support for `latest`, `next`, and custom dist-tags for version aliasing
- **Deprecation Markers:** Support for marking packages and versions as deprecated
- **Tarball Integrity:** SHA-1 and SHA-512 integrity hashes included in packument metadata

---

## Docker Registry HTTP API V2 (F-101)

Docker repositories implement the Docker Registry HTTP API V2 for container image management, supporting manifest push/pull, blob (layer) upload/download, tag listing, and catalog operations.

**Endpoint Base:** `/v2/{repo_name}/`

**Implementation:** `src/repositories/formats/docker.py`

### API Version Check

```
GET /v2/
```

Verify API version compatibility. Returns `200 OK` with `Docker-Distribution-Api-Version: registry/2.0` header.

### Pull Manifest

```
GET /v2/{repo_name}/manifests/{reference}
```

Pull a container image manifest by tag name or content digest.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `repo_name` | string | Docker repository name |
| `reference` | string | Tag name (e.g., `latest`) or digest (e.g., `sha256:abc123...`) |

**Request Headers:**

| Header | Value |
|--------|-------|
| `Accept` | `application/vnd.docker.distribution.manifest.v2+json` |
| `Accept` | `application/vnd.oci.image.manifest.v1+json` |
| `Accept` | `application/vnd.docker.distribution.manifest.list.v2+json` |
| `Accept` | `application/vnd.oci.image.index.v1+json` |

**Response: `200 OK`** — Manifest JSON with `Docker-Content-Digest` response header.

### Push Manifest

```
PUT /v2/{repo_name}/manifests/{reference}
```

Push a container image manifest to a hosted repository.

**Authentication:** Required — `write` permission on the repository

**Response: `201 Created`** — Manifest stored with `Location` header containing the canonical manifest URL.

### Pull Blob

```
GET /v2/{repo_name}/blobs/{digest}
```

Pull a blob (image layer) by its content digest.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `digest` | string | Content digest (e.g., `sha256:abc123...`) |

**Response: `200 OK`** — Binary blob content with `Docker-Content-Digest` header.

### Initiate Blob Upload

```
POST /v2/{repo_name}/blobs/uploads/
```

Start a new blob upload session.

**Response: `202 Accepted`** — Upload session created with `Location` header containing the upload URL and `Docker-Upload-UUID` header.

### Upload Blob Chunk

```
PATCH /v2/{repo_name}/blobs/uploads/{uuid}
```

Upload a chunk of blob data to an active upload session.

**Request Headers:**

| Header | Value |
|--------|-------|
| `Content-Type` | `application/octet-stream` |
| `Content-Range` | `{start}-{end}` |

**Response: `202 Accepted`** — Chunk received; `Location` header updated for next chunk.

### Complete Blob Upload

```
PUT /v2/{repo_name}/blobs/uploads/{uuid}?digest={digest}
```

Finalize a blob upload by providing the complete content digest.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `digest` | string | Complete content digest (`sha256:...`) |

**Response: `201 Created`** — Blob stored with `Docker-Content-Digest` header.

### List Tags

```
GET /v2/{repo_name}/tags/list
```

List all tags for a Docker repository.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `n` | integer | Maximum number of tags to return |
| `last` | string | Last tag from previous page for pagination |

**Response: `200 OK`**

```json
{
  "name": "my-app",
  "tags": ["latest", "v1.0.0", "v1.1.0"]
}
```

### List Repositories (Catalog)

```
GET /v2/_catalog
```

List all Docker-format repositories.

**Response: `200 OK`**

```json
{
  "repositories": ["my-app", "my-lib", "base-image"]
}
```

### Docker-Specific Features

- **Manifest v2s2 and OCI:** Support for Docker manifest schema 2 and OCI image manifest formats
- **Multi-Architecture Images:** Manifest list (Docker) and image index (OCI) support for multi-platform images
- **Content-Addressable Storage:** All blobs are identified and deduplicated by SHA-256 content digest
- **Tag Immutability:** Configurable tag immutability for hosted repositories to prevent tag overwrites
- **Chunked Uploads:** Support for streaming and chunked blob uploads for large image layers
- **Cross-Repository Blob Mounting:** Blob mount support for efficient cross-repository image promotion

---

## NuGet V3 Protocol (F-101)

NuGet repositories implement the NuGet V3 protocol for .NET package management, providing a discoverable service index, package registration, flat container downloads, and full-text search.

**Endpoint Base:** `/repository/{repo_name}/`

**Implementation:** `src/repositories/formats/nuget.py`

### Service Index

```
GET /repository/{repo_name}/index.json
```

NuGet V3 service index — the protocol entry point for all NuGet client operations.

**Response: `200 OK`**

```json
{
  "version": "3.0.0",
  "resources": [
    {
      "@id": "https://nexus.example.com/repository/nuget-proxy/query",
      "@type": "SearchQueryService"
    },
    {
      "@id": "https://nexus.example.com/repository/nuget-proxy/registration/",
      "@type": "RegistrationsBaseUrl"
    },
    {
      "@id": "https://nexus.example.com/repository/nuget-proxy/flatcontainer/",
      "@type": "PackageBaseAddress/3.0.0"
    },
    {
      "@id": "https://nexus.example.com/repository/nuget-proxy/",
      "@type": "PackagePublish/2.0.0"
    }
  ]
}
```

### Search Packages

```
GET /repository/{repo_name}/query
```

Search for NuGet packages.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `q` | string | Search query |
| `skip` | integer | Number of results to skip |
| `take` | integer | Number of results to return |
| `prerelease` | boolean | Include prerelease versions |

**Response: `200 OK`** — NuGet search response with package metadata.

### Package Registration

```
GET /repository/{repo_name}/registration/{id}/index.json
```

Get the registration index for a package, listing all available versions.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Package ID (case-insensitive) |

**Response: `200 OK`** — Registration index with inline version catalog.

### Download Package

```
GET /repository/{repo_name}/flatcontainer/{id}/{version}/{id}.{version}.nupkg
```

Download a NuGet package file.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Package ID (lowercase) |
| `version` | string | Package version |

**Response: `200 OK`** — Binary `.nupkg` file.

### Push Package

```
PUT /repository/{repo_name}/
```

Push a NuGet package to a hosted repository.

**Authentication:** Required — `write` permission on the repository

**Request Body:** Multipart form data containing the `.nupkg` file.

**Response: `201 Created`** — Package published.

### NuGet-Specific Features

- **V3 Service Index:** Discoverable service endpoints per the NuGet V3 protocol specification
- **Package Registration:** Inline version catalogs for each package ID with dependency information
- **Flat Container:** Direct package download by ID and version for efficient restore operations
- **Search:** Full-text package search with prerelease filtering and pagination
- **Symbol Packages:** Support for `.snupkg` symbol package uploads and hosting

---

## PyPI Simple Repository API (F-101)

PyPI repositories implement PEP 503 (Simple Repository API) for Python package management, supporting package index generation, file uploads via `twine`, and metadata extraction.

**Endpoint Base:** `/repository/{repo_name}/`

**Implementation:** `src/repositories/formats/pypi.py`

### Simple Index

```
GET /repository/{repo_name}/simple/
```

Root index page listing all available packages. Returns an HTML page with links to each package.

**Response: `200 OK`** — HTML page with package links per PEP 503.

### Package Index

```
GET /repository/{repo_name}/simple/{package_name}/
```

Package-specific index page listing all available files and versions for a package.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `package_name` | string | Normalized package name (PEP 503 normalization) |

**Response: `200 OK`** — HTML page with file download links including `data-requires-python` attributes and hash fragment identifiers.

### Download Package File

```
GET /repository/{repo_name}/packages/{filename}
```

Download a package file (wheel, source distribution, or egg).

**Response: `200 OK`** — Binary package file.

### Upload Package

```
POST /repository/{repo_name}/
```

Upload a package file to a hosted repository. Compatible with `twine upload`.

**Authentication:** Required — `write` permission on the repository

**Request Body:** Multipart form data containing the package file and metadata fields (`:action`, `name`, `version`, `filetype`, `content`).

**Response: `200 OK`** — Package uploaded and indexed.

### PyPI-Specific Features

- **PEP 503 Compliance:** Full compliance with PEP 503 Simple Repository API for `pip` compatibility
- **Package Index Generation:** Automatic HTML index page generation for root and per-package listings
- **Multiple File Types:** Support for wheels (`.whl`), source distributions (`.tar.gz`, `.zip`), and eggs (`.egg`)
- **Metadata Extraction:** Automatic extraction of package metadata from `PKG-INFO` and `METADATA` files within uploaded packages
- **Normalized Names:** Package names normalized per PEP 503 rules (lowercase, hyphens replaced with hyphens)
- **Hash Fragments:** Download links include `#sha256=...` hash fragments for integrity verification

---

## APT Repository Protocol (F-101)

APT repositories implement the Debian repository format for `.deb` package management, supporting Packages/Sources indices, GPG-signed Release files, and component-based organization.

**Endpoint Base:** `/repository/{repo_name}/`

**Implementation:** `src/repositories/formats/apt.py`

### Release File

```
GET /repository/{repo_name}/dists/{distribution}/Release
```

Repository metadata file containing checksums of all index files.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `distribution` | string | Distribution codename (e.g., `bionic`, `focal`, `jammy`) |

**Response: `200 OK`** — Release file in Debian control format.

### Signed Release File

```
GET /repository/{repo_name}/dists/{distribution}/InRelease
```

GPG inline-signed Release file for secure repository verification.

**Response: `200 OK`** — InRelease file with inline GPG signature.

### Packages Index

```
GET /repository/{repo_name}/dists/{distribution}/{component}/binary-{arch}/Packages
```

Package index listing all `.deb` packages for a specific component and architecture.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `distribution` | string | Distribution codename |
| `component` | string | Component name (e.g., `main`, `contrib`, `non-free`) |
| `arch` | string | Architecture (e.g., `amd64`, `arm64`, `i386`, `all`) |

**Response: `200 OK`** — Packages index in Debian control format. Also available as `Packages.gz` (gzip compressed) and `Packages.xz` (xz compressed).

### Download Package

```
GET /repository/{repo_name}/pool/{component}/{path}/{filename}.deb
```

Download a `.deb` package file.

**Response: `200 OK`** — Binary `.deb` package.

### Upload Package

```
POST /repository/{repo_name}/
```

Upload a `.deb` package to a hosted repository.

**Authentication:** Required — `write` permission on the repository

**Request Body:** Multipart form data containing the `.deb` file.

**Response: `201 Created`** — Package uploaded. Packages index is regenerated automatically.

### APT-Specific Features

- **Packages/Sources Index:** Auto-generated package indices per component and architecture combination
- **Release/InRelease Signing:** GPG-signed Release files for APT repository verification (`apt-key` / `signed-by`)
- **Component Organization:** Packages organized by distribution, component, and architecture hierarchy
- **Debian Package Parsing:** Automatic metadata extraction from `.deb` control files (package name, version, architecture, dependencies)
- **Compressed Indices:** Packages index served in plain text, gzip, and xz compressed formats

---

## Raw Repository Protocol (F-101)

Raw repositories provide generic content storage with path-based access, suitable for storing arbitrary binary files that do not fit a specific package format.

**Endpoint Base:** `/repository/{repo_name}/`

**Implementation:** `src/repositories/formats/raw.py`

### Download File

```
GET /repository/{repo_name}/{path}
```

Download a file by its full path.

**Authentication:** Required — `read` permission on the repository

**Response: `200 OK`** — Binary file content with automatically detected `Content-Type` header.

**Response: `404 Not Found`** — File does not exist at the specified path.

### Upload File

```
PUT /repository/{repo_name}/{path}
```

Upload a file to a specific path in a hosted repository.

**Authentication:** Required — `write` permission on the repository

**Request Body:** Binary file content.

**Response: `201 Created`** — File uploaded.

**Response: `405 Method Not Allowed`** — Target is a proxy or group repository.

### Delete File

```
DELETE /repository/{repo_name}/{path}
```

Delete a file from a hosted repository.

**Authentication:** Required — `delete` permission on the repository

**Response: `204 No Content`** — File deleted.

### Raw-Specific Features

- **Path-Based Access:** Simple, intuitive URL structure mapping directly to storage paths
- **Any Content Type:** Stores any file type without format-specific processing or metadata extraction
- **Directory Listing:** Optional directory index generation for browsing raw content
- **Content-Addressable Storage:** Binary content is deduplicated via SHA-256 hashing in the BlobStore

---

## Repository Types (F-102)

Three repository types are supported, each providing distinct artifact lifecycle behavior.

### Hosted Repositories

Local artifact storage with direct upload capability.

| Feature | Description |
|---------|-------------|
| **Storage** | Artifacts uploaded directly via format-native protocol or REST API |
| **BlobStore** | Binary content stored in configured BlobStore (File or S3) |
| **Indexing** | Metadata automatically indexed for search on upload |
| **Write Policies** | Control overwrite behavior: `ALLOW`, `ALLOW_ONCE`, `DENY` |
| **Deduplication** | Component deduplication via content-addressable SHA-256 hashing |
| **Soft-Delete** | Deleted blobs are soft-deleted first; reclaimed during next compaction cycle |

**Use Cases:** Internal artifact hosting, release repositories, snapshot repositories, private package registries.

### Proxy Repositories

Remote repository cache with configurable TTL and negative caching.

| Feature | Description |
|---------|-------------|
| **Upstream Fetch** | Artifacts fetched from configured `remote_url` on first request |
| **Local Cache** | Fetched artifacts cached in local BlobStore for subsequent requests |
| **Content Max Age** | `content_max_age` (minutes): TTL for cached binary content |
| **Metadata Max Age** | `metadata_max_age` (minutes): TTL for cached metadata (e.g., `maven-metadata.xml`) |
| **Negative Cache** | Failed upstream lookups cached to avoid repeated failed fetches; configurable `time_to_live` |
| **Content Age Validation** | Cache expiry triggers re-validation/re-fetch from upstream |
| **TLS Configuration** | Upstream connections use configurable TLS with custom trust store support via `src/security/ssl_manager.py` |
| **Auto-Blocking** | Automatic blocking of upstream connections after repeated failures |

**Proxy Configuration Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `remote_url` | string | — | Upstream repository URL (required) |
| `content_max_age` | integer | 1440 | Content cache TTL in minutes |
| `metadata_max_age` | integer | 1440 | Metadata cache TTL in minutes |
| `negative_cache.enabled` | boolean | true | Enable negative result caching |
| `negative_cache.time_to_live` | integer | 1440 | Negative cache TTL in minutes |

**Use Cases:** Caching Maven Central, npmjs.com, Docker Hub, nuget.org, pypi.org, and other public registries.

### Group Repositories

Virtual aggregation of multiple repositories with ordered member traversal.

| Feature | Description |
|---------|-------------|
| **Aggregation** | Combines multiple hosted and proxy repositories into a single logical endpoint |
| **Ordered Traversal** | Member repositories traversed in configured order; first match wins |
| **Read-Only** | Write operations are not supported; uploads must target a specific hosted member |
| **Unified Browse** | Browse tree displays aggregated content across all members |
| **Unified Search** | Search results span all member repositories |

**Use Cases:** Providing a single URL for Maven (`maven-public`), npm, or Docker clients that transparently resolves across multiple upstream and hosted repositories.

---

## Component Management

REST API endpoints for managing components (artifacts/packages) across repositories.

### List Components

```
GET /service/rest/v1/components
```

**Authentication:** Required — `read` privilege on the target repository

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `repository` | string | No | Filter by repository name |
| `format` | string | No | Filter by format |
| `namespace` | string | No | Filter by namespace/group (e.g., Maven groupId, npm scope) |
| `name` | string | No | Filter by component name |
| `version` | string | No | Filter by version |
| `page` | integer | No | Page number (default: 1) |
| `per_page` | integer | No | Items per page (default: 20, max: 100) |

**Response: `200 OK`**

```json
{
  "items": [
    {
      "id": "comp-001",
      "repository": "maven-releases",
      "format": "maven2",
      "namespace": "org.example",
      "name": "my-library",
      "version": "1.0.0",
      "assets": [
        {
          "id": "asset-001",
          "path": "/org/example/my-library/1.0.0/my-library-1.0.0.jar",
          "content_type": "application/java-archive",
          "size": 102400,
          "checksums": {
            "sha256": "abc123def456...",
            "sha1": "def456abc123...",
            "md5": "789ghi012jkl..."
          },
          "last_modified": "2026-02-24T10:00:00Z"
        },
        {
          "id": "asset-002",
          "path": "/org/example/my-library/1.0.0/my-library-1.0.0.pom",
          "content_type": "application/xml",
          "size": 2048,
          "checksums": {
            "sha256": "fed321cba654...",
            "sha1": "321cba654fed...",
            "md5": "654fed321cba..."
          },
          "last_modified": "2026-02-24T10:00:00Z"
        }
      ]
    }
  ],
  "total": 150,
  "page": 1,
  "per_page": 20
}
```

### Get Component

```
GET /service/rest/v1/components/{id}
```

**Authentication:** Required — `read` privilege on the component's repository

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Component UUID |

**Response: `200 OK`** — Full component object with all associated assets.

**Response: `404 Not Found`** — Component does not exist.

### Delete Component

```
DELETE /service/rest/v1/components/{id}
```

**Authentication:** Required — `delete` privilege on the component's repository

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Component UUID |

**Response: `204 No Content`** — Component and all its assets deleted.

Deletion emits the `component_deleted` signal, triggering search index removal and audit logging. Associated blobs in the BlobStore follow the soft-delete pattern and are reclaimed during the next compaction cycle.

---

## Asset Management

REST API endpoints for managing individual assets (files) within components.

### List Assets

```
GET /service/rest/v1/assets
```

**Authentication:** Required — `read` privilege on the target repository

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `repository` | string | No | Filter by repository name |
| `page` | integer | No | Page number (default: 1) |
| `per_page` | integer | No | Items per page (default: 20, max: 100) |

**Response: `200 OK`** — Paginated list of assets.

### Get Asset Metadata

```
GET /service/rest/v1/assets/{id}
```

**Authentication:** Required — `read` privilege on the asset's repository

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | string | Asset UUID |

**Response: `200 OK`**

```json
{
  "id": "asset-001",
  "component_id": "comp-001",
  "repository": "maven-releases",
  "path": "/org/example/my-library/1.0.0/my-library-1.0.0.jar",
  "content_type": "application/java-archive",
  "size": 102400,
  "blob_ref": "default@abc123def456",
  "checksums": {
    "sha256": "abc123def456...",
    "sha1": "def456abc123...",
    "md5": "789ghi012jkl..."
  },
  "last_modified": "2026-02-24T10:00:00Z",
  "created_at": "2026-02-24T10:00:00Z"
}
```

### Download Asset

```
GET /service/rest/v1/assets/{id}/download
```

**Authentication:** Required — `read` privilege on the asset's repository

Download an asset's binary content. The response includes appropriate `Content-Type`, `Content-Length`, and `Content-Disposition` headers.

**Response: `200 OK`** — Binary asset content.

### Delete Asset

```
DELETE /service/rest/v1/assets/{id}
```

**Authentication:** Required — `delete` privilege on the asset's repository

**Response: `204 No Content`** — Asset deleted from the repository and BlobStore (soft-delete).

---

## Content Search (F-103)

Full-text search across all indexed components and assets. The search backend is configurable: Whoosh for standalone deployments and Elasticsearch for production deployments, controlled by the `SEARCH_BACKEND` configuration setting.

**Implementation:** `src/repositories/search.py`

### Search Components

```
GET /service/rest/v1/search
```

**Authentication:** Required — repository `read` privilege (results are automatically filtered by user permissions through RBAC and CSEL evaluation)

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `q` | string | No | Full-text search query across all indexed fields |
| `repository` | string | No | Limit results to a specific repository |
| `format` | string | No | Limit results to a specific format |
| `group` | string | No | Maven group ID or npm scope filter |
| `name` | string | No | Component/package name filter |
| `version` | string | No | Version filter (exact match or range) |
| `sort` | string | No | Sort field: `name`, `version`, `date` (default: relevance) |
| `direction` | string | No | Sort direction: `asc`, `desc` (default: `desc`) |
| `page` | integer | No | Page number (default: 1) |
| `per_page` | integer | No | Items per page (default: 20, max: 100) |

**Response: `200 OK`**

```json
{
  "items": [
    {
      "id": "comp-001",
      "repository": "maven-central",
      "format": "maven2",
      "namespace": "org.apache.commons",
      "name": "commons-lang3",
      "version": "3.14.0",
      "assets": [
        {
          "id": "asset-001",
          "path": "/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar",
          "content_type": "application/java-archive",
          "size": 659949
        }
      ]
    }
  ],
  "total": 42,
  "page": 1,
  "per_page": 20
}
```

### Search Assets

```
GET /service/rest/v1/search/assets
```

File-level search with support for checksum-based lookups.

**Additional Query Parameters (in addition to component search parameters):**

| Parameter | Type | Description |
|-----------|------|-------------|
| `sha256` | string | SHA-256 checksum exact match |
| `sha1` | string | SHA-1 checksum exact match |
| `md5` | string | MD5 checksum exact match |

**Response: `200 OK`** — Paginated list of matching assets with component context.

### Search Indexing Behavior

| Event | Trigger Signal | Action |
|-------|---------------|--------|
| Component uploaded | `component_uploaded` | Component and assets added to search index |
| Component deleted | `component_deleted` | Component and assets removed from search index |
| Repository deleted | `repository_deleted` | All indexed content for the repository is bulk-removed |
| Manual reindex | Scheduled task API | Full reindex triggered via administration task management |

---

## Browse Tree Navigation (F-104)

Hierarchical tree traversal of repository contents, organized by component namespace and asset paths. Provides a filesystem-like view of repository content.

**Implementation:** `src/repositories/browse.py`

### Browse Repository Root

```
GET /service/rest/v1/browse/{repo_name}
```

**Authentication:** Required — `read` privilege on the repository

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `repo_name` | string | Repository name |

**Response: `200 OK`**

```json
{
  "items": [
    {
      "name": "org",
      "type": "folder",
      "path": "/org",
      "children_count": 45
    },
    {
      "name": "com",
      "type": "folder",
      "path": "/com",
      "children_count": 23
    }
  ],
  "repository": "maven-central",
  "path": "/"
}
```

### Browse Path

```
GET /service/rest/v1/browse/{repo_name}/{path}
```

Browse a specific path within a repository's content hierarchy.

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `repo_name` | string | Repository name |
| `path` | string | Path to browse (URL-encoded) |

**Example:**

```
GET /service/rest/v1/browse/maven-central/org/apache/commons/commons-lang3
```

**Response: `200 OK`**

```json
{
  "items": [
    {
      "name": "3.14.0",
      "type": "folder",
      "path": "/org/apache/commons/commons-lang3/3.14.0",
      "children_count": 4
    },
    {
      "name": "3.13.0",
      "type": "folder",
      "path": "/org/apache/commons/commons-lang3/3.13.0",
      "children_count": 4
    },
    {
      "name": "maven-metadata.xml",
      "type": "file",
      "path": "/org/apache/commons/commons-lang3/maven-metadata.xml",
      "size": 1024,
      "content_type": "application/xml",
      "last_modified": "2026-02-24T10:00:00Z"
    }
  ],
  "repository": "maven-central",
  "path": "/org/apache/commons/commons-lang3"
}
```

### Browse Node Types

| Type | Description | Additional Fields |
|------|-------------|-------------------|
| `folder` | Namespace/directory node with children | `children_count` |
| `file` | Asset/file leaf node | `size`, `content_type`, `last_modified` |

### Browse Features

- **Hierarchical Traversal:** Follows the format-specific namespace structure (Maven groupId → artifactId → version, npm scope → package, etc.)
- **Group Repository Aggregation:** Group repositories display an aggregated browse tree merging content across all member repositories
- **Permission Filtering:** Browse results respect RBAC and CSEL permissions; users only see content they are authorized to access
- **Lazy Loading:** Children are not recursively loaded; each level requires a separate request for efficient navigation

---

## BlobStore Configuration

REST API endpoints for managing binary artifact storage backends. BlobStores provide the physical storage for all artifact binary content, separate from the metadata stored in the DataStore (database).

### List BlobStores

```
GET /service/rest/v1/blobstores
```

**Authentication:** Required — `nx-admin` role or `blobstores-read` privilege

**Response: `200 OK`**

```json
{
  "items": [
    {
      "name": "default",
      "type": "file",
      "available_space_in_bytes": 107374182400,
      "blob_count": 12500,
      "total_size_in_bytes": 5368709120,
      "attributes": {
        "path": "/data/blobs/default"
      }
    }
  ]
}
```

### Create BlobStore

```
POST /service/rest/v1/blobstores/{type}
```

**Authentication:** Required — `nx-admin` role or `blobstores-create` privilege

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `type` | string | BlobStore type: `file` or `s3` |

**File BlobStore Request:**

```json
{
  "name": "local-blobs",
  "path": "/data/blobs/local-blobs"
}
```

**S3 BlobStore Request:**

```json
{
  "name": "s3-blobs",
  "bucket": "my-nexus-artifacts",
  "region": "us-east-1",
  "prefix": "nexus/",
  "encryption": {
    "type": "SSE-KMS",
    "kms_key_id": "arn:aws:kms:us-east-1:123456789012:key/my-key-id"
  },
  "authentication": {
    "type": "IAM_ROLE"
  }
}
```

**Response: `201 Created`** — BlobStore created.

### Get BlobStore Configuration

```
GET /service/rest/v1/blobstores/{name}
```

**Response: `200 OK`** — BlobStore configuration object.

### Update BlobStore Configuration

```
PUT /service/rest/v1/blobstores/{name}
```

**Response: `200 OK`** — BlobStore configuration updated.

### Delete BlobStore

```
DELETE /service/rest/v1/blobstores/{name}
```

**Response: `204 No Content`** — Empty BlobStore deleted.

**Response: `400 Bad Request`** — BlobStore is in use by one or more repositories.

**BlobStore Types:**

| Type | Storage Backend | Key Configuration |
|------|----------------|-------------------|
| `file` | Local filesystem (F-201) | `path`: absolute base directory for content-addressable blob storage |
| `s3` | AWS S3 bucket (F-202) | `bucket`, `region`, `prefix`, `encryption` (SSE-S3 or SSE-KMS with configurable KMS key ARN) |

**S3 Encryption Options:**

| Encryption Type | Description |
|----------------|-------------|
| `SSE-S3` | Server-side encryption with Amazon S3 managed keys (default) |
| `SSE-KMS` | Server-side encryption with AWS KMS customer-managed keys |

**S3 Authentication Options:**

| Authentication Type | Description |
|--------------------|-------------|
| `IAM_ROLE` | Use IAM roles or instance profiles (recommended for EC2/ECS/EKS) |
| `ACCESS_KEY` | Explicit AWS access key ID and secret access key |

---

## Data Models

### Repository Model

Defined in `src/models/repository.py`, the Repository model tracks repository configuration and lifecycle state.

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary Key | Unique repository identifier |
| `name` | string | Unique, Not Null | Repository name (used in URLs) |
| `format` | string | Not Null | Format: `maven2`, `npm`, `docker`, `nuget`, `pypi`, `apt`, `raw` |
| `type` | string | Not Null | Type: `hosted`, `proxy`, `group` |
| `status` | string | Not Null, Default `NEW` | Lifecycle state: `NEW`, `INITIALIZING`, `STARTED`, `STOPPED`, `FAILED`, `DELETED` |
| `online` | boolean | Not Null, Default `true` | Whether the repository is accepting requests |
| `attributes` | JSON | Nullable | Format-specific and type-specific configuration (storage, proxy, group settings) |
| `created_at` | datetime | Not Null, Auto | Creation timestamp |
| `updated_at` | datetime | Not Null, Auto | Last modification timestamp |

### Component Model

Defined in `src/models/component.py`, the Component model represents a versioned artifact (e.g., a Maven GAV, an npm package version).

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary Key | Unique component identifier |
| `repository_id` | string (UUID) | Foreign Key → Repository | Parent repository reference |
| `namespace` | string | Nullable | Group/scope/namespace (e.g., Maven groupId, npm scope) |
| `name` | string | Not Null | Component name (e.g., artifactId, package name) |
| `version` | string | Not Null | Component version string |
| `attributes` | JSON | Nullable | Format-specific metadata (e.g., packaging type, classifier) |
| `created_at` | datetime | Not Null, Auto | Creation timestamp |
| `updated_at` | datetime | Not Null, Auto | Last modification timestamp |

### Asset Model

Defined in `src/models/component.py`, the Asset model represents an individual file within a component.

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `id` | string (UUID) | Primary Key | Unique asset identifier |
| `component_id` | string (UUID) | Foreign Key → Component | Parent component reference |
| `path` | string | Not Null | Asset path within the repository namespace |
| `content_type` | string | Nullable | MIME type of the asset content |
| `size` | integer | Not Null, Default 0 | File size in bytes |
| `blob_ref` | string | Not Null | Reference to the blob in the BlobStore (`{blobstore_name}@{blob_id}`) |
| `checksums` | JSON | Not Null | Content checksums: `sha256`, `sha1`, `md5` |
| `last_modified` | datetime | Not Null, Auto | Last modification timestamp |
| `created_at` | datetime | Not Null, Auto | Creation timestamp |

### Entity Relationships

```
Repository (1) ──── (N) Component (1) ──── (N) Asset
                                                  │
                                                  └──── blob_ref ──── BlobStore
```

- A **Repository** contains zero or more **Components**
- A **Component** contains one or more **Assets** (e.g., JAR + POM + checksums)
- Each **Asset** references a blob in the **BlobStore** via `blob_ref`
- Binary content is stored in the BlobStore using content-addressable SHA-256 hashing for deduplication
- Blob deletion follows the soft-delete pattern: blobs are marked as deleted and reclaimed during the next compaction cycle

---

## Error Responses

All repository management endpoints return structured error responses on failure.

**Error Response Format:**

```json
{
  "error": {
    "type": "ClientError",
    "message": "Repository 'my-repo' already exists",
    "status": 400
  }
}
```

**Common HTTP Status Codes:**

| Status | Type | Description |
|--------|------|-------------|
| `400 Bad Request` | `ClientError` | Invalid request parameters, validation failure, or policy violation |
| `401 Unauthorized` | `AuthenticationError` | Missing or invalid authentication credentials |
| `403 Forbidden` | `AuthorizationError` | Insufficient privileges for the requested operation |
| `404 Not Found` | `NotFoundError` | Repository, component, or asset does not exist |
| `405 Method Not Allowed` | `ClientError` | Operation not supported (e.g., write to proxy/group repository) |
| `500 Internal Server Error` | `SystemError` | Unexpected server error or invalid state transition (`ConfigError`) |

---

## Related Documentation

- [API Index](README.md) — Overview of all REST API endpoints
- [Security Management API](security.md) — User, role, privilege, and certificate management
- [Administration API](admin.md) — Health checks, scheduled tasks, cleanup policies, and system configuration
- [Architecture Overview](../architecture.md) — System architecture with format handler and storage layer details
- [Configuration Reference](../configuration.md) — BlobStore, search backend, proxy, and scheduling configuration settings
