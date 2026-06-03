# 🔮 Vortex · Retail Analytics Engine

**End-to-end CCTV → Insights pipeline for the Purplle Engineering Hiring
Challenge.** Ingests 1080p retail footage via YOLOv8-m + ByteTrack,
produces structured behavioural events, and serves live analytics through a
containerised FastAPI backend.

> **AI-Assisted.** This codebase was designed and built with LLM collaboration.
> All AI-influenced decisions are documented in
> [`docs/CHOICES.md`](docs/CHOICES.md) — what the AI suggested, what was
> overridden, and why.

---

## Capabilities

| Feature | Detail |
|---|---|
| **Person detection** | YOLOv8-m at `imgsz=320`, `conf=0.20`, 5 fps subsampling — ~19k frames in ~46 min on CPU |
| **Tracking** | ByteTrack via `supervision` — persistent visitor IDs per camera |
| **Event types** | `ENTRY`, `EXIT`, `REENTRY`, `ZONE_ENTER`, `ZONE_EXIT`, `ZONE_DWELL`, `BILLING_QUEUE_JOIN`, `BILLING_QUEUE_LEAVE`, `BILLING_QUEUE_ABANDON`, `POS_TRANSACTION` |
| **Employee detection** | HSV uniform match + dwell-pattern heuristic (>2 zones in <30 s) |
| **Return-visitor detection** | 5-minute HSV histogram buffer with cosine similarity ≥ 0.90 |
| **Overlap suppression** | 3-second window per `(visitor_id, zone_id)` pair across cameras |
| **API** | FastAPI + PostgreSQL (asyncpg) — idempotent ingest, partial-success 207, structured error envelopes |
| **Live streaming** | WebSocket (`/ws/stores/{id}`) with 3-second TTL cache — powers web and terminal dashboards |
| **POS correlation** | Two-phase abandon classification — pipeline emits best-guess, API reclassifies after POS ingest |
| **Alert scanning** | Queue spike, conversion drop, dead zone, stale camera |
| **Test suite** | **41/41 passing** — pure-Python pipeline unit tests + in-process HTTP tests via SQLite |

---

## Getting Started

### Prerequisites

- Docker & Docker Compose (API + PostgreSQL)
- Python 3.11+ with venv (CV pipeline)

### Step 1 — Launch the Backend

```bash
docker-compose down -v        # clean slate
docker-compose up --build     # starts FastAPI + PostgreSQL
```

API will be live at **http://localhost:8000**. Keep this terminal open.

### Step 2 — Install Pipeline Dependencies

In a **second** terminal:

```powershell
python -m venv .venv
.\.venv\Scripts\activate            # Windows
# source .venv/bin/activate         # Linux / macOS
pip install -r requirements-pipeline.txt
```

### Step 3 — Open Dashboards

- **Web Dashboard:** http://localhost:8000/
- **Terminal Dashboard (optional):**

  **Windows:**
  ```powershell
  python dashboard\terminal_dashboard.py
  ```
  **macOS / Linux:**
  ```bash
  python dashboard/terminal_dashboard.py
  ```

Both dashboards will show zeroes until the pipeline starts feeding events.

### Step 4 (Optional) — Preview YOLO Detections

Visually verify that YOLOv8 is detecting people before running the full pipeline.
Opens a 3×2 grid window showing all 5 cameras with bounding boxes:

```bash
python pipeline/preview.py
```

Press `q` to quit. Uses YOLOv8-n (nano) for speed.

### Step 5 — Run the CV Pipeline

**Windows (PowerShell):**
```powershell
.\pipeline\run_windows.ps1
```

**macOS / Linux (Bash):**
```bash
bash pipeline/run_linux.sh
```

Processes all 5 cameras sequentially, then ingests real POS transaction data.
Watch the dashboards update in real time.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Service health — DB status, per-store staleness |
| `POST` | `/events/ingest` | Batch event ingest (idempotent, partial-success) |
| `POST` | `/pos/ingest` | POS transaction ingest (triggers abandon reclassification) |
| `GET` | `/stores/{id}/metrics` | Store KPIs — footfall, conversion, queue depth, staff count |
| `GET` | `/stores/{id}/funnel` | Conversion pipeline — Entry → ZoneVisit → BillingQueue → Purchase |
| `GET` | `/stores/{id}/heatmap` | Zone visit intensity with normalised [0,100] scores |
| `GET` | `/stores/{id}/anomalies` | Active alerts with severity and suggested actions |
| `GET` | `/stores/{id}/events` | Paginated raw event list (`?limit=100&offset=0`) |
| `WS`  | `/ws/stores/{id}` | Real-time WebSocket stream — metrics, funnel, heatmap, anomalies, health every 2 s |

---

## Testing

```bash
pytest tests/ -v
```

All **41 tests** pass, covering:

- API endpoints (ingest, metrics, funnel, heatmap, anomalies, events, health)
- Idempotency and partial-success semantics
- POS-correlated abandon reclassification
- Pipeline geometry (point-in-polygon, boundary crossing, region state machine)
- Return-visitor buffer eviction and cosine similarity
- Overlap suppression
- Event dispatcher JSONL buffering and HTTP batching
- UUID uniqueness and Pydantic schema compliance

---

## Project Structure

```
├── app/                  # FastAPI application
│   ├── main.py           # Routes, WebSocket, result caching
│   ├── models.py         # Pydantic v2 BehaviourEvent + SaleRecord schemas
│   ├── db.py             # SQLAlchemy async schema + engine
│   ├── ingestion.py      # Batch ingest with partial-success
│   ├── metrics.py        # Outlet snapshot computation
│   ├── funnel.py         # Session-based conversion pipeline
│   ├── heatmap.py        # Zone visit intensity
│   ├── anomalies.py      # Alert scanning rules
│   ├── health.py         # Health probe + stale feed detection
│   ├── errors.py         # Fault handlers
│   ├── logging_mw.py     # Request audit middleware
│   └── config.py         # Environment-based settings
├── pipeline/             # CV pipeline (runs locally, not in Docker)
│   ├── detect.py         # YOLOv8-m inference + zone + emit loop
│   ├── tracker.py        # ByteTrack wrapper with identity fallback
│   ├── zones.py          # Geometry: point-in-polygon, boundary crossing, dwell
│   ├── staff.py          # HSV + dwell-pattern employee detection
│   ├── reentry.py        # 5-minute appearance buffer for return visitors
│   ├── cross_camera.py   # Overlap suppression filter
│   ├── emit.py           # JSONL writer + buffered HTTP dispatch
│   ├── post_pos.py       # POS CSV → API ingest
│   ├── run_windows.ps1   # Windows pipeline runner
│   └── run_linux.sh      # Linux/macOS pipeline runner
├── dashboard/
│   ├── web/              # HTML/JS/CSS web dashboard
│   └── terminal_dashboard.py  # Rich-powered terminal dashboard
├── config/
│   ├── store_layout.json # Camera → zone mapping
│   └── alembic.ini       # Alembic migration config
├── tests/                # 41 test cases
├── docs/
│   ├── DESIGN.md         # Architecture and data flow
│   └── CHOICES.md        # Engineering decisions with AI rationale
├── Dockerfile            # Slim API image (~200 MB, no torch)
├── docker-compose.yml    # API + PostgreSQL 16
├── requirements.txt      # API-only deps (Docker)
└── requirements-pipeline.txt  # Full deps (local: API + torch + ultralytics)
```

---

## Documentation

- **[DESIGN.md](docs/DESIGN.md)** — Architecture, data flow, schema, storage, streaming, observability, error handling, camera alignment, testing strategy, scalability
- **[CHOICES.md](docs/CHOICES.md)** — 8 key engineering decisions with alternatives evaluated, AI suggestions documented, trade-offs accepted, measured results
