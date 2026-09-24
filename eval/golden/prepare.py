"""Prepare the 500-document golden dataset and validate the committed report."""

import argparse
import json
import logging
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from eval.datasets.voxel51 import BASE_URL, MAX_METADATA_BYTES, fetch_bytes
from eval.golden.builder import GoldenBuildError, build
from eval.golden.schema import BaselineReport
from invoiceops_agent.artifacts import write_new_artifact
from invoiceops_agent.obs.logging import configure_logging

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval/data/golden/v1.0.1"))
    parser.add_argument(
        "--metadata", type=Path, default=Path("eval/data/golden-source/samples.json")
    )
    parser.add_argument("--seed", type=int, default=20260827)
    parser.add_argument("--report-dir", type=Path, default=Path("eval/golden/v1.0.1"))
    args = parser.parse_args()
    configure_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        metadata = (
            args.metadata.read_bytes()
            if args.metadata.exists()
            else fetch_bytes(f"{BASE_URL}/samples.json", MAX_METADATA_BYTES)
        )
        baseline = BaselineReport.model_validate(
            json.loads(Path("eval/reports/voxel51-v1.json").read_text())
        )
        manifest, _ = build(args.output, metadata, baseline, seed=args.seed)
        for name in ("manifest.json", "erp.json"):
            source = (args.output / name).read_bytes()
            target = args.report_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                write_new_artifact(target, source)
            except FileExistsError:
                if target.read_bytes() != source:
                    raise GoldenBuildError("Committed golden report differs") from None
    except (
        GoldenBuildError,
        ValidationError,
        httpx.HTTPError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        logger.error("golden_preparation_failed error_type=%s", type(error).__name__)
        return 1
    logger.info("golden_prepared version=%s samples=%d", manifest.version, len(manifest.samples))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
