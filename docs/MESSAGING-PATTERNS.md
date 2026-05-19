# Messaging Patterns -- Sobel Distributed Image Processing System

## 1. Exchange and Queue Topology

### Topology Diagram

```
                         sobel.images (direct)
                         +----------+
                         | images   |
                         +----+-----+
                              | routing: images.new
                              v
                      +---------------+
                      |  images.new   |
                      |  queue        |<--- consumed by Split
                      +---------------+

                         sobel.fragments (direct)
                         +----------+
                         | fragments|
                         +----+-----+
                              | routing: fragments.pending
                              v
                      +-----------------------+
                      | fragments.pending     |
                      | x-dead-letter-exchange: sobel.fragments.dlx |
                      | x-message-ttl: 30000  |
                      | x-max-priority: 10    |
                      +-----------------------+
                                   |
                                   | (NACK / TTL expiry)
                                   v
                    sobel.fragments.dlx (direct)
                    +----------+
                    | fragments|
                    +----+-----+
                         | routing: fragments.dead
                         v
                   +---------------+
                   | fragments.dead|
                   | queue         |<--- consumed by DLQ Monitor
                   +---------------+

                         sobel.results (fanout)
                         +----------+
                         | results  |
                         +----+-----+
                              |
              +---------------+---------------+
              v                               v
      +----------------+           +-------------------+
      | results.joiner |           | results.dashboard |
      | (auto-delete)  |           | (auto-delete)     |
      | consumed by    |           | consumed by       |
      | Joiner         |           | Frontend (SSE)    |
      +----------------+           +-------------------+

                         sobel.final (direct)
                         +----------+
                         | final    |
                         +----+-----+
                              | routing: images.completed
                              v
                      +-------------------+
                      | images.completed  |
                      | queue             |<--- consumed by Backend
                      +-------------------+
```

### Exchange Definitions

| Exchange Name | Type | Durable | Auto-delete | Arguments |
|---|---|---|---|---|
| `sobel.images` | direct | true | false | -- |
| `sobel.fragments` | direct | true | false | -- |
| `sobel.fragments.dlx` | direct | true | false | -- |
| `sobel.results` | fanout | true | false | -- |
| `sobel.final` | direct | true | false | -- |

### Queue Definitions

| Queue Name | Durable | Exclusive | Auto-delete | Arguments |
|---|---|---|---|---|
| `images.new` | true | false | false | -- |
| `fragments.pending` | true | false | false | `x-dead-letter-exchange: sobel.fragments.dlx`, `x-message-ttl: 30000`, `x-max-priority: 10` |
| `fragments.dead` | true | false | false | -- |
| `results.joiner` | false | false | true | -- |
| `results.dashboard` | false | false | true | -- |
| `images.completed` | true | false | false | -- |

### Binding Definitions

| Source Exchange | Routing Key | Destination Queue |
|---|---|---|
| `sobel.images` | `images.new` | `images.new` |
| `sobel.fragments` | `fragments.pending` | `fragments.pending` |
| `sobel.fragments.dlx` | `fragments.dead` | `fragments.dead` |
| `sobel.results` | (fanout -- no routing key) | `results.joiner` |
| `sobel.results` | (fanout -- no routing key) | `results.dashboard` |
| `sobel.final` | `images.completed` | `images.completed` |

---

## 2. Message Schemas

All messages use JSON serialization with `Content-Type: application/json`.

### image.new

Published by Backend to `sobel.images` when a new image is uploaded.

```json
{
  "image_id": "550e8400-e29b-41d4-a716-446655440000",
  "filename": "my_photo.png",
  "gcs_path": "gs://sobel-uploads/550e8400-e29b-41d4-a716-446655440000.png",
  "total_fragments": 16,
  "width": 512,
  "height": 512,
  "timestamp": "2026-05-19T12:00:00Z"
}
```

### fragment.task

Published by Split to `sobel.fragments` for each image fragment.

```json
{
  "image_id": "550e8400-e29b-41d4-a716-446655440000",
  "fragment_id": 5,
  "row": 1,
  "col": 2,
  "gcs_path": "gs://sobel-uploads/550e8400-e29b-41d4-a716-446655440000/fragment_5.png",
  "width": 128,
  "height": 128,
  "total_fragments": 16,
  "timestamp": "2026-05-19T12:00:05Z"
}
```

### fragment.result

Published by Worker to `sobel.results` (fanout) after processing a fragment.

```json
{
  "image_id": "550e8400-e29b-41d4-a716-446655440000",
  "fragment_id": 5,
  "row": 1,
  "col": 2,
  "gcs_path": "gs://sobel-results/550e8400-e29b-41d4-a716-446655440000/fragment_5_sobel.png",
  "status": "success",
  "error": null,
  "processing_time_ms": 342,
  "worker_id": "worker-3",
  "timestamp": "2026-05-19T12:00:08Z"
}
```

### fragment.result (error)

```json
{
  "image_id": "550e8400-e29b-41d4-a716-446655440000",
  "fragment_id": 5,
  "row": 1,
  "col": 2,
  "gcs_path": null,
  "status": "error",
  "error": "Corrupt fragment data: invalid PNG header",
  "processing_time_ms": 50,
  "worker_id": "worker-3",
  "timestamp": "2026-05-19T12:00:08Z"
}
```

### image.completed

Published by Joiner to `sobel.final` when all 16 fragments are reassembled.

```json
{
  "image_id": "550e8400-e29b-41d4-a716-446655440000",
  "result_gcs_path": "gs://sobel-results/550e8400-e29b-41d4-a716-446655440000/final.png",
  "status": "completed",
  "total_fragments": 16,
  "successful_fragments": 16,
  "failed_fragments": 0,
  "total_processing_time_ms": 5400,
  "timestamp": "2026-05-19T12:00:14Z"
}
```

---

## 3. Dead Letter Queue Mechanics

### When Messages Enter the DLQ

A message is dead-lettered (moved from `fragments.pending` to `fragments.dead`) in three scenarios:

1. **Explicit NACK without requeue**: A worker receives a fragment, fails to process it (e.g., corrupt data, GCS error), and NACKs the message with `requeue=False`. RabbitMQ routes it to the DLX.

2. **TTL expiry**: A message sits in `fragments.pending` for 30 seconds without being consumed. This happens if all workers are busy or crashed. The message is automatically dead-lettered.

3. **Queue overflow**: If the queue reaches its length limit (if configured), the oldest message is dead-lettered.

### The `x-death` Header

When RabbitMQ dead-letters a message, it adds an `x-death` header array. Each entry contains:

```json
[
  {
    "count": 1,
    "reason": "rejected",
    "queue": "fragments.pending",
    "time": {
      "timestamp": 1716087608,
      "zone": "UTC"
    },
    "exchange": "sobel.fragments",
    "routing-keys": ["fragments.pending"]
  }
]
```

- `count`: How many times this message has been dead-lettered
- `reason`: `"rejected"` (NACK), `"expired"` (TTL), or `"maxlen"` (overflow)
- `queue`: The queue that dead-lettered it
- `routing-keys`: The original routing key

### DLQ Monitor Processing

The dlq-monitor consumes from `fragments.dead` and executes this decision tree:

```
Receive message from fragments.dead
  |
  +--> Parse x-death header
       |
       +--> If x-death[0].count < 3:
       |      Republish to sobel.fragments with delay
       |        delay = 2^count * 1000 ms
       |        routing_key = "fragments.pending"
       |      ACK the dead message
       |
       +--> If x-death[0].count >= 3:
              Log CRITICAL: "Permanent failure for fragment {fragment_id}"
              Log to permanent-failures file
              Optionally: publish to alert exchange (not implemented)
              ACK the dead message
```

---

## 4. Retry with Exponential Backoff

### Connection-Level Backoff

Both RabbitMQ (`aio_pika`) and Redis (`redis-py`) connections use exponential backoff:

```
initial_delay = 1.0s
multiplier = 2
max_delay = 30.0s
jitter = +/- 25%

Loop:
  attempt += 1
  delay = min(initial_delay * (multiplier ^ (attempt - 1)), max_delay)
  jitter_amount = delay * random.uniform(-0.25, 0.25)
  final_delay = delay + jitter_amount
  sleep(final_delay)
  try:
    connect()
    break
  except ConnectionError as e:
    log_warning(f"Connection attempt {attempt} failed: {e}. Retrying in {final_delay:.1f}s")
```

Pseudocode:

```python
import asyncio
import random

async def connect_with_backoff(connect_fn, label="RabbitMQ"):
    max_delay = 30.0
    delay = 1.0

    for attempt in range(1, 100):  # effectively unlimited
        try:
            connection = await connect_fn()
            print(f"[{label}] Connected on attempt {attempt}")
            return connection
        except Exception as e:
            jitter = delay * random.uniform(-0.25, 0.25)
            actual_delay = delay + jitter
            print(f"[{label}] Attempt {attempt} failed: {e}. Retrying in {actual_delay:.1f}s")
            await asyncio.sleep(actual_delay)
            delay = min(delay * 2, max_delay)

    raise RuntimeError(f"Failed to connect to {label} after maximum attempts")
```

### Message-Level Backoff (DLQ Monitor)

When the dlq-monitor republishes a failed message, it uses delayed delivery:

```
Retry 0 (first failure):  delay = 2^1 = 2s
Retry 1 (second failure): delay = 2^2 = 4s
Retry 2 (third failure):  delay = 2^3 = 8s
Retry 3 (fourth failure): PERMANENT FAILURE (no retry)
```

Note: `x-death[0].count` is 1-based. So `count=1` means one previous failure (2s delay), `count=3` means three previous failures (8s delay), and after counting 3 the next failure triggers permanent failure.

This is implemented by publishing to RabbitMQ with the `x-delay` header (requires the delayed message exchange plugin). If the plugin is not available, the dlq-monitor itself sleeps for the delay duration before republishing.

---

## 5. Pub/Sub Fanout for Results

### How Fanout Works

The `sobel.results` exchange is type `fanout`. Every message published to this exchange is delivered to ALL bound queues, regardless of routing key.

### Two Queues, Two Consumers

1. **results.joiner** (auto-delete): Bound by the Joiner service. The Joiner receives every `fragment.result` message to track completion state in Redis.

2. **results.dashboard** (auto-delete): Bound by the Frontend service. The Frontend receives every `fragment.result` message and pushes the progress update to connected browsers via Server-Sent Events (SSE).

### Delivery Guarantees

- Messages are `delivery_mode=2` (persistent) when published to the fanout exchange
- RabbitMQ stores the message until ALL bound queues have received and stored it
- If a queue has no consumers, messages accumulate in that queue -- they are NOT lost
- Auto-delete queues are deleted only when the last consumer cancels or disconnects

### Consumer Acknowledgements

Both the Joiner and Frontend use explicit acknowledgements (`auto_ack=False`):
- On successful processing: call `message.ack()`
- On failure: call `message.nack(requeue=False)` -- for fanout queues, this typically drops the message (one consumer failure should not affect the other consumer)

---

## 6. Message Sequencing and Idempotency

### Fragment IDs as Sequence Numbers

Each image is split into exactly 16 fragments, assigned IDs 0 through 15. The ID encodes the grid position:

```
Row 0:  [0]  [1]  [2]  [3]
Row 1:  [4]  [5]  [6]  [7]
Row 2:  [8]  [9]  [10] [11]
Row 3:  [12] [13] [14] [15]
```

fragment_id = row * 4 + col

### Duplicate Detection

The Joiner's Redis SADD operation is idempotent:
- `SADD image:{uuid}:fragments 5` adds fragment 5 to the set
- If fragment 5 is already in the set, SADD returns 0 (no change)
- The Joiner checks set cardinality after every SADD -- duplicates don't trigger early reassembly

### What Happens on Worker Crash

1. Worker is processing fragment 5
2. Worker VM crashes mid-processing (network, OOM, power loss)
3. RabbitMQ does NOT receive an ACK for the `fragment.task` message
4. The message remains in `fragments.pending` as "unacked"
5. After 30 seconds (TTL), RabbitMQ dead-letters the message
6. The message moves to `fragments.dead` queue
7. DLQ monitor inspects and republishes with delay
8. A different worker consumes the retried message

### Partial Fragment Upload Safety

If the worker crashed after uploading the fragment result to GCS but before publishing the AMQP confirmation:
- The fragment result is orphaned in GCS (no corresponding message)
- The Joiner never receives a `fragment.result` for that fragment
- After TTL + DLQ + retries, the fragment is re-processed by a new worker
- The new result overwrites the orphaned GCS object (same path)
- The Joiner's SADD handles the duplicate gracefully
