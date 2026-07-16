# Klovered — Python pipeline backend (FastAPI)

Self-hosted FastAPI backend for the Klovered Free tool. **No Supabase, no
Vercel** — runs on a DigitalOcean Droplet against DO Managed PostgreSQL, with
Mistral as the only AI vendor (generation + embeddings + OCR).

## Isolation model

Two Postgres roles, mirroring the trust split:

- **Request path** — the api connects as `app_user` (NOBYPASSRLS) and runs
  `SET LOCAL app.user_id = <verified uuid>` per transaction. Postgres RLS scopes
  every row to the caller's org; a missing check can't leak another tenant.
- **Admin/worker path** — connects as the DB superuser (owns the tables, so RLS
  doesn't apply) for guest provisioning and background work.

Guest auth is self-issued HS256 (`app/auth.py`) — no external auth service.

## Endpoints

| Method | Path | Notes |
|--------|------|-------|
| GET  | `/health` | liveness |
| POST | `/api/auth/guest` | mint token + provision org/deal (replaces Supabase anon + /api/session) |
| GET  | `/api/pipeline/whoami` | verify token, resolve org via RLS |
| POST | `/api/pipeline/parse` | stateless parse + timing (speed probe) |
| POST | `/api/pipeline/documents/upload` | one-RFP-per-session cap, local-disk storage |

Every response carries `X-Process-Time-Ms`.

## Local dev (bundled Postgres via Docker)

```bash
docker compose up -d              # api + pgvector Postgres (schema auto-applied)
curl -X POST localhost:8000/api/auth/guest
```

Run the test suite against the bundled DB:

```bash
export DATABASE_URL=postgresql://app_user:app_pw@localhost:5432/klovered
export ADMIN_DATABASE_URL=postgresql://klovered:klovered_pw@localhost:5432/klovered
pip install -e ".[dev]" && pytest -v
```

Without a database, `pytest` runs only the unit tests (parse + auth); integration
tests skip automatically.

## Production (Droplet + DO Managed Postgres)

The Droplet sits in the DO VPC and reaches the **private** DB endpoint. See
`docker-compose.prod.yml` header for the one-time setup; deploys are automated by
`.github/workflows/deploy.yml` (SSH → pull → build → apply schema → up).

## CI/CD

- **CI** (`ci.yml`) — ruff + full pytest against an ephemeral pgvector service on
  every push/PR. The integration + cross-tenant isolation tests run here.
- **Deploy** (`deploy.yml`) — after CI passes on `main`, SSH to the Droplet and
  redeploy. Secrets: `DROPLET_HOST`, `DROPLET_USER`, `DROPLET_SSH_KEY`.

## Migration status

Supabase-free foundation complete: self-issued auth, psycopg data layer with
GUC-based RLS, local-disk storage, parse + upload. Next: port the RAG pipeline
(chunk / embed / extract / rag / retrieval / jobs / docx-export) onto psycopg.
