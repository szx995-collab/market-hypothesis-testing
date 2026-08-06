"""Interpretation tests (v0.4.0 Phase 3, 18 required)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from market_validator.analysis.execution_models import (
    parse_analysis_result,
)
from market_validator.interpretation import (
    FixtureLLMClient,
    interpret_analysis_result,
)
from market_validator.interpretation.evidence import (
    build_analysis_evidence_package,
    derive_overall_conclusion,
)
from market_validator.interpretation.llm_client import (
    ChatCompletionsHTTPClient,
)
from market_validator.interpretation.models import (
    AnalysisInterpretation,
    InterpretationError,
    InterpretationErrorCode,
    TestInterpretation,
    calculate_analysis_evidence_package_sha256,
    parse_analysis_interpretation,
)
from market_validator.interpretation.report import (
    persist_analysis_interpretation,
    render_validation_report_markdown,
    verify_persisted_analysis_interpretation,
)
from market_validator.interpretation.validation import (
    validate_interpretation_matches_evidence,
)
from tests.test_analysis_execution import _execute

T0 = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


def _verified_context(tmp: Path):
    build, outcome, _receipt, result_root = _execute(tmp)
    result = parse_analysis_result(
        (
            result_root
            / "analysis-runs"
            / "attempt-exec-1"
            / "analysis-result.json"
        ).read_bytes()
    )
    return build, result, result_root


def _make_evidence(tmp: Path):
    build, result, _root = _verified_context(tmp)
    package = build_analysis_evidence_package(
        research_spec=build["spec"],
        analysis_plan=build["plan"],
        analysis_result=result,
        data_ready_manifest_sha256=build["plan"].data_ready_manifest_sha256,
        provenance={"source": "test"},
    )
    return build, result, package


def _fixture_interpretation(package, result):
    client = FixtureLLMClient()
    from market_validator.interpretation.prompting import (
        build_interpretation_prompt,
    )

    prompt = build_interpretation_prompt(package, language="zh-CN")
    raw = client.generate_interpretation_json(
        package, language="zh-CN", prompt=prompt
    )
    candidate = AnalysisInterpretation(
        **{
            **raw,
            "interpretation_schema_version": "1.0",
            "interpretation_id": "pending",
            "evidence_sha256": calculate_analysis_evidence_package_sha256(
                package
            ),
            "analysis_result_id": result.analysis_result_id,
        }
    )
    from market_validator.interpretation.models import (
        _derive_interpretation_id,
    )

    return candidate.model_copy(
        update={
            "interpretation_id": _derive_interpretation_id(candidate)
        }
    )


class OverallConclusionTest(unittest.TestCase):
    def test_single_test_conclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            _build, result, package = _make_evidence(Path(tmp))
            self.assertEqual(
                package.overall_conclusion,
                result.primary_test_results[0].conclusion,
            )

    def test_all_supported(self):
        self.assertEqual(
            derive_overall_conclusion(["supported", "supported"]),
            "supported",
        )

    def test_mixed(self):
        self.assertEqual(
            derive_overall_conclusion(["supported", "inconclusive"]),
            "mixed",
        )
        self.assertEqual(
            derive_overall_conclusion(
                ["supported", "not_supported", "inconclusive"]
            ),
            "mixed",
        )


class EvidencePackageTest(unittest.TestCase):
    def test_deterministic_and_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _build, _result, package = _make_evidence(Path(tmp))
            second = build_analysis_evidence_package(
                research_spec=_build["spec"],
                analysis_plan=_build["plan"],
                analysis_result=_result,
                data_ready_manifest_sha256=_build[
                    "plan"
                ].data_ready_manifest_sha256,
                provenance={"source": "test"},
            )
        self.assertEqual(
            calculate_analysis_evidence_package_sha256(package),
            calculate_analysis_evidence_package_sha256(second),
        )
        self.assertEqual(package.evidence_id, second.evidence_id)

    def test_no_raw_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            _build, _result, package = _make_evidence(Path(tmp))
            payload = package.model_dump_json()
        self.assertNotIn("observations", payload)
        self.assertNotIn('"rows"', payload)
        self.assertNotIn("residuals", payload)

    def test_deterministic_statements_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            _build, _result, package = _make_evidence(Path(tmp))
        for item in package.primary_test_evidence:
            self.assertTrue(item.deterministic_statement)

    def test_fixed_limitations_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            _build, _result, package = _make_evidence(Path(tmp))
        self.assertTrue(
            any("robustness" in item for item in package.limitations)
        )
        self.assertTrue(
            any("因果" in item for item in package.limitations)
        )


class GroundingTest(unittest.TestCase):
    def test_fixture_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            validate_interpretation_matches_evidence(
                package,
                interpretation,
                expected_analysis_result_id=result.analysis_result_id,
            )

    def test_conclusion_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            changed = interpretation.model_copy(
                update={"acknowledged_overall_conclusion": "mixed"}
            )
            with self.assertRaises(InterpretationError) as caught:
                validate_interpretation_matches_evidence(
                    package,
                    changed,
                    expected_analysis_result_id=result.analysis_result_id,
                )
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.LLM_CONCLUSION_MISMATCH,
        )

    def test_unknown_test_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            extra = TestInterpretation(
                test_id="0000000000000000",
                explanation="extra test",
            )
            changed = interpretation.model_copy(
                update={
                    "test_interpretations": (
                        interpretation.test_interpretations + [extra]
                    )
                }
            )
            with self.assertRaises(InterpretationError) as caught:
                validate_interpretation_matches_evidence(
                    package,
                    changed,
                    expected_analysis_result_id=result.analysis_result_id,
                )
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.UNKNOWN_TEST_REFERENCE,
        )

    def test_missing_test_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            changed = interpretation.model_copy(
                update={"test_interpretations": []}
            )
            with self.assertRaises(InterpretationError) as caught:
                validate_interpretation_matches_evidence(
                    package,
                    changed,
                    expected_analysis_result_id=result.analysis_result_id,
                )
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.MISSING_TEST_INTERPRETATION,
        )

    def test_invented_numbers_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            changed = interpretation.model_copy(
                update={
                    "plain_language_summary": "the p value is 0.05 and "
                    "very small"
                }
            )
            with self.assertRaises(InterpretationError) as caught:
                validate_interpretation_matches_evidence(
                    package,
                    changed,
                    expected_analysis_result_id=result.analysis_result_id,
                )
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.UNGROUNDED_NUMERIC_CLAIM,
        )

    def test_duplicate_test_reference_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            duplicate = [
                interpretation.test_interpretations[0],
                interpretation.test_interpretations[0],
            ]
            changed = interpretation.model_copy(
                update={"test_interpretations": duplicate}
            )
            with self.assertRaises(InterpretationError) as caught:
                validate_interpretation_matches_evidence(
                    package,
                    changed,
                    expected_analysis_result_id=result.analysis_result_id,
                )
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
        )

    def test_fullwidth_digit_rejected(self):
        # NFKC normalization must catch fullwidth digits
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            changed = interpretation.model_copy(
                update={"headline": "effect size is ０.９ huge"}
            )
            with self.assertRaises(InterpretationError) as caught:
                validate_interpretation_matches_evidence(
                    package,
                    changed,
                    expected_analysis_result_id=result.analysis_result_id,
                )
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.UNGROUNDED_NUMERIC_CLAIM,
        )

    def test_spaced_advice_word_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            changed = interpretation.model_copy(
                update={"headline": "you should b u y this"}
            )
            with self.assertRaises(InterpretationError) as caught:
                validate_interpretation_matches_evidence(
                    package,
                    changed,
                    expected_analysis_result_id=result.analysis_result_id,
                )
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.FORBIDDEN_ADVICE,
        )

    def test_warnings_numeric_claim_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            changed = interpretation.model_copy(
                update={"warnings": ["effect size 0.9 is large"]}
            )
            with self.assertRaises(InterpretationError) as caught:
                validate_interpretation_matches_evidence(
                    package,
                    changed,
                    expected_analysis_result_id=result.analysis_result_id,
                )
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.UNGROUNDED_NUMERIC_CLAIM,
        )

    def test_trading_advice_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            changed = interpretation.model_copy(
                update={"headline": "you should buy this asset now"}
            )
            with self.assertRaises(InterpretationError) as caught:
                validate_interpretation_matches_evidence(
                    package,
                    changed,
                    expected_analysis_result_id=result.analysis_result_id,
                )
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.FORBIDDEN_ADVICE,
        )

    def test_malformed_json_rejected(self):
        with self.assertRaises(InterpretationError) as caught:
            parse_analysis_interpretation(b"not json at all")
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.INVALID_INTERPRETATION_INPUT,
        )


class LLMClientTest(unittest.TestCase):
    def test_network_disabled_by_default(self):
        client = ChatCompletionsHTTPClient(
            endpoint_url="https://example.invalid/v1/chat/completions",
            model="m",
            api_key="k",
        )
        with self.assertRaises(InterpretationError) as caught:
            client.generate_interpretation_json(
                None, language="zh-CN", prompt="p"
            )
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.LLM_NETWORK_NOT_AUTHORIZED,
        )

    def test_http_loopback_mock_success(self):
        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "acknowledged_overall_conclusion": (
                                    "inconclusive"
                                ),
                                "language": "zh-CN",
                                "headline": "h",
                                "plain_language_summary": "s",
                                "test_interpretations": [
                                    {"test_id": "t1", "explanation": "e"}
                                ],
                                "limitations_explanation": "l",
                                "cannot_conclude": ["c"],
                                "suggested_followups": ["执行 robustness"],
                                "model_metadata": {"model": "mock"},
                                "warnings": [],
                            }
                        )
                    }
                }
            ]
        }

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                auth = self.headers.get("Authorization", "")
                if "Bearer secret-key" not in auth:
                    self.send_response(401)
                    self.end_headers()
                    return
                payload = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ChatCompletionsHTTPClient(
                endpoint_url=(
                    f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
                ),
                model="mock-model",
                api_key="secret-key",
                allow_network=True,
            )
            with tempfile.TemporaryDirectory() as tmp:
                build, result, package = _make_evidence(Path(tmp))
                from market_validator.interpretation.prompting import (
                    build_interpretation_prompt,
                )

                prompt = build_interpretation_prompt(
                    package, language="zh-CN"
                )
                raw = client.generate_interpretation_json(
                    package, language="zh-CN", prompt=prompt
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(
            raw["acknowledged_overall_conclusion"], "inconclusive"
        )

    def test_http_error_no_retry(self):
        request_count = {"n": 0}

        class ErrorHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                request_count["n"] += 1
                self.send_response(500)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), ErrorHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ChatCompletionsHTTPClient(
                endpoint_url=(
                    f"http://127.0.0.1:{server.server_port}/chat"
                ),
                model="m",
                api_key="k",
                allow_network=True,
                timeout_seconds=5,
            )
            with tempfile.TemporaryDirectory() as tmp:
                build, result, package = _make_evidence(Path(tmp))
                from market_validator.interpretation.prompting import (
                    build_interpretation_prompt,
                )

                prompt = build_interpretation_prompt(
                    package, language="zh-CN"
                )
                with self.assertRaises(InterpretationError) as caught:
                    client.generate_interpretation_json(
                        package, language="zh-CN", prompt=prompt
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.LLM_REQUEST_FAILED,
        )
        self.assertEqual(request_count["n"], 1, "must not retry")


class PersistenceTest(unittest.TestCase):
    def test_credential_not_in_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            report = render_validation_report_markdown(
                package,
                interpretation,
                model_identifier="fixture-v1",
            )
            final_dir = persist_analysis_interpretation(
                evidence_package=package,
                interpretation=interpretation,
                validation_report=report,
                model_identifier="fixture-v1",
                result_root=Path(tmp) / "results",
                created_at=T0,
            )
            verified = verify_persisted_analysis_interpretation(
                Path(tmp) / "results",
                interpretation.analysis_result_id,
                interpretation.interpretation_id,
            )
            for artifact in final_dir.iterdir():
                content = artifact.read_bytes().lower()
                self.assertNotIn(b"api_key", content)
                self.assertNotIn(b"authorization", content)
                self.assertNotIn(b"secret", content)
        self.assertEqual(verified, interpretation)

    def test_tampering_fails_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            report = render_validation_report_markdown(
                package,
                interpretation,
                model_identifier="fixture-v1",
            )
            final_dir = persist_analysis_interpretation(
                evidence_package=package,
                interpretation=interpretation,
                validation_report=report,
                model_identifier="fixture-v1",
                result_root=Path(tmp) / "results",
                created_at=T0,
            )
            target = final_dir / "interpretation.json"
            data = json.loads(target.read_text(encoding="utf-8"))
            data["warnings"] = ["tampered"]
            target.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(InterpretationError) as caught:
                verify_persisted_analysis_interpretation(
                    Path(tmp) / "results",
                    interpretation.analysis_result_id,
                    interpretation.interpretation_id,
                )
        self.assertEqual(
            caught.exception.code,
            InterpretationErrorCode.INTERPRETATION_OUTPUT_ERROR,
        )


class MarkdownTest(unittest.TestCase):
    def test_supported_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            interpretation = _fixture_interpretation(package, result)
            report = render_validation_report_markdown(
                package,
                interpretation,
                model_identifier="fixture-v1",
            ).decode("utf-8")
        self.assertIn("# 假设验证结果", report)
        self.assertIn("## 主要统计结果", report)
        self.assertIn("| test | estimate |", report)
        self.assertIn("## 免责声明", report)
        self.assertIn("不构成投资或交易建议", report)

    def test_mixed_report_wording(self):
        from market_validator.interpretation.models import (
            AnalysisEvidencePackage,
            PrimaryTestEvidence,
        )

        with tempfile.TemporaryDirectory() as tmp:
            build, result, package = _make_evidence(Path(tmp))
            mixed_package = package.model_copy(
                update={
                    "overall_conclusion": "mixed",
                    "primary_test_evidence": [
                        PrimaryTestEvidence(
                            **item.model_dump()
                        )
                        for item in package.primary_test_evidence
                    ],
                }
            )
            interpretation = _fixture_interpretation(mixed_package, result)
            report = render_validation_report_markdown(
                mixed_package,
                interpretation,
                model_identifier="fixture-v1",
            ).decode("utf-8")
        self.assertIn("不一致", report)

    def test_end_to_end_orchestration(self):
        with tempfile.TemporaryDirectory() as tmp:
            build, result, _root = _verified_context(Path(tmp))
            outcome = interpret_analysis_result(
                research_spec=build["spec"],
                analysis_plan=build["plan"],
                analysis_result=result,
                data_ready_manifest_sha256=build[
                    "plan"
                ].data_ready_manifest_sha256,
                llm_client=FixtureLLMClient(),
                language="zh-CN",
                style="concise",
                model_identifier="fixture-v1",
                result_root=Path(tmp) / "interpretations",
                created_at=T0,
            )
            report = outcome["report"].decode("utf-8")
            persisted = outcome["persisted_path"]
            self.assertTrue(persisted.is_dir())
            self.assertIn("## 结论", report)
            self.assertTrue(
                (persisted / "interpretation-manifest.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
