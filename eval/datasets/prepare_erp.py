"""Publish the deterministic ERP fixture digest and factual match labels."""

import argparse
import json
from pathlib import Path

from invoiceops_agent.artifacts import write_new_artifact
from invoiceops_agent.tools.erp_generator import DEFAULT_SEED, fixture_sha256, generate_fixture
from invoiceops_agent.tools.erp_schemas import ERPFixture

DEFAULT_OUTPUT = Path("eval/reports/synthetic-erp-v1.json")


def report_bytes(fixture: ERPFixture) -> bytes:
    report = {
        "version": fixture.version,
        "seed": fixture.seed,
        "fixture_sha256": fixture_sha256(fixture),
        "counts": {
            "vendors": len(fixture.vendors),
            "purchase_orders": len(fixture.purchase_orders),
            "goods_receipts": len(fixture.goods_receipts),
        },
        "ground_truth": [truth.model_dump(mode="json") for truth in fixture.ground_truth],
    }
    return (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    write_new_artifact(args.output, report_bytes(generate_fixture(args.seed)))


if __name__ == "__main__":
    main()
