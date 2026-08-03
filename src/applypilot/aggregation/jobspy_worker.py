"""One-request JobSpy worker process."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any

from applypilot.aggregation.sources.jobspy import (
    JOBSPY_RESPONSE_SCHEMA,
    _write_private_json,
    validate_jobspy_request,
)

_FIELDS = (
    "id",
    "site",
    "job_url",
    "job_url_direct",
    "title",
    "company",
    "location",
    "date_posted",
    "description",
    "min_amount",
    "max_amount",
    "interval",
    "currency",
    "is_remote",
)


def _safe_value(value: Any) -> str | int | float | bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    text = str(value)
    if text.lower() in {"nan", "nat", "none"}:
        return None
    return text


def _rows(frame: Any, *, limit: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records")[:limit]:
        row = {field: _safe_value(raw.get(field)) for field in _FIELDS}
        if isinstance(row.get("description"), str):
            row["description"] = row["description"][:20_000]
        output.append(row)
    return output


def run(request_path: Path, response_path: Path) -> int:
    import json

    payload = json.loads(request_path.read_text(encoding="utf-8"))
    unit = validate_jobspy_request(payload)
    response: dict[str, Any] = {
        "schema_version": JOBSPY_RESPONSE_SCHEMA,
        "request_digest": unit.request_digest,
        "unit_id": unit.unit_id,
        "board": unit.board,
        "status": "error",
        "rows": [],
        "error_class": "",
    }
    try:
        from jobspy import scrape_jobs

        kwargs: dict[str, Any] = {
            "site_name": [unit.board],
            "search_term": unit.term,
            "location": unit.location,
            "results_wanted": unit.results_wanted,
            "hours_old": unit.hours_old,
            "description_format": "markdown",
            "verbose": 0,
            "linkedin_fetch_description": False,
        }
        if unit.board == "google":
            kwargs["google_search_term"] = unit._unsigned_request()["google_search_term"]
        if unit.board == "indeed":
            kwargs["country_indeed"] = unit.country_indeed
        rows = _rows(scrape_jobs(**kwargs), limit=unit.results_wanted)
        response["rows"] = rows
        response["status"] = "complete" if rows else "empty"
    except Exception as exc:
        response["status"] = "error"
        response["error_class"] = type(exc).__name__[:120]
    _write_private_json(response_path, response)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    return run(args.request.resolve(strict=True), args.response.resolve())


if __name__ == "__main__":
    raise SystemExit(main())

