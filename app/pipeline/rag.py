"""Grounded generation with citations + confidence scoring. Port of lib/rag.ts.

Persists to `responses` + `citations` via the admin (BYPASSRLS) connection,
like the rest of the worker pipeline.

Library-first reuse (lib/answer-library.ts: skip generation for a near-
duplicate approved question) is NOT ported here — that file is explicitly
deferred in the migration plan, so every question currently goes through full
retrieval + generation. Wire it in once app/pipeline/answer_library.py exists.
"""

import asyncio
import math
import os
import re
from dataclasses import dataclass

from .. import db
from .embeddings import embed_texts, has_embeddings
from .llm import MODEL, MODEL_FAST, call_mistral_json, call_mistral_text, fast_lane_saturated, has_llm_key
from .retrieval import Candidate, RetrievalResult, is_no_source, retrieve_for_queries, retrieve_for_query

PROMPTS = {
    "generator_system_v1": """You are a proposal writer at the customer's company. You write answers to RFP requirements in the customer's own voice, drawing exclusively from the source chunks provided. You never invent facts. You never speculate. You never use external knowledge.

Rules:
1. Every SENTENCE must be individually traceable to a specific chunk in <sources>. If you cannot point to the exact chunk that supports a sentence, delete that sentence — do not write it and cite a nearby chunk hoping it's close enough.
2. Never combine a fact from one chunk with an unrelated claim from another chunk unless both facts are actually about the same subject (e.g. do not take a remediation timeline from a penetration-testing chunk and apply it to a support-SLA answer).
3. Never generalize a specific number, policy, or capability beyond what the chunk states. If a chunk describes one thing (e.g. audit logging) do not extend it into a different capability (e.g. full version control) that the chunk does not mention.
4. Cite every supported claim inline using [c:N], where N is the chunk's number from <sources> (e.g. [c:1], [c:3]). No quotes, no extra brackets, no UUIDs.
5. Write in business prose: confident, specific, concise. If voice examples are provided, match their tone.
6. If sources contradict each other, prefer the more recent document and note the discrepancy in a closing sentence.
7. If the sources do not cover the requirement, output exactly:
   "NO_SOURCE: The knowledge base does not contain content sufficient to answer this requirement."
   Do not draft a partial or hedged answer. A single loosely-related chunk is NOT coverage — if the chunk doesn't state the specific fact asked for, this is NO_SOURCE, not an inference.
8. Length: match the requirement. "Describe" gets 100-200 words. "Confirm" gets one sentence. Do not pad.""",
    "generator_batch_system_v1": """You are a proposal writer at the customer's company, answering RFP requirements in the customer's voice using ONLY the source chunks provided.

Rules:
1. Every sentence must be individually traceable to a specific chunk — if you can't point to the exact chunk supporting a sentence, delete that sentence rather than cite a nearby chunk hoping it's close enough.
2. Never combine facts from unrelated chunks (e.g. don't take a remediation timeline from a security chunk and apply it to a support-SLA answer), and never generalize a chunk's specific claim into a broader capability it doesn't state.
3. Cite every supported claim inline as [c:N] using that chunk's number. Never invent facts or use outside knowledge.
4. Business prose: confident, specific, concise. Match the voice examples if provided.
5. If the sources do not cover a question — including when a chunk is only topically related but doesn't state the specific fact asked — that answer must be exactly "NO_SOURCE".
6. Length follows the question: "describe/explain" 100-200 words; "confirm/yes-no" 1-2 sentences. No padding.

Return ONLY a JSON array, one item per question, no fences:
[{"q": <question number>, "answer": "<answer text with [c:N] citations>"}]""",
    "confidence_system_v1": """Score this answer's grounding 0.0-1.0.

- 1.0: every claim is directly supported by a cited chunk.
- 0.7: mostly supported; minor unsupported phrasing.
- 0.4: partially supported; weak source coverage on some claims.
- 0.0: not grounded.

Output a single decimal number, nothing else.""",
    "confidence_batch_system_v1": """You are scoring how well each answer is grounded in its OWN cited source chunks. Score each item independently — never let one item's sources influence another's score.

Per item, score 0.0-1.0:
- 1.0: every claim is directly supported by that item's cited chunks.
- 0.7: mostly supported; minor unsupported phrasing.
- 0.4: partially supported; weak source coverage on some claims.
- 0.0: not grounded.

Return ONLY a JSON array, one entry per item, no fences:
[{"q": <item number>, "score": <decimal>}]""",
}

# Max unique chunks sent in one batched call (~400 tokens each).
BATCH_SOURCE_CAP = 14
# Each question's top-K chunks are guaranteed a slot before global fill.
PER_QUESTION_GUARANTEE = 3

# Matches [c:N] and the full-width-bracket variant some models emit.
CITE_RE = re.compile(r"[\[【]\s*c:\s*(\d{1,3})\s*[\]】]", re.IGNORECASE)


@dataclass
class BatchQuestion:
    question_id: str
    question_text: str


@dataclass
class ParsedCitation:
    chunk_id: str
    document_filename: str
    section_path: str | None
    page: int | None
    quote: str


@dataclass
class _PreparedAnswer:
    question: BatchQuestion
    raw_answer: str
    clean: str
    valid_cited: list[ParsedCitation]
    grounded: bool
    confidence: float


async def generate_and_persist_answer(
    *, question_id: str, question_text: str, org_id: str, org_name: str, tone: str = "technical"
) -> dict:
    total_in = 0
    total_out = 0

    # 1. Retrieve
    retrieval = await retrieve_for_query(org_id, question_text, top_k=6)
    total_in += retrieval.usage["input_tokens"]
    total_out += retrieval.usage["output_tokens"]

    # 2. Gap gate
    if is_no_source(retrieval.top_score, len(retrieval.candidates)):
        _upsert_response(
            question_id=question_id,
            answer_text_with_markers=(
                "NO_SOURCE: The knowledge base does not contain content sufficient to answer this requirement."
            ),
            answer_text_clean="",
            tone=tone,
            confidence=0,
            gap_flag="no_source",
            status="requires_review",
            generated_by="ai",
            citations=[],
        )
        return {"input_tokens": total_in, "output_tokens": total_out}

    # 3. Voice examples, org-scoped.
    voice_examples = _fetch_voice_examples(org_id)

    # 4. Generate
    if not has_llm_key():
        _upsert_response(
            question_id=question_id,
            answer_text_with_markers="AI_DISABLED: no LLM API key configured.",
            answer_text_clean="AI_DISABLED: no LLM API key configured.",
            tone=tone,
            confidence=0,
            gap_flag="no_source",
            status="requires_review",
            generated_by="ai",
            citations=[],
        )
        return {"input_tokens": total_in, "output_tokens": total_out}

    user = _build_generator_user(
        org_name=org_name,
        question_text=question_text,
        voice_examples=voice_examples,
        sources=retrieval.candidates,
    )

    raw_answer, gen_usage = await call_mistral_text(
        system=PROMPTS["generator_system_v1"], user=user, max_tokens=900, model=MODEL_FAST
    )
    total_in += gen_usage["input_tokens"]
    total_out += gen_usage["output_tokens"]

    # 5. Detect the model's NO_SOURCE sentinel
    if re.match(r"^\s*NO_SOURCE:", raw_answer, re.IGNORECASE):
        _upsert_response(
            question_id=question_id,
            answer_text_with_markers=raw_answer.strip(),
            answer_text_clean="",
            tone=tone,
            confidence=0,
            gap_flag="no_source",
            status="requires_review",
            generated_by="ai",
            citations=[],
        )
        return {"input_tokens": total_in, "output_tokens": total_out}

    # 6. Parse citation markers
    valid_ids = {c.chunk_id for c in retrieval.candidates}
    cited = extract_citations(raw_answer, retrieval.candidates)
    valid_cited = [c for c in cited if c.chunk_id in valid_ids]
    clean = strip_markers(raw_answer)

    # 7. Confidence — heuristic from citation count, not a second LLM call
    # by default. Set RAG_USE_CONFIDENCE_LLM=1 to opt into the LLM-scored path.
    confidence = 0.0 if not valid_cited else (0.7 if len(valid_cited) >= 2 else 0.5)

    if os.getenv("RAG_USE_CONFIDENCE_LLM") == "1":
        try:
            sources_block = "\n".join(
                f'<chunk id="{c.chunk_id}">{c.text}</chunk>' for c in retrieval.candidates
            )
            text, usage = await call_mistral_text(
                system=PROMPTS["confidence_system_v1"],
                user=f"<answer>\n{raw_answer}\n</answer>\n\n<sources>\n{sources_block}\n</sources>",
                max_tokens=16,
                model=MODEL_FAST,
            )
            total_in += usage["input_tokens"]
            total_out += usage["output_tokens"]
            m = re.search(r"[01](?:\.\d+)?", text)
            if m:
                confidence = max(0.0, min(1.0, float(m.group(0))))
        except Exception:  # noqa: BLE001 — leave heuristic confidence if scorer fails
            pass

    grounded = len(valid_cited) > 0
    gap_flag = "no_source" if not grounded else ("ok" if confidence >= 0.7 else "partial")
    status = "draft" if (confidence >= 0.7 and gap_flag == "ok") else "requires_review"

    _upsert_response(
        question_id=question_id,
        answer_text_with_markers=raw_answer.strip(),
        answer_text_clean=clean if grounded else "",
        tone=tone,
        confidence=confidence,
        gap_flag=gap_flag,
        status=status,
        generated_by="ai",
        citations=valid_cited,
    )
    return {"input_tokens": total_in, "output_tokens": total_out}


async def generate_batch_answers(
    *, org_id: str, org_name: str, questions: list[BatchQuestion], tone: str = "technical"
) -> dict:
    """Answer several questions in ONE LLM call against a shared, deduplicated
    source list. Questions that fail the batch call fall back to the proven
    single-question path so one malformed JSON response can't strand a group.
    """
    total_in = 0
    total_out = 0
    if not questions:
        return {"input_tokens": 0, "output_tokens": 0}

    query_embeddings: list[list[float]] = []
    if has_embeddings():
        try:
            query_embeddings = await embed_texts([q.question_text for q in questions], "query")
        except Exception:  # noqa: BLE001 — degrade to sparse-only on embed failure
            query_embeddings = []

    batch_retrievals = await retrieve_for_queries(
        org_id,
        [q.question_text for q in questions],
        top_k=6,
        embeddings=query_embeddings or None,
    )

    live: list[tuple[BatchQuestion, RetrievalResult]] = []
    for question, retrieval in zip(questions, batch_retrievals):
        if is_no_source(retrieval.top_score, len(retrieval.candidates)):
            _upsert_response(
                question_id=question.question_id,
                answer_text_with_markers=(
                    "NO_SOURCE: The knowledge base does not contain content sufficient to answer this requirement."
                ),
                answer_text_clean="",
                tone=tone,
                confidence=0,
                gap_flag="no_source",
                status="requires_review",
                generated_by="ai",
                citations=[],
            )
        else:
            live.append((question, retrieval))

    if not live:
        return {"input_tokens": total_in, "output_tokens": total_out}

    if not has_llm_key():
        for question, _ in live:
            _upsert_response(
                question_id=question.question_id,
                answer_text_with_markers="AI_DISABLED: no LLM API key configured.",
                answer_text_clean="AI_DISABLED: no LLM API key configured.",
                tone=tone,
                confidence=0,
                gap_flag="no_source",
                status="requires_review",
                generated_by="ai",
                citations=[],
            )
        return {"input_tokens": total_in, "output_tokens": total_out}

    union: dict[str, Candidate] = {}
    for _, retrieval in live:
        for c in retrieval.candidates[:PER_QUESTION_GUARANTEE]:
            union.setdefault(c.chunk_id, c)
    overflow = sorted(
        (
            c
            for _, retrieval in live
            for c in retrieval.candidates[PER_QUESTION_GUARANTEE:]
            if c.chunk_id not in union
        ),
        key=lambda c: c.score,
        reverse=True,
    )
    for c in overflow:
        if len(union) >= BATCH_SOURCE_CAP:
            break
        union[c.chunk_id] = c
    shared_sources = list(union.values())

    voice_examples = _fetch_voice_examples(org_id)

    user = _build_batch_generator_user(
        org_name=org_name,
        questions=[q.question_text for q, _ in live],
        voice_examples=voice_examples,
        sources=shared_sources,
    )

    max_tokens = min(4096, 400 + len(live) * 350)
    answers: dict[int, str] | None = None
    last_err = ""
    for attempt in range(2):
        if answers is not None:
            break
        try:
            est_tokens = math.ceil(len(user) / 4) + max_tokens
            model = MODEL if fast_lane_saturated(est_tokens) else MODEL_FAST
            data, usage = await call_mistral_json(
                system=PROMPTS["generator_batch_system_v1"],
                user=(
                    user
                    if attempt == 0
                    else f"{user}\n\n[Previous attempt failed: {last_err}. Return ONLY the JSON array described in the system prompt.]"
                ),
                max_tokens=max_tokens,
                mode="text",
                model=model,
            )
            total_in += usage["input_tokens"]
            total_out += usage["output_tokens"]
            validated = _validate_batch_answers(data)
            if validated is None:
                last_err = "response did not match the expected [{q, answer}] array shape"
                continue
            answers = validated
        except Exception as e:  # noqa: BLE001
            last_err = str(e)

    valid_ids = {c.chunk_id for c in shared_sources}

    fallback_items: list[BatchQuestion] = []
    no_source_items: list[BatchQuestion] = []
    prepared: list[_PreparedAnswer] = []

    for i, (question, _) in enumerate(live):
        raw_answer = (answers or {}).get(i + 1)
        raw_answer = raw_answer.strip() if raw_answer else None
        if not raw_answer:
            fallback_items.append(question)
            continue
        if re.match(r'^\s*"?NO_SOURCE', raw_answer, re.IGNORECASE):
            no_source_items.append(question)
            continue

        cited = extract_citations(raw_answer, shared_sources)
        valid_cited = [c for c in cited if c.chunk_id in valid_ids]
        grounded = len(valid_cited) > 0
        prepared.append(
            _PreparedAnswer(
                question=question,
                raw_answer=raw_answer,
                clean=strip_markers(raw_answer),
                valid_cited=valid_cited,
                grounded=grounded,
                confidence=0.0 if not grounded else (0.7 if len(valid_cited) >= 2 else 0.5),
            )
        )

    scorable = [p for p in prepared if p.grounded]
    if scorable and os.getenv("RAG_USE_CONFIDENCE_LLM") == "1":
        try:
            items_block = "\n\n".join(
                _batch_confidence_item(idx, p, shared_sources) for idx, p in enumerate(scorable, start=1)
            )
            conf_max_tokens = 16 * len(scorable) + 32
            model = (
                MODEL
                if fast_lane_saturated(math.ceil(len(items_block) / 4) + conf_max_tokens)
                else MODEL_FAST
            )
            data, usage = await call_mistral_json(
                system=PROMPTS["confidence_batch_system_v1"],
                user=items_block,
                max_tokens=conf_max_tokens,
                mode="text",
                model=model,
            )
            total_in += usage["input_tokens"]
            total_out += usage["output_tokens"]
            scores = _validate_batch_scores(data)
            if scores:
                for idx, p in enumerate(scorable, start=1):
                    if idx in scores:
                        p.confidence = max(0.0, min(1.0, scores[idx]))
        except Exception:  # noqa: BLE001 — leave heuristic confidence if scorer fails
            pass

    # Fallback answers each make a real network round-trip (retrieval + LLM),
    # so they're the one part of this function worth running concurrently.
    if fallback_items:
        fallback_usages = await asyncio.gather(
            *(
                generate_and_persist_answer(
                    question_id=q.question_id,
                    question_text=q.question_text,
                    org_id=org_id,
                    org_name=org_name,
                    tone=tone,
                )
                for q in fallback_items
            )
        )
        for u in fallback_usages:
            total_in += u["input_tokens"]
            total_out += u["output_tokens"]

    for question in no_source_items:
        _upsert_response(
            question_id=question.question_id,
            answer_text_with_markers=(
                "NO_SOURCE: The knowledge base does not contain content sufficient to answer this requirement."
            ),
            answer_text_clean="",
            tone=tone,
            confidence=0,
            gap_flag="no_source",
            status="requires_review",
            generated_by="ai",
            citations=[],
        )

    for p in prepared:
        gap_flag = "no_source" if not p.grounded else ("ok" if p.confidence >= 0.7 else "partial")
        status = "draft" if (p.confidence >= 0.7 and gap_flag == "ok") else "requires_review"
        _upsert_response(
            question_id=p.question.question_id,
            answer_text_with_markers=p.raw_answer,
            answer_text_clean=p.clean if p.grounded else "",
            tone=tone,
            confidence=p.confidence,
            gap_flag=gap_flag,
            status=status,
            generated_by="ai",
            citations=p.valid_cited,
        )

    return {"input_tokens": total_in, "output_tokens": total_out}


# ---------- prompt building ----------


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _build_generator_user(
    *, org_name: str, question_text: str, voice_examples: list[str], sources: list[Candidate]
) -> str:
    voice = (
        "<voice_examples>\n"
        + "\n".join(f"<example>{v}</example>" for v in voice_examples)
        + "\n</voice_examples>"
        if voice_examples
        else ""
    )
    sources_block = "\n".join(
        f'<chunk id="{i + 1}" doc="{_esc(c.document_filename)}" section="{_esc(c.section_path or "")}" '
        f'page="{c.page_start if c.page_start is not None else ""}">\n{c.text}\n</chunk>'
        for i, c in enumerate(sources)
    )
    return (
        f"Company: {org_name}\n\n"
        f"<requirement>\n{question_text}\n</requirement>\n\n"
        f"{voice}\n\n"
        f"<sources>\n{sources_block}\n</sources>\n\n"
        "Write the answer now. Cite every supported claim with the chunk's number in square brackets, "
        "e.g. [c:1] or [c:3]. Use only the numbers shown in <sources>."
    )


def _build_batch_generator_user(
    *, org_name: str, questions: list[str], voice_examples: list[str], sources: list[Candidate]
) -> str:
    voice = (
        "<voice_examples>\n"
        + "\n".join(f"<example>{v}</example>" for v in voice_examples)
        + "\n</voice_examples>\n\n"
        if voice_examples
        else ""
    )
    sources_block = "\n".join(
        f'<chunk id="{i + 1}" doc="{_esc(c.document_filename)}" page="{c.page_start if c.page_start is not None else ""}">\n'
        f"{c.text[:1600]}\n</chunk>"
        for i, c in enumerate(sources)
    )
    questions_block = "\n".join(f'<question n="{i + 1}">{q}</question>' for i, q in enumerate(questions))
    return (
        f"Company: {org_name}\n\n"
        f"{voice}<sources>\n{sources_block}\n</sources>\n\n"
        f"<questions>\n{questions_block}\n</questions>\n\n"
        f"Answer all {len(questions)} questions. Cite with chunk numbers from <sources>, e.g. [c:2]."
    )


def _batch_confidence_item(idx: int, p: _PreparedAnswer, sources: list[Candidate]) -> str:
    cited_chunks = [
        next((c for c in sources if c.chunk_id == vc.chunk_id), None) for vc in p.valid_cited
    ]
    chunks_block = "\n".join(
        f'<chunk id="{c.chunk_id}">{c.text}</chunk>' for c in cited_chunks if c is not None
    )
    return f'<item n="{idx}">\n<answer>\n{p.raw_answer}\n</answer>\n<sources>\n{chunks_block}\n</sources>\n</item>'


# ---------- citations ----------


def extract_citations(text_with_markers: str, sources: list[Candidate]) -> list[ParsedCitation]:
    out: list[ParsedCitation] = []
    seen: set[int] = set()
    for m in CITE_RE.finditer(text_with_markers):
        n = int(m.group(1))
        if n in seen:
            continue
        idx = n - 1
        if idx < 0 or idx >= len(sources):
            continue
        src = sources[idx]
        seen.add(n)
        # Quote: take the sentence preceding this marker as supporting context.
        before = re.sub(r"\s+", " ", text_with_markers[: m.start()]).strip()
        sentences = re.split(r"(?<=[.!?])\s+", before)
        quote = (sentences[-1] if sentences and sentences[-1] else src.text[:200])[:400]
        out.append(
            ParsedCitation(
                chunk_id=src.chunk_id,
                document_filename=src.document_filename,
                section_path=src.section_path,
                page=src.page_start,
                quote=quote,
            )
        )
    return out


def strip_markers(text: str) -> str:
    return re.sub(r"\s+", " ", CITE_RE.sub("", text)).strip()


# ---------- validation (no zod equivalent — manual shape checks) ----------


def _validate_batch_answers(data: object) -> dict[int, str] | None:
    if not isinstance(data, list):
        return None
    out: dict[int, str] = {}
    for item in data:
        if not isinstance(item, dict):
            return None
        answer = item.get("answer")
        if not isinstance(answer, str):
            return None
        try:
            q = int(item.get("q"))
        except (TypeError, ValueError):
            return None
        if q < 1:
            return None
        out[q] = answer
    return out


def _validate_batch_scores(data: object) -> dict[int, float] | None:
    if not isinstance(data, list):
        return None
    out: dict[int, float] = {}
    for item in data:
        if not isinstance(item, dict):
            return None
        try:
            q = int(item.get("q"))
            score = float(item.get("score"))
        except (TypeError, ValueError):
            return None
        if q < 1 or not (0 <= score <= 1):
            return None
        out[q] = score
    return out


# ---------- persistence ----------


def _fetch_voice_examples(org_id: str) -> list[str]:
    """Approved answers from THIS org only.

    responses carry no org column, so join question -> document -> deal and
    filter on deals.org_id. Without this, approved answers from every tenant
    leak into the prompt — one customer's prose drafted into another's
    proposal (this runs on the admin/BYPASSRLS connection, so this join IS
    the isolation boundary, not RLS).
    """
    with db.admin_tx() as cur:
        cur.execute(
            "SELECT r.final_text, r.draft_text FROM responses r "
            "JOIN questions q ON q.id = r.question_id "
            "JOIN documents d ON d.id = q.document_id "
            "JOIN deals dl ON dl.id = d.deal_id "
            "WHERE r.status = 'approved' AND dl.org_id = %s AND r.final_text IS NOT NULL "
            "LIMIT 3",
            (org_id,),
        )
        rows = cur.fetchall()
    out: list[str] = []
    for r in rows:
        text = (r.get("final_text") or r.get("draft_text") or "")[:600]
        if text:
            out.append(text)
    return out[:3]


def _upsert_response(
    *,
    question_id: str,
    answer_text_with_markers: str,
    answer_text_clean: str,
    tone: str,
    confidence: float,
    gap_flag: str,
    status: str,
    generated_by: str,
    citations: list[ParsedCitation],
) -> None:
    with db.admin_tx() as cur:
        cur.execute("SELECT id FROM responses WHERE question_id = %s LIMIT 1", (question_id,))
        existing = cur.fetchone()

        if existing:
            response_id = existing["id"]
            cur.execute(
                "UPDATE responses SET ai_generated_draft = %s, draft_text = %s, "
                "answer_text_with_markers = %s, tone = %s, confidence = %s, gap_flag = %s, "
                "status = %s, generated_by = %s, updated_at = now() WHERE id = %s",
                (
                    answer_text_clean,
                    answer_text_clean,
                    answer_text_with_markers,
                    tone,
                    confidence,
                    gap_flag,
                    status,
                    generated_by,
                    response_id,
                ),
            )
        else:
            cur.execute(
                "INSERT INTO responses (question_id, ai_generated_draft, draft_text, "
                "answer_text_with_markers, tone, confidence, gap_flag, status, generated_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    question_id,
                    answer_text_clean,
                    answer_text_clean,
                    answer_text_with_markers,
                    tone,
                    confidence,
                    gap_flag,
                    status,
                    generated_by,
                ),
            )
            response_id = cur.fetchone()["id"]

        cur.execute("DELETE FROM citations WHERE response_id = %s", (response_id,))
        if citations:
            cur.executemany(
                "INSERT INTO citations (response_id, chunk_id, document_filename, section_path, page, quote) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (response_id, c.chunk_id, c.document_filename, c.section_path, c.page, c.quote)
                    for c in citations
                ],
            )

        if status == "requires_review":
            cur.execute("UPDATE questions SET status = 'drafting' WHERE id = %s", (question_id,))
