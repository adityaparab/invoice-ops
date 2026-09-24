"""List failed invoice runs and idempotently redrive one after operator review."""

import argparse
import asyncio
import json
import sys
from uuid import UUID

from invoiceops_agent.graph.retry import RetryConfig
from invoiceops_agent.graph.runtime import InvoiceRuntimeSettings, runtime_connection
from invoiceops_agent.graph.worker import PostgresAttemptStore


async def command_list() -> str:
    settings = InvoiceRuntimeSettings()
    store = PostgresAttemptStore(
        lambda: runtime_connection(settings.postgres_dsn.get_secret_value()), RetryConfig()
    )
    items = await store.list_dead_letters()
    return json.dumps([item.model_dump(mode="json") for item in items], sort_keys=True)


async def command_redrive(run_id: UUID, *, key: str, actor_id: str, reason: str) -> str:
    settings = InvoiceRuntimeSettings()
    store = PostgresAttemptStore(
        lambda: runtime_connection(settings.postgres_dsn.get_secret_value()), RetryConfig()
    )
    changed = await store.redrive(run_id, key=key, actor_id=actor_id, reason=reason)
    return json.dumps({"run_id": str(run_id), "redriven": changed}, sort_keys=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or redrive invoice dead letters")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    redrive = commands.add_parser("redrive")
    redrive.add_argument("run_id", type=UUID)
    redrive.add_argument("--idempotency-key", required=True)
    redrive.add_argument("--actor-id", required=True)
    redrive.add_argument("--reason", required=True)
    args = parser.parse_args()
    try:
        if args.command == "list":
            output = asyncio.run(command_list())
        else:
            output = asyncio.run(
                command_redrive(
                    args.run_id,
                    key=args.idempotency_key,
                    actor_id=args.actor_id,
                    reason=args.reason,
                )
            )
    except Exception as error:
        sys.stderr.write(f"invoice_dlq_failed error_type={type(error).__name__}\n")
        raise SystemExit(1) from None
    sys.stdout.write(output + "\n")


if __name__ == "__main__":
    main()
