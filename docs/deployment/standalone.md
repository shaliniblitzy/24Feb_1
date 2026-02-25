# Standalone Deployment Guide

The standalone deployment model is the simplest way to run the Nexus Repository Manager. It is designed for **single-server installations** targeting small teams, development environments, and evaluation setups.

**Technology stack:**

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Database | **SQLite** | Metadata persistence (zero-configuration, file-based) |
| Binary Storage | **File BlobStore** | Content-addressable artifact storage on local filesystem |
| Search Engine | **Whoosh** | Pure-Python full-text search index (in-process) |
| Web Server | **Gunicorn** | Production WSGI HTTP server |
| Framework | **Flask** | Application framework with blueprint-based modularity |

This model is suitable for deployments serving up to a small team with moderate artifact throughput. All data resides on a single machine — no external databases, object stores, or search clusters are required.

> **Need more capacity?** For high-availability production environments, see the [Clustered Deployment Guide](clustered.md) (PostgreSQL + Shared Filesystem) or the [Container-Native Deployment Guide](container.md) (Docker / Kubernetes with PostgreSQL + S3).

---

## Prerequisites

### Software Requirements

| Requirement | Version | Notes |
|-------------|---------|-------|
| **Python** | 3.13+ | Required. CPython reference implementation recommended. |
| **pip** | Latest | Bundled with Python 3.13. Used for dependency installation. |
| **Git** | Any recent | Optional. Used for cloning the repository source code. |
| **venv / virtualenv** | Built-in | Recommended for isolating the Python environment. |

### Hardware Requirements

These minimums are derived from the system resource specification (C-001):

| Resource | Minimum | Recommended | Notes |
|----------|---------|-------------|-------|
| **CPU** | 2 cores | 4 cores | More cores allow more Gunicorn workers. |
| **RAM** | 4 GB | 8 GB | Whoosh index and SQLAlchemy sessions consume memory. |
| **Disk** | 10 GB (OS + app) | 50 GB+ | Artifact storage grows with repository usage. SSD recommended. |

### Supported Operating Systems

- **Linux:** Ubuntu 22.04+, Debian 12+, RHEL 8+, Fedora 38+, Amazon Linux 2023+
- **macOS:** macOS 13 (Ventura) or later
- **Windows:** Windows Server 2022+ (Gunicorn requires WSL2 on Windows; consider Docker instead)

### Network Requirements

- **Port 8081** (default) must be accessible for HTTP traffic.
- Outbound internet access is required if configuring proxy repositories to fetch from upstream registries (Maven Central, npmjs.com, Docker Hub, pypi.org, etc.).

---

## Installation

### Step 1 — Clone the Repository

```bash
git clone <repository-url> nexus-repository
cd nexus-repository
```

Alternatively, download and extract a release archive if Git is not available.

### Step 2 — Create a Virtual Environment

A Python virtual environment isolates the application dependencies from system packages.

```bash
python3.13 -m venv venv
```

Activate the environment:

```bash
# Linux / macOS
source venv/bin/activate

# Windows (PowerShell)
venv\Scripts\Activate.ps1

# Windows (cmd.exe)
venv\Scripts\activate.bat
```

Verify the correct Python is active:

```bash
python --version
# Expected: Python 3.13.x
```

### Step 3 — Install Dependencies

All production dependencies are listed in `requirements.txt` with exact pinned versions for reproducible installations.

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This installs Flask 3.1.3, SQLAlchemy 2.0.46, Gunicorn 23.0.0, and all other required packages.

### Step 4 — Configure the Environment

Copy the environment template and customize it for your deployment:

```bash
cp .env.example .env
```

Edit the `.env` file and set the following values for standalone mode:

```dotenv
# ─── Security Keys (MUST CHANGE — see warning below) ───
SECRET_KEY=<generate-a-64-char-hex-string>
JWT_SECRET=<generate-a-separate-64-char-hex-string>

# ─── Database ───
DATABASE_URL=sqlite:///nexus.db

# ─── BlobStore ───
BLOBSTORE_TYPE=file
BLOBSTORE_PATH=./data/blobs

# ─── Search ───
SEARCH_BACKEND=whoosh
SEARCH_INDEX_PATH=./data/whoosh_index

# ─── Application ───
FLASK_ENV=production
LOG_LEVEL=INFO
LOG_FORMAT=json

# ─── Server ───
GUNICORN_BIND=0.0.0.0:8081
GUNICORN_WORKERS=4
GUNICORN_TIMEOUT=120
```

Generate secure random keys with Python:

```bash
# Generate SECRET_KEY
python -c "import secrets; print(secrets.token_hex(32))"

# Generate JWT_SECRET (use a DIFFERENT value)
python -c "import secrets; print(secrets.token_hex(32))"
```

> **⚠️ CRITICAL — Zero Plaintext Credential Policy:**
> `SECRET_KEY` and `JWT_SECRET` **MUST** be changed to cryptographically strong random values before running in production. Never commit secrets to version control. Never use default or placeholder values. All passwords are stored hashed with bcrypt — no credentials are ever persisted in plaintext.

Every configuration setting that differs between deployment environments can be overridden via environment variables. See the [Configuration Reference](../configuration.md) for the complete list of available settings.

### Step 5 — Create Data Directories

The File BlobStore and Whoosh search index require writable directories:

```bash
mkdir -p data/blobs
mkdir -p data/whoosh_index
```

Ensure the application user has read/write permissions:

```bash
chmod 750 data/blobs data/whoosh_index
```

### Step 6 — Initialize the Database

Run Alembic migrations to create all database tables:

```bash
flask db upgrade
```

This creates the SQLite database file (`nexus.db`) in the project directory and applies the initial schema migration, creating tables for all entity types — repositories, components, assets, users, roles, privileges, content selectors, tasks, audit events, blob store configurations, cleanup policies, and system configuration.

### Step 7 — Create Initial Admin User (Optional)

```bash
flask create-admin --username admin --email admin@example.com
```

You will be prompted to set a password interactively. If you skip this step, the application creates a default admin user (`admin` / `admin123`) on first startup.

> **⚠️ Security:** If the default admin account is created automatically, change its password immediately after first login.

---

## Running the Application

### Option A — Development Server (Not for Production)

The Flask built-in development server is convenient for local testing:

```bash
flask run --host=0.0.0.0 --port=8081
```

> **Warning:** The Flask development server is single-threaded, has no request timeout management, and provides no security hardening. **Never use it for production deployments.** Use Gunicorn instead.

### Option B — Production Server with Gunicorn (Recommended)

Start the application using the project's Gunicorn configuration file:

```bash
gunicorn --config gunicorn.conf.py "src.app:create_app()"
```

Or specify settings inline:

```bash
gunicorn \
  --bind 0.0.0.0:8081 \
  --workers 4 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  "src.app:create_app()"
```

**Gunicorn configuration reference:**

| Flag | Default | Description |
|------|---------|-------------|
| `--bind` | `0.0.0.0:8081` | Address and port to listen on. |
| `--workers` | `4` | Number of worker processes. Formula: `2 × CPU_CORES + 1`. |
| `--timeout` | `120` | Request timeout in seconds. Increase for large artifact uploads. |
| `--access-logfile` | `-` | Access log destination. `-` writes to stdout. |
| `--error-logfile` | `-` | Error log destination. `-` writes to stderr. |
| `--graceful-timeout` | `30` | Seconds to wait for workers to finish during shutdown. |

The application responds to `SIGTERM` by completing in-flight requests, flushing pending audit events, and cleanly stopping the scheduler before process exit.

### Option C — Running as a systemd Service

For Linux servers, configure the application as a managed system service for automatic startup and restart.

**1. Create a dedicated system user:**

```bash
sudo useradd --system --shell /usr/sbin/nologin --home-dir /opt/nexus-repository nexus
sudo chown -R nexus:nexus /opt/nexus-repository
```

**2. Create the systemd unit file:**

Save the following as `/etc/systemd/system/nexus-repository.service`:

```ini
[Unit]
Description=Nexus Repository Manager (Python/Flask)
Documentation=https://github.com/your-org/nexus-repository
After=network.target

[Service]
Type=notify
User=nexus
Group=nexus
WorkingDirectory=/opt/nexus-repository
Environment="PATH=/opt/nexus-repository/venv/bin:/usr/local/bin:/usr/bin"
Environment="FLASK_ENV=production"
EnvironmentFile=/opt/nexus-repository/.env
ExecStart=/opt/nexus-repository/venv/bin/gunicorn \
  --config gunicorn.conf.py \
  "src.app:create_app()"
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
Restart=on-failure
RestartSec=5
TimeoutStartSec=60
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

**3. Enable and start the service:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable nexus-repository
sudo systemctl start nexus-repository
```

**4. Check status and logs:**

```bash
sudo systemctl status nexus-repository
sudo journalctl -u nexus-repository -f
```

---

## Standalone Configuration Profile

The standalone deployment uses the following default configuration. Values can be set in `config/default.yaml`, overridden per environment in `config/development.yaml` or `config/production.yaml`, and further overridden by environment variables.

```yaml
# config/default.yaml — Standalone defaults
database:
  url: "sqlite:///nexus.db"
  pool_size: 5
  echo: false

blobstore:
  type: file
  path: "./data/blobs"

search:
  backend: whoosh
  index_path: "./data/whoosh_index"

security:
  auth_realms:
    - local     # Username/password with bcrypt hashing
  session_timeout: 1800  # 30 minutes

scheduler:
  enabled: true
  job_store: sqlalchemy   # Persists jobs to the same SQLite database
  timezone: UTC

logging:
  level: INFO
  format: json            # Structured JSON logging for SIEM compatibility
  file: null              # null = log to stdout/stderr (captured by systemd/Docker)

server:
  host: "0.0.0.0"
  port: 8081
```

All settings support environment variable overrides. The naming convention maps YAML hierarchy to uppercase underscore-delimited variable names (e.g., `database.url` → `DATABASE_URL`, `blobstore.type` → `BLOBSTORE_TYPE`).

---

## Six-Phase Startup Sequence

The application follows a strict six-phase startup sequence. No HTTP requests are accepted until all phases complete successfully. Each phase logs its completion status as a structured JSON log entry.

| Phase | Name | Actions | Log Confirmation |
|-------|------|---------|-----------------|
| 1 | **KERNEL** | Verify Python 3.13+ runtime, load YAML configuration, merge environment variable overrides, initialize structured JSON logging | `"KERNEL phase complete"` |
| 2 | **SCHEMAS** | Connect to SQLite database, run pending Alembic migrations, verify schema integrity | `"SCHEMAS phase complete"` |
| 3 | **STORAGE** | Initialize File BlobStore, create/verify `data/blobs` directory, validate write permissions | `"STORAGE phase complete"` |
| 4 | **SECURITY** | Configure the authentication chain (local realm for standalone), load RBAC roles and privileges, initialize JWT handler | `"SECURITY phase complete"` |
| 5 | **CAPABILITIES** | Register all 7 format handlers: Maven, npm, Docker, NuGet, PyPI, APT, Raw | `"CAPABILITIES phase complete"` |
| 6 | **SERVICES** | Start APScheduler with SQLAlchemy job store, activate health monitoring, open HTTP listener | `"SERVICES phase complete, accepting requests"` |

**Example startup log output (JSON):**

```json
{"timestamp": "2026-02-25T10:00:01Z", "level": "INFO", "module": "startup", "message": "KERNEL phase complete", "phase": 1}
{"timestamp": "2026-02-25T10:00:02Z", "level": "INFO", "module": "startup", "message": "SCHEMAS phase complete", "phase": 2, "migrations_applied": 1}
{"timestamp": "2026-02-25T10:00:02Z", "level": "INFO", "module": "startup", "message": "STORAGE phase complete", "phase": 3, "blobstore_type": "file"}
{"timestamp": "2026-02-25T10:00:03Z", "level": "INFO", "module": "startup", "message": "SECURITY phase complete", "phase": 4, "auth_realms": ["local"]}
{"timestamp": "2026-02-25T10:00:03Z", "level": "INFO", "module": "startup", "message": "CAPABILITIES phase complete", "phase": 5, "formats": 7}
{"timestamp": "2026-02-25T10:00:04Z", "level": "INFO", "module": "startup", "message": "SERVICES phase complete, accepting requests", "phase": 6}
```

If any phase fails, the application logs the error and exits with a non-zero status code. Check `journalctl` or container logs to identify which phase encountered the problem.

---

## Verification

After the application starts, verify the deployment is healthy.

### Health Check

```bash
curl -s http://localhost:8081/service/rest/v1/status/check | python -m json.tool
```

Expected response:

```json
{
  "status": "healthy",
  "components": {
    "database": {"status": "healthy", "type": "sqlite"},
    "blobstore": {"status": "healthy", "type": "file"},
    "scheduler": {"status": "healthy", "running_jobs": 0},
    "search": {"status": "healthy", "backend": "whoosh"}
  }
}
```

### Prometheus Metrics

```bash
curl -s http://localhost:8081/service/metrics
```

This returns metrics in Prometheus text exposition format, including:
- `http_requests_total` — Total HTTP requests by method and status
- `http_request_duration_seconds` — Request latency histogram
- `blob_operations_total` — BlobStore read/write/delete counts
- `auth_attempts_total` — Authentication success/failure counts
- `active_repositories_count` — Number of active repositories
- `scheduler_executions_total` — Scheduled task execution counts

### List Repositories (Authenticated)

```bash
curl -s -u admin:admin123 http://localhost:8081/service/rest/v1/repositories | python -m json.tool
```

### Format Protocol Verification

Verify that format-native endpoints respond correctly:

```bash
# Maven — repository metadata
curl -s http://localhost:8081/repository/maven-central/

# npm — registry root
curl -s http://localhost:8081/repository/npm-proxy/

# Docker — API version check
curl -s http://localhost:8081/v2/

# PyPI — simple index
curl -s http://localhost:8081/repository/pypi-proxy/simple/
```

---

## Data Backup Procedures

In standalone mode all data resides on the local filesystem. Regular backups are essential for disaster recovery.

### Database Backup (SQLite)

SQLite stores the entire database in a single file. For a consistent backup, either stop the application or use the SQLite `.backup` command.

**Method A — Stop and copy (safest):**

```bash
sudo systemctl stop nexus-repository
cp nexus.db "nexus.db.backup.$(date +%Y%m%d_%H%M%S)"
sudo systemctl start nexus-repository
```

**Method B — Online backup (no downtime):**

```bash
sqlite3 nexus.db ".backup 'nexus.db.backup.$(date +%Y%m%d_%H%M%S)'"
```

### BlobStore Backup (File System)

The File BlobStore directory contains all binary artifacts. Use `tar` or `rsync` to create a backup.

```bash
tar -czf "blobs-backup-$(date +%Y%m%d_%H%M%S).tar.gz" data/blobs/
```

For incremental backups:

```bash
rsync -av --delete data/blobs/ /backup/nexus/blobs/
```

### Configuration Backup

```bash
cp .env ".env.backup.$(date +%Y%m%d)"
tar -czf "config-backup-$(date +%Y%m%d).tar.gz" config/
```

### Full Automated Backup Script

Save the following as `scripts/backup.sh` and schedule it with cron:

```bash
#!/usr/bin/env bash
set -euo pipefail

# ─── Configuration ───
BACKUP_DIR="/backup/nexus/$(date +%Y%m%d_%H%M%S)"
APP_DIR="/opt/nexus-repository"
RETENTION_DAYS=30

mkdir -p "${BACKUP_DIR}"

echo "[$(date -Iseconds)] Starting Nexus Repository backup..."

# 1. Database backup (online)
sqlite3 "${APP_DIR}/nexus.db" ".backup '${BACKUP_DIR}/nexus.db'"
echo "[$(date -Iseconds)] Database backed up."

# 2. BlobStore backup
tar -czf "${BACKUP_DIR}/blobs.tar.gz" -C "${APP_DIR}" data/blobs/
echo "[$(date -Iseconds)] BlobStore backed up."

# 3. Configuration backup
tar -czf "${BACKUP_DIR}/config.tar.gz" -C "${APP_DIR}" .env config/
echo "[$(date -Iseconds)] Configuration backed up."

# 4. Clean up old backups
find /backup/nexus/ -maxdepth 1 -type d -mtime +${RETENTION_DAYS} -exec rm -rf {} +
echo "[$(date -Iseconds)] Old backups cleaned (retention: ${RETENTION_DAYS} days)."

echo "[$(date -Iseconds)] Backup complete → ${BACKUP_DIR}"
```

```bash
chmod +x scripts/backup.sh

# Schedule daily at 02:00 via cron
echo "0 2 * * * /opt/nexus-repository/scripts/backup.sh >> /var/log/nexus-backup.log 2>&1" \
  | sudo crontab -u nexus -
```

---

## Upgrade Instructions

Follow these steps to upgrade the Nexus Repository Manager to a new version.

### Pre-Upgrade Checklist

- [ ] Read the release notes for breaking changes
- [ ] Verify the new version's Python and dependency requirements
- [ ] Ensure sufficient disk space for the backup
- [ ] Notify users of the planned maintenance window

### Upgrade Procedure

**1. Back up everything:**

```bash
/opt/nexus-repository/scripts/backup.sh
```

See the [Data Backup Procedures](#data-backup-procedures) section above for manual backup steps.

**2. Stop the application:**

```bash
sudo systemctl stop nexus-repository
```

**3. Pull the new version:**

```bash
cd /opt/nexus-repository
git fetch origin
git checkout <release-tag>
```

Or, for archive-based deployments, extract the new release over the existing directory.

**4. Update dependencies:**

```bash
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

**5. Run database migrations:**

```bash
flask db upgrade
```

Alembic applies only the new migrations that have not yet been executed. Existing data is preserved.

**6. Start the application:**

```bash
sudo systemctl start nexus-repository
```

**7. Verify health:**

```bash
curl -s http://localhost:8081/service/rest/v1/status/check | python -m json.tool
```

Confirm all components report `"status": "healthy"`.

### Rollback Procedure

If the upgrade fails:

1. Stop the application: `sudo systemctl stop nexus-repository`
2. Restore the previous version: `git checkout <previous-tag>`
3. Restore the database backup: `cp /backup/nexus/<timestamp>/nexus.db /opt/nexus-repository/nexus.db`
4. Restore dependencies: `pip install -r requirements.txt`
5. Start the application: `sudo systemctl start nexus-repository`

---

## Troubleshooting

### Port Already in Use

**Symptom:** `[ERROR] Can't connect to ('0.0.0.0', 8081)` on startup.

**Solution:** Identify and stop the conflicting process, or change the bind port:

```bash
# Find what is using port 8081
sudo lsof -i :8081

# Option A: Stop the conflicting process
sudo kill <PID>

# Option B: Change the port in .env
GUNICORN_BIND=0.0.0.0:9081
```

### Permission Denied on BlobStore Path

**Symptom:** `PermissionError: [Errno 13] Permission denied: './data/blobs'`

**Solution:** Ensure the application user owns the data directories:

```bash
sudo chown -R nexus:nexus /opt/nexus-repository/data
sudo chmod -R 750 /opt/nexus-repository/data
```

### Database Locked (SQLite)

**Symptom:** `sqlite3.OperationalError: database is locked`

**Cause:** SQLite supports only one concurrent writer. Under high load with multiple Gunicorn workers, write contention can occur.

**Solutions:**
- Reduce Gunicorn workers for write-heavy workloads: `--workers 2`
- Enable WAL (Write-Ahead Logging) mode for better concurrency: set `DATABASE_URL=sqlite:///nexus.db?mode=wal` or ensure the application enables WAL at startup
- For sustained high concurrency, migrate to PostgreSQL — see the [Clustered Deployment Guide](clustered.md)

### Startup Phase Failure

**Symptom:** Application exits immediately with a non-zero status code.

**Diagnosis:** Check the logs for the last completed phase:

```bash
sudo journalctl -u nexus-repository --no-pager -n 50
```

| Failed Phase | Common Causes | Resolution |
|-------------|---------------|------------|
| KERNEL | Invalid configuration YAML, missing `.env` file | Fix syntax errors in config files; ensure `.env` exists |
| SCHEMAS | Database file permissions, corrupt migration | Check file permissions; run `flask db upgrade` manually |
| STORAGE | BlobStore directory missing or not writable | Create directory and set permissions (see above) |
| SECURITY | Invalid `SECRET_KEY` or `JWT_SECRET` | Regenerate keys (see [Installation Step 4](#step-4--configure-the-environment)) |
| CAPABILITIES | Missing Python package for a format handler | Re-run `pip install -r requirements.txt` |
| SERVICES | Scheduler configuration error, port conflict | Check scheduler config; resolve port conflict |

### Memory Issues

**Symptom:** Application killed by the OOM killer or excessive swap usage.

**Solutions:**
- Increase available RAM (8 GB recommended)
- Reduce Gunicorn workers: `--workers 2`
- Reduce Whoosh index cache size in configuration
- Monitor memory with: `sudo journalctl -u nexus-repository | grep memory`

### Whoosh Index Corruption

**Symptom:** Search returns no results or errors after a crash.

**Solution:** Rebuild the search index:

```bash
flask search reindex
```

This re-reads all component metadata from the database and rebuilds the Whoosh index from scratch.

---

## Limitations of Standalone Mode

Before choosing the standalone deployment model, understand its inherent constraints:

| Limitation | Impact | Alternative |
|-----------|--------|-------------|
| **Single server** | No horizontal scaling; all load on one machine | [Clustered Deployment](clustered.md) |
| **SQLite concurrency** | One writer at a time; not suitable for high-throughput CI/CD pipelines | PostgreSQL (clustered or container mode) |
| **Local filesystem storage** | BlobStore not accessible from other nodes; no redundancy | S3 BlobStore (container-native mode) |
| **Whoosh search** | In-process index; slower than Elasticsearch for large datasets (100k+ components) | Elasticsearch (clustered or container mode) |
| **No failover** | Single point of failure; downtime during upgrades | Clustered deployment with multiple nodes |
| **Backup complexity** | Must coordinate SQLite + filesystem backups; no built-in replication | PostgreSQL streaming replication |

**When to upgrade from standalone:**
- Your team exceeds ~20 active developers regularly publishing artifacts
- You need zero-downtime upgrades or high availability
- Artifact counts exceed 100,000 components
- CI/CD pipelines require sustained concurrent write throughput

---

## Security Hardening

Apply these measures before exposing the application to production traffic.

### 1. Generate Strong Secrets

```bash
# Replace placeholder keys with cryptographically strong values
python -c "import secrets; print(secrets.token_hex(32))"
```

Set unique values for both `SECRET_KEY` and `JWT_SECRET` in your `.env` file. These keys protect session integrity and JWT token signing. Compromised keys allow session hijacking and token forgery.

### 2. Run Behind a Reverse Proxy

Configure Nginx or Apache as a TLS-terminating reverse proxy in front of Gunicorn:

```nginx
# /etc/nginx/sites-available/nexus-repository
server {
    listen 443 ssl http2;
    server_name nexus.example.com;

    ssl_certificate     /etc/ssl/certs/nexus.pem;
    ssl_certificate_key /etc/ssl/private/nexus.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    client_max_body_size 10G;  # Allow large artifact uploads

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

With Nginx handling TLS, bind Gunicorn only to localhost:

```dotenv
GUNICORN_BIND=127.0.0.1:8081
```

### 3. Configure Firewall Rules

```bash
# Allow only HTTPS (443) and SSH (22); block direct access to 8081
sudo ufw allow 22/tcp
sudo ufw allow 443/tcp
sudo ufw deny 8081/tcp
sudo ufw enable
```

### 4. Disable Debug Mode

Ensure the following in your `.env`:

```dotenv
FLASK_ENV=production
DEBUG=false
```

Debug mode exposes the Werkzeug debugger, which allows arbitrary code execution.

### 5. File Permissions

```bash
# Database: readable/writable only by the nexus user
chmod 600 /opt/nexus-repository/nexus.db

# BlobStore: no world access
chmod -R 750 /opt/nexus-repository/data/

# Configuration: protect secrets
chmod 600 /opt/nexus-repository/.env
```

### 6. Enable Audit Logging

Ensure structured JSON audit logging is active for security event tracking:

```dotenv
LOG_FORMAT=json
LOG_LEVEL=INFO
```

All authentication attempts (success and failure), authorization decisions, configuration changes, and administrative actions are logged as structured JSON events compatible with SIEM ingestion pipelines (Splunk, ELK, Datadog, etc.).

### 7. Change Default Admin Password

If the default admin account was created automatically, change the password immediately:

```bash
curl -u admin:admin123 -X PUT \
  -H "Content-Type: application/json" \
  -d '{"password": "<new-strong-password>"}' \
  http://localhost:8081/service/rest/v1/security/users/admin/change-password
```

---

## Related Resources

- [Configuration Reference](../configuration.md) — Complete list of all configuration settings and environment variables
- [Architecture Overview](../architecture.md) — System design, five-layer architecture, and component interactions
- [REST API Documentation](../api/README.md) — API endpoint reference for all modules
- [Clustered Deployment](clustered.md) — Multi-node high-availability deployment with PostgreSQL and shared filesystem
- [Container-Native Deployment](container.md) — Docker and Kubernetes deployment with PostgreSQL and S3
