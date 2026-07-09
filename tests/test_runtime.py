from applypilot.apply import launcher
from applypilot.apply.runtime import canonical_job_id, full_jitter_delay_seconds
from applypilot.database import close_connection, init_db, store_jobs


def test_canonical_job_id_strips_tracking_and_preserves_job_keys():
    assert (
        canonical_job_id("https://jobs.example.com/apply?id=123&utm_source=newsletter")
        == "jobs.example.com/apply?id=123"
    )
    assert canonical_job_id("https://boards.greenhouse.io/acme/jobs/456?gh_src=abc") == "greenhouse:456"
    assert canonical_job_id("https://jobs.lever.co/acme/abc-def?utm_campaign=x") == "lever:acme:abc-def"


def test_full_jitter_delay_is_capped_with_seeded_rng():
    import random

    delay = full_jitter_delay_seconds(10, rng=random.Random(7))

    assert 0 <= delay <= 60


def test_store_jobs_skips_duplicate_canonical_job_ids(tmp_path):
    conn = init_db(tmp_path / "applypilot.db")

    new_count, duplicate_count = store_jobs(
        conn,
        [
            {"url": "https://jobs.example.com/apply?id=123&utm_source=a", "title": "Engineer"},
            {"url": "https://jobs.example.com/apply?id=123&utm_source=b", "title": "Engineer duplicate"},
        ],
        site="Example",
        strategy="test",
    )
    close_connection(tmp_path / "applypilot.db")

    assert new_count == 1
    assert duplicate_count == 1


def test_acquire_job_skips_future_retry_and_open_breaker(monkeypatch, tmp_path):
    db_path = tmp_path / "applypilot.db"
    conn = init_db(db_path)
    conn.executemany(
        "INSERT INTO jobs (url, title, site, tailored_resume_path, application_url, fit_score, "
        "canonical_job_id, apply_domain, next_apply_attempt_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "https://future.example.com/job",
                "Future",
                "Example",
                "/tmp/resume.txt",
                "https://future.example.com/apply",
                10,
                "future",
                "future.example.com",
                "2999-01-01T00:00:00+00:00",
            ),
            (
                "https://blocked.example.com/job",
                "Blocked",
                "Example",
                "/tmp/resume.txt",
                "https://blocked.example.com/apply",
                9,
                "blocked",
                "blocked.example.com",
                None,
            ),
            (
                "https://ready.example.com/job",
                "Ready",
                "Example",
                "/tmp/resume.txt",
                "https://ready.example.com/apply",
                8,
                "ready",
                "ready.example.com",
                None,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO apply_domain_circuit_breakers "
        "(domain, failure_count, opened_until, last_reason, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("blocked.example.com", 3, "2999-01-01T00:00:00+00:00", "captcha", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    job = launcher.acquire_job(min_score=7, worker_id=2)
    close_connection(db_path)

    assert job is not None
    assert job["url"] == "https://ready.example.com/job"


def test_mark_result_sets_retry_metadata_and_opens_breaker(monkeypatch, tmp_path):
    db_path = tmp_path / "applypilot.db"
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO jobs (url, title, tailored_resume_path, application_url, fit_score, "
        "canonical_job_id, apply_domain) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "https://blocked.example.com/job",
            "Blocked",
            "/tmp/resume.txt",
            "https://blocked.example.com/apply",
            9,
            "blocked",
            "blocked.example.com",
        ),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(launcher, "next_retry_at", lambda attempt: f"retry-{attempt}")
    monkeypatch.setattr(launcher, "breaker_open_until", lambda: "opened")

    launcher.mark_result("https://blocked.example.com/job", "failed", "timeout", permanent=False)
    row = conn.execute(
        "SELECT apply_attempts, next_apply_attempt_at, apply_error_class FROM jobs WHERE url = ?",
        ("https://blocked.example.com/job",),
    ).fetchone()
    assert row["apply_attempts"] == 1
    assert row["next_apply_attempt_at"] == "retry-1"
    assert row["apply_error_class"] == "retryable"

    for _ in range(3):
        launcher.mark_result("https://blocked.example.com/job", "failed", "captcha", permanent=True)
    breaker = conn.execute(
        "SELECT failure_count, opened_until, last_reason FROM apply_domain_circuit_breakers WHERE domain = ?",
        ("blocked.example.com",),
    ).fetchone()
    close_connection(db_path)

    assert breaker["failure_count"] == 3
    assert breaker["opened_until"] == "opened"
    assert breaker["last_reason"] == "captcha"
