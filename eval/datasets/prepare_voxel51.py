"""Prepare the public synthetic development subset; no inference or credential loading."""

import argparse
import logging
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from eval.datasets.voxel51 import PreparationError, Settings, prepare, tier_counts
from invoiceops_agent.obs.logging import configure_logging

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval/data/voxel51-v1"))
    parser.add_argument("--count", type=int, default=32)
    parser.add_argument("--seed", type=int, default=20260827)
    parser.add_argument("--max-edge", type=int, default=2400)
    args = parser.parse_args()
    configure_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings(count=args.count, seed=args.seed, max_edge=args.max_edge)
        manifest = prepare(args.output, settings)
    except (
        PreparationError,
        ValidationError,
        httpx.HTTPError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        logger.error("dataset_preparation_failed error_type=%s", type(error).__name__)
        return 1
    logger.info(
        "dataset_prepared samples=%d tiers=%s", len(manifest.samples), tier_counts(manifest)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
