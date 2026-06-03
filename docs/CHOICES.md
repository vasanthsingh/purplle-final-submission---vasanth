# CHOICES.md — Engineering Decisions

This document captures the key architectural and engineering decisions made
during the development of Vortex Retail Analytics Engine. Each section covers
what alternatives were evaluated, what the AI assistant initially recommended,
what the system actually uses, and the rationale for that choice.

---

## 1. Detection Model — YOLOv8-m (Medium)

### Options Evaluated

| Model | Strengths | Weaknesses |
|---|---|---|
| **YOLOv8-L (Large)** | High accuracy, widely documented | Slow on CPU, large model size |
| **RT-DETR-L** | Superior occlusion handling | Significantly heavier — impractical for CPU-only inference |
| **YOLOv8-m (Medium)** ✅ | Strong speed/accuracy balance, CPU-friendly | Slightly less accurate on heavily occluded persons |

### AI Recommendation

The AI initially suggested **YOLOv8-L**, reasoning that it is the community
default for person detection. When CPU speed concerns were raised, it pivoted
to **RT-DETR-L**.

### Final Decision

We selected **YOLOv8-m**. Combined with `imgsz=320` and 5 fps subsampling,
the pipeline completes all five 1080p clips in ~46 minutes on standard CPU
hardware — practical for evaluation without a GPU.

### Accepted Trade-offs

- Lower accuracy than RT-DETR-L on occluded persons (mitigated by low
  confidence threshold `0.20` and downstream ByteTrack).
- Reduced `imgsz=320` sacrifices fine-grained detection for CPU speed.

### Measured Results (Brigade Bangalore Dataset)

| Camera | Frames | Events | Time |
|---|---|---|---|
| CAM_ENTRY_01 | 4,436 | 48 | 208 s |
| CAM_FLOOR_SKIN | 4,193 | 38 | 103 s |
| CAM_FLOOR_MAKEUP | 3,774 | 43 | 1,420 s |
| CAM_CASH_COUNTER | 3,465 | 64 | 523 s |
| CAM_STOCKROOM | 3,647 | 0 (staff) | 476 s |
| **Total** | **19,515** | **193** | **~46 min** |

Key statistics: 56 unique visitors, 55 staff-flagged events (28.5%),
average confidence 0.710, 9 REENTRY events (16% of entry activity),
4 BILLING_QUEUE_ABANDON events.

---

## 2. Event Schema — Single Unified Table

### Options Evaluated

| Approach | Strengths | Weaknesses |
|---|---|---|
| **Split entity tables** | Normalised, no sparse rows | `UNION ALL` in every query, complicated PK per table |
| **Unified `events` table** ✅ | Single canonical type, trivial idempotency | Slightly wider rows on average |

### Current Design

One `events` table with a single Pydantic model (`BehaviourEvent` in
`app/models.py`), 10 columns (9 typed + JSON `metadata`). A separate
`pos_transactions` table stores POS data, joined at query time for
conversion metrics.

### AI Recommendation

The AI drafted **four separate tables**: `person_events`, `zone_events`,
`billing_events`, and `pos`. Argument: "each entity family has different
columns."

### Final Decision

Every analytics query joins across event types in the same time window.
A split schema would force `UNION ALL`, complicated idempotency, and
unwieldy Pydantic union types. The unified table eliminated all three issues.

### Accepted Trade-offs

- Slightly wider rows. Mitigated by targeted indexes:
  `(store_id, timestamp)` and `(event_type, store_id, timestamp)`.
- Query writers must filter on `event_type`. Enforced by keeping computations
  in dedicated modules.

---

## 3. API Architecture — Idempotent Batch Ingest with Partial-Success

### The Challenge

How should `POST /events/ingest` behave when a batch mixes valid rows,
duplicate retries, and malformed rows?

### Options Evaluated

| Approach | Strengths | Weaknesses |
|---|---|---|
| **All-or-nothing** | Simple to reason about | Loses hundreds of valid events due to one bad row |
| **Best-effort** (silent drop) | Low friction | Opaque — silent data loss |
| **Partial-success envelope** ✅ | Accepts valid subset, reports every rejection | Client must interpret 207 |

### Current Design

Response envelope: `{accepted, duplicates, rejected}`.
- **200** — fully clean batch. **207** — some rejections. **413** — oversized.

### AI Recommendation

The AI drafted all-or-nothing: "validate with `IngestBatch.model_validate()`,
raise 422 on any error." Wrong semantics for a pipeline that can't stop and
fix one frame.

### Final Decision

Per-row validation + PK idempotency via `ON CONFLICT DO NOTHING`. Three
requirements in one contract: idempotent by `event_id`, partial success
on malformed events, structured error responses.

### Two-Phase Abandon Classification

1. **Pipeline phase**: emits `BILLING_QUEUE_LEAVE` (short visits) or
   `BILLING_QUEUE_ABANDON` (dwell ≥5s) as best-guess.
2. **API phase**: on POS ingest, reclassifies `LEAVE` → `ABANDON` where
   no POS transaction falls within 5 minutes.

---

## 4. Live Architecture — WebSockets & In-Memory Result Cache

### Options Evaluated

| Approach | Strengths | Weaknesses |
|---|---|---|
| **HTTP Polling** | Simple | Up to 5 DB queries per client per interval |
| **SSE** | Simpler than WebSocket, auto-reconnect | Server→client only; no native Python CLI support |
| **WebSockets + TTL Cache** ✅ | Single connection, one DB query set regardless of client count | Requires WebSocket support |

### AI Recommendation

The AI recommended **SSE**, arguing "dashboard only needs server→client flow."
Correct for web-only, but this system has two dashboard clients.

### Final Decision

WebSockets — the `websockets` Python library gives the terminal dashboard
native support with a single `websockets.connect()` call. The in-memory
result cache (3-second TTL) shields PostgreSQL under multiple concurrent
dashboard connections.

---

## 5. Physical Store Alignment — Blueprint Mapping

The zone config in `config/store_layout.json` is explicitly aligned to the
physical blueprint (`Brigade Road - Store layout.xlsx`):

| Camera | Logical ID | Physical Zone | Role |
|---|---|---|---|
| CAM 3 | `CAM_ENTRY_01` | Glass doorway entrance | Entry boundary crossing |
| CAM 1 | `CAM_FLOOR_SKIN` | Skincare section | Product floor zone |
| CAM 2 | `CAM_FLOOR_MAKEUP` | Makeup section | Product floor zone |
| CAM 5 | `CAM_CASH_COUNTER` | Cash Counter | Billing zone |
| CAM 4 | `CAM_STOCKROOM` | Stockroom | Staff-only |

Queue detection dynamically resolves the `type: "billing"` flag — no
hardcoded camera IDs in billing logic.

---

## 6. Employee Detection — HSV Match + Dwell-Pattern Heuristic

### AI Recommendation

CLIP zero-shot classification per bounding box. At 5 fps × 5 cameras,
this would dominate runtime.

### Final Decision

Two-signal approach in `pipeline/staff.py`:
1. **Primary** — HSV colour match against `staff_uniform_hsv` config.
2. **Secondary** — dwell-pattern: >2 zones in <30 seconds → flagged as staff.
3. **Forced** — `CAM_STOCKROOM` has `force_is_staff: true`.

CLIP available as optional fallback but not used in the default path.

---

## 7. Return-Visitor Detection — Bounded Sliding Buffer

### AI Recommendation

Per-frame cosine similarity against all history — O(all_history).

### Final Decision

`pipeline/reentry.py` maintains a `ReturnVisitorBuffer` — a deque with a
5-minute sliding window. On new ENTRY:
1. 3-bin HSV histogram computed from bounding box.
2. Cosine similarity checked against recent exits.
3. Similarity ≥ 0.90 → reclassified as `REENTRY`.

Buffer self-prunes on every lookup. Cost is O(live_candidates), not
O(all_history).

### Rationale

Matches the business intent: "don't double-count a shopper who steps out
briefly and returns." Exits older than 5 minutes are genuinely new visits.

---

## 8. Persistence — PostgreSQL with AsyncPG

### Options Evaluated

| Option | Strengths | Weaknesses |
|---|---|---|
| **Redis** | Fast key-value lookups | No relational queries |
| **Flat files (JSONL)** | Simple, no deps | No indexing, O(n) scans |
| **PostgreSQL 16** ✅ | Relational queries, PK idempotency, indexes | Requires running instance |

### Current Design

PostgreSQL 16 (via `asyncpg` + SQLAlchemy async) in production, SQLite
(`aiosqlite`) for tests. Schema defined once in `app/db.py`, works on
both dialects.

Key properties:
- **PK idempotency**: `ON CONFLICT DO NOTHING` for re-run safety.
- **Secondary indexes** for O(log n) analytics.
- **JSONL as durable backup**: every event written to `data/events.jsonl`
  AND posted to the API.
