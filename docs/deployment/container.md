# Container-Native Deployment Guide

This guide covers the **container-native deployment model** for the Nexus Repository Manager
Python/Flask application using **Docker** and **Kubernetes**.

**Target audience:** Cloud infrastructure engineers, Kubernetes administrators, and DevOps teams
responsible for deploying and operating the Nexus Repository Manager in cloud-native environments.

**Stack summary:**

| Component | Technology | Purpose |
|-----------|------------|---------|
| **Application** | Python 3.13 / Flask 3.1.3 / Gunicorn 23.0.0 | Stateless application containers |
| **Database** | PostgreSQL 16 (RDS / CloudSQL / self-managed) | Metadata persistence (DataStore) |
| **Blob Storage** | AWS S3 with SSE-S3 / SSE-KMS encryption | Binary artifact storage (BlobStore) |
| **Search** | Elasticsearch 8.x (managed or self-hosted) | Full-text component search and indexing |

This deployment model is suitable for cloud-native infrastructure with auto-scaling, managed
services, and container orchestration. All application pods are **stateless** — persistent state
lives exclusively in PostgreSQL and S3, enabling effortless horizontal scaling.

> **Alternative deployment models:**
> - For single-server setups with SQLite and local file storage, see [Standalone Deployment](standalone.md).
> - For on-premises high-availability with shared filesystems, see [Clustered Deployment](clustered.md).

---

## Architecture Overview

The container-native architecture separates the stateless application tier from managed
persistence services, enabling independent scaling of compute and storage resources.

```
                    ┌──────────────────┐
                    │  Ingress / ALB   │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
        ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────┐
        │  Pod 1     │ │  Pod 2     │ │  Pod N     │
        │ (Gunicorn) │ │ (Gunicorn) │ │ (Gunicorn) │
        └──────┬─────┘ └──────┬─────┘ └──────┬─────┘
               │              │              │
     ┌─────────┴──────────────┴──────────────┴──────────┐
     │                                                    │
┌────▼──────────┐  ┌─────────────────┐  ┌──────────────┐
│ PostgreSQL    │  │    AWS S3       │  │ Elasticsearch│
│ (RDS/CloudSQL)│  │   BlobStore    │  │  (Managed)   │
│               │  │ SSE-S3/SSE-KMS │  │              │
└───────────────┘  └─────────────────┘  └──────────────┘
```

**Key architectural decisions:**

- **Stateless application pods** — Each Gunicorn worker process runs the Flask application
  independently. No local state is stored on the pod filesystem, so any pod can be replaced
  or scaled without data loss.
- **Managed PostgreSQL** — AWS RDS, GCP CloudSQL, Azure Database for PostgreSQL, or a
  self-managed PostgreSQL 16 instance provides the DataStore for all repository metadata,
  user accounts, roles, task definitions, audit events, and system configuration.
- **S3 BlobStore** — All binary artifacts (JARs, tarballs, Docker layers, packages) are stored
  in AWS S3 with server-side encryption (SSE-S3 or SSE-KMS with customer-managed keys).
- **Managed Elasticsearch** — AWS OpenSearch, Elastic Cloud, or a self-hosted Elasticsearch 8.x
  cluster provides full-text search and component indexing.
- **Horizontal Pod Autoscaler (HPA)** — Kubernetes automatically scales the number of
  application pods based on CPU and memory utilization thresholds.

---

## Prerequisites

Before deploying the Nexus Repository Manager in container-native mode, ensure the following
tools and services are available:

### Tools

| Tool | Minimum Version | Purpose |
|------|----------------|---------|
| Docker | Engine 24+ or Docker Desktop | Build and run container images |
| Kubernetes | v1.28+ (EKS, GKE, AKS, or self-managed) | Container orchestration |
| kubectl | Matching cluster version | Kubernetes cluster management |
| Helm | v3.14+ (optional) | Helm chart deployment |
| AWS CLI | v2.x | S3 bucket and IAM management |

### Services

| Service | Requirement | Notes |
|---------|------------|-------|
| Container Registry | Push/pull access for the application image | ECR, GCR, ACR, Docker Hub, or private registry |
| AWS S3 | Bucket with appropriate IAM policies | Or S3-compatible storage (MinIO) |
| PostgreSQL 16 | RDS, CloudSQL, or self-managed instance | Minimum 2 vCPUs, 4 GB RAM for production |
| Elasticsearch 8.x | Managed or self-hosted cluster | Minimum 2 GB heap per node |

### Network Requirements

- Application pods must have network access to PostgreSQL (port 5432), S3 (HTTPS/443),
  and Elasticsearch (port 9200).
- Ingress controller must be configured to accept inbound traffic on ports 80/443.
- DNS record pointing to the Ingress load balancer for the desired hostname.

---

## Building the Docker Image

The project includes a multi-stage `Dockerfile` based on `python:3.13-slim` with Gunicorn
as the production WSGI server entry point.

### Dockerfile Overview

```dockerfile
# Stage 1: Install dependencies
FROM python:3.13-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Production image
FROM python:3.13-slim
WORKDIR /app

# Create non-root user for security hardening
RUN groupadd -r nexus && useradd -r -g nexus nexus

# Copy installed Python packages from builder stage
COPY --from=builder /install /usr/local

# Copy application source code
COPY . .

# Set ownership to non-root user
RUN chown -R nexus:nexus /app

USER nexus
EXPOSE 8081

CMD ["gunicorn", "--config", "gunicorn.conf.py", "src.app:create_app()"]
```

**Design decisions:**

- **Multi-stage build** reduces final image size by excluding build tools and caches.
- **Non-root user** (`nexus`) follows container security best practices.
- **`python:3.13-slim`** provides a minimal Debian-based Python runtime.
- **Gunicorn** serves the Flask application factory via `src.app:create_app()`.

### Building and Pushing the Image

```bash
# Build the Docker image
docker build -t nexus-repository:latest .

# Tag for your container registry
docker tag nexus-repository:latest <registry>/nexus-repository:v1.0.0

# Push to the registry
docker push <registry>/nexus-repository:v1.0.0
```

Replace `<registry>` with your container registry URL (e.g., `123456789012.dkr.ecr.us-east-1.amazonaws.com`,
`gcr.io/my-project`, or `myregistry.azurecr.io`).

### Build Arguments (Optional)

You can pass build-time arguments for customization:

```bash
docker build \
  --build-arg PYTHON_VERSION=3.13 \
  --build-arg GUNICORN_VERSION=23.0.0 \
  -t nexus-repository:latest .
```

---

## Docker Compose Setup

For local development and testing, the project provides a `docker-compose.yml` that runs the
complete three-service stack: the Flask application, PostgreSQL 16, and Elasticsearch 8.17.0.

### docker-compose.yml

```yaml
version: "3.8"
services:
  app:
    build: .
    ports:
      - "8081:8081"
    environment:
      - DATABASE_URL=postgresql://nexus:nexus@postgres:5432/nexus_repository
      - BLOBSTORE_TYPE=s3
      - S3_BUCKET=nexus-local
      - S3_REGION=us-east-1
      - S3_ENDPOINT_URL=http://localstack:4566
      - SEARCH_BACKEND=elasticsearch
      - ELASTICSEARCH_URL=http://elasticsearch:9200
      - SECRET_KEY=change-me-in-production
      - JWT_SECRET=change-me-in-production
    depends_on:
      postgres:
        condition: service_healthy
      elasticsearch:
        condition: service_healthy

  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: nexus
      POSTGRES_PASSWORD: nexus
      POSTGRES_DB: nexus_repository
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U nexus"]
      interval: 5s
      timeout: 5s
      retries: 5

  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.17.0
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - "ES_JAVA_OPTS=-Xms512m -Xmx512m"
    volumes:
      - es_data:/usr/share/elasticsearch/data
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:9200/_cluster/health || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  postgres_data:
  es_data:
```

### Running with Docker Compose

```bash
# Start all services in detached mode
docker-compose up -d

# Follow application logs
docker-compose logs -f app

# Verify the application is healthy
curl http://localhost:8081/service/rest/v1/status/check

# Stop all services
docker-compose down

# Stop and remove volumes (clean slate)
docker-compose down -v
```

> **Note:** The Docker Compose setup uses `S3_ENDPOINT_URL=http://localstack:4566` for local
> S3-compatible testing. Add a LocalStack service to docker-compose.yml for full local S3
> emulation, or switch `BLOBSTORE_TYPE` to `file` for simple file-based storage during development.

---

## S3 Bucket Configuration

The S3 BlobStore stores all binary artifacts (Maven JARs, npm tarballs, Docker layers, NuGet
packages, Python wheels, APT .deb files, and raw content). Configure S3 access using one of
three IAM authentication methods.

### Create the S3 Bucket

```bash
aws s3 mb s3://nexus-artifacts --region us-east-1
```

Enable versioning for additional data protection (optional):

```bash
aws s3api put-bucket-versioning \
  --bucket nexus-artifacts \
  --versioning-configuration Status=Enabled
```

### IAM Policy

Create an IAM policy granting the minimum required S3 permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "NexusBlobStoreAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::nexus-artifacts",
        "arn:aws:s3:::nexus-artifacts/*"
      ]
    }
  ]
}
```

If using SSE-KMS encryption, add the following KMS permissions to the same policy:

```json
{
  "Sid": "NexusKMSAccess",
  "Effect": "Allow",
  "Action": [
    "kms:Decrypt",
    "kms:GenerateDataKey",
    "kms:DescribeKey"
  ],
  "Resource": "arn:aws:kms:us-east-1:123456789012:key/<your-kms-key-id>"
}
```

### IAM Authentication Methods

The S3 BlobStore supports three methods of IAM-based authentication. Choose the method
appropriate for your Kubernetes environment.

#### Option A — IAM Roles for Service Accounts (IRSA) — Recommended for EKS

IAM Roles for Service Accounts (IRSA) associates an IAM role directly with a Kubernetes
service account, providing pod-level credential isolation without managing access keys.

```bash
# Create an IAM role linked to a Kubernetes service account
eksctl create iamserviceaccount \
  --name nexus-sa \
  --namespace nexus \
  --cluster my-cluster \
  --attach-policy-arn arn:aws:iam::123456789012:policy/NexusS3Access \
  --approve
```

Reference the service account in your Deployment manifest:

```yaml
spec:
  serviceAccountName: nexus-sa
```

For GKE, use Workload Identity to achieve equivalent functionality.
For AKS, use Azure AD Workload Identity.

#### Option B — Instance Profile (EC2-Based Kubernetes Nodes)

Attach the S3 access IAM policy to the EC2 instance profile used by your Kubernetes worker
nodes. All pods on the node inherit the instance profile credentials.

```bash
# Attach the policy to the node group's IAM role
aws iam attach-role-policy \
  --role-name my-eks-node-role \
  --policy-arn arn:aws:iam::123456789012:policy/NexusS3Access
```

> **Security note:** Instance profiles grant S3 access to all pods on the node.
> IRSA (Option A) is preferred for production because it provides fine-grained,
> pod-level credential scoping.

#### Option C — Explicit Access Keys (Least Recommended)

Pass AWS access keys as environment variables. Use this method only for development or
environments where IAM roles are unavailable.

```bash
S3_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
S3_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

> **Warning:** Storing access keys in Kubernetes Secrets or environment variables is less
> secure than IAM roles. If you must use access keys, rotate them regularly and store them
> in a secrets management system (AWS Secrets Manager, HashiCorp Vault).

### Server-Side Encryption

Configure encryption for all artifacts stored in S3:

#### SSE-S3 (Default) — Amazon-Managed Keys

Amazon manages the encryption keys transparently. No additional configuration is required
beyond setting the encryption type.

```bash
S3_ENCRYPTION=SSE-S3
```

#### SSE-KMS — Customer-Managed Keys

Use AWS Key Management Service for customer-managed encryption keys, providing full control
over key rotation, access policies, and audit logging.

```bash
S3_ENCRYPTION=SSE-KMS
S3_KMS_KEY_ARN=arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012
```

Ensure the IAM role used by the application has `kms:Decrypt`, `kms:GenerateDataKey`, and
`kms:DescribeKey` permissions on the specified KMS key (see the IAM policy above).

---

## Kubernetes Manifests

This section provides the complete set of Kubernetes resource definitions for deploying the
Nexus Repository Manager in a production Kubernetes cluster.

### Namespace

Create a dedicated namespace to isolate Nexus resources:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: nexus
  labels:
    app.kubernetes.io/name: nexus-repository
    app.kubernetes.io/part-of: nexus
```

```bash
kubectl apply -f namespace.yaml
```

### ConfigMap

Store non-sensitive configuration values in a ConfigMap:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: nexus-config
  namespace: nexus
data:
  BLOBSTORE_TYPE: "s3"
  S3_BUCKET: "nexus-artifacts"
  S3_REGION: "us-east-1"
  S3_ENCRYPTION: "SSE-KMS"
  SEARCH_BACKEND: "elasticsearch"
  ELASTICSEARCH_URL: "http://elasticsearch:9200"
  LOG_LEVEL: "INFO"
  LOG_FORMAT: "json"
  GUNICORN_WORKERS: "4"
  GUNICORN_BIND: "0.0.0.0:8081"
  GUNICORN_TIMEOUT: "120"
  METRICS_ENABLED: "true"
  FLASK_ENV: "production"
  DATABASE_POOL_SIZE: "10"
  DATABASE_MAX_OVERFLOW: "20"
```

### Secret

Store sensitive credentials in a Kubernetes Secret. Never store plaintext credentials in
ConfigMaps, environment variables committed to source control, or container images.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: nexus-secrets
  namespace: nexus
type: Opaque
stringData:
  DATABASE_URL: "postgresql://nexus:secure-password@rds-host.region.rds.amazonaws.com:5432/nexus_repository"
  SECRET_KEY: "<generate-with: python -c 'import secrets; print(secrets.token_hex(32))'>"
  JWT_SECRET: "<generate-with: python -c 'import secrets; print(secrets.token_hex(32))'>"
  S3_KMS_KEY_ARN: "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012"
```

> **Production recommendation:** Use an external secrets management solution instead of
> Kubernetes Secrets stored in etcd:
> - **AWS Secrets Manager** with the External Secrets Operator
> - **HashiCorp Vault** with the Vault Secrets Operator
> - **Azure Key Vault** with the Secrets Store CSI Driver

### Deployment

The Deployment creates stateless application pods running Gunicorn with the Flask application:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nexus
  namespace: nexus
  labels:
    app: nexus
    app.kubernetes.io/name: nexus-repository
    app.kubernetes.io/version: "1.0.0"
spec:
  replicas: 3
  selector:
    matchLabels:
      app: nexus
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        app: nexus
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8081"
        prometheus.io/path: "/service/metrics"
    spec:
      serviceAccountName: nexus-sa
      terminationGracePeriodSeconds: 60
      containers:
        - name: nexus
          image: <registry>/nexus-repository:v1.0.0
          ports:
            - containerPort: 8081
              name: http
              protocol: TCP
          envFrom:
            - configMapRef:
                name: nexus-config
            - secretRef:
                name: nexus-secrets
          livenessProbe:
            httpGet:
              path: /service/rest/v1/status/check
              port: 8081
            initialDelaySeconds: 60
            periodSeconds: 30
            timeoutSeconds: 10
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /service/rest/v1/status/check
              port: 8081
            initialDelaySeconds: 30
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
          startupProbe:
            httpGet:
              path: /service/rest/v1/status/check
              port: 8081
            initialDelaySeconds: 10
            periodSeconds: 5
            failureThreshold: 30
          resources:
            requests:
              cpu: "1"
              memory: "2Gi"
            limits:
              cpu: "4"
              memory: "4Gi"
          volumeMounts:
            - name: tmp-uploads
              mountPath: /tmp/nexus-uploads
          lifecycle:
            preStop:
              exec:
                command: ["/bin/sh", "-c", "sleep 5"]
      volumes:
        - name: tmp-uploads
          emptyDir:
            sizeLimit: 5Gi
```

**Probe configuration explained:**

| Probe | Purpose | Behavior on Failure |
|-------|---------|-------------------|
| **livenessProbe** | Checks database connectivity, BlobStore availability, and scheduler state | Kubernetes restarts the pod |
| **readinessProbe** | Verifies all six startup phases (KERNEL → SCHEMAS → STORAGE → SECURITY → CAPABILITIES → SERVICES) completed | Pod removed from Service endpoints; no traffic routed |
| **startupProbe** | Allows extended initial startup time (up to 150 seconds) without triggering liveness failures | Pod is killed and restarted after `failureThreshold × periodSeconds` |

**Resource allocation:**

| Resource | Request | Limit | Rationale |
|----------|---------|-------|-----------|
| CPU | 1 core | 4 cores | Artifact upload/download is CPU-intensive for checksum computation |
| Memory | 2 GiB | 4 GiB | Matches the minimum requirement of 4 CPU / 4 GB RAM per ADR C-001 |

### Service

Expose the application pods within the cluster:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: nexus
  namespace: nexus
  labels:
    app: nexus
spec:
  selector:
    app: nexus
  ports:
    - port: 8081
      targetPort: 8081
      name: http
      protocol: TCP
  type: ClusterIP
```

### Ingress

Configure external access through a Kubernetes Ingress resource with TLS termination:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: nexus
  namespace: nexus
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "10g"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "600"
    nginx.ingress.kubernetes.io/proxy-connect-timeout: "60"
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - nexus.example.com
      secretName: nexus-tls
  rules:
    - host: nexus.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: nexus
                port:
                  number: 8081
```

**Ingress annotations explained:**

| Annotation | Value | Purpose |
|------------|-------|---------|
| `proxy-body-size` | `10g` | Allows large artifact uploads (Docker layers, Maven artifacts) |
| `proxy-read-timeout` | `600` | Extends timeout for slow proxy repository upstream fetches |
| `proxy-send-timeout` | `600` | Extends timeout for large artifact downloads |
| `proxy-connect-timeout` | `60` | Connection timeout for upstream backends |

> **Cloud-specific Ingress:** Replace NGINX Ingress annotations with the appropriate
> annotations for your cloud provider's Ingress controller:
> - **AWS ALB Ingress:** `kubernetes.io/ingress.class: alb`
> - **GKE Ingress:** `kubernetes.io/ingress.class: gce`
> - **Azure Application Gateway:** `kubernetes.io/ingress.class: azure/application-gateway`

---

## Environment Variable Reference

Every configuration setting that differs between deployment environments is overridable
via environment variables. The table below documents all variables used in the container-native
deployment model.

### Core Application Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | Yes | — | Flask secret key for session signing and CSRF protection |
| `JWT_SECRET` | Yes | — | Secret key for JWT token signing (HS256) or path to RS256 private key |
| `FLASK_ENV` | No | `production` | Flask environment (`production`, `development`, `testing`) |
| `LOG_LEVEL` | No | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `LOG_FORMAT` | No | `json` | Log output format (`json` for SIEM integration, `text` for human reading) |

### Database Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes | — | SQLAlchemy database URL (`postgresql://user:pass@host:5432/dbname`) |
| `DATABASE_POOL_SIZE` | No | `10` | SQLAlchemy connection pool size |
| `DATABASE_MAX_OVERFLOW` | No | `20` | Maximum connections above pool size |
| `DATABASE_POOL_TIMEOUT` | No | `30` | Seconds to wait for a connection from the pool |
| `DATABASE_POOL_RECYCLE` | No | `1800` | Seconds before a connection is recycled |

### S3 BlobStore Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BLOBSTORE_TYPE` | No | `file` | BlobStore backend type (`file` or `s3`) |
| `S3_BUCKET` | Yes (if s3) | — | S3 bucket name for artifact storage |
| `S3_REGION` | Yes (if s3) | — | AWS region (e.g., `us-east-1`) |
| `S3_PREFIX` | No | `""` | Key prefix within the S3 bucket |
| `S3_ENCRYPTION` | No | `SSE-S3` | Server-side encryption (`SSE-S3` or `SSE-KMS`) |
| `S3_KMS_KEY_ARN` | Yes (if KMS) | — | ARN of the KMS key for SSE-KMS encryption |
| `S3_ACCESS_KEY_ID` | No | — | Explicit AWS access key (prefer IAM roles) |
| `S3_SECRET_ACCESS_KEY` | No | — | Explicit AWS secret key (prefer IAM roles) |
| `S3_ENDPOINT_URL` | No | — | Custom S3 endpoint (for MinIO or LocalStack) |

### Search Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SEARCH_BACKEND` | No | `whoosh` | Search backend (`whoosh` or `elasticsearch`) |
| `ELASTICSEARCH_URL` | Yes (if es) | — | Elasticsearch cluster URL |
| `WHOOSH_INDEX_PATH` | No | `/app/data/search-index` | Filesystem path for Whoosh index |

### Server Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GUNICORN_WORKERS` | No | `4` | Number of Gunicorn worker processes |
| `GUNICORN_BIND` | No | `0.0.0.0:8081` | Gunicorn bind address and port |
| `GUNICORN_TIMEOUT` | No | `120` | Worker timeout in seconds |
| `GUNICORN_WORKER_CLASS` | No | `sync` | Worker class (`sync`, `gthread`, `gevent`) |

### Metrics Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `METRICS_ENABLED` | No | `true` | Enable Prometheus metrics collection |
| `METRICS_PATH` | No | `/service/metrics` | Metrics endpoint path |

See [Configuration Reference](../configuration.md) for the complete list of all available
settings including LDAP, SAML/OIDC, scheduler, and cleanup policy configuration.

---

## Health Check Probes

The application exposes a health check endpoint at `/service/rest/v1/status/check` that
reports the status of all system components. This endpoint remains available even when other
application endpoints experience failures.

### Liveness Probe

The liveness probe verifies the application process is running and responsive. It checks:

- **Database connectivity** — Can the application execute a query against PostgreSQL?
- **BlobStore availability** — Can the application reach the S3 bucket?
- **Scheduler state** — Is the APScheduler background scheduler running?

If the liveness probe fails, Kubernetes restarts the pod.

```yaml
livenessProbe:
  httpGet:
    path: /service/rest/v1/status/check
    port: 8081
  initialDelaySeconds: 60
  periodSeconds: 30
  timeoutSeconds: 10
  failureThreshold: 3
```

### Readiness Probe

The readiness probe verifies the application has completed all six startup phases and is
ready to serve traffic:

1. **KERNEL** — Runtime environment verified
2. **SCHEMAS** — Database migrations applied
3. **STORAGE** — BlobStore backends initialized
4. **SECURITY** — Authentication chain and RBAC configured
5. **CAPABILITIES** — Format handlers registered
6. **SERVICES** — Scheduler and health monitoring started

Until all phases complete, the readiness probe returns a non-200 status code, and Kubernetes
does not route traffic to the pod.

```yaml
readinessProbe:
  httpGet:
    path: /service/rest/v1/status/check
    port: 8081
  initialDelaySeconds: 30
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 3
```

### Startup Probe

The startup probe provides a generous window for the initial startup sequence, preventing
the liveness probe from killing pods that are still initializing. This is important because
database migrations or BlobStore initialization may take longer than the liveness probe's
`initialDelaySeconds`.

```yaml
startupProbe:
  httpGet:
    path: /service/rest/v1/status/check
    port: 8081
  initialDelaySeconds: 10
  periodSeconds: 5
  failureThreshold: 30  # Allows up to 150 seconds for startup
```

---

## Horizontal Pod Autoscaling

Configure the Horizontal Pod Autoscaler (HPA) to automatically scale the number of
application pods based on resource utilization:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: nexus
  namespace: nexus
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: nexus
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Pods
          value: 1
          periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 30
      policies:
        - type: Pods
          value: 2
          periodSeconds: 60
```

### Scaling Considerations

| Factor | Guidance |
|--------|----------|
| **Stateless design** | All pods are stateless — S3 handles blob storage, PostgreSQL handles metadata. Pods can be added or removed freely. |
| **CPU scaling** | Artifact upload/download drives CPU usage through checksum computation (SHA-1, SHA-256, MD5) and content processing. Scale on CPU utilization at or above 70%. |
| **Memory scaling** | Large artifact uploads and search index operations consume memory. Scale on memory utilization at or above 80%. |
| **Minimum replicas** | Set `minReplicas` to 2 or higher for high availability. If one pod is restarting, the other continues serving traffic. |
| **Connection pool sizing** | Ensure PostgreSQL can handle `maxReplicas * DATABASE_POOL_SIZE` total connections. For 10 pods with pool size 10 = 100 max connections. |
| **Scale-down cooldown** | The 300-second stabilization window prevents flapping during temporary load spikes. |

---

## Persistent Volume Claims

The container-native deployment model is designed to be **stateless at the pod level** — all
persistent data lives in PostgreSQL (metadata) and S3 (binary artifacts). However, persistent
volumes may be needed for specific configurations.

### Whoosh Search Index (If Not Using Elasticsearch)

If you use the Whoosh search backend instead of Elasticsearch, the search index must persist
across pod restarts. This configuration is suitable only for single-replica deployments.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: nexus-search-index
  namespace: nexus
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: gp3
  resources:
    requests:
      storage: 10Gi
```

Mount the PVC in the Deployment:

```yaml
volumeMounts:
  - name: search-index
    mountPath: /app/data/search-index
volumes:
  - name: search-index
    persistentVolumeClaim:
      claimName: nexus-search-index
```

> **Production recommendation:** Use Elasticsearch instead of Whoosh for container-native
> deployments. Whoosh indexes are local filesystem-based and do not support multi-replica
> read/write access.

### Temporary Upload Staging

The application uses temporary disk space for staging artifact uploads before writing to S3.
An `emptyDir` volume is sufficient since this data is ephemeral:

```yaml
volumes:
  - name: tmp-uploads
    emptyDir:
      sizeLimit: 5Gi
```

The `sizeLimit` prevents a runaway upload from consuming all node disk space. Adjust based on
the maximum expected artifact size in your environment.

---

## Database Migration Job

Run database migrations as a Kubernetes Job **before** deploying or updating application pods.
This ensures the database schema is up to date before the application starts serving traffic.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: nexus-db-migrate
  namespace: nexus
  labels:
    app: nexus
    component: migration
spec:
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels:
        app: nexus
        component: migration
    spec:
      serviceAccountName: nexus-sa
      containers:
        - name: migrate
          image: <registry>/nexus-repository:v1.0.0
          command: ["flask", "db", "upgrade"]
          envFrom:
            - configMapRef:
                name: nexus-config
            - secretRef:
                name: nexus-secrets
          resources:
            requests:
              cpu: "500m"
              memory: "512Mi"
            limits:
              cpu: "1"
              memory: "1Gi"
      restartPolicy: Never
  backoffLimit: 3
```

### Running Migrations

```bash
# Apply the migration Job
kubectl apply -f migration-job.yaml

# Monitor migration progress
kubectl logs -n nexus job/nexus-db-migrate -f

# Verify the Job completed successfully
kubectl get jobs -n nexus nexus-db-migrate
```

### Migration Strategy for Upgrades

When deploying a new application version:

1. **Build and push** the new container image with updated code.
2. **Run the migration Job** using the new image to apply schema changes.
3. **Verify the migration** succeeded (`kubectl get jobs`).
4. **Update the Deployment** to use the new image tag (rolling update).

This ordering ensures that new application code always runs against a compatible database
schema. Alembic migrations include both `upgrade()` and `downgrade()` functions for
rollback support.

---

## Graceful Shutdown

The application handles `SIGTERM` signals for clean shutdown by completing in-flight requests,
flushing pending audit events, and stopping the APScheduler before process exit.

### Kubernetes Shutdown Sequence

1. **Kubernetes sends SIGTERM** to the Gunicorn master process.
2. **preStop hook** executes `sleep 5` to allow the load balancer to drain existing connections.
3. **Gunicorn begins graceful shutdown** — stops accepting new connections, waits for workers
   to finish in-flight requests.
4. **Application shutdown handlers** flush pending audit events to the database and cleanly
   stop the APScheduler background scheduler.
5. **After `terminationGracePeriodSeconds`** (60 seconds), Kubernetes sends `SIGKILL` if the
   process has not exited.

### Deployment Configuration

```yaml
spec:
  terminationGracePeriodSeconds: 60
  containers:
    - name: nexus
      lifecycle:
        preStop:
          exec:
            command: ["/bin/sh", "-c", "sleep 5"]
```

### Gunicorn Graceful Timeout

The `gunicorn.conf.py` configures a graceful timeout matching the Kubernetes
`terminationGracePeriodSeconds`:

```python
# gunicorn.conf.py
graceful_timeout = 55  # Slightly less than terminationGracePeriodSeconds
timeout = 120           # Worker request timeout
```

> **Important:** Set `graceful_timeout` to a value slightly less than
> `terminationGracePeriodSeconds` to ensure Gunicorn finishes its shutdown sequence before
> Kubernetes forcefully kills the process with SIGKILL.

---

## Monitoring Integration

The application exposes Prometheus-compatible metrics at `/service/metrics` for integration
with monitoring and alerting infrastructure.

### Prometheus Service Discovery

The Deployment manifest includes standard Prometheus annotations for automatic scrape target
discovery:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "8081"
  prometheus.io/path: "/service/metrics"
```

### Available Metrics

The following Prometheus metrics are exposed:

| Metric | Type | Description |
|--------|------|-------------|
| `http_requests_total` | Counter | Total HTTP requests by method, endpoint, and status code |
| `http_request_duration_seconds` | Histogram | Request latency distribution by endpoint |
| `blob_operations_total` | Counter | BlobStore operations by type and status |
| `auth_attempts_total` | Counter | Authentication attempts by realm and result |
| `active_repositories_count` | Gauge | Number of active repositories by format and type |
| `scheduler_executions_total` | Counter | Scheduled task executions by task type and result |
| `db_pool_connections` | Gauge | Database connection pool usage |

### Grafana Dashboard

Import the following Grafana dashboard panels for operational monitoring:

- **Request Rate and Latency** — `http_requests_total` rate and `http_request_duration_seconds` P50/P95/P99
- **Artifact Operations** — `blob_operations_total` rate by operation type (store, get, delete)
- **Authentication** — `auth_attempts_total` rate with success/failure breakdown by realm
- **Repository Status** — `active_repositories_count` gauge by format (Maven, npm, Docker, NuGet, PyPI, APT, Raw)
- **Resource Utilization** — Kubernetes pod CPU and memory metrics from kube-state-metrics

### Alerting Rules

Configure Prometheus alerting rules for critical conditions:

| Alert | Condition | Severity |
|-------|-----------|----------|
| PodRestartLoop | `increase(kube_pod_container_status_restarts_total[15m]) > 3` | Critical |
| HighLatency | `histogram_quantile(0.95, http_request_duration_seconds) > 0.5` | Warning |
| S3OperationErrors | `rate(blob_operations_total{status="error"}[5m]) > 0` | Critical |
| DBPoolExhausted | `db_pool_connections{state="overflow"} > 0` | Warning |
| ESClusterRed | Elasticsearch cluster health status is red | Critical |

---

## Security Hardening

Apply the following security measures for production container-native deployments:

### Container Security

| Measure | Implementation |
|---------|---------------|
| **Non-root user** | The Dockerfile creates and runs as the `nexus` user. Enforce with `runAsNonRoot: true` in the pod security context. |
| **Read-only root filesystem** | Mount the root filesystem as read-only. Use `emptyDir` volumes for writable paths (`/tmp`, `/tmp/nexus-uploads`). |
| **No privilege escalation** | Set `allowPrivilegeEscalation: false` in the container security context. |
| **Drop all capabilities** | Set `capabilities: { drop: ["ALL"] }` to remove all Linux capabilities. |

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  fsGroup: 1000
containers:
  - name: nexus
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop:
          - ALL
    volumeMounts:
      - name: tmp
        mountPath: /tmp
      - name: tmp-uploads
        mountPath: /tmp/nexus-uploads
volumes:
  - name: tmp
    emptyDir: {}
  - name: tmp-uploads
    emptyDir:
      sizeLimit: 5Gi
```

### Network Policies

Restrict network traffic to only the necessary communication paths:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: nexus-network-policy
  namespace: nexus
spec:
  podSelector:
    matchLabels:
      app: nexus
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              name: ingress-nginx
      ports:
        - protocol: TCP
          port: 8081
  egress:
    - to: []
      ports:
        - protocol: TCP
          port: 5432    # PostgreSQL
        - protocol: TCP
          port: 9200    # Elasticsearch
        - protocol: TCP
          port: 443     # S3 (HTTPS) and upstream registries
```

### Additional Security Measures

- **Pod Security Standards** — Apply the `restricted` Pod Security Standard to the `nexus`
  namespace using Kubernetes Pod Security Admission.
- **Secret encryption at rest** — Enable KMS-backed encryption for Kubernetes Secrets stored
  in etcd using the `EncryptionConfiguration` resource.
- **TLS termination** — Terminate TLS at the Ingress controller or service mesh (Istio, Linkerd)
  level. Use cert-manager for automatic certificate management with Let's Encrypt.
- **Image scanning** — Scan container images for known vulnerabilities before deployment using
  Trivy, Snyk, or your cloud provider's image scanning service.
- **Supply chain security** — Sign container images with Cosign and verify signatures before
  deployment using Kyverno or OPA Gatekeeper admission policies.

---

## Troubleshooting

### Common Issues

| Symptom | Possible Cause | Resolution |
|---------|---------------|------------|
| Pods stuck in `CrashLoopBackOff` | Database connection refused | Verify `DATABASE_URL`, PostgreSQL security group rules, and VPC peering |
| Readiness probe failures | Slow startup | Increase `startupProbe.failureThreshold` or check migration Job logs |
| S3 access denied | Missing IAM permissions | Verify IAM role/policy attachment and S3 bucket policy |
| Elasticsearch connection timeout | Network policy blocking port 9200 | Check NetworkPolicy egress rules |
| High memory usage | Large artifact uploads | Increase pod memory limits and `emptyDir.sizeLimit` |
| Database pool exhaustion | Too many pods for pool size | Increase `DATABASE_MAX_OVERFLOW` or reduce HPA `maxReplicas` |

### Diagnostic Commands

```bash
# Check pod status and events
kubectl get pods -n nexus
kubectl describe pod -n nexus <pod-name>

# View application logs
kubectl logs -n nexus <pod-name> -f

# Check health endpoint directly from within the pod
kubectl exec -n nexus <pod-name> -- curl -s localhost:8081/service/rest/v1/status/check

# Verify database connectivity from within the pod
kubectl exec -n nexus <pod-name> -- python -c "
from sqlalchemy import create_engine, text
import os
engine = create_engine(os.environ['DATABASE_URL'])
with engine.connect() as conn:
    print(conn.execute(text('SELECT 1')).scalar())
"

# Check S3 connectivity from within the pod
kubectl exec -n nexus <pod-name> -- python -c "
import boto3, os
s3 = boto3.client('s3', region_name=os.environ.get('S3_REGION', 'us-east-1'))
print(s3.head_bucket(Bucket=os.environ['S3_BUCKET']))
"

# Generate a support ZIP for diagnostics
curl -u admin:password https://nexus.example.com/service/rest/v1/admin/support/supportzip \
  --output support-bundle.zip
```

---

## Related Resources

- [Configuration Reference](../configuration.md) — Complete list of all configuration settings
- [Architecture Overview](../architecture.md) — System design, five-layer architecture, and component details
- [REST API Documentation](../api/README.md) — API endpoint reference for all modules
- [Standalone Deployment](standalone.md) — Single-server deployment with SQLite and file-based storage
- [Clustered Deployment](clustered.md) — On-premises multi-node deployment with shared filesystem
