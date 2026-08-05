"""Stable JSON CLI surface for the sole deterministic offline workflow."""

from __future__ import annotations

import argparse
from enum import IntEnum
import json
import os
from pathlib import Path
import stat
import sys
from typing import TextIO

from pydantic import ValidationError

from market_validator.analysis.artifacts import (
    ArtifactIntegrityError,
    ArtifactPathError,
    PriceChangeVolatilityArtifactError,
    load_price_change_volatility_artifact,
)
from market_validator.planning import (
    PlanProposalError,
    PlanningErrorCode,
    calculate_plan_proposal_sha256,
    compile_confirmed_workflow_plan,
    parse_market_validation_plan_proposal,
    parse_plan_proposal_confirmation,
    persist_compiled_workflow_plan,
)
from market_validator.plan_providers import (
    PlanProposalProviderError,
    PlanProposalProviderErrorCode,
    SUPPORTED_PLAN_PROPOSAL_PROVIDERS,
    create_plan_proposal_provider,
    generate_market_validation_plan_proposal,
    persist_generated_plan_proposal,
    validate_plan_proposal_output_path,
)
from market_validator.workflow import (
    MarketValidationWorkflowPlan,
    WorkflowErrorCode,
    WorkflowExecutionError,
    run_market_validation_workflow,
)


class WorkflowCliExitCode(IntEnum):
    SUCCESS = 0
    CLI_INPUT_ERROR = 2
    INVALID_PLAN = 3
    WORKFLOW_PATH_ERROR = 4
    SOURCE_IDENTITY_MISMATCH = 5
    ANALYSIS_CONTRACT_MISMATCH = 6
    ANALYSIS_FAILED = 7
    ARTIFACT_CONFLICT = 8
    ARTIFACT_VERIFICATION_FAILED = 9
    INTERNAL_ERROR = 10
    INVALID_PROPOSAL = 11
    INVALID_CONFIRMATION = 12
    CONFIRMATION_MISMATCH = 13
    PROPOSAL_NOT_CONFIRMABLE = 14
    PROPOSAL_DATA_CONTRACT_MISMATCH = 15
    PLAN_OUTPUT_CONFLICT = 16
    PLAN_OUTPUT_ERROR = 17
    PROVIDER_CONFIGURATION_MISSING = 18
    PROVIDER_REQUEST_FAILED = 19
    PROVIDER_TIMEOUT = 20
    PROVIDER_REFUSED = 21
    PROVIDER_INVALID_PROPOSAL = 22
    PROPOSAL_OUTPUT_CONFLICT = 23
    PROPOSAL_OUTPUT_ERROR = 24


WORKFLOW_ERROR_EXIT_CODES = {
    WorkflowErrorCode.INVALID_PLAN: WorkflowCliExitCode.INVALID_PLAN,
    WorkflowErrorCode.WORKFLOW_PATH_ERROR: WorkflowCliExitCode.WORKFLOW_PATH_ERROR,
    WorkflowErrorCode.SOURCE_IDENTITY_MISMATCH: (
        WorkflowCliExitCode.SOURCE_IDENTITY_MISMATCH
    ),
    WorkflowErrorCode.ANALYSIS_CONTRACT_MISMATCH: (
        WorkflowCliExitCode.ANALYSIS_CONTRACT_MISMATCH
    ),
    WorkflowErrorCode.ANALYSIS_FAILED: WorkflowCliExitCode.ANALYSIS_FAILED,
    WorkflowErrorCode.ARTIFACT_CONFLICT: WorkflowCliExitCode.ARTIFACT_CONFLICT,
    WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED: (
        WorkflowCliExitCode.ARTIFACT_VERIFICATION_FAILED
    ),
}

PLANNING_ERROR_EXIT_CODES = {
    PlanningErrorCode.INVALID_PROPOSAL: WorkflowCliExitCode.INVALID_PROPOSAL,
    PlanningErrorCode.INVALID_CONFIRMATION: WorkflowCliExitCode.INVALID_CONFIRMATION,
    PlanningErrorCode.CONFIRMATION_MISMATCH: (
        WorkflowCliExitCode.CONFIRMATION_MISMATCH
    ),
    PlanningErrorCode.PROPOSAL_NOT_CONFIRMABLE: (
        WorkflowCliExitCode.PROPOSAL_NOT_CONFIRMABLE
    ),
    PlanningErrorCode.PROPOSAL_DATA_CONTRACT_MISMATCH: (
        WorkflowCliExitCode.PROPOSAL_DATA_CONTRACT_MISMATCH
    ),
    PlanningErrorCode.PLANNING_PATH_ERROR: WorkflowCliExitCode.WORKFLOW_PATH_ERROR,
    PlanningErrorCode.PLAN_OUTPUT_CONFLICT: WorkflowCliExitCode.PLAN_OUTPUT_CONFLICT,
    PlanningErrorCode.PLAN_OUTPUT_ERROR: WorkflowCliExitCode.PLAN_OUTPUT_ERROR,
}

PROVIDER_ERROR_EXIT_CODES = {
    PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING: (
        WorkflowCliExitCode.PROVIDER_CONFIGURATION_MISSING
    ),
    PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED: (
        WorkflowCliExitCode.PROVIDER_REQUEST_FAILED
    ),
    PlanProposalProviderErrorCode.PROVIDER_TIMEOUT: (
        WorkflowCliExitCode.PROVIDER_TIMEOUT
    ),
    PlanProposalProviderErrorCode.PROVIDER_REFUSED: (
        WorkflowCliExitCode.PROVIDER_REFUSED
    ),
    PlanProposalProviderErrorCode.PROVIDER_INVALID_PROPOSAL: (
        WorkflowCliExitCode.PROVIDER_INVALID_PROPOSAL
    ),
    PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_CONFLICT: (
        WorkflowCliExitCode.PROPOSAL_OUTPUT_CONFLICT
    ),
    PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR: (
        WorkflowCliExitCode.PROPOSAL_OUTPUT_ERROR
    ),
}


class WorkflowCliInputError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        stage: str,
        exit_code: WorkflowCliExitCode,
    ) -> None:
        self.code = code
        self.message = message
        self.stage = stage
        self.exit_code = exit_code
        super().__init__(message)


def _stable_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def emit_success(data: object, *, stream: TextIO | None = None) -> None:
    target = stream if stream is not None else sys.stdout
    target.write(_stable_json({"ok": True, "data": data}) + "\n")


def emit_error(
    code: str,
    message: str,
    stage: str,
    *,
    stream: TextIO | None = None,
) -> None:
    target = stream if stream is not None else sys.stderr
    target.write(
        _stable_json(
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "stage": stage,
                },
            }
        )
        + "\n"
    )


def add_workflow_parsers(subparsers: argparse._SubParsersAction) -> None:
    propose_plan = subparsers.add_parser(
        "propose-plan",
        help="explicitly call an optional AI provider for an untrusted proposal",
    )
    propose_plan.add_argument("--question-file", required=True, type=Path)
    propose_plan.add_argument(
        "--provider",
        required=True,
        choices=SUPPORTED_PLAN_PROPOSAL_PROVIDERS,
    )
    propose_plan.add_argument("--model", required=True)
    propose_plan.add_argument("--output", required=True, type=Path)
    propose_plan.add_argument(
        "--allow-network",
        required=True,
        action="store_true",
        help="explicitly authorize this provider request to use the network",
    )

    validate_proposal = subparsers.add_parser(
        "validate-proposal",
        help="strictly validate one untrusted AI plan proposal without executing it",
    )
    validate_proposal.add_argument("proposal", type=Path)

    compile_plan = subparsers.add_parser(
        "compile-plan",
        help="bind a confirmed proposal to an explicit Bundle and write a WorkflowPlan",
    )
    compile_plan.add_argument("--proposal", required=True, type=Path)
    compile_plan.add_argument("--confirmation", required=True, type=Path)
    compile_plan.add_argument("--bundle", required=True, type=Path)
    compile_plan.add_argument("--output", required=True, type=Path)
    compile_plan.add_argument("--expected-artifact-manifest-sha256")

    validate_plan = subparsers.add_parser(
        "validate-plan",
        help="strictly validate one explicit UTF-8 workflow plan without running it",
    )
    validate_plan.add_argument("plan", type=Path)

    run = subparsers.add_parser(
        "run",
        help="run the sole supported deterministic offline workflow",
    )
    run.add_argument("--plan", required=True, type=Path)
    run.add_argument("--bundle", required=True, type=Path)
    run.add_argument("--artifact-root", required=True, type=Path)

    verify = subparsers.add_parser(
        "verify-artifact",
        help="strictly verify an existing analysis artifact without modifying it",
    )
    verify.add_argument("--artifact", required=True, type=Path)
    verify.add_argument("--expected-manifest-sha256", required=True)


def _path_is_symlink(path: Path) -> bool:
    return path.is_symlink()


def _validate_plan_file_path(path: Path) -> Path:
    if any(part == os.pardir for part in path.parts):
        raise WorkflowCliInputError(
            "cli_input_error",
            "plan path must not contain path traversal",
            "plan_input",
            WorkflowCliExitCode.CLI_INPUT_ERROR,
        )
    absolute = path.absolute()
    for candidate in [absolute, *absolute.parents]:
        try:
            exists = candidate.exists()
        except OSError as exc:
            raise WorkflowCliInputError(
                "cli_input_error",
                "plan path cannot be inspected safely",
                "plan_input",
                WorkflowCliExitCode.CLI_INPUT_ERROR,
            ) from exc
        if exists and _path_is_symlink(candidate):
            raise WorkflowCliInputError(
                "cli_input_error",
                "plan file and its path must not contain symbolic links",
                "plan_input",
                WorkflowCliExitCode.CLI_INPUT_ERROR,
            )
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise WorkflowCliInputError(
            "cli_input_error",
            "plan file does not exist or cannot be inspected",
            "plan_input",
            WorkflowCliExitCode.CLI_INPUT_ERROR,
        ) from exc
    if not stat.S_ISREG(mode):
        raise WorkflowCliInputError(
            "cli_input_error",
            "plan path must identify a regular file",
            "plan_input",
            WorkflowCliExitCode.CLI_INPUT_ERROR,
        )
    return path.resolve(strict=True)


def _load_plan_file(path: Path) -> MarketValidationWorkflowPlan:
    safe_path = _validate_plan_file_path(path)
    try:
        raw_bytes = safe_path.read_bytes()
        raw_text = raw_bytes.decode("utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise WorkflowCliInputError(
            "cli_input_error",
            "plan file must be readable UTF-8",
            "plan_input",
            WorkflowCliExitCode.CLI_INPUT_ERROR,
        ) from exc
    try:
        decoded = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise WorkflowCliInputError(
            "cli_input_error",
            "plan file is not valid JSON",
            "plan_input",
            WorkflowCliExitCode.CLI_INPUT_ERROR,
        ) from exc
    if not isinstance(decoded, dict):
        raise WorkflowCliInputError(
            WorkflowErrorCode.INVALID_PLAN.value,
            "workflow plan must be a JSON object",
            "plan_validation",
            WorkflowCliExitCode.INVALID_PLAN,
        )
    try:
        return MarketValidationWorkflowPlan.model_validate_json(raw_bytes)
    except ValidationError as exc:
        raise WorkflowCliInputError(
            WorkflowErrorCode.INVALID_PLAN.value,
            "workflow plan failed strict validation",
            "plan_validation",
            WorkflowCliExitCode.INVALID_PLAN,
        ) from exc


def _read_planning_input_file(
    path: Path,
    *,
    label: str,
    code: PlanningErrorCode,
    exit_code: WorkflowCliExitCode,
) -> bytes:
    if any(part == os.pardir for part in path.parts):
        raise WorkflowCliInputError(
            code.value,
            f"{label} path must not contain path traversal",
            f"{label}_validation",
            exit_code,
        )
    absolute = path.absolute()
    for candidate in [absolute, *absolute.parents]:
        if candidate.exists() and _path_is_symlink(candidate):
            raise WorkflowCliInputError(
                code.value,
                f"{label} path must not contain symbolic links",
                f"{label}_validation",
                exit_code,
            )
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise WorkflowCliInputError(
            code.value,
            f"{label} file does not exist or cannot be inspected",
            f"{label}_validation",
            exit_code,
        ) from exc
    if not stat.S_ISREG(mode):
        raise WorkflowCliInputError(
            code.value,
            f"{label} path must identify a regular file",
            f"{label}_validation",
            exit_code,
        )
    try:
        return path.resolve(strict=True).read_bytes()
    except OSError as exc:
        raise WorkflowCliInputError(
            code.value,
            f"{label} file could not be read safely",
            f"{label}_validation",
            exit_code,
        ) from exc


def _handle_validate_plan(args: argparse.Namespace) -> int:
    plan = _load_plan_file(args.plan)
    emit_success(plan.model_dump(mode="json"))
    return int(WorkflowCliExitCode.SUCCESS)


def _read_question_file(path: Path) -> str:
    if any(part == os.pardir for part in path.parts):
        raise WorkflowCliInputError(
            "cli_input_error",
            "question path must not contain path traversal",
            "question_input",
            WorkflowCliExitCode.CLI_INPUT_ERROR,
        )
    absolute = path.absolute()
    for candidate in [absolute, *absolute.parents]:
        try:
            exists = candidate.exists()
        except OSError as exc:
            raise WorkflowCliInputError(
                "cli_input_error",
                "question path cannot be inspected safely",
                "question_input",
                WorkflowCliExitCode.CLI_INPUT_ERROR,
            ) from exc
        if exists and _path_is_symlink(candidate):
            raise WorkflowCliInputError(
                "cli_input_error",
                "question file and its path must not contain symbolic links",
                "question_input",
                WorkflowCliExitCode.CLI_INPUT_ERROR,
            )
    try:
        mode = path.lstat().st_mode
        payload = path.resolve(strict=True).read_bytes()
        question = payload.decode("utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise WorkflowCliInputError(
            "cli_input_error",
            "question file must be a readable regular UTF-8 file",
            "question_input",
            WorkflowCliExitCode.CLI_INPUT_ERROR,
        ) from exc
    if not stat.S_ISREG(mode):
        raise WorkflowCliInputError(
            "cli_input_error",
            "question path must identify a regular file",
            "question_input",
            WorkflowCliExitCode.CLI_INPUT_ERROR,
        )
    if not question.strip() or "\x00" in question:
        raise WorkflowCliInputError(
            "cli_input_error",
            "question file must contain non-empty text without NUL bytes",
            "question_input",
            WorkflowCliExitCode.CLI_INPUT_ERROR,
        )
    return question


def _handle_propose_plan(args: argparse.Namespace) -> int:
    question = _read_question_file(args.question_file)
    validate_plan_proposal_output_path(args.output)
    provider = create_plan_proposal_provider(
        args.provider,
        args.model,
        allow_network=args.allow_network,
    )
    generated = generate_market_validation_plan_proposal(provider, question)
    persisted = persist_generated_plan_proposal(generated, args.output)
    emit_success(persisted.model_dump(mode="json"))
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_validate_proposal(args: argparse.Namespace) -> int:
    raw_proposal = _read_planning_input_file(
        args.proposal,
        label="proposal",
        code=PlanningErrorCode.INVALID_PROPOSAL,
        exit_code=WorkflowCliExitCode.INVALID_PROPOSAL,
    )
    proposal = parse_market_validation_plan_proposal(raw_proposal)
    emit_success(
        {
            "proposal": proposal.model_dump(mode="json"),
            "proposal_sha256": calculate_plan_proposal_sha256(proposal),
        }
    )
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_compile_plan(args: argparse.Namespace) -> int:
    raw_proposal = _read_planning_input_file(
        args.proposal,
        label="proposal",
        code=PlanningErrorCode.INVALID_PROPOSAL,
        exit_code=WorkflowCliExitCode.INVALID_PROPOSAL,
    )
    raw_confirmation = _read_planning_input_file(
        args.confirmation,
        label="confirmation",
        code=PlanningErrorCode.INVALID_CONFIRMATION,
        exit_code=WorkflowCliExitCode.INVALID_CONFIRMATION,
    )
    proposal = parse_market_validation_plan_proposal(raw_proposal)
    confirmation = parse_plan_proposal_confirmation(raw_confirmation)
    plan = compile_confirmed_workflow_plan(
        proposal,
        confirmation,
        args.bundle,
        expected_artifact_manifest_sha256=(
            args.expected_artifact_manifest_sha256
        ),
    )
    persisted = persist_compiled_workflow_plan(plan, args.output)
    emit_success(persisted.model_dump(mode="json"))
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_run(args: argparse.Namespace) -> int:
    plan = _load_plan_file(args.plan)
    completed = run_market_validation_workflow(
        plan,
        args.bundle,
        args.artifact_root,
    )
    emit_success(completed.model_dump(mode="json"))
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_verify_artifact(args: argparse.Namespace) -> int:
    loaded = load_price_change_volatility_artifact(
        args.artifact,
        expected_manifest_sha256=args.expected_manifest_sha256,
    )
    emit_success(loaded.model_dump(mode="json"))
    return int(WorkflowCliExitCode.SUCCESS)


def handle_workflow_cli_command(args: argparse.Namespace) -> int:
    """Handle only the three stable offline workflow CLI commands."""

    try:
        if args.command == "propose-plan":
            return _handle_propose_plan(args)
        if args.command == "validate-proposal":
            return _handle_validate_proposal(args)
        if args.command == "compile-plan":
            return _handle_compile_plan(args)
        if args.command == "validate-plan":
            return _handle_validate_plan(args)
        if args.command == "run":
            return _handle_run(args)
        if args.command == "verify-artifact":
            return _handle_verify_artifact(args)
    except WorkflowCliInputError as exc:
        emit_error(exc.code, exc.message, exc.stage)
        return int(exc.exit_code)
    except WorkflowExecutionError as exc:
        failure = exc.failure
        emit_error(
            failure.code.value,
            failure.message,
            failure.stage.value,
        )
        return int(WORKFLOW_ERROR_EXIT_CODES[failure.code])
    except PlanProposalError as exc:
        failure = exc.failure
        emit_error(
            failure.code.value,
            failure.message,
            failure.stage.value,
        )
        return int(PLANNING_ERROR_EXIT_CODES[failure.code])
    except PlanProposalProviderError as exc:
        failure = exc.failure
        emit_error(
            failure.code.value,
            failure.message,
            failure.stage.value,
        )
        return int(PROVIDER_ERROR_EXIT_CODES[failure.code])
    except ArtifactPathError:
        emit_error(
            WorkflowErrorCode.WORKFLOW_PATH_ERROR.value,
            "artifact path failed safety validation",
            "artifact_verification",
        )
        return int(WorkflowCliExitCode.WORKFLOW_PATH_ERROR)
    except ArtifactIntegrityError:
        emit_error(
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED.value,
            "strict artifact verification failed",
            "artifact_verification",
        )
        return int(WorkflowCliExitCode.ARTIFACT_VERIFICATION_FAILED)
    except PriceChangeVolatilityArtifactError:
        emit_error(
            WorkflowErrorCode.ARTIFACT_VERIFICATION_FAILED.value,
            "analysis artifact could not be strictly verified",
            "artifact_verification",
        )
        return int(WorkflowCliExitCode.ARTIFACT_VERIFICATION_FAILED)
    except Exception:
        emit_error(
            "internal_error",
            "unexpected internal workflow CLI error",
            "internal",
        )
        return int(WorkflowCliExitCode.INTERNAL_ERROR)

    emit_error("cli_input_error", "unsupported CLI command", "argument_parsing")
    return int(WorkflowCliExitCode.CLI_INPUT_ERROR)


__all__ = [
    "PLANNING_ERROR_EXIT_CODES",
    "PROVIDER_ERROR_EXIT_CODES",
    "WORKFLOW_ERROR_EXIT_CODES",
    "WorkflowCliExitCode",
    "add_workflow_parsers",
    "emit_error",
    "emit_success",
    "handle_workflow_cli_command",
]
