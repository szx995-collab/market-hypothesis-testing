"""Offline tests for proposal-only AI providers and their CLI boundary."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import fields
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from market_validator.cli import main
from market_validator.planning import (
    compile_confirmed_workflow_plan,
    parse_plan_proposal_confirmation,
    serialize_market_validation_plan_proposal,
)
from market_validator.plan_providers import (
    DeepSeekApiPlanProposalProvider,
    PlanProposalGenerationRequest,
    PlanProposalProviderError,
    PlanProposalProviderErrorCode,
    PlanProposalProviderFailure,
    PlanProposalProviderStage,
    RawPlanProposalResponse,
    create_plan_proposal_provider,
    generate_market_validation_plan_proposal,
    persist_generated_plan_proposal,
)
from market_validator.plan_providers.deepseek_api import (
    DeepSeekTransportError,
    DeepSeekTransportTimeout,
    UrllibDeepSeekPlanTransport,
)
from market_validator.workflow import run_market_validation_workflow
from market_validator.workflow_cli import WorkflowCliExitCode


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PARENT = ROOT / "tests" / ".runtime_plan_providers"
PROPOSAL_PATH = (
    ROOT
    / "examples"
    / "ai_planning"
    / "wti_price_change_volatility.proposal.json"
)
CONFIRMATION_PATH = (
    ROOT
    / "examples"
    / "ai_planning"
    / "wti_price_change_volatility.confirmation.json"
)
QUESTION = "2020-03-01 至 2020-05-31 的 WTI 相邻有效报价价格变化波动，是否高于 2021-01-01 至 2024-12-31？"
MODEL = "deepseek-v4-flash"
SENTINEL_KEY = "test-secret-key-never-expose"


class FakePlanProposalProvider:
    def __init__(
        self,
        content: bytes,
        *,
        error: Exception | None = None,
        name: str = "fake_provider",
        model: str = "fake-model",
    ) -> None:
        self._content = content
        self._error = error
        self._name = name
        self._model = model
        self.requests: list[PlanProposalGenerationRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    def generate_proposal(
        self,
        request: PlanProposalGenerationRequest,
    ) -> RawPlanProposalResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return RawPlanProposalResponse(
            content=self._content,
            provider=self.name,
            model=self.model,
        )


class FakeDeepSeekTransport:
    def __init__(self, response: bytes | None = None, error: Exception | None = None):
        self.response = response or b"{}"
        self.error = error
        self.calls: list[dict[str, object]] = []

    def post_json(self, **kwargs: object) -> bytes:
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def _provider_error(
    code: PlanProposalProviderErrorCode,
) -> PlanProposalProviderError:
    stage = (
        PlanProposalProviderStage.PROVIDER_CONFIGURATION
        if code is PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING
        else PlanProposalProviderStage.PROVIDER_REQUEST
    )
    return PlanProposalProviderError(
        PlanProposalProviderFailure(
            code=code,
            stage=stage,
            message="safe synthetic provider failure",
        )
    )


class PlanProposalProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = RUNTIME_PARENT / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.raw_proposal = PROPOSAL_PATH.read_bytes()

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        if RUNTIME_PARENT.exists() and not any(RUNTIME_PARENT.iterdir()):
            RUNTIME_PARENT.rmdir()

    @staticmethod
    def _deepseek_response(content: bytes, finish_reason: str = "stop") -> bytes:
        return json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {
                            "role": "assistant",
                            "content": content.decode("utf-8"),
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def test_provider_receives_only_the_four_allowed_inputs(self) -> None:
        provider = FakePlanProposalProvider(self.raw_proposal)
        with (
            mock.patch("market_validator.planning.load_data_bundle") as load_bundle,
            mock.patch("market_validator.workflow.run_market_validation_workflow") as run,
        ):
            generated = generate_market_validation_plan_proposal(
                provider,
                "  " + QUESTION + "\n",
            )
        load_bundle.assert_not_called()
        run.assert_not_called()
        self.assertEqual(len(provider.requests), 1)
        request = provider.requests[0]
        self.assertEqual(
            {item.name for item in fields(PlanProposalGenerationRequest)},
            {
                "original_market_question",
                "system_prompt",
                "output_schema",
                "supported_capabilities",
            },
        )
        self.assertEqual(request.original_market_question, QUESTION)
        self.assertEqual(
            set(request.supported_capabilities),
            {"price_change_volatility"},
        )
        schema_text = json.dumps(request.output_schema, sort_keys=True)
        for forbidden in (
            "bundle_path",
            "expected_source_request_id",
            "expected_source_bundle_sha256",
            "artifact_id",
            "expected_artifact_manifest_sha256",
            "api_key",
        ):
            self.assertNotIn(forbidden, schema_text)
        self.assertEqual(generated.proposal.original_market_question, QUESTION)

    def test_every_raw_output_goes_through_existing_strict_parser(self) -> None:
        provider = FakePlanProposalProvider(self.raw_proposal)
        with mock.patch(
            "market_validator.plan_providers.service.parse_market_validation_plan_proposal",
            wraps=__import__(
                "market_validator.planning",
                fromlist=["parse_market_validation_plan_proposal"],
            ).parse_market_validation_plan_proposal,
        ) as parser:
            generated = generate_market_validation_plan_proposal(provider, QUESTION)
        parser.assert_called_once_with(self.raw_proposal)
        self.assertEqual(
            serialize_market_validation_plan_proposal(generated.proposal),
            serialize_market_validation_plan_proposal(generated.proposal),
        )

    def test_malformed_fenced_prose_unknown_and_changed_question_are_rejected(self) -> None:
        parsed = json.loads(self.raw_proposal)
        unknown = dict(parsed)
        unknown["bundle_path"] = "C:/not-allowed.json"
        changed = dict(parsed)
        changed["original_market_question"] = "different"
        candidates = {
            "malformed": b"{bad-json",
            "fenced": b"```json\n" + self.raw_proposal + b"\n```",
            "prefix": b"proposal follows\n" + self.raw_proposal,
            "unknown": json.dumps(unknown, ensure_ascii=False).encode("utf-8"),
            "changed_question": json.dumps(changed, ensure_ascii=False).encode("utf-8"),
        }
        for name, content in candidates.items():
            with self.subTest(name=name):
                with self.assertRaises(PlanProposalProviderError) as caught:
                    generate_market_validation_plan_proposal(
                        FakePlanProposalProvider(content),
                        QUESTION,
                    )
                self.assertEqual(
                    caught.exception.failure.code,
                    PlanProposalProviderErrorCode.PROVIDER_INVALID_PROPOSAL,
                )

    def test_generic_provider_failure_and_timeout_are_sanitized(self) -> None:
        candidates = (
            (RuntimeError(SENTINEL_KEY), PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED),
            (TimeoutError(SENTINEL_KEY), PlanProposalProviderErrorCode.PROVIDER_TIMEOUT),
        )
        for error, expected in candidates:
            with self.subTest(code=expected):
                with self.assertRaises(PlanProposalProviderError) as caught:
                    generate_market_validation_plan_proposal(
                        FakePlanProposalProvider(self.raw_proposal, error=error),
                        QUESTION,
                    )
                self.assertEqual(caught.exception.failure.code, expected)
                self.assertNotIn(SENTINEL_KEY, str(caught.exception))

    def test_deepseek_adapter_body_has_no_tools_or_credentials(self) -> None:
        transport = FakeDeepSeekTransport(
            self._deepseek_response(self.raw_proposal)
        )
        provider = DeepSeekApiPlanProposalProvider(
            model=MODEL,
            environment={
                "DEEPSEEK_API_KEY": SENTINEL_KEY,
                "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
            },
            transport=transport,
            allow_network=True,
        )
        generated = generate_market_validation_plan_proposal(provider, QUESTION)
        self.assertEqual(generated.provider, "deepseek_api")
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        body_bytes = call["body"]
        self.assertIsInstance(body_bytes, bytes)
        body = json.loads(body_bytes)
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["stream"])
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertEqual(body["messages"][1], {"role": "user", "content": QUESTION})
        self.assertNotIn(SENTINEL_KEY, body_bytes.decode("utf-8"))
        self.assertEqual(
            call["headers"]["Authorization"],
            f"Bearer {SENTINEL_KEY}",
        )

    def test_deepseek_configuration_network_errors_timeout_and_refusal(self) -> None:
        missing = DeepSeekApiPlanProposalProvider(
            model=MODEL,
            environment={},
            transport=FakeDeepSeekTransport(),
            allow_network=True,
        )
        with self.assertRaises(PlanProposalProviderError) as missing_error:
            generate_market_validation_plan_proposal(missing, QUESTION)
        self.assertEqual(
            missing_error.exception.failure.code,
            PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
        )

        not_allowed = DeepSeekApiPlanProposalProvider(
            model=MODEL,
            environment={"DEEPSEEK_API_KEY": SENTINEL_KEY},
            transport=FakeDeepSeekTransport(),
        )
        with self.assertRaises(PlanProposalProviderError) as network_error:
            generate_market_validation_plan_proposal(not_allowed, QUESTION)
        self.assertEqual(
            network_error.exception.failure.code,
            PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
        )

        cases = (
            (
                FakeDeepSeekTransport(error=DeepSeekTransportError(SENTINEL_KEY)),
                PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED,
            ),
            (
                FakeDeepSeekTransport(error=DeepSeekTransportTimeout(SENTINEL_KEY)),
                PlanProposalProviderErrorCode.PROVIDER_TIMEOUT,
            ),
            (
                FakeDeepSeekTransport(
                    response=self._deepseek_response(
                        self.raw_proposal,
                        finish_reason="content_filter",
                    )
                ),
                PlanProposalProviderErrorCode.PROVIDER_REFUSED,
            ),
        )
        for transport, expected in cases:
            provider = DeepSeekApiPlanProposalProvider(
                model=MODEL,
                environment={"DEEPSEEK_API_KEY": SENTINEL_KEY},
                transport=transport,
                allow_network=True,
            )
            with self.subTest(code=expected):
                with self.assertRaises(PlanProposalProviderError) as caught:
                    generate_market_validation_plan_proposal(provider, QUESTION)
                self.assertEqual(caught.exception.failure.code, expected)
                self.assertNotIn(SENTINEL_KEY, str(caught.exception))
                self.assertEqual(len(transport.calls), 1)

    def test_deepseek_timeout_response_size_and_retry_count_are_bounded(self) -> None:
        for timeout in (0.0, -1.0, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(
                PlanProposalProviderError
            ) as caught:
                DeepSeekApiPlanProposalProvider(
                    model=MODEL,
                    environment={"DEEPSEEK_API_KEY": SENTINEL_KEY},
                    transport=FakeDeepSeekTransport(),
                    timeout_seconds=timeout,
                    allow_network=True,
                )
            self.assertEqual(
                caught.exception.failure.code,
                PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
            )

        class OversizedResponse:
            requested_amount: int | None = None

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, amount: int) -> bytes:
                self.requested_amount = amount
                return b"x" * amount

        response = OversizedResponse()
        transport = UrllibDeepSeekPlanTransport()
        with (
            mock.patch(
                "market_validator.plan_providers.deepseek_api.MAX_RESPONSE_BYTES",
                10,
            ),
            mock.patch(
                "market_validator.plan_providers.deepseek_api.urlopen",
                return_value=response,
            ) as opener,
            self.assertRaises(DeepSeekTransportError),
        ):
            transport.post_json(
                url="https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {SENTINEL_KEY}"},
                body=b"{}",
                timeout_seconds=1.0,
            )
        opener.assert_called_once()
        self.assertEqual(response.requested_amount, 11)

    def test_factory_rejects_unknown_provider_without_network(self) -> None:
        with self.assertRaises(PlanProposalProviderError) as caught:
            create_plan_proposal_provider(
                "unknown",
                MODEL,
                environment={"DEEPSEEK_API_KEY": SENTINEL_KEY},
                transport=FakeDeepSeekTransport(),
                allow_network=True,
            )
        self.assertEqual(
            caught.exception.failure.code,
            PlanProposalProviderErrorCode.PROVIDER_CONFIGURATION_MISSING,
        )

    def test_proposal_persistence_is_atomic_idempotent_and_conflict_safe(self) -> None:
        generated = generate_market_validation_plan_proposal(
            FakePlanProposalProvider(self.raw_proposal),
            QUESTION,
        )
        output = self.root / "proposal.json"
        first = persist_generated_plan_proposal(generated, output)
        original = output.read_bytes()
        mtime = output.stat().st_mtime_ns
        second = persist_generated_plan_proposal(generated, output)
        self.assertEqual(first, second)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(output.stat().st_mtime_ns, mtime)
        self.assertEqual(first.sha256, hashlib.sha256(original).hexdigest())

        changed_payload = json.loads(self.raw_proposal)
        changed_payload["assumptions"] = [*changed_payload["assumptions"], "Changed."]
        changed_raw = json.dumps(changed_payload, ensure_ascii=False).encode("utf-8")
        changed_generated = generate_market_validation_plan_proposal(
            FakePlanProposalProvider(changed_raw),
            QUESTION,
        )
        with self.assertRaises(PlanProposalProviderError) as conflict:
            persist_generated_plan_proposal(changed_generated, output)
        self.assertEqual(
            conflict.exception.failure.code,
            PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_CONFLICT,
        )
        self.assertEqual(output.read_bytes(), original)

    def test_failed_atomic_publish_leaves_no_output(self) -> None:
        generated = generate_market_validation_plan_proposal(
            FakePlanProposalProvider(self.raw_proposal),
            QUESTION,
        )
        output = self.root / "failed.json"
        with mock.patch(
            "market_validator.plan_providers.service._publish_new_output",
            side_effect=OSError("synthetic"),
        ):
            with self.assertRaises(PlanProposalProviderError) as caught:
                persist_generated_plan_proposal(generated, output)
        self.assertEqual(
            caught.exception.failure.code,
            PlanProposalProviderErrorCode.PROPOSAL_OUTPUT_ERROR,
        )
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".plan-proposal-tmp-*")), [])


class PlanProposalProviderCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = RUNTIME_PARENT / self._testMethodName
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        self.question_path = self.root / "question.txt"
        self.question_path.write_text(QUESTION + "\n", encoding="utf-8")
        self.output_path = self.root / "proposal.json"
        self.raw_proposal = PROPOSAL_PATH.read_bytes()

    def tearDown(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        if RUNTIME_PARENT.exists() and not any(RUNTIME_PARENT.iterdir()):
            RUNTIME_PARENT.rmdir()

    @staticmethod
    def _environment_without_key() -> dict[str, str]:
        allowed_names = (
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "WINDIR",
        )
        environment = {
            name: os.environ[name]
            for name in allowed_names
            if name in os.environ
        }
        environment.update(
            {
                "DEEPSEEK_API_KEY": "",
                "FRED_API_KEY": "",
                "NO_PROXY": "*",
                "PYTHONUTF8": "1",
            }
        )
        source = str(ROOT / "src")
        environment["PYTHONPATH"] = source
        return environment

    def _subprocess(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "market_validator", *arguments],
            cwd=ROOT,
            env=self._environment_without_key(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )

    def _arguments(self) -> list[str]:
        return [
            "propose-plan",
            "--question-file",
            str(self.question_path),
            "--provider",
            "deepseek_api",
            "--model",
            MODEL,
            "--output",
            str(self.output_path),
            "--allow-network",
        ]

    def _direct_main_with_provider(
        self,
        provider: FakePlanProposalProvider,
        *,
        output: Path | None = None,
    ) -> tuple[int, str, str]:
        arguments = self._arguments()
        if output is not None:
            arguments[arguments.index("--output") + 1] = str(output)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                "market_validator.workflow_cli.create_plan_proposal_provider",
                return_value=provider,
            ) as factory,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = main(arguments)
        factory.assert_called_once_with(
            "deepseek_api",
            MODEL,
            allow_network=True,
        )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_existing_exit_codes_zero_through_seventeen_are_unchanged(self) -> None:
        expected = {
            "SUCCESS": 0,
            "CLI_INPUT_ERROR": 2,
            "INVALID_PLAN": 3,
            "WORKFLOW_PATH_ERROR": 4,
            "SOURCE_IDENTITY_MISMATCH": 5,
            "ANALYSIS_CONTRACT_MISMATCH": 6,
            "ANALYSIS_FAILED": 7,
            "ARTIFACT_CONFLICT": 8,
            "ARTIFACT_VERIFICATION_FAILED": 9,
            "INTERNAL_ERROR": 10,
            "INVALID_PROPOSAL": 11,
            "INVALID_CONFIRMATION": 12,
            "CONFIRMATION_MISMATCH": 13,
            "PROPOSAL_NOT_CONFIRMABLE": 14,
            "PROPOSAL_DATA_CONTRACT_MISMATCH": 15,
            "PLAN_OUTPUT_CONFLICT": 16,
            "PLAN_OUTPUT_ERROR": 17,
        }
        for name, value in expected.items():
            self.assertEqual(int(getattr(WorkflowCliExitCode, name)), value)

    def test_cli_requires_explicit_network_permission_before_provider_creation(self) -> None:
        arguments = self._arguments()
        arguments.remove("--allow-network")
        completed = self._subprocess(*arguments)
        self.assertEqual(completed.returncode, WorkflowCliExitCode.CLI_INPUT_ERROR)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(json.loads(completed.stderr)["error"]["code"], "cli_input_error")
        self.assertFalse(self.output_path.exists())

    def test_cli_missing_provider_configuration_does_not_attempt_network(self) -> None:
        completed = self._subprocess(*self._arguments())
        self.assertEqual(
            completed.returncode,
            WorkflowCliExitCode.PROVIDER_CONFIGURATION_MISSING,
        )
        self.assertEqual(completed.stdout, "")
        self.assertEqual(
            json.loads(completed.stderr)["error"]["code"],
            "provider_configuration_missing",
        )
        self.assertFalse(self.output_path.exists())
        self.assertNotIn("Traceback", completed.stderr)

    def test_cli_fake_success_writes_only_canonical_proposal(self) -> None:
        provider = FakePlanProposalProvider(self.raw_proposal)
        exit_code, stdout, stderr = self._direct_main_with_provider(provider)
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.output_path.read_bytes(), serialize_market_validation_plan_proposal(
            generate_market_validation_plan_proposal(
                FakePlanProposalProvider(self.raw_proposal), QUESTION
            ).proposal
        ))
        self.assertEqual(set(path.name for path in self.root.iterdir()), {"question.txt", "proposal.json"})
        self.assertNotIn("confirmed", payload["data"])
        self.assertNotIn("workflow_plan", payload["data"])
        self.assertNotIn("artifact_id", payload["data"])

    def test_cli_provider_and_parser_failures_have_stable_exit_codes(self) -> None:
        cases = (
            (
                FakePlanProposalProvider(
                    self.raw_proposal,
                    error=_provider_error(
                        PlanProposalProviderErrorCode.PROVIDER_REQUEST_FAILED
                    ),
                ),
                19,
                "provider_request_failed",
            ),
            (
                FakePlanProposalProvider(
                    self.raw_proposal,
                    error=_provider_error(
                        PlanProposalProviderErrorCode.PROVIDER_TIMEOUT
                    ),
                ),
                20,
                "provider_timeout",
            ),
            (
                FakePlanProposalProvider(
                    self.raw_proposal,
                    error=_provider_error(
                        PlanProposalProviderErrorCode.PROVIDER_REFUSED
                    ),
                ),
                21,
                "provider_refused",
            ),
            (
                FakePlanProposalProvider(b"```json\n{}\n```"),
                22,
                "provider_invalid_proposal",
            ),
        )
        for index, (provider, expected_exit, expected_code) in enumerate(cases):
            output = self.root / f"failure-{index}.json"
            with self.subTest(code=expected_code):
                exit_code, stdout, stderr = self._direct_main_with_provider(
                    provider,
                    output=output,
                )
                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["error"]["code"], expected_code)
                self.assertNotIn("Traceback", stderr)
                self.assertFalse(output.exists())

    def test_cli_output_is_idempotent_and_conflict_protected(self) -> None:
        provider = FakePlanProposalProvider(self.raw_proposal)
        first_code, _, first_error = self._direct_main_with_provider(provider)
        self.assertEqual(first_code, 0, first_error)
        original = self.output_path.read_bytes()
        mtime = self.output_path.stat().st_mtime_ns
        second_code, _, second_error = self._direct_main_with_provider(
            FakePlanProposalProvider(self.raw_proposal)
        )
        self.assertEqual(second_code, 0, second_error)
        self.assertEqual(self.output_path.read_bytes(), original)
        self.assertEqual(self.output_path.stat().st_mtime_ns, mtime)

        changed = json.loads(self.raw_proposal)
        changed["assumptions"] = [*changed["assumptions"], "Different output."]
        changed_provider = FakePlanProposalProvider(
            json.dumps(changed, ensure_ascii=False).encode("utf-8")
        )
        conflict_code, conflict_stdout, conflict_stderr = (
            self._direct_main_with_provider(changed_provider)
        )
        self.assertEqual(conflict_code, 23)
        self.assertEqual(conflict_stdout, "")
        self.assertEqual(
            json.loads(conflict_stderr)["error"]["code"],
            "proposal_output_conflict",
        )
        self.assertEqual(self.output_path.read_bytes(), original)

    def test_cli_invalid_output_is_rejected_before_provider_request(self) -> None:
        provider = FakePlanProposalProvider(self.raw_proposal)
        arguments = self._arguments()
        arguments[arguments.index("--output") + 1] = str(
            self.root / "missing-parent" / "proposal.json"
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch(
                "market_validator.workflow_cli.create_plan_proposal_provider",
                return_value=provider,
            ) as factory,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = main(arguments)
        self.assertEqual(exit_code, 24)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            json.loads(stderr.getvalue())["error"]["code"],
            "proposal_output_error",
        )
        factory.assert_not_called()
        self.assertEqual(provider.requests, [])

    def test_credential_sentinel_never_reaches_cli_or_proposal_output(self) -> None:
        transport = FakeDeepSeekTransport(
            PlanProposalProviderTest._deepseek_response(self.raw_proposal)
        )
        real_adapter = DeepSeekApiPlanProposalProvider(
            model=MODEL,
            environment={"DEEPSEEK_API_KEY": SENTINEL_KEY},
            transport=transport,
            allow_network=True,
        )
        exit_code, stdout, stderr = self._direct_main_with_provider(real_adapter)
        self.assertEqual(exit_code, 0, stderr)
        combined = stdout + stderr + self.output_path.read_text(encoding="utf-8")
        self.assertNotIn(SENTINEL_KEY, combined)
        self.assertNotIn(SENTINEL_KEY, str(self.output_path))

    @unittest.skipUnless(
        (
            ROOT
            / ".market_validator"
            / "data"
            / "bundles"
            / "fred"
            / "fred-dcoilwtico-20260804T085757136534Z-58aa38ed3b12.json"
        ).is_file(),
        "verified local WTI snapshot is not present",
    )
    def test_fixed_fake_proposal_still_reproduces_wti_golden_workflow(self) -> None:
        request_id = "fred-dcoilwtico-20260804T085757136534Z-58aa38ed3b12"
        data_root = ROOT / ".market_validator" / "data"
        bundle = data_root / "bundles" / "fred" / f"{request_id}.json"
        source_files = (
            bundle,
            data_root / "manifests" / "fred" / f"{request_id}.json",
            data_root / "raw" / "fred" / f"{request_id}-series.json",
            data_root / "raw" / "fred" / f"{request_id}-observations-0000.json",
        )
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
        generated = generate_market_validation_plan_proposal(
            FakePlanProposalProvider(self.raw_proposal),
            QUESTION,
        )
        confirmation = parse_plan_proposal_confirmation(CONFIRMATION_PATH.read_bytes())
        plan = compile_confirmed_workflow_plan(
            generated.proposal,
            confirmation,
            bundle,
            expected_artifact_manifest_sha256=(
                "5be9387e8425838931618c585805a19e046a4b4ba9da5950373d83385e960a2e"
            ),
        )
        completed = run_market_validation_workflow(
            plan,
            bundle,
            self.root / "artifacts",
        )
        self.assertEqual(
            completed.artifact_id,
            "price-change-volatility-0796a66788e5cfd71dff6a3222dd5cab",
        )
        self.assertEqual(
            completed.manifest_sha256,
            "5be9387e8425838931618c585805a19e046a4b4ba9da5950373d83385e960a2e",
        )
        self.assertEqual(completed.final_conclusion.value, "supported")
        self.assertEqual(completed.result.errors, [])
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
