# Clustered Deployment Guide

This guide describes the **high-availability clustered deployment model** for the Nexus Repository Manager Python/Flask application. It is intended for enterprise DevOps engineers and system administrators who need to operate a multi-node deployment with fault tolerance and horizontal scaling.

**Stack summary:**

| Component | Technology | Role |
|-----------|------------|------|
| Application | Python 3.13 / Flask / Gunicorn | Stateless HTTP workers |
| Database | PostgreSQL 16 | Shared metadata DataStore |
| Artifact Storage | NFS v4.1+ or GlusterFS | Shared File BlobStore |
| Search | Elasticsearch 8.17.0 | Distributed full-text indexing |
| Load Balancer | Nginx or HAProxy | Request distribution and TLS termination |

This deployment model is suitable for enterprise production environments that require **high availability**, **horizontal scalability**, and **shared state** across multiple application nodes.

> **Other deployment models:**
> - For simpler single-server deployments, see [Standalone Deployment](standalone.md).
> - For Docker and Kubernetes deployments using S3 BlobStore, see [Container-Native Deployment](container.md).

---

## Architecture Overview

The clustered architecture separates stateless application workers from shared backend services so that any individual node can be replaced without data loss.

```
                    ┌──────────────────┐
                    │   Load Balancer  │
                    │  (Nginx/HAProxy) │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
        ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────┐
        │  Nexus     │ │  Nexus     │ │  Nexus     │
        │  Node 1    │ │  Node 2    │ │  Node 3    │
        │ (Gunicorn) │ │ (Gunicorn) │ │ (Gunicorn) │
        └──────┬─────┘ └──────┬─────┘ └──────┬─────┘
               │              │              │
     ┌─────────┴──────────────┴──────────────┴─────────┐
     │                                                   │
┌────▼──────┐     ┌───────────────┐     ┌──────────────┐
│ PostgreSQL│     │ Shared FS     │     │ Elasticsearch│
│    16     │     │ (NFS/GlusterFS│     │    8.x       │
│ (Primary) │     │  BlobStore)   │     │  Cluster     │
└───────────┘     └───────────────┘     └──────────────┘
```

**Key design principles:**

- **Stateless application nodes** — Every Nexus node runs an identical Gunicorn process. No local state is kept on any node; all persistent data resides in PostgreSQL, the shared filesystem, or Elasticsearch.
- **Shared DataStore** — PostgreSQL 16 stores all metadata (repositories, components, assets, users, roles, tasks, audit events, configuration). SQLAlchemy's `QueuePool` manages per-node connection pools.
- **Shared File BlobStore** — Binary artifacts are stored on a network filesystem (NFS or GlusterFS) mounted at the same path on every node.
- **Distributed search** — Elasticsearch 8.x provides full-text component search, replacing the embedded Whoosh engine used in standalone mode.
- **Load balancer** — An external reverse proxy (Nginx, HAProxy, or a cloud ALB/NLB) distributes traffic and performs TLS termination.
- **Six-phase startup** — Each node executes the startup sequence (KERNEL → SCHEMAS → STORAGE → SECURITY → CAPABILITIES → SERVICES) independently on boot.

---

## Prerequisites

### Per Application Node

| Requirement | Minimum |
|-------------|---------|
| Python | 3.13+ |
| CPU | 4 cores |
| RAM | 8 GB |
| Disk | 20 GB local (OS + application code) |
| Network | Access to PostgreSQL, shared filesystem mount, and Elasticsearch |
| Open ports | 8081 (HTTP — configurable) |

### PostgreSQL Server

| Requirement | Minimum |
|-------------|---------|
| Version | PostgreSQL 16 |
| CPU | 4 cores |
| RAM | 8 GB |
| Storage | Fast SSD, sized to metadata volume |
| Network | Reachable from every application node |

### Shared Filesystem

| Requirement | Detail |
|-------------|--------|
| Protocol | NFS v4.1+ **or** GlusterFS |
| Mount path | Identical on every application node |
| IOPS | Sufficient for concurrent blob read/write |
| Capacity | Sized to total artifact storage volume |

### Elasticsearch Cluster

| Requirement | Minimum |
|-------------|---------|
| Version | Elasticsearch 8.17.0 (8.x series) |
| Nodes | 3 (for high availability) |
| CPU | 4 cores per node |
| RAM | 8 GB per node |
| Storage | SSD, sized to search index volume |

### Load Balancer

| Requirement | Detail |
|-------------|--------|
| Software | Nginx, HAProxy, AWS ALB/NLB, or equivalent |
| Health check | `GET /service/rest/v1/status/check` |
| Sticky sessions | Optional but recommended |
| TLS termination | Recommended at the LB tier |

---

## PostgreSQL Setup

### Install PostgreSQL 16

**Ubuntu / Debian:**

```bash
sudo apt-get update
sudo apt-get install -y postgresql-16
```

**RHEL / CentOS / Fedora:**

```bash
sudo dnf install -y postgresql16-server
sudo postgresql-setup --initdb
sudo systemctl enable --now postgresql
```

### Create Database and User

Connect to the PostgreSQL instance as the superuser and run:

```sql
CREATE USER nexus WITH PASSWORD 'secure-password-here';
CREATE DATABASE nexus_repository OWNER nexus;
GRANT ALL PRIVILEGES ON DATABASE nexus_repository TO nexus;

-- Recommended: restrict default public schema access
\c nexus_repository
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO nexus;
```

> **Warning:** Replace `'secure-password-here'` with a strong, randomly generated password. Never store database credentials in plaintext configuration files — use environment variables or a secrets manager.

### Configure Remote Access

Edit `pg_hba.conf` to allow connections from every application node. Use the network CIDR that covers all nodes:

```
# TYPE  DATABASE         USER    ADDRESS              METHOD
host    nexus_repository nexus   10.0.0.0/24          scram-sha-256
```

Then reload the configuration:

```bash
sudo systemctl reload postgresql
```

> **Security note:** Always use `scram-sha-256` authentication — never `md5` or `trust`.

### Tune PostgreSQL for Production

Apply the following recommended settings in `postgresql.conf` (adjust values to your hardware):

```ini
# Connection handling
max_connections = 200
# Memory (tune for 8 GB RAM)
shared_buffers = 2GB
effective_cache_size = 6GB
work_mem = 16MB
maintenance_work_mem = 512MB
# WAL and replication
wal_level = replica
max_wal_senders = 3
wal_keep_size = 1GB
# Checkpoints
checkpoint_completion_target = 0.9
min_wal_size = 256MB
max_wal_size = 1GB
# Logging
log_min_duration_statement = 500
log_line_prefix = '%m [%p] %q%u@%d '
```

**Connection pool sizing:** The application exposes `DATABASE_POOL_SIZE` and `DATABASE_MAX_OVERFLOW` environment variables. The total connections across all nodes must not exceed PostgreSQL's `max_connections`. For example, with 3 nodes, `POOL_SIZE=10` and `MAX_OVERFLOW=20` yields at most 90 connections — well within 200. SQLAlchemy's `QueuePool` manages this automatically.

### PostgreSQL High Availability (Optional)

For database-tier HA, consider:

| Approach | Description |
|----------|-------------|
| **Streaming replication** | Built-in primary/standby replication with manual failover |
| **Patroni** | Automatic failover orchestrator backed by etcd/ZooKeeper/Consul |
| **pgBouncer** | Lightweight connection pooler in front of PostgreSQL for connection multiplexing |
| **Cloud-managed** | AWS RDS, GCP Cloud SQL, or Azure Database for PostgreSQL with automatic HA |

When using a standby, point a DNS alias (e.g., `pg.nexus.internal`) at the current primary so that `DATABASE_URL` does not need to change on application nodes during a failover.

---

## Shared Filesystem Configuration

All application nodes must mount the same filesystem at an identical path. This directory is the File BlobStore for binary artifact storage.

### Option A — NFS Setup

**On the NFS server:**

```bash
sudo mkdir -p /exports/nexus-blobs
sudo chown nobody:nogroup /exports/nexus-blobs
sudo chmod 0770 /exports/nexus-blobs

# Export to the application subnet
echo "/exports/nexus-blobs 10.0.0.0/24(rw,sync,no_subtree_check,no_root_squash)" \
  | sudo tee -a /etc/exports
sudo exportfs -ra
```

**On each application node:**

```bash
sudo apt-get install -y nfs-common   # Ubuntu/Debian
sudo mkdir -p /shared/blobs
sudo mount -t nfs4 nfs-server:/exports/nexus-blobs /shared/blobs
```

### Option B — GlusterFS Setup (Alternative)

GlusterFS provides replicated storage without a single point of failure.

```bash
# Create a replicated volume across 3 storage nodes
gluster volume create nexus-blobs replica 3 \
  storage1:/data/brick1 storage2:/data/brick2 storage3:/data/brick3
gluster volume start nexus-blobs

# Mount on each application node
sudo mount -t glusterfs storage1:/nexus-blobs /shared/blobs
```

### Persistent Mount Configuration

Add an entry to `/etc/fstab` on each application node so the mount survives reboots.

**NFS example:**

```
nfs-server:/exports/nexus-blobs  /shared/blobs  nfs4  rw,hard,intr,timeo=600,retrans=3  0 0
```

**GlusterFS example:**

```
storage1:/nexus-blobs  /shared/blobs  glusterfs  defaults,_netdev  0 0
```

### Permissions and Ownership

- The BlobStore directory must be **writable** by the application user on **all** nodes.
- Use a dedicated system user (e.g., `nexus`) with a consistent UID/GID across every node.
- Set the `BLOBSTORE_PATH` environment variable to the mount point on every node:

```bash
export BLOBSTORE_PATH=/shared/blobs
```

Verify write access from every node:

```bash
touch /shared/blobs/.healthcheck && rm /shared/blobs/.healthcheck
```

---

## Elasticsearch Cluster Setup

### Install Elasticsearch 8.17.0

Repeat the following on each Elasticsearch node:

```bash
wget https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-8.17.0-linux-x86_64.tar.gz
tar -xzf elasticsearch-8.17.0-linux-x86_64.tar.gz
cd elasticsearch-8.17.0
```

### Configure the Cluster

Edit `config/elasticsearch.yml` on each node (change `node.name` per host):

```yaml
cluster.name: nexus-search
node.name: es-node-1        # es-node-2, es-node-3 on other hosts

network.host: 0.0.0.0
http.port: 9200

discovery.seed_hosts:
  - es-node-1
  - es-node-2
  - es-node-3
cluster.initial_master_nodes:
  - es-node-1
  - es-node-2
  - es-node-3

xpack.security.enabled: true
xpack.security.transport.ssl.enabled: true
```

### JVM Heap Tuning

In `config/jvm.options` set the heap to **half of available RAM** (max 32 GB):

```
-Xms4g
-Xmx4g
```

### Start and Verify

```bash
# Start on each node
./bin/elasticsearch -d

# Verify cluster health
curl -u elastic:password https://es-node-1:9200/_cluster/health?pretty
```

Expected output includes:

```json
{
  "cluster_name": "nexus-search",
  "status": "green",
  "number_of_nodes": 3
}
```

---

## Application Node Setup

Perform the following steps on **every** application node.

### Install the Application

```bash
# Clone the repository
git clone <repository-url> /opt/nexus-repository
cd /opt/nexus-repository

# Create a virtual environment
python3.13 -m venv venv
source venv/bin/activate

# Install production dependencies
pip install --no-cache-dir -r requirements.txt
```

### Configure Environment Variables

Create a `.env` file or export the variables directly. **All nodes must share the same values** for security-sensitive settings.

```bash
# ── Database (shared PostgreSQL) ──
DATABASE_URL=postgresql://nexus:secure-password@pg-host:5432/nexus_repository
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=20

# ── BlobStore (shared filesystem) ──
BLOBSTORE_TYPE=file
BLOBSTORE_PATH=/shared/blobs

# ── Search (shared Elasticsearch) ──
SEARCH_BACKEND=elasticsearch
ELASTICSEARCH_URL=https://es-node-1:9200
ELASTICSEARCH_USERNAME=elastic
ELASTICSEARCH_PASSWORD=<es-password>

# ── Security (MUST be identical on all nodes) ──
SECRET_KEY=<generate-a-64-char-random-hex>
JWT_SECRET=<generate-a-64-char-random-hex>

# ── Logging ──
LOG_LEVEL=INFO
LOG_FORMAT=json

# ── Application ──
FLASK_ENV=production
NEXUS_CONFIG=production

# ── Gunicorn ──
GUNICORN_WORKERS=9
GUNICORN_BIND=0.0.0.0:8081
GUNICORN_TIMEOUT=120
```

> **CRITICAL — Secret Consistency:** `SECRET_KEY` and `JWT_SECRET` **must** be identical across every node. Flask sessions are signed with `SECRET_KEY` and JWT tokens are verified with `JWT_SECRET`. Differing values will cause authentication failures when a request is routed to a different node. Generate secrets once and distribute them via a secrets manager (HashiCorp Vault, AWS Secrets Manager, etc.). **Never commit secrets to version control.**

Generate a secure random key:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### Initialize the Database (Run Once)

Execute Alembic migrations from **one node only**. The shared PostgreSQL database will be accessible to all nodes afterwards.

```bash
source venv/bin/activate
cd /opt/nexus-repository
flask db upgrade
```

> Do **not** run `flask db upgrade` on multiple nodes simultaneously — concurrent migration execution may cause conflicts.

### Start the Application with Gunicorn

```bash
source venv/bin/activate
cd /opt/nexus-repository
gunicorn --config gunicorn.conf.py "src.app:create_app()"
```

Each node independently executes the six-phase startup sequence:

1. **KERNEL** — Verify Python runtime and load configuration
2. **SCHEMAS** — Confirm database schema is up to date (read-only check after initial migration)
3. **STORAGE** — Initialize File BlobStore connection to `/shared/blobs`
4. **SECURITY** — Configure the multi-realm authentication chain and RBAC
5. **CAPABILITIES** — Register format handlers (Maven, npm, Docker, NuGet, PyPI, APT, Raw)
6. **SERVICES** — Start the scheduler (if enabled on this node) and health monitoring

The node begins accepting requests only after all six phases complete successfully.

### Graceful Shutdown

Gunicorn handles `SIGTERM` by:

1. Stopping acceptance of new connections
2. Completing in-flight requests (within the configured timeout)
3. Flushing pending audit events
4. Cleanly stopping the APScheduler instance
5. Closing database and Elasticsearch connections

Send `SIGTERM` for a graceful stop:

```bash
kill -TERM $(cat /opt/nexus-repository/gunicorn.pid)
```

---

## Load Balancer Configuration

### Nginx

```nginx
upstream nexus_backend {
    least_conn;
    server nexus-node-1:8081;
    server nexus-node-2:8081;
    server nexus-node-3:8081;
}

server {
    listen 443 ssl http2;
    server_name nexus.example.com;

    ssl_certificate     /etc/nginx/ssl/nexus.crt;
    ssl_certificate_key /etc/nginx/ssl/nexus.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    # Allow large artifact uploads (Docker layers, Maven multi-module builds)
    client_max_body_size 10G;

    # Proxy timeouts for long-running uploads
    proxy_connect_timeout 60s;
    proxy_send_timeout    600s;
    proxy_read_timeout    600s;

    location / {
        proxy_pass http://nexus_backend;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering  off;
    }

    # Health check endpoint — no authentication required
    location /service/rest/v1/status/check {
        proxy_pass http://nexus_backend;
        access_log off;
    }

    # Prometheus metrics endpoint
    location /service/metrics {
        proxy_pass http://nexus_backend;
    }
}
```

### HAProxy (Alternative)

```
global
    maxconn 4096
    ssl-default-bind-ciphersuites TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384
    ssl-default-bind-options ssl-min-ver TLSv1.2

defaults
    mode    http
    timeout connect  5s
    timeout client  60s
    timeout server 600s
    option  httplog

frontend nexus_front
    bind *:443 ssl crt /etc/haproxy/nexus.pem
    default_backend nexus_back

backend nexus_back
    balance leastconn
    option httpchk GET /service/rest/v1/status/check
    http-check expect status 200

    server nexus1 nexus-node-1:8081 check inter 5s rise 2 fall 3
    server nexus2 nexus-node-2:8081 check inter 5s rise 2 fall 3
    server nexus3 nexus-node-3:8081 check inter 5s rise 2 fall 3
```

---

## Gunicorn Worker Tuning

The recommended formula for Gunicorn sync workers is:

```
workers = (2 × CPU_CORES) + 1
```

| Node CPU Cores | Recommended Workers | Approximate Memory per Worker | Total Worker Memory |
|----------------|--------------------:|------------------------------:|--------------------:|
| 4 | 9 | ~150 MB | ~1.35 GB |
| 8 | 17 | ~150 MB | ~2.55 GB |
| 16 | 33 | ~150 MB | ~4.95 GB |

**Key configuration variables:**

```bash
# Number of worker processes
GUNICORN_WORKERS=9

# Bind address and port
GUNICORN_BIND=0.0.0.0:8081

# Request timeout (seconds) — increase for large artifact uploads
GUNICORN_TIMEOUT=120

# Graceful restart timeout
GUNICORN_GRACEFUL_TIMEOUT=30
```

Ensure total worker memory fits comfortably within available RAM, leaving headroom for the OS, PostgreSQL client libraries, and BlobStore I/O buffers.

Reference the project's `gunicorn.conf.py` file for the complete set of tunable parameters.

---

## Session Management Across Nodes

In a clustered deployment, requests may be routed to any node. The application handles this through shared secrets and database-backed state:

| Concern | Mechanism |
|---------|-----------|
| Flask session signing | `SECRET_KEY` — identical on all nodes |
| JWT token verification | `JWT_SECRET` — identical on all nodes |
| User/role data | Stored in PostgreSQL, accessed from any node |
| Request-scoped state | Flask's `g` object is per-request and never crosses nodes |
| API key validation | Keys stored in PostgreSQL, queryable from any node |

**Sticky sessions** at the load balancer are **optional**. Because all session-critical state is either cryptographically signed (cookies) or stored in the shared database, any node can serve any request. Sticky sessions may reduce token re-verification overhead but are not required for correctness.

---

## Scheduler Configuration for Clustered Mode

The application uses APScheduler with a `SQLAlchemyJobStore` backed by the shared PostgreSQL database. Task definitions and execution history are visible from any node.

**Preventing duplicate job execution:**

In a multi-node deployment, only **one** scheduler instance should actively execute jobs. Two approaches are supported:

### Option A — Designated Scheduler Node

Enable the scheduler on exactly one node:

```bash
# On the designated scheduler node
SCHEDULER_ENABLED=true

# On all other nodes
SCHEDULER_ENABLED=false
```

If the scheduler node fails, manually enable the scheduler on another node.

### Option B — Database-Level Locking

Enable the scheduler on all nodes and rely on APScheduler's database-backed locking to prevent concurrent execution of the same job:

```bash
# On all nodes
SCHEDULER_ENABLED=true
SCHEDULER_USE_LOCKING=true
```

This approach provides automatic failover — if the node that acquired the lock goes down, another node will pick up execution on the next trigger.

### Task Visibility

Regardless of which node executes the tasks, all task definitions (`TaskDefinition`) and execution records (`TaskExecution`) are stored in PostgreSQL and can be viewed or managed via the REST API from any node:

```
GET  /service/rest/v1/admin/tasks
POST /service/rest/v1/admin/tasks
```

---

## Failover Procedures

### Application Node Failure

1. The load balancer detects the failure via the health check endpoint (`GET /service/rest/v1/status/check`).
2. The failed node is automatically removed from the backend pool.
3. Remaining nodes continue serving all requests without interruption.
4. **Recovery:** Restart or replace the failed node. It will rejoin the pool once the health check passes.

### PostgreSQL Failover

| Topology | Procedure |
|----------|-----------|
| **Single primary** | Restore from backup or restart the service. Application nodes will retry connections automatically via SQLAlchemy pool reconnect. |
| **Streaming replication** | Promote the standby: `pg_ctl promote -D /var/lib/postgresql/16/main`. Update the DNS alias or `DATABASE_URL` if not using a virtual IP. |
| **Patroni** | Failover is automatic. Patroni promotes the most up-to-date replica and updates the service endpoint. No application changes required. |
| **Cloud-managed (RDS, Cloud SQL)** | Automatic failover with a brief DNS switchover. No application changes required. |

### Shared Filesystem Failure

- **NFS server failure** is a single point of failure that affects all nodes. All blob operations will fail until the NFS server is restored.
- **Mitigation:** Use GlusterFS replicated volumes for filesystem-level HA.
- **Long-term migration:** Consider switching to the S3 BlobStore for the highest storage availability. See [Container-Native Deployment](container.md) for S3 configuration details.

### Elasticsearch Node Failure

- An Elasticsearch cluster with 3+ nodes can tolerate the loss of one node.
- Cluster health will degrade from `green` to `yellow`, but search functionality remains operational.
- **Recovery:** Replace or restart the failed node. Elasticsearch will automatically rebalance shards once the node rejoins the cluster.

---

## Monitoring and Health Checks

### Application Health

Every node exposes a health check endpoint that reports the status of all backend connections:

```
GET /service/rest/v1/status/check
```

Example response:

```json
{
  "status": "healthy",
  "checks": {
    "database": "ok",
    "blobstore": "ok",
    "elasticsearch": "ok",
    "scheduler": "ok"
  }
}
```

### Prometheus Metrics

Every node exposes application metrics in Prometheus text exposition format:

```
GET /service/metrics
```

Metrics include:

| Metric | Type | Description |
|--------|------|-------------|
| `http_requests_total` | Counter | Total HTTP requests by method, path, and status |
| `http_request_duration_seconds` | Histogram | Request latency distribution |
| `blob_operations_total` | Counter | Blob read/write/delete operations |
| `auth_attempts_total` | Counter | Authentication attempts by realm and result |
| `active_repositories_count` | Gauge | Number of active repositories |
| `scheduler_executions_total` | Counter | Scheduled task executions by task type and result |

### Prometheus Scrape Configuration

```yaml
# prometheus.yml
scrape_configs:
  - job_name: nexus
    scrape_interval: 15s
    static_configs:
      - targets:
          - nexus-node-1:8081
          - nexus-node-2:8081
          - nexus-node-3:8081
    metrics_path: /service/metrics
```

### Recommended Alerts

| Alert | Condition | Severity |
|-------|-----------|----------|
| Node down | Health check returns non-200 for > 30 s | Critical |
| DB connection pool exhaustion | Pool usage > 90% | Warning |
| BlobStore disk space | Free space < 10% | Critical |
| Elasticsearch cluster yellow | Cluster status != green for > 5 min | Warning |
| Elasticsearch cluster red | Cluster status == red | Critical |
| High request latency | p99 latency > 5 s for > 5 min | Warning |
| Authentication failures | auth_attempts_total{result="failure"} spike | Warning |

---

## Scaling

### Horizontal Scaling (Application Tier)

1. Provision a new application node with identical configuration.
2. Install the application, configure environment variables (same secrets), mount the shared filesystem.
3. Start Gunicorn — the node completes the six-phase startup independently.
4. Add the new node to the load balancer backend pool.

No database migration or data redistribution is required.

### Vertical Scaling (Application Tier)

- Increase CPU and RAM on existing nodes.
- Adjust `GUNICORN_WORKERS` using the `(2 × CPU_CORES) + 1` formula.
- Restart Gunicorn to apply the new worker count.

### Database Scaling

| Strategy | Use Case |
|----------|----------|
| **Vertical** | Increase CPU/RAM on the PostgreSQL server |
| **Read replicas** | Offload read-heavy queries to standby replicas (requires application-level read routing) |
| **Connection pooling (pgBouncer)** | Multiplex many application connections over fewer database connections |

### Storage Scaling

- **NFS:** Expand the underlying volume and resize the export.
- **GlusterFS:** Add bricks and rebalance the volume.

### Search Scaling

- Add Elasticsearch data nodes to the cluster. Shards will rebalance automatically.
- Increase replica count for improved query throughput.

---

## Security Hardening

### Inter-Component TLS

Encrypt all traffic between application nodes and backend services:

| Connection | TLS Configuration |
|------------|-------------------|
| Application → PostgreSQL | Set `?sslmode=require` (or `verify-full`) in `DATABASE_URL` |
| Application → Elasticsearch | Use `https://` in `ELASTICSEARCH_URL`; configure CA cert if self-signed |
| Application → NFS | Enable NFS with Kerberos (`sec=krb5p`) for encryption and authentication |
| Application → GlusterFS | Enable GlusterFS TLS (`gluster volume set nexus-blobs client.ssl on`) |
| Load balancer → Application | Optional (TLS termination at LB is typical; add if required by policy) |

### Authentication Security

- PostgreSQL: Use `scram-sha-256` authentication — **never** `md5` or `trust`.
- Elasticsearch: Enable `xpack.security.enabled: true` with TLS transport encryption.
- Application: All user passwords are hashed with `bcrypt` before storage. **No plaintext credentials** are stored anywhere.

### Secret Management

- `SECRET_KEY` and `JWT_SECRET` must be generated as cryptographically random values (minimum 32 bytes / 64 hex characters).
- Distribute secrets via a dedicated secrets manager (HashiCorp Vault, AWS Secrets Manager, Azure Key Vault, GCP Secret Manager).
- **Never** store secrets in:
  - Version control (`.env` files committed to git)
  - Unencrypted configuration files on disk
  - Application logs or audit events
  - Support ZIP bundles (the application automatically redacts credentials)

### Network Segmentation

Recommended VLAN or subnet separation:

```
┌─────────────────────────────┐
│       DMZ / Public          │
│   Load Balancer (443)       │
└─────────────┬───────────────┘
              │
┌─────────────▼───────────────┐
│     Application Tier        │
│  Nexus Nodes (8081)         │
└──────┬──────────┬───────────┘
       │          │
┌──────▼──┐  ┌───▼────────────┐
│ Database│  │  Storage Tier   │
│  Tier   │  │ NFS / GlusterFS│
│ PG:5432 │  │ ES:9200/9300   │
└─────────┘  └────────────────┘
```

- Only the load balancer should be exposed to external networks.
- Application nodes communicate only with the database, storage, and search tiers.
- Database and storage services should **not** be directly accessible from external networks.

### Operating System Hardening

- Run application nodes as a **non-root** system user (e.g., `nexus`).
- Apply the principle of least privilege for filesystem permissions.
- Keep the OS and Python runtime patched with security updates.
- Enable firewall rules to restrict traffic to required ports only.

---

## Related Resources

- [Configuration Reference](../configuration.md) — Complete list of all configuration settings and environment variables
- [Architecture Overview](../architecture.md) — System design, five-layer architecture, and component details
- [Standalone Deployment](standalone.md) — Single-server deployment with SQLite and local File BlobStore
- [Container-Native Deployment](container.md) — Docker and Kubernetes deployment with PostgreSQL and S3 BlobStore
