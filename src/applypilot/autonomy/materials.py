"""Evidence-bound role-specific resume artifacts.

The canonical resume transformer never rewrites applicant claims.  It may only
reorder existing bullet lines within their original entry, using the verified
job text as a relevance signal.  The provenance artifact proves that output
lines are an exact multiset of source lines.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from applypilot.autonomy.models import ResumeStrategy


def build_evidence_bound_resume(
    base_text: str,
    *,
    verified_job_text: str,
    resume_strategy: ResumeStrategy | None = None,
    evidence_by_id: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    source_lines = base_text.splitlines()
    job_tokens = _tokens(verified_job_text)
    strategy = resume_strategy or ResumeStrategy()
    evidence_by_id = evidence_by_id or {}
    priority_terms = {
        token
        for term in strategy.priority_job_terms
        for token in _tokens(term)
    }
    priority_evidence_tokens = {
        token
        for evidence_id in strategy.priority_evidence_ids
        for token in _tokens(evidence_by_id.get(evidence_id, ""))
    }
    output_lines = list(source_lines)

    index = 0
    while index < len(output_lines):
        if not _is_bullet(output_lines[index]):
            index += 1
            continue
        end = index + 1
        while end < len(output_lines) and _is_bullet(output_lines[end]):
            end += 1
        original_run = output_lines[index:end]
        ranked_run = sorted(
            enumerate(original_run),
            key=lambda item: (
                -_line_relevance(
                    item[1],
                    job_tokens=job_tokens,
                    priority_terms=priority_terms,
                    priority_evidence_tokens=priority_evidence_tokens,
                ),
                item[0],
            ),
        )
        output_lines[index:end] = [line for _, line in ranked_run]
        index = end

    if Counter(source_lines) != Counter(output_lines):
        raise ValueError("role resume changed applicant claim text")
    output_text = "\n".join(output_lines)
    if base_text.endswith("\n"):
        output_text += "\n"
    provenance = {
        "schema_version": "applypilot-evidence-resume-v1",
        "source_sha256": _sha256(base_text),
        "verified_job_sha256": _sha256(verified_job_text),
        "output_sha256": _sha256(output_text),
        "source_line_count": len(source_lines),
        "output_line_count": len(output_lines),
        "claims_rewritten": False,
        "claims_added": False,
        "line_multiset_preserved": True,
        "line_order_changed": source_lines != output_lines,
        "resume_strategy": {
            "priority_evidence_ids": list(strategy.priority_evidence_ids),
            "priority_job_terms": list(strategy.priority_job_terms),
            "applied": bool(priority_terms or priority_evidence_tokens),
        },
    }
    return output_text, provenance


def write_evidence_bound_resume(
    *,
    base_path: Path,
    output_dir: Path,
    verified_job_text: str,
    resume_strategy: ResumeStrategy | None = None,
    evidence_by_id: dict[str, str] | None = None,
    render_pdf: bool = True,
) -> dict[str, str]:
    """Write text, provenance, HTML, and best-effort PDF resume artifacts."""
    base_text = base_path.read_text(encoding="utf-8")
    output_text, provenance = build_evidence_bound_resume(
        base_text,
        verified_job_text=verified_job_text,
        resume_strategy=resume_strategy,
        evidence_by_id=evidence_by_id,
    )
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    text_path = output_dir / "resume_evidence_bound.txt"
    provenance_path = output_dir / "resume_provenance.json"
    html_path = output_dir / "resume_evidence_bound.html"
    text_path.write_text(output_text, encoding="utf-8")
    provenance_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(_resume_html(output_text), encoding="utf-8")
    for path in (text_path, provenance_path, html_path):
        path.chmod(0o600)
    paths = {
        "resume_text": str(text_path),
        "resume_html": str(html_path),
        "resume_provenance": str(provenance_path),
    }
    if render_pdf:
        pdf_path = output_dir / "resume_evidence_bound.pdf"
        try:
            from applypilot.scoring.pdf import render_pdf as render_html_pdf

            render_html_pdf(html_path.read_text(encoding="utf-8"), str(pdf_path))
        except Exception:
            pdf_path.unlink(missing_ok=True)
        else:
            if pdf_path.is_file() and pdf_path.stat().st_size:
                pdf_path.chmod(0o600)
                paths["resume_pdf"] = str(pdf_path)
    return paths


def _resume_html(text: str) -> str:
    escaped = html.escape(text)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
@page {{ size: letter; margin: 0.35in 0.45in; }}
body {{ margin: 0; color: #111; font-family: Arial, Helvetica, sans-serif; }}
pre {{ white-space: pre-wrap; font-family: Arial, Helvetica, sans-serif;
       font-size: 8.6pt; line-height: 1.22; margin: 0; }}
</style></head><body><pre>{escaped}</pre></body></html>"""


def _is_bullet(line: str) -> bool:
    return bool(re.match(r"^\s*[•*\-]", line))


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9+#.-]{1,}", value.lower())
        if token not in {"and", "for", "from", "the", "this", "with"}
    }


def _line_relevance(
    line: str,
    *,
    job_tokens: set[str],
    priority_terms: set[str],
    priority_evidence_tokens: set[str],
) -> int:
    line_tokens = _tokens(line)
    return (
        len(line_tokens & job_tokens)
        + 3 * len(line_tokens & priority_terms)
        + 2 * len(line_tokens & priority_evidence_tokens)
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
