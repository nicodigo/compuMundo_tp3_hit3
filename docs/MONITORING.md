# Monitoring and Observability -- Sobel Distributed Image Processing System

## 1. Observability Stack

### Components

| Component | Purpose | Configuration |
|---|---|---|
| Cloud Logging | Structured log aggregation | Log to stdout/stderr, GKE collects automatically |
| Cloud Monitoring | Metrics and alerting | Custom metrics via Monitoring API; built-in GKE metrics |
| RabbitMQ Management API | Queue-level metrics | Port 15672, queried by worker-autoscaler |
| Health Check Endpoints | Liveness and readiness | `/health` (liveness), `/ready` (readiness) per service |

### Why No Prometheus/Grafana?

A self-hosted Prometheus + Grafana stack would provide powerful dashboards but:
- Requires additional cluster resources (another 2-4 GiB RAM)
- Needs PersistentVolumes for time-series data
- Adds Grafana operator, Prometheus operator configuration
- Duplicates what GKE's Cloud Monitoring already provides
- The operational overhead is disproportionate to the academic value for a distributed systems course

GCP's built-in observability (Cloud Logging + Cloud Monitoring) provides:
- Automatic log collection from all containers (no agent or sidecar required)
- Pre-built GKE dashboards (node CPU/memory, pod churn, cluster health)
- Alerting policies with email, SMS, and Pub/Sub integration
- Custom metrics API for application-level metrics
- All within the free tier (first 50 GiB of logs/month, first 10 custom metrics/month)

---

## 2. Logging Strategy

### Structured Log Format

Every service logs JSON objects to stdout. GKE's Logging agent (fluentd-based, built into the node image) forwards these to Cloud Logging.

```json
{
  "timestamp": "2026-05-19T12:00:00.123Z",
  "level": "INFO",
  "service": "backend",
  "image_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "Image uploaded successfully",
  "extra": {
    "filename": "my_photo.png",
    "size_bytes": 245760,
    "gcs_path": "gs://sobel-uploads/...",
    "processing_time_ms": 150
  }
}
```

### Log Levels Convention

| Level | Usage | Example |
|---|---|---|
| INFO | Normal flow events | "Image uploaded", "Fragment processed", "Message acked" |
| WARN | Recoverable anomalies | "Connection attempt 2 failed, retrying", "DLQ retry attempt 1/3" |
| ERROR | Operation failures | "Cannot download from GCS", "Failed to publish message" |
| CRITICAL | Permanent failures | "Fragment permanently failed after 3 retries", "Connection lost with no recovery" |

### Key Log Events

| Event | Level | Service | Fields |
|---|---|---|---|
| Image upload | INFO | Backend | image_id, filename, size_bytes, gcs_path |
| Fragment published | INFO | Split | image_id, fragment_id, gcs_path |
| Fragment consumed | INFO | Worker | image_id, fragment_id, worker_id |
| Sobel processing complete | INFO | Worker | image_id, fragment_id, processing_time_ms |
| Fragment result published | INFO | Worker | image_id, fragment_id, status |
| Fragment result received | INFO | Joiner | image_id, fragment_id, set_cardinality |
| Image complete | INFO | Joiner | image_id, total_fragments, total_time_ms |
| DLQ message received | WARN | DLQ Monitor | image_id, fragment_id, x_death_count |
| DLQ permanent failure | CRITICAL | DLQ Monitor | image_id, fragment_id, x_death_header |
| Connection backoff start | WARN | All | component, attempt, delay_seconds |
| Connection recovered | INFO | All | component, attempt |
| Health check failed | ERROR | All | component, reason |

### Console Output (Dev Mode)

For local development, logs are formatted as plain text with color:

```
[2026-05-19 12:00:00] [INFO] [backend] Image uploaded: 550e8400 (my_photo.png) [245760 bytes]
[2026-05-19 12:00:01] [INFO] [split] Published fragment 0/16 for image 550e8400
[2026-05-19 12:00:03] [INFO] [worker-3] Sobel processed fragment 5 in 342ms
[2026-05-19 12:00:04] [WARN] [dlq-monitor] DLQ received fragment 7 for image 550e8400 (retry 1/3, 2s delay)
```

In production (Kubernetes), logs are always JSON.

---

## 3. Metrics

### Custom Metrics

All custom metrics are published to Cloud Monitoring via the `google.cloud.monitoring_v3` Python client.

#### `custom.googleapis.com/sobel/fragments/pending`

| Property | Value |
|---|---|
| Type | Gauge (int64) |
| Description | Number of unprocessed fragment tasks in `fragments.pending` queue |
| Reported by | Worker autoscaler (polls RabbitMQ management API every 30s) |
| Used for | MIG autoscaling, alerting |

#### `custom.googleapis.com/sobel/images/processed`

| Property | Value |
|---|---|
| Type | Counter (int64) |
| Description | Total number of images fully processed (all 16 fragments) |
| Reported by | Backend (increments on receiving `image.completed` message) |
| Used for | Throughput dashboard, alerting on throughput drop |

#### `custom.googleapis.com/sobel/fragments/dead`

| Property | Value |
|---|---|
| Type | Rate (double, per minute) |
| Description | Rate of fragments entering the Dead Letter Queue |
| Reported by | DLQ Monitor |
| Used for | Alerting on fragment failure rate |

### Built-in GKE Metrics (Available Automatically)

| Metric | Source | What It Shows |
|---|---|---|
| `kubernetes.io/container/cpu/core_usage_time` | Node-level cAdvisor | CPU consumption per container |
| `kubernetes.io/container/memory/bytes_used` | Node-level cAdvisor | Memory consumption per container |
| `kubernetes.io/node/cpu/allocatable_usage` | GKE | Node-level CPU pressure |
| `kubernetes.io/container/restart_count` | kubelet | Pod restart count (indicating crashes) |
| `kubernetes.io/container/uptime` | kubelet | How long since last container start |

### RabbitMQ Management API Metrics

The worker autoscaler polls these directly (not through Cloud Monitoring):

| Endpoint | Metric | Used For |
|---|---|---|
| `/api/queues/%2f/fragments.pending` | `messages_ready` | MIG target size calculation |
| `/api/queues/%2f/fragments.pending` | `messages_unacked` | Detecting stuck consumers |
| `/api/queues/%2f/fragments.pending` | `consumers` | Ensuring workers are connected |
| `/api/queues/%2f/fragments.dead` | `messages` | Alerting: spike in DLQ messages |
| `/api/overview` | `object_totals.connections` | Network health |

---

## 4. Health Checks

### Liveness Probe (`/health`)

Simple process-alive check. Returns 200 immediately if the process is running. No dependency checks -- a dependency failure should NOT trigger a pod restart (it would cause unnecessary pod churn).

```python
@app.get("/health")
async def health():
    return {"status": "healthy"}
```

### Readiness Probe (`/ready`)

Checks that all required dependencies are reachable. Returns:
- `200 OK` if all dependencies are connected
- `503 Service Unavailable` with details if any dependency is down

```python
@app.get("/ready")
async def readiness(request: Request):
    statuses = {}

    # Check RabbitMQ
    rabbitmq = request.app.state.rabbitmq_channel
    try:
        # Send an empty message to a test queue to verify
        await rabbitmq.default_exchange.publish(
            aio_pika.Message(body=b"ping"),
            routing_key="health.ping",
        )
        statuses["rabbitmq"] = "connected"
    except Exception:
        statuses["rabbitmq"] = "disconnected"

    # Check Redis
    try:
        await request.app.state.redis.ping()
        statuses["redis"] = "connected"
    except Exception:
        statuses["redis"] = "disconnected"

    # Check GCS (backend only)
    if hasattr(request.app.state, "gcs_client"):
        try:
            await request.app.state.gcs_client.list_buckets()
            statuses["gcs"] = "accessible"
        except Exception:
            statuses["gcs"] = "unavailable"

    all_healthy = all(v == "connected" or v == "accessible" for v in statuses.values())
    if all_healthy:
        return {"status": "healthy", "dependencies": statuses}
    else:
        from fastapi import Response
        return Response(
            content=json.dumps({"status": "degraded", "dependencies": statuses}),
            status_code=503,
            media_type="application/json",
        )
```

### Probe Configuration (Kubernetes)

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 15
  timeoutSeconds: 5
  failureThreshold: 3

readinessProbe:
  httpGet:
    path: /ready
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 2
```

---

## 5. Alerting Strategy

### Conceptual Alert Definitions

These are intended as guidance for creating alert policies in Cloud Monitoring. They are NOT configured as Terraform resources (to keep the project within free-tier scope).

| Alert | Condition | Severity | Expected Action |
|---|---|---|---|
| High queue depth | `fragments.pending` > 100 for 5 minutes | Warning | Check worker MIG: is it scaling? Are workers healthy? |
| DLQ spike | `fragments.dead` > 10 in 5 minutes | Warning | Check DLQ monitor logs for failure patterns. Could be corrupt images or a worker bug. |
| Permanent failure | `fragments.dead` with retry >= 3 | Critical | Investigate the specific image/fragment. Check GCS data integrity. |
| Pod crash loop | Any pod restart count > 3 in 10 min | Critical | Check `kubectl describe pod`. Review container logs. |
| Service not ready | Readiness probe failing for > 2 min | Critical | Dependency failure (Redis, RabbitMQ down). Check infra namespace. |
| Zero workers with pending fragments | `fragments.pending` > 20 AND worker count = 0 for 5 min | Critical | Worker autoscaler not working. Check autoscaler logs. |
| Throughput drop | `images.processed` rate < 1 per minute for 10 min | Warning | System idle or processing hung. Investigate. |

### How to Create Cloud Monitoring Alerts

1. Navigate to Cloud Monitoring > Alerting > Create Policy
2. Select the custom metric (e.g., `fragments.pending`)
3. Set condition: Metric threshold > 100 for >= 5 minutes
4. Configure notification: Email to your GCP account email
5. Name the alert: "High Fragment Queue Depth"
6. Save

### Recommended Dashboard

Create a Cloud Monitoring dashboard with these charts:

1. **Fragment Queue Depth** (line chart, `fragments.pending`)
2. **Images Processed Rate** (line chart, `images.processed` per minute)
3. **Worker VM Count** (line chart, MIG target size -- reported by worker-autoscaler)
4. **Pod CPU/Memory** (GKE built-in metrics, per pod)
5. **DLQ Rate** (line chart, `fragments.dead` per minute)
6. **Component Health** (table: current readiness status per service -- scraped from `/ready`)
