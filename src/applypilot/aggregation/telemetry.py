"""Live terminal projection for aggregation lifecycle events."""

from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.table import Table

from applypilot.observability.events import EventJournal, RunEvent


def build_table(events: list[RunEvent]) -> Table:
    latest: dict[str, RunEvent] = {}
    for event in events:
        if event.source:
            latest[event.source] = event
    table = Table(title="ApplyPilot aggregation")
    table.add_column("Source")
    table.add_column("State")
    table.add_column("Observed", justify="right")
    table.add_column("Elapsed", justify="right")
    for source in sorted(latest):
        event = latest[source]
        observed = event.counts.get("observed", event.counts.get("source_observed", 0))
        table.add_row(source, event.status, str(observed), f"{event.elapsed_ms / 1000:.1f}s")
    return table


async def run_with_live(
    *,
    aggregator: Any,
    run_id: str,
    request: Any,
    journal: EventJournal,
    console: Console,
) -> dict:
    task = asyncio.create_task(aggregator.run(run_id, request))
    with Live(build_table(journal.read()), console=console, refresh_per_second=4) as live:
        while not task.done():
            live.update(build_table(journal.read()))
            await asyncio.sleep(0.25)
        snapshot = await task
        live.update(build_table(journal.read()), refresh=True)
        return snapshot

