"""Stable offline-first CLI for the deterministic DataPlan lifecycle."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import stat

from market_validator.data.calendars import CalendarRegistry
from market_validator.data.data_plan_review import (
    DataPlanConfirmation,
    DataPlanReviewError,
    DataPlanReviewErrorCode,
    GeneratedDataPlan,
    calculate_data_plan_confirmation_sha256,
    confirm_data_plan,
    data_plan_confirmation_json_schema,
    data_plan_readiness_blockers,
    generate_data_plan,
    parse_data_plan_confirmation,
    persist_data_plan_confirmation,
    persist_generated_data_plan,
    validate_data_plan_confirmation_matches,
)
from market_validator.data.registry import InstrumentRegistry
from market_validator.data.serialization import (
    DataPlanSerializationError,
    calculate_data_plan_sha256,
    parse_data_plan,
)
from market_validator.research.models import ResearchSpec
from market_validator.research.serialization import (
    ResearchSpecSerializationError,
    calculate_research_spec_sha256,
    parse_research_spec,
)
from market_validator.workflow_cli import (
    WorkflowCliExitCode,
    emit_error,
    emit_success,
)

DATA_PLAN_REVIEW_ERROR_EXIT_CODES = {
    DataPlanReviewErrorCode.INVALID_DATA_PLAN: (
        WorkflowCliExitCode.INVALID_DATA_PLAN
    ),
    DataPlanReviewErrorCode.DATA_PLAN_NOT_READY: (
        WorkflowCliExitCode.DATA_PLAN_NOT_READY
    ),
    DataPlanReviewErrorCode.DATA_PLAN_MISMATCH: (
        WorkflowCliExitCode.DATA_PLAN_MISMATCH
    ),
    DataPlanReviewErrorCode.REGISTRY_MISMATCH: (
        WorkflowCliExitCode.DATA_PLAN_REGISTRY_MISMATCH
    ),
    DataPlanReviewErrorCode.INVALID_DATA_PLAN_CONFIRMATION: (
        WorkflowCliExitCode.INVALID_DATA_PLAN_CONFIRMATION
    ),
    DataPlanReviewErrorCode.DATA_PLAN_CONFIRMATION_MISMATCH: (
        WorkflowCliExitCode.DATA_PLAN_CONFIRMATION_MISMATCH
    ),
    DataPlanReviewErrorCode.OUTPUT_CONFLICT: (
        WorkflowCliExitCode.DATA_PLAN_OUTPUT_CONFLICT
    ),
    DataPlanReviewErrorCode.OUTPUT_ERROR: (
        WorkflowCliExitCode.DATA_PLAN_OUTPUT_ERROR
    ),
}


class DataPlanCliInputError(ValueError):
    def __init__(self, message: str, stage: str) -> None:
        self.message = message
        self.stage = stage
        super().__init__(message)


def add_data_plan_parsers(subparsers: argparse._SubParsersAction) -> None:
    data_plan = subparsers.add_parser(
        "data-plan",
        help="offline provider-neutral DataPlan generation and confirmation",
    )
    commands = data_plan.add_subparsers(dest="data_plan_command", required=True)

    generate = commands.add_parser(
        "generate",
        help="deterministically generate a DataPlan and provenance sidecar",
    )
    generate.add_argument("--research-spec", required=True, type=Path)
    generate.add_argument("--instrument-registry", required=True, type=Path)
    generate.add_argument("--calendar-registry", required=True, type=Path)
    generate.add_argument("--output", required=True, type=Path)
    generate.add_argument(
        "--proposal", type=Path, help="optional Proposal for provenance"
    )
    generate.add_argument(
        "--confirmation", type=Path, help="optional Proposal confirmation for provenance"
    )

    validate = commands.add_parser(
        "validate",
        help="strictly validate a DataPlan file and its canonical hash",
    )
    validate.add_argument("--plan", required=True, type=Path)

    commands.add_parser(
        "confirmation-schema",
        help="print the strict DataPlan confirmation JSON Schema",
    )
    confirm = commands.add_parser(
        "confirm",
        help="explicitly confirm one fully ready provider-neutral DataPlan",
    )
    confirm.add_argument("--plan", required=True, type=Path)
    confirm.add_argument("--research-spec", required=True, type=Path)
    confirm.add_argument("--instrument-registry", required=True, type=Path)
    confirm.add_argument("--calendar-registry", required=True, type=Path)
    confirm.add_argument("--output", required=True, type=Path)

    validate_confirmation = commands.add_parser(
        "validate-confirmation",
        help="verify a DataPlan confirmation still matches the current inputs",
    )
    validate_confirmation.add_argument("--confirmation", required=True, type=Path)
    validate_confirmation.add_argument("--plan", required=True, type=Path)
    validate_confirmation.add_argument("--research-spec", required=True, type=Path)
    validate_confirmation.add_argument("--instrument-registry", required=True, type=Path)
    validate_confirmation.add_argument("--calendar-registry", required=True, type=Path)


def _read_regular_utf8(path: Path, *, label: str) -> bytes:
    if any(part == os.pardir for part in path.parts):
        raise DataPlanCliInputError(
            f"{label} path must not contain path traversal",
            f"{label}_input",
        )
    absolute = path.absolute()
    for candidate in [absolute, *absolute.parents]:
        try:
            exists = candidate.exists()
        except OSError as exc:
            raise DataPlanCliInputError(
                f"{label} path cannot be inspected safely",
                f"{label}_input",
            ) from exc
        if exists and candidate.is_symlink():
            raise DataPlanCliInputError(
                f"{label} path must not contain symbolic links",
                f"{label}_input",
            )
    try:
        mode = path.lstat().st_mode
        resolved = path.resolve(strict=True)
        payload = resolved.read_bytes()
    except OSError as exc:
        raise DataPlanCliInputError(
            f"{label} file does not exist or cannot be read safely",
            f"{label}_input",
        ) from exc
    if not stat.S_ISREG(mode):
        raise DataPlanCliInputError(
            f"{label} path must identify a regular file",
            f"{label}_input",
        )
    try:
        payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise DataPlanCliInputError(
            f"{label} file must be UTF-8",
            f"{label}_input",
        ) from exc
    return payload


def _load_spec(path: Path) -> ResearchSpec:
    try:
        return parse_research_spec(_read_regular_utf8(path, label="research_spec"))
    except ResearchSpecSerializationError:
        raise DataPlanCliInputError(
            "research-spec file failed strict ResearchSpec validation",
            "research_spec_input",
        ) from None


def _load_registries(
    instrument_path: Path, calendar_path: Path
) -> tuple[CalendarRegistry, InstrumentRegistry]:
    try:
        calendars = CalendarRegistry.from_json_file(calendar_path)
        instruments = InstrumentRegistry.from_json_file(instrument_path, calendars)
    except Exception:
        raise DataPlanCliInputError(
            "instrument or calendar registry failed strict validation",
            "registry_input",
        ) from None
    return calendars, instruments


def _load_plan(path: Path) -> tuple[object, str]:
    try:
        plan = parse_data_plan(_read_regular_utf8(path, label="data_plan"))
    except DataPlanSerializationError:
        raise DataPlanCliInputError(
            "plan file failed strict DataPlan validation",
            "data_plan_input",
        ) from None
    return plan, calculate_data_plan_sha256(plan)


def _proposal_provenance_hashes(
    proposal_path: Path | None,
    confirmation_path: Path | None,
) -> tuple[str | None, str | None]:
    from market_validator.hypothesis import (
        calculate_research_hypothesis_confirmation_sha256,
        parse_research_hypothesis_confirmation,
        parse_research_hypothesis_proposal,
    )

    proposal_sha256 = None
    confirmation_sha256 = None
    if proposal_path is not None:
        proposal = parse_research_hypothesis_proposal(
            _read_regular_utf8(proposal_path, label="proposal")
        )
        from market_validator.hypothesis import (
            calculate_research_hypothesis_proposal_sha256,
        )

        proposal_sha256 = calculate_research_hypothesis_proposal_sha256(proposal)
    if confirmation_path is not None:
        confirmation = parse_research_hypothesis_confirmation(
            _read_regular_utf8(confirmation_path, label="proposal_confirmation")
        )
        confirmation_sha256 = calculate_research_hypothesis_confirmation_sha256(
            confirmation
        )
    return proposal_sha256, confirmation_sha256


def _handle_generate(args: argparse.Namespace) -> int:
    research_spec = _load_spec(args.research_spec)
    calendars, instruments = _load_registries(
        args.instrument_registry, args.calendar_registry
    )
    generated = generate_data_plan(research_spec, instruments, calendars)
    proposal_sha256, confirmation_sha256 = _proposal_provenance_hashes(
        args.proposal, args.confirmation
    )
    persisted = persist_generated_data_plan(
        generated,
        args.output,
        proposal_sha256=proposal_sha256,
        proposal_confirmation_sha256=confirmation_sha256,
    )
    blockers = data_plan_readiness_blockers(generated, instruments)
    emit_success(
        {
            "data_plan_path": str(persisted.data_plan_path),
            "data_plan_sha256": persisted.data_plan_sha256,
            "research_spec_sha256": generated.research_spec_sha256,
            "instrument_registry_sha256": generated.instrument_registry_sha256,
            "calendar_registry_sha256": generated.calendar_registry_sha256,
            "unresolved_instruments": generated.data_plan.unresolved_instruments,
            "ready_for_confirmation": not blockers,
            "blockers": blockers,
        }
    )
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_validate(args: argparse.Namespace) -> int:
    plan, plan_sha256 = _load_plan(args.plan)
    emit_success(
        {
            "data_plan_sha256": plan_sha256,
            "valid": True,
            "unresolved_instruments": plan.unresolved_instruments,
            "requirements": [
                item.model_dump(mode="json") for item in plan.requirements
            ],
        }
    )
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_confirmation_schema() -> int:
    emit_success(data_plan_confirmation_json_schema())
    return int(WorkflowCliExitCode.SUCCESS)


def _reload_generated(
    args: argparse.Namespace,
) -> tuple[GeneratedDataPlan, InstrumentRegistry]:
    research_spec = _load_spec(args.research_spec)
    calendars, instruments = _load_registries(
        args.instrument_registry, args.calendar_registry
    )
    return generate_data_plan(research_spec, instruments, calendars), instruments


def _handle_confirm(args: argparse.Namespace) -> int:
    plan, _ = _load_plan(args.plan)
    generated, instruments = _reload_generated(args)
    if generated.data_plan_sha256 != calculate_data_plan_sha256(plan):
        from market_validator.data.data_plan_review import (
            DataPlanReviewStage,
            fail_data_plan_review,
        )

        fail_data_plan_review(
            DataPlanReviewErrorCode.DATA_PLAN_MISMATCH,
            DataPlanReviewStage.DATA_PLAN_CONFIRMATION_VALIDATION,
            "plan file does not match the canonical DataPlan for these inputs",
        )
    confirmation = confirm_data_plan(
        generated,
        instruments,
        confirmed_at=datetime.now(timezone.utc),
    )
    output_path = persist_data_plan_confirmation(confirmation, args.output)
    emit_success(
        {
            "output_path": str(output_path),
            "data_plan_sha256": confirmation.data_plan_sha256,
            "research_spec_sha256": confirmation.research_spec_sha256,
            "instrument_registry_sha256": confirmation.instrument_registry_sha256,
            "calendar_registry_sha256": confirmation.calendar_registry_sha256,
            "confirmed": confirmation.confirmed,
        }
    )
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_validate_confirmation(args: argparse.Namespace) -> int:
    confirmation = parse_data_plan_confirmation(
        _read_regular_utf8(args.confirmation, label="data_plan_confirmation")
    )
    plan, _ = _load_plan(args.plan)
    generated, _ = _reload_generated(args)
    if generated.data_plan_sha256 != calculate_data_plan_sha256(plan):
        from market_validator.data.data_plan_review import (
            DataPlanReviewStage,
            fail_data_plan_review,
        )

        fail_data_plan_review(
            DataPlanReviewErrorCode.DATA_PLAN_MISMATCH,
            DataPlanReviewStage.DATA_PLAN_CONFIRMATION_VALIDATION,
            "plan file does not match the canonical DataPlan for these inputs",
        )
    validate_data_plan_confirmation_matches(generated, confirmation)
    emit_success(
        {
            "matches": True,
            "data_plan_sha256": confirmation.data_plan_sha256,
            "research_spec_sha256": confirmation.research_spec_sha256,
            "instrument_registry_sha256": confirmation.instrument_registry_sha256,
            "calendar_registry_sha256": confirmation.calendar_registry_sha256,
        }
    )
    return int(WorkflowCliExitCode.SUCCESS)


def handle_data_plan_cli_command(args: argparse.Namespace) -> int:
    try:
        if args.command == "data-plan":
            if args.data_plan_command == "generate":
                return _handle_generate(args)
            if args.data_plan_command == "validate":
                return _handle_validate(args)
            if args.data_plan_command == "confirmation-schema":
                return _handle_confirmation_schema()
            if args.data_plan_command == "confirm":
                return _handle_confirm(args)
            if args.data_plan_command == "validate-confirmation":
                return _handle_validate_confirmation(args)
    except DataPlanCliInputError as exc:
        emit_error("cli_input_error", exc.message, exc.stage)
        return int(WorkflowCliExitCode.CLI_INPUT_ERROR)
    except DataPlanReviewError as exc:
        failure = exc.failure
        details = None
        if failure.unresolved_requirements:
            details = {
                "unresolved_requirements": [
                    item.model_dump(mode="json")
                    for item in failure.unresolved_requirements
                ]
            }
        emit_error(
            failure.code.value,
            failure.message,
            failure.stage.value,
            details=details,
        )
        return int(DATA_PLAN_REVIEW_ERROR_EXIT_CODES[failure.code])
    except Exception:
        emit_error(
            "internal_error",
            "unexpected internal data-plan CLI error",
            "internal",
        )
        return int(WorkflowCliExitCode.INTERNAL_ERROR)
    emit_error("cli_input_error", "unsupported data-plan command", "argument_parsing")
    return int(WorkflowCliExitCode.CLI_INPUT_ERROR)


__all__ = [
    "DATA_PLAN_REVIEW_ERROR_EXIT_CODES",
    "add_data_plan_parsers",
    "handle_data_plan_cli_command",
]
