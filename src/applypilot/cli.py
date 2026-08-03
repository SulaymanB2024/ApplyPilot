"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

if TYPE_CHECKING:
    from applypilot.workflow import WorkflowStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
improve_app = typer.Typer(
    name="improve",
    help="Bounded self-improvement development harness.",
    no_args_is_help=True,
)
autonomy_app = typer.Typer(
    name="autonomy",
    help="Tool-first, budgeted ChatGPT Web application funnel.",
    no_args_is_help=True,
)
campaign_app = typer.Typer(
    name="campaign",
    help="Durable, evidence-bound multi-application campaign state.",
    no_args_is_help=True,
)
opportunities_app = typer.Typer(
    name="opportunities",
    help="Evidence-bound startup opportunity research and outreach preparation.",
    no_args_is_help=True,
)
app.add_typer(improve_app, name="improve")
app.add_typer(autonomy_app, name="autonomy")
app.add_typer(campaign_app, name="campaign")
app.add_typer(opportunities_app, name="opportunities")
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _bootstrap_config_only() -> None:
    """Load env and create user data directories without opening the jobs DB."""
    from applypilot.config import load_env, ensure_dirs

    load_env()
    ensure_dirs()


def _resolve_autonomy_run_selector(
    *,
    run_dir: Optional[Path],
    latest: bool,
) -> Path:
    """Require one explicit run selector and resolve it without guessing."""
    if latest == (run_dir is not None):
        raise ValueError("use exactly one of --run-dir or --latest")
    if run_dir is not None:
        return run_dir
    from applypilot.autonomy.runner import latest_autonomy_run_dir

    return latest_autonomy_run_dir()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


def _current_git_revision(*, require_clean: bool = False) -> str:
    """Return the exact checkout revision used to create campaign state."""
    import subprocess

    from applypilot import config
    from applypilot.autonomy.approval import require_root_protected_file

    repository_root = Path(__file__).resolve().parents[2]
    executable = require_root_protected_file(
        config.SYSTEM_GIT_PATH,
        label="system git",
        executable=True,
    )
    clean_environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    result = subprocess.run(
        [str(executable), "rev-parse", "HEAD"],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=clean_environment,
    )
    revision = result.stdout.strip().lower()
    if result.returncode != 0 or not revision:
        raise ValueError("cannot resolve code revision; pass --code-revision explicitly")
    if require_clean:
        status = subprocess.run(
            [str(executable), "status", "--porcelain"],
            cwd=repository_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=clean_environment,
        )
        if status.returncode != 0 or status.stdout.strip():
            raise ValueError("live campaign creation requires a clean reviewed checkout")
    return revision


def _aggregation_data_paths() -> tuple[Path, Path, Path]:
    """Resolve aggregation paths at command time so APPLYPILOT_DIR remains testable."""
    from applypilot import config

    data_dir = Path(os.environ.get("APPLYPILOT_DIR") or config.APP_DIR).expanduser().resolve()
    run_dir = data_dir / "aggregation-runs"
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return data_dir / "aggregation.sqlite3", run_dir, data_dir / "applypilot.db"


def _opportunity_data_paths() -> tuple[Path, Path]:
    """Resolve the company-intelligence ledger separately from job/workflow state."""
    from applypilot import config

    data_dir = Path(os.environ.get("APPLYPILOT_DIR") or config.APP_DIR).expanduser().resolve()
    run_dir = data_dir / "opportunity-runs"
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return data_dir / "opportunities.sqlite3", run_dir


def _ensure_off_posting_research(*, parent_run_id: str) -> dict[str, Any]:
    """Idempotently route an empty posted-job funnel into company-level research."""
    from applypilot import config
    from applypilot.observability.events import EventJournal
    from applypilot.opportunities.models import OpportunitySignal
    from applypilot.opportunities.research import build_research_request, write_research_mission
    from applypilot.opportunities.store import OpportunityStore

    route_digest = hashlib.sha256(parent_run_id.encode("utf-8")).hexdigest()[:24]
    run_id = f"offpost-{route_digest}"
    database_path, run_root = _opportunity_data_paths()
    with OpportunityStore(database_path) as store:
        try:
            existing = store.run_status(run_id)
        except KeyError:
            run_dir = run_root / run_id
            run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            request = build_research_request(
                run_id=run_id,
                signals=(
                    OpportunitySignal.ACTIVELY_HIRING,
                    OpportunitySignal.GENERAL_GROWTH,
                ),
                recent_days=45,
                profile=config.load_profile(),
            )
            journal = EventJournal(run_dir / "events.ndjson", run_id=run_id)
            request_path = write_research_mission(
                run_dir=run_dir,
                request=request,
                journal=journal,
            )
            store.start_run(run_id, request.to_dict(), request_path=request_path)
            existing = store.run_status(run_id)
    return {
        "route_run_id": run_id,
        "status": str(existing["status"]),
        "request_path": str(existing["request_path"]),
        "route_priority": [
            "general_interest_application",
            "speculative_outreach",
        ],
        "external_contact_attempted": False,
    }


def _build_aggregation_sources(
    *,
    source_names: list[str],
    import_path: Optional[Path],
    cache_db_path: Path,
) -> list[Any]:
    """Build only explicit deterministic source adapters."""
    from applypilot.aggregation.sources import (
        CacheSource,
        DirectATSSource,
        ManualImportSource,
        SmartExtractSource,
        WorkdaySource,
    )

    registry = {
        "cache": lambda: CacheSource(db_path=cache_db_path),
        "direct_ats": DirectATSSource,
        "workday": WorkdaySource,
        "smart_extract": SmartExtractSource,
    }
    sources: list[Any] = []
    for name in source_names:
        factory = registry.get(name)
        if factory is None:
            if name in {"handshake", "runway"}:
                raise ValueError(f"use --portal {name} for a browser mission")
            raise ValueError(f"unknown aggregation source: {name}")
        sources.append(factory())
    if import_path is not None:
        sources.append(ManualImportSource(import_path))
    return sources


def _launch_jobspy_enrichment(*, run_id: str, data_dir: Path, run_directory: Path) -> int:
    """Launch the durable JobSpy manager without inheriting credentials or proxy settings."""
    import subprocess
    import sys

    stdout_path = run_directory / "jobspy-manager.stdout.log"
    stderr_path = run_directory / "jobspy-manager.stderr.log"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    stdout_descriptor = os.open(stdout_path, flags, 0o600)
    stderr_descriptor = os.open(stderr_path, flags, 0o600)
    stdout_handle = os.fdopen(stdout_descriptor, "wb")
    stderr_handle = os.fdopen(stderr_descriptor, "wb")
    environment = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "APPLYPILOT_DIR": str(data_dir),
    }
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "applypilot.cli",
                "aggregate-enrich-jobspy",
                "--run-id",
                run_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()
    return int(process.pid)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command("profile-cache")
def profile_cache_status(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the value-free cache report as JSON.",
    ),
    collect: bool = typer.Option(
        False,
        "--collect",
        help="Ask for missing applicant-owned facts and store them privately.",
    ),
    required_only: bool = typer.Option(
        False,
        "--required-only",
        help="With --collect, ask only for facts required before form work.",
    ),
) -> None:
    """Show which resume-backed and applicant-owned facts are ready for autofill."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.profile_cache import (
        build_profile_cache_report,
        missing_profile_cache_questions,
        update_profile_cache,
        write_private_profile,
    )
    from rich.prompt import Prompt

    if not config.PROFILE_PATH.exists() or not config.RESUME_PATH.exists():
        console.print("[red]Profile cache is incomplete. Run applypilot init first.[/red]")
        raise typer.Exit(code=1)
    if required_only and not collect:
        console.print("[red]--required-only can only be used with --collect.[/red]")
        raise typer.Exit(code=2)

    profile = config.load_profile()
    if collect:
        answers: dict[str, Any] = {}
        questions = missing_profile_cache_questions(profile, required_only=required_only)
        for question in questions:
            if question.value_kind == "boolean":
                answer = Prompt.ask(
                    question.prompt,
                    choices=["yes", "no", "skip"],
                    default="skip",
                )
                if answer != "skip":
                    answers[question.path] = answer == "yes"
                continue
            answer = Prompt.ask(f"{question.prompt} (leave blank to skip)", default="")
            if not answer.strip():
                continue
            answers[question.path] = (
                [item.strip() for item in answer.split(",") if item.strip()]
                if question.value_kind == "list"
                else answer
            )
        if answers:
            profile = update_profile_cache(profile, answers)
            backup_path = write_private_profile(config.PROFILE_PATH, profile)
            console.print(f"Updated [bold]{len(answers)}[/bold] private profile-cache fields.")
            if backup_path is not None:
                console.print(f"Previous profile backed up to {backup_path}.")
        else:
            console.print("No profile-cache values changed.")

    report = build_profile_cache_report(
        profile,
        resume_text=config.RESUME_PATH.read_text(encoding="utf-8"),
        resume_pdf_path=config.RESUME_PDF_PATH,
    )
    if as_json:
        console.print_json(data=report)
        return

    table = Table(title="ApplyPilot profile cache")
    table.add_column("Field")
    table.add_column("Form work")
    table.add_column("Source")
    table.add_column("Status")
    for field in report["fields"]:
        table.add_row(
            field["label"],
            "required" if field["required_for_form_work"] else "recommended",
            field["source"],
            "ready" if field["present"] else "missing",
        )
    console.print(table)
    state = "ready" if report["ready_for_form_work"] else "missing required facts"
    console.print(f"Profile cache: [bold]{state}[/bold]")
    if report["pending_verification"]:
        console.print(
            "Pending verification: "
            + ", ".join(report["pending_verification"])
            + " (not used for autofill)"
        )


@opportunities_app.command("discover")
def discover_opportunities(
    signal: Optional[list[str]] = typer.Option(
        None,
        "--signal",
        help="Repeat recently-funded and/or actively-hiring; defaults to both.",
    ),
    recent_days: int = typer.Option(45, "--recent-days", min=1, max=365),
    watch: bool = typer.Option(False, "--watch", help="Include the current telemetry cursor."),
) -> None:
    """Create one bounded public-browser research mission; do not contact companies."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.observability.events import EventJournal
    from applypilot.opportunities.models import OpportunitySignal
    from applypilot.opportunities.research import (
        build_research_request,
        write_research_mission,
    )
    from applypilot.opportunities.store import OpportunityStore

    aliases = {
        "recently-funded": OpportunitySignal.RECENT_FUNDING,
        "recent_funding": OpportunitySignal.RECENT_FUNDING,
        "actively-hiring": OpportunitySignal.ACTIVELY_HIRING,
        "actively_hiring": OpportunitySignal.ACTIVELY_HIRING,
    }
    requested = signal or ["recently-funded", "actively-hiring"]
    try:
        signals = tuple(aliases[item] for item in requested)
        if len(set(signals)) != len(signals):
            raise ValueError("opportunity signals must not be repeated")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"opp-{stamp}-{secrets.token_hex(4)}"
        database_path, run_root = _opportunity_data_paths()
        run_dir = run_root / run_id
        run_dir.mkdir(mode=0o700)
        request = build_research_request(
            run_id=run_id,
            signals=signals,
            recent_days=recent_days,
            profile=config.load_profile(),
        )
        journal = EventJournal(run_dir / "events.ndjson", run_id=run_id)
        request_path = write_research_mission(
            run_dir=run_dir,
            request=request,
            journal=journal,
        )
        with OpportunityStore(database_path) as store:
            store.start_run(run_id, request.to_dict(), request_path=request_path)
        console.print_json(
            data={
                "run_id": run_id,
                "status": "awaiting_browser",
                "request_path": str(request_path),
                "next_action": "service_bounded_browser_research_handoff",
                "watch_requested": watch,
                "event_sequence": journal.read()[-1].sequence,
                "external_contact_attempted": False,
            }
        )
    except KeyError as exc:
        console.print(f"[red]Opportunity discovery failed:[/red] unknown signal {exc.args[0]!r}")
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        console.print(
            f"[red]Opportunity discovery failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc


@opportunities_app.command("status")
def opportunity_status(
    run_id: str = typer.Argument(...),
    watch: bool = typer.Option(False, "--watch", help="Include the current telemetry cursor."),
) -> None:
    """Report durable research state without running or consuming the browser mission."""
    from applypilot.observability.events import EventJournal
    from applypilot.opportunities.store import OpportunityStore

    database_path, _ = _opportunity_data_paths()
    try:
        with OpportunityStore(database_path) as store:
            status = store.run_status(run_id)
        request_path = Path(str(status["request_path"]))
        response_path = request_path.with_name(
            request_path.name.replace(".request.json", ".response.json")
        )
        receipt_path = response_path.with_name(
            response_path.name.replace(".response.json", ".receipt.json")
        )
        journal_path = request_path.parent.parent / "events.ndjson"
        events = EventJournal(journal_path, run_id=run_id).read() if journal_path.exists() else []
        console.print_json(
            data={
                **status,
                "request": status["request"],
                "browser_state": (
                    "consumed"
                    if receipt_path.exists()
                    else "response_ready"
                    if response_path.exists()
                    else "awaiting_response"
                ),
                "event_sequence": events[-1].sequence if events else 0,
                "latest_phase": events[-1].phase if events else "",
                "watch_requested": watch,
            }
        )
    except Exception as exc:
        console.print(
            f"[red]Opportunity status failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc


@opportunities_app.command("import")
def import_opportunity_research(
    run_id: str = typer.Argument(...),
    response: Optional[Path] = typer.Option(
        None,
        "--response",
        help="Browser-produced response to validate and import; omit if already at response_path.",
    ),
) -> None:
    """Validate and consume one bounded research artifact into the separate ledger."""
    from applypilot.autonomy.handoff import import_response_artifact
    from applypilot.observability.events import EventJournal
    from applypilot.opportunities.research import consume_research_response
    from applypilot.opportunities.store import OpportunityStore

    database_path, _ = _opportunity_data_paths()
    try:
        with OpportunityStore(database_path) as store:
            status = store.run_status(run_id)
            request_path = Path(str(status["request_path"]))
            if response is not None:
                import_response_artifact(request_path=request_path, input_path=response)
            result = consume_research_response(
                request_path=request_path,
                store=store,
                journal=EventJournal(
                    request_path.parent.parent / "events.ndjson",
                    run_id=run_id,
                ),
            )
        console.print_json(data=result)
    except Exception as exc:
        console.print(
            f"[red]Opportunity import failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc


@opportunities_app.command("list")
def list_opportunities(
    status: Optional[str] = typer.Option(None, "--status"),
    limit: int = typer.Option(25, "--limit", min=1, max=100),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List company signals; these are not job candidates or sent messages."""
    from applypilot.opportunities.store import OpportunityStore

    database_path, _ = _opportunity_data_paths()
    try:
        with OpportunityStore(database_path) as store:
            rows = store.list_leads(status=status, limit=limit)
        if as_json:
            console.print_json(data=rows)
            return
        table = Table(title="Verified startup opportunity ledger")
        for column in ("Lead", "Domain", "Signal", "Status", "Score"):
            table.add_column(column)
        for row in rows:
            table.add_row(
                str(row["lead_id"]),
                str(row["company_domain"]),
                str(row["signal"]),
                str(row["status"]),
                str(row["score"] if row["score"] is not None else ""),
            )
        console.print(table)
    except Exception as exc:
        console.print(
            f"[red]Opportunity list failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc


@opportunities_app.command("show")
def show_opportunity(
    lead_id: str = typer.Argument(...),
    evidence: bool = typer.Option(False, "--evidence"),
) -> None:
    """Show one company lead and optionally its complete structured evidence."""
    from applypilot.opportunities.store import OpportunityStore

    database_path, _ = _opportunity_data_paths()
    try:
        with OpportunityStore(database_path) as store:
            record = store.get_lead(lead_id)
        if not evidence:
            record.pop("evidence", None)
        console.print_json(data=record)
    except Exception as exc:
        console.print(
            f"[red]Opportunity show failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc


@opportunities_app.command("draft")
def draft_opportunity_outreach(
    lead_id: str = typer.Argument(...),
    channel: str = typer.Option("auto", "--channel"),
    sender: str = typer.Option("sybatx@gmail.com", "--sender"),
) -> None:
    """Create a local evidence-bound inquiry draft; never access a mailbox or send."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.observability.events import EventJournal
    from applypilot.opportunities.models import OpportunityLead, OpportunityRoute
    from applypilot.opportunities.outreach import build_outreach_draft, persist_draft
    from applypilot.opportunities.store import OpportunityStore

    database_path, _ = _opportunity_data_paths()
    try:
        with OpportunityStore(database_path) as store:
            record = store.get_lead(lead_id)
            lead = OpportunityLead.from_dict(record["lead"])
            selected_channel = (
                "contact_form"
                if channel == "auto"
                and lead.route is OpportunityRoute.GENERAL_INTEREST_APPLICATION
                else "email"
                if channel == "auto"
                else channel
            )
            run = store.run_status(str(record["run_id"]))
            run_dir = Path(str(run["request_path"])).parent.parent
            journal = EventJournal(
                run_dir / "events.ndjson", run_id=str(record["run_id"])
            )
            journal.emit(
                component="outreach",
                phase="draft_started",
                status="started",
                source=selected_channel,
                counts={"item_count": 1},
            )
            draft = build_outreach_draft(
                lead,
                profile=config.load_profile(),
                channel=selected_channel,
                sender=sender,
            )
            draft_path = persist_draft(
                run_dir / "outreach" / f"{draft.draft_id}.json", draft
            )
            store.persist_draft(draft, artifact_path=draft_path)
            journal.emit(
                component="outreach",
                phase="draft_validated",
                status="complete",
                source=selected_channel,
                counts={"word_count": len(draft.body.split())},
            )
            journal.emit(
                component="outreach",
                phase="awaiting_authorization",
                status="blocked",
                source=selected_channel,
                counts={"item_count": 1},
            )
        console.print_json(
            data={
                "draft_id": draft.draft_id,
                "lead_id": draft.lead_id,
                "status": "draft_ready",
                "draft_sha256": draft.sha256,
                "draft_path": str(draft_path),
                "external_contact_attempted": False,
                "next_action": "review_exact_draft",
            }
        )
    except Exception as exc:
        console.print(
            f"[red]Opportunity draft failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc


@opportunities_app.command("review-draft")
def review_opportunity_draft(draft_id: str = typer.Argument(...)) -> None:
    """Print the exact local draft and all digest bindings for user review."""
    from applypilot.opportunities.store import OpportunityStore

    database_path, _ = _opportunity_data_paths()
    try:
        with OpportunityStore(database_path) as store:
            record = store.get_draft(draft_id)
        console.print_json(data=record)
    except Exception as exc:
        console.print(
            f"[red]Draft review failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc


@opportunities_app.command("authorize-outreach")
def authorize_opportunity_outreach(
    item: list[str] = typer.Option(
        ...,
        "--item",
        help="Repeat exact LEAD_ID:DRAFT_ID:DRAFT_SHA256 bindings (1-10).",
    ),
    sender: str = typer.Option("sybatx@gmail.com", "--sender"),
    channel: str = typer.Option("auto", "--channel"),
) -> None:
    """Mint one exact local grant only after explicit user approval of this batch."""
    from applypilot.opportunities.outreach import OutreachDraft
    from applypilot.opportunities.send_handoff import (
        build_outreach_authorization,
        write_outreach_authorization,
    )
    from applypilot.opportunities.store import OpportunityStore

    database_path, _ = _opportunity_data_paths()
    try:
        if not 1 <= len(item) <= 10:
            raise ValueError("authorize-outreach requires 1 to 10 exact items")
        bindings: list[tuple[str, str, str]] = []
        for value in item:
            parts = value.split(":")
            if len(parts) != 3:
                raise ValueError("each --item must be LEAD_ID:DRAFT_ID:DRAFT_SHA256")
            bindings.append((parts[0], parts[1], parts[2]))
        with OpportunityStore(database_path) as store:
            drafts: list[OutreachDraft] = []
            preview: list[dict[str, Any]] = []
            for lead_id, draft_id, draft_sha256 in bindings:
                record = store.get_draft(draft_id)
                draft = OutreachDraft.from_dict(record["draft"])
                if draft.lead_id != lead_id or draft.sha256 != draft_sha256:
                    raise ValueError("exact outreach item differs from the stored draft")
                drafts.append(draft)
                preview.append(
                    {
                        "lead_id": lead_id,
                        "draft_id": draft_id,
                        "draft_sha256": draft.sha256,
                        "recipient_display": draft.recipient,
                        "subject": draft.subject,
                        "attachment_digests": list(draft.attachment_digests),
                        "body_sha256": hashlib.sha256(draft.body.encode()).hexdigest(),
                    }
                )
            selected_channels = {draft.channel for draft in drafts}
            if channel == "auto":
                if len(selected_channels) != 1:
                    raise ValueError(
                        "automatic authorization requires drafts with one shared channel"
                    )
                selected_channel = next(iter(selected_channels))
            else:
                selected_channel = channel
            authorization = build_outreach_authorization(
                tuple(drafts), sender=sender, channel=selected_channel
            )
            data_dir = database_path.parent
            path = write_outreach_authorization(
                data_dir
                / "outreach-authorizations"
                / f"{authorization.authorization_id}.json",
                authorization,
            )
            store.record_authorization(authorization, artifact_path=path)
        console.print_json(
            data={
                "authorization_id": authorization.authorization_id,
                "authorization_path": str(path),
                "authorization_sha256": authorization.sha256,
                "sender": authorization.sender,
                "channel": authorization.channel,
                "expires_at": authorization.expires_at,
                "items": preview,
                "status": "authorized_not_sent",
                "external_contact_attempted": False,
            }
        )
    except Exception as exc:
        console.print(
            f"[red]Outreach authorization failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc


@opportunities_app.command("send")
def queue_opportunity_send(
    authorization: Path = typer.Option(..., "--authorization"),
) -> None:
    """Consume one exact grant and create a send handoff; this command has no provider adapter."""
    from applypilot.observability.events import EventJournal
    from applypilot.opportunities.send_handoff import (
        load_outreach_authorization,
        queue_send_handoff,
    )
    from applypilot.opportunities.store import OpportunityStore

    database_path, _ = _opportunity_data_paths()
    try:
        grant = load_outreach_authorization(authorization)
        run_dir = database_path.parent / "outreach-send-runs" / grant.authorization_id
        journal = EventJournal(
            run_dir / "events.ndjson", run_id=grant.authorization_id
        )
        with OpportunityStore(database_path) as store:
            request_path = queue_send_handoff(
                authorization_path=authorization,
                store=store,
                run_dir=run_dir,
                journal=journal,
            )
        console.print_json(
            data={
                "authorization_id": grant.authorization_id,
                "status": "send_handoff_queued",
                "request_path": str(request_path),
                "provider_call_performed": False,
                "next_action": "service_exact_authenticated_outreach_handoff",
            }
        )
    except Exception as exc:
        console.print(
            f"[red]Outreach send queue failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc


@opportunities_app.command("send-import")
def import_opportunity_send_result(
    authorization_id: str = typer.Argument(...),
    response: Optional[Path] = typer.Option(None, "--response"),
) -> None:
    """Import and record per-item provider outcomes without inferring delivery or reply."""
    from applypilot.autonomy.handoff import import_response_artifact
    from applypilot.observability.events import EventJournal
    from applypilot.opportunities.send_handoff import consume_send_response
    from applypilot.opportunities.store import OpportunityStore

    database_path, _ = _opportunity_data_paths()
    run_dir = database_path.parent / "outreach-send-runs" / authorization_id
    request_path = run_dir / "handoff" / f"{authorization_id}.request.json"
    try:
        if response is not None:
            import_response_artifact(request_path=request_path, input_path=response)
        journal = EventJournal(run_dir / "events.ndjson", run_id=authorization_id)
        with OpportunityStore(database_path) as store:
            result = consume_send_response(
                request_path=request_path,
                store=store,
                journal=journal,
            )
        console.print_json(data=result)
    except Exception as exc:
        console.print(
            f"[red]Outreach send import failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc


@app.command("aggregate")
def aggregate_jobs(
    query: str = typer.Option(..., "--query", "-q", help="Exact aggregation objective."),
    term: list[str] = typer.Option(
        ...,
        "--term",
        help="Bounded provider search term; repeat to add terms.",
    ),
    location: Optional[list[str]] = typer.Option(
        None,
        "--location",
        help="Bounded location; repeat to add locations.",
    ),
    source: Optional[list[str]] = typer.Option(
        None,
        "--source",
        help="Deterministic source: cache, direct_ats, workday, or smart_extract.",
    ),
    enrich: Optional[list[str]] = typer.Option(
        None,
        "--enrich",
        help="Non-blocking enrichment lane; currently jobspy.",
    ),
    portal: Optional[list[str]] = typer.Option(
        None,
        "--portal",
        help="Serialized model-piloted browser mission: handshake or runway.",
    ),
    import_path: Optional[Path] = typer.Option(
        None,
        "--import",
        help="Explicit Handshake or Runway JSONL import.",
    ),
    mode: str = typer.Option("quick", "--mode", help="quick or deep."),
    watch: bool = typer.Option(True, "--watch/--no-watch", help="Show live source telemetry."),
) -> None:
    """Publish a fast immutable job snapshot and queue optional enrichment."""
    import asyncio

    from applypilot.aggregation.models import AggregationRequest
    from applypilot.aggregation.orchestrator import Aggregator
    from applypilot.aggregation.store import AggregationStore
    from applypilot.aggregation.telemetry import run_with_live
    from applypilot.observability.events import EventJournal

    store: Optional[AggregationStore] = None
    try:
        source_names = list(source or ["cache", "direct_ats", "workday", "smart_extract"])
        if len(set(source_names)) != len(source_names):
            raise ValueError("aggregation sources must be unique")
        enrichment = list(enrich or [])
        if any(name != "jobspy" for name in enrichment) or len(set(enrichment)) != len(enrichment):
            raise ValueError("--enrich accepts jobspy once")
        portals = list(portal or [])
        if not set(portals) <= {"handshake", "runway"} or len(set(portals)) != len(portals):
            raise ValueError("--portal accepts handshake and runway at most once each")
        if mode not in {"quick", "deep"}:
            raise ValueError("aggregation mode must be quick or deep")

        database_path, aggregation_runs, cache_db_path = _aggregation_data_paths()
        adapters = _build_aggregation_sources(
            source_names=source_names,
            import_path=import_path,
            cache_db_path=cache_db_path,
        )
        if not adapters:
            raise ValueError("at least one deterministic source or import is required")
        now = datetime.now(timezone.utc)
        run_id = f"agg-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        run_directory = aggregation_runs / run_id
        run_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        event_path = run_directory / "events.ndjson"
        journal = EventJournal(event_path, run_id=run_id)
        store = AggregationStore(database_path, run_dir=aggregation_runs)
        request = AggregationRequest(
            query=query,
            query_terms=tuple(term),
            locations=tuple(location or ()),
            mode=mode,
            global_deadline_seconds=15.0 if mode == "quick" else 90.0,
            per_source_timeout_seconds=10.0 if mode == "quick" else 30.0,
        )
        pending = tuple(sorted(set(enrichment + portals)))
        aggregator = Aggregator(
            store=store,
            journal=journal,
            sources=adapters,
            pending_enrichment=pending,
        )

        async def execute() -> dict:
            if watch:
                return await run_with_live(
                    aggregator=aggregator,
                    run_id=run_id,
                    request=request,
                    journal=journal,
                    console=Console(stderr=True),
                )
            return await aggregator.run(run_id, request)

        snapshot = asyncio.run(execute())
        snapshot_path, _ = store.get_snapshot(run_id, int(snapshot["revision"]))
        enrichment_state: dict[str, str] = {}
        browser_handoff_request = ""
        if portals:
            from applypilot.aggregation.portal_handoff import initialize_portal_queue

            active_request = initialize_portal_queue(
                store=store,
                journal=journal,
                run_dir=run_directory,
                run_id=run_id,
                aggregation_request=request,
                portals=tuple(portals),
            )
            browser_handoff_request = str(active_request or "")
            for row in store.portal_missions(run_id):
                enrichment_state[str(row["portal"])] = str(row["status"])
        if "jobspy" in pending:
            try:
                _launch_jobspy_enrichment(
                    run_id=run_id,
                    data_dir=database_path.parent,
                    run_directory=run_directory,
                )
                enrichment_state["jobspy"] = "started"
                journal.emit(
                    component="aggregation",
                    phase="enrichment",
                    status="started",
                    source="jobspy",
                )
            except Exception as exc:
                enrichment_state["jobspy"] = "launch_failed"
                journal.emit(
                    component="aggregation",
                    phase="enrichment",
                    status="launch_failed",
                    source="jobspy",
                    detail={"error_class": type(exc).__name__},
                )
        console.print_json(
            data={
                "run_id": run_id,
                "status": snapshot["status"],
                "candidate_count": snapshot["candidate_count"],
                "snapshot_revision": snapshot["revision"],
                "snapshot_path": str(snapshot_path),
                "events_path": str(event_path),
                "pending_enrichment": snapshot["pending_enrichment"],
                "enrichment_state": enrichment_state,
                "browser_handoff_request": browser_handoff_request,
            }
        )
    except Exception as exc:
        console.print(f"[red]Aggregation failed:[/red] {type(exc).__name__}: {str(exc)[:240]}")
        raise typer.Exit(code=1) from exc
    finally:
        if store is not None:
            store.close()


@app.command("aggregate-status")
def aggregate_status(
    run_id: str = typer.Option(..., "--run-id", help="Exact aggregation run ID."),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable state."),
    watch: bool = typer.Option(False, "--watch", help="Include the current telemetry cursor."),
) -> None:
    """Read persisted aggregation state without restarting any source."""
    from applypilot.aggregation.store import AggregationStore

    store: Optional[AggregationStore] = None
    try:
        database_path, aggregation_runs, _ = _aggregation_data_paths()
        store = AggregationStore(database_path, run_dir=aggregation_runs)
        revision = store.latest_revision(run_id)
        if revision:
            _, snapshot = store.get_snapshot(run_id, revision)
        else:
            snapshot = store.snapshot(run_id)
            snapshot["revision"] = 0
        missions = store.portal_missions(run_id)
        event_path = aggregation_runs / run_id / "events.ndjson"
        events = []
        if event_path.is_file() and not event_path.is_symlink():
            from applypilot.observability.events import EventJournal

            events = EventJournal(event_path, run_id=run_id).read()
        fast_elapsed_ms = next(
            (
                event.elapsed_ms
                for event in events
                if event.phase == "snapshot"
                and event.status == "published"
                and event.counts.get("revision") == 1
            ),
            0,
        )
        fast_snapshot: dict[str, Any] = {"revision": 0, "status": "pending", "elapsed_ms": 0}
        if revision:
            _, first = store.get_snapshot(run_id, 1)
            fast_snapshot = {
                "revision": 1,
                "status": "ready",
                "elapsed_ms": fast_elapsed_ms,
                "candidate_count": int(first["candidate_count"]),
                "sha256": str(first["sha256"]),
            }
        grouped_sources: dict[str, list[str]] = {}
        for row in snapshot.get("sources") or []:
            grouped_sources.setdefault(str(row["source"]), []).append(str(row["status"]))
        latest_by_source = {event.source: event.status for event in events if event.source}
        for pending in snapshot.get("pending_enrichment") or []:
            source_name = "jobspy" if pending == "jobspy" else str(pending)
            grouped_sources.setdefault(source_name, [])
            if source_name in latest_by_source:
                grouped_sources[source_name].append(latest_by_source[source_name])

        def source_family_state(statuses: list[str]) -> str:
            if any(status in {"started", "running", "heartbeat"} for status in statuses):
                return "running"
            for state in ("failed", "timed_out", "cancelled", "partial", "complete"):
                if state in statuses:
                    return state
            return "pending"

        source_projection = {
            source_name: source_family_state(statuses)
            for source_name, statuses in sorted(grouped_sources.items())
        }
        active_portals = [row for row in missions if row["status"] == "awaiting_response"]
        checkpoint: dict[str, Any] = {}
        if active_portals:
            request_path = Path(str(active_portals[0]["request_path"]))
            checkpoint_path = request_path.with_name(
                request_path.name.replace(".request.json", ".checkpoint.json")
            )
            if checkpoint_path.is_file() and not checkpoint_path.is_symlink():
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint_age = 0
        if checkpoint.get("observed_at"):
            observed = datetime.fromisoformat(
                str(checkpoint["observed_at"]).replace("Z", "+00:00")
            )
            checkpoint_age = max(
                0,
                int((datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()),
            )
        snapshot["browser_queue"] = {
            "active": str(active_portals[0]["portal"]) if active_portals else "",
            "queued": [str(row["portal"]) for row in missions if row["status"] == "queued"],
            "state": str(checkpoint.get("state") or (active_portals[0]["status"] if active_portals else "idle")),
            "navigation_count": int(checkpoint.get("navigation_count") or 0),
            "result_count": int(checkpoint.get("result_count") or 0),
            "checkpoint_sequence": int(checkpoint.get("sequence") or 0),
            "last_checkpoint_age_seconds": checkpoint_age,
        }
        snapshot["fast_snapshot"] = fast_snapshot
        snapshot["latest_snapshot"] = {
            "revision": int(snapshot["revision"]),
            "candidate_count": int(snapshot["candidate_count"]),
            "advanceable_count": int(snapshot.get("advanceable_count") or 0),
            "sha256": str(snapshot.get("sha256") or ""),
        }
        snapshot["source_states"] = source_projection
        opportunity_database = database_path.parent / "opportunities.sqlite3"
        opportunity_counts = {
            "observed": 0,
            "verified": 0,
            "draft_ready": 0,
            "sent": 0,
            "posted_job_leads": 0,
            "general_interest_application_leads": 0,
            "general_interest_application_completed": 0,
            "speculative_outreach_leads": 0,
            "speculative_outreach_completed": 0,
        }
        if opportunity_database.is_file() and not opportunity_database.is_symlink():
            from applypilot.opportunities.store import OpportunityStore

            with OpportunityStore(opportunity_database) as opportunity_store:
                available_counts = opportunity_store.summary_counts()
            opportunity_counts = {
                key: int(available_counts.get(key) or 0) for key in opportunity_counts
            }
        snapshot["opportunities"] = opportunity_counts
        snapshot["event_sequence"] = events[-1].sequence if events else 0
        snapshot["watch_requested"] = watch
        if as_json:
            console.print_json(data=snapshot)
            return
        table = Table(title=f"Aggregation {run_id}")
        table.add_column("Source")
        table.add_column("State")
        table.add_column("Observed", justify="right")
        for row in snapshot.get("sources") or []:
            table.add_row(
                str(row["source"]),
                str(row["status"]),
                str(row.get("observed_count") or 0),
            )
        console.print(table)
        console.print(
            f"Revision {snapshot['revision']} · {snapshot['candidate_count']} candidates · "
            f"{snapshot['observation_count']} observations · {snapshot['status']}"
        )
    except Exception as exc:
        console.print(f"[red]Aggregation status failed:[/red] {type(exc).__name__}: {str(exc)[:240]}")
        raise typer.Exit(code=1) from exc
    finally:
        if store is not None:
            store.close()


@app.command("aggregate-portal-checkpoint")
def aggregate_portal_checkpoint(
    run_id: str = typer.Option(..., "--run-id", help="Exact aggregation run ID."),
    portal: str = typer.Option(..., "--portal", help="handshake or runway."),
    state: str = typer.Option(..., "--state", help="Safe browser mission lifecycle state."),
    sequence: int = typer.Option(..., "--sequence", min=1),
    navigation_count: int = typer.Option(0, "--navigation-count", min=0),
    result_count: int = typer.Option(0, "--result-count", min=0),
    elapsed_seconds: int = typer.Option(0, "--elapsed-seconds", min=0),
    safe_hostname: str = typer.Option("", "--safe-hostname"),
) -> None:
    """Persist one privacy-bounded liveness checkpoint from the model pilot."""
    from applypilot.aggregation.portal_handoff import write_portal_checkpoint
    from applypilot.aggregation.store import AggregationStore
    from applypilot.observability.events import EventJournal

    store: Optional[AggregationStore] = None
    try:
        database_path, aggregation_runs, _ = _aggregation_data_paths()
        run_directory = (aggregation_runs / run_id).resolve(strict=True)
        store = AggregationStore(database_path, run_dir=aggregation_runs)
        active = [
            row
            for row in store.portal_missions(run_id)
            if row["status"] == "awaiting_response" and row["portal"] == portal
        ]
        if len(active) != 1:
            raise ValueError("portal is not the active authenticated-browser mission")
        checkpoint_path = write_portal_checkpoint(
            request_path=Path(str(active[0]["request_path"])),
            state=state,
            sequence=sequence,
            navigation_count=navigation_count,
            result_count=result_count,
            elapsed_seconds=elapsed_seconds,
            safe_hostname=safe_hostname,
        )
        EventJournal(run_directory / "events.ndjson", run_id=run_id).emit(
            component="browser_mission",
            phase="mission",
            status=state,
            source=portal,
            counts={
                "navigations": navigation_count,
                "observed": result_count,
                "elapsed_seconds": elapsed_seconds,
            },
            detail={"safe_hostname": safe_hostname},
        )
        console.print_json(
            data={
                "run_id": run_id,
                "portal": portal,
                "state": state,
                "sequence": sequence,
                "checkpoint_path": str(checkpoint_path),
            }
        )
    except Exception as exc:
        console.print(
            f"[red]Portal checkpoint failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc
    finally:
        if store is not None:
            store.close()


@app.command("aggregate-enrich-jobspy", hidden=True)
def aggregate_enrich_jobspy(
    run_id: str = typer.Option(..., "--run-id", help="Exact aggregation run ID."),
) -> None:
    """Run a previously declared JobSpy enrichment in a detached local manager."""
    import asyncio

    from applypilot.aggregation.sources.jobspy import enrich_jobspy_run
    from applypilot.aggregation.store import AggregationStore
    from applypilot.observability.events import EventJournal

    store: Optional[AggregationStore] = None
    try:
        database_path, aggregation_runs, _ = _aggregation_data_paths()
        run_directory = (aggregation_runs / run_id).resolve(strict=True)
        if run_directory.parent != aggregation_runs.resolve() or run_directory.is_symlink():
            raise ValueError("aggregation run directory is invalid")
        store = AggregationStore(database_path, run_dir=aggregation_runs)
        revision = store.latest_revision(run_id)
        if revision < 1:
            raise ValueError("JobSpy enrichment requires a published fast snapshot")
        _, latest = store.get_snapshot(run_id, revision)
        if "jobspy" not in (latest.get("pending_enrichment") or []):
            raise ValueError("JobSpy enrichment is not pending for this run")
        journal = EventJournal(run_directory / "events.ndjson", run_id=run_id)
        snapshot = asyncio.run(
            enrich_jobspy_run(
                store=store,
                journal=journal,
                run_id=run_id,
                request=store.get_request(run_id),
                work_dir=run_directory / "jobspy",
            )
        )
        console.print_json(
            data={
                "run_id": run_id,
                "status": snapshot["status"],
                "snapshot_revision": snapshot["revision"],
                "candidate_count": snapshot["candidate_count"],
            }
        )
    except Exception as exc:
        console.print(
            f"[red]JobSpy enrichment failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc
    finally:
        if store is not None:
            store.close()


@app.command("aggregate-portal-import")
def aggregate_portal_import(
    run_id: str = typer.Option(..., "--run-id", help="Exact aggregation run ID."),
    response: Path = typer.Option(..., "--response", help="Validated browser mission response JSON."),
) -> None:
    """Import one active Handshake or Runway response and publish a later revision."""
    from applypilot.aggregation.portal_handoff import consume_portal_response
    from applypilot.aggregation.store import AggregationStore
    from applypilot.autonomy.handoff import import_response_artifact
    from applypilot.observability.events import EventJournal

    store: Optional[AggregationStore] = None
    try:
        database_path, aggregation_runs, _ = _aggregation_data_paths()
        run_directory = (aggregation_runs / run_id).resolve(strict=True)
        if run_directory.parent != aggregation_runs.resolve() or run_directory.is_symlink():
            raise ValueError("aggregation run directory is invalid")
        store = AggregationStore(database_path, run_dir=aggregation_runs)
        active = [
            row
            for row in store.portal_missions(run_id)
            if row["status"] in {"awaiting_response", "response_ready"}
        ]
        if len(active) != 1:
            raise ValueError("aggregation run does not have one active portal mission")
        request_path = Path(str(active[0]["request_path"])).resolve(strict=True)
        import_response_artifact(request_path=request_path, input_path=response.resolve(strict=True))
        snapshot = consume_portal_response(
            store=store,
            journal=EventJournal(run_directory / "events.ndjson", run_id=run_id),
            run_dir=run_directory,
            run_id=run_id,
            request_path=request_path,
        )
        next_active = [
            row for row in store.portal_missions(run_id) if row["status"] == "awaiting_response"
        ]
        console.print_json(
            data={
                "run_id": run_id,
                "status": snapshot["status"],
                "snapshot_revision": snapshot["revision"],
                "candidate_count": snapshot["candidate_count"],
                "pending_enrichment": snapshot["pending_enrichment"],
                "next_request": (
                    str(next_active[0]["request_path"]) if next_active else ""
                ),
            }
        )
    except Exception as exc:
        console.print(
            f"[red]Portal import failed:[/red] {type(exc).__name__}: {str(exc)[:240]}"
        )
        raise typer.Exit(code=1) from exc
    finally:
        if store is not None:
            store.close()


@app.command("prepare")
def prepare_workflow(
    query: Optional[str] = typer.Option(
        None,
        "--query",
        "-q",
        help="Exact role-family, level, and location objective for a new run.",
    ),
    run_dir: Optional[Path] = typer.Option(
        None,
        "--run-dir",
        help="Resume an existing canonical workflow run.",
    ),
    response: Optional[Path] = typer.Option(
        None,
        "--response",
        help="Browser/model response for the run's one active handoff request.",
    ),
    aggregation_snapshot: Optional[str] = typer.Option(
        None,
        "--aggregation-snapshot",
        help="Exact aggregation selector RUN_ID@REVISION for a new run.",
    ),
    legacy_web_discovery: bool = typer.Option(
        False,
        "--legacy-web-discovery",
        help="Compatibility-only ChatGPT Web discovery instead of an aggregation snapshot.",
    ),
    out: Optional[Path] = typer.Option(None, "--out", help="Autonomy transport directory."),
) -> None:
    """Discover, verify, rank, and prepare one resumable reviewed shortlist."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.autonomy.handoff import import_response_artifact
    from applypilot.autonomy.runner import advance_artifact_run, prepare_run
    from applypilot.workflow import WorkflowStore

    workflow_path = config.APP_DIR / "workflow.sqlite3"
    try:
        if run_dir is None:
            if not query:
                raise ValueError("--query is required when creating a workflow run")
            if response is not None:
                raise ValueError("--response requires --run-dir")
            if bool(aggregation_snapshot) == legacy_web_discovery:
                raise ValueError(
                    "use exactly one of --aggregation-snapshot or --legacy-web-discovery"
                )
            snapshot_path: Path | None = None
            snapshot_revision: int | None = None
            snapshot_sha256 = ""
            if aggregation_snapshot is not None:
                if not re.fullmatch(
                    r"[a-zA-Z0-9_.:-]{1,120}@[1-9][0-9]*", aggregation_snapshot
                ):
                    raise ValueError(
                        "--aggregation-snapshot must be an exact RUN_ID@REVISION selector"
                    )
                aggregation_run_id, revision_text = aggregation_snapshot.rsplit("@", 1)
                snapshot_revision = int(revision_text)
                from applypilot.aggregation.store import AggregationStore

                aggregation_db, aggregation_runs, _ = _aggregation_data_paths()
                aggregation_store = AggregationStore(
                    aggregation_db, run_dir=aggregation_runs
                )
                try:
                    snapshot_path, snapshot_payload = aggregation_store.verify_snapshot_chain(
                        aggregation_run_id, snapshot_revision
                    )
                    snapshot_sha256 = str(snapshot_payload["sha256"])
                finally:
                    aggregation_store.close()
            paths = prepare_run(
                query=query,
                output_dir=out or config.APP_DIR / "autonomy-runs",
                aggregation_snapshot_path=snapshot_path,
                aggregation_snapshot_revision=snapshot_revision,
                aggregation_snapshot_sha256=snapshot_sha256,
                legacy_web_discovery=legacy_web_discovery,
            )
            run_dir = Path(paths["run_dir"])
            with WorkflowStore(workflow_path) as store:
                run_id = store.register_run(run_dir)
                status = store.status(run_id)
            console.print_json(
                data={
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "status": status["status"],
                    "next_action": (
                        "service_browser_handoff"
                        if paths.get("request")
                        else "advance_snapshot_run"
                    ),
                    "request_path": str(paths.get("request") or ""),
                }
            )
            return

        if aggregation_snapshot is not None or legacy_web_discovery:
            raise ValueError(
                "--aggregation-snapshot and --legacy-web-discovery apply only to new runs"
            )

        run_dir = run_dir.resolve()
        if response is not None:
            request_path = _one_active_handoff_request(run_dir)
            import_response_artifact(request_path=request_path, input_path=response)

        fact_ledger = json.loads(
            (run_dir / "fact_ledger.json").read_text(encoding="utf-8")
        )
        result = advance_artifact_run(
            run_dir=run_dir,
            approved_fact_digest=str(fact_ledger["digest"]),
        )
        with WorkflowStore(workflow_path) as store:
            status = store.sync_batch_result(run_dir=run_dir, result=result)
            fact_digest, profile = _workflow_fact_snapshot(
                store,
                status["run_id"],
                require_submission_facts=False,
            )
            status = store.reconcile_candidate_eligibility(
                run_id=status["run_id"],
                profile=profile,
                fact_digest=fact_digest,
            )
        off_posting_route = (
            _ensure_off_posting_research(parent_run_id=status["run_id"])
            if result.get("status") == "no_eligible_verified_roles"
            else None
        )
        console.print_json(
            data={
                "run_id": status["run_id"],
                "status": status["status"],
                "candidate_counts": status["candidate_counts"],
                "shortlist": status["shortlist"],
                "pending_requests": result.get("pending_requests") or [],
                "off_posting_route": off_posting_route,
            }
        )
    except Exception as exc:
        console.print(f"[red]Prepare failed:[/red] {type(exc).__name__}: {str(exc)[:240]}")
        raise typer.Exit(code=1) from exc


@app.command("workflow-status")
def workflow_status(
    run_id: str = typer.Option(..., "--run-id", help="Canonical workflow run id."),
) -> None:
    """Show the canonical shortlist and candidate-state counts."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.workflow import WorkflowStore

    try:
        with WorkflowStore(config.APP_DIR / "workflow.sqlite3") as store:
            console.print_json(data=store.status(run_id))
    except Exception as exc:
        console.print(f"[red]Workflow status failed:[/red] {type(exc).__name__}: {str(exc)[:240]}")
        raise typer.Exit(code=1) from exc


@app.command("campaign-ledger-create")
def campaign_ledger_create(
    campaign_id: str = typer.Option(..., "--campaign-id", help="Durable canonical campaign identifier."),
    summer_target: int = typer.Option(80, "--summer-target", min=0),
    fall_target: int = typer.Option(20, "--fall-target", min=0),
) -> None:
    """Create one canonical season-bound campaign ledger."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.workflow import WorkflowStore

    try:
        with WorkflowStore(config.APP_DIR / "workflow.sqlite3") as store:
            console.print_json(
                data=store.create_campaign(
                    campaign_id=campaign_id,
                    summer_target=summer_target,
                    fall_target=fall_target,
                )
            )
    except Exception as exc:
        console.print(f"[red]Campaign ledger creation failed:[/red] {type(exc).__name__}: {str(exc)[:240]}")
        raise typer.Exit(code=1) from exc


@app.command("campaign-ledger-status")
def campaign_ledger_status(
    campaign_id: str = typer.Option(..., "--campaign-id", help="Canonical campaign identifier."),
) -> None:
    """Print durable candidate, confirmation, and season-count evidence."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.workflow import WorkflowStore

    try:
        with WorkflowStore(config.APP_DIR / "workflow.sqlite3") as store:
            console.print_json(data=store.campaign_status(campaign_id))
    except Exception as exc:
        console.print(f"[red]Campaign ledger status failed:[/red] {type(exc).__name__}: {str(exc)[:240]}")
        raise typer.Exit(code=1) from exc


@app.command("campaign-ledger-replacement")
def campaign_ledger_replacement(
    campaign_id: str = typer.Option(..., "--campaign-id", help="Canonical campaign identifier."),
    run_id: str = typer.Option(..., "--run-id", help="Workflow run id for the replacement."),
    candidate: str = typer.Option(..., "--candidate", help="Workflow candidate id for the replacement."),
    season: str = typer.Option(..., "--season", help="Campaign season: summer_2027 or fall_2026."),
    category: str = typer.Option(..., "--category", help="blocked, duplicate, failed, or unqualified."),
    reason: str = typer.Option(..., "--reason", help="Verified non-submission reason."),
) -> None:
    """Record a non-submission that must be replaced without counting it as applied."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.workflow import WorkflowStore

    try:
        with WorkflowStore(config.APP_DIR / "workflow.sqlite3") as store:
            console.print_json(
                data=store.record_campaign_replacement(
                    campaign_id=campaign_id,
                    run_id=run_id,
                    candidate_id=candidate,
                    season=season,
                    category=category,
                    reason=reason,
                )
            )
    except Exception as exc:
        console.print(f"[red]Campaign replacement recording failed:[/red] {type(exc).__name__}: {str(exc)[:240]}")
        raise typer.Exit(code=1) from exc


@app.command("dry-run")
def dry_run_workflow(
    run_id: str = typer.Option(..., "--run-id", help="Canonical workflow run id."),
    candidate: Optional[list[str]] = typer.Option(
        None,
        "--candidate",
        help="Exact candidate id to fill and review. Repeat for up to five.",
    ),
    request: Optional[Path] = typer.Option(
        None,
        "--request",
        help="Existing browser dry-run request to import.",
    ),
    response: Optional[Path] = typer.Option(
        None,
        "--response",
        help="Browser dry-run response to validate and import.",
    ),
) -> None:
    """Create or import visible-Chrome form dry-runs; never submit."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.workflow import WorkflowStore

    try:
        with WorkflowStore(config.APP_DIR / "workflow.sqlite3") as store:
            if request is not None or response is not None:
                if request is None or response is None:
                    raise ValueError("--request and --response must be supplied together")
                result = store.import_browser_response(
                    request_path=request,
                    input_path=response,
                )
                console.print_json(data=result)
                return
            form_fact_digest, profile = _confirmed_form_fact_snapshot(store, run_id)
            store.reconcile_candidate_eligibility(
                run_id=run_id,
                profile=profile,
                fact_digest=form_fact_digest,
            )
            candidate_ids = list(candidate or [])
            if not candidate_ids:
                candidate_ids = [
                    item["candidate_id"]
                    for item in store.shortlist(run_id, limit=5)
                    if item["state"] == "materials_ready"
                ][:3]
            if not candidate_ids:
                raise ValueError("no material-ready candidates are available for dry-run")
            paths = store.create_dry_run_requests(
                run_id=run_id,
                candidate_ids=candidate_ids,
                form_fact_digest=form_fact_digest,
            )
            console.print_json(
                data={
                    "run_id": run_id,
                    "status": "awaiting_visible_chrome_dry_run",
                    "request_paths": [str(path) for path in paths],
                }
            )
    except Exception as exc:
        console.print(f"[red]Dry-run failed:[/red] {type(exc).__name__}: {str(exc)[:240]}")
        raise typer.Exit(code=1) from exc


@app.command("approve")
def approve_workflow_batch(
    run_id: str = typer.Option(..., "--run-id", help="Canonical workflow run id."),
    candidate: list[str] = typer.Option(
        ...,
        "--candidate",
        help="Exact dry-run-reviewed candidate id. Repeat for the approved batch.",
    ),
    max_submissions: int = typer.Option(
        3,
        "--max-submissions",
        min=1,
        max=3,
        help="Maximum final submissions permitted by this one approval.",
    ),
    valid_hours: int = typer.Option(24, "--valid-hours", min=1, max=72),
    campaign_id: str = typer.Option("", "--campaign-id", help="Optional canonical campaign ledger."),
    season: str = typer.Option("", "--season", help="Campaign season: summer_2027 or fall_2026."),
) -> None:
    """Authorize one exact, evidence-bound batch after candidate review."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.workflow import WorkflowStore

    try:
        with WorkflowStore(config.APP_DIR / "workflow.sqlite3") as store:
            form_fact_digest, _profile = _confirmed_form_fact_snapshot(store, run_id)
            approval = store.create_approval(
                run_id=run_id,
                candidate_ids=candidate,
                form_fact_digest=form_fact_digest,
                max_submissions=max_submissions,
                valid_hours=valid_hours,
                campaign_id=campaign_id,
                season=season,
            )
            approval["candidates"] = [
                item
                for item in store.shortlist(run_id, limit=20)
                if item["candidate_id"] in set(candidate)
            ]
            console.print_json(data=approval)
    except Exception as exc:
        console.print(f"[red]Approval failed:[/red] {type(exc).__name__}: {str(exc)[:240]}")
        raise typer.Exit(code=1) from exc


@app.command("execute")
def execute_workflow_batch(
    approval_id: str = typer.Option(..., "--approval-id", help="Exact active batch approval id."),
    request: Optional[Path] = typer.Option(
        None,
        "--request",
        help="Existing submission request whose browser response is being imported.",
    ),
    response: Optional[Path] = typer.Option(
        None,
        "--response",
        help="Visible-Chrome submission result to validate and import.",
    ),
) -> None:
    """Resume an approved batch one candidate at a time with duplicate prevention."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.workflow import WorkflowStore

    try:
        with WorkflowStore(config.APP_DIR / "workflow.sqlite3") as store:
            imported = None
            if request is not None or response is not None:
                if request is None or response is None:
                    raise ValueError("--request and --response must be supplied together")
                imported = store.import_browser_response(
                    request_path=request,
                    input_path=response,
                )
            approval = store.approval_status(approval_id)
            form_fact_digest, _profile = _confirmed_form_fact_snapshot(
                store,
                approval["run_id"],
            )
            next_request = store.create_submission_request(
                approval_id=approval_id,
                form_fact_digest=form_fact_digest,
            )
            console.print_json(
                data={
                    "approval_id": approval_id,
                    "imported": imported,
                    "status": "awaiting_visible_chrome_submission" if next_request else "batch_stopped",
                    "request_path": str(next_request) if next_request else None,
                }
            )
    except Exception as exc:
        console.print(f"[red]Execute failed:[/red] {type(exc).__name__}: {str(exc)[:240]}")
        raise typer.Exit(code=1) from exc


def _one_active_handoff_request(run_dir: Path) -> Path:
    """Resolve exactly one unanswered autonomy transport request."""
    handoff_dir = run_dir / "handoff"
    active: list[Path] = []
    for request_path in sorted(handoff_dir.glob("*.request.json")):
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        response_path = (run_dir / str(payload.get("response_path") or "")).resolve()
        receipt_path = response_path.with_name(
            response_path.name.replace(".response.json", ".receipt.json")
        )
        if not response_path.exists() and not receipt_path.exists():
            active.append(request_path)
    if len(active) != 1:
        raise ValueError(f"expected one active handoff request, found {len(active)}")
    return active[0]


def _confirmed_form_fact_snapshot(
    store: WorkflowStore,
    run_id: str,
) -> tuple[str, dict[str, Any]]:
    """Bind form work to a monotonic extension of the reviewed run facts."""
    return _workflow_fact_snapshot(
        store,
        run_id,
        require_submission_facts=True,
    )


def _workflow_fact_snapshot(
    store: WorkflowStore,
    run_id: str,
    *,
    require_submission_facts: bool,
) -> tuple[str, dict[str, Any]]:
    """Persist the current monotonic fact view, optionally requiring form facts."""
    from applypilot import config
    from applypilot.autonomy.facts import (
        REQUIRED_AUTONOMY_FACT_IDS,
        build_monotonic_fact_snapshot,
        fact_ledger_from_dict,
        require_confirmed_facts,
    )

    profile = config.load_profile()
    resume_text = config.RESUME_PATH.read_text(encoding="utf-8")
    run_dir = store.source_run_dir(run_id)
    base = fact_ledger_from_dict(
        json.loads((run_dir / "fact_ledger.json").read_text(encoding="utf-8"))
    )
    ledger = build_monotonic_fact_snapshot(
        base,
        profile,
        resume_text=resume_text,
    )
    blockers = (
        require_confirmed_facts(ledger, REQUIRED_AUTONOMY_FACT_IDS)
        if require_submission_facts
        else []
    )
    if blockers:
        raise ValueError(
            "dry-run and approval require confirmed phone, work authorization, sponsorship, "
            "and earliest start date: " + ",".join(blockers)
        )
    store.persist_fact_snapshot(run_id, ledger.to_dict())
    return ledger.digest, profile


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max application forms to process."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override agent executor model."),
    agent_backend: Optional[str] = typer.Option(
        None,
        "--agent-backend",
        help="Deterministic controller backend. Only codex is supported; defaults to codex.",
    ),
    supervisor_model: Optional[str] = typer.Option(
        None,
        "--supervisor-model",
        help="Supervisor model label written into the deterministic harness contract.",
    ),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--submit",
        help="Dry-run by default. --submit is an explicit irreversible-action authorization.",
    ),
    allow_account_creation: bool = typer.Option(
        False,
        "--allow-account-creation",
        help="Explicitly allow creation of a job-site account during this invocation.",
    ),
    approved_fact_digest: Optional[str] = typer.Option(
        None,
        "--approved-fact-digest",
        help="Required for --submit; exact digest from a reviewed autonomy fact ledger.",
    ),
    corrections: Optional[Path] = typer.Option(
        None,
        "--corrections",
        help="Fact corrections file used to produce the approved digest.",
    ),
    authorization_manifest: Optional[Path] = typer.Option(
        None,
        "--authorization-manifest",
        help="Required for --submit; one-time manifest produced by the matching dry-run.",
    ),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch deterministic auto-apply; dry-run unless --submit is explicit."""
    _bootstrap()

    from applypilot.config import PROFILE_PATH as _profile_path, get_chrome_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/agent needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---
    if agent_backend and agent_backend not in {"claude", "codex"}:
        console.print("[red]Invalid --agent-backend.[/red] Choose: claude, codex")
        raise typer.Exit(code=1)

    from applypilot.apply.harness import load_settings as load_harness_settings
    harness_settings = load_harness_settings(
        agent_backend=agent_backend,
        executor_model=model,
        supervisor_model=supervisor_model,
        allow_account_creation=allow_account_creation,
    )
    if harness_settings.agent_backend != "codex" or not harness_settings.deterministic_controller:
        console.print(
            "[red]Legacy model-driven apply controllers are disabled.[/red]\n"
            "Use the deterministic Codex controller."
        )
        raise typer.Exit(code=1)
    if not dry_run and not approved_fact_digest:
        console.print(
            "[red]--submit requires --approved-fact-digest from a reviewed autonomy plan.[/red]"
        )
        raise typer.Exit(code=1)
    if not dry_run:
        if continuous or workers != 1 or (limit is not None and limit != 1) or not url:
            console.print(
                "[red]--submit is limited to one exact --url, one worker, and --limit 1.[/red]"
            )
            raise typer.Exit(code=1)
        if authorization_manifest is None or not authorization_manifest.exists():
            console.print(
                "[red]--submit requires an existing --authorization-manifest from the matching dry-run.[/red]"
            )
            raise typer.Exit(code=1)

    if not gen:
        try:
            get_chrome_path()
        except FileNotFoundError as exc:
            console.print("[red]Chrome/Chromium is required for auto-apply.[/red]")
            raise typer.Exit(code=1) from exc

    from applypilot.apply.field_resolver import find_codex_executable

    codex_bin = find_codex_executable()
    if harness_settings.requires_model_cli and not codex_bin:
        console.print(
            "[red]Codex is required only because APPLYPILOT_FIELD_MODEL_CALL_BUDGET "
            "is greater than zero.[/red]"
        )
        raise typer.Exit(code=1)

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if (
        not gen
        and harness_settings.agent_backend == "codex"
        and harness_settings.deterministic_controller
    ):
        from applypilot.apply.google_passwords import choose_chrome_profile_for_google_passwords
        from applypilot.apply.onepassword import (
            OnePasswordClient,
            OnePasswordError,
            choose_chrome_profile_for_extension,
        )

        if (
            headless
            and harness_settings.uses_onepassword
            and harness_settings.allow_account_creation
        ):
            console.print(
                "[red]Headless mode is not supported with 1Password-backed account creation.[/red]\n"
                "Run without [bold]--headless[/bold] so the 1Password extension can operate."
            )
            raise typer.Exit(code=1)

        if harness_settings.uses_onepassword and harness_settings.allow_account_creation:
            try:
                OnePasswordClient(vault=harness_settings.onepassword_vault).require_ready()
            except OnePasswordError as exc:
                console.print(f"[red]1Password is not ready:[/red] {exc}")
                raise typer.Exit(code=1)

            profile_name = choose_chrome_profile_for_extension(
                extension_id=harness_settings.onepassword_extension_id
            )
            if not profile_name:
                console.print(
                    "[red]1Password Chrome extension not found in any local Chrome profile.[/red]\n"
                    "Install/unlock the extension, or set APPLYPILOT_CHROME_PROFILE_DIRECTORY "
                    "to a profile that contains it."
                )
                raise typer.Exit(code=1)
            console.print(f"[dim]1Password Chrome profile: {profile_name}[/dim]")
        elif harness_settings.uses_google_password_manager:
            profile_name = choose_chrome_profile_for_google_passwords()
            if not profile_name:
                console.print(
                    "[red]Google Password Manager profile not found.[/red]\n"
                    "Sign in to Chrome or set APPLYPILOT_CHROME_PROFILE_DIRECTORY "
                    "to the profile that owns your saved passwords."
                )
                raise typer.Exit(code=1)
            console.print(f"[dim]Google Password Manager Chrome profile: {profile_name}[/dim]")

    if gen:
        console.print(
            "[red]Legacy free-form prompt generation is disabled.[/red]\n"
            "Use [bold]applypilot autonomy plan --query QUERY[/bold] for a compact, "
            "reviewable request artifact."
        )
        raise typer.Exit(code=1)

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Backend:  {harness_settings.agent_backend}")
    console.print(f"  Model:    {harness_settings.executor_model}")
    console.print(f"  Supervisor: {harness_settings.supervisor_model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        agent_backend=agent_backend,
        supervisor_model=supervisor_model,
        allow_account_creation=allow_account_creation,
        approved_fact_digest=approved_fact_digest,
        corrections_path=corrections,
        authorization_manifest=authorization_manifest,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@autonomy_app.command("plan")
def autonomy_plan(
    query: str = typer.Option(..., "--query", "-q", help="Bounded role-search query."),
    out: Optional[Path] = typer.Option(None, "--out", help="Run artifact directory."),
    corrections: Optional[Path] = typer.Option(
        None,
        "--corrections",
        help="Optional fact_corrections.json path.",
    ),
) -> None:
    """Create a compact run packet without network or browser actions."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.autonomy.runner import prepare_run

    output_dir = out or config.APP_DIR / "autonomy-runs"
    try:
        paths = prepare_run(
            query=query,
            output_dir=output_dir,
            corrections_path=corrections,
        )
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Autonomy plan failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Wrote autonomy run packet:[/green] {paths['run_dir']}")
    console.print(f"[dim]ChatGPT Web request: {paths['request']}[/dim]")
    console.print(f"[bold]Fact digest to approve after review:[/bold] {paths['fact_digest']}")


@autonomy_app.command("import-response")
def autonomy_import_response(
    request: Path = typer.Option(..., "--request", help="Bound ChatGPT handoff request."),
    input_path: Path = typer.Option(
        ...,
        "--input",
        help="File containing ChatGPT's natural-language discovery reply or strict material JSON.",
    ),
    observed_model: str = typer.Option(
        "unobserved",
        "--observed-model",
        help="Exact visible ChatGPT model/provider label; use unobserved when the UI does not show one.",
    ),
) -> None:
    """Normalize, validate, and atomically import one ChatGPT Web response."""
    _bootstrap_config_only()
    from applypilot.autonomy.handoff import import_response_artifact

    try:
        result = import_response_artifact(
            request_path=request,
            input_path=input_path,
            observed_model=observed_model,
        )
    except Exception as exc:
        console.print(
            f"[red]ChatGPT response import failed:[/red] {type(exc).__name__}: {str(exc)[:160]}"
        )
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)


@autonomy_app.command("status")
def autonomy_status(
    run_dir: Optional[Path] = typer.Option(None, "--run-dir", help="Autonomy run directory."),
    latest: bool = typer.Option(
        False,
        "--latest",
        help="Use the newest canonical run under the ApplyPilot data directory.",
    ),
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Print only the precedence-resolved supervisor decision and liveness fields.",
    ),
) -> None:
    """Print one immutable-run-checked redacted pre-campaign status."""
    _bootstrap_config_only()
    from applypilot.autonomy.runner import compact_run_status, run_status_snapshot

    try:
        selected_run_dir = _resolve_autonomy_run_selector(run_dir=run_dir, latest=latest)
        result = run_status_snapshot(run_dir=selected_run_dir)
        if compact:
            result = compact_run_status(result)
    except Exception as exc:
        console.print(
            f"[red]Autonomy status failed:[/red] {type(exc).__name__}: {str(exc)[:160]}"
        )
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)


@autonomy_app.command("heartbeat")
def autonomy_heartbeat(
    run_dir: Optional[Path] = typer.Option(None, "--run-dir", help="Autonomy run directory."),
    latest: bool = typer.Option(
        False,
        "--latest",
        help="Use the newest canonical run under the ApplyPilot data directory.",
    ),
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Print only the precedence-resolved supervisor decision and liveness fields.",
    ),
) -> None:
    """Fsync one fixed-name redacted five-minute pre-campaign heartbeat."""
    _bootstrap_config_only()
    from applypilot.autonomy.runner import compact_run_status, record_run_heartbeat

    try:
        selected_run_dir = _resolve_autonomy_run_selector(run_dir=run_dir, latest=latest)
        result = record_run_heartbeat(run_dir=selected_run_dir)
        if compact:
            result = compact_run_status(result)
    except Exception as exc:
        console.print(
            f"[red]Autonomy heartbeat failed:[/red] {type(exc).__name__}: {str(exc)[:160]}"
        )
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)


@autonomy_app.command("observe-runtime")
def autonomy_observe_runtime(
    run_dir: Optional[Path] = typer.Option(None, "--run-dir", help="Autonomy run directory."),
    latest: bool = typer.Option(
        False,
        "--latest",
        help="Use the newest canonical run under the ApplyPilot data directory.",
    ),
    chronicle_state: str = typer.Option(
        ...,
        "--chronicle-state",
        help="capturing, idle_paused, stale, unavailable, or unknown.",
    ),
    chronicle_evidence_code: str = typer.Option(
        ...,
        "--chronicle-evidence-code",
        help="Bounded evidence code matching the Chronicle state.",
    ),
    latest_frame_at: Optional[str] = typer.Option(
        None,
        "--latest-frame-at",
        help="Canonical UTC timestamp of the explicitly observed latest Chronicle frame.",
    ),
    browser_surface: str = typer.Option(
        ...,
        "--browser-surface",
        help="codex_chrome_connector, wrong_surface, unavailable, or unknown.",
    ),
    browser_readiness: str = typer.Option(
        ...,
        "--browser-readiness",
        help="ready, unauthenticated, unavailable, or unknown.",
    ),
    ttl_seconds: int = typer.Option(
        360,
        "--ttl-seconds",
        min=30,
        max=600,
        help="Seconds before the external observation expires.",
    ),
) -> None:
    """Record one redacted, expiring browser/Chronicle diagnostic observation."""
    _bootstrap_config_only()
    import json as json_module

    from applypilot.autonomy.handoff import RunBindings
    from applypilot.autonomy.supervisor import record_runtime_observation

    try:
        selected_run_dir = _resolve_autonomy_run_selector(run_dir=run_dir, latest=latest)
        if selected_run_dir.is_symlink():
            raise ValueError("autonomy run directory must not be a symlink")
        selected_run_dir = selected_run_dir.resolve(strict=True)
        manifest_path = selected_run_dir / "run_manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("autonomy run manifest must be a regular non-symlink file")
        bindings = RunBindings.from_manifest(
            json_module.loads(manifest_path.read_text(encoding="utf-8"))
        )
        if bindings.run_id != selected_run_dir.name:
            raise ValueError("autonomy run directory differs from its manifest run id")
        result = record_runtime_observation(
            root=selected_run_dir,
            scope_kind="run",
            scope_id=bindings.run_id,
            chronicle_state=chronicle_state,
            chronicle_evidence_code=chronicle_evidence_code,
            latest_frame_at=latest_frame_at,
            browser_surface=browser_surface,
            browser_readiness=browser_readiness,
            ttl_seconds=ttl_seconds,
        )
    except Exception as exc:
        console.print(
            f"[red]Runtime observation failed:[/red] {type(exc).__name__}: {str(exc)[:160]}"
        )
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)


@autonomy_app.command("advance")
def autonomy_advance(
    run_dir: Path = typer.Option(..., "--run-dir", help="Reviewed autonomy run directory."),
    approved_fact_digest: str = typer.Option(
        ...,
        "--approved-fact-digest",
        help="Exact digest from this run's reviewed fact_ledger.json.",
    ),
) -> None:
    """Advance a reviewed artifact run until the next bounded browser handoff."""
    _bootstrap_config_only()
    from applypilot.autonomy.runner import advance_artifact_run

    try:
        result = advance_artifact_run(
            run_dir=run_dir,
            approved_fact_digest=approved_fact_digest,
        )
    except Exception as exc:
        console.print(f"[red]Autonomy advance failed:[/red] {type(exc).__name__}: {str(exc)[:160]}")
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)
    if result.get("status") not in {
        "awaiting_browser_tool",
        "awaiting_chatgpt_web",
        "review_ready",
        "no_eligible_verified_roles",
    }:
        raise typer.Exit(code=1)


@autonomy_app.command("reconcile-handoffs")
def autonomy_reconcile_handoffs(
    run_dir: Path = typer.Option(..., "--run-dir", help="Reviewed autonomy run directory."),
    approved_fact_digest: str = typer.Option(
        ...,
        "--approved-fact-digest",
        help="Exact digest from this run's reviewed fact_ledger.json.",
    ),
    retain_request_id: str = typer.Option(
        ...,
        "--retain-request-id",
        help="Exact unanswered request id to retain after queue forensics.",
    ),
) -> None:
    """Archive duplicate unanswered handoffs with an immutable reconciliation record."""
    _bootstrap_config_only()
    from applypilot.autonomy.runner import reconcile_artifact_handoffs

    try:
        result = reconcile_artifact_handoffs(
            run_dir=run_dir,
            approved_fact_digest=approved_fact_digest,
            retain_request_id=retain_request_id,
        )
    except Exception as exc:
        console.print(
            f"[red]Handoff reconciliation failed:[/red] "
            f"{type(exc).__name__}: {str(exc)[:160]}"
        )
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)


@autonomy_app.command("import-fact-approval")
def autonomy_import_fact_approval(
    run_dir: Path = typer.Option(..., "--run-dir", help="Reviewed autonomy run directory."),
    approved_fact_digest: str = typer.Option(
        ...,
        "--approved-fact-digest",
        help="Exact digest from this run's reviewed fact_ledger.json.",
    ),
    attestation: Path = typer.Option(
        ...,
        "--attestation",
        help="Exact applicant-reviewed JSON attestation.",
    ),
    signature: Path = typer.Option(
        ...,
        "--signature",
        help="Detached OpenSSH signature for the attestation.",
    ),
) -> None:
    """Verify and immutably import applicant-signed fact approval."""
    _bootstrap_config_only()
    from applypilot.autonomy.runner import import_signed_fact_approval

    try:
        result = import_signed_fact_approval(
            run_dir=run_dir,
            approved_fact_digest=approved_fact_digest,
            attestation_path=attestation,
            signature_path=signature,
        )
    except Exception as exc:
        console.print(
            f"[red]Fact approval import failed:[/red] {type(exc).__name__}: {str(exc)[:160]}"
        )
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)


@autonomy_app.command("prepare-fact-approval")
def autonomy_prepare_fact_approval(
    run_dir: Path = typer.Option(..., "--run-dir", help="Reviewed autonomy run directory."),
    approved_fact_digest: str = typer.Option(
        ...,
        "--approved-fact-digest",
        help="Exact digest from this run's reviewed fact_ledger.json.",
    ),
    issuer: str = typer.Option(..., "--issuer", help="Allowed-signers identity that will sign."),
    source_surface: str = typer.Option(
        ...,
        "--source-surface",
        help="Machine code for the user-controlled approval surface.",
    ),
    source_message_sha256: str = typer.Option(
        ...,
        "--source-message-sha256",
        help="SHA-256 of the exact applicant approval message.",
    ),
    source_author_sha256: str = typer.Option(
        ...,
        "--source-author-sha256",
        help="SHA-256 of the exact applicant author identity.",
    ),
    source_observed_at: str = typer.Option(
        ...,
        "--source-observed-at",
        help="Timezone-aware ISO timestamp for the source evidence.",
    ),
    valid_hours: int = typer.Option(24, "--valid-hours", help="Approval lifetime, 1 to 168 hours."),
    out: Path = typer.Option(..., "--out", help="New unsigned JSON path for external signing."),
) -> None:
    """Prepare exact unsigned approval bytes; this command cannot authorize anything."""
    from datetime import datetime

    _bootstrap_config_only()
    from applypilot.autonomy.runner import prepare_fact_approval_attestation

    try:
        observed_at = datetime.fromisoformat(source_observed_at)
        result = prepare_fact_approval_attestation(
            run_dir=run_dir,
            approved_fact_digest=approved_fact_digest,
            issuer=issuer,
            source_surface=source_surface,
            source_message_sha256=source_message_sha256,
            source_author_sha256=source_author_sha256,
            source_observed_at=observed_at,
            valid_hours=valid_hours,
            output_path=out,
        )
    except Exception as exc:
        console.print(
            f"[red]Fact approval preparation failed:[/red] {type(exc).__name__}: {str(exc)[:160]}"
        )
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)


@autonomy_app.command("probe-chatgpt")
def autonomy_probe_chatgpt(
    cdp_port: int = typer.Option(9222, "--cdp-port", help="Authenticated Chrome debugging port."),
    allow_legacy_cdp: bool = typer.Option(
        False,
        "--allow-legacy-cdp",
        help="Explicitly opt into caller-provided CDP instead of the normal Chrome connector handoff.",
    ),
) -> None:
    """Read legacy CDP auth/composer state without sending a prompt."""
    _bootstrap_config_only()
    from applypilot.autonomy.runner import probe_chatgpt_cdp

    try:
        result = probe_chatgpt_cdp(
            cdp_port=cdp_port,
            allow_legacy_cdp=allow_legacy_cdp,
        )
    except Exception as exc:
        console.print(f"[red]ChatGPT Web probe failed:[/red] {type(exc).__name__}: {str(exc)[:160]}")
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)
    if not result.get("available"):
        raise typer.Exit(code=1)


@autonomy_app.command("run")
def autonomy_run(
    query: str = typer.Option(..., "--query", "-q", help="Bounded role-search query."),
    cdp_port: int = typer.Option(9222, "--cdp-port", help="Authenticated Chrome debugging port."),
    allow_legacy_cdp: bool = typer.Option(
        False,
        "--allow-legacy-cdp",
        help="Explicitly opt into caller-provided CDP instead of the normal Chrome connector handoff.",
    ),
    out: Optional[Path] = typer.Option(None, "--out", help="Run artifact directory."),
    corrections: Optional[Path] = typer.Option(
        None,
        "--corrections",
        help="Optional fact_corrections.json path.",
    ),
    approved_fact_digest: str = typer.Option(
        ...,
        "--approved-fact-digest",
        help="Exact digest from a reviewed autonomy plan fact_ledger.json.",
    ),
) -> None:
    """Run the legacy review-only CDP funnel; never upload or submit."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.autonomy.runner import run_with_cdp

    output_dir = out or config.APP_DIR / "autonomy-runs"
    try:
        result = run_with_cdp(
            query=query,
            cdp_port=cdp_port,
            output_dir=output_dir,
            corrections_path=corrections,
            approved_fact_digest=approved_fact_digest,
            allow_legacy_cdp=allow_legacy_cdp,
        )
    except Exception as exc:
        console.print(f"[red]Autonomy run failed:[/red] {type(exc).__name__}: {str(exc)[:160]}")
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)
    if result.get("status") not in {"review_ready", "no_eligible_verified_roles"}:
        raise typer.Exit(code=1)


@campaign_app.command("create")
def campaign_create(
    run_dir: Path = typer.Option(..., "--run-dir", help="Reviewed autonomy run directory."),
    approved_fact_digest: str = typer.Option(
        ...,
        "--approved-fact-digest",
        help="Exact digest from the reviewed run fact ledger.",
    ),
    campaign_id: str = typer.Option(..., "--campaign-id", help="Bounded campaign identifier."),
    target: int = typer.Option(100, "--target", help="Authoritatively confirmed submission target."),
    submit: bool = typer.Option(
        False,
        "--submit/--review-only",
        help="Record explicit campaign-level submission intent; exact per-candidate grants remain required.",
    ),
    code_revision: Optional[str] = typer.Option(
        None,
        "--code-revision",
        help="Review-only revision override; live mode requires clean checkout HEAD.",
    ),
    out: Optional[Path] = typer.Option(None, "--out", help="Exact campaign state directory."),
) -> None:
    """Create immutable campaign state from one reviewed autonomy run packet."""
    _bootstrap_config_only()
    from applypilot import config
    from applypilot.autonomy.campaign import CampaignManifest, CampaignStore
    from applypilot.autonomy.runner import load_reviewed_run_snapshot

    if submit and code_revision is not None:
        console.print("[red]Live campaigns cannot override the current reviewed Git revision.[/red]")
        raise typer.Exit(code=1)
    try:
        snapshot = load_reviewed_run_snapshot(
            run_dir=run_dir,
            approved_fact_digest=approved_fact_digest,
            require_signed_approval=submit,
        )
        revision = code_revision or _current_git_revision(require_clean=submit)
        manifest = CampaignManifest.new(
            campaign_id=campaign_id,
            source_run_id=snapshot["run_id"],
            query=snapshot["query"],
            fact_digest=snapshot["fact_digest"],
            context_digest=snapshot["context_digest"],
            policy_digest=snapshot["policy_digest"],
            code_revision=revision,
            submit_authorized=submit,
            allow_account_creation=False,
            fact_approval_receipt_sha256=snapshot["fact_approval_receipt_sha256"],
            fact_approval_signature_sha256=snapshot["fact_approval_signature_sha256"],
            approval_issuer=snapshot["approval_issuer"],
            approval_trust_store_sha256=snapshot["approval_trust_store_sha256"],
            fact_approval_expires_at=snapshot["fact_approval_expires_at"],
            target_confirmed=target,
        )
        campaign_dir = (out or config.CAMPAIGN_DIR / campaign_id).resolve()
        store = CampaignStore.create(campaign_dir, manifest)
    except Exception as exc:
        console.print(f"[red]Campaign creation failed:[/red] {type(exc).__name__}: {str(exc)[:160]}")
        raise typer.Exit(code=1) from exc
    console.print_json(
        data={
            "campaign_dir": str(store.root),
            "campaign_id": manifest.campaign_id,
            "manifest_digest": manifest.digest,
            "submit_authorized": manifest.submit_authorized,
            "target_confirmed": manifest.target_confirmed,
        }
    )


@campaign_app.command("status")
def campaign_status(
    campaign_dir: Path = typer.Option(..., "--campaign-dir", help="Campaign state directory."),
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Print only bounded campaign decision, progress, liveness, and runtime fields.",
    ),
) -> None:
    """Print a bounded, redacted campaign heartbeat snapshot."""
    _bootstrap_config_only()
    from applypilot.autonomy.campaign import CampaignStore, compact_heartbeat_snapshot

    try:
        snapshot = CampaignStore.open(campaign_dir).heartbeat_snapshot()
        if compact:
            snapshot = compact_heartbeat_snapshot(snapshot)
    except Exception as exc:
        console.print(f"[red]Campaign status failed:[/red] {type(exc).__name__}: {str(exc)[:160]}")
        raise typer.Exit(code=1) from exc
    console.print_json(data=snapshot)


@campaign_app.command("heartbeat")
def campaign_heartbeat(
    campaign_dir: Path = typer.Option(..., "--campaign-dir", help="Campaign state directory."),
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Print only bounded campaign decision, progress, liveness, and runtime fields.",
    ),
) -> None:
    """Record and print one redacted five-minute campaign heartbeat."""
    _bootstrap_config_only()
    from applypilot.autonomy.campaign import CampaignStore, compact_heartbeat_snapshot

    try:
        store = CampaignStore.open(campaign_dir)
        with store.acquire_lease("campaign-heartbeat-cli"):
            snapshot = store.record_heartbeat()
        if compact:
            snapshot = compact_heartbeat_snapshot(snapshot)
    except Exception as exc:
        console.print(f"[red]Campaign heartbeat failed:[/red] {type(exc).__name__}: {str(exc)[:160]}")
        raise typer.Exit(code=1) from exc
    console.print_json(data=snapshot)


@campaign_app.command("observe-runtime")
def campaign_observe_runtime(
    campaign_dir: Path = typer.Option(..., "--campaign-dir", help="Campaign state directory."),
    chronicle_state: str = typer.Option(
        ...,
        "--chronicle-state",
        help="capturing, idle_paused, stale, unavailable, or unknown.",
    ),
    chronicle_evidence_code: str = typer.Option(
        ...,
        "--chronicle-evidence-code",
        help="Bounded evidence code matching the Chronicle state.",
    ),
    latest_frame_at: Optional[str] = typer.Option(
        None,
        "--latest-frame-at",
        help="Canonical UTC timestamp of the explicitly observed latest Chronicle frame.",
    ),
    browser_surface: str = typer.Option(
        ...,
        "--browser-surface",
        help="codex_chrome_connector, wrong_surface, unavailable, or unknown.",
    ),
    browser_readiness: str = typer.Option(
        ...,
        "--browser-readiness",
        help="ready, unauthenticated, unavailable, or unknown.",
    ),
    ttl_seconds: int = typer.Option(
        360,
        "--ttl-seconds",
        min=30,
        max=600,
        help="Seconds before the external observation expires.",
    ),
) -> None:
    """Record one redacted, expiring campaign runtime observation."""
    _bootstrap_config_only()
    from applypilot.autonomy.campaign import CampaignStore
    from applypilot.autonomy.supervisor import record_runtime_observation

    try:
        store = CampaignStore.open(campaign_dir)
        result = record_runtime_observation(
            root=store.root,
            scope_kind="campaign",
            scope_id=store.manifest.campaign_id,
            chronicle_state=chronicle_state,
            chronicle_evidence_code=chronicle_evidence_code,
            latest_frame_at=latest_frame_at,
            browser_surface=browser_surface,
            browser_readiness=browser_readiness,
            ttl_seconds=ttl_seconds,
        )
    except Exception as exc:
        console.print(
            f"[red]Campaign runtime observation failed:[/red] "
            f"{type(exc).__name__}: {str(exc)[:160]}"
        )
        raise typer.Exit(code=1) from exc
    console.print_json(data=result)


@improve_app.command("plan")
def improve_plan(
    scope: str = typer.Option("apply", "--scope", help="Improvement scope label."),
    out: Optional[Path] = typer.Option(None, "--out", help="Output directory for the improve run."),
    goal: Optional[str] = typer.Option(None, "--goal", help="Specific improvement goal for this run."),
    allowed_file: Optional[list[str]] = typer.Option(
        None,
        "--allowed-file",
        help="Allowed file or glob pattern. Repeat to override the default allowlist.",
    ),
) -> None:
    """Create a bounded self-improvement plan and prompt packet."""
    _bootstrap_config_only()

    from applypilot.dev_harness.runner import create_plan

    plan_path = create_plan(
        scope=scope,
        out_dir=out,
        goal=goal,
        allowed_files=tuple(allowed_file) if allowed_file else None,
    )
    console.print(f"[green]Wrote improve plan:[/green] {plan_path}")
    console.print(f"[dim]Worker prompt: {plan_path.parent / 'worker_prompt.md'}[/dim]")
    console.print(f"[dim]Reviewer prompt: {plan_path.parent / 'reviewer_prompt.md'}[/dim]")


@improve_app.command("worker")
def improve_worker(
    artifact: Path = typer.Option(..., "--artifact", help="Path to plan.json."),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="Write proposal artifact without editing files."),
) -> None:
    """Create a worker proposal artifact from an improve plan."""
    _bootstrap_config_only()

    from applypilot.dev_harness.runner import create_worker_proposal

    try:
        proposal_path = create_worker_proposal(plan_path=artifact, dry_run=dry_run)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Wrote worker proposal:[/green] {proposal_path}")


@improve_app.command("review")
def improve_review(
    artifact: Path = typer.Option(..., "--artifact", help="Path to proposal.json."),
) -> None:
    """Review a worker proposal against its improve plan."""
    _bootstrap_config_only()

    from applypilot.dev_harness.artifacts import read_json
    from applypilot.dev_harness.reviewer import review_proposal

    review_path = review_proposal(proposal_path=artifact)
    review = read_json(review_path)
    verdict = "approved" if review.get("approved") else "not approved"
    console.print(f"[green]Wrote improve review:[/green] {review_path}")
    console.print(f"Verdict: [bold]{verdict}[/bold]")
    if not review.get("approved"):
        raise typer.Exit(code=1)


@improve_app.command("validate")
def improve_validate(
    artifact: Path = typer.Option(..., "--artifact", help="Path to plan.json."),
    timeout_seconds: int = typer.Option(300, "--timeout-seconds", help="Per-command validation timeout."),
) -> None:
    """Run deterministic validation commands from an improve plan."""
    _bootstrap_config_only()

    from applypilot.dev_harness.artifacts import read_json
    from applypilot.dev_harness.runner import run_validation

    results_path = run_validation(plan_path=artifact, timeout_seconds=timeout_seconds)
    results = read_json(results_path)
    status = "passed" if results.get("passed") else "failed"
    console.print(f"[green]Wrote validation results:[/green] {results_path}")
    console.print(f"Validation: [bold]{status}[/bold]")
    if not results.get("passed"):
        raise typer.Exit(code=1)


@app.command()
def training_audit(
    json_output: bool = typer.Option(False, "--json", help="Print the raw audit and manifest as JSON."),
) -> None:
    """Audit apply-agent training coverage for job boards, Workday, Runway, and email drafts."""
    from applypilot.apply.prompt import build_training_manifest
    from applypilot.apply.training_audit import audit_training_manifest

    manifest = build_training_manifest()
    audit = audit_training_manifest(manifest)

    if json_output:
        console.print_json(data={"audit": audit, "manifest": manifest})
        if not audit["passed"]:
            raise typer.Exit(code=1)
        return

    console.print()
    console.print("[bold]ApplyPilot Training Audit[/bold]\n")

    table = Table(title="Training Coverage", show_header=True, header_style="bold cyan")
    table.add_column("Check", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Detail")

    def status(ok: bool) -> str:
        return "[green]PASS[/green]" if ok else "[red]FAIL[/red]"

    missing_capabilities = audit["missing_capabilities"]
    table.add_row(
        "Capabilities",
        status(not missing_capabilities),
        "All required boundaries present"
        if not missing_capabilities
        else "Missing: " + ", ".join(missing_capabilities),
    )

    missing_scenarios = audit["missing_scenarios"]
    table.add_row(
        "Scenarios",
        status(not missing_scenarios),
        "Workday, email-only, Runway, aggregator, native, and external ATS drills present"
        if not missing_scenarios
        else "Missing: " + ", ".join(missing_scenarios),
    )

    table.add_row(
        "Runway",
        status(audit["has_runway_source"]),
        audit["runway_url"] if audit["has_runway_source"] else "Missing Runway smart-extract source",
    )
    table.add_row(
        "Email draft",
        status(audit["email_draft_artifact_ok"]),
        audit["email_draft_artifact"],
    )

    missing_result_codes = audit["missing_result_codes"]
    table.add_row(
        "Result codes",
        status(not missing_result_codes),
        "APPLIED, EMAIL_DRAFT, and FAILED contracts present"
        if not missing_result_codes
        else "Missing: " + ", ".join(missing_result_codes),
    )

    board_detail = f"{audit['configured_jobspy_boards']} configured JobSpy board(s)"
    if audit["jobspy_boards_without_rules"]:
        board_detail += "; missing specific rules: " + ", ".join(audit["jobspy_boards_without_rules"])
    board_status = audit["jobspy_board_status"]
    if board_status == "not_applicable":
        board_label = "[dim]N/A[/dim]"
        board_detail += "; not required in direct_sources mode"
    elif board_status == "fail":
        board_label = "[red]FAIL[/red]"
        board_detail += f"; required in {audit['discovery_mode']} mode"
    elif audit["jobspy_boards_without_rules"]:
        board_label = "[yellow]WARN[/yellow]"
    else:
        board_label = "[green]PASS[/green]"
    table.add_row(
        "JobSpy boards",
        board_label,
        board_detail,
    )

    table.add_row(
        "Smart sources",
        "[green]PASS[/green]" if audit["smart_extract_source_count"] else "[red]FAIL[/red]",
        (
            f"{audit['smart_extract_source_count']} total; "
            f"{audit['search_source_count']} search, {audit['static_source_count']} static"
        ),
    )
    table.add_row("Manual ATS", "[green]PASS[/green]", f"{audit['manual_ats_count']} configured domain(s)")

    console.print(table)
    console.print()

    if not audit["passed"]:
        raise typer.Exit(code=1)


@app.command()
def doctor(
    strict: bool = typer.Option(False, "--strict", help="Exit nonzero when required checks are missing."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable check results."),
    autonomy: bool = typer.Option(
        False,
        "--autonomy",
        help="Compatibility alias for the canonical ChatGPT Web workflow check.",
    ),
    legacy: bool = typer.Option(
        False,
        "--legacy",
        help="Check the old API-key pipeline instead of the canonical workflow.",
    ),
    autonomy_corrections: Optional[Path] = typer.Option(
        None,
        "--autonomy-corrections",
        help="Optional fact_corrections.json used by the autonomy readiness check.",
    ),
    chatgpt_cdp_port: Optional[int] = typer.Option(
        None,
        "--chatgpt-cdp-port",
        help="Optionally probe an authenticated Chrome/ChatGPT Web CDP session without sending.",
    ),
) -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path, get_secret,
        load_search_config,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)
    canonical_workflow = autonomy or not legacy

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    search_cfg = load_search_config()
    from applypilot.pipeline import discovery_plan
    plan = discovery_plan(search_cfg)
    enabled_sources = [
        name for name in ("jobspy", "workday", "direct_ats", "smartextract") if plan.get(name)
    ]
    results.append((
        "Discovery mode",
        ok_mark,
        f"{plan['mode']} ({', '.join(enabled_sources) or 'no sources enabled'})",
    ))

    # jobspy (optional discovery extra)
    if plan["jobspy"]:
        try:
            import jobspy  # noqa: F401
            results.append(("python-jobspy", ok_mark, "Job board scraping available"))
        except ImportError:
            results.append(("python-jobspy", warn_mark,
                            "Install discovery extra: pip install 'applypilot[discovery]'"))
    else:
        results.append(("python-jobspy", ok_mark, "Not required for current discovery mode"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(get_secret("GEMINI_API_KEY"))
    has_openai = bool(get_secret("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    configured_provider = os.environ.get("APPLYPILOT_LLM_PROVIDER", "").strip().lower()
    if canonical_workflow:
        results.append((
            "ChatGPT Web artifact transport",
            ok_mark,
            "portable request/response queue; no model API key, cloned profile, or CDP ownership required",
        ))
        if PROFILE_PATH.exists() and RESUME_PATH.exists():
            try:
                import json as json_module

                from applypilot.autonomy.facts import (
                    REQUIRED_AUTONOMY_FACT_IDS,
                    build_fact_ledger,
                    load_corrections,
                    require_confirmed_facts,
                )

                profile_data = json_module.loads(PROFILE_PATH.read_text(encoding="utf-8"))
                corrections = (
                    load_corrections(autonomy_corrections)
                    if autonomy_corrections is not None
                    else ()
                )
                fact_ledger = build_fact_ledger(
                    profile_data,
                    resume_text=RESUME_PATH.read_text(encoding="utf-8"),
                    corrections=corrections,
                )
                fact_blockers = require_confirmed_facts(
                    fact_ledger,
                    REQUIRED_AUTONOMY_FACT_IDS,
                )
                from applypilot.autonomy.context import candidate_profile_from_data

                candidate_profile = candidate_profile_from_data(
                    profile_data,
                    search_config=search_cfg,
                )
            except Exception as exc:
                results.append((
                    "Autonomy facts",
                    fail_mark,
                    f"fact readiness check failed: {type(exc).__name__}",
                ))
            else:
                results.append((
                    "Submission facts",
                    fail_mark if fact_blockers else ok_mark,
                    ", ".join(fact_blockers)
                    if fact_blockers
                    else "required contact, work authorization, sponsorship, and availability confirmed",
                ))
                results.append((
                    "Search preferences",
                    ok_mark if candidate_profile.preferred_locations else fail_mark,
                    ", ".join(candidate_profile.preferred_locations)
                    if candidate_profile.preferred_locations
                    else "add at least one preferred location",
                ))
        from applypilot import config as applypilot_config

        workflow_path = applypilot_config.APP_DIR / "workflow.sqlite3"
        results.append((
            "Canonical workflow state",
            ok_mark,
            str(workflow_path) if workflow_path.exists() else "created automatically by applypilot prepare",
        ))
        results.append((
            "Visible Chrome boundary",
            ok_mark,
            "browser handoffs require the authenticated Codex Chrome connector and fail closed",
        ))
        if chatgpt_cdp_port is not None:
            try:
                from applypilot.autonomy.runner import probe_chatgpt_cdp

                probe = probe_chatgpt_cdp(
                    cdp_port=chatgpt_cdp_port,
                    allow_legacy_cdp=True,
                )
            except Exception as exc:
                results.append(("Optional ChatGPT CDP probe", warn_mark, f"probe failed: {type(exc).__name__}"))
            else:
                results.append((
                    "Optional ChatGPT CDP probe",
                    ok_mark if probe.get("available") else warn_mark,
                    "authenticated composer available; no prompt sent"
                    if probe.get("available")
                    else "authenticated composer unavailable; artifact browser tool remains supported",
                ))
    elif configured_provider == "chatgpt_web":
        if chatgpt_cdp_port is None:
            results.append((
                "ChatGPT Web",
                fail_mark if strict else warn_mark,
                "configured but unprobed; run doctor --chatgpt-cdp-port PORT for a no-send auth probe",
            ))
        else:
            try:
                from applypilot.autonomy.runner import probe_chatgpt_cdp

                probe = probe_chatgpt_cdp(
                    cdp_port=chatgpt_cdp_port,
                    allow_legacy_cdp=True,
                )
            except Exception as exc:
                results.append(("ChatGPT Web", fail_mark, f"probe failed: {type(exc).__name__}"))
            else:
                results.append((
                    "ChatGPT Web",
                    ok_mark if probe.get("available") else fail_mark,
                    "authenticated composer available; no prompt sent"
                    if probe.get("available")
                    else "authenticated composer unavailable",
                ))
    elif has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        f"Set GEMINI_API_KEY in {ENV_PATH} or OS keyring (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Agent CLIs
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", warn_mark,
                        "Install from https://claude.ai/code for Claude backend"))

    codex_bin = shutil.which("codex")
    if not codex_bin:
        bundled_codex = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        if bundled_codex.exists():
            codex_bin = str(bundled_codex)
    if codex_bin:
        results.append(("Codex CLI", ok_mark, codex_bin))
    else:
        results.append(("Codex CLI", warn_mark,
                        "Install Codex CLI for codex backend"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append((
            "Node.js (npx)",
            warn_mark,
            "not required by the deterministic Python controller; needed only for legacy MCP tooling",
        ))

    results.append(("CAPTCHA policy", ok_mark, "fail closed; solver APIs are not used"))

    from applypilot.apply.harness import load_settings
    harness_settings = load_settings()
    codex_settings = load_settings(agent_backend="codex")
    results.append(("Harness backend", ok_mark, harness_settings.agent_backend))
    results.append(("Executor model", ok_mark, harness_settings.executor_model))
    results.append(("Supervisor model", ok_mark, harness_settings.supervisor_model))
    results.append((
        "Deterministic controller",
        ok_mark if harness_settings.deterministic_controller else warn_mark,
        "enabled" if harness_settings.deterministic_controller else "disabled",
    ))

    if codex_bin:
        if codex_settings.executor_model == "gpt-5.5":
            results.append(("Codex model readiness", ok_mark, "configured for gpt-5.5"))
        else:
            results.append((
                "Codex model readiness",
                warn_mark,
                f"configured for {codex_settings.executor_model}; gpt-5.5 is recommended",
            ))

    from applypilot.apply.google_passwords import (
        chrome_profiles_with_password_store,
        choose_chrome_profile_for_google_passwords,
    )
    from applypilot.apply.onepassword import (
        OnePasswordClient,
        OnePasswordError,
        chrome_profiles_with_extension,
        choose_chrome_profile_for_extension,
    )

    results.append(("Credential provider", ok_mark, harness_settings.credential_provider))

    if harness_settings.uses_google_password_manager:
        profiles = chrome_profiles_with_password_store()
        selected_profile = choose_chrome_profile_for_google_passwords()
        if selected_profile:
            detail = f"profile {selected_profile}"
            if profiles:
                detail += f"; password store metadata in {', '.join(profiles)}"
            results.append(("Google Password Manager", ok_mark, detail))
        else:
            results.append((
                "Google Password Manager",
                warn_mark,
                "Chrome profile not found; set APPLYPILOT_CHROME_PROFILE_DIRECTORY",
            ))
    elif harness_settings.uses_onepassword:
        op_bin = shutil.which("op")
        if op_bin:
            try:
                OnePasswordClient(vault=harness_settings.onepassword_vault).require_ready()
                results.append(("1Password CLI", ok_mark, f"{op_bin} (signed in)"))
            except OnePasswordError as exc:
                results.append(("1Password CLI", fail_mark, str(exc)))
        else:
            results.append(("1Password CLI", fail_mark, "Install 1Password CLI `op` and run `op signin`"))

        profiles = chrome_profiles_with_extension(
            extension_id=harness_settings.onepassword_extension_id
        )
        selected_profile = choose_chrome_profile_for_extension(
            extension_id=harness_settings.onepassword_extension_id
        )
        if selected_profile:
            results.append((
                "1Password extension",
                ok_mark,
                f"profile {selected_profile}; found in {', '.join(profiles)}",
            ))
        else:
            results.append((
                "1Password extension",
                fail_mark,
                f"Chrome extension {harness_settings.onepassword_extension_id} not found",
            ))
    else:
        results.append((
            "Credential manager",
            warn_mark,
            "disabled; login/account forms will fail closed",
        ))

    if harness_settings.uses_onepassword and harness_settings.allow_account_creation:
        results.append((
            "Headless apply",
            warn_mark,
            "disabled for 1Password-backed account creation",
        ))
    elif harness_settings.uses_google_password_manager:
        results.append((
            "Headless apply",
            warn_mark,
            "use visible Chrome for browser-managed password prompts/autofill",
        ))
    else:
        results.append(("Headless apply", ok_mark, "available"))

    serialized_results = [
        {
            "check": check,
            "status": "missing" if status == fail_mark else "warn" if status == warn_mark else "ok",
            "note": note,
        }
        for check, status, note in results
    ]
    missing_checks = [item["check"] for item in serialized_results if item["status"] == "missing"]
    static_ready = not missing_checks
    runtime_ready = None
    ready = not missing_checks

    if json_output:
        console.print_json(
            data={
                "ready": ready,
                "static_ready": static_ready,
                "runtime_ready": runtime_ready,
                "strict": strict,
                "missing_checks": missing_checks,
                "checks": serialized_results,
            }
        )
    else:
        # --- Render results ---
        console.print()
        console.print("[bold]ApplyPilot Doctor[/bold]\n")

        col_w = max(len(r[0]) for r in results) + 2
        for check, status, note in results:
            pad = " " * (col_w - len(check))
            console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

        console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    if not json_output and legacy:
        console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if not json_output and canonical_workflow:
        console.print(
            "[dim]  → Canonical path: prepare → dry-run → approve → execute (visible Chrome)[/dim]"
        )
    elif not json_output and tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs agent CLI + Chrome + Node.js)[/dim]")
    elif not json_output and tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs agent CLI + Chrome + Node.js)[/dim]")

    if not json_output:
        console.print()
    if strict and missing_checks:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
