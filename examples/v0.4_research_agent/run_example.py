"""Offline user-level end-to-end example for the v0.4 Research Agent.

TEST-ONLY: this example uses synthetic local CSV data and a deterministic
fixture LLM backend. It never touches the network, never reads credentials
and never provides investment advice.

Usage:
    python examples\\v0.4_research_agent\\run_example.py --output DIR

On success a single JSON object is printed to stdout:
    valid, status, session_id, revision, analysis_result_id,
    overall_conclusion, report_path, report_sha256
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from fixture_support import (
    FixedClock,
    FixtureProposalBackend,
    QUESTION,
    build_synthetic_workspace,
    fixture_interpreter,
    schedule_snapshots,
    session_adapters,
)

from market_validator.agent.orchestrator import (
    AgentConfig,
    apply_research_agent_response,
    create_research_session,
)
from market_validator.agent.session import (
    ResearchSession,
)
from market_validator.data.providers.csv_provider import CSVProvider


def _find_artifact(session: ResearchSession, kind: str):
    for artifact in session.artifact_references:
        if artifact.artifact_type == kind:
            return artifact
    return None


def _analysis_result_id(workspace: Path, reference) -> str:
    path = workspace / "artifacts" / reference.relative_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in ("analysis_result_id", "result_id", "id"):
        if key in payload:
            return str(payload[key])
    return reference.relative_path


def _sha256_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_overall_conclusion(
    workspace: Path, session: ResearchSession, analysis_result_id: str
) -> str:
    interpretations_root = (
        workspace / "analysis-interpretations" / analysis_result_id
    )
    if not interpretations_root.is_dir():
        return ""
    for entry in sorted(interpretations_root.iterdir()):
        candidate = entry / "evidence-package.json"
        if candidate.is_file():
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            return str(payload["overall_conclusion"])
    return ""


def _run_example(output: Path) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    build_synthetic_workspace(output)

    providers = {"local_csv": CSVProvider(output / "csv")}

    config = AgentConfig(
        workspace=str(output),
        language="zh-CN",
        allow_llm_network=True,
        allow_data_network=True,
        proposal_backend=FixtureProposalBackend(),
        expected_backend="fixture",
        expected_model="fixture-v1",
        providers=providers,
        session_adapters=session_adapters(),
        schedule_snapshots=schedule_snapshots(),
        interpretation_client=fixture_interpreter(),
        interpretation_model="fixture-v1",
        clock=FixedClock(),
    )

    session = create_research_session(question=QUESTION, config=config)

    handlers = {
        "confirm_hypothesis": {"answer": "confirm"},
        "confirm_data_plan": {"answer": "confirm"},
        "choose_data_source": {"choice_index": 0},
        "confirm_source_selection": {"answer": "confirm"},
        "authorize_data_access": {"answer": "confirm"},
        "authorize_analysis_plan": {"answer": "confirm"},
        "execute_analysis": {"answer": "confirm"},
        "configure_interpretation_llm": {},
    }

    for _ in range(40):
        if session.status in ("completed", "failed"):
            break
        action = session.current_next_action
        if action is None:
            raise RuntimeError(
                f"session stopped with status {session.status} and no action"
            )
        if action.action_type not in handlers:
            raise RuntimeError(
                f"unexpected action {action.action_type} "
                f"(status={session.status})"
            )
        session = apply_research_agent_response(
            config,
            session,
            action_type=action.action_type,
            payload=handlers[action.action_type],
        )
    else:
        raise RuntimeError("session did not finish within 40 transitions")

    if session.status != "completed":
        raise RuntimeError(f"session ended with status {session.status}")

    result_artifact = _find_artifact(session, "analysis_result")
    if result_artifact is None:
        raise RuntimeError("no analysis_result artifact found")
    analysis_result_id = _analysis_result_id(output, result_artifact)

    report_path = Path(session.completed_report)
    if not report_path.is_file():
        raise RuntimeError(f"completed report missing: {report_path}")

    return {
        "valid": True,
        "status": session.status,
        "session_id": session.session_id,
        "revision": session.revision,
        "analysis_result_id": analysis_result_id,
        "overall_conclusion": _load_overall_conclusion(
            output, session, analysis_result_id
        ),
        "report_path": str(report_path),
        "report_sha256": _sha256_bytes(report_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline v0.4 research agent example"
    )
    parser.add_argument("--output", required=True, help="output directory")
    args = parser.parse_args(argv)

    output = Path(args.output)
    try:
        result = _run_example(output)
    except Exception as exc:  # noqa: BLE001 - user-facing CLI boundary
        print(
            json.dumps(
                {
                    "valid": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
