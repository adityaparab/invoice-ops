"""Load complete packaged instructions without interpolating document or model text."""

import asyncio
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files

PROMPT_VERSION = "extract@v4"
REPAIR_PROMPT_VERSION = "extract@v4+repair@v1"


@dataclass(frozen=True)
class ExtractionPrompts:
    base: str = field(repr=False)
    repair: str = field(repr=False)


@lru_cache(maxsize=1)
def _read_prompts() -> ExtractionPrompts:
    root = files("invoiceops_agent.prompts")
    return ExtractionPrompts(
        base=root.joinpath("extract_v4.md").read_text(encoding="utf-8"),
        repair=root.joinpath("extract_repair_v1.md").read_text(encoding="utf-8"),
    )


async def load_prompts() -> ExtractionPrompts:
    return await asyncio.to_thread(_read_prompts)
