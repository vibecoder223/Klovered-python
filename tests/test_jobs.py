"""Unit tests for jobs' pure logic (backoff timing, derived doc status) —
no database, always run."""

from app.pipeline.jobs import _backoff_seconds, _derive_status_from_jobs


def test_backoff_seconds_follows_the_table():
    assert _backoff_seconds(1) == 5.0
    assert _backoff_seconds(2) == 30.0
    assert _backoff_seconds(3) == 120.0


def test_backoff_seconds_caps_at_last_value_beyond_table():
    assert _backoff_seconds(4) == 120.0
    assert _backoff_seconds(100) == 120.0


def _job(stage: str, status: str) -> dict:
    return {"stage": stage, "status": status}


def test_derive_status_no_rows_returns_none():
    assert _derive_status_from_jobs([]) is None


def test_derive_status_dead_pre_generate_stage_blocks_everything():
    rows = [_job("ingest", "dead"), _job("extract", "pending")]
    assert _derive_status_from_jobs(rows) == "failed"


def test_derive_status_dead_extract_stage_maps_to_extraction_failed():
    rows = [_job("extract", "dead")]
    assert _derive_status_from_jobs(rows) == "extraction_failed"


def test_derive_status_active_pre_generate_stage_reports_running_status():
    rows = [_job("ingest", "done"), _job("extract", "claimed")]
    assert _derive_status_from_jobs(rows) == "analyzing"


def test_derive_status_active_reports_earliest_active_stage():
    rows = [_job("ingest", "pending"), _job("structure", "claimed")]
    assert _derive_status_from_jobs(rows) == "extracting"


def test_derive_status_all_generate_jobs_dead_with_no_done_is_generation_failed():
    rows = [_job("structure", "done"), _job("generate", "dead")]
    assert _derive_status_from_jobs(rows) == "generation_failed"


def test_derive_status_one_dead_generate_job_does_not_block_completion():
    rows = [_job("structure", "done"), _job("generate", "dead"), _job("generate", "done")]
    assert _derive_status_from_jobs(rows) == "completed"


def test_derive_status_all_done_is_completed():
    rows = [_job("ingest", "done"), _job("extract", "done"), _job("structure", "done"), _job("generate", "done")]
    assert _derive_status_from_jobs(rows) == "completed"
