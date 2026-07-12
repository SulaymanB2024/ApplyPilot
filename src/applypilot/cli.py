"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

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
app.add_typer(improve_app, name="improve")
app.add_typer(autonomy_app, name="autonomy")
app.add_typer(campaign_app, name="campaign")
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
        help="File containing the one bare JSON object returned by ChatGPT Web.",
    ),
) -> None:
    """Validate and atomically import one ChatGPT Web response artifact."""
    _bootstrap_config_only()
    from applypilot.autonomy.handoff import import_response_artifact

    try:
        result = import_response_artifact(
            request_path=request,
            input_path=input_path,
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
) -> None:
    """Print a bounded, redacted campaign heartbeat snapshot."""
    _bootstrap_config_only()
    from applypilot.autonomy.campaign import CampaignStore

    try:
        snapshot = CampaignStore.open(campaign_dir).heartbeat_snapshot()
    except Exception as exc:
        console.print(f"[red]Campaign status failed:[/red] {type(exc).__name__}: {str(exc)[:160]}")
        raise typer.Exit(code=1) from exc
    console.print_json(data=snapshot)


@campaign_app.command("heartbeat")
def campaign_heartbeat(
    campaign_dir: Path = typer.Option(..., "--campaign-dir", help="Campaign state directory."),
) -> None:
    """Record and print one redacted five-minute campaign heartbeat."""
    _bootstrap_config_only()
    from applypilot.autonomy.campaign import CampaignStore

    try:
        store = CampaignStore.open(campaign_dir)
        with store.acquire_lease("campaign-heartbeat-cli"):
            snapshot = store.record_heartbeat()
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
        help="Check the portable ChatGPT Web autonomy path instead of legacy API-key scoring.",
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
    autonomy_runtime_ready: bool | None = None

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
    if autonomy:
        results.append((
            "ChatGPT Web artifact transport",
            ok_mark,
            "portable request/response queue; no model API key or CDP ownership required",
        ))
        if PROFILE_PATH.exists() and RESUME_PATH.exists():
            try:
                import json as json_module

                from applypilot.autonomy.facts import (
                    REQUIRED_AUTONOMY_FACT_IDS,
                    build_fact_ledger,
                    confirmed_preferred_location_fact_ids,
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
                location_fact_ids = confirmed_preferred_location_fact_ids(fact_ledger)
            except Exception as exc:
                results.append((
                    "Autonomy facts",
                    fail_mark,
                    f"fact readiness check failed: {type(exc).__name__}",
                ))
            else:
                results.append((
                    "Autonomy facts",
                    fail_mark if fact_blockers else ok_mark,
                    ", ".join(fact_blockers)
                    if fact_blockers
                    else "required contact, work authorization, sponsorship, and availability confirmed",
                ))
                results.append((
                    "Autonomy preferred locations",
                    ok_mark if location_fact_ids else fail_mark,
                    f"{len(location_fact_ids)} confirmed location fact(s)"
                    if location_fact_ids
                    else "add at least one confirmed preferred location before live approval",
                ))
        try:
            from applypilot.autonomy.approval import (
                FactApprovalError,
                require_system_approval_trust_store,
            )

            trust_store = require_system_approval_trust_store()
        except FactApprovalError as exc:
            results.append(("System approval trust store", fail_mark, str(exc)))
        else:
            results.append(("System approval trust store", ok_mark, str(trust_store)))
        try:
            from applypilot.autonomy.runner import latest_autonomy_run_dir
            from applypilot.autonomy.supervisor import runtime_observation_snapshot

            latest_run_dir = latest_autonomy_run_dir()
            runtime_status = runtime_observation_snapshot(
                root=latest_run_dir,
                scope_kind="run",
                scope_id=latest_run_dir.name,
            )
        except Exception as exc:
            autonomy_runtime_ready = False
            results.append(
                (
                    "Autonomy runtime observation",
                    fail_mark,
                    f"latest-run runtime check failed: {type(exc).__name__}",
                )
            )
        else:
            autonomy_runtime_ready = bool(runtime_status["runtime_ready"])
            results.append(
                (
                    "Autonomy runtime observation",
                    ok_mark if autonomy_runtime_ready else fail_mark,
                    "; ".join(
                        (
                            f"observation={runtime_status['observation_state']}",
                            f"chronicle={runtime_status['chronicle_state']}",
                            f"browser={runtime_status['browser_surface']}",
                            f"readiness={runtime_status['browser_readiness']}",
                        )
                    ),
                )
            )
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
    runtime_check_name = "Autonomy runtime observation"
    static_missing_checks = [
        check for check in missing_checks if check != runtime_check_name
    ]
    static_ready = not static_missing_checks
    runtime_ready = autonomy_runtime_ready if autonomy else None
    ready = static_ready and bool(runtime_ready) if autonomy else not missing_checks

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
    if not json_output:
        console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if not json_output and tier == 1:
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
