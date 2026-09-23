"""Validate the selected proxy configuration before replacing this process with LiteLLM."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NoReturn

import yaml

_ENVIRONMENT_REFERENCE = re.compile(r"os\.environ/([A-Za-z_][A-Za-z0-9_]*)\Z")
_LOGGER = logging.getLogger(__name__)
type Executor = Callable[[str, list[str]], None]


class PreflightError(Exception):
    """A startup failure with safe, structured diagnostic fields."""

    def __init__(self, code: str, variables: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.variables = variables


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise PreflightError("arguments_invalid")


def environment_references(configuration: object) -> frozenset[str]:
    """Find complete environment references in nested configuration values."""
    references: set[str] = set()
    pending = [configuration]
    visited: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            match = _ENVIRONMENT_REFERENCE.fullmatch(value)
            if match is not None:
                references.add(match.group(1))
        elif isinstance(value, Mapping | list | tuple):
            # YAML aliases can repeat containers or make them recursive.
            identity = id(value)
            if identity in visited:
                continue
            visited.add(identity)
            pending.extend(value.values() if isinstance(value, Mapping) else value)
    return frozenset(references)


def validate_environment(configuration: object, environment: Mapping[str, str]) -> None:
    """Reject missing or whitespace-only values without exposing their contents."""
    missing = tuple(
        sorted(
            name
            for name in environment_references(configuration)
            if not environment.get(name, "").strip()
        )
    )
    if missing:
        raise PreflightError("environment_missing", missing)


def load_configuration(path: Path) -> object:
    """Read a YAML mapping while keeping parser and filesystem errors out of logs."""
    try:
        configuration: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise PreflightError("config_unreadable") from error
    except yaml.YAMLError as error:
        raise PreflightError("config_invalid_yaml") from error
    if not isinstance(configuration, Mapping):
        raise PreflightError("config_root_invalid")
    return configuration


def _configuration_path(arguments: Sequence[str]) -> Path:
    parser = _ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--config", required=True)
    parsed, _ = parser.parse_known_args(arguments)
    return Path(parsed.config)


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    execute: Executor = os.execvp,
) -> int:
    """Check the selected file, then preserve all arguments when starting LiteLLM."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    arguments = list(sys.argv[1:] if argv is None else argv)
    effective_environment = os.environ if environment is None else environment
    try:
        configuration = load_configuration(_configuration_path(arguments))
        validate_environment(configuration, effective_environment)
    except PreflightError as error:
        _LOGGER.error(
            json.dumps(
                {
                    "event": "gateway.preflight_failed",
                    "error": error.code,
                    "variables": error.variables,
                }
            )
        )
        return 1
    _LOGGER.info(
        json.dumps(
            {
                "event": "gateway.preflight_passed",
                "required_variables": len(environment_references(configuration)),
            }
        )
    )
    try:
        execute("litellm", ["litellm", *arguments])
    except OSError:
        _LOGGER.error(
            json.dumps({"event": "gateway.preflight_failed", "error": "execution_failed"})
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
