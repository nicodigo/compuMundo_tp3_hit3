# Resilience Strategies -- Sobel Distributed Image Processing System

## 1. Resilience Overview

### Definition

The system continues operating correctly despite failures. Failures are expected, not exceptional. Every component is designed to degrade gracefully when its dependencies are unavailable, and to recover automatically when they return.

### Failure Model

| Failure Type | Component(s) Affected | Impact |
|---|---|---|
| RabbitMQ crash | All async processing | No new images processed; in-flight fragments may timeout |
| Redis crash | Backend, Joiner, Split | Cannot track fragment completion; partial fragment sets orphaned |
| Worker VM crash | In-progress fragment | Fragment times out (TTL), enters DLQ, retried by another worker |
| GCS unavailable | All services | Uploads fail, fragments cannot be downloaded/results not stored |
| Pod eviction | Any stateless pod | Restarts within seconds, no data loss |
| Network partition | Cross-component communication | Connection backoff loops, messages accumulate in queues |

### Layered Approach

1. **Connection resilience**: Application-level retry with backoff for RabbitMQ and Redis connections
2. **Message resilience**: Dead Letter Queue with exponential retry for failed fragment processing
3. **Infrastructure resilience**: Kubernetes pod restarts, StatefulSet PVCs, GKE node auto-repair
4. **Graceful degradation**: Services report themselves unhealthy when dependencies are unavailable

---

## 2. Connection Resilience -- Exponential Backoff

### Algorithm

Every service that connects to RabbitMQ or Redis uses the same backoff strategy:

```
initial_delay: 1.0s
multiplier:    2.0
max_delay:     30.0s
jitter_range:  +/-25% of calculated delay
```

### RabbitMQ Connection Loop

```python
import asyncio, random

async def connect_rabbitmq(amqp_url: str) -> aio_pika.RobustConnection:
    delay = 1.0
    max_delay = 30.0
    attempt = 0

    while True:
        attempt += 1
        try:
            connection = await aio_pika.connect_robust(amqp_url)
            log.info("RabbitMQ connected on attempt %d", attempt)
            return connection
        except (aio_pika.exceptions.AMQPConnectionError, OSError) as e:
            jitter = delay * random.uniform(-0.25, 0.25)
            actual = delay + jitter
            log.warning(
                "RabbitMQ connection attempt %d failed: %s. "
                "Retrying in %.1fs", attempt, e, actual
            )
            await asyncio.sleep(actual)
            delay = min(delay * 2, max_delay)
```

### Redis Connection Loop

```python
import aioredis

async def connect_redis(redis_url: str) -> aioredis.Redis:
    delay = 1.0
    max_delay = 30.0
    attempt = 0

    while True:
        attempt += 1
        try:
            redis = await aioredis.from_url(redis_url)
            await redis.ping()
            log.info("Redis connected on attempt %d", attempt)
            return redis
        except (aioredis.ConnectionError, OSError, TimeoutError) as e:
            jitter = delay * random.uniform(-0.25, 0.25)
            actual = delay + jitter
            log.warning(
                "Redis connection attempt %d failed: %s. "
                "Retrying in %.1fs", attempt, e, actual
            )
            await asyncio.sleep(actual)
            delay = min(delay * 2, max_delay)
```

### Why Not Fixed-Delay Retry?

Fixed-delay (e.g., retry every 3 seconds) causes **reconnection storms**. If RabbitMQ or Redis goes down, every service detects it simultaneously and begins retrying in lockstep. All services hammer the restarting server at the exact same moment. Exponential backoff with jitter spreads these retries across a wide time window, giving the server room to recover.

---

## 3. Message Resilience -- Dead Letter Queue

### Lifecycle

```
                    Worker acks successfully
                    +---------------------------+
                    |                           |
                    v                           |
fragment.task --> Worker processing --> result published to sobel.results
  (consumed)                                   (happy path)
                    |
                    | Worker NACKs (requeue=False)
                    | or TTL expires (30s)
                    v
               Dead Letter Exchange
          (sobel.fragments.dlx)
                    |
                    v
               fragments.dead queue
                    |
                    v
               DLQ Monitor
                    |
        +-----------+-----------+
        |                       |
   count < 3               count >= 3
        |                       |
        v                       v
  Republish to            Log CRITICAL
  sobel.fragments         Alert: permanent failure
  with delay
```

### DLQ Monitor Decision Tree (Detailed)

```python
async def process_dead_message(message: aio_pika.IncomingMessage):
    async with message.process():
        death_header = message.headers.get("x-death", [])
        if not death_header:
            # No death info -- treat as first failure
            retry_count = 0
        else:
            retry_count = death_header[0].get("count", 1)

        if retry_count < 3:
            delay_ms = (2 ** retry_count) * 1000  # 2s, 4s, 8s
            log.info(
                "Retrying fragment (attempt %d/%d) in %dms",
                retry_count + 1, 3, delay_ms
            )
            republish_with_delay(message.body, delay_ms)
        else:
            payload = json.loads(message.body)
            log.critical(
                "Permanent failure for fragment %d of image %s. "
                "Error history: %s",
                payload.get("fragment_id"),
                payload.get("image_id"),
                death_header
            )
            write_permanent_failure(payload)
```

### What Makes a Failure "Retryable"?

A failure is retryable if the underlying cause might be transient:

| Failure | Retryable? | Typical x-death reason |
|---|---|---|
| Worker VM crashes mid-processing | Yes | `expired` (TTL) |
| Network glitch during GCS download | Yes | `rejected` |
| RabbitMQ connection reset | Yes | `expired` |
| Corrupt image data (bad PNG) | No | `rejected` |
| GCS bucket not found | No | `rejected` |
| Out of disk space on worker | Maybe | `rejected` (retry 3 times) |

The dlq-monitor does NOT distinguish between retryable and permanent failures by error type -- it uses pure retry count. A corrupt fragment will fail all 3 retries and reach permanent failure. This is by design: distinguishing failure types adds complexity and small chances of false negatives (treating a retryable failure as permanent).

---

## 4. Infrastructure Resilience

### Kubernetes Pod Restarts

- All stateless services use `RestartPolicy: Always` (default)
- Liveness probe (`/health`): if the process is alive but stuck, the probe fails and Kubelet restarts the pod
- Readiness probe (`/ready`): if the service can't connect to RabbitMQ, Redis, or GCS, it's removed from Service endpoints

### StatefulSet Resilience (RabbitMQ, Redis)

- PersistentVolumeClaim (PVC): message data and cache state survive pod restarts
- Headless service: enables stable DNS names for stateful discovery
- StatefulSet ordinal naming ensures deterministic pod identity (not critical for a single-replica setup, but follows best practice)

### GKE Node Auto-Repair

- GKE periodically checks node health via the node's `NodeProblemDetector`
- Unhealthy nodes are automatically cordoned and drained
- Pods are rescheduled to healthy nodes within 1-2 minutes

### Regional MIG for Workers

- Workers are distributed across zones within the region
- If one zone goes down, workers in other zones continue processing
- The MIG auto-heals: if a VM is terminated (zone failure, crash), a replacement is created automatically

---

## 5. Graceful Degradation

### What Happens When Redis is Unavailable

| Service | Behavior |
|---|---|
| Backend | `/ready` returns 503. Upload requests fail immediately (`503 Service Unavailable`). Application returns errors with clear "system degraded" message. |
| Split | `/ready` returns 503. Cannot write fragment meta. Consumer waits (connection backoff) until Redis returns. |
| Joiner | `/ready` returns 503. Cannot track fragment completions. Messages accumulate in `results.joiner`. When Redis recovers, Joiner catches up. |
| Frontend | Shows "System Degraded -- Image processing temporarily unavailable" message. SSE endpoints return 503. |

### What Happens When RabbitMQ is Unavailable

| Service | Behavior |
|---|---|
| Backend | `/ready` returns 503. Upload accepted and stored in GCS, but cannot publish `image.new`. Uploads queued in memory (best-effort, risk of loss on backend pod restart). |
| Split | Cannot consume. Messages accumulate in `images.new`. |
| Joiner | Cannot consume. Messages accumulate in `results.joiner`. |
| Worker | Connection backoff. Cannot consume or publish. |
| Frontend | Can still serve static pages. Upload UI shows "Cannot submit -- system unavailable". |

### What Happens with Zero Workers

1. Split publishes 16 fragment messages to `fragments.pending`
2. No workers are available to consume them
3. Messages accumulate in the queue
4. Worker autoscaler detects: `fragments.pending` messages_ready > 0 for 30 seconds
5. Autoscaler calls MIG resize to add workers (up to 10)
6. Workers start up (30-60 seconds)
7. Workers begin consuming the backlog
8. Autoscaler detects queue draining, reduces workers after cooldown
9. If queue remains empty for 5 minutes, autoscaler scales to 0

### What Happens When GCS is Unavailable

Upload and download operations fail at the first API call. The system stops processing new images entirely. In-flight processing continues until workers need to download fragments or upload results, at which point they NACK and the messages enter the DLQ retry cycle.

---

## 6. Testing Resilience (Chaos Scenarios)

### Scenario 1: Kill RabbitMQ Pod

**Action**: `kubectl delete pod -n infra -l app=rabbitmq`

**Expected behavior**:
1. All RabbitMQ connections drop
2. Connection backoff starts in all services (1s, 2s, 4s...)
3. Readiness probes fail -- services removed from Service endpoints
4. StatefulSet controller recreates the pod (PVC preserves messages)
5. RabbitMQ starts, loads persisted data from PVC
6. Connection backoff succeeds -- all services reconnect
7. Readiness probes succeed -- services rejoin Service endpoints
8. Processing resumes from where it left off (no message loss)

**Observation window**: ~10-30 seconds total disruption.

### Scenario 2: Kill a Worker VM

**Action**: `gcloud compute instances delete sobel-worker-xxxx`

**Expected behavior**:
1. In-progress fragment messages become unacked in RabbitMQ
2. After 30 seconds (TTL), messages are dead-lettered
3. DLQ monitor detects `x-death` count = 1 (first failure)
4. DLQ monitor republishes with 2-second delay
5. Another worker picks up the retried fragment
6. MIG auto-healer creates a replacement VM
7. New VM starts consumer, connects, begins processing

**Observation window**: ~40 seconds from deletion to fragment reprocessing.

### Scenario 3: Flood with 100 Images (1600 Fragments)

**Action**: Upload 100 PNG images simultaneously via a script

**Expected behavior**:
1. `fragments.pending` queue depth spikes to 1600
2. Worker autoscaler detects queue depth, scales MIG from 0 to 10
3. Workers start, consume fragments sequentially
4. Processing time: 1600 fragments / (10 workers * 1 fragment / 2s) = ~320 seconds
5. As queue drains, autoscaler reduces workers
6. After queue empty for 5 minutes, workers scale to 0

### Scenario 4: Simulate a Corrupt Fragment

**Action**:
```bash
# Upload a valid PNG, then corrupt one fragment in GCS
gsutil cp /dev/null gs://sobel-uploads/{uuid}/fragment_7.png
```

**Expected behavior**:
1. Worker consumes fragment 7
2. Worker downloads PNG data, fails to decode (Pillow raises an error)
3. Worker NACKs with requeue=False
4. Message enters `fragments.dead`
5. DLQ monitor retries with 2s, 4s, 8s delays
6. All three retries fail
7. DLQ monitor logs permanent failure
8. Joiner waits indefinitely for fragment 7
9. After 1 hour, Redis TTL expires, image is orphaned
10. A monitoring alert should trigger: fragment 7 never completed
