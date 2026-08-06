"""Analysis confirmation and authorization tests (v0.4.0 Phase 1, H/I/L)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from market_validator.analysis.authorization import (
    ANALYSIS_AUTHORIZATION_STATEMENT,
    ANALYSIS_PLAN_CONFIRMATION_STATEMENT,
    AnalysisAuthorization,
    AnalysisPlanConfirmation,
    calculate_analysis_authorization_sha256,
    calculate_analysis_plan_confirmation_sha256,
    confirm_analysis_plan,
    create_analysis_authorization,
    parse_analysis_authorization,
    parse_analysis_plan_confirmation,
    persist_analysis_authorization,
    persist_analysis_plan_confirmation,
    serialize_analysis_authorization,
    serialize_analysis_plan_confirmation,
    validate_analysis_authorization_matches,
    validate_analysis_plan_confirmation_matches,
)
from market_validator.analysis.planning import (
    AnalysisPlan,
    AnalysisPlanningError,
    AnalysisPlanningErrorCode,
)
from market_validator.analysis.planning_generator import (
    generate_analysis_plan,
    persist_analysis_plan,
)
from tests.test_analysis_planning import _decisions, _make_context

T0 = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


def _ready_plan(tmp: Path):
    ctx = _make_context(tmp)
    generated = generate_analysis_plan(
        research_spec=ctx["spec"],
        readiness_assessment=ctx["assessment"],
        data_ready_manifest=ctx["manifest"],
        generated_plan=ctx["chain"]["plan"],
        data_plan=ctx["chain"]["data_plan"],
        instrument_registry=ctx["chain"]["instruments"],
        calendar_registry=ctx["chain"]["calendars"],
        decisions=_decisions(ctx["spec"]),
    )
    return generated, ctx


class ConfirmationTest(unittest.TestCase):
    def test_ready_plan_confirmable(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
        self.assertEqual(confirmation.confirmed, True)
        self.assertEqual(
            confirmation.confirmation_statement,
            ANALYSIS_PLAN_CONFIRMATION_STATEMENT,
        )
        self.assertEqual(
            confirmation.analysis_plan_sha256,
            generated.analysis_plan_sha256,
        )
        validate_analysis_plan_confirmation_matches(
            generated.analysis_plan, confirmation
        )

    def test_tampered_plan_id_rejected_before_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            tampered = generated.analysis_plan.model_copy(
                update={"analysis_plan_id": "0" * 32}
            )
            with self.assertRaises(AnalysisPlanningError) as caught:
                confirm_analysis_plan(tampered, confirmed_at=T0)
        self.assertEqual(
            caught.exception.code,
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_MISMATCH,
        )

    def test_draft_plan_not_confirmable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _make_context(Path(tmp))
            generated = generate_analysis_plan(
                research_spec=ctx["spec"],
                readiness_assessment=ctx["assessment"],
                data_ready_manifest=ctx["manifest"],
                generated_plan=ctx["chain"]["plan"],
                data_plan=ctx["chain"]["data_plan"],
                instrument_registry=ctx["chain"]["instruments"],
                calendar_registry=ctx["chain"]["calendars"],
                decisions=_decisions(ctx["spec"], method_profile=None),
            )
            with self.assertRaises(AnalysisPlanningError) as caught:
                confirm_analysis_plan(
                    generated.analysis_plan, confirmed_at=T0
                )
        self.assertEqual(
            caught.exception.code,
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_NOT_READY,
        )

    def test_stale_plan_confirmation_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            changed = generated.analysis_plan.model_copy(
                update={"warnings": ["changed"]}
            )
            with self.assertRaises(AnalysisPlanningError):
                validate_analysis_plan_confirmation_matches(
                    changed, confirmation
                )

    def test_naive_timestamp_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            with self.assertRaises(Exception):
                confirm_analysis_plan(
                    generated.analysis_plan,
                    confirmed_at=datetime(2026, 8, 6, 12, 0, 0),
                )

    def test_false_confirmed_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            payload = json.loads(
                serialize_analysis_plan_confirmation(confirmation).decode(
                    "utf-8"
                )
            )
            payload["confirmed"] = False
            with self.assertRaises(Exception):
                AnalysisPlanConfirmation.model_validate_json(
                    json.dumps(payload).encode("utf-8")
                )

    def test_statement_change_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            payload = json.loads(
                serialize_analysis_plan_confirmation(
                    confirm_analysis_plan(
                        generated.analysis_plan, confirmed_at=T0
                    )
                ).decode("utf-8")
            )
            payload["confirmation_statement"] = "anything else"
            with self.assertRaises(Exception):
                AnalysisPlanConfirmation.model_validate_json(
                    json.dumps(payload).encode("utf-8")
                )

    def test_confirmation_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            restored = parse_analysis_plan_confirmation(
                serialize_analysis_plan_confirmation(confirmation)
            )
        self.assertEqual(restored, confirmation)


class AuthorizationTest(unittest.TestCase):
    def test_confirmed_plan_authorizable(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            authorization = create_analysis_authorization(
                generated.analysis_plan, confirmation, authorized_at=T0
            )
        self.assertEqual(authorization.analysis_execution_authorized, True)
        self.assertEqual(authorization.robustness_execution_authorized, False)
        self.assertEqual(authorization.report_generation_authorized, False)
        self.assertEqual(authorization.network_access_authorized, False)
        self.assertEqual(authorization.provider_access_authorized, False)
        self.assertEqual(authorization.data_mutation_authorized, False)
        self.assertEqual(authorization.automatic_retry_authorized, False)
        self.assertEqual(authorization.fallback_authorized, False)
        self.assertEqual(authorization.single_use, True)
        self.assertEqual(
            authorization.authorization_statement,
            ANALYSIS_AUTHORIZATION_STATEMENT,
        )
        self.assertEqual(
            authorization.authorized_primary_test_ids,
            [test.test_id for test in generated.analysis_plan.primary_tests],
        )
        validate_analysis_authorization_matches(
            generated.analysis_plan, confirmation, authorization
        )

    def test_confirmation_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            other = generated.analysis_plan.model_copy(
                update={"warnings": ["other"]}
            )
            # the mutated plan is no longer self-consistent, so confirmation
            # itself must reject it before any authorization is attempted
            with self.assertRaises(AnalysisPlanningError) as caught:
                confirm_analysis_plan(other, confirmed_at=T0)
        self.assertEqual(
            caught.exception.code,
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_MISMATCH,
        )

    def test_scope_forbidden_flags_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            authorization = create_analysis_authorization(
                generated.analysis_plan, confirmation, authorized_at=T0
            )
            payload = json.loads(
                serialize_analysis_authorization(authorization).decode(
                    "utf-8"
                )
            )
        for key, value in [
            ("robustness_execution_authorized", True),
            ("report_generation_authorized", True),
            ("network_access_authorized", True),
            ("provider_access_authorized", True),
            ("data_mutation_authorized", True),
            ("automatic_retry_authorized", True),
            ("fallback_authorized", True),
            ("single_use", False),
            ("analysis_execution_authorized", False),
        ]:
            mutated = dict(payload)
            mutated[key] = value
            with self.assertRaises(Exception):
                AnalysisAuthorization.model_validate_json(
                    json.dumps(mutated).encode("utf-8")
                )

    def test_tampered_authorization_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            authorization = create_analysis_authorization(
                generated.analysis_plan, confirmation, authorized_at=T0
            )
            tampered = authorization.model_copy(
                update={"authorization_id": "0" * 20}
            )
            with self.assertRaises(AnalysisPlanningError) as caught:
                validate_analysis_authorization_matches(
                    generated.analysis_plan, confirmation, tampered
                )
        self.assertEqual(
            caught.exception.code,
            AnalysisPlanningErrorCode.ANALYSIS_AUTHORIZATION_MISMATCH,
        )

    def test_tampered_plan_rejected_at_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            tampered = generated.analysis_plan.model_copy(
                update={"analysis_plan_id": "0" * 32}
            )
            with self.assertRaises(AnalysisPlanningError) as caught:
                create_analysis_authorization(
                    tampered, confirmation, authorized_at=T0
                )
        self.assertEqual(
            caught.exception.code,
            AnalysisPlanningErrorCode.ANALYSIS_PLAN_MISMATCH,
        )

    def test_missing_or_extra_test_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            authorization = create_analysis_authorization(
                generated.analysis_plan, confirmation, authorized_at=T0
            )
            changed = authorization.model_copy(
                update={
                    "authorized_primary_test_ids": [
                        "0000000000000000"
                    ]
                }
            )
            with self.assertRaises(AnalysisPlanningError):
                validate_analysis_authorization_matches(
                    generated.analysis_plan, confirmation, changed
                )

    def test_authorization_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            authorization = create_analysis_authorization(
                generated.analysis_plan, confirmation, authorized_at=T0
            )
            restored = parse_analysis_authorization(
                serialize_analysis_authorization(authorization)
            )
        self.assertEqual(restored, authorization)
        self.assertEqual(
            calculate_analysis_authorization_sha256(restored),
            calculate_analysis_authorization_sha256(authorization),
        )


class AuthorizationPersistenceTest(unittest.TestCase):
    def test_persist_confirmation_and_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            confirmation_path = persist_analysis_plan_confirmation(
                confirmation, Path(tmp) / "confirmation.json"
            )
            authorization = create_analysis_authorization(
                generated.analysis_plan, confirmation, authorized_at=T0
            )
            authorization_path = persist_analysis_authorization(
                authorization, Path(tmp) / "authorization.json"
            )
            restored_confirmation = parse_analysis_plan_confirmation(
                confirmation_path.read_bytes()
            )
            restored_authorization = parse_analysis_authorization(
                authorization_path.read_bytes()
            )
        self.assertEqual(restored_confirmation, confirmation)
        self.assertEqual(restored_authorization, authorization)

    def test_persist_conflict_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            path = Path(tmp) / "confirmation.json"
            persist_analysis_plan_confirmation(confirmation, path)
            other = confirmation.model_copy(
                update={"confirmed_at": datetime(2026, 8, 6, 13, 0, tzinfo=timezone.utc)}
            )
            with self.assertRaises(AnalysisPlanningError):
                persist_analysis_plan_confirmation(other, path)

    def test_persist_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            with self.assertRaises(Exception):
                persist_analysis_plan_confirmation(
                    confirmation, Path(tmp) / ".." / "escape.json"
                )

    def test_plan_change_invalidates_confirmation_and_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated, _ctx = _ready_plan(Path(tmp))
            confirmation = confirm_analysis_plan(
                generated.analysis_plan, confirmed_at=T0
            )
            authorization = create_analysis_authorization(
                generated.analysis_plan, confirmation, authorized_at=T0
            )
            changed_plan = generated.analysis_plan.model_copy(
                update={"warnings": ["changed"]}
            )
            with self.assertRaises(AnalysisPlanningError):
                validate_analysis_plan_confirmation_matches(
                    changed_plan, confirmation
                )
            with self.assertRaises(AnalysisPlanningError):
                validate_analysis_authorization_matches(
                    changed_plan, confirmation, authorization
                )


class NoExecutionSideEffectsTest(unittest.TestCase):
    def test_authorization_module_imports_only_planning(self):
        from market_validator.analysis import authorization as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "price_change_volatility",
            "workflow",
            "data.execution",
            "credentials",
            "network",
        ):
            self.assertNotIn(f"import {forbidden}", source)
            self.assertNotIn(f"from market_validator.{forbidden}", source)


if __name__ == "__main__":
    unittest.main()
