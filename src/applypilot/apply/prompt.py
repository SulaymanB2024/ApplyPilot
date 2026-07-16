"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells Claude Code / the AI agent
how to fill out a job application form using Playwright MCP tools. All
personal data is loaded from the user's profile -- nothing is hardcoded.
"""

import logging
import shutil
from datetime import datetime
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)


def _build_profile_summary(profile: dict) -> str:
    """Format the applicant profile section of the prompt.

    Reads all relevant fields from the profile dict and returns a
    human-readable multi-line summary for the agent.
    """
    p = profile
    personal = p["personal"]
    work_auth = p["work_authorization"]
    comp = p["compensation"]
    exp = p.get("experience", {})
    avail = p.get("availability", {})
    eeo = p.get("eeo_voluntary", {})
    screening = p.get("screening", {})
    eligibility = p.get("eligibility", {})

    lines = [
        f"Name: {personal['full_name']}",
        f"Email: {personal['email']}",
        f"Phone: {personal['phone']}",
    ]

    # Address -- handle optional fields gracefully
    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")
    if personal.get("website_url"):
        lines.append(f"Website: {personal['website_url']}")

    # Work authorization
    lines.append(f"Work Auth: {_supplied(work_auth.get('legally_authorized_to_work'))}")
    lines.append(f"Sponsorship Needed: {_supplied(work_auth.get('require_sponsorship'))}")
    if work_auth.get("work_permit_type"):
        lines.append(f"Work Permit: {work_auth['work_permit_type']}")

    # Compensation
    currency = comp.get("salary_currency", "USD")
    salary = _supplied(comp.get("salary_expectation"))
    lines.append(f"Salary Expectation: {salary} {currency}")

    # Experience
    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")
    if exp.get("education_level"):
        lines.append(f"Education: {exp['education_level']}")

    # Availability
    lines.append(f"Available: {_supplied(avail.get('earliest_start_date'))}")

    # Screening answers must come from the profile. Missing answers remain explicit abstentions.
    lines.extend(
        [
            f"Age 18+: {_supplied(eligibility.get('is_at_least_18'))}",
            f"Background Check: {_supplied(screening.get('consent_background_check'))}",
            f"Felony: {_supplied(screening.get('felony_conviction'))}",
            f"Previously Worked Here: {_supplied(screening.get('previously_worked_here'))}",
            f"How Heard: {_supplied(screening.get('how_heard'))}",
        ]
    )

    # EEO
    lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
    lines.append(f"Race: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
    lines.append(f"Veteran: {eeo.get('veteran_status', 'Decline to self-identify')}")
    lines.append(f"Disability: {eeo.get('disability_status', 'I do not wish to answer')}")

    return "\n".join(lines)


def _supplied(value: object) -> object:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "UNCONFIRMED — do not infer"
    return value


def _build_location_check(profile: dict, search_config: dict) -> str:
    """Build the location eligibility check section of the prompt.

    Uses the accept_patterns from search config to determine which cities
    are acceptable for hybrid/onsite roles.
    """
    personal = profile["personal"]
    location_cfg = search_config.get("location", {})
    accept_patterns = location_cfg.get("accept_patterns", [])
    primary_city = personal.get("city", location_cfg.get("primary", "your city"))

    # Build the list of acceptable cities for hybrid/onsite
    if accept_patterns:
        city_list = ", ".join(accept_patterns)
    else:
        city_list = primary_city

    return f"""== LOCATION CHECK (do this FIRST before any form) ==
Read the job page. Determine the work arrangement. Then decide:
- "Remote" or "work from anywhere" -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in {city_list} -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in another city BUT the posting also says "remote OK" or "remote option available" -> ELIGIBLE. Apply.
- "Onsite only" or "hybrid only" in any city outside the list above with NO remote option -> NOT ELIGIBLE. Stop immediately. Output RESULT:FAILED:not_eligible_location
- City is overseas (India, Philippines, Europe, etc.) with no remote option -> NOT ELIGIBLE. Output RESULT:FAILED:not_eligible_location
- Cannot determine location -> Continue applying. If a screening question reveals it's non-local onsite, answer honestly and let the system reject if needed.
Do NOT fill out forms for jobs that are clearly onsite in a non-acceptable location. Check EARLY, save time."""


def _build_salary_section(profile: dict) -> str:
    """Build the salary negotiation instructions.

    Adapts floor, range, and currency from the profile's compensation section.
    """
    comp = profile["compensation"]
    currency = comp.get("salary_currency", "USD")
    floor = comp["salary_expectation"]
    range_min = comp.get("salary_range_min", floor)
    range_max = comp.get("salary_range_max", str(int(floor) + 20000) if floor.isdigit() else floor)
    conversion_note = comp.get("currency_conversion_note", "")

    # Compute example hourly rates at 3 salary levels
    try:
        floor_int = int(floor)
        examples = [
            (f"${floor_int // 1000}K", floor_int // 2080),
            (f"${(floor_int + 25000) // 1000}K", (floor_int + 25000) // 2080),
            (f"${(floor_int + 55000) // 1000}K", (floor_int + 55000) // 2080),
        ]
        hourly_line = ", ".join(f"{sal} = ${hr}/hr" for sal, hr in examples)
    except (ValueError, TypeError):
        hourly_line = "Divide annual salary by 2080"

    # Currency conversion guidance
    if conversion_note:
        convert_line = f"Posting is in a different currency? -> {conversion_note}"
    else:
        convert_line = "Posting is in a different currency? -> Target midpoint of their range. Convert if needed."

    return f"""== SALARY (think, don't just copy) ==
${floor} {currency} is the FLOOR. Never go below it. But don't always use it either.

Decision tree:
1. Job posting shows a range (e.g. "$120K-$160K")? -> Answer with the MIDPOINT ($140K).
2. Title says Senior, Staff, Lead, Principal, Architect, or level II/III/IV? -> Minimum $110K {currency}. Use midpoint of posted range if higher.
3. {convert_line}
4. No salary info anywhere? -> Use ${floor} {currency}.
5. Asked for a range? -> Give posted midpoint minus 10% to midpoint plus 10%. No posted range? -> "${range_min}-${range_max} {currency}".
6. Hourly rate? -> Divide your annual answer by 2080. ({hourly_line})"""


def _build_screening_section(profile: dict) -> str:
    """Build the screening questions guidance section."""
    personal = profile["personal"]
    exp = profile.get("experience", {})
    city = _supplied(personal.get("city"))
    years = _supplied(exp.get("years_of_experience_total"))
    target_role = _supplied(exp.get("target_role") or personal.get("current_job_title"))
    work_auth = profile["work_authorization"]
    screening = profile.get("screening", {})

    return f"""== SCREENING QUESTIONS (be factual) ==
Hard facts -> answer truthfully from the profile. No guessing. This includes:
  - Location: {city}
  - Relocation: {_supplied(screening.get('willing_to_relocate'))}
  - Work authorization: {_supplied(work_auth.get('legally_authorized_to_work'))}
  - Citizenship, clearance, licenses, certifications: answer from profile only
  - Criminal/background: answer from profile only

Skills and tools -> use only skills or experience explicitly supported by the profile or resume. Target role: {target_role}. Confirmed experience years: {years}. Similar-domain experience is not proof of a named tool; abstain when the exact answer is missing.

Open-ended questions ("Why do you want this role?", "Tell us about yourself", "What interests you?") -> use only claims copied or faithfully paraphrased from the reviewed resume and materials. If a truthful supported answer is unavailable, stop as an unresolved required field.

EEO/demographics -> "Decline to self-identify" or "Prefer not to say" for everything."""


def _build_hard_rules(profile: dict) -> str:
    """Build the hard rules section with work auth and name from profile."""
    personal = profile["personal"]
    work_auth = profile["work_authorization"]

    full_name = personal["full_name"]
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    preferred_last = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {preferred_last}".strip() if preferred_last else preferred_name

    # Build work auth rule dynamically
    sponsorship = work_auth.get("require_sponsorship", "")
    permit_type = work_auth.get("work_permit_type", "")

    work_auth_rule = "Work auth: Answer truthfully from profile."
    if permit_type:
        work_auth_rule = (
            f"Work auth: {permit_type}. Sponsorship needed: {_supplied(sponsorship)}."
        )

    name_rule = f'Name: Legal name = {full_name}.'
    if preferred_name and preferred_name != full_name.split()[0]:
        name_rule += f' Preferred name = {preferred_name}. Use "{display_name}" unless a field specifically says "legal name".'

    return f"""== HARD RULES (never break these) ==
1. Never lie about: citizenship, work authorization, criminal history, education credentials, security clearance, licenses.
2. {work_auth_rule}
3. {name_rule}"""


_BOARD_LABELS = {
    "indeed": "Indeed",
    "linkedin": "LinkedIn",
    "glassdoor": "Glassdoor",
    "zip_recruiter": "ZipRecruiter",
    "ziprecruiter": "ZipRecruiter",
    "google": "Google Jobs",
    "google_jobs": "Google Jobs",
}

_JOBSPY_BOARD_RULES = {
    "indeed": (
        "Indeed: prefer employer apply links over profile-building flows. "
        "Use Indeed Apply only when it is clearly a direct application for this role; "
        "skip assessments or account/profile setup that is not part of the application."
    ),
    "linkedin": (
        "LinkedIn: Easy Apply is acceptable only for the exact role. If redirected to "
        "an employer site, continue there. Do not treat saving a job, following a company, "
        "or editing the LinkedIn profile as applying."
    ),
    "glassdoor": (
        "Glassdoor: treat primarily as a discovery/referral source. Open the employer "
        "apply link when available; if Glassdoor blocks access, loops login, or only offers "
        "reviews/salary/profile prompts, stop with RESULT:FAILED:site_blocked."
    ),
    "zip_recruiter": (
        "ZipRecruiter: one-click apply is acceptable only when it submits this role with "
        "the uploaded resume. Otherwise follow the employer apply link and continue on the ATS."
    ),
    "google": (
        "Google Jobs: this is an aggregator. Never treat bookmarking, sharing, or choosing "
        "an apply-provider link as submission. Pick the most direct company/ATS apply link."
    ),
    "google_jobs": (
        "Google Jobs: this is an aggregator. Never treat bookmarking, sharing, or choosing "
        "an apply-provider link as submission. Pick the most direct company/ATS apply link."
    ),
}

_SMART_SOURCE_RULES = """Smart-extract source rules:
- Direct-source mode: prefer employer-owned career pages and ATS-native pages (Workday, Greenhouse, Lever, Ashby, SmartRecruiters, iCIMS, Workable, Jobvite, BambooHR) over aggregators.
- Search result/listing pages: inspect cards, open the best matching job detail, find the Apply/External Apply/Company Site link, then continue on the employer ATS.
- Static/fresh-role boards: treat date/freshness, company, location, and title as discovery metadata. They are not application evidence.
- Remote boards: verify the role is full-time salaried and not a contractor marketplace or talent-network profile before applying.
- If a configured source sends you to a blocked source, blocked SSO domain, unsolvable manual ATS, or challenge wall before the employer application is visible, stop with the matching RESULT:FAILED reason."""

_SCENARIO_DRILLS = """Scenario drills:
- Workday drill: choose the role, start a fresh application, upload the tailored resume, wait for parsing, repair parsed fields, complete each page, expand Review sections, verify facts, then submit.
- Email-only drill: do not send mail. Write email_application_draft.md with To, Subject, Attachments, Body, and Evidence fields, then output RESULT:EMAIL_DRAFT.
- Runway drill: use https://app.joinrunway.io/explore as a fresh-role discovery surface. Use filters/match scores/job details to reach the employer apply link; Save/Get Match Scores/signup prompts are not applications.
- Aggregator drill: Google Jobs, Runway, Glassdoor, and many remote boards are discovery surfaces. The successful endpoint is the employer confirmation page, not the aggregator page.
- Native apply drill: Indeed, LinkedIn, or ZipRecruiter native apply is allowed only when it submits this exact role with the candidate's current tailored resume and no unrelated public-profile/assessment setup.
- External ATS drill: Greenhouse, Lever, Ashby, SmartRecruiters, iCIMS, Taleo, Workable, Jobvite, BambooHR, and company career sites are application surfaces. Fill required fields, upload files, answer questions, review, and submit."""

_TRAINING_SCENARIOS = (
    {
        "name": "Workday resume parser review",
        "signals": "URL contains myworkdayjobs.com; page has My Information, My Experience, Application Questions, Review",
        "actions": (
            "start a fresh application, upload the tailored resume, wait for parser completion, "
            "compare parsed fields against profile/resume, expand Review sections, fix mismatches before submit"
        ),
        "result": "RESULT:APPLIED only after Workday shows a submitted/thank-you confirmation",
    },
    {
        "name": "Email-only application",
        "signals": "posting says email resume/CV to an address and no web form exists",
        "actions": (
            "write email_application_draft.md in the working directory with To, Subject, "
            "Attachments, Body, and Evidence fields; attach paths are local references only"
        ),
        "result": "RESULT:EMAIL_DRAFT; never send email or create an external email draft",
    },
    {
        "name": "Runway fresh-role discovery",
        "signals": "URL is app.joinrunway.io/explore; page shows filters, fresh roles, company, location, date, match score, save",
        "actions": (
            "use filters/match score/job detail to identify a role, open the employer apply link, "
            "continue on the employer ATS; ignore Save/Get Match Scores as application evidence"
        ),
        "result": "continue to employer ATS or RESULT:LOGIN_ISSUE if Runway login blocks role inspection",
    },
    {
        "name": "Aggregator to employer ATS",
        "signals": "Google Jobs, Glassdoor, Runway, or remote-board listing offers multiple apply/provider links",
        "actions": (
            "choose the most direct company or ATS apply link, avoid sponsored/profile/setup flows, "
            "then classify the destination as ATS/application page"
        ),
        "result": "do not output RESULT:APPLIED until an employer/ATS confirmation page appears",
    },
    {
        "name": "Native easy apply",
        "signals": "Indeed Apply, LinkedIn Easy Apply, or ZipRecruiter one-click apply is available",
        "actions": (
            "use native apply only for the exact role and current tailored resume; "
            "reject public-profile setup, assessments, or unrelated talent-network flows"
        ),
        "result": "RESULT:APPLIED only after native apply confirms submission for this role",
    },
    {
        "name": "External ATS form",
        "signals": "Greenhouse, Lever, Ashby, SmartRecruiters, iCIMS, Taleo, Workable, Jobvite, BambooHR, or company career form",
        "actions": (
            "fill required fields, upload tailored resume, paste/upload cover letter when asked, "
            "answer screening questions from profile facts, review visible values before submit"
        ),
        "result": "RESULT:APPLIED only after confirmation; otherwise use the specific failure code",
    },
)


def _compact_list(values: list[str], empty: str = "none configured") -> str:
    """Render a compact comma-separated list for prompt context."""
    cleaned = [v for v in values if v]
    return ", ".join(cleaned) if cleaned else empty


def _build_source_catalog(search_config: dict) -> str:
    """Build a prompt-visible catalog from discovery configuration."""
    board_codes = search_config.get("boards") or search_config.get("sites") or []
    jobspy_boards = [
        _BOARD_LABELS.get(str(code).lower(), str(code))
        for code in board_codes
    ]
    discovery_mode = str(search_config.get("discovery_mode", "hybrid"))
    if config.uses_direct_source_mode(search_config):
        jobspy_label = "skipped in direct_sources mode"
    else:
        jobspy_label = _compact_list(jobspy_boards)
    direct_ats_sources = [
        str(source.get("name") or source.get("slug") or source.get("url"))
        for source in search_config.get("direct_ats_sources", []) or []
        if source.get("name") or source.get("slug") or source.get("url")
    ]

    sites_cfg = config.load_sites_config()
    direct_sources: list[str] = []
    searchable_sources: list[str] = []
    static_sources: list[str] = []
    for site in sites_cfg.get("sites", []):
        name = site.get("name")
        if not name:
            continue
        if site.get("direct_source") is True:
            direct_sources.append(name)
        if site.get("type") == "search":
            searchable_sources.append(name)
        else:
            static_sources.append(name)

    blocked_cfg = sites_cfg.get("blocked", {})
    blocked_sites = blocked_cfg.get("sites", [])
    blocked_patterns = blocked_cfg.get("url_patterns", [])
    manual_ats = sites_cfg.get("manual_ats", [])

    return f"""Configured discovery and routing catalog:
- Discovery mode: {discovery_mode}
- Configured direct ATS sources from searches.yaml: {_compact_list(direct_ats_sources)}
- Direct employer/ATS sources from sites.yaml: {_compact_list(direct_sources)}
- JobSpy boards from searches.yaml: {jobspy_label}
- Searchable smart-extract sources from sites.yaml: {_compact_list(searchable_sources)}
- Static/fresh-role smart-extract sources from sites.yaml: {_compact_list(static_sources)}
- Manual-only ATS domains: {_compact_list(manual_ats)}
- Blocked/problematic source names: {_compact_list(blocked_sites)}
- Blocked/problematic URL patterns: {_compact_list(blocked_patterns)}

If the current page is one of the configured discovery sources above, use it to find the employer apply link. Do not confuse saving, matching, filtering, or profile setup on a discovery source with submitting an application."""


def _build_jobspy_board_rules(search_config: dict) -> str:
    """Build board-specific execution rules for configured JobSpy boards."""
    board_codes = search_config.get("boards") or search_config.get("sites") or []
    rules: list[str] = []
    seen: set[str] = set()
    for code in board_codes:
        key = str(code).lower()
        if key in seen:
            continue
        seen.add(key)
        rule = _JOBSPY_BOARD_RULES.get(key)
        if rule:
            rules.append(f"- {rule}")
    if not rules:
        return "Configured JobSpy board rules: none configured."
    return "Configured JobSpy board rules:\n" + "\n".join(rules)


def _build_training_scenarios() -> str:
    """Build concrete offline scenarios for the apply agent prompt."""
    lines = ["== TRAINING SCENARIOS =="]
    for idx, scenario in enumerate(_TRAINING_SCENARIOS, start=1):
        lines.extend([
            f"{idx}. {scenario['name']}",
            f"   Signals: {scenario['signals']}",
            f"   Actions: {scenario['actions']}",
            f"   Expected result: {scenario['result']}",
        ])
    return "\n".join(lines)


def build_training_manifest(search_config: dict | None = None) -> dict:
    """Build machine-readable training coverage metadata for each run."""
    if search_config is None:
        search_config = config.load_search_config()

    sites_cfg = config.load_sites_config()
    board_codes = search_config.get("boards") or search_config.get("sites") or []
    jobspy_boards = [
        {
            "code": str(code),
            "label": _BOARD_LABELS.get(str(code).lower(), str(code)),
            "has_rule": str(code).lower() in _JOBSPY_BOARD_RULES,
        }
        for code in board_codes
    ]
    direct_sources = [
        {
            "name": site.get("name"),
            "type": site.get("type", "static"),
            "url": site.get("url"),
            "source_kind": site.get("source_kind", "direct_ats"),
        }
        for site in sites_cfg.get("sites", [])
        if site.get("name") and site.get("direct_source") is True
    ]
    direct_ats_sources = [
        {
            "name": source.get("name") or source.get("slug"),
            "url": source.get("url"),
            "ats": source.get("ats"),
            "slug": source.get("slug"),
        }
        for source in search_config.get("direct_ats_sources", []) or []
        if source.get("name") or source.get("url")
    ]
    smart_sources = [
        {
            "name": site.get("name"),
            "type": site.get("type", "static"),
            "url": site.get("url"),
        }
        for site in sites_cfg.get("sites", [])
        if site.get("name")
    ]

    return {
        "version": "apply-training-v1",
        "discovery_mode": search_config.get("discovery_mode", "hybrid"),
        "direct_source_focus": config.uses_direct_source_mode(search_config),
        "required_capabilities": [
            "workday_application_flow",
            "email_only_local_draft",
            "runway_fresh_role_discovery",
            "aggregator_to_employer_ats_handoff",
            "native_easy_apply_boundary",
            "external_ats_form_completion",
            "configured_job_board_catalog",
        ],
        "scenario_names": [scenario["name"] for scenario in _TRAINING_SCENARIOS],
        "jobspy_boards": jobspy_boards,
        "direct_ats_sources": direct_ats_sources,
        "direct_sources": direct_sources,
        "smart_extract_sources": smart_sources,
        "manual_ats_domains": sites_cfg.get("manual_ats", []),
        "blocked_sources": sites_cfg.get("blocked", {}),
        "email_draft_artifact": "email_application_draft.md",
        "runway_url": "https://app.joinrunway.io/explore",
        "result_codes": {
            "submitted": "RESULT:APPLIED",
            "email_draft": "RESULT:EMAIL_DRAFT",
            "expired": "RESULT:EXPIRED",
            "captcha": "RESULT:CAPTCHA",
            "login_issue": "RESULT:LOGIN_ISSUE",
            "sso_required": "RESULT:FAILED:sso_required",
            "not_a_job_application": "RESULT:FAILED:not_a_job_application",
            "generic_failure": "RESULT:FAILED:reason",
        },
    }


def _build_job_board_playbook(search_config: dict | None = None) -> str:
    """Build board/ATS-specific navigation instructions for the apply agent."""
    if search_config is None:
        search_config = config.load_search_config()
    source_catalog = _build_source_catalog(search_config)
    board_rules = _build_jobspy_board_rules(search_config)

    return f"""== JOB BOARD AND ATS PLAYBOOK ==
{source_catalog}

{board_rules}

{_SMART_SOURCE_RULES}

{_SCENARIO_DRILLS}

First classify the page you are on:
- Discovery/job-board page: Runway, Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs, Dice, Otta, Wellfound, BuiltIn, RemoteOK, WeWorkRemotely, or any configured source in the catalog. These pages help find jobs; they are not proof of an application submission unless they have a native Easy Apply flow. Open the role, identify the employer application link, and continue on the employer/ATS page.
- ATS/application page: Workday, Greenhouse, Lever, Ashby, SmartRecruiters, iCIMS, Taleo, Workable, Jobvite, BambooHR, or a company career site. Fill the actual application there.

Runway (https://app.joinrunway.io/explore):
- Treat Runway as a job-discovery source. Use filters, fresh-role tables, match scores, and job detail links to find a role.
- Do not treat "Save", "Get Match Scores", or Runway signup/profile prompts as an application submission.
- If Runway opens or links to an employer application, continue on that employer URL and apply there.
- If Runway requires login only to inspect recommendations and no credentials are available in the profile, output RESULT:LOGIN_ISSUE. Do not create an unrelated profile.

Workday:
- Workday is a multi-page ATS. Expect sign-in/create-account, resume upload/parser, My Information, My Experience, Application Questions, voluntary disclosures, and Review pages.
- Prefer "Apply Manually", "Apply", or "Start Application" over "Use My Last Application" unless the page makes reuse mandatory.
- Upload the tailored resume first, wait for parsing to finish, then inspect every parser-filled field because Workday often guesses wrong.
- Use the Review page as the final verification gate. Expand each section if Workday hides parsed fields behind accordions.
- Continue with Next/Save and Continue until the Review page. On Review, snapshot and verify required fields before final submit.
- If Workday says the candidate already applied, output RESULT:FAILED:already_applied.

Greenhouse/Lever/Ashby/SmartRecruiters:
- These usually have a single form with optional resume parsing and custom questions. Fill required text fields, upload resume, paste/upload cover letter only when asked, then review visible values before submit.

Indeed/LinkedIn/native Easy Apply:
- Use native easy apply only if it is clearly a direct job application for this role. If it asks to build a public profile, take assessments, or set marketplace availability, stop with RESULT:FAILED:not_a_job_application."""


def _build_captcha_section() -> str:
    """Build CAPTCHA detection and fail-closed instructions."""
    return """== CAPTCHA ==
CAPTCHA and anti-bot challenges are fail-closed states. Do not use solver APIs, token injection,
browser fingerprint workarounds, or manual puzzle attempts.

Run CAPTCHA DETECT after navigation, Apply/Submit/Login clicks, or when a page appears stuck.
If any CAPTCHA, anti-bot challenge, or challenge iframe is detected, output RESULT:CAPTCHA.

--- CAPTCHA DETECT ---
browser_evaluate function: () => {
  const signals = [];
  const selectors = [
    '.h-captcha',
    '[data-hcaptcha-sitekey]',
    '.g-recaptcha',
    '.cf-turnstile',
    '[data-turnstile-sitekey]',
    '#FunCaptcha',
    '[data-pkey]',
    '.funcaptcha'
  ];
  for (const selector of selectors) {
    if (document.querySelector(selector)) signals.push(selector);
  }
  for (const frame of document.querySelectorAll('iframe')) {
    const src = frame.getAttribute('src') || '';
    if (/hcaptcha|recaptcha|challenges\\.cloudflare|arkoselabs|funcaptcha/i.test(src)) {
      signals.push(src);
    }
  }
  for (const script of document.querySelectorAll('script[src]')) {
    const src = script.getAttribute('src') || '';
    if (/hcaptcha|recaptcha|challenges\\.cloudflare|arkoselabs|funcaptcha/i.test(src)) {
      signals.push(src);
    }
  }
  return signals.length ? {type: 'captcha', signals, url: window.location.href} : null;
}

Result actions:
- null -> no CAPTCHA signal. Continue normally.
- any object -> stop immediately with RESULT:CAPTCHA."""


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False) -> str:
    """Build the full instruction prompt for the apply agent.

    Loads the user profile and search config internally. All personal data
    comes from the profile -- nothing is hardcoded.

    Args:
        job: Job dict from the database (must have url, title, site,
             application_url, fit_score, tailored_resume_path).
        tailored_resume: Plain-text content of the tailored resume.
        cover_letter: Optional plain-text cover letter content.
        dry_run: If True, tell the agent not to click Submit.

    Returns:
        Complete prompt string for the AI agent.
    """
    profile = config.load_profile()
    search_config = config.load_search_config()
    personal = profile["personal"]

    # --- Resolve resume PDF path ---
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    # Copy to a clean filename for upload (recruiters see the filename)
    full_name = personal["full_name"]
    name_slug = full_name.replace(" ", "_")
    dest_dir = config.APPLY_WORKER_DIR / "current"
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(upload_pdf))
    pdf_path = str(upload_pdf)

    # --- Cover letter handling ---
    cover_letter_text = cover_letter or ""
    cl_upload_path = ""
    cl_path = job.get("cover_letter_path")
    if cl_path and Path(cl_path).exists():
        cl_src = Path(cl_path)
        # Read text from .txt sibling (PDF is binary)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        # Upload must be PDF
        cl_pdf_src = cl_src.with_suffix(".pdf")
        if cl_pdf_src.exists():
            cl_upload = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy(str(cl_pdf_src), str(cl_upload))
            cl_upload_path = str(cl_upload)

    # --- Build all prompt sections ---
    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile)
    hard_rules = _build_hard_rules(profile)
    job_board_playbook = _build_job_board_playbook(search_config)
    training_scenarios = _build_training_scenarios()
    captcha_section = _build_captcha_section()

    # Cover letter fallback text
    if not cover_letter_text:
        cl_display = (
            "None available. Skip if optional. If required, do not draft or infer one "
            "inside the form; stop with RESULT:FAILED:required_cover_letter_missing."
        )
    else:
        cl_display = cover_letter_text

    # Phone digits only (for fields with country prefix)
    phone_digits = "".join(c for c in personal.get("phone", "") if c.isdigit())

    # SSO domains the agent cannot sign into (loaded from config/sites.yaml)
    from applypilot.config import load_blocked_sso
    blocked_sso = load_blocked_sso()

    # Preferred display name
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    last_name = full_name.split()[-1] if " " in full_name else ""
    display_name = f"{preferred_name} {last_name}".strip()

    # Dry-run: override submit instruction
    if dry_run:
        submit_instruction = "IMPORTANT: Do NOT click the final Submit/Apply button. Review the form, verify all fields, then output RESULT:APPLIED with a note that this was a dry run."
    else:
        submit_instruction = "BEFORE clicking Submit/Apply, take a snapshot and review EVERY field on the page. Verify all data matches the APPLICANT PROFILE and TAILORED RESUME -- name, email, phone, location, work auth, resume uploaded, cover letter if applicable. If anything is wrong or missing, fix it FIRST. Only click Submit after confirming everything is correct."

    prompt = f"""You are an autonomous job application agent. Your ONE mission: get this candidate an interview. You have all the information and tools. Think strategically. Act decisively. Submit the application.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {job.get('site', 'Unknown')}
Fit Score: {job.get('fit_score', 'N/A')}/10

== FILES ==
Resume PDF (upload this): {pdf_path}
Cover Letter PDF (upload if asked): {cl_upload_path or "N/A"}

== RESUME TEXT (use when filling text fields) ==
{tailored_resume}

== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==
{cl_display}

== APPLICANT PROFILE ==
{profile_summary}

== YOUR MISSION ==
Submit a complete, accurate application. Use the profile and resume as source data -- adapt to fit each form's format.

If something unexpected happens and these instructions do not cover it, fail closed with a specific result code. Do not broaden permissions, improvise credentials, or bypass a safety gate.

{hard_rules}

{job_board_playbook}

{training_scenarios}

== NEVER DO THESE (immediate RESULT:FAILED if encountered) ==
- NEVER grant camera, microphone, screen sharing, or location permissions. If a site requests them -> RESULT:FAILED:unsafe_permissions
- NEVER do video/audio verification, selfie capture, ID photo upload, or biometric anything -> RESULT:FAILED:unsafe_verification
- NEVER set up a freelancing profile (Mercor, Toptal, Upwork, Fiverr, Turing, etc.). These are contractor marketplaces, not job applications -> RESULT:FAILED:not_a_job_application
- NEVER agree to hourly/contract rates, availability calendars, or "set your rate" flows. You are applying for FULL-TIME salaried positions only.
- NEVER install browser extensions, download executables, or run assessment software.
- NEVER enter payment info, bank details, or SSN/SIN.
- NEVER click "Allow" on any browser permission popup. Always deny/block.
- NEVER send outbound email or create external email drafts. For email-only applications, write the local draft artifact and return RESULT:EMAIL_DRAFT.
- If the site is NOT a job application form (it's a profile builder, skills marketplace, talent network signup, coding assessment platform) -> RESULT:FAILED:not_a_job_application

{location_check}

{salary_section}

{screening_section}

== STEP-BY-STEP ==
1. browser_navigate to the job URL.
2. browser_snapshot to read the page. Then run CAPTCHA DETECT (see CAPTCHA section). If a CAPTCHA is found, output RESULT:CAPTCHA and stop.
3. LOCATION CHECK. Read the page for location info. If not eligible, output RESULT and stop.
4. Find and click the Apply button. If email-only (page says "email resume to X"):
   - Do NOT send email and do NOT create an external email draft. Outbound communication requires user review.
   - Write a local file named email_application_draft.md in the current working directory.
   - Include: To, Subject "Application for {job['title']} -- {display_name}", Attachments ["{pdf_path}"{', "' + cl_upload_path + '"' if cl_upload_path else ''}], and a 2-3 sentence factual body using the cover letter text if available.
   - Output RESULT:EMAIL_DRAFT. Done.
   After clicking Apply: browser_snapshot. Run CAPTCHA DETECT -- many sites trigger CAPTCHAs right after the Apply click. If found, output RESULT:CAPTCHA and stop.
5. Login wall?
   5a. FIRST: check the URL. If you landed on {', '.join(blocked_sso)}, or any SSO/OAuth page -> STOP. Output RESULT:FAILED:sso_required. Do NOT try to sign in to Google/Microsoft/SSO.
   5b. Check for popups. Run browser_tabs action "list". If a new tab/window appeared (login popup), switch to it with browser_tabs action "select". Check the URL there too -- if it's SSO -> RESULT:FAILED:sso_required.
   5c. Regular login form (employer's own site)? Use the configured browser-managed credential provider. Never read, print, paste into the prompt, or persist a password.
   5d. After clicking Login/Sign-in: run CAPTCHA DETECT. Login pages frequently have invisible CAPTCHAs that silently block form submissions. If found, output RESULT:CAPTCHA and stop.
   5e. Sign in failed? Stop with RESULT:LOGIN_ISSUE. Do not invent or export credentials.
   5f. Need email verification, MFA, passkey, or SSO? Stop with RESULT:LOGIN_ISSUE.
   5g. After login, run browser_tabs action "list" again. Switch back to the application tab if needed.
   5h. All failed? Output RESULT:LOGIN_ISSUE. Do not loop.
6. Upload resume. ALWAYS upload fresh -- delete any existing resume first, then browser_file_upload with the PDF path above. This is the tailored resume for THIS job. Non-negotiable.
7. Upload cover letter if there's a field for it. Text field -> paste the cover letter text. File upload -> use the cover letter PDF path.
8. Check ALL pre-filled fields. ATS systems parse your resume and auto-fill -- it's often WRONG.
   - "Current Job Title" or "Most Recent Title" -> use the title from the TAILORED RESUME summary, NOT whatever the parser guessed.
   - Compare every other field to the APPLICANT PROFILE. Fix mismatches. Fill empty fields.
9. Answer screening questions using the rules above.
10. {submit_instruction}
11. After submit: browser_snapshot. Run CAPTCHA DETECT -- submit buttons often trigger invisible CAPTCHAs. If found, output RESULT:CAPTCHA and stop. Then check for new tabs (browser_tabs action: "list"). Switch to newest, close old. Snapshot to confirm submission. Look for employer/ATS confirmation, not generic success text alone.
12. Output your result.

== RESULT CODES (output EXACTLY one) ==
RESULT:APPLIED -- submitted successfully
RESULT:EMAIL_DRAFT -- email-only application needs user-reviewed outbound email; draft saved to email_application_draft.md
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- blocked by unsolvable captcha
RESULT:LOGIN_ISSUE -- could not sign in or create account
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:reason -- any other failure (brief reason)

== BROWSER EFFICIENCY ==
- browser_snapshot ONCE per page to understand it. Then use browser_take_screenshot to check results (10x less memory).
- Only snapshot again when you need element refs to click/fill.
- Multi-page forms (Workday, Taleo, iCIMS): snapshot each new page, fill all fields, click Next/Continue. Repeat until final review page.
- Fill ALL fields in ONE browser_fill_form call. Not one at a time.
- Keep your thinking SHORT. Don't repeat page structure back.
- CAPTCHA AWARENESS: After any navigation, Apply/Submit/Login click, or when a page feels stuck -- run CAPTCHA DETECT (see CAPTCHA section). Invisible CAPTCHAs (Turnstile, reCAPTCHA v3) show NO visual widget but block form submissions silently. The detect script finds them even when invisible.

== FORM TRICKS ==
- Popup/new window opened? browser_tabs action "list" to see all tabs. browser_tabs action "select" with the tab index to switch. ALWAYS check for new tabs after clicking login/apply/sign-in buttons.
- "Upload your resume" pre-fill page (Workday, Lever, etc.): This is NOT the application form yet. Click "Select file" or the upload area, then browser_file_upload with the resume PDF path. Wait for parsing to finish. Then click Next/Continue to reach the actual form.
- File upload not working? Try: (1) browser_click the upload button/area, (2) browser_file_upload with the path. If still failing, look for a hidden file input or a "Select file" link and click that first.
- Dropdown won't fill? browser_click to open it, then browser_click the option.
- Checkbox won't check via fill_form? Use browser_click on it instead. Snapshot to verify.
- Phone field with country prefix: just type digits {phone_digits}
- Date fields: {datetime.now().strftime('%m/%d/%Y')}
- Validation errors after submit? Take BOTH snapshot AND screenshot. Snapshot shows text errors, screenshot shows red-highlighted fields. Fix all, retry.
- Honeypot fields (hidden, "leave blank"): skip them.
- Format-sensitive fields: read the placeholder text, match it exactly.

{captcha_section}

== WHEN TO GIVE UP ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck
- Job is closed/expired/page says "no longer accepting" -> RESULT:EXPIRED
- Page is broken/500 error/blank -> RESULT:FAILED:page_error
Stop immediately. Output your RESULT code. Do not loop."""

    return prompt
