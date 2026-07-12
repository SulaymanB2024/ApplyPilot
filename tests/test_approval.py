import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot import config
from applypilot import cli as cli_module
from applypilot.autonomy import approval as approval_module
from applypilot.autonomy.approval import (
    FACT_APPROVAL_SIGNATURE_NAMESPACE,
    FactApprovalError,
    FactApprovalExpectation,
    approval_json_bytes,
    build_unsigned_fact_approval,
    load_verified_fact_approval,
    verify_and_import_fact_approval,
)
from applypilot.autonomy.facts import fact_ledger_from_dict
from applypilot.autonomy.runner import load_reviewed_run_snapshot, prepare_run
from applypilot.cli import app


ISSUER = "applicant@example.com"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _run_packet(monkeypatch, tmp_path):
    app_dir = tmp_path / "app-data"
    app_dir.mkdir()
    profile = {
        "personal": {"phone": "555-0100", "email": "candidate@example.com"},
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": False,
        },
        "availability": {
            "earliest_start_date": "2027-05-15",
            "preferred_locations": ["Remote", "State College, PA"],
        },
        "experience": {"target_role": "product and data analyst"},
    }
    profile_path = app_dir / "profile.json"
    resume_path = app_dir / "resume.txt"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    resume_path.write_text("Built Python and SQL analytics tools.\n", encoding="utf-8")
    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)
    paths = prepare_run(
        query="entry-level product and data roles",
        output_dir=tmp_path / "runs",
    )
    run_dir = (tmp_path / "runs" / paths["run_dir"].split("/")[-1]).resolve()
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ledger = fact_ledger_from_dict(
        json.loads((run_dir / "fact_ledger.json").read_text(encoding="utf-8"))
    )
    expectation = FactApprovalExpectation.from_run(
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        fact_ledger=ledger,
    )
    return run_dir, ledger.digest, expectation


def _signer(tmp_path, *, name="approval-key"):
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen is required for signed-approval tests")
    key_path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        check=True,
    )
    trust_store = tmp_path / f"{name}.allowed_signers"
    trust_store.write_text(
        f"{ISSUER} {key_path.with_suffix('.pub').read_text(encoding='utf-8')}",
        encoding="utf-8",
    )
    return key_path, trust_store


def _attestation(expectation, tmp_path, *, name="approval", mutate=None):
    payload = build_unsigned_fact_approval(
        expectation,
        issuer=ISSUER,
        source_surface="codex_user_message",
        source_message_sha256=_sha("explicit applicant answers"),
        source_author_sha256=_sha(ISSUER),
        source_observed_at=NOW,
        issued_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    if mutate is not None:
        mutate(payload)
    path = tmp_path / f"{name}.json"
    path.write_bytes(approval_json_bytes(payload))
    return path


def _sign(key_path, attestation):
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(key_path),
            "-n",
            FACT_APPROVAL_SIGNATURE_NAMESPACE,
            str(attestation),
        ],
        check=True,
        capture_output=True,
    )
    return attestation.with_name(attestation.name + ".sig")


def test_signed_fact_approval_is_run_bound_and_reverifiable(monkeypatch, tmp_path):
    run_dir, fact_digest, expectation = _run_packet(monkeypatch, tmp_path)
    key_path, trust_store = _signer(tmp_path)
    attestation = _attestation(expectation, tmp_path)
    signature = _sign(key_path, attestation)

    imported = verify_and_import_fact_approval(
        run_dir=run_dir,
        expectation=expectation,
        attestation_path=attestation,
        signature_path=signature,
        trust_store_path=trust_store,
        now=NOW,
    )
    loaded = load_verified_fact_approval(
        run_dir=run_dir,
        expectation=expectation,
        trust_store_path=trust_store,
        now=NOW,
    )
    monkeypatch.setattr(
        approval_module,
        "require_system_approval_trust_store",
        lambda: trust_store,
    )
    snapshot = load_reviewed_run_snapshot(
        run_dir=run_dir,
        approved_fact_digest=fact_digest,
        require_signed_approval=True,
    )

    assert loaded == imported
    assert snapshot["fact_approval_receipt_sha256"] == imported.receipt_sha256
    assert snapshot["approval_issuer"] == ISSUER

    campaign_dir = tmp_path / "live-campaign"
    monkeypatch.setattr(cli_module, "_current_git_revision", lambda **_: "a" * 40)
    created = CliRunner().invoke(
        app,
        [
            "campaign",
            "create",
            "--run-dir",
            str(run_dir),
            "--approved-fact-digest",
            fact_digest,
            "--campaign-id",
            "signed-live-campaign",
            "--submit",
            "--out",
            str(campaign_dir),
        ],
    )
    assert created.exit_code == 0, created.output
    campaign_manifest = json.loads(
        (campaign_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert campaign_manifest["fact_approval_receipt_sha256"] == imported.receipt_sha256
    assert campaign_manifest["approval_issuer"] == ISSUER
    assert campaign_manifest["fact_approval_expires_at"] == imported.expires_at


def test_digest_only_or_untrusted_signature_cannot_enable_live_campaign(monkeypatch, tmp_path):
    run_dir, fact_digest, expectation = _run_packet(monkeypatch, tmp_path)
    _, trust_store = _signer(tmp_path, name="trusted")
    monkeypatch.setattr(
        approval_module,
        "require_system_approval_trust_store",
        lambda: trust_store,
    )
    with pytest.raises(FactApprovalError, match="cannot be read"):
        load_reviewed_run_snapshot(
            run_dir=run_dir,
            approved_fact_digest=fact_digest,
            require_signed_approval=True,
        )

    untrusted_key, _ = _signer(tmp_path, name="untrusted")
    attestation = _attestation(expectation, tmp_path, name="untrusted-approval")
    signature = _sign(untrusted_key, attestation)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_ssh_keygen = fake_bin / "ssh-keygen"
    fake_ssh_keygen.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_ssh_keygen.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    with pytest.raises(FactApprovalError, match="invalid or untrusted"):
        verify_and_import_fact_approval(
            run_dir=run_dir,
            expectation=expectation,
            attestation_path=attestation,
            signature_path=signature,
            trust_store_path=trust_store,
            now=NOW,
        )


def test_mutated_or_mismatched_fact_approval_fails_closed(monkeypatch, tmp_path):
    run_dir, _, expectation = _run_packet(monkeypatch, tmp_path)
    key_path, trust_store = _signer(tmp_path)
    attestation = _attestation(expectation, tmp_path)
    signature = _sign(key_path, attestation)
    payload = json.loads(attestation.read_text(encoding="utf-8"))
    payload["approved_fact_value_hashes"]["profile.personal.phone"] = "0" * 64
    attestation.write_bytes(approval_json_bytes(payload))

    with pytest.raises(FactApprovalError, match="binding mismatch"):
        verify_and_import_fact_approval(
            run_dir=run_dir,
            expectation=expectation,
            attestation_path=attestation,
            signature_path=signature,
            trust_store_path=trust_store,
            now=NOW,
        )


def test_approval_for_another_run_cannot_be_replayed(monkeypatch, tmp_path):
    run_dir, _, expectation = _run_packet(monkeypatch, tmp_path)
    key_path, trust_store = _signer(tmp_path)
    other = replace(expectation, approval_challenge="f" * 64)
    attestation = _attestation(other, tmp_path, name="other-run")
    signature = _sign(key_path, attestation)

    with pytest.raises(FactApprovalError, match="approval_challenge"):
        verify_and_import_fact_approval(
            run_dir=run_dir,
            expectation=expectation,
            attestation_path=attestation,
            signature_path=signature,
            trust_store_path=trust_store,
            now=NOW,
        )


def test_same_user_trust_store_is_not_a_live_trust_anchor(monkeypatch, tmp_path):
    _, trust_store = _signer(tmp_path)
    monkeypatch.setattr(config, "SYSTEM_APPROVAL_TRUST_STORE_PATH", trust_store)
    with pytest.raises(FactApprovalError, match="root-owned"):
        approval_module.require_system_approval_trust_store()


def test_live_git_revision_ignores_path_injected_binary(monkeypatch, tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nprintf '%064d\\n' 0\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    repository_root = Path(cli_module.__file__).resolve().parents[2]
    expected = subprocess.run(
        [str(config.SYSTEM_GIT_PATH), "rev-parse", "HEAD"],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert cli_module._current_git_revision() == expected
