# Deployment Guide — Sonatype Nexus Repository (Python/Flask)

This guide covers the deployment of the **Sonatype Nexus Repository Manager** backend — a Python 3 / Flask 3.1.3 reimplementation of the universal binary repository manager. It supports Maven, npm, Docker, NuGet, PyPI, APT, and Raw artifact formats with Hosted, Proxy, and Group repository types.

Three deployment models are supported:

| Model | Use Case | Database | BlobStore |
|---|---|---|---|
| **Docker** (recommended) | Production, CI/CD, cloud | PostgreSQL | File or S3 |
| **Standalone** | Development, single-node | SQLite or PostgreSQL | File |
| **Clustered** | High availability, scale-out | PostgreSQL | S3 (shared) |

## Prerequisites

All deployment models require:

- **Python 3.12+** — the minimum supported runtime version
- **pip** — Python package manager (bundled with Python 3.12+)

Additional prerequisites by deployment model:

| Prerequisite | Docker | Standalone | Clustered |
|---|---|---|---|
| Docker Engine 24+ | ✅ Required | — | — |
| Docker Compose v2 | ✅ Required | — | — |
| PostgreSQL 13+ | Included in Compose | Optional | ✅ Required |
| Elasticsearch 7.x | Included in Compose | Optional | ✅ Required |
| Load Balancer | — | — | ✅ Required |

> For a high-level overview of the system architecture, see [`docs/architecture/overview.md`](../architecture/overview.md).

---

## Docker Deployment

Docker is the **primary and recommended** deployment model. It packages the Flask application with Gunicorn 25.1.0 as the production WSGI server, alongside PostgreSQL for data persistence and Elasticsearch for content indexing and search.

### Building the Docker Image

The `Dockerfile` at the repository root uses a Python 3.12 slim base image and installs all runtime dependencies.

```bash
# Build the container image
docker build -t nexus-repository .

# Verify the image
docker images nexus-repository
```

The resulting image includes:

- Python 3.12 runtime
- Gunicorn 25.1.0 — production-grade WSGI HTTP server with pre-fork worker model
- All Python dependencies from `requirements.txt`
- Application source code under `/app`
- A non-root `nexus` user for security

### Docker Compose Deployment

The `docker-compose.yml` at the repository root defines three services:

| Service | Image | Purpose |
|---|---|---|
| **app** | Built from `Dockerfile` | Flask application served by Gunicorn |
| **postgres** | `postgres:16-alpine` | PostgreSQL database for production data storage |
| **elasticsearch** | `elasticsearch:7.17.12` | Content indexing and full-text search (Feature F-103) |

Start all services:

```bash
# Start in detached mode
docker-compose up -d

# Check service status
docker-compose ps

# View application logs
docker-compose logs -f app

# View all service logs
docker-compose logs -f
```

Stop and remove services:

```bash
# Stop all services
docker-compose down

# Stop and remove volumes (WARNING: deletes all data)
docker-compose down -v
```

#### Persistent Volumes

Docker Compose configures named volumes for data persistence:

| Volume | Mount Path | Purpose |
|---|---|---|
| `./data/blobs` | `/app/data/blobs` | BlobStore artifact data (Feature F-201) |
| `./logs` | `/app/logs` | Application log files |
| `postgres_data` | `/var/lib/postgresql/data` | PostgreSQL database files |
| `elasticsearch_data` | `/usr/share/elasticsearch/data` | Elasticsearch index data |

### Environment Configuration for Docker

Configuration follows the [twelve-factor app](https://12factor.net/) methodology — all settings are driven by environment variables.

1. Copy the environment template:

```bash
cp .env.example .env
```

2. Edit `.env` and customize the following variables:

**Core Application:**

| Variable | Description | Default |
|---|---|---|
| `SECRET_KEY` | Flask secret key for session signing | `change-me-to-a-random-secret-key` |
| `JWT_SECRET_KEY` | Secret for JWT token generation and validation (PyJWT 2.10.1) | `change-me-to-a-random-jwt-secret` |
| `FLASK_ENV` | Environment profile | `production` |
| `LOG_LEVEL` | Logging level | `INFO` |

**Database:**

| Variable | Description | Example |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://nexus:nexus@postgres:5432/nexus` |

**Elasticsearch:**

| Variable | Description | Example |
|---|---|---|
| `ELASTICSEARCH_URL` | Elasticsearch endpoint | `http://elasticsearch:9200` |

**Gunicorn Server:**

| Variable | Description | Default |
|---|---|---|
| `GUNICORN_WORKERS` | Number of worker processes | `4` |
| `GUNICORN_BIND` | Bind address and port | `0.0.0.0:8000` |

**BlobStore — File (Feature F-201):**

| Variable | Description | Default |
|---|---|---|
| `BLOBSTORE_PATH` | Base path for file BlobStore | `/app/data/blobs` |

**BlobStore — S3 (Feature F-202):**

| Variable | Description | Default |
|---|---|---|
| `S3_BUCKET` | S3 bucket name | *(empty)* |
| `S3_ACCESS_KEY` | AWS access key ID | *(empty)* |
| `S3_SECRET_KEY` | AWS secret access key | *(empty)* |
| `S3_REGION` | AWS region | `us-east-1` |

> **Security note:** Never commit `.env` files with real credentials to version control. The `.env.example` template contains only placeholder values.

### Database Initialization in Docker

The database schema is managed by **Alembic** (via Flask-Migrate 4.0.7). Run the initial migration to create all 14 entity tables:

```bash
docker-compose exec app flask db upgrade
```

The initial migration (`001_initial_schema.py`) creates the following tables:

- `repository` — Repository definitions (format, type, blob_store_name, online status)
- `component` — Artifact components (namespace, name, version)
- `asset` — Binary assets (path, content_type, checksums, size)
- `user` — User accounts (password_hash, status, email)
- `role` — Security roles (name, description, privileges)
- `privilege` — Privilege descriptors (type, name, properties)
- `content_selector` — Content selector expressions (CSEL)
- `role_assignment` — User-to-role junction table
- `task_definition` — Scheduled task definitions (type, cron_expression)
- `task_execution` — Task execution history (status, duration)
- `system_config` — System configuration key-value pairs
- `audit_event` — Audit log entries (event_type, timestamp, payload)
- `blobstore_config` — BlobStore configurations (type, settings)
- `cleanup_policy` — Cleanup policy rules (format, criteria)

To check the current migration status:

```bash
docker-compose exec app flask db current
```

---

## Standalone Deployment

Standalone deployment runs the Flask application directly with Gunicorn on a single host, without Docker containers. This model is suited for development, testing, or simple single-node production setups.

### Installation

```bash
# Create a Python virtual environment
python3 -m venv venv

# Activate the virtual environment
source venv/bin/activate

# Install runtime dependencies
pip install -r requirements.txt

# Copy and configure environment variables
cp .env.example .env
# Edit .env with your settings
```

### SQLite Configuration (Zero-Config)

By default, standalone mode uses **SQLite** as the database backend. This provides a zero-configuration experience with no external database server required.

Set the database URL in `.env`:

```
DATABASE_URL=sqlite:///nexus.db
```

SQLite is suitable for:

- Local development
- Testing and CI/CD pipelines
- Single-user or low-traffic deployments

For production workloads with concurrent users, use PostgreSQL instead (see [Clustered Deployment](#clustered-deployment)).

### Running with Gunicorn (Production)

Gunicorn 25.1.0 is the production WSGI HTTP server. It provides a pre-fork worker model for handling concurrent requests.

```bash
# Start Gunicorn with the project configuration
gunicorn -c gunicorn.conf.py wsgi:app
```

The `gunicorn.conf.py` file at the repository root configures:

| Setting | Description | Default |
|---|---|---|
| `bind` | Listen address | `0.0.0.0:8000` |
| `workers` | Worker process count | `(2 × CPU cores) + 1` |
| `worker_class` | Worker type | `sync` |
| `timeout` | Request timeout (seconds) | `120` |
| `keepalive` | Keep-alive timeout (seconds) | `5` |
| `max_requests` | Requests before worker restart | `1000` |
| `preload_app` | Preload app before fork | `True` |

Available worker types:

| Worker Class | Description | Best For |
|---|---|---|
| `sync` | Synchronous (default) | CPU-bound workloads |
| `gevent` | Async with greenlets | I/O-bound with many connections |
| `gthread` | Threaded workers | Mixed workloads |

### Running the Development Server

For local development only:

```bash
# Start the Flask development server
python run.py
```

The `.flaskenv` file configures the Flask CLI:

```
FLASK_APP=wsgi:app
FLASK_ENV=development
FLASK_DEBUG=1
FLASK_RUN_HOST=0.0.0.0
FLASK_RUN_PORT=5000
```

> ⚠️ **Warning:** Never use the Flask development server in production. It is single-threaded, not optimized for performance, and lacks security hardening. Always use Gunicorn for production deployments.

### Database Migration (Standalone)

Apply database migrations with Alembic:

```bash
# Apply all pending migrations
flask db upgrade

# Check current migration version
flask db current

# Generate a new migration after model changes
flask db migrate -m "description of changes"

# Downgrade one version
flask db downgrade
```

---

## Clustered Deployment

Clustered deployment provides **high availability and horizontal scalability** by running multiple application instances behind a load balancer, with shared PostgreSQL and S3 storage backends.

### Architecture Overview

```
                    ┌──────────────┐
                    │ Load Balancer │
                    └──────┬───────┘
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ Gunicorn │ │ Gunicorn │ │ Gunicorn │
        │ Node 1   │ │ Node 2   │ │ Node 3   │
        └────┬─────┘ └────┬─────┘ └────┬─────┘
             │             │             │
     ┌───────┴─────────────┴─────────────┴───────┐
     │                                           │
┌────┴─────┐    ┌──────────────┐    ┌────────────┴──┐
│PostgreSQL│    │ Elasticsearch│    │  S3 BlobStore  │
│ Cluster  │    │   Cluster    │    │  (shared)      │
└──────────┘    └──────────────┘    └───────────────┘
```

### PostgreSQL Backend

A dedicated PostgreSQL 13+ instance (or cluster) is **required** for clustered deployments. The application connects using psycopg2-binary 2.9.10 as the PostgreSQL adapter and SQLAlchemy 2.0.36 as the ORM.

Set the database connection in `.env`:

```
DATABASE_URL=postgresql://nexus:password@db.example.com:5432/nexus
```

**Connection pooling** is managed by SQLAlchemy's built-in engine pool. Configure pool settings via environment variables:

| Variable | Description | Default |
|---|---|---|
| `SQLALCHEMY_POOL_SIZE` | Maximum number of persistent connections | `10` |
| `SQLALCHEMY_MAX_OVERFLOW` | Maximum overflow connections beyond pool_size | `20` |
| `SQLALCHEMY_POOL_TIMEOUT` | Seconds to wait for a connection from the pool | `30` |
| `SQLALCHEMY_POOL_RECYCLE` | Seconds before a connection is recycled | `3600` |

**PostgreSQL recommendations:**

- Use PostgreSQL 13 or newer
- Enable connection SSL for encrypted client-server communication
- Configure WAL archiving for point-in-time recovery
- Set `shared_buffers` to 25% of available RAM
- Set `effective_cache_size` to 75% of available RAM
- Use a connection pooler (PgBouncer) for very high connection counts

### Shared S3 BlobStore

For multi-node deployments, all instances must access the **same artifact storage**. The S3 BlobStore (Feature F-202) provides shared, durable storage via Amazon S3 or any S3-compatible service (MinIO, Ceph, etc.).

Configure S3 in `.env`:

```
S3_BUCKET=nexus-artifacts
S3_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE
S3_SECRET_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
S3_REGION=us-east-1
S3_ENDPOINT=                    # Leave empty for AWS; set for S3-compatible services
```

The S3 BlobStore implementation (using boto3 1.36.7) supports:

- **Server-Side Encryption (SSE)** — AES-256 or AWS KMS encryption at rest
- **Multipart uploads** — efficient transfer of large artifacts
- **Soft-delete** — deleted blobs are marked for deferred cleanup
- **Metadata sidecar files** — stored alongside blob data for fast lookups

### Elasticsearch Cluster

Elasticsearch is **required** for content indexing and full-text search across the cluster (Feature F-103).

Configure the Elasticsearch endpoint in `.env`:

```
ELASTICSEARCH_URL=http://es-cluster.example.com:9200
```

The Python Elasticsearch client (elasticsearch-py 7.17.12) connects to the cluster for:

- Component and asset indexing
- Full-text search queries
- Browse tree navigation data

For high availability, run at minimum a 3-node Elasticsearch cluster with one replica per shard.

### Load Balancing

Deploy a load balancer (Nginx, HAProxy, AWS ALB) in front of the Gunicorn instances.

**Health check endpoint:**

```
GET /api/health
```

This endpoint (Feature F-401) returns the health status of the application node, including database connectivity, BlobStore availability, Elasticsearch connectivity, and scheduler status.

**Load balancer requirements:**

- **Session stickiness is NOT required** — authentication uses stateless JWT tokens
- Route health checks to `GET /api/health` with a 10-second interval
- Set connection timeout to at least 120 seconds for large artifact uploads
- Forward standard proxy headers: `X-Forwarded-For`, `X-Forwarded-Proto`, `Host`

---

## SSL/TLS Configuration

SSL/TLS support (Feature F-302) secures communication between clients and the Nexus Repository server.

### Generating Certificates

For **development** environments, generate a self-signed certificate using the `cryptography` Python library (44.0.0):

```bash
python -c "
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import datetime

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
subject = issuer = x509.Name([
    x509.NameAttribute(NameOID.COMMON_NAME, u'localhost'),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, u'Nexus Repository'),
])
cert = (
    x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(issuer)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.datetime.utcnow())
    .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
    .sign(key, hashes.SHA256())
)
with open('server.key', 'wb') as f:
    f.write(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()
    ))
with open('server.crt', 'wb') as f:
    f.write(cert.public_bytes(serialization.Encoding.PEM))
print('Generated server.key and server.crt')
"
```

For **production** environments, obtain a certificate from a trusted Certificate Authority (CA) and place the `.crt` and `.key` files in a secure location.

### Configuring HTTPS

**Option 1 — TLS termination at Gunicorn (direct):**

```bash
gunicorn -c gunicorn.conf.py \
  --certfile=/path/to/server.crt \
  --keyfile=/path/to/server.key \
  wsgi:app
```

**Option 2 — TLS termination at reverse proxy (recommended):**

Terminate TLS at Nginx or another reverse proxy and forward plain HTTP to Gunicorn. This is the recommended approach because:

- Centralized certificate management
- Better TLS performance via Nginx's optimized TLS stack
- Easier certificate rotation without application restart

See the [Reverse Proxy Setup](#reverse-proxy-setup-nginx) section below.

### Certificate Management API

The application provides REST API endpoints for certificate management:

- `GET /api/security/certificates` — List trusted certificates
- `POST /api/security/certificates` — Import a certificate
- `DELETE /api/security/certificates/{id}` — Remove a certificate

These endpoints are managed by `src/app/security/ssl_manager.py` and `src/app/security/certificate_store.py`.

---

## Reverse Proxy Setup (Nginx)

Nginx is the **recommended reverse proxy** for production deployments. It handles TLS termination, static file serving, request buffering, and load balancing.

### Nginx Configuration Example

```nginx
upstream nexus_backend {
    server 127.0.0.1:8000;
    # For clustered deployments, add additional Gunicorn instances:
    # server 10.0.0.2:8000;
    # server 10.0.0.3:8000;
}

server {
    listen 443 ssl http2;
    server_name nexus.example.com;

    # SSL/TLS certificates
    ssl_certificate     /etc/nginx/ssl/server.crt;
    ssl_certificate_key /etc/nginx/ssl/server.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Maximum artifact upload size (adjust as needed)
    client_max_body_size 10G;

    # Buffering settings for large artifact transfers
    proxy_buffering on;
    proxy_buffer_size 128k;
    proxy_buffers 4 256k;
    proxy_busy_buffers_size 256k;

    # Timeouts for long-running proxy fetch operations
    proxy_connect_timeout 60s;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;

    location / {
        proxy_pass http://nexus_backend;

        # Standard proxy headers
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support (if needed for real-time events)
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # Health check endpoint (no auth required)
    location /api/health {
        proxy_pass http://nexus_backend;
        proxy_set_header Host $host;
        access_log off;
    }

    # Prometheus metrics endpoint (restrict access)
    location /metrics {
        proxy_pass http://nexus_backend;
        proxy_set_header Host $host;
        # Restrict to monitoring network
        # allow 10.0.0.0/8;
        # deny all;
    }
}

# HTTP to HTTPS redirect
server {
    listen 80;
    server_name nexus.example.com;
    return 301 https://$server_name$request_uri;
}
```

### Key Nginx Settings

| Setting | Recommended Value | Reason |
|---|---|---|
| `client_max_body_size` | `10G` | Large Docker images and binary artifacts |
| `proxy_read_timeout` | `600s` | Proxy repository remote fetches can be slow |
| `proxy_buffering` | `on` | Buffer upstream responses for client delivery |
| `ssl_protocols` | `TLSv1.2 TLSv1.3` | Modern, secure TLS only |

---

## Monitoring and Health Checks

The application provides comprehensive monitoring endpoints (Feature F-401) for operational visibility.

### Health Check Endpoint

```
GET /api/health
```

Returns a JSON response with overall system health and individual component statuses:

```json
{
  "status": "healthy",
  "checks": {
    "database": {
      "status": "healthy",
      "response_time_ms": 5
    },
    "blobstore": {
      "status": "healthy",
      "type": "file",
      "available_space_gb": 450.2
    },
    "elasticsearch": {
      "status": "healthy",
      "cluster_name": "nexus-search",
      "node_count": 3
    },
    "scheduler": {
      "status": "healthy",
      "active_jobs": 12,
      "next_run": "2026-02-25T03:00:00Z"
    }
  },
  "uptime_seconds": 86400,
  "version": "1.0.0"
}
```

**Health check components:**

| Component | Check Description |
|---|---|
| **database** | Executes a lightweight query to verify connectivity |
| **blobstore** | Confirms read/write access to the configured BlobStore |
| **elasticsearch** | Pings the Elasticsearch cluster for connectivity |
| **scheduler** | Verifies the APScheduler 3.10.4 task scheduler is running |

### Prometheus Metrics

```
GET /metrics
```

The metrics endpoint exports Prometheus-compatible metrics using prometheus-client 0.21.1 and prometheus-flask-instrumentator for automatic request instrumentation.

**Key metrics exported:**

| Metric | Type | Description |
|---|---|---|
| `http_request_duration_seconds` | Histogram | HTTP request latency by endpoint and method |
| `http_requests_total` | Counter | Total HTTP request count by status code |
| `db_pool_connections_active` | Gauge | Active database connections |
| `db_pool_connections_idle` | Gauge | Idle database connections in the pool |
| `blobstore_operation_duration_seconds` | Histogram | BlobStore read/write latencies |
| `blobstore_total_size_bytes` | Gauge | Total BlobStore storage usage |
| `task_execution_duration_seconds` | Histogram | Scheduled task execution time |
| `task_executions_total` | Counter | Total task executions by status |
| `repository_operations_total` | Counter | Repository operations by type and format |
| `component_count` | Gauge | Total components across all repositories |

### Grafana Integration

Connect Grafana to the Prometheus endpoint for visualization dashboards:

1. Add a Prometheus data source pointing to your Prometheus server
2. Import or create dashboards for:
   - **Application Overview** — request rates, latencies, error rates
   - **Database Health** — connection pool utilization, query latencies
   - **Storage Metrics** — BlobStore usage, operation throughput
   - **Task Scheduler** — task execution frequency, durations, failures
3. Set up alerts for critical thresholds (e.g., error rate > 1%, disk usage > 90%)

---

## Backup and Recovery

### Database Backup

**SQLite (Standalone):**

```bash
# File-level backup (stop application first for consistency)
cp nexus.db nexus.db.backup

# Or use SQLite's online backup API
sqlite3 nexus.db ".backup nexus.db.backup"
```

**PostgreSQL (Production/Clustered):**

```bash
# Logical backup (full database dump)
pg_dump -U nexus -h db.example.com nexus > nexus_backup_$(date +%Y%m%d).sql

# Compressed backup
pg_dump -U nexus -h db.example.com -Fc nexus > nexus_backup_$(date +%Y%m%d).dump

# Restore from logical backup
psql -U nexus -h db.example.com nexus < nexus_backup_20260225.sql

# Restore from compressed backup
pg_restore -U nexus -h db.example.com -d nexus nexus_backup_20260225.dump
```

For continuous backup, configure **WAL archiving** on the PostgreSQL server to enable point-in-time recovery.

### BlobStore Backup

**File BlobStore (Feature F-201):**

```bash
# Standard filesystem backup of the BlobStore directory
rsync -av /app/data/blobs/ /backup/blobs/

# Or use tar for a compressed archive
tar -czf blobs_backup_$(date +%Y%m%d).tar.gz /app/data/blobs/
```

**S3 BlobStore (Feature F-202):**

- Enable **S3 bucket versioning** for automatic object version history
- Configure **cross-region replication** for disaster recovery
- Use **S3 lifecycle policies** to manage backup retention

```bash
# Sync S3 bucket to a backup bucket
aws s3 sync s3://nexus-artifacts s3://nexus-artifacts-backup
```

### Elasticsearch Backup

Use the Elasticsearch snapshot and restore API:

```bash
# Register a snapshot repository
curl -X PUT "http://localhost:9200/_snapshot/backup" -H 'Content-Type: application/json' -d '{
  "type": "fs",
  "settings": { "location": "/backup/elasticsearch" }
}'

# Create a snapshot
curl -X PUT "http://localhost:9200/_snapshot/backup/snapshot_$(date +%Y%m%d)?wait_for_completion=true"

# Restore from a snapshot
curl -X POST "http://localhost:9200/_snapshot/backup/snapshot_20260225/_restore"
```

> **Note:** Elasticsearch search indices can be fully rebuilt from the database if needed. The database is the authoritative source of truth for all component and asset data.

### Disaster Recovery Procedure

1. **Restore the database** — Apply the most recent database backup (SQLite file copy or PostgreSQL `pg_restore`)
2. **Restore the BlobStore** — Restore artifact files from filesystem backup or verify S3 bucket integrity
3. **Restart the application** — Start all application services
4. **Rebuild search indices** — Trigger a full re-index from the database to Elasticsearch
5. **Verify system health** — Check `GET /api/health` to confirm all components are operational

---

## Performance Tuning

### Performance Targets

| Metric | Target |
|---|---|
| REST API response time | < 500ms average |
| Cached artifact resolution | < 200ms |
| Full-text search (100K components) | < 2 seconds |
| System uptime | ≥ 99.9% |
| Storage efficiency (deduplication) | > 2:1 ratio |

### Gunicorn Worker Configuration

The number of Gunicorn workers directly affects request throughput. Configure in `gunicorn.conf.py` or via environment variables.

**Recommended formula:**

```
workers = (2 × CPU_CORES) + 1
```

| Server Size | CPU Cores | Workers | Worker Class |
|---|---|---|---|
| Small (dev) | 1–2 | 3–5 | `sync` |
| Medium (production) | 4–8 | 9–17 | `sync` or `gthread` |
| Large (high traffic) | 16+ | 33+ | `gevent` |

**Timeout tuning** — Increase the worker timeout for environments with large artifact uploads:

```bash
# In gunicorn.conf.py or via environment variable
GUNICORN_TIMEOUT=300  # 5 minutes for very large uploads
```

### Database Connection Pooling

SQLAlchemy 2.0.36 provides built-in connection pooling. Tune pool settings based on workload:

| Parameter | Small | Medium | Large | Description |
|---|---|---|---|---|
| `pool_size` | 5 | 10 | 30 | Persistent connections |
| `max_overflow` | 10 | 20 | 50 | Additional connections when pool is full |
| `pool_timeout` | 30 | 30 | 10 | Seconds to wait for a connection |
| `pool_recycle` | 3600 | 1800 | 900 | Seconds before recycling a connection |

Enable connection health checking:

```
SQLALCHEMY_POOL_PRE_PING=True
```

This executes a lightweight `SELECT 1` before handing connections to the application, preventing stale connection errors.

### Elasticsearch Tuning

**Index refresh interval:**

```bash
# Increase refresh interval during bulk import for faster indexing
curl -X PUT "http://localhost:9200/nexus_components/_settings" -H 'Content-Type: application/json' -d '{
  "index.refresh_interval": "30s"
}'

# Reset to default after import
curl -X PUT "http://localhost:9200/nexus_components/_settings" -H 'Content-Type: application/json' -d '{
  "index.refresh_interval": "1s"
}'
```

**Shard and replica configuration:**

| Cluster Size | Primary Shards | Replicas | Notes |
|---|---|---|---|
| Single node | 1 | 0 | Development only |
| 3 nodes | 3 | 1 | Standard production |
| 5+ nodes | 5 | 1–2 | High availability |

**JVM heap size** — Allocate no more than 50% of available RAM to the Elasticsearch JVM heap, with a maximum of 32 GB:

```
ES_JAVA_OPTS=-Xms4g -Xmx4g
```

### BlobStore Optimization

**File BlobStore (Feature F-201):**

- Use XFS or ext4 filesystem for the BlobStore directory
- Mount with `noatime` option to reduce I/O overhead
- Use SSD storage for high-throughput artifact serving
- Configure the I/O scheduler to `noop` or `deadline` for SSDs

**S3 BlobStore (Feature F-202):**

- Set multipart upload threshold to 64 MB for efficient large file transfers
- Increase boto3 connection pool (`max_pool_connections=50`) for high concurrency
- Use S3 Transfer Acceleration for geographically distributed uploads
- Enable S3 Intelligent-Tiering for cost optimization on infrequently accessed artifacts

---

## Scaling

### Horizontal Scaling

Deploy multiple Gunicorn instances behind a load balancer for increased throughput:

1. **Provision additional application nodes** — each running Gunicorn with the same configuration
2. **Shared PostgreSQL database** — all nodes connect to the same PostgreSQL instance or cluster
3. **Shared S3 BlobStore** — all nodes read/write artifacts from the same S3 bucket
4. **Elasticsearch cluster** — distributed search across all nodes
5. **Load balancer** — distribute traffic using round-robin or least-connections strategy

**Scaling considerations:**

- Each node is stateless (JWT authentication, no server-side sessions)
- Database connection pool size should account for total connections across all nodes
- Monitor PostgreSQL `max_connections` to ensure it exceeds `(pool_size + max_overflow) × node_count`

### Vertical Scaling

Scale a single node for increased capacity:

| Component | Action | Impact |
|---|---|---|
| Gunicorn workers | Increase `GUNICORN_WORKERS` | More concurrent request handling |
| Database pool | Increase `SQLALCHEMY_POOL_SIZE` | More concurrent database queries |
| Elasticsearch heap | Increase `ES_JAVA_OPTS -Xmx` | Larger search indices, faster queries |
| Filesystem I/O | Use NVMe SSD storage | Faster BlobStore operations |
| Network | Use 10 Gbps networking | Faster artifact transfers |

---

## Logging Configuration

The application uses Python's built-in `logging` module with **structured JSON output** for SIEM integration compatibility.

### Configuration

Logging is configured via `logging.conf` at the repository root. The configuration defines:

**Loggers:**

| Logger Name | Default Level | Purpose |
|---|---|---|
| `root` | `INFO` | Catch-all root logger |
| `src.app` | `INFO` | Main application events |
| `src.app.api` | `INFO` | API request/response logging |
| `src.app.auth` | `INFO` | Authentication events |
| `src.app.storage` | `INFO` | BlobStore operations |
| `src.app.scheduler` | `INFO` | Task scheduler events |
| `src.app.events` | `INFO` | Event system activity |
| `sqlalchemy.engine` | `WARNING` | SQL query logging (verbose at DEBUG) |
| `elasticsearch` | `WARNING` | Elasticsearch client logs |

**Handlers:**

| Handler | Type | Output | Purpose |
|---|---|---|---|
| `console` | StreamHandler | stdout | Container and development logging |
| `file` | RotatingFileHandler | `logs/nexus.log` | Persistent application logs (10 MB rotation, 5 backups) |
| `audit` | RotatingFileHandler | `logs/audit.log` | Dedicated audit trail (50 MB rotation, 10 backups) |
| `error` | RotatingFileHandler | `logs/error.log` | Error-only log (10 MB rotation, 5 backups) |

### Log Levels

| Level | Usage |
|---|---|
| `DEBUG` | Detailed diagnostic information (development only) |
| `INFO` | General operational events (default for production) |
| `WARNING` | Unexpected situations that are handled gracefully |
| `ERROR` | Failures that affect a single operation |
| `CRITICAL` | System-wide failures requiring immediate attention |

Set the log level via environment variable:

```bash
LOG_LEVEL=INFO    # Production default
LOG_LEVEL=DEBUG   # Development / troubleshooting
```

### Structured JSON Output

All log entries are formatted as JSON for machine parsing:

```json
{
  "timestamp": "2026-02-25T12:00:00",
  "level": "INFO",
  "logger": "src.app.api",
  "message": "GET /api/repositories 200 45ms",
  "module": "repositories",
  "function": "list_repositories",
  "line": 42
}
```

This format enables integration with log aggregation platforms (ELK Stack, Splunk, Datadog, Fluentd).

### Per-Task Logging

Scheduled tasks (Feature F-402) generate dedicated log entries with task identifiers for traceability:

```json
{
  "timestamp": "2026-02-25T03:00:00",
  "level": "INFO",
  "logger": "src.app.scheduler",
  "message": "Task 'cleanup-snapshots' completed in 12450ms",
  "task_id": "cleanup-snapshots",
  "task_type": "cleanup",
  "duration_ms": 12450,
  "status": "success"
}
```

---

## Troubleshooting

### Common Issues

#### Database Connection Errors

**Symptom:** Application fails to start with `OperationalError: could not connect to server`

**Resolution:**

1. Verify the `DATABASE_URL` in `.env` is correct
2. Confirm the database server is running and accepting connections
3. Check firewall rules allow connections on port 5432 (PostgreSQL) or verify the SQLite file path exists
4. Test connectivity: `psql -U nexus -h db.example.com -d nexus -c "SELECT 1"`
5. Verify `SQLALCHEMY_POOL_SIZE` does not exceed PostgreSQL `max_connections`

#### Elasticsearch Connectivity Issues

**Symptom:** Search features return errors or empty results

**Resolution:**

1. Verify `ELASTICSEARCH_URL` in `.env` is correct
2. Confirm Elasticsearch is running: `curl http://localhost:9200/_cluster/health`
3. Check that the index prefix matches: `ELASTICSEARCH_INDEX_PREFIX=nexus_`
4. Verify Elasticsearch version compatibility (7.x required)
5. Rebuild indices if they are corrupted or missing

#### BlobStore Permission Errors

**Symptom:** `PermissionError` when uploading or downloading artifacts

**Resolution:**

1. Verify the `BLOBSTORE_PATH` directory exists and is writable
2. Check file ownership: `ls -la /app/data/blobs/`
3. For Docker: ensure volume mount permissions match the `nexus` user inside the container
4. For S3: verify IAM credentials have `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` permissions
5. Test S3 access: `aws s3 ls s3://nexus-artifacts/`

#### Port Conflicts

**Symptom:** `OSError: [Errno 98] Address already in use`

**Resolution:**

1. Check what is using the port: `lsof -i :8000`
2. Either stop the conflicting process or change `GUNICORN_BIND` to a different port
3. For Docker: verify host port mappings in `docker-compose.yml` do not conflict

#### Memory Issues with Large Artifact Uploads

**Symptom:** Worker processes crash during large file uploads

**Resolution:**

1. Increase Gunicorn worker timeout: `GUNICORN_TIMEOUT=300`
2. Increase `client_max_body_size` in Nginx configuration
3. For S3 BlobStore: large files are automatically handled via multipart uploads
4. Monitor memory usage and increase worker memory limits if using Docker resource constraints
5. Consider switching to `gevent` worker class for better memory efficiency with concurrent uploads

### Diagnostic Tools

**Support ZIP (Feature F-403):**

Generate a comprehensive diagnostic bundle:

```bash
curl -u admin:password -o support-bundle.zip "http://localhost:8000/api/system/support-zip"
```

The Support ZIP includes:

- System configuration (with sensitive values sanitized)
- Application logs
- Thread dumps
- Database connection statistics
- BlobStore health information
- Elasticsearch cluster status

**Health Check:**

Verify overall system status:

```bash
curl -s http://localhost:8000/api/health | python -m json.tool
```

**OpenAPI Documentation:**

Browse the interactive API documentation:

```
http://localhost:8000/api/docs
```

---

## Quick Reference

### Common Commands

| Action | Command |
|---|---|
| Start (Docker) | `docker-compose up -d` |
| Stop (Docker) | `docker-compose down` |
| Start (Standalone) | `gunicorn -c gunicorn.conf.py wsgi:app` |
| Start (Development) | `python run.py` |
| Apply migrations | `flask db upgrade` |
| Check health | `curl http://localhost:8000/api/health` |
| View metrics | `curl http://localhost:8000/metrics` |
| Generate Support ZIP | `curl -o support.zip http://localhost:8000/api/system/support-zip` |
| View logs (Docker) | `docker-compose logs -f app` |

### Key File Reference

| File | Purpose |
|---|---|
| `Dockerfile` | Container image definition |
| `docker-compose.yml` | Multi-service orchestration |
| `gunicorn.conf.py` | Production WSGI server configuration |
| `wsgi.py` | WSGI entry point for Gunicorn |
| `run.py` | Development server entry point |
| `.env.example` | Environment variable template |
| `.flaskenv` | Flask CLI settings |
| `logging.conf` | Logging configuration |
| `requirements.txt` | Python runtime dependencies |
| `config/default.py` | Base application configuration |
| `config/production.py` | Production configuration |
| `config/development.py` | Development configuration |
| `migrations/` | Alembic database migration scripts |
| `src/app/factory.py` | Flask application factory (`create_app()`) |

### Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | Yes | — | Flask session signing secret |
| `JWT_SECRET_KEY` | Yes | — | JWT token signing secret |
| `DATABASE_URL` | Yes | `sqlite:///nexus.db` | Database connection string |
| `ELASTICSEARCH_URL` | No | `http://localhost:9200` | Elasticsearch endpoint |
| `BLOBSTORE_PATH` | No | `./data/blobs` | File BlobStore path |
| `S3_BUCKET` | No | — | S3 BlobStore bucket |
| `S3_ACCESS_KEY` | No | — | AWS access key |
| `S3_SECRET_KEY` | No | — | AWS secret key |
| `S3_REGION` | No | `us-east-1` | AWS region |
| `GUNICORN_WORKERS` | No | `(2 × CPU) + 1` | Worker process count |
| `GUNICORN_BIND` | No | `0.0.0.0:8000` | Bind address |
| `GUNICORN_TIMEOUT` | No | `120` | Worker timeout (seconds) |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `FLASK_ENV` | No | `production` | Config profile |
