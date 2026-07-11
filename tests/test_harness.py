import json

import pytest

from applypilot.apply.harness import load_settings, prompt_header
from applypilot.apply.harness import write_contract
from applypilot.apply import launcher
from applypilot.apply.launcher import _is_permanent_failure
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.prompt import (
    _build_job_board_playbook,
    _build_captcha_section,
    _build_training_scenarios,
    build_training_manifest,
)
from applypilot.apply.training_audit import audit_training_manifest
from applypilot.cli import app
from applypilot.config import load_sites_config, normalize_search_config
from applypilot.database import close_connection, init_db
from typer.testing import CliRunner

runner = CliRunner()


def test_claude_backend_keeps_lightweight_default_model():
    settings = load_settings(agent_backend="claude")

    assert settings.agent_backend == "claude"
    assert settings.executor_model == "haiku"


def test_codex_backend_defaults_to_gpt55_and_supervisor(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_ALLOW_ACCOUNT_CREATION", raising=False)
    monkeypatch.delenv("APPLYPILOT_FIELD_MODEL_CALL_BUDGET", raising=False)

    settings = load_settings(agent_backend="codex")

    assert settings.agent_backend == "codex"
    assert settings.executor_model == "gpt-5.5"
    assert settings.supervisor_model == "gpt-5.5"
    assert settings.deterministic_controller is True
    assert settings.allow_account_creation is False
    assert settings.credential_provider == "google_password_manager"
    assert settings.uses_google_password_manager is True
    assert settings.uses_onepassword is False
    assert settings.requires_model_cli is False


def test_model_cli_is_required_only_when_field_budget_is_enabled(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FIELD_MODEL_CALL_BUDGET", "1")

    settings = load_settings(agent_backend="codex")

    assert settings.requires_model_cli is True


def test_account_creation_requires_explicit_settings_override(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_ALLOW_ACCOUNT_CREATION", raising=False)

    assert load_settings().allow_account_creation is False
    assert load_settings(allow_account_creation=True).allow_account_creation is True


@pytest.mark.parametrize(
    ("extra_args", "expected_dry_run", "expected_account_creation"),
    [
        ([], True, False),
        (["--submit", "--approved-fact-digest", "reviewed"], False, False),
        (["--allow-account-creation"], True, True),
    ],
)
def test_apply_uses_deterministic_controller_without_model_cli(
    monkeypatch,
    tmp_path,
    extra_args,
    expected_dry_run,
    expected_account_creation,
):
    from applypilot import cli, config
    from applypilot.apply import google_passwords
    from applypilot.apply import field_resolver

    class FakeCursor:
        @staticmethod
        def fetchone():
            return (1,)

    class FakeConnection:
        @staticmethod
        def execute(_query):
            return FakeCursor()

    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}", encoding="utf-8")
    captured = {}
    monkeypatch.delenv("APPLYPILOT_FIELD_MODEL_CALL_BUDGET", raising=False)
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "get_chrome_path", lambda: "/Applications/Google Chrome.app")
    monkeypatch.setattr("applypilot.database.get_connection", lambda: FakeConnection())
    monkeypatch.setattr(field_resolver, "find_codex_executable", lambda: None)
    monkeypatch.setattr(
        google_passwords,
        "choose_chrome_profile_for_google_passwords",
        lambda: "Default",
    )
    monkeypatch.setattr(
        launcher,
        "main",
        lambda **kwargs: captured.update(kwargs),
    )

    result = runner.invoke(app, ["apply", "--limit", "1", *extra_args])

    assert result.exit_code == 0, result.output
    assert captured["dry_run"] is expected_dry_run
    assert captured["allow_account_creation"] is expected_account_creation


def test_executor_model_override_wins_for_codex():
    settings = load_settings(agent_backend="codex", executor_model="custom-model")

    assert settings.executor_model == "custom-model"


def test_prompt_header_keeps_email_submissions_at_draft_boundary():
    header = prompt_header(load_settings(agent_backend="codex"))

    assert "never send outbound email" in header
    assert "email_application_draft.md" in header
    assert "RESULT:EMAIL_DRAFT" in header


def test_captcha_section_fails_closed_without_solver_recipe():
    section = _build_captcha_section()

    assert "RESULT:CAPTCHA" in section
    assert "CapSolver" not in section
    assert "createTask" not in section
    assert "getTaskResult" not in section
    assert "g-recaptcha-response" not in section


def test_job_board_playbook_trains_runway_and_workday_paths():
    playbook = _build_job_board_playbook({
        "boards": ["indeed", "linkedin", "glassdoor", "zip_recruiter", "google"],
    })

    assert "Runway" in playbook
    assert "https://app.joinrunway.io/explore" in playbook
    assert "Workday" in playbook
    assert "multi-page ATS" in playbook
    assert "ZipRecruiter" in playbook
    assert "Google Jobs" in playbook
    assert "Easy Apply is acceptable only for the exact role" in playbook
    assert "one-click apply is acceptable only when it submits this role" in playbook
    assert "Pick the most direct company/ATS apply link" in playbook


def test_job_board_playbook_includes_scenario_drills():
    playbook = _build_job_board_playbook({
        "boards": ["indeed", "linkedin", "google"],
    })

    assert "Workday drill" in playbook
    assert "expand Review sections" in playbook
    assert "Email-only drill" in playbook
    assert "To, Subject, Attachments, Body, and Evidence fields" in playbook
    assert "Runway drill" in playbook
    assert "Aggregator drill" in playbook
    assert "Native apply drill" in playbook
    assert "External ATS drill" in playbook


def test_training_scenarios_cover_requested_application_paths():
    scenarios = _build_training_scenarios()

    assert "== TRAINING SCENARIOS ==" in scenarios
    assert "Workday resume parser review" in scenarios
    assert "myworkdayjobs.com" in scenarios
    assert "Email-only application" in scenarios
    assert "email_application_draft.md" in scenarios
    assert "Runway fresh-role discovery" in scenarios
    assert "app.joinrunway.io/explore" in scenarios
    assert "Aggregator to employer ATS" in scenarios
    assert "Native easy apply" in scenarios
    assert "External ATS form" in scenarios


def test_training_manifest_records_requested_coverage():
    manifest = build_training_manifest({
        "boards": ["indeed", "linkedin", "zip_recruiter", "google"],
    })

    assert manifest["version"] == "apply-training-v1"
    assert "workday_application_flow" in manifest["required_capabilities"]
    assert "email_only_local_draft" in manifest["required_capabilities"]
    assert "runway_fresh_role_discovery" in manifest["required_capabilities"]
    assert "configured_job_board_catalog" in manifest["required_capabilities"]
    assert manifest["email_draft_artifact"] == "email_application_draft.md"
    assert manifest["runway_url"] == "https://app.joinrunway.io/explore"
    assert "Workday resume parser review" in manifest["scenario_names"]
    assert any(board["label"] == "ZipRecruiter" for board in manifest["jobspy_boards"])
    assert any(source["name"] == "Runway" for source in manifest["smart_extract_sources"])


def test_training_manifest_audit_passes_for_default_training_surface():
    audit = audit_training_manifest(build_training_manifest())

    assert audit["passed"] is True
    assert audit["has_runway_source"] is True
    assert audit["email_draft_artifact_ok"] is True
    assert audit["missing_capabilities"] == []
    assert audit["missing_scenarios"] == []


def test_training_manifest_audit_fails_closed_on_missing_boundaries():
    manifest = build_training_manifest({
        "boards": ["indeed", "linkedin"],
    })
    manifest["required_capabilities"] = [
        capability
        for capability in manifest["required_capabilities"]
        if capability != "runway_fresh_role_discovery"
    ]
    manifest["smart_extract_sources"] = [
        source
        for source in manifest["smart_extract_sources"]
        if source.get("name") != "Runway"
    ]

    audit = audit_training_manifest(manifest)

    assert audit["passed"] is False
    assert "runway_fresh_role_discovery" in audit["missing_capabilities"]
    assert audit["has_runway_source"] is False


def test_training_manifest_audit_marks_zero_boards_not_applicable_for_direct_sources():
    manifest = build_training_manifest({
        "discovery_mode": "direct_sources",
        "boards": [],
    })

    audit = audit_training_manifest(manifest)

    assert audit["passed"] is True
    assert audit["jobspy_board_status"] == "not_applicable"
    assert audit["failures"]["jobspy_boards"] is False


def test_training_manifest_audit_fails_zero_boards_when_jobspy_is_enabled():
    for discovery_mode in ("hybrid", "job_boards"):
        manifest = build_training_manifest({
            "discovery_mode": discovery_mode,
            "boards": [],
        })

        audit = audit_training_manifest(manifest)

        assert audit["passed"] is False
        assert audit["jobspy_board_status"] == "fail"
        assert audit["failures"]["jobspy_boards"] is True


def test_training_audit_cli_prints_user_facing_report():
    result = runner.invoke(app, ["training-audit"])

    assert result.exit_code == 0
    assert "ApplyPilot Training Audit" in result.output
    assert "Runway" in result.output
    assert "email_application_draft.md" in result.output


def test_training_audit_cli_renders_zero_direct_source_boards_as_not_applicable(monkeypatch):
    manifest = build_training_manifest({
        "discovery_mode": "direct_sources",
        "boards": [],
    })
    monkeypatch.setattr(prompt_mod, "build_training_manifest", lambda: manifest)

    result = runner.invoke(app, ["training-audit"])

    assert result.exit_code == 0
    assert "N/A" in result.output
    assert "0 configured JobSpy board(s)" in result.output


def test_training_audit_cli_fails_zero_hybrid_boards(monkeypatch):
    manifest = build_training_manifest({
        "discovery_mode": "hybrid",
        "boards": [],
    })
    monkeypatch.setattr(prompt_mod, "build_training_manifest", lambda: manifest)

    result = runner.invoke(app, ["training-audit"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "0 configured JobSpy board(s)" in result.output


def test_doctor_strict_json_exits_nonzero_for_required_missing_files(
    monkeypatch,
    tmp_path,
):
    from applypilot import config

    monkeypatch.setattr(config, "PROFILE_PATH", tmp_path / "profile.json")
    monkeypatch.setattr(config, "RESUME_PATH", tmp_path / "resume.txt")
    monkeypatch.setattr(config, "RESUME_PDF_PATH", tmp_path / "resume.pdf")
    monkeypatch.setattr(config, "SEARCH_CONFIG_PATH", tmp_path / "searches.yaml")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "load_search_config", lambda: {"discovery_mode": "direct_sources"})

    result = runner.invoke(app, ["doctor", "--strict", "--json"])

    assert result.exit_code == 1
    assert '"ready": false' in result.output
    assert '"profile.json"' in result.output
    assert '"resume.txt"' in result.output


def test_doctor_strict_requires_chatgpt_web_probe(monkeypatch, tmp_path):
    from applypilot import config

    profile_path = tmp_path / "profile.json"
    resume_path = tmp_path / "resume.txt"
    profile_path.write_text("{}", encoding="utf-8")
    resume_path.write_text("resume", encoding="utf-8")
    monkeypatch.setenv("APPLYPILOT_LLM_PROVIDER", "chatgpt_web")
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(config, "RESUME_PDF_PATH", tmp_path / "resume.pdf")
    monkeypatch.setattr(config, "SEARCH_CONFIG_PATH", tmp_path / "searches.yaml")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "load_search_config", lambda: {"discovery_mode": "direct_sources"})
    monkeypatch.setattr(config, "get_chrome_path", lambda: "/Applications/Google Chrome.app")

    result = runner.invoke(app, ["doctor", "--strict", "--json"])

    assert result.exit_code == 1
    assert '"ChatGPT Web"' in result.output
    assert "configured but unprobed" in result.output


def test_harness_contract_references_training_manifest(tmp_path):
    prompt_path = tmp_path / "input_prompt.md"
    prompt_path.write_text("prompt", encoding="utf-8")
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text("{}", encoding="utf-8")
    manifest_path = tmp_path / "apply_training_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")

    contract_path = write_contract(
        worker_dir=tmp_path,
        worker_id=2,
        port=9333,
        job={"url": "https://example.com/job", "title": "Engineer", "site": "Example"},
        settings=load_settings(agent_backend="codex"),
        prompt_path=prompt_path,
        mcp_config_path=mcp_path,
        training_manifest_path=manifest_path,
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))

    assert contract["artifacts"]["training_manifest"] == str(manifest_path)
    assert "training manifest records Workday, email draft, Runway, and board handoff coverage" in contract["done_criteria"]


def test_build_prompt_injects_training_scenarios(monkeypatch, tmp_path):
    resume_txt = tmp_path / "tailored_resume.txt"
    resume_pdf = tmp_path / "tailored_resume.pdf"
    resume_txt.write_text("Tailored resume text", encoding="utf-8")
    resume_pdf.write_bytes(b"%PDF-1.4\n")

    profile = {
        "personal": {
            "full_name": "Test Candidate",
            "email": "candidate@example.com",
            "phone": "555-0100",
            "city": "Austin",
            "country": "USA",
            "password": "never-include-this-password",
        },
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": False,
        },
        "compensation": {
            "salary_expectation": "120000",
            "salary_currency": "USD",
        },
        "experience": {
            "years_of_experience_total": "5",
            "target_role": "software engineer",
        },
        "availability": {"earliest_start_date": "Immediately"},
        "eeo_voluntary": {},
    }
    search_config = {
        "boards": ["indeed", "linkedin", "google"],
        "location": {"accept_patterns": ["Austin", "Remote"]},
    }

    monkeypatch.setattr(prompt_mod.config, "load_profile", lambda: profile)
    monkeypatch.setattr(prompt_mod.config, "load_search_config", lambda: search_config)
    monkeypatch.setattr(prompt_mod.config, "APPLY_WORKER_DIR", tmp_path / "workers")
    monkeypatch.setattr(prompt_mod, "_build_captcha_section", lambda: "== CAPTCHA ==\nnot tested")

    prompt = prompt_mod.build_prompt(
        job={
            "url": "https://example.com/job",
            "application_url": "https://example.com/apply",
            "title": "Software Engineer",
            "site": "ExampleCo",
            "fit_score": 9,
            "tailored_resume_path": str(resume_txt),
        },
        tailored_resume="Tailored resume text",
    )

    assert "== TRAINING SCENARIOS ==" in prompt
    assert "Workday resume parser review" in prompt
    assert "Email-only application" in prompt
    assert "Runway fresh-role discovery" in prompt
    assert "Aggregator to employer ATS" in prompt
    assert "never-include-this-password" not in prompt


def test_legacy_agent_controller_is_disabled_before_prompt_construction(monkeypatch):
    monkeypatch.setattr(
        launcher.harness,
        "load_settings",
        lambda **_kwargs: type(
            "Settings",
            (),
            {"agent_backend": "claude", "deterministic_controller": False},
        )(),
    )
    monkeypatch.setattr(
        launcher.prompt_mod,
        "build_prompt",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("prompt must not be built")),
    )

    with pytest.raises(RuntimeError, match="legacy_agent_controller_disabled"):
        launcher.run_job({"url": "https://example.com/job"}, port=9222)


def test_job_board_playbook_includes_configured_smart_extract_sources():
    playbook = _build_job_board_playbook({"boards": []})
    configured_names = [
        site["name"]
        for site in load_sites_config().get("sites", [])
        if site.get("name")
    ]

    missing = [name for name in configured_names if name not in playbook]
    assert missing == []


def test_email_draft_result_is_permanent_handoff_status():
    assert _is_permanent_failure("email_draft")


def test_submitted_unconfirmed_and_required_unresolved_are_permanent():
    assert _is_permanent_failure("submitted_unconfirmed")
    assert _is_permanent_failure("failed:required_field_unresolved")


def test_dry_run_verified_releases_lock_without_applied_at(monkeypatch, tmp_path):
    db_path = tmp_path / "applypilot.db"
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO jobs (url, title, tailored_resume_path, application_url, apply_status, agent_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("https://example.com/job", "Engineer", "/tmp/resume.txt", "https://example.com/apply", "in_progress", "w0"),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    launcher.mark_dry_run_verified("https://example.com/job", duration_ms=1234)

    row = conn.execute(
        "SELECT apply_status, applied_at, apply_duration_ms, verification_confidence, agent_id "
        "FROM jobs WHERE url = ?",
        ("https://example.com/job",),
    ).fetchone()
    close_connection(db_path)

    assert row["apply_status"] is None
    assert row["applied_at"] is None
    assert row["apply_duration_ms"] == 1234
    assert row["verification_confidence"] == "dry_run"
    assert row["agent_id"] is None


def test_search_config_normalizes_board_and_location_aliases():
    cfg = normalize_search_config({
        "boards": ["indeed", "linkedin", "google"],
        "country": "USA",
        "location": {
            "accept_patterns": ["Remote", "Austin"],
            "reject_patterns": ["India"],
        },
    })

    assert cfg["sites"] == ["indeed", "linkedin", "google"]
    assert cfg["defaults"]["country_indeed"] == "usa"
    assert cfg["location_accept"] == ["Remote", "Austin"]
    assert cfg["location_reject_non_remote"] == ["India"]
