"""Job queue for the async, resumable document pipeline. Port of lib/jobs.ts.

Each stage is an idempotent row in `jobs`. drain_once() claims a small batch,
runs one stage per row, then enqueues the successor stage(s). A failed unit
retries with backoff on its own; it never re-runs the whole document.
"""

import asyncio
import time
from dataclasses import dataclass

from psycopg.errors import UniqueViolation

from .. import db
from .agents import (
    Doc,
    ExtractedRequirement,
    record_run,
    run_chunking_agent,
    run_extraction_agent,
    run_ingestion_agent,
    run_structuring_agent,
)
from .chunk import ProducedChunk
from .llm import MODEL_FAST
from .rag import BatchQuestion, generate_and_persist_answer, generate_batch_answers

# Questions per batched LLM call. 5 keeps the shared-source list relevant to
# every question in the group and the JSON output well under max_tokens.
_GENERATE_BATCH_SIZE = 5

# drain_once(): claim size per round, and the wall-clock budget for one call
# before returning (the caller — a cron tick or the post-upload kick — gets
# called again on the next tick / is left to the interval driver as the
# recovery net for anything still queued).
_DRAIN_CLAIM_BATCH = 8
_DRAIN_TIME_BUDGET_S = 4 * 60

# Retry backoff in ms, indexed by attempt count just made.
_BACKOFF_MS = [5_000, 30_000, 120_000]

_STAGE_RUNNING_STATUS = {
    "ingest": "extracting",
    "extract": "analyzing",
    "structure": "analyzing",
    "generate": "structured",
}
_STAGE_DEAD_STATUS = {
    "ingest": "failed",
    "extract": "extraction_failed",
    "structure": "failed",
    "generate": "generation_failed",
}
_STAGE_ORDER = ["ingest", "extract", "structure", "generate"]


@dataclass
class Job:
    id: str
    document_id: str
    org_id: str
    stage: str
    target_id: str | None
    status: str
    attempts: int
    max_attempts: int


# ---------- enqueue ----------


def enqueue_job(document_id: str, org_id: str, stage: str, target_id: str | None = None) -> None:
    """Insert a job, ignoring the unique-violation when a live row already exists."""
    try:
        with db.admin_tx() as cur:
            cur.execute(
                "INSERT INTO jobs (document_id, org_id, stage, target_id) VALUES (%s, %s, %s, %s)",
                (document_id, org_id, stage, target_id),
            )
    except UniqueViolation:
        pass


def enqueue_ingest(document_id: str, org_id: str) -> None:
    """Kick off a document by queuing its first stage."""
    enqueue_job(document_id, org_id, "ingest")


# ---------- claim / drain primitives ----------


def recover_stuck_jobs() -> None:
    with db.admin_tx() as cur:
        cur.execute("SELECT recover_stuck_jobs()")


def claim_jobs(limit: int) -> list[Job]:
    with db.admin_tx() as cur:
        cur.execute("SELECT * FROM claim_jobs(%s)", (limit,))
        rows = cur.fetchall()
    return [
        Job(
            id=str(r["id"]),
            document_id=str(r["document_id"]),
            org_id=str(r["org_id"]),
            stage=r["stage"],
            target_id=str(r["target_id"]) if r["target_id"] else None,
            status=r["status"],
            attempts=r["attempts"],
            max_attempts=r["max_attempts"],
        )
        for r in rows
    ]


def _backoff_seconds(attempts: int) -> float:
    idx = attempts - 1
    ms = _BACKOFF_MS[idx] if 0 <= idx < len(_BACKOFF_MS) else _BACKOFF_MS[-1]
    return ms / 1000


def mark_done(job_id: str) -> None:
    with db.admin_tx() as cur:
        cur.execute("UPDATE jobs SET status = 'done', updated_at = now() WHERE id = %s", (job_id,))


def mark_failed(job: Job, message: str) -> None:
    """Failed unit: re-queue with backoff, or bury as 'dead' once attempts exhausted."""
    dead = job.attempts >= job.max_attempts
    with db.admin_tx() as cur:
        if dead:
            cur.execute(
                "UPDATE jobs SET status = 'dead', error = %s, run_after = now(), lease_until = NULL, "
                "updated_at = now() WHERE id = %s",
                (message[:1000], job.id),
            )
        else:
            cur.execute(
                "UPDATE jobs SET status = 'pending', error = %s, "
                "run_after = now() + make_interval(secs => %s), lease_until = NULL, updated_at = now() "
                "WHERE id = %s",
                (message[:1000], _backoff_seconds(job.attempts), job.id),
            )


# ---------- stage dispatch ----------


def _load_doc(document_id: str) -> Doc:
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT id, deal_id, filename, file_path, mime_type, extracted_text FROM documents WHERE id = %s",
            (document_id,),
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError("Document not found")
    return Doc(
        id=str(row["id"]),
        deal_id=str(row["deal_id"]),
        filename=row["filename"],
        file_path=row["file_path"],
        mime_type=row["mime_type"],
        extracted_text=row["extracted_text"],
    )


async def run_job(job: Job) -> None:
    """Run one job's stage. Raises on failure (drain decides retry vs dead)."""
    doc = _load_doc(job.document_id)

    if job.stage == "ingest":
        parsed = await run_ingestion_agent(doc)
        await run_chunking_agent(doc, parsed)
        return
    if job.stage == "extract":
        chunks = _read_chunks(job.document_id)
        await run_extraction_agent(doc, chunks)
        return
    if job.stage == "structure":
        reqs = _read_requirements(job.document_id)
        await run_structuring_agent(doc, reqs)
        return
    if job.stage == "generate":
        # New shape: one doc-level job (target_id null) answers every question
        # in grouped batch calls. Legacy per-question rows (target_id set) may
        # still exist from before the cutover — run those through the single
        # path.
        if job.target_id:
            await _run_generate(job)
        else:
            await _run_generate_batched(job)
        return
    raise ValueError(f"unknown job stage: {job.stage}")


def enqueue_successors(job: Job) -> None:
    """Enqueue the next stage(s) after a job completes.

    Idempotent — duplicate inserts are swallowed by the unique-live index.
    """
    if job.stage == "ingest":
        enqueue_job(job.document_id, job.org_id, "extract")
    elif job.stage == "extract":
        enqueue_job(job.document_id, job.org_id, "structure")
    elif job.stage == "structure":
        # ONE doc-level generate job answers all questions in grouped batch
        # calls — replaces the old per-question fan-out that cost one LLM
        # call per question.
        enqueue_job(job.document_id, job.org_id, "generate")
    # "generate" has no successor.


# ---------- derived document status ----------


def _derive_status_from_jobs(rows: list[dict]) -> str | None:
    """Pure: compute documents.processing_status from a document's job rows."""
    if not rows:
        return None

    dead = [r for r in rows if r["status"] == "dead"]
    active = [r for r in rows if r["status"] in ("pending", "claimed")]

    # A dead PRE-generate stage (ingest/extract/structure) blocks everything
    # downstream — a genuine hard fail. A dead *generate* job only kills one
    # question's answer; the document is still usable if other questions
    # succeeded. Never let one dead answer condemn the whole document.
    blocking_dead = next((r for r in dead if r["stage"] != "generate"), None)
    gen_jobs = [r for r in rows if r["stage"] == "generate"]
    gen_done = [r for r in gen_jobs if r["status"] == "done"]

    if blocking_dead:
        return _STAGE_DEAD_STATUS[blocking_dead["stage"]]
    if active:
        stage = next((s for s in _STAGE_ORDER if any(r["stage"] == s for r in active)), "generate")
        return _STAGE_RUNNING_STATUS[stage]
    if gen_jobs and not gen_done:
        # Every answer failed — nothing usable produced.
        return _STAGE_DEAD_STATUS["generate"]
    # No active work, no blocking failure, at least one answer produced.
    # Completed — possibly partial (some generate jobs may be dead).
    return "completed"


def derive_doc_status(document_id: str) -> None:
    with db.admin_tx() as cur:
        cur.execute("SELECT stage, status FROM jobs WHERE document_id = %s", (document_id,))
        rows = cur.fetchall()
    status = _derive_status_from_jobs(rows)
    if status is None:
        return
    with db.admin_tx() as cur:
        cur.execute(
            "UPDATE documents SET processing_status = %s, updated_at = now() WHERE id = %s",
            (status, document_id),
        )


# ---------- helpers ----------


def _read_chunks(document_id: str) -> list[ProducedChunk]:
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT raw_text, cleaned_text, section_path, page_start, page_end, sparse_terms "
            "FROM document_chunks WHERE document_id = %s ORDER BY chunk_index ASC",
            (document_id,),
        )
        rows = cur.fetchall()
    return [
        ProducedChunk(
            text=r["cleaned_text"] or r["raw_text"] or "",
            text_for_embedding=r["cleaned_text"] or r["raw_text"] or "",
            section_path=r["section_path"] or "",
            page_start=r["page_start"] or 0,
            page_end=r["page_end"] or r["page_start"] or 0,
            sparse_terms=r["sparse_terms"] or [],
        )
        for r in rows
    ]


def _read_requirements(document_id: str) -> list[ExtractedRequirement]:
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT requirement_id, description, section, source_page, classification, topic "
            "FROM extracted_requirements WHERE document_id = %s",
            (document_id,),
        )
        rows = cur.fetchall()
    return [
        ExtractedRequirement(
            requirement_id=str(r["requirement_id"] or ""),
            section=r["section"],
            text=r["description"] or "",
            classification=r["classification"] or "must",
            topic=r["topic"] or "technical",
            source_page=r["source_page"],
        )
        for r in rows
    ]


async def _run_generate(job: Job) -> None:
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT q.id, q.question_text, o.name AS org_name "
            "FROM questions q "
            "JOIN documents d ON d.id = q.document_id "
            "JOIN deals dl ON dl.id = d.deal_id "
            "JOIN organizations o ON o.id = dl.org_id "
            "WHERE q.id = %s",
            (job.target_id,),
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError("Question not found")
    await generate_and_persist_answer(
        question_id=str(row["id"]),
        question_text=row["question_text"],
        org_id=job.org_id,
        org_name=row["org_name"] or "Workspace",
        tone="technical",
    )


async def _heartbeat_loop(job_id: str) -> None:
    """Extend the job's lease while a long batched-generate run is in flight,
    so recover_stuck_jobs doesn't reclaim it mid-work."""
    try:
        while True:
            await asyncio.sleep(60)
            with db.admin_tx() as cur:
                cur.execute(
                    "UPDATE jobs SET lease_until = now() + interval '5 minutes' WHERE id = %s",
                    (job_id,),
                )
    except asyncio.CancelledError:
        pass


async def _run_generate_batched(job: Job) -> None:
    """Doc-level generate: answer every unanswered question in grouped batch
    calls. Idempotent — questions that already have a response are skipped, so
    a retry after a mid-run failure only redoes the unanswered remainder.
    """
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT q.id, q.question_text, q.category, o.name AS org_name "
            "FROM questions q "
            "JOIN documents d ON d.id = q.document_id "
            "JOIN deals dl ON dl.id = d.deal_id "
            "JOIN organizations o ON o.id = dl.org_id "
            "WHERE q.document_id = %s",
            (job.document_id,),
        )
        questions = cur.fetchall()
    if not questions:
        return

    org_name = questions[0]["org_name"] or "Workspace"

    with db.admin_tx() as cur:
        cur.execute(
            "SELECT question_id FROM responses WHERE question_id = ANY(%s)",
            ([q["id"] for q in questions],),
        )
        answered = {str(r["question_id"]) for r in cur.fetchall()}

    pending = [q for q in questions if str(q["id"]) not in answered]
    if not pending:
        return

    # Group by category so each batch shares a topical source pool, then
    # split into fixed-size sub-batches.
    by_category: dict[str, list] = {}
    for q in pending:
        by_category.setdefault(q["category"] or "general", []).append(q)

    sub_batches: list[list[BatchQuestion]] = []
    for group in by_category.values():
        for i in range(0, len(group), _GENERATE_BATCH_SIZE):
            batch = group[i : i + _GENERATE_BATCH_SIZE]
            sub_batches.append(
                [BatchQuestion(question_id=str(q["id"]), question_text=q["question_text"]) for q in batch]
            )

    heartbeat = asyncio.create_task(_heartbeat_loop(job.id))
    started_at = time.time()
    total_in = 0
    total_out = 0

    async def run_batches(batches: list[list[BatchQuestion]]) -> list[list[BatchQuestion]]:
        nonlocal total_in, total_out
        results = await asyncio.gather(
            *(
                generate_batch_answers(org_id=job.org_id, org_name=org_name, tone="technical", questions=b)
                for b in batches
            ),
            return_exceptions=True,
        )
        still_failed: list[list[BatchQuestion]] = []
        for b, r in zip(batches, results):
            if isinstance(r, BaseException):
                still_failed.append(b)
            else:
                total_in += r["input_tokens"]
                total_out += r["output_tokens"]
        return still_failed

    try:
        # Fire all sub-batches; the per-model rate gate in llm.py paces
        # request starts. Any sub-batch that raises (e.g. an exhausted
        # rate-limit retry) is retried once inline — no re-answering of
        # questions that already succeeded, since generate_batch_answers
        # skips persisted responses.
        failed_batches = await run_batches(sub_batches)
        if failed_batches:
            failed_batches = await run_batches(failed_batches)

        # Resilience: a document must never end in a hard failure just
        # because a few sub-batches couldn't complete. Count how many
        # questions actually got a response. If ANY did, complete the stage —
        # the unanswered remainder is left as regenerable "todo" rather than
        # condemning the whole document. Only fail the stage (which lets the
        # job retry, then surface an error) when ZERO answers were produced.
        with db.admin_tx() as cur:
            cur.execute(
                "SELECT question_id FROM responses WHERE question_id = ANY(%s)",
                ([q["id"] for q in pending],),
            )
            answered_count = len(cur.fetchall())

        if answered_count == 0:
            raise RuntimeError(
                f"generation produced no answers: {len(failed_batches)}/{len(sub_batches)} sub-batches failed"
            )

        record_run(
            document_id=job.document_id,
            agent_type="generate",
            status="completed",
            input_tokens=total_in,
            output_tokens=total_out,
            result={
                "questions": len(pending),
                "answered": answered_count,
                "unanswered": len(pending) - answered_count,
                "sub_batches": len(sub_batches),
                "failed_sub_batches": len(failed_batches),
                "model": MODEL_FAST,
            },
            started_at=started_at,
        )
    except Exception as e:  # noqa: BLE001
        record_run(
            document_id=job.document_id,
            agent_type="generate",
            status="failed",
            input_tokens=total_in,
            output_tokens=total_out,
            error_message=str(e),
            result={"questions": len(pending), "sub_batches": len(sub_batches), "model": MODEL_FAST},
            started_at=started_at,
        )
        raise
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


# ---------- drain ----------


async def _run_one(job: Job) -> dict:
    try:
        await run_job(job)
        # Enqueue successors BEFORE marking done. If this crashes mid-fan-out
        # the stage stays claimed, gets recovered, and re-runs — re-enqueue is
        # idempotent (unique-live index). Marking done first would leave a
        # permanent gap: a "done" stage with missing successors nothing ever
        # revisits.
        enqueue_successors(job)
        mark_done(job.id)
        return {"id": job.id, "stage": job.stage, "ok": True}
    except Exception as e:  # noqa: BLE001
        mark_failed(job, str(e))
        return {"id": job.id, "stage": job.stage, "ok": False, "error": str(e)}


async def drain_once() -> dict:
    """Worker entrypoint. Recovers stuck claims, then loops: claim a small
    batch, run it concurrently, enqueue successors, repeat — until the queue
    is empty or the time budget is spent. Called both by the jobs/drain
    router (on an interval, via a scheduler) and as an in-process background
    kick right after a document is queued, so processing starts immediately
    instead of waiting for the next tick.
    """
    recover_stuck_jobs()

    started_at = time.monotonic()
    all_results: list[dict] = []
    total_claimed = 0

    while True:
        claimed = claim_jobs(_DRAIN_CLAIM_BATCH)
        if not claimed:
            break
        total_claimed += len(claimed)

        # Run the claimed jobs concurrently — each is an independent unit of
        # work (different document or question), so there's no ordering
        # dependency within a batch.
        batch_results = await asyncio.gather(*(_run_one(j) for j in claimed))
        all_results.extend(batch_results)

        # Status updates inside the loop so the UI tracks progress live.
        touched_docs = {j.document_id for j in claimed}
        for document_id in touched_docs:
            derive_doc_status(document_id)

        if time.monotonic() - started_at > _DRAIN_TIME_BUDGET_S:
            break

    return {"claimed": total_claimed, "results": all_results}
