"""Research agent CLI (v0.4.0 Phase 4): start / status / resume / revise."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from market_validator.agent import (
    AgentConfig,
    ResearchAgentError,
    create_research_session,
    format_session_json,
    format_session_status,
    load_latest_research_session,
    revise_research_session,
)
from market_validator.agent.orchestrator import (
    advance_research_session,
    advance_until_blocked,
    apply_research_agent_response,
)


MAX_QUESTION_FILE_BYTES = 1024 * 1024


def _read_question(question: str | None, question_file: str | None) -> str:
    if bool(question) == bool(question_file):
        raise ValueError("exactly one of --question or --question-file is required")
    if question_file is not None:
        content = Path(question_file).read_bytes()
        if len(content) > MAX_QUESTION_FILE_BYTES:
            raise ValueError("question file exceeds the 1 MiB limit")
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ValueError("question file must be valid UTF-8") from None
        if not text.strip() or "\x00" in text:
            raise ValueError("question file must be non-empty text")
        return text.strip()
    assert question is not None
    if not question.strip():
        raise ValueError("question must not be empty")
    return question.strip()


def _build_config(args: argparse.Namespace) -> AgentConfig:
    providers = {}
    proposal_backend = None
    expected_backend = None
    expected_model = None
    if getattr(args, "provider", None) and getattr(args, "model", None):
        if args.provider == "deepseek_api":
            from market_validator.backends.deepseek_api import (
                DeepSeekApiBackend,
            )

            proposal_backend = DeepSeekApiBackend(
                model=args.model,
                allow_network=bool(args.allow_llm_network),
            )
            expected_backend = "deepseek_api"
            expected_model = args.model
    interpretation_client = None
    if getattr(args, "interpretation_model", None) == "fixture":
        from market_validator.interpretation import FixtureLLMClient

        interpretation_client = FixtureLLMClient()
    return AgentConfig(
        workspace=getattr(args, "workspace", None) or args.session,
        language=getattr(args, "language", "zh-CN") or "zh-CN",
        allow_llm_network=bool(args.allow_llm_network),
        allow_data_network=bool(args.allow_data_network),
        proposal_backend=proposal_backend,
        expected_backend=expected_backend,
        expected_model=expected_model,
        providers=providers,
        interpretation_client=interpretation_client,
        interpretation_model=(
            getattr(args, "interpretation_model", None) or "fixture-v1"
        ),
    )


def _handle_start(args: argparse.Namespace) -> int:
    try:
        question = _read_question(
            getattr(args, "question", None), args.question_file
        )
        config = _build_config(args)
    except ValueError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except OSError:
        print("错误：无法读取问题文件", file=sys.stderr)
        return 2
    try:
        session = create_research_session(question=question, config=config)
    except ResearchAgentError as error:
        print(f"错误：{error.safe_message}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(format_session_json(session), ensure_ascii=False))
    else:
        print(format_session_status(session))
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    try:
        session = load_latest_research_session(args.session)
    except ResearchAgentError as error:
        print(f"错误：{error.safe_message}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(format_session_json(session), ensure_ascii=False))
    else:
        print(format_session_status(session))
    return 0


def _handle_resume(args: argparse.Namespace) -> int:
    config = _build_config(args)
    try:
        session = load_latest_research_session(args.session)
        if session.status in ("completed", "failed"):
            raise ResearchAgentError(
                "session_not_resumable",
                "the session is already finished",
            )
        if session.status == "blocked":
            # the user may have repaired the workspace (registry, CSV,
            # schedule files); retry the current stage exactly once
            session = advance_research_session(config, session)
        action = session.current_next_action
        if action is None:
            session = advance_until_blocked(config, session)
            action = session.current_next_action
        print(format_session_status(session))
        if action is None or action.action_type in (
            "completed",
            "blocked",
        ):
            return 0
        if args.json:
            print(json.dumps(format_session_json(session), ensure_ascii=False))
            return 0
        try:
            answer = input("> ").strip()
        except EOFError:
            print("已取消。", file=sys.stderr)
            return 0
        payload: dict[str, object] = {}
        if action.action_type == "choose_data_source":
            payload["choice_index"] = answer
        elif action.action_type in (
            "confirm_hypothesis",
            "confirm_data_plan",
            "confirm_source_selection",
            "authorize_data_access",
            "authorize_analysis_plan",
            "execute_analysis",
        ):
            if answer not in ("confirm", "cancel"):
                print("请输入 confirm 或 cancel。", file=sys.stderr)
                return 2
            payload["answer"] = answer
        elif action.action_type in (
            "answer_hypothesis_questions",
            "complete_research_spec",
        ):
            payload["answers_path"] = answer
        elif action.action_type == "request_llm_access":
            payload["proposal_path"] = answer
        session = apply_research_agent_response(
            config, session, action_type=action.action_type, payload=payload
        )
        print(format_session_status(session))
    except ResearchAgentError as error:
        print(f"错误：{error.safe_message}", file=sys.stderr)
        return 1
    except (OSError, EOFError, ValueError):
        print("错误：无法读取输入文件或输入不合法", file=sys.stderr)
        return 1
    return 0


def _handle_revise(args: argparse.Namespace) -> int:
    try:
        instruction = _read_question(
            getattr(args, "instruction", None), args.instruction_file
        )
        config = _build_config(args)
        session = revise_research_session(
            parent_workspace=args.session,
            instruction=instruction,
            config=config,
        )
    except (ValueError, ResearchAgentError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except OSError:
        print("错误：无法读取修订指令文件", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(format_session_json(session), ensure_ascii=False))
    else:
        print(format_session_status(session))
    return 0


def build_research_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "research", help="Research agent session commands"
    )
    research_sub = parser.add_subparsers(dest="research_command", required=True)

    start = research_sub.add_parser("start", help="start a research session")
    start.add_argument("--question", default=None)
    start.add_argument("--question-file", default=None)
    start.add_argument("--workspace", required=True)
    start.add_argument("--provider", default=None, choices=["deepseek_api"])
    start.add_argument("--model", default=None)
    start.add_argument("--allow-llm-network", action="store_true")
    start.add_argument("--allow-data-network", action="store_true")
    start.add_argument("--language", default="zh-CN")
    start.add_argument("--use-fixture-proposal", action="store_true")
    start.add_argument("--interpretation-model", default=None)
    start.add_argument("--json", action="store_true")
    start.set_defaults(func=_handle_start)

    status = research_sub.add_parser("status", help="show session status")
    status.add_argument("--session", required=True)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_handle_status)

    resume = research_sub.add_parser("resume", help="resume a session")
    resume.add_argument("--session", required=True)
    resume.add_argument("--json", action="store_true")
    resume.add_argument("--language", default="zh-CN")
    resume.add_argument("--use-fixture-proposal", action="store_true")
    resume.add_argument("--interpretation-model", default=None)
    resume.add_argument("--allow-llm-network", action="store_true")
    resume.add_argument("--allow-data-network", action="store_true")
    resume.set_defaults(func=_handle_resume)

    revise = research_sub.add_parser("revise", help="revise a session")
    revise.add_argument("--session", required=True)
    revise.add_argument("--instruction", default=None)
    revise.add_argument("--instruction-file", default=None)
    revise.add_argument("--workspace", required=True)
    revise.add_argument("--provider", default=None, choices=["deepseek_api"])
    revise.add_argument("--model", default=None)
    revise.add_argument("--allow-llm-network", action="store_true")
    revise.add_argument("--allow-data-network", action="store_true")
    revise.add_argument("--language", default="zh-CN")
    revise.add_argument("--use-fixture-proposal", action="store_true")
    revise.add_argument("--interpretation-model", default=None)
    revise.add_argument("--json", action="store_true")
    revise.set_defaults(func=_handle_revise)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="market-validator research",
        description="Research agent session commands",
    )
    build_research_parser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
