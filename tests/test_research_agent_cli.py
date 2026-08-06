"""Research agent CLI tests (v0.4.0 Phase 4)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_research_agent import (
    _agent_config,
    _fixture_interpreter,
    _run_to_interpretation,
    QUESTION,
)
from market_validator.agent import (
    AgentConfig,
    apply_research_agent_response,
    create_research_session,
)
from market_validator.research_agent_cli import (
    _handle_revise,
    _handle_status,
    main,
)


class _Args:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class CliTest(unittest.TestCase):
    def _workspace_with_session(self, tmp):
        config = _agent_config(Path(tmp))
        session = create_research_session(question=QUESTION, config=config)
        return config, session

    def test_cli_status_human_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, _session = self._workspace_with_session(tmp)
            args = _Args(session=config.workspace, json=False)
            output = _capture_stdout(lambda: _handle_status(args))
        self.assertIn("当前阶段", output)
        self.assertIn("session id", output)

    def test_cli_status_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, _session = self._workspace_with_session(tmp)
            args = _Args(session=config.workspace, json=True)
            output = _capture_stdout(lambda: _handle_status(args))
            payload = json.loads(output)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["status"], "needs_user")
        self.assertEqual(
            payload["next_action"]["action_type"],
            "request_llm_access",
        )

    def test_cli_resume_injected_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, session = self._workspace_with_session(tmp)
            # allow llm + full run to a confirmation boundary
            config = AgentConfig(
                **{**config.__dict__, "allow_llm_network": True}
            )
            session = apply_research_agent_response(
                config,
                session,
                action_type="request_llm_access",
                payload={},
            )
            with mock.patch("builtins.input", return_value="confirm"):
                args = _Args(
                    session=config.workspace,
                    json=False,
                    language="zh-CN",
                    use_fixture_proposal=False,
                    interpretation_model=None,
                    allow_llm_network=True,
                    allow_data_network=False,
                )
                output = _capture_stdout(lambda: _handle_resume(args))
        self.assertIn("数据计划", output)

    def test_cli_revise_creates_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, _session = self._workspace_with_session(tmp)
            child = str(Path(tmp) / "child")
            args = _Args(
                session=config.workspace,
                workspace=child,
                instruction="把样本改成二〇二一年以后",
                instruction_file=None,
                provider=None,
                model=None,
                allow_llm_network=False,
                allow_data_network=False,
                language="zh-CN",
                use_fixture_proposal=True,
                interpretation_model=None,
                json=False,
            )
            output = _capture_stdout(lambda: _handle_revise(args))
            from market_validator.agent import load_latest_research_session

            child_session = load_latest_research_session(child)
        self.assertEqual(child_session.parent_session_id, _session.session_id)
        self.assertIn("session id", output)

    def test_cli_revise_inherits_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, _session = self._workspace_with_session(tmp)
            child = str(Path(tmp) / "child2")
            args = _Args(
                session=config.workspace,
                workspace=child,
                instruction="修改样本窗口",
                instruction_file=None,
                provider=None,
                model=None,
                allow_llm_network=False,
                allow_data_network=False,
                language="zh-CN",
                use_fixture_proposal=True,
                interpretation_model=None,
                json=False,
            )
            _capture_stdout(lambda: _handle_revise(args))
            from market_validator.agent import load_latest_research_session

            child_session = load_latest_research_session(child)
        self.assertEqual(child_session.artifact_references, [])


def _capture_stdout(func) -> str:
    import io
    import sys

    buffer = io.StringIO()
    old = sys.stdout
    sys.stdout = buffer
    try:
        func()
    finally:
        sys.stdout = old
    return buffer.getvalue()


# import lazily to avoid circular import at module load
def _handle_resume(args):
    from market_validator.research_agent_cli import _handle_resume as impl

    return impl(args)


if __name__ == "__main__":
    unittest.main()
