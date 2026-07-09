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
app.add_typer(improve_app, name="improve")
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


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


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
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override agent executor model."),
    agent_backend: Optional[str] = typer.Option(
        None,
        "--agent-backend",
        help="Agent runner backend: claude or codex. Defaults to APPLYPILOT_AGENT_BACKEND or claude.",
    ),
    supervisor_model: Optional[str] = typer.Option(
        None,
        "--supervisor-model",
        help="Supervisor model label written into the deterministic harness contract.",
    ),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
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

    # Check 1: Tier 3 required (agent CLI + Chrome)
    check_tier(3, "auto-apply")

    import shutil
    from applypilot.apply.harness import load_settings as load_harness_settings
    harness_settings = load_harness_settings(
        agent_backend=agent_backend,
        executor_model=model,
        supervisor_model=supervisor_model,
    )
    if not shutil.which(harness_settings.agent_backend):
        console.print(
            f"[red]Missing {harness_settings.agent_backend} executable for selected "
            f"--agent-backend {harness_settings.agent_backend}.[/red]"
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
        from applypilot.apply.launcher import gen_prompt
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(
            target,
            min_score=min_score,
            model=model,
            agent_backend=agent_backend,
            supervisor_model=supervisor_model,
        )
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print("\n[bold]Run manually:[/bold]")
        if harness_settings.agent_backend == "codex":
            console.print(
                f"  codex exec --model {harness_settings.executor_model} "
                f"--sandbox danger-full-access --ephemeral --cd {prompt_file.parent} < {prompt_file}"
            )
        else:
            console.print(
                f"  claude --model {harness_settings.executor_model} -p "
                f"--mcp-config {mcp_path} "
                f"--permission-mode bypassPermissions < {prompt_file}"
            )
        return

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
    table.add_row(
        "JobSpy boards",
        "[yellow]WARN[/yellow]" if audit["jobspy_boards_without_rules"] else "[green]PASS[/green]",
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
def doctor() -> None:
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
    if has_gemini:
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
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

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
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs agent CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs agent CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
