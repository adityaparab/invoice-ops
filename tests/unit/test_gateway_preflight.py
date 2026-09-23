"""Offline checks for selected-file gateway startup validation."""

from __future__ import annotations

import importlib.util
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[2]


class PreflightModule(Protocol):
    def environment_references(self, configuration: object) -> frozenset[str]: ...

    def validate_environment(
        self, configuration: object, environment: Mapping[str, str]
    ) -> None: ...

    def main(
        self,
        argv: Sequence[str] | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        execute: Mock,
    ) -> int: ...


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gateway_preflight", _ROOT / "deploy" / "litellm" / "preflight.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def preflight() -> PreflightModule:
    return cast(PreflightModule, _load_module())


def test_recognizes_only_complete_value_references(preflight: PreflightModule) -> None:
    configuration = {
        "os.environ/KEY_NAME_IS_NOT_A_VALUE": "literal",
        "models": [
            {"key": "os.environ/API_KEY", "url": "os.environ/BASE_URL"},
            {"key": "os.environ/API_KEY"},
        ],
        "examples": ["set os.environ/NOT_A_REFERENCE", "os.environ/INVALID NAME", 42, None],
    }

    assert preflight.environment_references(configuration) == frozenset({"API_KEY", "BASE_URL"})


def test_recursive_yaml_aliases_do_not_loop(preflight: PreflightModule) -> None:
    recursive: list[object] = ["os.environ/API_KEY"]
    recursive.append(recursive)

    assert preflight.environment_references(recursive) == frozenset({"API_KEY"})


@pytest.mark.parametrize("value", [None, "", " \t\n"])
def test_missing_and_blank_values_block_execution_without_secrets(
    preflight: PreflightModule,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    value: str | None,
) -> None:
    config = tmp_path / "selected.yaml"
    config.write_text("api_key: os.environ/API_KEY\nbase: os.environ/BASE_URL\n")
    environment = {"BASE_URL": "https://secret-host.example.invalid/private-token"}
    if value is not None:
        environment["API_KEY"] = value
    execute = Mock()

    assert preflight.main(["--config", str(config)], environment=environment, execute=execute) == 1

    execute.assert_not_called()
    assert json.loads(caplog.records[-1].message) == {
        "event": "gateway.preflight_failed",
        "error": "environment_missing",
        "variables": ["API_KEY"],
    }
    assert "secret-host" not in caplog.text
    assert "private-token" not in caplog.text


def test_valid_selected_file_preserves_cli_and_ignores_other_configurations(
    preflight: PreflightModule, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    selected = tmp_path / "selected.yaml"
    selected.write_text("api_key: os.environ/NATIVE_KEY\n")
    (tmp_path / "unused.yaml").write_text("api_key: os.environ/UNSET_PRODUCTION_KEY\n")
    arguments = ["--host", "0.0.0.0", f"--config={selected}", "--port", "4000"]
    execute = Mock()

    with caplog.at_level(logging.INFO):
        result = preflight.main(
            arguments, environment={"NATIVE_KEY": "secret-native-value"}, execute=execute
        )

    assert result == 0
    execute.assert_called_once_with("litellm", ["litellm", *arguments])
    assert json.loads(caplog.records[-1].message) == {
        "event": "gateway.preflight_passed",
        "required_variables": 1,
    }
    assert "secret-native-value" not in caplog.text


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("api_key: [secret-parser-value", "config_invalid_yaml"),
        ("- secret-parser-value\n", "config_root_invalid"),
        ("", "config_root_invalid"),
    ],
)
def test_bad_yaml_blocks_execution_and_sanitizes_parser_errors(
    preflight: PreflightModule,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    text: str,
    error: str,
) -> None:
    config = tmp_path / "selected.yaml"
    config.write_text(text)
    execute = Mock()

    assert preflight.main(["--config", str(config)], environment={}, execute=execute) == 1

    execute.assert_not_called()
    assert json.loads(caplog.records[-1].message)["error"] == error
    assert "secret-parser-value" not in caplog.text


def test_unreadable_file_reports_safe_error(
    preflight: PreflightModule, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    execute = Mock()

    result = preflight.main(
        ["--config", str(tmp_path / "missing-secret-path.yaml")], environment={}, execute=execute
    )

    assert result == 1
    execute.assert_not_called()
    assert json.loads(caplog.records[-1].message)["error"] == "config_unreadable"
    assert "missing-secret-path" not in caplog.text


def test_missing_config_argument_reports_structured_error(
    preflight: PreflightModule, caplog: pytest.LogCaptureFixture
) -> None:
    execute = Mock()

    assert preflight.main(["--port", "4000"], environment={}, execute=execute) == 1

    execute.assert_not_called()
    assert json.loads(caplog.records[-1].message)["error"] == "arguments_invalid"


def test_missing_executable_reports_safe_error(
    preflight: PreflightModule, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = tmp_path / "selected.yaml"
    config.write_text("model_list: []\n")
    execute = Mock(side_effect=OSError("secret-system-message"))

    assert preflight.main(["--config", str(config)], environment={}, execute=execute) == 1

    assert json.loads(caplog.records[-1].message)["error"] == "execution_failed"
    assert "secret-system-message" not in caplog.text


@pytest.mark.parametrize(
    ("filename", "required"),
    [
        (
            "config.yaml",
            {"LITELLM_MASTER_KEY", "NATIVE_LITELLM_BASE_URL", "NATIVE_LITELLM_API_KEY"},
        ),
        ("config.prod.yaml", {"LITELLM_MASTER_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY"}),
        (
            "config.ollama.yaml",
            {
                "LITELLM_MASTER_KEY",
                "OLLAMA_BASE_URL",
                "OLLAMA_EXTRACT_MODEL",
                "OLLAMA_TRIAGE_MODEL",
                "OLLAMA_JUDGE_MODEL",
                "OLLAMA_EMBED_MODEL",
            },
        ),
    ],
)
def test_repository_configurations_require_only_their_provider_environment(
    preflight: PreflightModule, filename: str, required: set[str]
) -> None:
    execute = Mock()
    arguments = ["--config", str(_ROOT / "deploy" / "litellm" / filename)]

    assert preflight.main(arguments, environment={}, execute=execute) == 1
    execute.assert_not_called()
    assert (
        preflight.main(
            arguments,
            environment=dict.fromkeys(required, "synthetic-nonempty-value"),
            execute=execute,
        )
        == 0
    )
    execute.assert_called_once_with("litellm", ["litellm", *arguments])
