# Master-Worker Platform

Infrastructure platform for task orchestration: routing, queue, persistence, timeout, UI, and authentication.

## Scope & Boundaries

**This platform does NOT handle:**
- DAG decomposition / task graph parsing
- Schema validation beyond envelope structure
- Acceptance judgment / quality gates
- Approval decisions (human-in-the-loop is a message type, not a decision engine)
- Business domain logic (quantitative, backtesting, stocks, etc.)

**This platform DOES handle:**
- Message routing (8 message types only)
- Task queue management
- Ledger persistence (append-only, no delete)
- Timeout & retry policies
- Web UI for monitoring
- Authentication & authorization

## Message Types (Exactly 8)

| Type | Direction | Purpose |
|------|-----------|---------|
| `task_dispatch` | Master → Worker | Assign new task |
| `task_heartbeat` | Worker → Master | Progress update |
| `task_return` | Worker → Master | Task completion |
| `task_reject` | Worker → Master | Refuse task |
| `task_abort` | Master → Worker | Cancel task |
| `human_confirm_req` | Master → Human | Request approval |
| `human_confirm_resp` | Human → Master | Approval response |
| `task_summary` | Master → All | Pipeline summary |

## Installation

```bash
# Use the pre-configured Python 3.11 environment
.venv311\Scripts\python.exe -m pip install -r requirements.txt
```

> **Note**: Dependencies are pre-installed in `.venv311`. Do not create new virtual environments.

## Initialize Database

```bash
# Create tables and indexes only (production mode)
.venv311\Scripts\python.exe scripts/init_db.py

# With demo data (for development only)
.venv311\Scripts\python.exe scripts/init_db.py --demo
```

## Run Server

```bash
.venv311\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Project Structure

```
master-worker-platform/
├── app/
│   ├── api/              # FastAPI routes (health only in Batch1)
│   │   ├── __init__.py
│   │   ├── deps.py       # DB session dependency
│   │   └── routes_health.py
│   ├── core/
│   │   ├── config.py     # Settings via pydantic-settings
│   │   └── logging.py    # setup_logging()
│   ├── models/           # SQLAlchemy ORM models (ledger tables)
│   │   └── __init__.py
│   ├── schemas/          # Pydantic models (envelope + payloads)
│   │   └── __init__.py
│   ├── services/         # Business services (archive, etc.)
│   │   └── __init__.py
│   └── main.py           # FastAPI app factory
├── scripts/
│   └── init_db.py        # DB initialization (with --demo flag)
├── data/                 # SQLite database (gitignored)
├── archive/              # JSONL message archives (gitignored)
├── .venv311/             # Pre-configured Python 3.11 env (gitignored)
├── requirements.txt
├── .env.example
└── AGENTS.md
```

## Ledger Tables (Append-Only)

- `pipeline_ledger` — one row per trace_id
- `task_ledger` — one row per task_id, FK to pipeline_ledger.trace_id (no CASCADE DELETE)
- `message_archive` — index row + JSONL file per trace_id

**No DELETE capability exists by design.** The platform never removes ledger entries.

## Configuration

Copy `.env.example` to `.env` and adjust:

```ini
DATABASE_URL=sqlite:///./data/mw_platform.db
ARCHIVE_ROOT=./archive
LOG_LEVEL=INFO
```

## Health Check

```bash
curl http://localhost:8000/api/health
# {"status": "ok"}
```

## Self-Tests

Run all four validation checks:

```bash
# 1. Import all core modules
.venv311\Scripts\python.exe -c "import app.models, app.schemas, app.services, app.core.logging"

# 2. Envelope validation rejects invalid msg_type (MUST fail)
.venv311\Scripts\python.exe -c "from app.schemas import Envelope; Envelope(trace_id='t', task_id='k', msg_type='bad_type', sender='master', timestamp='2026-01-01T00:00:00+08:00')"

# 3. Initialize database (tables only)
.venv311\Scripts\python.exe scripts/init_db.py

# 4. FastAPI routes registered
.venv311\Scripts\python.exe -c "from app.main import app; print([r.path for r in app.routes if hasattr(r, 'path')])"
```
