"""Research agent tests (v0.4.0 Phase 4)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from market_validator.agent import (
    AgentConfig,
    ResearchAgentError,
    ResearchAgentErrorCode,
    ResearchSession,
    advance_until_blocked,
    apply_research_agent_response,
    create_research_session,
    load_latest_research_session,
    revise_research_session,
)
from market_validator.agent.session import (
    load_verified_artifact,
    persist_session_revision,
)

T0 = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)

QUESTION = "原油价格变化与 A 股能源板块是否存在关联？"


class _Clock:
    def __call__(self) -> datetime:
        return T0


class FakeProposalBackend:
    """Deterministic offline StructuredGenerationBackend for tests."""

    def status(self):
        class Status:
            name = "fixture"
            ready = True

        return Status()

    def _payload(self) -> dict:
        import copy

        from tests.test_hypothesis_review import _ready_association_payload

        payload = copy.deepcopy(_ready_association_payload())
        payload["original_question"] = QUESTION
        payload["normalized_research_question"] = (
            "synthetic oil-energy association"
        )
        instruments = {
            "us_equity_market_index": {
                "instrument_id": "energy.sector.index",
                "display_name": "Energy Sector Index",
                "asset_type": "macro_series",
                "market": "synthetic",
                "exchange_or_venue": "synthetic",
                "timezone": "UTC",
                "currency": "USD",
                "unit": "Index",
            },
            "broad_usd_index": {
                "instrument_id": "oil.price",
                "display_name": "Oil Price",
                "asset_type": "macro_series",
                "market": "synthetic",
                "exchange_or_venue": "synthetic",
                "timezone": "UTC",
                "currency": "USD",
                "unit": "Dollars per Barrel",
            },
        }
        variable_map = {
            "us_equity_return": "energy_sector_return",
            "usd_index_return": "oil_price_change",
        }
        outcome = payload["outcome"]
        outcome["variable_id"] = "energy_sector_return"
        outcome["market_context"] = "synthetic energy sector"
        outcome["asset_type"] = "macro_series"
        outcome["transformation"] = "level"
        for predictor in payload["predictors"]:
            predictor["variable_id"] = "oil_price_change"
            predictor["market_context"] = "synthetic commodity market"
            predictor["asset_type"] = "macro_series"
            predictor["transformation"] = "level"
        payload["sample"]["start_date"] = "2020-01-01"
        payload["sample"]["end_date"] = "2020-01-10"
        payload["research_spec_inputs"]["minimum_observations"] = 4
        hypothesis = payload["statistical_hypothesis"]
        if hypothesis.get("target_parameter") is not None:
            hypothesis["target_parameter"]["predictor_variable_id"] = (
                "oil_price_change"
            )
        for item in payload["research_spec_inputs"]["variables"]:
            old_id = item["variable_id"]
            item["variable_id"] = variable_map.get(old_id, old_id)
            key = (
                "us_equity_market_index"
                if old_id == "us_equity_return"
                else "broad_usd_index"
            )
            instrument = dict(instruments[key])
            instrument["continuous_contract"] = False
            item["instrument"] = instrument
        return payload

    def generate(self, request):
        from market_validator.hypothesis.serialization import (
            parse_research_hypothesis_proposal,
            serialize_research_hypothesis_proposal,
        )
        from market_validator.backends.base import (
            StructuredGenerationResult,
        )

        proposal = parse_research_hypothesis_proposal(
            json.dumps(self._payload(), ensure_ascii=False).encode("utf-8")
        )
        return StructuredGenerationResult(
            data=self._payload(),
            backend="fixture",
            model="fixture-v1",
            metadata={"fake": True},
        )


def _synthetic_workspace(tmp: Path) -> Path:
    """Create a workspace with synthetic registries and CSV data."""
    root = tmp / "workspace"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    csv_dir = root / "csv"
    csv_dir.mkdir()
    (root / "artifacts").mkdir()
    # calendars (registry document: list)
    calendars = {
        "schema_version": "1.0",
        "calendars": [
            {
                "calendar_id": "synthetic.test.equity",
                "display_name": "Synthetic Test Equity",
                "timezone": "UTC",
                "calendar_type": "equity_exchange",
                "schedule_adapter": "test.fixed.daily",
                "supports_special_sessions": False,
                "notes": ["synthetic"],
            }
        ],
    }
    (config_dir / "calendars.json").write_text(
        json.dumps(calendars), encoding="utf-8"
    )
    # instruments with verified local_csv mapping (registry document: list)
    energy_file = (csv_dir / "energy.csv").resolve()
    oil_file = (csv_dir / "oil.csv").resolve()
    instruments = {
        "schema_version": "1.0",
        "instruments": [
            {
                "instrument_id": "energy.sector.index",
                "display_name": "Energy Sector Index",
                "asset_type": "macro_series",
                "market": "synthetic",
                "exchange_or_venue": "synthetic",
                "calendar_id": "synthetic.test.equity",
                "timezone": "UTC",
                "currency": "USD",
                "unit": "Index",
                "aliases": [],
                "identity_status": "verified",
                "provider_mappings": [
                    {
                        "provider_id": "local_csv",
                        "provider_symbol": str(energy_file),
                        "dataset_or_endpoint": str(energy_file),
                        "market": "synthetic",
                        "verified": True,
                        "verified_on": "2026-01-01",
                        "verification_source_uri": (
                            "https://example.invalid/verified/energy"
                        ),
                        "notes": ["fixture"],
                    }
                ],
                "notes": ["fixture"],
            },
            {
                "instrument_id": "oil.price",
                "display_name": "Oil Price",
                "asset_type": "macro_series",
                "market": "synthetic",
                "exchange_or_venue": "synthetic",
                "calendar_id": "synthetic.test.equity",
                "timezone": "UTC",
                "currency": "USD",
                "unit": "Dollars per Barrel",
                "aliases": [],
                "identity_status": "verified",
                "provider_mappings": [
                    {
                        "provider_id": "local_csv",
                        "provider_symbol": str(oil_file),
                        "dataset_or_endpoint": str(oil_file),
                        "market": "synthetic",
                        "verified": True,
                        "verified_on": "2026-01-01",
                        "verification_source_uri": (
                            "https://example.invalid/verified/oil"
                        ),
                        "notes": ["fixture"],
                    }
                ],
                "notes": ["fixture"],
            },
        ],
    }
    (config_dir / "instruments.json").write_text(
        json.dumps(instruments), encoding="utf-8"
    )
    # CSV data (9 observations to cover pre-sample needs of returns)
    columns = (
        "instrument_id,field,value,observation_time,available_time,"
        "session_date,timezone,currency,unit"
    )
    dates = [
        "2020-01-01",
        "2020-01-02",
        "2020-01-03",
        "2020-01-06",
        "2020-01-07",
        "2020-01-08",
        "2020-01-09",
        "2020-01-10",
    ]
    for instrument, unit in (
        ("energy.sector.index", "Index"),
        ("oil.price", "Dollars per Barrel"),
    ):
        rows = [columns]
        for index, day in enumerate(dates):
            rows.append(
                f"{instrument},close,{100.0 + index * 0.7},"
                f"{day}T09:30:00+00:00,{day}T09:30:00+00:00,"
                f"{day},UTC,USD,{unit}"
            )
        (csv_dir / f"{instrument.split('.')[0]}.csv").write_text(
            "\n".join(rows), encoding="utf-8"
        )
    return root


def _agent_config(tmp: Path) -> AgentConfig:
    root = _synthetic_workspace(tmp)
    from market_validator.data.providers.csv_provider import CSVProvider

    providers = {
        "local_csv": CSVProvider(root / "csv"),
    }
    config = AgentConfig(
        workspace=str(root),
        language="zh-CN",
        allow_llm_network=False,
        allow_data_network=True,
        proposal_backend=FakeProposalBackend(),
        expected_backend="fixture",
        expected_model="fixture-v1",
        providers=providers,
        session_adapters=_session_adapters(),
        schedule_snapshots=_schedule_snapshots(),
        interpretation_client=None,
        interpretation_model="fixture-v1",
        clock=_Clock(),
    )
    return config


def _schedule_snapshots():
    from tests.test_data_readiness import _make_schedule_snapshot

    snapshot = _make_schedule_snapshot()
    return {snapshot.schedule_adapter_id: snapshot}


def _session_adapters():
    from tests.test_data_readiness import _FixedDailyAdapter

    adapter = _FixedDailyAdapter()
    return {
        adapter.adapter_id: lambda start, periods: (
            adapter.previous_sessions(start, periods)[0]
        )
    }


def _make_session(config: AgentConfig, *, allow_llm: bool = False) -> ResearchSession:
    config = AgentConfig(**{**config.__dict__, "allow_llm_network": allow_llm})
    return create_research_session(question=QUESTION, config=config)


class SessionTest(unittest.TestCase):
    def test_start_creates_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = create_research_session(
                question=QUESTION, config=config
            )
            revisions = list(
                (Path(config.workspace) / "session-revisions").glob("*.json")
            )
            self.assertTrue(revisions)
            self.assertEqual(session.session_id[:32], session.session_id)
            self.assertGreaterEqual(session.revision, 1)

    def test_revision_create_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = create_research_session(
                question=QUESTION, config=config
            )
            # identical bytes at the same revision are idempotent
            persist_session_revision(session, config.workspace)
            # different content at the same revision must conflict
            tampered = session.model_copy(
                update={"last_error": "tampered"}
            )
            with self.assertRaises(ResearchAgentError) as caught:
                persist_session_revision(tampered, config.workspace)
        self.assertEqual(
            caught.exception.code,
            ResearchAgentErrorCode.SESSION_REVISION_CONFLICT,
        )

    def test_artifact_hash_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            reference = session.artifact_references[0]
            target = (
                Path(config.workspace)
                / "artifacts"
                / reference.relative_path
            )
            target.write_bytes(target.read_bytes() + b"x")
            with self.assertRaises(ResearchAgentError) as caught:
                load_verified_artifact(config.workspace, reference)
        self.assertEqual(
            caught.exception.code,
            ResearchAgentErrorCode.SESSION_ARTIFACT_MISMATCH,
        )

    def test_advance_skips_safe_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            # proposal generated automatically -> confirmation boundary
            self.assertEqual(session.status, "needs_user")
            self.assertEqual(
                session.current_next_action.action_type,
                "confirm_hypothesis",
            )

    def test_stops_at_hypothesis_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            self.assertEqual(
                session.current_next_action.action_type,
                "confirm_hypothesis",
            )
            self.assertEqual(session.current_stage, "hypothesis_confirmation")

    def test_llm_not_authorized_no_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=False)
            self.assertEqual(session.status, "needs_user")
            self.assertEqual(
                session.current_next_action.action_type,
                "request_llm_access",
            )
            self.assertEqual(
                session.current_next_action.why_needed,
                "假设草案必须由结构化生成服务产生",
            )

    def test_clarification_applies_existing_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            config = AgentConfig(
                **{
                    **config.__dict__,
                    "proposal_backend": _AmbiguousBackend(),
                    "allow_llm_network": True,
                }
            )
            session = create_research_session(
                question=QUESTION, config=config
            )
            self.assertEqual(
                session.current_next_action.action_type,
                "answer_hypothesis_questions",
            )
            answers = _make_clarification_answers(
                session, Path(config.workspace)
            )
            session = apply_research_agent_response(
                config,
                session,
                action_type="answer_hypothesis_questions",
                payload={"answers_path": str(answers)},
            )
        self.assertEqual(session.status, "needs_user")
        self.assertEqual(
            session.current_next_action.action_type,
            "confirm_hypothesis",
        )

    def test_data_plan_confirmation_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            session = apply_research_agent_response(
                config,
                session,
                action_type="confirm_hypothesis",
                payload={"answer": "confirm"},
            )
            self.assertEqual(
                session.current_next_action.action_type,
                "confirm_data_plan",
            )
            self.assertIsNotNone(
                _find_artifact(session, "data_plan"),
                "data plan must be generated automatically",
            )

    def test_single_candidate_recommended_not_auto_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            session = apply_research_agent_response(
                config,
                session,
                action_type="confirm_hypothesis",
                payload={"answer": "confirm"},
            )
            session = apply_research_agent_response(
                config,
                session,
                action_type="confirm_data_plan",
                payload={"answer": "confirm"},
            )
            action = session.current_next_action
        self.assertEqual(action.action_type, "choose_data_source")
        self.assertIsNone(_find_artifact(session, "source_selection"))
        self.assertIsNone(
            _find_artifact(session, "source_selection_confirmation")
        )

    def test_no_mapping_outputs_interface_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            # strip verified mappings BEFORE the session starts so the
            # registry hash stays consistent with the DataPlan
            root = _synthetic_workspace(Path(tmp))
            instruments = json.loads(
                (root / "config" / "instruments.json").read_text(
                    encoding="utf-8"
                )
            )
            for instrument in instruments["instruments"]:
                instrument["provider_mappings"] = []
            (root / "config" / "instruments.json").write_text(
                json.dumps(instruments), encoding="utf-8"
            )
            from market_validator.data.providers.csv_provider import (
                CSVProvider,
            )

            config = AgentConfig(
                workspace=str(root),
                language="zh-CN",
                allow_llm_network=True,
                allow_data_network=True,
                proposal_backend=FakeProposalBackend(),
                expected_backend="fixture",
                expected_model="fixture-v1",
                providers={"local_csv": CSVProvider(root / "csv")},
                schedule_snapshots=_schedule_snapshots(),
                interpretation_client=None,
                interpretation_model="fixture-v1",
                clock=_Clock(),
            )
            session = create_research_session(question=QUESTION, config=config)
            session = apply_research_agent_response(
                config,
                session,
                action_type="confirm_hypothesis",
                payload={"answer": "confirm"},
            )
            session = apply_research_agent_response(
                config,
                session,
                action_type="confirm_data_plan",
                payload={"answer": "confirm"},
            )
            action = session.current_next_action
        self.assertEqual(action.action_type, "configure_data_interface")
        self.assertIn("A. 在 InstrumentRegistry", action.message)
        self.assertIn("B. 使用 local CSV", action.message)

    def test_provider_network_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            # map the instruments to the fake network provider before
            # the session starts so the registry hash stays consistent
            root = _synthetic_workspace(Path(tmp))
            instruments = json.loads(
                (root / "config" / "instruments.json").read_text(
                    encoding="utf-8"
                )
            )
            for index, instrument in enumerate(instruments["instruments"]):
                instrument["provider_mappings"] = [
                    {
                        "provider_id": "fred",
                        "provider_symbol": f"SYNTHETIC/SERIES-{index}",
                        "dataset_or_endpoint": (
                            "https://fred.example.invalid/series/"
                            f"SYNTHETIC-{index}"
                        ),
                        "market": "synthetic",
                        "verified": True,
                        "verified_on": "2026-01-01",
                        "verification_source_uri": (
                            "https://example.invalid/verified/synthetic"
                        ),
                        "notes": ["fixture"],
                    }
                ]
            (root / "config" / "instruments.json").write_text(
                json.dumps(instruments), encoding="utf-8"
            )
            config = AgentConfig(
                workspace=str(root),
                language="zh-CN",
                allow_llm_network=True,
                allow_data_network=False,
                proposal_backend=FakeProposalBackend(),
                expected_backend="fixture",
                expected_model="fixture-v1",
                providers={"fred": _NetworkProvider()},
                session_adapters=_session_adapters(),
                schedule_snapshots=_schedule_snapshots(),
                interpretation_client=None,
                interpretation_model="fixture-v1",
                clock=_Clock(),
            )
            session = create_research_session(question=QUESTION, config=config)
            session = _advance_to_acquisition(config, session)
            session = apply_research_agent_response(
                config,
                session,
                action_type="authorize_data_access",
                payload={"answer": "confirm"},
            )
            action = session.current_next_action
        self.assertEqual(action.action_type, "blocked")
        self.assertEqual(
            action.why_needed, "Provider 网络请求必须显式授权"
        )

    def test_data_access_authorization_not_auto_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            session = _advance_to_acquisition(config, session)
        self.assertIsNone(
            _find_artifact(session, "data_access_authorization")
        )
        self.assertEqual(
            session.current_next_action.action_type,
            "authorize_data_access",
        )

    def test_analysis_plan_suggestion_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            session = _run_to_analysis(config, session)
        self.assertEqual(
            session.current_next_action.action_type,
            "authorize_analysis_plan",
        )
        self.assertIn(
            "confirm", session.current_next_action.required_inputs[0]
        )

    def test_analysis_authorization_not_auto_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            session = _run_to_analysis(config, session)
        self.assertIsNone(
            _find_artifact(session, "analysis_authorization")
        )

    def test_completed_result_enters_interpretation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            session = _run_to_interpretation(config, session)
        self.assertEqual(
            session.current_next_action.action_type,
            "configure_interpretation_llm",
        )
        self.assertIsNotNone(_find_artifact(session, "analysis_result"))

    def test_completed_session_returns_report_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            session = _run_to_interpretation(config, session)
            self.assertEqual(
                session.current_next_action.action_type,
                "configure_interpretation_llm",
            )
            config = AgentConfig(
                **{
                    **config.__dict__,
                    "interpretation_client": _fixture_interpreter(),
                }
            )
            session = apply_research_agent_response(
                config,
                session,
                action_type="configure_interpretation_llm",
                payload={},
            )
            self.assertEqual(session.status, "completed")
            self.assertIsNotNone(session.completed_report)
            self.assertTrue(
                Path(session.completed_report).is_file()
            )

    def test_resume_does_not_repeat_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            revisions_after_start = len(
                list(
                    (
                        Path(config.workspace) / "session-revisions"
                    ).glob("*.json")
                )
            )
            resumed = load_latest_research_session(config.workspace)
            revisions_after_resume = len(
                list(
                    (
                        Path(config.workspace) / "session-revisions"
                    ).glob("*.json")
                )
            )
        self.assertEqual(revisions_after_start, revisions_after_resume)
        self.assertEqual(resumed.session_id, session.session_id)

    def test_consumed_authorization_no_auto_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            session = _run_to_analysis(config, session)
            session = apply_research_agent_response(
                config,
                session,
                action_type="authorize_analysis_plan",
                payload={"answer": "confirm"},
            )
            # execution consumes the single-use authorization and then
            # fails (alignment requires schedule snapshots): the agent
            # must NOT retry automatically
            config_broken = AgentConfig(
                **{**config.__dict__, "schedule_snapshots": {}}
            )
            session = apply_research_agent_response(
                config_broken,
                session,
                action_type="execute_analysis",
                payload={"answer": "confirm"},
            )
            self.assertEqual(session.status, "blocked")
            session = advance_until_blocked(config_broken, session)
        self.assertEqual(session.status, "blocked")
        self.assertIn("不会自动重试", session.current_next_action.message)

    def test_revise_creates_child_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            parent = _make_session(config, allow_llm=True)
            child_config = AgentConfig(
                **{**config.__dict__, "workspace": str(Path(tmp) / "child")}
            )
            child = revise_research_session(
                parent_workspace=config.workspace,
                instruction="把样本改成二〇二一年以后",
                config=child_config,
            )
        self.assertEqual(child.parent_session_id, parent.session_id)
        self.assertNotEqual(child.session_id, parent.session_id)
        self.assertGreaterEqual(child.revision, 1)
        self.assertNotIn("修订", parent.original_question)

    def test_source_choice_out_of_range_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            session = _make_session(config, allow_llm=True)
            session = apply_research_agent_response(
                config,
                session,
                action_type="confirm_hypothesis",
                payload={"answer": "confirm"},
            )
            session = apply_research_agent_response(
                config,
                session,
                action_type="confirm_data_plan",
                payload={"answer": "confirm"},
            )
            with self.assertRaises(ResearchAgentError) as caught:
                apply_research_agent_response(
                    config,
                    session,
                    action_type="choose_data_source",
                    payload={"choice_index": 99},
                )
        self.assertEqual(
            caught.exception.code,
            ResearchAgentErrorCode.INVALID_AGENT_INPUT,
        )

    def test_revise_inherits_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _agent_config(Path(tmp))
            parent = _make_session(config, allow_llm=True)
            child_config = AgentConfig(
                **{**config.__dict__, "workspace": str(Path(tmp) / "child2")}
            )
            child = revise_research_session(
                parent_workspace=config.workspace,
                instruction="修改样本窗口",
                config=child_config,
            )
        self.assertEqual(child.artifact_references, [])
        self.assertNotIn("确认", "".join(child.history_summary))


def _find_artifact(session, artifact_type):
    for reference in session.artifact_references:
        if reference.artifact_type == artifact_type:
            return reference
    return None


class _AmbiguousBackend(FakeProposalBackend):
    def generate(self, request):
        from market_validator.hypothesis.models import (
            ResearchHypothesisProposal,
        )
        from market_validator.hypothesis.serialization import (
            parse_research_hypothesis_proposal,
            serialize_research_hypothesis_proposal,
        )
        from market_validator.backends.base import (
            StructuredGenerationResult,
        )

        result = super().generate(request)
        proposal = parse_research_hypothesis_proposal(
            json.dumps(result.data, ensure_ascii=False).encode("utf-8")
        )
        proposal = proposal.model_copy(
            update={
                "ambiguities": ["样本窗口是哪个时间段？"],
                "ready_for_spec_review": False,
            }
        )
        payload = serialize_research_hypothesis_proposal(proposal)
        return StructuredGenerationResult(
            data=json.loads(payload.decode("utf-8")),
            backend="fixture",
            model="fixture-v1",
            metadata={"fake": True},
        )


def _make_clarification_answers(session, workspace: Path) -> Path:
    from datetime import date

    from market_validator.research.enums import Frequency
    from market_validator.hypothesis.clarification import (
        ClarificationAnswer,
        ClarificationAnswers,
        proposal_ambiguity_references,
        serialize_clarification_answers,
    )
    from market_validator.hypothesis.serialization import (
        calculate_research_hypothesis_proposal_sha256,
    )
    from market_validator.agent.session import load_verified_artifact

    reference = _find_artifact(session, "hypothesis_proposal")
    proposal, _ = load_verified_artifact(workspace, reference)
    references = proposal_ambiguity_references(proposal)
    answers = ClarificationAnswers(
        clarification_schema_version="1.0",
        proposal_sha256=calculate_research_hypothesis_proposal_sha256(
            proposal
        ),
        answers=[
            ClarificationAnswer(
                ambiguity_id=references[0].ambiguity_id,
                update={
                    "sample": {
                        "start_date": date(2020, 1, 1),
                        "end_date": date(2020, 1, 10),
                        "frequency": Frequency.ONE_DAY,
                    }
                },
            )
        ],
    )
    path = workspace / "clarification-answers.json"
    path.write_bytes(serialize_clarification_answers(answers))
    return path


def _advance_to_acquisition(config, session):
    session = apply_research_agent_response(
        config, session, action_type="confirm_hypothesis",
        payload={"answer": "confirm"},
    )
    session = apply_research_agent_response(
        config, session, action_type="confirm_data_plan",
        payload={"answer": "confirm"},
    )
    session = apply_research_agent_response(
        config, session, action_type="choose_data_source",
        payload={"choice_index": 0},
    )
    session = apply_research_agent_response(
        config, session, action_type="confirm_source_selection",
        payload={"answer": "confirm"},
    )
    return session


def _run_to_analysis(config, session):
    session = _advance_to_acquisition(config, session)
    session = apply_research_agent_response(
        config, session, action_type="authorize_data_access",
        payload={"answer": "confirm"},
    )
    return session


def _run_to_interpretation(config, session):
    session = _run_to_analysis(config, session)
    session = apply_research_agent_response(
        config, session, action_type="authorize_analysis_plan",
        payload={"answer": "confirm"},
    )
    session = apply_research_agent_response(
        config, session, action_type="execute_analysis",
        payload={"answer": "confirm"},
    )
    return session


class _NetworkProvider:
    """Fake provider whose capability is network access."""

    provider_id = "fred"

    def capabilities(self):
        from market_validator.data.providers.base import (
            ProviderCapabilities,
        )
        from market_validator.research.enums import (
            AssetType,
            DataRevisionMode,
            Frequency,
            PriceAdjustment,
        )

        return ProviderCapabilities(
            provider_id="fred",
            supported_asset_types=[AssetType.MACRO_SERIES],
            supported_markets=["synthetic"],
            supported_frequencies=[Frequency.ONE_DAY],
            supported_fields=["value"],
            supported_price_adjustments=list(PriceAdjustment),
            supports_continuous_futures=False,
            requires_authentication=True,
            requires_network=True,
            supports_local_files=False,
            supported_revision_policies=[DataRevisionMode.LATEST_AVAILABLE],
        )


def _fixture_interpreter():
    from market_validator.interpretation import FixtureLLMClient

    return FixtureLLMClient()


if __name__ == "__main__":
    unittest.main()
