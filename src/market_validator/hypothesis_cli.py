"""Stable offline-first CLI for untrusted hypothesis proposal drafts."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat

from market_validator.backends.base import (
    StructuredGenerationBackendError,
    StructuredGenerationErrorCode,
)
from market_validator.backends.deepseek_api import DeepSeekApiBackend
from market_validator.hypothesis import (
    HypothesisProposalError,
    HypothesisProposalService,
    calculate_research_hypothesis_proposal_sha256,
    parse_research_hypothesis_proposal,
    persist_generated_research_hypothesis_proposal,
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
            "unsupported_requests": proposal.unsupported_requests,
        }
    )
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
        if args.command == "propose-hypothesis":
            return _handle_propose(args)
    except HypothesisCliInputError as exc:
        emit_error("cli_input_error", exc.message, exc.stage)
        return int(WorkflowCliExitCode.CLI_INPUT_ERROR)
    except HypothesisProposalError as exc:
        failure = exc.failure
        emit_error(failure.code.value, failure.message, failure.stage.value)
        return int(HYPOTHESIS_ERROR_EXIT_CODES[failure.code])
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
    "add_hypothesis_parsers",
    "handle_hypothesis_cli_command",
]
