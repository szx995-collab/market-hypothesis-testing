"""Stable offline-first CLI for untrusted hypothesis proposal drafts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import stat

from market_validator.backends.base import (
    StructuredGenerationBackendError,
    StructuredGenerationErrorCode,
)
from market_validator.backends.deepseek_api import DeepSeekApiBackend
from market_validator.hypothesis import (
    ClarificationAnswers,
    HypothesisLifecycleError,
    HypothesisLifecycleErrorCode,
    HypothesisProposalError,
    HypothesisProposalService,
    ResearchHypothesisProposal,
    apply_clarification_answers,
    calculate_research_hypothesis_proposal_sha256,
    clarification_answers_json_schema,
    compile_confirmed_research_spec,
    confirm_research_hypothesis_proposal,
    confirmation_json_schema,
    parse_clarification_answers,
    parse_research_hypothesis_confirmation,
    parse_research_hypothesis_proposal,
    persist_clarified_proposal,
    persist_compiled_research_spec,
    persist_generated_research_hypothesis_proposal,
    persist_research_hypothesis_confirmation,
    proposal_ambiguity_references,
    research_hypothesis_proposal_json_schema,
    validate_hypothesis_proposal_output_path,
)
from market_validator.hypothesis.serialization import HypothesisProposalErrorCode
from market_validator.workflow_cli import (
    WorkflowCliExitCode,
    emit_error,
    emit_success,
)


HYPOTHESIS_ERROR_EXIT_CODES = {
    HypothesisProposalErrorCode.INVALID_PROPOSAL: (
        WorkflowCliExitCode.INVALID_HYPOTHESIS_PROPOSAL
    ),
    HypothesisProposalErrorCode.BACKEND_UNAVAILABLE: (
        WorkflowCliExitCode.HYPOTHESIS_BACKEND_UNAVAILABLE
    ),
    HypothesisProposalErrorCode.BACKEND_IDENTITY_MISMATCH: (
        WorkflowCliExitCode.HYPOTHESIS_BACKEND_IDENTITY_MISMATCH
    ),
    HypothesisProposalErrorCode.OUTPUT_CONFLICT: (
        WorkflowCliExitCode.HYPOTHESIS_OUTPUT_CONFLICT
    ),
    HypothesisProposalErrorCode.OUTPUT_ERROR: (
        WorkflowCliExitCode.HYPOTHESIS_OUTPUT_ERROR
    ),
}

BACKEND_ERROR_EXIT_CODES = {
    StructuredGenerationErrorCode.CONFIGURATION_MISSING: (
        WorkflowCliExitCode.PROVIDER_CONFIGURATION_MISSING
    ),
    StructuredGenerationErrorCode.REQUEST_FAILED: (
        WorkflowCliExitCode.PROVIDER_REQUEST_FAILED
    ),
    StructuredGenerationErrorCode.TIMEOUT: WorkflowCliExitCode.PROVIDER_TIMEOUT,
    StructuredGenerationErrorCode.REFUSED: WorkflowCliExitCode.PROVIDER_REFUSED,
    StructuredGenerationErrorCode.INVALID_RESPONSE: (
        WorkflowCliExitCode.PROVIDER_INVALID_PROPOSAL
    ),
}

HYPOTHESIS_LIFECYCLE_ERROR_EXIT_CODES = {
    HypothesisLifecycleErrorCode.INVALID_CLARIFICATIONS: (
        WorkflowCliExitCode.INVALID_HYPOTHESIS_CLARIFICATIONS
    ),
    HypothesisLifecycleErrorCode.CLARIFICATION_MISMATCH: (
        WorkflowCliExitCode.HYPOTHESIS_CLARIFICATION_MISMATCH
    ),
    HypothesisLifecycleErrorCode.CLARIFICATION_CONFLICT: (
        WorkflowCliExitCode.HYPOTHESIS_CLARIFICATION_CONFLICT
    ),
    HypothesisLifecycleErrorCode.PROPOSAL_NOT_READY: (
        WorkflowCliExitCode.HYPOTHESIS_PROPOSAL_NOT_READY
    ),
    HypothesisLifecycleErrorCode.INVALID_CONFIRMATION: (
        WorkflowCliExitCode.INVALID_HYPOTHESIS_CONFIRMATION
    ),
    HypothesisLifecycleErrorCode.CONFIRMATION_MISMATCH: (
        WorkflowCliExitCode.HYPOTHESIS_CONFIRMATION_MISMATCH
    ),
    HypothesisLifecycleErrorCode.RESEARCH_SPEC_UNRESOLVED: (
        WorkflowCliExitCode.RESEARCH_SPEC_UNRESOLVED
    ),
    HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID: (
        WorkflowCliExitCode.RESEARCH_SPEC_INVALID
    ),
    HypothesisLifecycleErrorCode.OUTPUT_CONFLICT: (
        WorkflowCliExitCode.HYPOTHESIS_REVIEW_OUTPUT_CONFLICT
    ),
    HypothesisLifecycleErrorCode.OUTPUT_ERROR: (
        WorkflowCliExitCode.HYPOTHESIS_REVIEW_OUTPUT_ERROR
    ),
}


class HypothesisCliInputError(ValueError):
    def __init__(self, message: str, stage: str) -> None:
        self.message = message
        self.stage = stage
        super().__init__(message)


def add_hypothesis_parsers(subparsers: argparse._SubParsersAction) -> None:
    hypothesis = subparsers.add_parser(
        "hypothesis",
        help="inspect or validate an untrusted statistical hypothesis proposal",
    )
    commands = hypothesis.add_subparsers(dest="hypothesis_command", required=True)
    commands.add_parser("schema", help="print the hypothesis proposal JSON Schema")
    validate = commands.add_parser(
        "validate-proposal",
        help="strictly validate and summarize a proposal without confirming it",
    )
    validate.add_argument("proposal", type=Path)

    commands.add_parser(
        "clarification-schema",
        help="print the strict clarification-answer JSON Schema",
    )
    validate_clarifications = commands.add_parser(
        "validate-clarifications",
        help="validate and apply clarifications in memory without writing output",
    )
    validate_clarifications.add_argument("--proposal", required=True, type=Path)
    validate_clarifications.add_argument("--answers", required=True, type=Path)
    apply_clarifications = commands.add_parser(
        "apply-clarifications",
        help="apply whitelisted answers and create a new canonical proposal",
    )
    apply_clarifications.add_argument("--proposal", required=True, type=Path)
    apply_clarifications.add_argument("--answers", required=True, type=Path)
    apply_clarifications.add_argument("--output", required=True, type=Path)
    commands.add_parser(
        "confirmation-schema",
        help="print the strict explicit-confirmation JSON Schema",
    )
    confirm = commands.add_parser(
        "confirm-proposal",
        help="explicitly confirm one exact ready proposal without executing it",
    )
    confirm.add_argument("--proposal", required=True, type=Path)
    confirm.add_argument("--output", required=True, type=Path)
    compile_spec = commands.add_parser(
        "compile-research-spec",
        help="compile a confirmed proposal into the existing ResearchSpec contract",
    )
    compile_spec.add_argument("--proposal", required=True, type=Path)
    compile_spec.add_argument("--confirmation", required=True, type=Path)
    compile_spec.add_argument("--output", required=True, type=Path)

    propose = subparsers.add_parser(
        "propose-hypothesis",
        help="explicitly request one untrusted hypothesis proposal from a provider",
    )
    propose.add_argument("--question-file", required=True, type=Path)
    propose.add_argument("--provider", required=True, choices=("deepseek_api",))
    propose.add_argument("--model", required=True)
    propose.add_argument("--output", required=True, type=Path)
    propose.add_argument(
        "--allow-network",
        required=True,
        action="store_true",
        help="authorize exactly one provider request; does not confirm or execute",
    )


def _read_regular_utf8(path: Path, *, label: str) -> bytes:
    if any(part == os.pardir for part in path.parts):
        raise HypothesisCliInputError(
            f"{label} path must not contain path traversal",
            f"{label}_input",
        )
    absolute = path.absolute()
    for candidate in [absolute, *absolute.parents]:
        try:
            exists = candidate.exists()
        except OSError as exc:
            raise HypothesisCliInputError(
                f"{label} path cannot be inspected safely",
                f"{label}_input",
            ) from exc
        if exists and candidate.is_symlink():
            raise HypothesisCliInputError(
                f"{label} path must not contain symbolic links",
                f"{label}_input",
            )
    try:
        mode = path.lstat().st_mode
        resolved = path.resolve(strict=True)
        payload = resolved.read_bytes()
    except OSError as exc:
        raise HypothesisCliInputError(
            f"{label} file does not exist or cannot be read safely",
            f"{label}_input",
        ) from exc
    if not stat.S_ISREG(mode):
        raise HypothesisCliInputError(
            f"{label} path must identify a regular file",
            f"{label}_input",
        )
    try:
        payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise HypothesisCliInputError(
            f"{label} file must be UTF-8",
            f"{label}_input",
        ) from exc
    return payload


def _handle_schema() -> int:
    emit_success(research_hypothesis_proposal_json_schema())
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_validate(args: argparse.Namespace) -> int:
    proposal = parse_research_hypothesis_proposal(
        _read_regular_utf8(args.proposal, label="hypothesis_proposal")
    )
    emit_success(
        {
            "proposal_schema_version": proposal.proposal_schema_version,
            "proposal_sha256": calculate_research_hypothesis_proposal_sha256(proposal),
            "claim_type": proposal.claim_type.value,
            "statistical_method": (
                proposal.statistical_hypothesis.statistical_method.value
                if proposal.statistical_hypothesis.statistical_method is not None
                else None
            ),
            "ready_for_spec_review": proposal.ready_for_spec_review,
            "ambiguities": proposal.ambiguities,
            "ambiguity_references": [
                item.model_dump(mode="json")
                for item in proposal_ambiguity_references(proposal)
            ],
            "unsupported_requests": proposal.unsupported_requests,
        }
    )
    return int(WorkflowCliExitCode.SUCCESS)


def _load_proposal(path: Path) -> ResearchHypothesisProposal:
    return parse_research_hypothesis_proposal(
        _read_regular_utf8(path, label="hypothesis_proposal")
    )


def _load_clarifications(path: Path) -> ClarificationAnswers:
    return parse_clarification_answers(
        _read_regular_utf8(path, label="hypothesis_clarifications")
    )


def _handle_clarification_schema() -> int:
    emit_success(clarification_answers_json_schema())
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_validate_clarifications(args: argparse.Namespace) -> int:
    proposal = _load_proposal(args.proposal)
    answers = _load_clarifications(args.answers)
    applied = apply_clarification_answers(proposal, answers)
    emit_success(
        {
            "source_proposal_sha256": applied.source_proposal_sha256,
            "clarified_proposal_sha256": applied.clarified_proposal_sha256,
            "answered_ambiguity_ids": applied.answered_ambiguity_ids,
            "remaining_ambiguities": applied.remaining_ambiguities,
            "ready_for_spec_review": applied.proposal.ready_for_spec_review,
        }
    )
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_apply_clarifications(args: argparse.Namespace) -> int:
    applied = apply_clarification_answers(
        _load_proposal(args.proposal),
        _load_clarifications(args.answers),
    )
    output_path = persist_clarified_proposal(applied, args.output)
    emit_success(
        {
            "output_path": str(output_path),
            "proposal_sha256": applied.clarified_proposal_sha256,
            "ready_for_spec_review": applied.proposal.ready_for_spec_review,
            "remaining_ambiguities": applied.remaining_ambiguities,
        }
    )
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_confirmation_schema() -> int:
    emit_success(confirmation_json_schema())
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_confirm(args: argparse.Namespace) -> int:
    proposal = _load_proposal(args.proposal)
    confirmation = confirm_research_hypothesis_proposal(
        proposal,
        confirmed_at=datetime.now(timezone.utc),
    )
    output_path = persist_research_hypothesis_confirmation(
        confirmation,
        args.output,
    )
    emit_success(
        {
            "output_path": str(output_path),
            "proposal_sha256": confirmation.proposal_sha256,
            "confirmed": confirmation.confirmed,
        }
    )
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_compile_research_spec(args: argparse.Namespace) -> int:
    proposal = _load_proposal(args.proposal)
    confirmation = parse_research_hypothesis_confirmation(
        _read_regular_utf8(args.confirmation, label="hypothesis_confirmation")
    )
    compiled = compile_confirmed_research_spec(proposal, confirmation)
    persisted = persist_compiled_research_spec(compiled, args.output)
    emit_success(persisted.model_dump(mode="json"))
    return int(WorkflowCliExitCode.SUCCESS)


def _handle_propose(args: argparse.Namespace) -> int:
    question_bytes = _read_regular_utf8(args.question_file, label="question")
    question = question_bytes.decode("utf-8", errors="strict")
    if not question.strip() or "\x00" in question:
        raise HypothesisCliInputError(
            "question file must contain non-empty text without NUL bytes",
            "question_input",
        )
    validate_hypothesis_proposal_output_path(args.output)
    backend = DeepSeekApiBackend(
        model=args.model,
        allow_network=args.allow_network,
    )
    service = HypothesisProposalService(
        backend,
        expected_backend=args.provider,
        expected_model=args.model,
    )
    generated = service.generate(question)
    persisted = persist_generated_research_hypothesis_proposal(
        generated,
        args.output,
    )
    emit_success(persisted.model_dump(mode="json"))
    return int(WorkflowCliExitCode.SUCCESS)


def handle_hypothesis_cli_command(args: argparse.Namespace) -> int:
    try:
        if args.command == "hypothesis":
            if args.hypothesis_command == "schema":
                return _handle_schema()
            if args.hypothesis_command == "validate-proposal":
                return _handle_validate(args)
            if args.hypothesis_command == "clarification-schema":
                return _handle_clarification_schema()
            if args.hypothesis_command == "validate-clarifications":
                return _handle_validate_clarifications(args)
            if args.hypothesis_command == "apply-clarifications":
                return _handle_apply_clarifications(args)
            if args.hypothesis_command == "confirmation-schema":
                return _handle_confirmation_schema()
            if args.hypothesis_command == "confirm-proposal":
                return _handle_confirm(args)
            if args.hypothesis_command == "compile-research-spec":
                return _handle_compile_research_spec(args)
        if args.command == "propose-hypothesis":
            return _handle_propose(args)
    except HypothesisCliInputError as exc:
        emit_error("cli_input_error", exc.message, exc.stage)
        return int(WorkflowCliExitCode.CLI_INPUT_ERROR)
    except HypothesisProposalError as exc:
        failure = exc.failure
        emit_error(failure.code.value, failure.message, failure.stage.value)
        return int(HYPOTHESIS_ERROR_EXIT_CODES[failure.code])
    except HypothesisLifecycleError as exc:
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
        return int(HYPOTHESIS_LIFECYCLE_ERROR_EXIT_CODES[failure.code])
    except StructuredGenerationBackendError as exc:
        emit_error(exc.code.value, exc.safe_message, "hypothesis_backend_generation")
        return int(BACKEND_ERROR_EXIT_CODES[exc.code])
    except Exception:
        emit_error(
            "internal_error",
            "unexpected internal hypothesis CLI error",
            "internal",
        )
        return int(WorkflowCliExitCode.INTERNAL_ERROR)
    emit_error("cli_input_error", "unsupported hypothesis command", "argument_parsing")
    return int(WorkflowCliExitCode.CLI_INPUT_ERROR)


__all__ = [
    "BACKEND_ERROR_EXIT_CODES",
    "HYPOTHESIS_ERROR_EXIT_CODES",
    "HYPOTHESIS_LIFECYCLE_ERROR_EXIT_CODES",
    "add_hypothesis_parsers",
    "handle_hypothesis_cli_command",
]
