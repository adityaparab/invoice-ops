"""Smoke checks for the installed package's architectural entry points."""

from importlib import import_module
from importlib.resources import files

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "component",
    ["api", "graph", "graph.nodes", "agents", "tools", "ledger", "gateway_client", "obs"],
)
def test_architecture_component_is_importable(component: str) -> None:
    module_name = f"invoiceops_agent.{component}"

    assert import_module(module_name).__name__ == module_name


def test_installed_package_declares_inline_types() -> None:
    assert files("invoiceops_agent").joinpath("py.typed").is_file()


@pytest.mark.parametrize(
    "name", ["extract_v2.md", "extract_v3.md", "extract_v4.md", "extract_repair_v1.md"]
)
def test_extraction_prompt_resources_are_packaged(name: str) -> None:
    prompt = files("invoiceops_agent.prompts").joinpath(name).read_text(encoding="utf-8")
    assert "JSON" in prompt and "confidence" in prompt
