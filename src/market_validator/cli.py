"""Command-line diagnostics for Market Validator."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import asdict
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from market_validator.backends.codex_plus import CodexPlusBackend
from market_validator.backends.config import (
    BackendConfigurationError,
    get_selected_backend_name,
)
from market_validator.backends.deepseek_api import DeepSeekApiBackend
from market_validator.data.cli import add_data_parser, handle_data_command
from market_validator.data_plan_cli import (
    add_data_plan_parsers,
    handle_data_plan_cli_command,
)
from market_validator.hypothesis_cli import (
    add_hypothesis_parsers,
    handle_hypothesis_cli_command,
)
from market_validator.research.models import ResearchSpec
from market_validator.workflow_cli import (
    WorkflowCliExitCode,
    add_workflow_parsers,
    emit_error,
    handle_workflow_cli_command,
)


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        emit_error("cli_input_error", message, "argument_parsing")
        raise SystemExit(int(WorkflowCliExitCode.CLI_INPUT_ERROR))


def _build_parser() -> argparse.ArgumentParser:
    parser = _StableArgumentParser(prog="market-validator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="check that the scaffold is runnable")
    subparsers.add_parser("backends", help="show offline-safe backend status")
    spec_parser = subparsers.add_parser("spec", help="inspect or validate ResearchSpec")
    spec_commands = spec_parser.add_subparsers(dest="spec_command", required=True)
    spec_commands.add_parser("schema", help="print the ResearchSpec JSON Schema")
    validate_parser = spec_commands.add_parser(
        "validate", help="validate a ResearchSpec JSON file"
    )
    validate_parser.add_argument("path", type=Path)
    lifecycle_parser = subparsers.add_parser(
        "data-lifecycle",
        help="unified JSON-only data lifecycle CLI (v0.3.0)",
    )
    lifecycle_parser.add_argument(
        "lifecycle_args",
        nargs=argparse.REMAINDER,
        help="subcommand arguments passed to the data-lifecycle CLI",
    )
    add_data_parser(subparsers)
    add_workflow_parsers(subparsers)
    add_hypothesis_parsers(subparsers)
    add_data_plan_parsers(subparsers)
    from market_validator.research_agent_cli import build_research_parser

    build_research_parser(subparsers)
    return parser


def _error_path(location: tuple[object, ...]) -> str:
    if not location:
        return "$"
    parts: list[str] = []
    for item in location:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        elif parts:
            parts.append(f".{item}")
        else:
            parts.append(str(item))
    return "".join(parts)


def _validation_error_payload(error: ValidationError) -> dict[str, object]:
    errors = []
    for detail in error.errors(
        include_url=False, include_context=False, include_input=False
    ):
        errors.append(
            {
                "path": _error_path(detail["loc"]),
                "code": detail["type"],
                "message": detail["msg"],
            }
        )
    return {"valid": False, "errors": errors}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface and return a process exit code."""
    args = _build_parser().parse_args(argv)

    if args.command == "research":
        from market_validator.research_agent_cli import main as research_main

        return research_main(argv[1:])
    if args.command == "data-lifecycle":
        from market_validator import data_lifecycle_cli

        return data_lifecycle_cli.main(args.lifecycle_args)

    if args.command == "doctor":
        print(json.dumps({"status": "ok", "stage": "scaffold"}, sort_keys=True))
        return 0

    if args.command == "backends":
        try:
            selected = get_selected_backend_name()
        except BackendConfigurationError as error:
            print(
                json.dumps({"status": "error", "error": str(error)}, sort_keys=True),
                file=sys.stderr,
            )
            return 2

        statuses = {
            "codex_plus": asdict(CodexPlusBackend().status()),
            "deepseek_api": asdict(DeepSeekApiBackend().status()),
        }
        print(
            json.dumps(
                {"selected_backend": selected, "backends": statuses}, sort_keys=True
            )
        )
        return 0

    if args.command == "spec":
        if args.spec_command == "schema":
            print(json.dumps(ResearchSpec.model_json_schema(), sort_keys=True))
            return 0

        if args.spec_command == "validate":
            try:
                raw_spec = args.path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                print(
                    json.dumps(
                        {
                            "valid": False,
                            "errors": [
                                {
                                    "path": str(args.path),
                                    "code": "file_error",
                                    "message": str(error),
                                }
                            ],
                        },
                        sort_keys=True,
                    )
                )
                return 2

            try:
                spec = ResearchSpec.model_validate_json(raw_spec)
            except ValidationError as error:
                print(json.dumps(_validation_error_payload(error), sort_keys=True))
                return 2

            print(
                json.dumps(
                    {
                        "valid": True,
                        "spec_id": spec.spec_id,
                        "schema_version": spec.schema_version,
                    },
                    sort_keys=True,
                )
            )
            return 0

    if args.command == "data":
        return handle_data_command(args)

    if args.command in {
        "propose-plan",
        "validate-proposal",
        "compile-plan",
        "validate-plan",
        "run",
        "verify-artifact",
    }:
        return handle_workflow_cli_command(args)

    if args.command in {"hypothesis", "propose-hypothesis"}:
        return handle_hypothesis_cli_command(args)

    if args.command == "data-plan":
        return handle_data_plan_cli_command(args)

    return 2
