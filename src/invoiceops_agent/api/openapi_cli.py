"""Export the in-process FastAPI contract for deterministic frontend type generation."""

import argparse
import json
from pathlib import Path

from invoiceops_agent.api.app import create_app
from invoiceops_agent.api.settings import ApiSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the InvoiceOps OpenAPI contract")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    schema = create_app(ApiSettings(_env_file=None)).openapi()
    args.output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
