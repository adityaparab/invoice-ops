"""Run a bounded list of already ingested invoices through the real workflow."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter
from uuid import UUID

from invoiceops_agent.gateway_client.cassettes import AliasCassetteTransport
from invoiceops_agent.graph.invoice_cli import run_invoice

MAX_RUNS = 500


def parse_run_ids(body: str) -> tuple[UUID, ...]:
    lines = body.splitlines()
    if not 1 <= len(lines) <= MAX_RUNS:
        raise ValueError("Batch requires one to 500 run IDs")
    try:
        ids = tuple(UUID(line.strip()) for line in lines)
    except ValueError:
        raise ValueError("Batch contains an invalid run ID") from None
    if len(ids) != len(set(ids)):
        raise ValueError("Batch run IDs must be unique")
    return ids


async def run_batch(
    run_ids: tuple[UUID, ...], *, cassette_dir: Path | None = None
) -> list[dict[str, str]]:
    outcomes: list[dict[str, str]] = []
    for run_id in run_ids:
        started = perf_counter()
        transport = AliasCassetteTransport(cassette_dir) if cassette_dir else None
        try:
            result = await run_invoice(run_id, gateway_transport=transport)
        except Exception as error:
            outcome = {"run_id": str(run_id), "error_type": type(error).__name__}
        else:
            outcome = result
        outcomes.append({**outcome, "duration_ms": f"{(perf_counter() - started) * 1000:.3f}"})
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cassette-dir", type=Path)
    args = parser.parse_args()
    try:
        run_ids = parse_run_ids(sys.stdin.read())
        if args.cassette_dir is not None and len(run_ids) != 1:
            raise ValueError("Cassette smoke mode requires exactly one run")
        outcomes = asyncio.run(run_batch(run_ids, cassette_dir=args.cassette_dir))
    except ValueError as error:
        sys.stderr.write(f"invoice_batch_invalid error_type={type(error).__name__}\n")
        raise SystemExit(2) from None
    for result in outcomes:
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    if any("error_type" in result for result in outcomes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
