"""Offline contract tests for clarification, confirmation, and spec compilation."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market_validator.hypothesis import (
    ClarificationAnswers,
    CompiledResearchSpec,
    HypothesisLifecycleError,
    HypothesisLifecycleErrorCode,
    ResearchHypothesisProposalConfirmation,
    apply_clarification_answers,
    calculate_research_hypothesis_proposal_sha256,
    compile_confirmed_research_spec,
    confirm_research_hypothesis_proposal,
    parse_clarification_answers,
    parse_research_hypothesis_confirmation,
    parse_research_hypothesis_proposal,
    persist_clarified_proposal,
    persist_compiled_research_spec,
    proposal_ambiguity_references,
    serialize_clarification_answers,
    serialize_research_hypothesis_confirmation,
    serialize_research_hypothesis_proposal,
    validate_confirmation_matches_proposal,
)
from market_validator.research import parse_research_spec


ROOT = Path(__file__).resolve().parents[1]
OIL_EXAMPLE = (
    ROOT
    / "examples"
    / "hypothesis_proposals"
    / "oil_to_a_share_energy.proposal.json"
)
CONFIRMED_AT = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _oil_proposal():
    return parse_research_hypothesis_proposal(OIL_EXAMPLE.read_bytes())


def _oil_answers(proposal=None) -> ClarificationAnswers:
    proposal = proposal or _oil_proposal()
    references = proposal_ambiguity_references(proposal)
    self_hash = calculate_research_hypothesis_proposal_sha256(proposal)
    updates = [
        {
            "sample": {
                "start_date": "2015-01-01",
                "end_date": "2024-12-31",
            }
        },
        {
            "variables": [
                {
                    "variable_id": "international_oil_return",
                    "asset_type": "commodity_spot",
                }
            ]
        },
        {
            "variables": [
                {
                    "variable_id": "international_oil_return",
                    "transformation": "log_return",
                },
                {
                    "variable_id": "a_share_energy_return",
                    "transformation": "log_return",
                },
            ]
        },
        {
            "variables": [
                {
                    "variable_id": "a_share_energy_return",
                    "asset_type": "sector_index",
                    "proxy_for": None,
                }
            ]
        },
        {
            "alignment": {
                "target_calendar": "China A-share trading calendar",
                "information_cutoff": {"type": "before_target_open"},
            }
        },
        {"controls": []},
        {
            "statistical_hypothesis": {
                "significance_level": 0.05,
                "minimum_effect_size": 0.001,
            }
        },
    ]
    return parse_clarification_answers(
        _json_bytes({
            "proposal_sha256": self_hash,
            "answers": [
                {"ambiguity_id": ref.ambiguity_id, "update": update}
                for ref, update in zip(references, updates, strict=True)
            ],
        })
    )


def _ready_association_payload() -> dict[str, object]:
    return {
        "proposal_schema_version": "1.0",
        "original_question": (
            "Are daily US equity-market log returns associated with daily USD "
            "index log returns from 2020 through 2024 using a two-sided test?"
        ),
        "normalized_research_question": (
            "Are contemporaneous daily US equity-market and USD index log "
            "returns associated?"
        ),
        "claim_type": "association",
        "outcome": {
            "variable_id": "us_equity_return",
            "concept_name": "US equity-market daily log return",
            "role": "outcome",
            "market_context": "United States equity market",
            "asset_type": "equity_index",
            "transformation": "log_return",
            "time_relation": {
                "relation": "outcome_period",
                "lag_periods": None,
                "available_before_outcome": None,
                "description": "daily outcome observation period",
            },
            "proxy_for": None,
            "contract_roll_method": None,
        },
        "predictors": [
            {
                "variable_id": "usd_index_return",
                "concept_name": "USD index daily log return",
                "role": "predictor",
                "market_context": "United States foreign-exchange market",
                "asset_type": "fx",
                "transformation": "log_return",
                "time_relation": {
                    "relation": "contemporaneous",
                    "lag_periods": 0,
                    "available_before_outcome": True,
                    "description": "same observation period as the outcome",
                },
                "proxy_for": None,
                "contract_roll_method": None,
            }
        ],
        "controls": [],
        "sample": {
            "start_date": "2020-01-01",
            "end_date": "2024-12-31",
            "frequency": "1d",
        },
        "alignment": {
            "market_relation": "same_market",
            "target_market": None,
            "target_timezone": None,
            "target_calendar": None,
            "target_session": None,
            "information_cutoff": None,
        },
        "statistical_hypothesis": {
            "statistical_method": "pearson_correlation",
            "target_parameter": {
                "kind": "correlation",
                "predictor_variable_id": "usd_index_return",
                "reference_value": 0,
            },
            "direction": "two_sided",
            "null_hypothesis": "H0: rho = 0",
            "alternative_hypothesis": "H1: rho != 0",
            "significance_level": 0.05,
            "minimum_effect_size": 0.1,
        },
        "assumptions": ["The question requests association, not causation."],
        "ambiguities": [],
        "unsupported_requests": [],
        "ready_for_spec_review": True,
        "research_spec_inputs": {
            "spec_id": "us-equity-usd-association-v1",
            "title": "US equity and USD daily-return association",
            "variables": [
                {
                    "variable_id": "us_equity_return",
                    "instrument": {
                        "instrument_id": "us_equity_market_index",
                        "display_name": "US equity market index",
                        "asset_type": "equity_index",
                        "market": "United States",
                        "exchange_or_venue": "US equity market",
                        "timezone": "America/New_York",
                        "currency": "USD",
                        "unit": "index points",
                        "continuous_contract": False,
                    },
                    "field": "close",
                    "availability_lag_periods": 0,
                    "price_adjustment": None,
                    "rolling_window_periods": None,
                    "revision_policy": {
                        "mode": "not_applicable",
                        "as_of_date": None,
                    },
                },
                {
                    "variable_id": "usd_index_return",
                    "instrument": {
                        "instrument_id": "broad_usd_index",
                        "display_name": "Broad USD index",
                        "asset_type": "fx",
                        "market": "United States",
                        "exchange_or_venue": "Foreign exchange market",
                        "timezone": "America/New_York",
                        "currency": "USD",
                        "unit": "index points",
                        "continuous_contract": False,
                    },
                    "field": "close",
                    "availability_lag_periods": 0,
                    "price_adjustment": None,
                    "rolling_window_periods": None,
                    "revision_policy": {
                        "mode": "not_applicable",
                        "as_of_date": None,
                    },
                },
            ],
            "minimum_observations": 100,
            "join_policy": None,
            "max_staleness_days": None,
            "missing_data_policy": None,
            "multiple_testing_correction": "none",
            "robustness_checks": [],
            "limitations": [
                "This retrospective association does not establish causation."
            ],
        },
    }


def _ready_association():
    return parse_research_hypothesis_proposal(
        _json_bytes(_ready_association_payload())
    )


class ClarificationContractTest(unittest.TestCase):
    def test_optional_compilation_inputs_preserve_legacy_canonical_hash(self) -> None:
        self.assertEqual(
            calculate_research_hypothesis_proposal_sha256(_oil_proposal()),
            "38da48a8329ac3297e9dc64a444d2c5d3e5ae873bb3d987231a9c976aeb059fe",
        )

    def test_all_seven_answers_produce_ready_proposal(self) -> None:
        applied = apply_clarification_answers(_oil_proposal(), _oil_answers())
        self.assertTrue(applied.proposal.ready_for_spec_review)
        self.assertEqual(applied.remaining_ambiguities, [])
        self.assertEqual(len(applied.answered_ambiguity_ids), 7)
        self.assertEqual(
            applied.proposal.statistical_hypothesis.null_hypothesis,
            "H0: beta <= 0",
        )

    def test_partial_answers_remain_not_ready(self) -> None:
        proposal = _oil_proposal()
        full = _oil_answers(proposal)
        partial = full.model_copy(update={"answers": full.answers[:1]})
        applied = apply_clarification_answers(proposal, partial)
        self.assertFalse(applied.proposal.ready_for_spec_review)
        self.assertEqual(len(applied.remaining_ambiguities), 6)

    def test_unknown_duplicate_and_hash_mismatch_are_rejected(self) -> None:
        proposal = _oil_proposal()
        answers = _oil_answers(proposal)
        cases = (
            answers.model_copy(
                update={
                    "answers": [
                        answers.answers[0].model_copy(
                            update={"ambiguity_id": "ambiguity-0000000000000000"}
                        )
                    ]
                }
            ),
            answers.model_copy(update={"answers": [answers.answers[0]] * 2}),
            answers.model_copy(update={"proposal_sha256": "0" * 64}),
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(HypothesisLifecycleError):
                    apply_clarification_answers(proposal, case)

    def test_conflicting_field_updates_are_rejected(self) -> None:
        proposal = _oil_proposal()
        answers = _oil_answers(proposal)
        first = answers.answers[1]
        second = answers.answers[2].model_copy(update={"update": first.update})
        conflict = answers.model_copy(update={"answers": [first, second]})
        with self.assertRaises(HypothesisLifecycleError) as raised:
            apply_clarification_answers(proposal, conflict)
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.CLARIFICATION_CONFLICT,
        )

    def test_time_leakage_update_cannot_become_ready(self) -> None:
        proposal = _oil_proposal()
        answers = _oil_answers(proposal)
        payload = answers.model_dump(mode="json")
        payload["answers"][1]["update"]["variables"][0]["time_relation"] = {
            "relation": "follows_outcome",
            "lag_periods": 1,
            "available_before_outcome": False,
            "description": "available only after the outcome",
        }
        with self.assertRaises(HypothesisLifecycleError):
            apply_clarification_answers(
                proposal,
                parse_clarification_answers(_json_bytes(payload)),
            )

    def test_unsupported_request_cannot_be_washed_by_clarification(self) -> None:
        payload = _ready_association_payload()
        payload["original_question"] = "Place a trade for me."
        payload["unsupported_requests"] = [
            "Trading and order execution are unsupported."
        ]
        payload["ready_for_spec_review"] = False
        payload["ambiguities"] = ["The research framing must be reviewed."]
        proposal = parse_research_hypothesis_proposal(_json_bytes(payload))
        ref = proposal_ambiguity_references(proposal)[0]
        answers = parse_clarification_answers(
            _json_bytes({
                "proposal_sha256": (
                    calculate_research_hypothesis_proposal_sha256(proposal)
                ),
                "answers": [
                    {
                        "ambiguity_id": ref.ambiguity_id,
                        "update": {"sample": {"start_date": "2020-01-02"}},
                    }
                ],
            })
        )
        applied = apply_clarification_answers(proposal, answers)
        self.assertFalse(applied.proposal.ready_for_spec_review)
        self.assertEqual(applied.proposal.unsupported_requests, proposal.unsupported_requests)

    def test_strict_clarification_json_rejections(self) -> None:
        valid = serialize_clarification_answers(_oil_answers())
        cases = (
            b"```json\n" + valid + b"```",
            b"prefix " + valid,
            valid.replace(b'"answers":', b'"unknown":true,"answers":'),
            valid.replace(b'"answers":', b'"answers":[],"answers":'),
            valid.replace(b'"answers":', b'"nan":NaN,"answers":'),
        )
        for raw in cases:
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(HypothesisLifecycleError):
                    parse_clarification_answers(raw)

    def test_clarified_output_is_create_only_and_idempotent(self) -> None:
        applied = apply_clarification_answers(_oil_proposal(), _oil_answers())
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "clarified.json"
            first = persist_clarified_proposal(applied, output)
            first_bytes = first.read_bytes()
            persist_clarified_proposal(applied, output)
            self.assertEqual(output.read_bytes(), first_bytes)
            different = applied.model_copy(
                update={
                    "proposal": applied.proposal.model_copy(
                        update={"assumptions": ["A different reviewed assumption."]}
                    )
                }
            )
            with self.assertRaises(HypothesisLifecycleError):
                persist_clarified_proposal(different, output)

    def test_controls_replacement_combined_with_variable_update_is_rejected(self) -> None:
        # Deterministic contract: replacing controls cannot be combined with
        # variable-field updates touching a control id (old or replacement)
        # within one clarification round. The round fails closed instead of
        # silently preferring one side of the update.
        payload = _ready_association_payload()
        payload["controls"] = [
            {
                "variable_id": "control_market_volume",
                "concept_name": "US equity market volume log",
                "role": "control",
                "market_context": "United States equity market",
                "asset_type": "equity_index",
                "transformation": "log_return",
                "time_relation": {
                    "relation": "contemporaneous",
                    "lag_periods": 0,
                    "available_before_outcome": True,
                    "description": "same observation period as the outcome",
                },
                "proxy_for": None,
                "contract_roll_method": None,
            }
        ]
        payload["ambiguities"] = [
            "The control variable must be reviewed.",
            "Control timing must be reviewed.",
        ]
        payload["ready_for_spec_review"] = False
        proposal = parse_research_hypothesis_proposal(_json_bytes(payload))
        references = proposal_ambiguity_references(proposal)
        new_control = {
            "variable_id": "control_vix",
            "concept_name": "VIX level",
            "role": "control",
            "market_context": "United States equity options market",
            "asset_type": "equity_index",
            "transformation": "level",
            "time_relation": {
                "relation": "contemporaneous",
                "lag_periods": 0,
                "available_before_outcome": True,
                "description": "same observation period as the outcome",
            },
            "proxy_for": None,
            "contract_roll_method": None,
        }
        single_answer = parse_clarification_answers(
            _json_bytes({
                "proposal_sha256": (
                    calculate_research_hypothesis_proposal_sha256(proposal)
                ),
                "answers": [
                    {
                        "ambiguity_id": references[0].ambiguity_id,
                        "update": {
                            "controls": [new_control],
                            "variables": [
                                {
                                    "variable_id": "control_market_volume",
                                    "asset_type": "equity_index",
                                }
                            ],
                        },
                    }
                ],
            })
        )
        split_answers = parse_clarification_answers(
            _json_bytes({
                "proposal_sha256": (
                    calculate_research_hypothesis_proposal_sha256(proposal)
                ),
                "answers": [
                    {
                        "ambiguity_id": references[0].ambiguity_id,
                        "update": {"controls": [new_control]},
                    },
                    {
                        "ambiguity_id": references[1].ambiguity_id,
                        "update": {
                            "variables": [
                                {
                                    "variable_id": "control_market_volume",
                                    "asset_type": "equity_index",
                                }
                            ]
                        },
                    },
                ],
            })
        )
        for answers in (single_answer, split_answers):
            with self.subTest(answers=answers):
                with self.assertRaises(HypothesisLifecycleError) as raised:
                    apply_clarification_answers(proposal, answers)
                self.assertEqual(
                    raised.exception.failure.code,
                    HypothesisLifecycleErrorCode.CLARIFICATION_CONFLICT,
                )


class ConfirmationContractTest(unittest.TestCase):
    def test_ready_proposal_can_be_confirmed_and_round_trips(self) -> None:
        proposal = _ready_association()
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        raw = serialize_research_hypothesis_confirmation(confirmation)
        self.assertEqual(parse_research_hypothesis_confirmation(raw), confirmation)
        self.assertEqual(
            confirmation.proposal_sha256,
            calculate_research_hypothesis_proposal_sha256(proposal),
        )

    def test_not_ready_ambiguous_or_unsupported_proposal_cannot_confirm(self) -> None:
        for proposal in (
            _oil_proposal(),
            parse_research_hypothesis_proposal(
                _json_bytes(
                    {
                        **_ready_association_payload(),
                        "original_question": "Place a trade for me.",
                        "ambiguities": [],
                        "unsupported_requests": [
                            "Trading and order execution are unsupported."
                        ],
                        "ready_for_spec_review": False,
                    }
                )
            ),
        ):
            with self.subTest(proposal=proposal.original_question):
                with self.assertRaises(HypothesisLifecycleError):
                    confirm_research_hypothesis_proposal(
                        proposal,
                        confirmed_at=CONFIRMED_AT,
                    )

    def test_modified_proposal_invalidates_confirmation(self) -> None:
        proposal = _ready_association()
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        payload = proposal.model_dump(mode="json")
        payload["sample"]["start_date"] = "2020-01-02"
        modified = parse_research_hypothesis_proposal(_json_bytes(payload))
        with self.assertRaises(HypothesisLifecycleError):
            validate_confirmation_matches_proposal(modified, confirmation)

    def test_json_formatting_does_not_change_confirmation_identity(self) -> None:
        payload = _ready_association_payload()
        compact = parse_research_hypothesis_proposal(_json_bytes(payload))
        pretty = parse_research_hypothesis_proposal(
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        )
        self.assertEqual(
            calculate_research_hypothesis_proposal_sha256(compact),
            calculate_research_hypothesis_proposal_sha256(pretty),
        )

    def test_confirmation_rejects_unknown_duplicate_and_wrong_hash(self) -> None:
        proposal = _ready_association()
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        raw = serialize_research_hypothesis_confirmation(confirmation)
        cases = (
            raw.replace(b'"confirmed":true', b'"unknown":1,"confirmed":true'),
            raw.replace(
                b'"confirmed":true',
                b'"confirmed":true,"confirmed":true',
            ),
            raw.replace(confirmation.proposal_sha256.encode(), b"0" * 64),
        )
        for index, case in enumerate(cases):
            with self.subTest(index=index):
                if index < 2:
                    with self.assertRaises(HypothesisLifecycleError):
                        parse_research_hypothesis_confirmation(case)
                else:
                    parsed = parse_research_hypothesis_confirmation(case)
                    with self.assertRaises(HypothesisLifecycleError):
                        validate_confirmation_matches_proposal(proposal, parsed)


class ResearchSpecCompilerTest(unittest.TestCase):
    def test_matching_confirmation_compiles_deterministically(self) -> None:
        proposal = _ready_association()
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        first = compile_confirmed_research_spec(proposal, confirmation)
        second = compile_confirmed_research_spec(proposal, confirmation)
        self.assertEqual(first, second)
        self.assertEqual(first.research_spec.claim_type.value, "association")
        self.assertEqual(first.research_spec.model.method.value, "pearson_correlation")
        self.assertEqual(first.research_spec.model.null_hypothesis, "H0: rho = 0")
        self.assertEqual(first.research_spec.model.alternative_hypothesis, "H1: rho != 0")

    def test_compiled_result_passes_existing_research_spec_parser(self) -> None:
        proposal = _ready_association()
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        compiled = compile_confirmed_research_spec(proposal, confirmation)
        parsed = parse_research_spec(
            json.dumps(
                compiled.research_spec.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self.assertEqual(parsed, compiled.research_spec)

    def test_missing_compilation_inputs_are_structured_unresolved_requirements(self) -> None:
        applied = apply_clarification_answers(_oil_proposal(), _oil_answers())
        confirmation = confirm_research_hypothesis_proposal(
            applied.proposal,
            confirmed_at=CONFIRMED_AT,
        )
        with self.assertRaises(HypothesisLifecycleError) as raised:
            compile_confirmed_research_spec(applied.proposal, confirmation)
        failure = raised.exception.failure
        self.assertEqual(
            failure.code,
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_UNRESOLVED,
        )
        paths = {item.path for item in failure.unresolved_requirements}
        self.assertIn("research_spec_inputs", paths)
        self.assertIn("research_spec_inputs.variables", paths)
        self.assertIn("research_spec_inputs.minimum_observations", paths)

    def test_confirmation_hash_mismatch_blocks_compilation(self) -> None:
        proposal = _ready_association()
        confirmation = ResearchHypothesisProposalConfirmation(
            proposal_sha256="0" * 64,
            confirmed=True,
            confirmed_at=CONFIRMED_AT,
        )
        with self.assertRaises(HypothesisLifecycleError):
            compile_confirmed_research_spec(proposal, confirmation)

    def test_association_cannot_be_upgraded_while_compiling(self) -> None:
        proposal = _ready_association()
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        compiled = compile_confirmed_research_spec(proposal, confirmation)
        self.assertEqual(compiled.research_spec.claim_type, proposal.claim_type)
        self.assertEqual(
            compiled.research_spec.model.direction,
            proposal.statistical_hypothesis.direction,
        )

    def test_predictive_direction_hypotheses_and_lag_map_exactly(self) -> None:
        payload = _ready_association_payload()
        payload["original_question"] = (
            "Do prior USD index returns positively predict later US equity returns?"
        )
        payload["normalized_research_question"] = payload["original_question"]
        payload["claim_type"] = "predictive"
        payload["predictors"][0]["time_relation"] = {
            "relation": "precedes_outcome",
            "lag_periods": 1,
            "available_before_outcome": True,
            "description": "known one outcome period before the outcome",
        }
        statistical = payload["statistical_hypothesis"]
        statistical.update(
            {
                "statistical_method": "lead_lag_regression",
                "target_parameter": {
                    "kind": "regression_coefficient",
                    "predictor_variable_id": "usd_index_return",
                    "reference_value": 0,
                },
                "direction": "positive",
                "null_hypothesis": "H0: beta <= 0",
                "alternative_hypothesis": "H1: beta > 0",
            }
        )
        proposal = parse_research_hypothesis_proposal(_json_bytes(payload))
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        spec = compile_confirmed_research_spec(proposal, confirmation).research_spec
        self.assertEqual(spec.claim_type.value, "predictive")
        self.assertEqual(spec.model.method.value, "lead_lag_regression")
        self.assertEqual(spec.model.direction.value, "positive")
        self.assertEqual(spec.model.null_hypothesis, "H0: beta <= 0")
        self.assertEqual(spec.model.alternative_hypothesis, "H1: beta > 0")
        self.assertEqual(spec.predictors[0].lag_periods, 1)

    def test_persisted_spec_and_provenance_strictly_reload(self) -> None:
        proposal = _ready_association()
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        compiled = compile_confirmed_research_spec(proposal, confirmation)
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "research-spec.json"
            first = persist_compiled_research_spec(compiled, output)
            first_spec = first.research_spec_path.read_bytes()
            first_provenance = first.provenance_path.read_bytes()
            second = persist_compiled_research_spec(compiled, output)
            self.assertEqual(second.research_spec_path.read_bytes(), first_spec)
            self.assertEqual(second.provenance_path.read_bytes(), first_provenance)
            self.assertEqual(parse_research_spec(first_spec), compiled.research_spec)

    def test_different_existing_output_is_never_overwritten(self) -> None:
        proposal = _ready_association()
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        compiled = compile_confirmed_research_spec(proposal, confirmation)
        with TemporaryDirectory(dir=ROOT) as directory:
            output = Path(directory) / "research-spec.json"
            output.write_text("{}", encoding="utf-8")
            (Path(str(output) + ".provenance.json")).write_text(
                "{}", encoding="utf-8"
            )
            with self.assertRaises(HypothesisLifecycleError):
                persist_compiled_research_spec(compiled, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "{}")

    def test_compiler_has_no_data_network_analysis_or_backtest_side_effects(self) -> None:
        proposal = _ready_association()
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        before = set(ROOT.rglob("*"))
        compile_confirmed_research_spec(proposal, confirmation)
        after = set(ROOT.rglob("*"))
        self.assertEqual(before, after)

    def test_compile_rejects_not_ready_proposal_before_hash_checks(self) -> None:
        payload = _ready_association_payload()
        payload["ready_for_spec_review"] = False
        payload["ambiguities"] = ["The research framing must be reviewed."]
        proposal = parse_research_hypothesis_proposal(_json_bytes(payload))
        # Readiness is checked before confirmation binding in the compiler, so
        # this structurally valid but non-matching confirmation still reaches
        # the not-ready failure first.
        confirmation = ResearchHypothesisProposalConfirmation(
            proposal_sha256="0" * 64,
            confirmed=True,
            confirmed_at=CONFIRMED_AT,
        )
        with self.assertRaises(HypothesisLifecycleError) as raised:
            compile_confirmed_research_spec(proposal, confirmation)
        failure = raised.exception.failure
        self.assertEqual(
            failure.code,
            HypothesisLifecycleErrorCode.PROPOSAL_NOT_READY,
        )
        self.assertEqual(failure.unresolved_requirements, [])

    def test_explicit_default_fields_change_hash_but_keep_semantic_equality(self) -> None:
        # Compatibility contract: Proposal identity is the current canonical
        # serialized representation. Whether a default-valued field was
        # explicitly provided is part of that representation, so explicitly
        # providing a default and omitting it keep the parsed models
        # semantically equal yet produce different canonical hashes. This
        # conservative anti-drift rule is intentional, not accidental; it is
        # locked here so a future semantic-canonical change needs a deliberate
        # schema/version and confirmation migration.
        payload = _ready_association_payload()
        explicit = parse_research_hypothesis_proposal(_json_bytes(payload))
        omitted_payload = deepcopy(payload)
        omitted_payload["research_spec_inputs"]["variables"][0].pop(
            "revision_policy"
        )
        omitted = parse_research_hypothesis_proposal(_json_bytes(omitted_payload))
        self.assertEqual(explicit, omitted)
        self.assertNotEqual(
            calculate_research_hypothesis_proposal_sha256(explicit),
            calculate_research_hypothesis_proposal_sha256(omitted),
        )
        # Canonical bytes are stable for the same parsed model.
        self.assertEqual(
            calculate_research_hypothesis_proposal_sha256(explicit),
            calculate_research_hypothesis_proposal_sha256(
                parse_research_hypothesis_proposal(
                    serialize_research_hypothesis_proposal(explicit)
                )
            ),
        )

    def test_follows_outcome_ready_confirmable_but_compile_unresolved(self) -> None:
        payload = _ready_association_payload()
        payload["predictors"][0]["time_relation"] = {
            "relation": "follows_outcome",
            "lag_periods": 1,
            "available_before_outcome": False,
            "description": "known one outcome period after the outcome",
        }
        proposal = parse_research_hypothesis_proposal(_json_bytes(payload))
        self.assertEqual(proposal.readiness_blockers(), [])
        self.assertTrue(proposal.ready_for_spec_review)
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        with self.assertRaises(HypothesisLifecycleError) as raised:
            compile_confirmed_research_spec(proposal, confirmation)
        failure = raised.exception.failure
        self.assertEqual(
            failure.code,
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_UNRESOLVED,
        )
        paths = {item.path for item in failure.unresolved_requirements}
        self.assertIn("usd_index_return.time_relation", paths)
        # The proposal remains ready and the confirmation stays valid; no spec
        # bytes or sidecar are produced by the in-memory compile failure.
        self.assertTrue(proposal.ready_for_spec_review)
        validate_confirmation_matches_proposal(proposal, confirmation)

    def test_asset_type_conflict_returns_research_spec_invalid(self) -> None:
        payload = _ready_association_payload()
        payload["research_spec_inputs"]["variables"][0]["instrument"][
            "asset_type"
        ] = "fx"
        proposal = parse_research_hypothesis_proposal(_json_bytes(payload))
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        with self.assertRaises(HypothesisLifecycleError) as raised:
            compile_confirmed_research_spec(proposal, confirmation)
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID,
        )

    def test_same_market_with_cross_market_policies_is_rejected(self) -> None:
        payload = _ready_association_payload()
        inputs = payload["research_spec_inputs"]
        inputs["join_policy"] = "strict_match"
        inputs["max_staleness_days"] = 0
        inputs["missing_data_policy"] = "keep_missing"
        proposal = parse_research_hypothesis_proposal(_json_bytes(payload))
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        with self.assertRaises(HypothesisLifecycleError) as raised:
            compile_confirmed_research_spec(proposal, confirmation)
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID,
        )

    def test_cross_market_mapping_without_alignment_is_rejected(self) -> None:
        payload = _ready_association_payload()
        predictor_input = payload["research_spec_inputs"]["variables"][1]
        predictor_input["instrument"]["market"] = "China"
        predictor_input["instrument"]["timezone"] = "Asia/Shanghai"
        proposal = parse_research_hypothesis_proposal(_json_bytes(payload))
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        with self.assertRaises(HypothesisLifecycleError) as raised:
            compile_confirmed_research_spec(proposal, confirmation)
        self.assertEqual(
            raised.exception.failure.code,
            HypothesisLifecycleErrorCode.RESEARCH_SPEC_INVALID,
        )

    def test_inconsistent_compiled_identity_is_rejected(self) -> None:
        proposal = _ready_association()
        confirmation = confirm_research_hypothesis_proposal(
            proposal,
            confirmed_at=CONFIRMED_AT,
        )
        compiled = compile_confirmed_research_spec(proposal, confirmation)
        with self.assertRaises(ValueError):
            CompiledResearchSpec(
                proposal_sha256=compiled.proposal_sha256,
                confirmation_sha256=compiled.confirmation_sha256,
                research_spec_sha256="0" * 64,
                research_spec=compiled.research_spec,
                provenance=compiled.provenance,
            )


if __name__ == "__main__":
    unittest.main()
