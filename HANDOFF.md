# Klovered — session handoff

Paste this whole file into the first message of a new session if memory
didn't transfer. Repo: https://github.com/vibecoder223/Klovered-python (private).

## The decision
Migrating Klovered Free **off Supabase and off Vercel, entirely onto
DigitalOcean**: self-hosted Postgres (DO Managed PG, pgvector) + ONE Droplet
running Docker Compose (web+api+worker+db). **Not App Platform** — rejected
as too expensive for a pre-revenue tool (~10 users/day, ~4 uploads/day).
Mistral is the only AI vendor (LLM + embeddings + OCR) — no Jina, no
Anthropic. Reference/source-to-port-from repo: `klovered-free` (TS/Next.js,
sibling folder) — keep it, don't delete, it's what we're porting from.

## Done (commit `42ce3c8`) — verified: ruff clean, 7/7 unit tests pass
- `app/config.py`, `app/db.py` — psycopg pools: `user_tx(user_id)` (RLS via
  `SET LOCAL app.user_id`, app_user role, NOBYPASSRLS) vs `admin_tx()`
  (superuser, BYPASSRLS, for provisioning/workers)
- `db/schema.sql` — base tables + RLS via `current_user_org_ids()`
  (SECURITY DEFINER), `auth.uid()` replaced with `current_setting('app.user_id')`
- `app/auth.py` — self-issued HS256 guest JWTs (no Supabase Auth/JWKS)
- `app/storage.py` — local-disk files (20MB cap, path-traversal guard)
- `app/routers/auth.py` (`POST /api/auth/guest`), `app/routers/documents.py`
  (`whoami`, `parse`, `documents/upload` w/ one-RFP cap + concurrency guard)
- `app/pipeline/parse.py` — PyMuPDF/mammoth parser
- `tests/test_unit.py` (7, always run), `tests/test_integration.py` (5,
  DB-gated, includes the **cross-tenant isolation** test — most important one)
- `docker-compose.yml` (local, bundled pgvector), `docker-compose.prod.yml`
  (Droplet, connects to DO Managed PG), `Dockerfile`, `Caddyfile`
- `.github/workflows/ci.yml` (own ephemeral Postgres, runs full suite),
  `deploy.yml` (SSH to Droplet, needs DROPLET_HOST/USER/SSH_KEY secrets)

**NOT CONFIRMED:** whether CI actually passed on `42ce3c8`/`dd943b4` — check
https://github.com/vibecoder223/Klovered-python/actions first thing.
Local Docker Desktop was unresponsive at session end (repeated `docker ps`
timeouts) so integration tests were never run locally either — do that once
Docker's cooperating: `docker compose up -d db && pytest -v`.

## In progress (commit `dd943b4`) — written, NOT tested, NOT wired up
Porting the RAG pipeline from `klovered-free/lib/*.ts` (~4500 lines TS).
Schema extended with `document_chunks` (vector(1024)+HNSW), `extracted_requirements`,
`compliance_matrix`, `questions`, `responses`, `citations`, `agent_runs`, `jobs`
+ functions `claim_jobs`, `recover_stuck_jobs`, `match_chunks` (all RLS/org-scoped).

Written: `app/pipeline/llm.py` (Mistral client + per-model rate gate, port of
`lib/mistral.ts`), `app/pipeline/chunk.py` (port of `lib/chunk.ts`),
`app/pipeline/embeddings.py` (port of `lib/embeddings.ts`).

## Not started — in this order
1. `app/pipeline/retrieval.py` — port of `lib/retrieval.ts` (hybrid dense+BM25) — **write this next**
2. `app/pipeline/rag.py` — port of `lib/rag.ts` (grounded generation, biggest file, 856 lines)
3. `app/pipeline/agents.py` — port of `lib/agents.ts` (orchestration, rewrite Supabase calls -> psycopg via `db.admin_tx()`)
4. `app/pipeline/jobs.py` — port of `lib/jobs.ts` (queue, uses claim_jobs/recover_stuck_jobs already in schema)
5. Wire routers: `documents/process`, `jobs/drain`, `cron/cleanup`
6. Add tests for each as it's built — nothing past Phase 1 has coverage yet
7. Deferred (port last): docx-export + docx-template-fill (needs `docxtpl`,
   template re-authoring), answer-library (reuse-suggestion), rate-limit,
   safe-fetch (SSRF guard, not urgent — nothing fetches user URLs yet)
8. Only after pipeline runs end-to-end locally: actually provision the DO
   Droplet (none exists yet), point deploy.yml secrets at it, real deploy

## Security note
User pasted a live DO Postgres admin connection string (password included) in
plaintext chat mid-session. **Confirm it's been rotated** in the DO console
before reusing it, if not already done.

## Local-only files that do NOT transfer with git
- `.env.local` in Klovered-python (AUTH_JWT_SECRET, APP_USER_PASSWORD, DO DSN)
  — gitignored on purpose; copy manually if wanted
- SSH key (`~/.ssh/id_klovered` or similar, passphrase-protected)
