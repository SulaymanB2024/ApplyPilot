import json

import pytest
from typer.testing import CliRunner

from applypilot.cli import app
from applypilot.dev_harness.artifacts import read_json, write_json
from applypilot.dev_harness.contracts import DevHarnessSettings, load_settings
from applypilot.dev_harness.knowledge import build_knowledge_index
from applypilot.dev_harness.reviewer import review_proposal
from applypilot.dev_harness.runner import create_plan, create_worker_proposal, run_validation

runner = CliRunner()


def test_settings_reject_forbidden_worker_model(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_DEV_WORKER_MODEL", "gpt-5.3-codex-spark")
    monkeypatch.delenv("APPLYPILOT_DEV_REVIEWER_MODEL", raising=False)

    with pytest.raises(ValueError, match="forbidden"):
        load_settings()


def test_create_plan_writes_bounded_prompt_packet(tmp_path):
    settings = DevHarnessSettings(worker_model="gpt-5.5", reviewer_model="gpt-5.5")

    plan_path = create_plan(
        out_dir=tmp_path,
        goal="Harden the apply harness",
        allowed_files=("src/applypilot/apply/harness.py", "tests/test_harness.py"),
        settings=settings,
    )
    plan = read_json(plan_path)
    worker_prompt = (tmp_path / "worker_prompt.md").read_text(encoding="utf-8")

    assert plan["mode"] == "dry-run"
    assert plan["models"]["forbidden"] == ["gpt-5.3-codex-spark"]
    assert plan["allowed_files"] == ["src/applypilot/apply/harness.py", "tests/test_harness.py"]
    assert plan["artifact_paths"]["results"] == str(tmp_path / "results.json")
    assert "Do not inspect parent directories" in worker_prompt
    assert "Do not spawn subagents" in worker_prompt


def test_plan_writes_progressive_knowledge_artifacts(tmp_path):
    plan_path = create_plan(out_dir=tmp_path, settings=DevHarnessSettings())
    plan = read_json(plan_path)
    index = read_json(tmp_path / "knowledge_index.json")
    research_queue = read_json(tmp_path / "research_queue.json")
    worker_prompt = (tmp_path / "worker_prompt.md").read_text(encoding="utf-8")

    assert plan["artifact_paths"]["knowledge_index"] == str(tmp_path / "knowledge_index.json")
    assert plan["artifact_paths"]["knowledge_cards_dir"] == str(tmp_path / "knowledge_cards")
    assert plan["artifact_paths"]["research_queue"] == str(tmp_path / "research_queue.json")
    assert len(index["cards"]) >= 6
    assert "preferred_actions" not in index["cards"][0]
    assert "ChatGPT Web primary" in research_queue["research_surface"]
    assert "Knowledge index" in worker_prompt
    assert "codex_context_budget" in worker_prompt
    assert "preferred_actions" not in worker_prompt


def test_knowledge_index_omits_full_case_details():
    index = build_knowledge_index()

    assert "retrieval_policy" in index
    assert index["cards"]
    assert all("misalignment_risks" not in card for card in index["cards"])
    assert all("source_refs" not in card for card in index["cards"])


def test_worker_proposal_is_artifact_only_and_not_approved_without_validation(tmp_path):
    plan_path = create_plan(out_dir=tmp_path, settings=DevHarnessSettings())

    proposal_path = create_worker_proposal(plan_path=plan_path)
    proposal = read_json(proposal_path)
    review_path = review_proposal(proposal_path=proposal_path)
    review = read_json(review_path)

    assert proposal["mode"] == "dry-run"
    assert proposal["touches_files"] == []
    assert proposal["proposed_changes"] == []
    assert review["approved"] is False
    assert any(finding["code"] == "missing_validation" for finding in review["findings"])


def test_reviewer_rejects_forbidden_model_out_of_scope_file_and_recursion(tmp_path):
    plan_path = create_plan(out_dir=tmp_path, settings=DevHarnessSettings())
    proposal_path = tmp_path / "proposal.json"
    write_json(
        proposal_path,
        {
            "plan_path": str(plan_path),
            "models": {"worker": "gpt-5.3-codex-spark"},
            "touches_files": ["src/applypilot/apply/prompt.py"],
            "recursive_delegation": True,
            "validation_results": [{"command": "pytest", "exit_code": 0}],
        },
    )

    review_path = review_proposal(proposal_path=proposal_path)
    codes = {finding["code"] for finding in read_json(review_path)["findings"]}

    assert "forbidden_model" in codes
    assert "out_of_scope_file" in codes
    assert "recursive_delegation" in codes


def test_reviewer_rejects_context_bloat(tmp_path):
    plan_path = create_plan(out_dir=tmp_path, settings=DevHarnessSettings())
    proposal_path = tmp_path / "proposal.json"
    write_json(
        proposal_path,
        {
            "plan_path": str(plan_path),
            "models": {"worker": "gpt-5.5"},
            "touches_files": ["src/applypilot/cli.py"],
            "summary": "Load all docs and paste the entire ChatGPT transcript into the prompt.",
            "recursive_delegation": False,
            "validation_results": [{"command": "pytest", "exit_code": 0}],
        },
    )

    review_path = review_proposal(proposal_path=proposal_path)
    codes = {finding["code"] for finding in read_json(review_path)["findings"]}

    assert "context_bloat" in codes


def test_reviewer_approves_only_when_guardrails_and_validation_pass(tmp_path):
    plan_path = create_plan(out_dir=tmp_path, settings=DevHarnessSettings())
    proposal_path = tmp_path / "proposal.json"
    write_json(
        proposal_path,
        {
            "plan_path": str(plan_path),
            "models": {"worker": "gpt-5.5"},
            "touches_files": ["src/applypilot/cli.py"],
            "recursive_delegation": False,
            "validation_results": [{"command": "pytest", "exit_code": 0}],
        },
    )

    review_path = review_proposal(proposal_path=proposal_path)
    review = json.loads(review_path.read_text(encoding="utf-8"))

    assert review["approved"] is True
    assert review["ready_for_patch"] is True


def test_validation_updates_proposal_before_review(tmp_path):
    plan_path = create_plan(out_dir=tmp_path, settings=DevHarnessSettings())
    plan = read_json(plan_path)
    plan["validation_commands"] = ["python -c 'print(\"ok\")'"]
    write_json(plan_path, plan)
    proposal_path = create_worker_proposal(plan_path=plan_path)

    results_path = run_validation(plan_path=plan_path)
    review_path = review_proposal(proposal_path=proposal_path)
    results = read_json(results_path)
    review = read_json(review_path)

    assert results["passed"] is True
    assert review["approved"] is True


def test_cli_review_and_validation_fail_closed_with_nonzero_exit(tmp_path):
    review_dir = tmp_path / "review"
    plan_path = create_plan(out_dir=review_dir, settings=DevHarnessSettings())
    proposal_path = create_worker_proposal(plan_path=plan_path)

    review_result = runner.invoke(app, ["improve", "review", "--artifact", str(proposal_path)])

    assert review_result.exit_code == 1
    assert "not approved" in review_result.output

    validate_dir = tmp_path / "validate"
    failing_plan = create_plan(out_dir=validate_dir, settings=DevHarnessSettings())
    payload = read_json(failing_plan)
    payload["validation_commands"] = ["python -c 'raise SystemExit(7)'"]
    write_json(failing_plan, payload)

    validate_result = runner.invoke(
        app,
        ["improve", "validate", "--artifact", str(failing_plan)],
    )

    assert validate_result.exit_code == 1
    assert "failed" in validate_result.output
