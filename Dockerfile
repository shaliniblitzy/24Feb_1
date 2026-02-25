# ==============================================
# Nexus Repository Manager — Python/Flask
# Multi-stage Docker build
# ==============================================
# Replaces the Docker Maven Plugin + Temurin 21 JDK build from the
# original Java implementation (AAP Section 0.1.3).
#
# Build:  docker build -t nexus-repository .
# Run:    docker run -p 8081:8081 nexus-repository
#
# Deployment models supported:
#   Standalone:       SQLite + File BlobStore  (default)
#   Clustered:        PostgreSQL + Shared Filesystem
#   Container-Native: PostgreSQL + S3 BlobStore
# ==============================================

# -----------------------------------------------
# Stage 1: Builder — compile C extensions
# -----------------------------------------------
# A dedicated build stage installs GCC and header packages so that
# psycopg2, python-ldap, and cryptography can compile their native
# extensions.  These build-time tools are NOT carried forward to the
# production image, keeping the final layer small and secure.
# -----------------------------------------------
FROM python:3.13-slim AS builder

WORKDIR /build

# System packages required for compiling C extension modules:
#   gcc            — C compiler for native extensions
#   libpq-dev      — PostgreSQL client headers (psycopg2-binary)
#   libldap2-dev   — OpenLDAP client headers (python-ldap)
#   libsasl2-dev   — Cyrus SASL headers (python-ldap SASL support)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        libldap2-dev \
        libsasl2-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the pinned dependency manifest first so that Docker can cache
# the expensive pip-install layer independently of application source
# code changes.
COPY requirements.txt .

# Create a virtual environment for dependency isolation.  Every
# package is installed into /opt/venv so the entire tree can be
# COPY'd into the production stage in a single layer.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt


# -----------------------------------------------
# Stage 2: Production — lean runtime image
# -----------------------------------------------
# Only runtime shared libraries, the pre-built virtual environment,
# and application source are included.  No compilers, no headers,
# no pip cache.
# -----------------------------------------------
FROM python:3.13-slim AS production

# OCI / Docker image metadata
LABEL maintainer="Nexus Repository Team"
LABEL description="Sonatype Nexus Repository Manager — Python/Flask"
LABEL version="1.0.0"
LABEL org.opencontainers.image.title="Nexus Repository Manager"
LABEL org.opencontainers.image.description="Universal binary repository manager supporting Maven, npm, Docker, NuGet, PyPI, APT, and Raw formats"
LABEL org.opencontainers.image.vendor="Nexus Repository Team"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.source="https://github.com/nexus-repository/nexus-repository-python"

# Runtime shared libraries (NO dev headers, NO compilers):
#   libpq5         — PostgreSQL client library (psycopg2-binary at runtime)
#   libldap-2.5-0  — OpenLDAP client library (python-ldap at runtime)
#   libsasl2-2     — Cyrus SASL library (python-ldap SASL at runtime)
#   curl           — Used by the HEALTHCHECK instruction to probe the app
#   tini           — Lightweight init for proper PID 1 signal handling
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        libldap-2.5-0 \
        libsasl2-2 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# ---- Security: run as non-root ----
# Create a dedicated system user/group with no login shell.
# All application files and data directories are owned by this user.
RUN groupadd -r nexus \
    && useradd -r -g nexus -d /app -s /sbin/nologin nexus

# ---- Python virtual environment from builder stage ----
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ---- Application working directory ----
WORKDIR /app

# ---- Copy application source code and configuration ----
# Each COPY is a separate layer so that changes to one directory
# do not invalidate the cache for others.
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY config/ ./config/
COPY alembic.ini .
COPY gunicorn.conf.py .

# ---- Persistent data directories ----
# /data/blobs   — File-based BlobStore content-addressable storage
# /data/logs    — Application log files (when not sent to stdout)
# /data/search  — Whoosh full-text search index (standalone mode)
RUN mkdir -p /data/blobs /data/logs /data/search \
    && chown -R nexus:nexus /app /data

# ---- Environment variable defaults ----
# Every setting listed here can be overridden at container runtime
# via -e or an env_file (see .env.example for the full reference).
ENV FLASK_APP="src.app:create_app" \
    FLASK_ENV="production" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONHASHSEED="random" \
    BLOBSTORE_TYPE="file" \
    BLOBSTORE_PATH="/data/blobs" \
    SEARCH_BACKEND="whoosh" \
    LOG_LEVEL="INFO" \
    LOG_FORMAT="json" \
    GUNICORN_BIND="0.0.0.0:8081" \
    GUNICORN_WORKERS="4" \
    GUNICORN_TIMEOUT="120"

# ---- Network ----
# Port 8081 is the standard Nexus Repository port.
EXPOSE 8081

# ---- Health check ----
# Probes the F-401 health check endpoint which returns component and
# system status.  The start-period allows time for the six-phase
# startup sequence (KERNEL → SCHEMAS → STORAGE → SECURITY →
# CAPABILITIES → SERVICES) to complete before the first probe.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8081/service/rest/v1/status/check || exit 1

# ---- Drop to non-root user ----
USER nexus

# ---- Data volumes ----
# Declare volumes so that orchestrators (Docker Compose, Kubernetes)
# know which paths should be backed by persistent storage.
VOLUME ["/data/blobs", "/data/logs", "/data/search"]

# ---- Entry point ----
# Use tini as PID 1 to properly reap zombie processes and forward
# signals (SIGTERM) to the Gunicorn master for graceful shutdown
# (AAP Section 0.7.5 — Operational Rules).
#
# Gunicorn is configured via gunicorn.conf.py which reads all
# tunables from environment variables.  The default is 4 sync workers
# matching the C-001 resource constraint (4 CPU / 4 GB RAM).
ENTRYPOINT ["tini", "--"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "src.app:create_app()"]
