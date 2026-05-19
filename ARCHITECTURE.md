# Architecture -- Sobel Distributed Image Processing System

## 1. Design Decisions

### 1.1 Compute Platform: Google Kubernetes Engine (GKE)

**Decision**: Run all application services (frontend, backend, split, joiner, dlq-monitor) as Kubernetes Deployments on GKE. Workers run externally on Compute Engine VMs via a Managed Instance Group (MIG).

**Reasoning**: GKE provides a managed control plane, eliminating the operational overhead of etcd management. It offers native Horizontal Pod Autoscaler (HPA) integration, automatic cluster autoscaling for node pools, and tight GCP-native integration with Cloud Load Balancing, Cloud Storage, and Secret Manager. The course explicitly requires demonstrating container orchestration -- GKE is the canonical GCP choice.

**Alternative dismissed**: GCE VMs directly (no Kubernetes) would force manual deployment scripts, custom health checking, and ad-hoc scaling logic. This contradicts the core learning objective of container orchestration.

### 1.2 Node Pool Separation

**Decision**: Two node pools -- `infra-pool` (1 node, e2-standard-2, tainted) for RabbitMQ and Redis; `app-pool` (2-4 nodes, autoscaled, e2-standard-2) for all application Deployments.

**Reasoning**: Stateful infrastructure (RabbitMQ, Redis) is I/O sensitive. A noisy neighbor from a CPU-spiking application pod could degrade messaging latency. Separate pools with taints and tolerations ensure resource isolation. The `app-pool` cluster autoscaler can grow independently of the fixed `infra-pool`.

**Alternative dismissed**: Single shared node pool is simpler but risks RabbitMQ/Redis being evicted or starved during application scaling events. Demonstrating resource isolation adds academic value.

### 1.3 Worker VM Strategy: Managed Instance Group (MIG)

**Decision**: Workers run as Compute Engine VMs in a regional MIG. Scaling is driven by a custom Cloud Monitoring metric derived from RabbitMQ queue depth. A `worker-autoscaler` service (Kubernetes Deployment) polls queue depth and adjusts MIG target size.

**Reasoning**: MIG provides automatic VM replacement on failure, zonal distribution, and programmatic scaling without Terraform state drift. The custom-metric autoscaler demonstrates cross-service orchestration.

**Alternative dismissed**: Terraform apply/destroy per scale event is slow (30-60s), creates state drift, and is not how production systems scale compute. Kubernetes Jobs for workers would be simpler but violates the explicit requirement for external VMs.

### 1.4 Messaging Topology (RabbitMQ)

**Decision**: Five exchanges with specific exchange/queue/binding topology:

| Exchange | Type | Purpose |
|---|---|---|
| `sobel.images` | direct | Routes new-image events to the split service |
| `sobel.fragments` | direct | Routes fragment tasks to workers (round-robin by routing key) |
| `sobel.fragments.dlx` | direct | Dead Letter Exchange for timed-out or rejected fragment tasks |
| `sobel.results` | fanout | Broadcasts completed fragments to both joiner and frontend |
| `sobel.final` | direct | Routes final image-completion events to the backend |

**Reasoning**: Direct exchanges give targeted routing (one producer, one consumer type). Fanout for results ensures the joiner and frontend both receive every fragment completion without coupling their consumption rates. Each exchange has a clean semantic boundary, making topology easier to debug and extend.

**Alternative dismissed**: Single exchange with routing keys only creates entangled routing logic where normal traffic and DLX/retry traffic share the same namespace. Google Pub/Sub is fully managed but the course requires RabbitMQ specifically.

### 1.5 Retry with Exponential Backoff

**Decision**: Two-level retry strategy.

1. Connection-level: RabbitMQ and Redis client backoff on connect/reconnect: 1s, 2s, 4s, 8s, 16s, capped at 30s. Jitter +/-25% prevents thundering herd.
2. Message-level: DLQ monitor inspects `x-death` header count. If less than 3 retries, republish with delay = 2^retry_count seconds. If 3 or more, log as permanent failure and alert.

**Reasoning**: Connection backoff prevents reconnect storms when RabbitMQ or Redis restarts. Message-level backoff via DLQ gives transient worker failures a chance to self-heal. The cap at 3 retries prevents infinite failure loops.

**Alternative dismissed**: Retry in worker itself (NACK + requeue) keeps failed messages at the front of the queue, blocking other messages and risking infinite loops if the failure is deterministic.

### 1.6 State Management (Redis)

**Decision**: Redis stores in-flight image metadata. Key schema:

- `image:{image_id}:meta` -- hash: `total_fragments`, `status`, `filename`
- `image:{image_id}:fragments` -- set: completed fragment IDs
- `image:{image_id}:result` -- string: GCS path of reassembled result

TTL: 1 hour after image completion (auto-cleanup).

**Reasoning**: The joiner needs atomic set operations to track completed fragments. Without Redis, the joiner would require its own persistent database, adding coupling. Redis's built-in key expiry provides automatic garbage collection.

**Alternative dismissed**: Joiner in-memory state is lost on pod restart, breaking delivery guarantees for in-flight images. Google Memorystore is managed but costs beyond free tier.

### 1.7 Object Storage (Google Cloud Storage)

**Decision**: GCS buckets for original uploads (`sobel-uploads`) and processed results (`sobel-results`). 7-day lifecycle policy. Fragments are ephemeral -- stored in worker temporary files, not persisted.

**Reasoning**: GCS decouples storage from compute -- images survive cluster teardown. Signed URLs enable secure direct download. Lifecycle management controls costs.

**Alternative dismissed**: PersistentVolumeClaim ties storage to the cluster lifecycle and requires PV provisioning and backup management.

### 1.8 Secrets Management

**Decision**: Kubernetes Secrets for RabbitMQ credentials, Redis password, and GCS service account key. Created via `kubectl create secret` in CI/CD, referenced in Deployment manifests as environment variables.

**Reasoning**: Simpler than Google Secret Manager while still demonstrating secrets-as-resources. GKE encrypts etcd at rest, and access is controlled via RBAC.

**Alternative dismissed**: Google Secret Manager adds Workload Identity setup, IAM bindings, and operational complexity without changing the distributed systems learning outcomes.

### 1.9 CI/CD Pipeline Architecture

**Decision**: Four GitHub Actions workflows:

| Pipeline | Trigger | Purpose |
|---|---|---|
| `pipeline-k8s-provisioning.yaml` | Manual (`workflow_dispatch`) | Terraform plan -> apply for cluster, networking, GCS |
| `pipeline-infrastructure-services.yaml` | Manual | `kubectl apply` for RabbitMQ and Redis |
| `pipeline-applications.yaml` | Push to `main` (path-filtered) | Build images -> push to Artifact Registry -> rolling deploy |
| `pipeline-worker-vms.yaml` | Manual | Terraform plan -> apply for MIG |

**Reasoning**: Infrastructure changes are destructive and should be manual. Application deployments are frequent and should be automated.

**Alternative dismissed**: Single monolithic pipeline triggers full deploys on any change. Separate pipelines mirror real-world CI/CD patterns.

### 1.10 Docker Build Strategy

**Decision**: Multi-stage builds for all services. Builder stage (`python:3.11-slim`) installs dependencies; runtime stage copies only the venv and application code.

**Reasoning**: Smaller images (faster pulls), no build tools in production images (reduced attack surface), and satisfies the explicit requirement.

**Alternative dismissed**: Single-stage builds are simpler but produce larger images with unused tooling.

### 1.11 Observability

**Decision**: Structured JSON logging (all services -> stdout -> Cloud Logging), health check endpoints (`/health` and `/ready`), custom Cloud Monitoring metric for queue depth.

**Reasoning**: Cloud Logging and Cloud Monitoring are GCP-native and require no additional infrastructure deployment. This satisfies the observability requirement without the operational overhead of Prometheus/Grafana.

**Alternative dismissed**: Self-hosted Prometheus + Grafana consumes cluster resources and adds PVs. The learning value for distributed systems is marginal compared to the operational cost.

### 1.12 Python Async Model

**Decision**: All services use `asyncio` with `aio_pika` (AMQP), `redis-py` (async), and `httpx` (async HTTP). Workers use the same stack but without FastAPI -- pure `asyncio.run()` consumer loop.

**Reasoning**: FastAPI is async-native. Blocking the event loop with synchronous RabbitMQ clients would serialize request handling and require thread pools. The async stack avoids thread-management complexity for I/O-bound messaging.

**Alternative dismissed**: `pika` with `run_in_executor` is synchronous and wastes threads on I/O waits. `aio_pika` matches FastAPI's concurrency model natively.

### 1.13 Sobel Implementation

**Decision**: Use `numpy` + `scipy.ndimage.sobel` on grayscale-converted fragment arrays. Output normalized as uint8 edge-map, encoded as PNG via Pillow.

**Reasoning**: Established, vectorized library. Manual convolution would be error-prone and detracts from the core learning objective (distributed processing).

**Alternative dismissed**: OpenCV (`cv2`) adds a ~200MB native dependency for a single function. SciPy is lighter and sufficient.

---

## 2. Architecture Diagram

```
+----------------------------------------------------------------+
|                        User Browser                             |
+-----------------+----------------------------------------------+
                  | HTTP upload (POST /api/images)
                  | EventSource (GET /events/{id})
                  v
+----------------------------------------------------------------+
|                  GKE Cluster (apps namespace)                   |
|                                                                 |
|  +----------+  +----------+  +----------+  +----------------+  |
|  | Frontend |  | Backend  |  |  Split   |  | dlq-monitor    |  |
|  | 2 replicas|  | 2 replicas|  | 1 replica|  | 1 replica     |  |
|  +-----+----+  +----+-----+  +----+-----+  +-------+--------+  |
|        |             |             |                |            |
|        +------+------+             |                |            |
|               |                    |                |            |
|        +------v------+        +----v----+     +----v----+      |
|        |   RabbitMQ  |        |  Redis  |     |   GCS   |      |
|        |  (infra ns) |        |(infra ns)|     | (ext.)  |      |
|        +------+------+        +---------+     +---------+      |
|               |                                                 |
+---------------+-------------------------------------------------+
                | fragment.task
                v
+----------------------------------------------------------------+
|           Managed Instance Group (Compute Engine)               |
|                                                                 |
|  +----------+  +----------+  +----------+                      |
|  | Worker 1 |  | Worker 2 |  | Worker N |  (0-10 VMs)          |
|  | Sobel    |  | Sobel    |  | Sobel    |                      |
|  +-----+----+  +-----+----+  +-----+----+                      |
|        |             |             |                            |
|        +------+------+             |                            |
|               | fragment.result    |                            |
+---------------+-------------------------------------------------+
                |
                v
         +--------------+
         |sobel.results |
         |  (fanout)    |
         +--+--------+--+
            |        |
            v        v
      +--------+ +----------+
      | Joiner | | Frontend |
      | 1 repl | |(SSE push)|
      +----+---+ +----------+
           | image.completed
           v
      +----------+
      | Backend  |--> User notified
      +----------+
```

---

## 3. System Components

| Component | Type | Replicas (min-max) | Ports | Dependencies |
|---|---|---|---|---|
| Frontend | Deployment | 2-4 | 80 (HTTP) | Backend, RabbitMQ (fanout queue) |
| Backend | Deployment | 2-8 | 8000 (HTTP) | RabbitMQ, Redis, GCS |
| Split | Deployment | 1-4 | 8000 (HTTP, admin) | RabbitMQ, GCS, Redis |
| Joiner | Deployment | 1-4 | 8000 (HTTP, admin) | RabbitMQ, Redis, GCS |
| Worker | MIG (external) | 0-10 | N/A (consumer only) | RabbitMQ, GCS |
| DLQ Monitor | Deployment | 1 | 8000 (HTTP, admin) | RabbitMQ |
| Worker Autoscaler | Deployment | 1 | N/A | RabbitMQ mgmt API, Compute Engine API |
| RabbitMQ | StatefulSet | 1 | 5672 (AMQP), 15672 (mgmt) | PVC (10Gi) |
| Redis | StatefulSet | 1 | 6379 | PVC (5Gi) |

### Component Responsibilities

- **Frontend**: Serves the HTML/JS upload page. Provides a Server-Sent Events endpoint (`/events/{image_id}`) that subscribes to fragment results via `sobel.results` fanout queue, pushing real-time progress to the browser. Proxies final result downloads via signed GCS URL.

- **Backend**: REST API -- receives image uploads, stores originals in GCS, publishes `image.new` to `sobel.images`, exposes status polling (`GET /api/images/{id}/status`) reading from Redis, and returns signed result URLs upon completion.

- **Split**: Consumes `image.new` from `sobel.images`. Downloads the original PNG from GCS. Uses Pillow to split into a 4x4 grid (16 fragments). Publishes one `fragment.task` message to `sobel.fragments` per fragment. Writes image metadata to Redis.

- **Joiner**: Consumes `fragment.result` messages from `sobel.results` via its anonymous queue bound to the fanout. Adds each completed fragment ID to the Redis set for its image. When the set reaches cardinality 16, downloads all fragments from GCS, reassembles them using Pillow, uploads the final result to GCS, and publishes `image.completed` to `sobel.final`.

- **Worker**: Runs on Compute Engine VMs. Connects to RabbitMQ, consumes from `fragments.pending`, downloads the fragment from GCS, applies the Sobel filter using `scipy.ndimage.sobel`, uploads the result back to GCS, and publishes `fragment.result` to `sobel.results`. Uses publisher confirms. Handles errors by NACKing without requeue (message flows to DLX).

- **DLQ Monitor**: Consumes from `fragments.dead` (DLX queue). Inspects `x-death` header. On retry <= 3, republishes to `sobel.fragments` with configurable delay. On retry >= 3, logs permanent failure and publishes an error notification.

- **Worker Autoscaler**: Polls the RabbitMQ management API (`GET /api/queues/%2f/fragments.pending`) every 30 seconds. Calculates target MIG size: `ceil(messages_ready / WORKER_FRAGMENT_CAPACITY)`, clamped to [0, MAX_WORKERS]. Calls Compute Engine API `instanceGroupManagers.resize` on the MIG.

---

## 4. Resilience Patterns

See [docs/RESILIENCE-STRATEGIES.md](./docs/RESILIENCE-STRATEGIES.md) for detailed treatment.

Summary:

- DLQ: Messages NACKed or TTL-expired flow to `sobel.fragments.dlx` -> `fragments.dead` queue -> dlq-monitor inspects and decides retry vs permanent fail
- Connection backoff: 1-2-4-8-16-30s with +/-25% jitter on RabbitMQ and Redis connections
- Message retry: Up to 3 retries with exponential delay, then permanent failure log
- Graceful degradation: Services report readiness based on upstream dependency health (RabbitMQ, Redis)
- Stateless design: All services except the Worker are stateless Kubernetes Deployments, restart-tolerant

---

## 5. Processing Flow

1. User uploads a PNG image via the frontend web UI
2. Frontend sends `POST /api/images` with multipart/form-data to Backend
3. Backend stores the original image in GCS (`sobel-uploads/{uuid}.png`)
4. Backend writes image metadata to Redis: `image:{uuid}:meta = {total_fragments: 16, status: "uploaded"}`
5. Backend publishes `image.new` message to `sobel.images` exchange, routing key `images.new`
6. Split service (auto-ack off) consumes the `image.new` message
7. Split downloads the original from GCS, opens with Pillow
8. Split divides the image into 16 fragments (4x4 grid)
9. For each fragment (fragment_id 0-15), Split uploads to GCS as `sobel-uploads/{uuid}/fragment_{id}.png`
10. Split publishes 16 `fragment.task` messages to `sobel.fragments` exchange
11. Workers consume `fragment.task` messages from `fragments.pending` queue
12. Each worker downloads the fragment, applies Sobel filter, uploads result to GCS
13. Worker publishes `fragment.result` to `sobel.results` fanout exchange
14. Joiner receives `fragment.result` -- adds `fragment_id` to Redis set via SADD
15. Frontend (SSE) also receives `fragment.result` -- pushes progress to browser
16. When Redis set cardinality reaches 16, Joiner downloads all 16 processed fragments
17. Joiner reassembles the full processed image
18. Joiner uploads the assembled result to `sobel-results/{uuid}/final.png`
19. Joiner publishes `image.completed` to `sobel.final` exchange
20. Backend receives `image.completed`, updates Redis status to `completed`
21. Frontend detects `image.completed` from SSE stream, enables download

---

## 6. Scalability

### Horizontal Pod Autoscaling (HPA)

- Backend: CPU-based HPA, target 70%, min 2, max 8 replicas
- Frontend: CPU-based HPA, target 70%, min 2, max 4 replicas
- Split: Custom metric HPA based on queue depth
- Joiner: Custom metric HPA based on fragment volume

### Worker Scaling (MIG)

- `worker-autoscaler` polls RabbitMQ every 30s
- Target size = `max(0, min(MAX_WORKERS, ceil(queued_fragments / 8)))`
- Default MAX_WORKERS = 10, WORKER_FRAGMENT_CAPACITY = 8
- Minimum workers = 0 (scale to zero)
- Scaling cooldown: 60 seconds between resize calls

### Cluster Autoscaling (GKE)

- `app-pool` autoscales node count from 2 to 4
- `infra-pool` is fixed at 1 node

### Bottleneck Analysis

- Worker count: primary throughput bottleneck. More workers = more parallelism. 10 VMs x 1 fragment per ~2s = ~5 fragments/sec.
- Single RabbitMQ: handles tens of thousands of messages/second -- not a bottleneck at student-project scale.
- Single Redis: similarly sub-millisecond SADD/SMEMBERS -- not a bottleneck.
- GCS: upload/download throughput is the main I/O bottleneck per fragment. Workers in the same region minimize latency.
