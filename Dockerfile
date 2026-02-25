# =============================================================================
# Dockerfile - Sonatype Nexus Repository (Python/Flask Backend)
# =============================================================================
# Multi-stage Docker build for the Sonatype Nexus Repository Flask application.
# Replaces the Docker Maven Plugin configuration from the Java source system.
# Runs Gunicorn 25.1.0 as the production WSGI HTTP server (replacing Jetty 12.0.5).
#
# Build:   docker build -t nexus-repository .
# Run:     docker run -p 8081:8000 nexus-repository
# Compose: docker-compose up -d
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder — Install build tools and compile native dependencies
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Metadata labels
LABEL maintainer="Nexus Repository Team"
LABEL description="Sonatype Nexus Repository Manager - Python/Flask Backend (Builder Stage)"
LABEL version="1.0.0"

# Prevent Python from writing .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system-level build dependencies required for compiling native Python
# extensions: psycopg2-binary (libpq-dev), python-ldap (libldap2-dev, libsasl2-dev),
# and cryptography (libffi-dev, libssl-dev). build-essential provides gcc/make.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libldap2-dev \
        libsasl2-dev \
        libffi-dev \
        libssl-dev && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory for the build stage
WORKDIR /build

# Copy only the requirements file first to leverage Docker layer caching.
# Dependency installation is one of the most time-consuming steps, so isolating
# this layer means rebuilds only re-install if requirements.txt changes.
COPY requirements.txt .

# Install Python runtime dependencies into a virtual environment so that they
# can be cleanly copied to the final stage without build tools or caches.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt


# -----------------------------------------------------------------------------
# Stage 2: Runtime — Minimal production image with only runtime dependencies
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Metadata labels
LABEL maintainer="Nexus Repository Team"
LABEL description="Sonatype Nexus Repository Manager - Python/Flask Backend"
LABEL version="1.0.0"
LABEL org.opencontainers.image.title="Nexus Repository Python/Flask"
LABEL org.opencontainers.image.description="Universal binary repository manager reimplemented in Python 3.12 with Flask 3.1.3"
LABEL org.opencontainers.image.vendor="Nexus Repository Team"

# Prevent Python from writing .pyc files and enable unbuffered stdout/stderr
# for real-time log output in container environments.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install only the runtime shared libraries required by compiled Python packages.
# No build tools (gcc, make) are needed since wheels were compiled in Stage 1.
# curl is included for the Docker HEALTHCHECK instruction.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpq5 \
        libldap-2.5-0 \
        libsasl2-2 \
        libffi8 \
        libssl3 \
        curl \
        tini && \
    rm -rf /var/lib/apt/lists/*

# Copy the pre-built virtual environment from the builder stage.
# This ensures the runtime image contains no build tools, headers, or caches.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create a non-root user 'nexus' for running the application.
# Running as non-root follows Docker security best practices and prevents
# privilege escalation attacks within the container.
RUN groupadd --gid 1000 nexus && \
    useradd --uid 1000 --gid nexus --shell /bin/bash --create-home nexus

# Set the working directory for the application
WORKDIR /app

# Copy the entire application source into the container.
# This includes: wsgi.py, run.py, gunicorn.conf.py, logging.conf,
# config/, src/, migrations/, and any other application files.
COPY . .

# Create necessary runtime directories:
# - /app/data/blobs: Default BlobStore path for local filesystem storage (F-201)
# - /app/logs: Application log files (structured JSON output)
# - /app/data/tmp: Temporary staging for atomic blob writes
RUN mkdir -p /app/data/blobs /app/data/tmp /app/logs && \
    chown -R nexus:nexus /app

# ---------------------------------------------------------------------------
# Runtime Environment Variables
# ---------------------------------------------------------------------------
# FLASK_APP:        Flask application entry point (Gunicorn and Flask CLI)
# FLASK_ENV:        Production mode (disables debug, enables optimizations)
# GUNICORN_BIND:    Default bind address (all interfaces, port 8000)
# GUNICORN_WORKERS: Number of Gunicorn worker processes
# GUNICORN_TIMEOUT: Worker timeout — generous for large artifact uploads
# LOG_LEVEL:        Application logging level
# BLOBSTORE_TYPE:   Default storage backend (file or s3)
# BLOBSTORE_PATH:   Local filesystem BlobStore path (F-201)
ENV FLASK_APP=wsgi:app \
    FLASK_ENV=production \
    GUNICORN_BIND=0.0.0.0:8000 \
    GUNICORN_WORKERS=4 \
    GUNICORN_TIMEOUT=120 \
    LOG_LEVEL=INFO \
    BLOBSTORE_TYPE=file \
    BLOBSTORE_PATH=/app/data/blobs

# Switch to the non-root 'nexus' user for all subsequent commands and at runtime.
USER nexus

# Expose the Gunicorn HTTP port.
# The standard Nexus Repository port 8081 can be mapped externally:
#   docker run -p 8081:8000 nexus-repository
EXPOSE 8000

# Health check to verify the application is responsive.
# Queries the health check endpoint (F-401) every 30 seconds.
# The application must respond with HTTP 200 within 10 seconds.
# After 3 consecutive failures, Docker marks the container as unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health || exit 1

# Use tini as the PID 1 init process to properly handle signals and
# reap zombie processes, which is critical for Gunicorn's pre-fork model.
ENTRYPOINT ["tini", "--"]

# Start the Gunicorn WSGI server using the project's configuration file.
# gunicorn.conf.py contains worker count, timeouts, logging, and server hooks.
# wsgi:app references the Flask application instance created by the app factory.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
