# Vortex Retail Analytics Engine — Architecture Document

## 1. Overview

This system converts raw retail CCTV footage (five 1080p clips from a single
Purplle store) into structured behavioural events and live analytics.
A containerised FastAPI service delivers sub-second per-request latency
under typical load.

---

## 2. High-Level Architecture

```
┌─────────────────────┐    ┌───────────────────────────────┐    ┌──────────────────────┐
│  CCTV clips         │    │  pipeline/                    │    │  POST /events/ingest │
│  CAM 1..5.mp4       │───▶│    YOLOv8-m (Ultralytics)     │───▶│  Idempotent          │
│  1080p · ~20 min    │    │    ByteTrack (supervision)    │    │  Partial-success     │
│                     │    │    zones.py   (geometry)      │    │  207 Multi-Status    │
│                     │    │    staff.py   (HSV + dwell)   │    └──────────┬───────────┘
│                     │    │    reentry.py (5-min buffer)  │               │
│                     │    │    emit.py    (JSONL + POST)  │               ▼
│                     │    └───────────────────────────────┘    ┌──────────────────────┐
│                     │                                        │  FastAPI  (app/)     │
└─────────────────────┘                                        │    Pydantic v2       │
                                                               │    Structured logs   │
                                                               │    Fault handlers    │
                                                               └──────────┬───────────┘
                                                                          │
                                                                          ▼
                                                               ┌──────────────────────┐
                                                               │  PostgreSQL 16       │
                                                               │  (asyncpg)           │
                                                               │  events (PK=event_id)│
                                                               │  pos_transactions    │
                                                               └──────────┬───────────┘
                                                                          │
                            ┌──────────────┬──────────────┬───────────────┼───────────────┐
                            ▼              ▼              ▼               ▼               ▼
                       /metrics       /funnel        /heatmap        /anomalies       /health
                            │              │              │               │
                            └──────────────┴──────┬───────┴───────────────┘
                                                  │
                                       ┌──────────▼──────────┐
                                       │  Result Cache (3s)  │
                                       └──────────┬──────────┘
                                                  │
                                                  ▼
                                       ┌─────────────────────┐
                                       │ /ws/stores/{id}     │
                                       │ WebSocket endpoint  │
                                       └──────────┬──────────┘
                                                  │
                                    ┌─────────────┴─────────────┐
                                    ▼                           ▼
                       ┌────────────────────────┐  ┌────────────────────────┐
                       │ Terminal Dashboard     │  │ Web Dashboard          │
                       │ (Rich via WebSocket)   │  │ (HTML/JS via WebSocket)│
                       └────────────────────────┘  └────────────────────────┘
```

---

## 3. Event Pipeline

Video is transformed into structured events through these stages:

1. **Clip sequencing** — `pipeline/run_windows.ps1` (or `run_linux.sh`)
   iterates clips per `config/store_layout.json`, each assigned a
   `camera_id` and role (`entry` | `floor` | `stockroom`).

2. **Person detection** — Each sampled frame (5 fps) is run through
   YOLOv8-m at `imgsz=320` with `conf=0.20`.

3. **Multi-object tracking** — ByteTrack (via `supervision`) assigns
   persistent `track_id` values per camera, converting per-frame detections
   into continuous visitor trajectories.

4. **Region event generation** — `pipeline/zones.py` converts track positions
   into events:
   - **`ENTRY` / `EXIT`** via signed boundary crossing at the entry camera
   - **`ZONE_ENTER` / `ZONE_EXIT` / `ZONE_DWELL`** via point-in-polygon
     with a 30-second cadence
   - **`BILLING_QUEUE_JOIN` / `LEAVE` / `ABANDON`** via the billing polygon
     with 5-second minimum residency

5. **Return-visitor detection** — `pipeline/reentry.py` maintains a 5-minute
   appearance-histogram buffer. Any `ENTRY` whose 3-bin HSV signature
   matches a recent `EXIT` (cosine similarity ≥ 0.90) is reclassified
   as `REENTRY`.

6. **Employee detection** — `pipeline/staff.py` uses HSV uniform match
   plus a dwell-pattern heuristic (>2 distinct zones in <30 seconds).

7. **Event dispatch** — `pipeline/emit.py` writes every event to
   `data/events.jsonl` AND posts batches (up to 500) to
   `POST /events/ingest`. JSONL is the durable backup; the API is the
   query surface.

8. **Ingest validation** — `POST /events/ingest` validates each event
   against the Pydantic `BehaviourEvent` schema, deduplicates on `event_id`
   via `ON CONFLICT DO NOTHING`, and returns `{accepted, duplicates, rejected}`
   for partial-success semantics.

9. **POS integration** — `pipeline/post_pos.py` reads POS data and posts it
   to `POST /pos/ingest` for conversion correlation.

---

## 4. Event Schema

The Pydantic `BehaviourEvent` model in `app/models.py` is the single canonical
type emitted by the CV pipeline and consumed by the API:

| Endpoint Family | Key Fields |
|---|---|
| **`/metrics`** | `event_type=ENTRY`, `is_staff`, `zone_id` + `dwell_ms` for `ZONE_DWELL`, `metadata.queue_depth` |
| **`/funnel`** | `event_type ∈ {ENTRY, REENTRY, ZONE_ENTER, BILLING_QUEUE_JOIN}` + POS join |
| **`/heatmap`** | `zone_id`, `event_type ∈ {ZONE_ENTER, ZONE_DWELL}`, `dwell_ms` |
| **`/anomalies`** | `metadata.queue_depth`, `BILLING_QUEUE_JOIN` timestamps, missing `ZONE_ENTER` |

The `metadata: dict[str, Any]` field holds type-specific extras
(`queue_depth`, `sku_zone`, `session_seq`) without schema churn. `event_id`
is a `UUID` for offline minting and deduplication without coordination.

---

## 5. Persistence & Idempotency

**PostgreSQL 16** is the primary data store:

- **Primary-key idempotency** — `PK (event_id)` + `ON CONFLICT DO NOTHING`
  enables bit-exact re-run safety.
- **Secondary indexes** on `(store_id, timestamp)` and
  `(event_type, store_id, timestamp)` keep analytics at O(log n).
- **Asyncpg + SQLAlchemy async** keeps the event loop responsive under
  concurrent ingest and read operations.
- **SQLite** (`aiosqlite`) provides zero-setup isolation for tests. The schema
  is defined once in `app/db.py` and works on both dialects.

---

## 6. Live Streaming

The **WebSocket endpoint** (`/ws/stores/{store_id}`) is backed by an
in-memory result cache with a 3-second TTL:

- **Single DB query set per cache interval** — whether 1 or 100 clients
  connect, the database is queried at most once every 3 seconds.
- **Unified payload** — each push contains metrics, funnel, heatmap,
  anomalies, and health in a single JSON frame.
- **Dual dashboard support** — web dashboard and Rich terminal dashboard
  consume the same WebSocket endpoint.

---

## 7. Observability

Every request emits one structured JSON log line with `trace_id`, `endpoint`,
`store_id`, `latency_ms`, `event_count`, and `status_code`. The `x-trace-id`
header is propagated on responses for end-to-end correlation.

`/health` reports per-store last-event timestamps and flags `STALE_FEED`
for any store whose feed exceeds 10 minutes of silence.

---

## 8. Fault Handling

Four global fault handlers in `app/errors.py` ensure no stack trace
ever reaches the client:

| Exception | Response | Behaviour |
|---|---|---|
| `RequestValidationError` | **422** | Per-field `detail` array |
| `SQLAlchemyError` | **503** | Returns `request_id` — API stays up even if the DB blips |
| `HTTPException` | Variable | Safe `{error, request_id}` envelope |
| Catch-all `Exception` | **500** | Safe `{error, request_id}` envelope |

---

## 9. Camera & Zone Mapping

Zone configuration in `config/store_layout.json` aligns to the physical
store blueprint (`Brigade Road - Store layout.xlsx`):

| Camera | Logical ID | Physical Zone | Role |
|---|---|---|---|
| CAM 3 | `CAM_ENTRY_01` | Glass doorway entrance | Entry boundary crossing |
| CAM 1 | `CAM_FLOOR_SKIN` | Skincare section | Product floor zone |
| CAM 2 | `CAM_FLOOR_MAKEUP` | Makeup section | Product floor zone |
| CAM 5 | `CAM_CASH_COUNTER` | Cash Counter | Billing zone |
| CAM 4 | `CAM_STOCKROOM` | Stockroom | Staff-only (`force_is_staff: true`) |

Queue detection dynamically resolves the `type: "billing"` flag in the
layout config — no camera IDs are hardcoded in billing logic.

---

## 10. Testing Approach

| Layer | Scope | Dependencies |
|---|---|---|
| **Pure-Python unit tests** | Geometry, boundary crossing, region tracking, return-visitor buffer, dispatcher buffering | None (no torch / OpenCV) — runs in <1 second |
| **In-process HTTP tests** | Every API endpoint via `httpx.AsyncClient` over `ASGITransport` | Fresh SQLite DB per test for isolation |
| **Named test cases** | Partial-success, 413, 422, idempotency, staff-exclusion, WebSocket, POS reclassification | — |

**41/41 tests pass.** CV-runtime modules (`detect.py`, `tracker.py`,
`post_pos.py`, `staff.py`) are excluded from unit-test line count
because they require heavy ML wheels and are exercised by the end-to-end
pipeline.

---

## 11. AI-Assisted Design Decisions

### 11.1 Event Schema — Override

The AI proposed a **split-table schema** with separate tables per entity family.
We **overrode this** to use a single `events` table with an open `metadata` dict.
Every analytics query joins across event types for the same visitor in the same
time window — a split schema would require `UNION ALL` everywhere.

### 11.2 Employee Detection — Override

The AI defaulted to **CLIP zero-shot classification** per bounding box.
We **overrode this** with a two-signal heuristic: HSV colour match + dwell
pattern. This runs at near-zero cost on CPU.

### 11.3 Return-Visitor Handling — Override

The AI proposed **per-frame cosine similarity against every active track**
in full history (O(all_history)). We **overrode this** by bounding the search
to a 5-minute sliding cache so cost is O(live_candidates).

---

## 12. Overlap Suppression

The entry camera and floor cameras partially overlap. Without deduplication,
a person walking from the entry into the skincare zone would produce
duplicate `ZONE_ENTER` events.

The `pipeline/cross_camera.py` module implements an `OverlapFilter`:

1. Events are keyed by `(visitor_id, zone_id)`.
2. Same key within 3 seconds → second emission is suppressed.
3. Cache self-prunes every 50 frames to cap memory.

---

## 13. Scalability (40-Store Projection)

| Bottleneck | Current | At 40 Stores |
|---|---|---|
| **DB writes** | Single Postgres, ~50 events/s | Pool exhaustion at 2000 events/s. Fix: pgBouncer, partition by `store_id`. |
| **Result cache** | In-memory dict | 200 entries. Still fits. Fix: LRU eviction. |
| **WebSocket connections** | One WS per client | Fix: Redis Pub/Sub fan-out for stateless API pods. |
| **Pipeline compute** | Sequential YOLO inference | Fix: GPU batch inference, horizontal workers via message queue. |

Everything is `store_id`-keyed, enabling horizontal partitioning.

---

## 14. Known Limitations

- **Re-entry false matches** on similar clothing (3-bin HSV). Production fix: learned Re-ID embeddings.
- **Zone polygon imprecision** at edges — brief flickering. Production fix: hysteresis buffer.
- **Queue depth jitter** from occlusion. Production fix: moving-average filter.
- **HSV staff classification** under variable lighting. Production fix: per-camera calibration.

---

## 15. Intentionally Out of Scope

- **Cross-store federation** — schema scales but no inter-store reconciliation.
- **Authentication** — not required by the challenge.
- **Custom detector training** — pretrained YOLOv8-m suffices for person detection.
- **Kubernetes / Prometheus / tracing** — observability stops at structured JSON logs + `/health`.
