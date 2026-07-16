# Klovered — Python pipeline backend (FastAPI)

Python/FastAPI backend for the Klovered Free tool's document / RAG / export
pipeline. Reads and writes the **same Supabase project** as the Next.js app
(`klovered-free`); the frontend stays Next.js and proxies `/api/pipeline/*` here.

Two Supabase access paths (isolation-safe):

- **User path** — PostgREST called with the guest's forwarded JWT + the anon
  key, so Postgres RLS enforces tenant isolation.
- **Service path** — service-role key, RLS bypassed, for trusted worker/storage
  code only.

## Endpoints (current)

| Method | Path | Notes |
|--------|------|-------|
| GET  | `/health` | liveness |
| GET  | `/api/pipeline/whoami` | verifies guest JWT, resolves org via RLS |
| POST | `/api/pipeline/parse` | stateless parse + timing (speed probe) |
| POST | `/api/pipeline/documents/upload` | port of the TS upload route |

## Local dev

```bash
python -m venv .venv
. .venv/Scripts/activate         # macOS/Linux: . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env.local       # or point at klovered-free/.env.local values
pytest -v
uvicorn app.main:app --reload --port 8000
```

Every response carries an `X-Process-Time-Ms` header for latency measurement.

## Migration status

Port of the TS `lib/*` pipeline is staged (strangler): **parse + upload** landed;
chunk / embeddings / extract / agents / rag / retrieval / jobs / docx-export are
next. See `docs/` in the Propello repo for the full design + phase plan.
