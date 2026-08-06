#!/usr/bin/env python3
"""TEST-ONLY synthetic offline example: DataPlan Confirmed -> Data Ready.

Runs the formal v0.3 lifecycle over fully synthetic fixtures with an explicit
local CSV provider and an explicit session schedule snapshot. Writes nothing
into tracked directories; uses the caller-provided or TEMP output directory.

NOT MARKET DATA. NOT ANALYSIS. NOT INVESTMENT ADVICE.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

CONFIRMED_AT = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)


def _load_strict_json(path: Path):
    from market_validator.research.models import StrictResearchModel

    return json.loads(path.read_text(encoding="utf-8"))


def _registry_payload(fixture_name: str, substitutions: dict[str, str]):
    payload = _load_strict_json(FIXTURES / fixture_name)
    text = json.dumps(payload, ensure_ascii=False)
    for key, value in substitutions.items():
        text = text.replace("{" + key + "}", value)
    return json.loads(text)


def _write_canonical(path: Path, payload) -> None:
    path.write_bytes(
        (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    )


def run_example(output_dir: Path | None = None) -> dict[str, object]:
    from market_validator.data.access_authorization import (
        create_data_access_authorization,
    )
    from market_validator.data.calendars import CalendarRegistry
    from market_validator.data.data_plan_review import (
        confirm_data_plan,
        generate_data_plan,
    )
    from market_validator.data.execution import (
        LocalFileExecutionAdapter,
        execute_authorized_acquisition,
    )
    from market_validator.data.providers.csv_provider import CSVProvider
    from market_validator.data.readiness import (
        assess_data_readiness,
        create_data_ready_manifest,
        persist_data_readiness_assessment,
        persist_data_ready_manifest,
        validate_data_ready_manifest_matches,
        verify_persisted_data_ready_manifest,
    )
    from market_validator.data.registry import InstrumentRegistry
    from market_validator.data.session_schedule import (
        ExplicitSessionScheduleAdapter,
        parse_explicit_session_schedule_snapshot,
        serialize_explicit_session_schedule_snapshot,
    )
    from market_validator.data.source_selection import (
        SourceSelectionDecision,
        confirm_source_selection,
        generate_source_selection,
    )
    from market_validator.research.serialization import parse_research_spec

    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="v0.3-example-"))
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit("output directory must be empty or new")
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = output_dir / "artifacts"
    artifacts.mkdir()
    snapshots = output_dir / "snapshots"
    snapshots.mkdir()

    outcome_csv = output_dir / "outcome.csv"
    predictor_csv = output_dir / "predictor.csv"
    shutil.copy(FIXTURES / "local-data.csv", outcome_csv)
    predictor_csv.write_text(
        (FIXTURES / "local-data.csv")
        .read_text(encoding="utf-8")
        .replace(
            "test.only.outcome.series",
            "test.only.predictor.series",
        ),
        encoding="utf-8",
    )

    substitutions = {
        "LOCAL_CSV_OUTCOME_PATH": str(outcome_csv),
        "LOCAL_CSV_PREDICTOR_PATH": str(predictor_csv),
    }
    spec = parse_research_spec(
        (FIXTURES / "research-spec.json").read_bytes()
    )
    calendars = CalendarRegistry.from_json_file(
        FIXTURES / "calendar-registry.json"
    )
    instruments = InstrumentRegistry.from_json_file(
        FIXTURES / "instrument-registry.json", calendars
    )
    instruments = InstrumentRegistry(
        [
            entry.model_copy(
                update={
                    "provider_mappings": [
                        mapping.model_copy(
                            update={
                                "provider_symbol": str(
                                    outcome_csv
                                    if "outcome" in entry.instrument_id
                                    else predictor_csv
                                ),
                                "dataset_or_endpoint": str(
                                    outcome_csv
                                    if "outcome" in entry.instrument_id
                                    else predictor_csv
                                ),
                            }
                        )
                        for mapping in entry.provider_mappings
                    ]
                }
            )
            for entry in instruments.entries
        ],
        calendars,
    )
    _write_canonical(
        artifacts / "instrument-registry.json",
        {
            "schema_version": "1.0",
            "instruments": [
                entry.model_dump(mode="json")
                for entry in instruments.entries
            ],
        },
    )
    _write_canonical(
        artifacts / "calendar-registry.json",
        {
            "schema_version": "1.0",
            "calendars": [
                entry.model_dump(mode="json")
                for entry in calendars.definitions
            ],
        },
    )
    schedule = parse_explicit_session_schedule_snapshot(
        (FIXTURES / "session-schedule.json").read_bytes()
    )
    schedule_adapter = ExplicitSessionScheduleAdapter(schedule)

    generated = generate_data_plan(spec, instruments, calendars)
    confirmation = confirm_data_plan(
        generated, instruments, confirmed_at=CONFIRMED_AT
    )
    _write_canonical(
        artifacts / "data-plan.json", generated.data_plan.model_dump(mode="json")
    )
    _write_canonical(
        artifacts / "data-plan-confirmation.json",
        confirmation.model_dump(mode="json"),
    )

    # TEST-ONLY fixture behavior: decisions are derived from the single
    # verified mapping of each requirement. This is NOT a real user choice.
    decisions = []
    for requirement in generated.data_plan.requirements:
        mapping = instruments.get(
            requirement.instrument_id
        ).provider_mappings[0]
        decisions.append(
            SourceSelectionDecision(
                requirement_id=requirement.requirement_id,
                provider_id=mapping.provider_id,
                provider_symbol=mapping.provider_symbol,
                dataset_or_endpoint=mapping.dataset_or_endpoint,
            )
        )
    selection = generate_source_selection(
        generated, confirmation, instruments, calendars, decisions
    )
    selection_confirmation = confirm_source_selection(
        selection, instruments, calendars, confirmed_at=CONFIRMED_AT
    )
    _write_canonical(
        artifacts / "source-selection.json",
        selection.source_selection.model_dump(mode="json"),
    )
    _write_canonical(
        artifacts / "source-selection-confirmation.json",
        selection_confirmation.model_dump(mode="json"),
    )

    from market_validator.data.acquisition_request import (
        AccessMode,
        ProviderCapabilitySnapshot,
        generate_acquisition_request_plan,
    )
    from market_validator.research.enums import Frequency, Transformation

    csv_capability = ProviderCapabilitySnapshot(
        provider_id="local_csv",
        supported_access_modes=[AccessMode.LOCAL_FILE],
        supported_frequencies=[Frequency.ONE_DAY],
        supported_transforms=list(Transformation),
        supports_date_range=True,
        supports_revision_policy=False,
        supports_dry_run=False,
        supports_previous_observations=False,
        requires_credential=False,
        paid_access_possible=False,
    )
    capabilities = {"local_csv": csv_capability}
    plan = generate_acquisition_request_plan(
        selection,
        selection_confirmation,
        generated.data_plan,
        instruments,
        calendars,
        capabilities,
        session_adapters={
            schedule.schedule_adapter_id: (
                lambda start, periods: (
                    schedule_adapter.previous_sessions(start, periods)[0]
                    if periods
                    else None
                )
            )
        },
    )
    _write_canonical(
        artifacts / "acquisition-request-plan.json",
        plan.acquisition_request_plan.model_dump(mode="json"),
    )
    request_ids = [
        request.requirement_id
        for request in plan.acquisition_request_plan.requests
    ]
    authorization = create_data_access_authorization(
        plan,
        instruments,
        calendars,
        capabilities,
        request_ids,
        authorized_at=CONFIRMED_AT,
    )
    _write_canonical(
        artifacts / "data-access-authorization.json",
        authorization.model_dump(mode="json"),
    )
    _write_canonical(
        artifacts / "provider-capability.json",
        csv_capability.model_dump(mode="json"),
    )

    provider = CSVProvider(outcome_csv)
    verified = execute_authorized_acquisition(
        generated_plan=plan,
        authorization=authorization,
        data_plan=generated.data_plan,
        instrument_registry=instruments,
        calendar_registry=calendars,
        capability_snapshots=capabilities,
        attempt_id="v0.3-example-attempt-1",
        receipt_path=artifacts / "authorization-receipt.json",
        snapshot_root=snapshots,
        adapters={
            "local_csv": LocalFileExecutionAdapter(provider),
        },
    )
    _write_canonical(
        artifacts / "snapshot-manifest.json",
        verified.manifest.model_dump(mode="json"),
    )

    assessment = assess_data_readiness(
        generated_plan=plan,
        data_plan=generated.data_plan,
        instrument_registry=instruments,
        calendar_registry=calendars,
        snapshot_path=verified.snapshot_path,
        session_schedules={
            schedule.schedule_adapter_id: schedule,
        },
    )
    persist_data_readiness_assessment(
        assessment, artifacts / "readiness-assessment.json"
    )
    _write_canonical(
        artifacts / "session-schedule.json",
        json.loads(
            serialize_explicit_session_schedule_snapshot(schedule)
            .decode("utf-8")
        ),
    )
    if assessment.status.value != "ready":
        raise SystemExit(
            "example failed to reach Data Ready: "
            + json.dumps([b.code for b in assessment.blockers])
        )
    manifest = create_data_ready_manifest(
        assessment=assessment,
        generated_plan=plan,
    )
    persist_data_ready_manifest(
        manifest,
        artifacts / "data-ready-manifest.json",
        snapshot_path=verified.snapshot_path,
    )
    persisted = verify_persisted_data_ready_manifest(
        artifacts / "data-ready-manifest.json", manifest
    )
    validate_data_ready_manifest_matches(
        manifest,
        assessment,
        plan,
        generated.data_plan,
        instruments,
        calendars,
    )

    return {
        "TEST_ONLY_SYNTHETIC_EXAMPLE": True,
        "NOT_MARKET_DATA": True,
        "NOT_ANALYSIS": True,
        "NOT_INVESTMENT_ADVICE": True,
        "status": "data_ready",
        "output_dir": str(output_dir),
        "data_ready_id": manifest.data_ready_manifest.data_ready_id,
        "data_ready_manifest_sha256": manifest.data_ready_manifest_sha256,
        "readiness_assessment_sha256": (
            manifest.readiness_assessment_sha256
        ),
        "snapshot_id": verified.snapshot_id,
        "snapshot_manifest_sha256": verified.manifest_sha256,
        "requirement_count": len(
            manifest.data_ready_manifest.bundles
        ),
        "analysis_artifact_generated": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "TEST-ONLY synthetic offline v0.3 data-ready example. "
            "NOT MARKET DATA. NOT ANALYSIS. NOT INVESTMENT ADVICE."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="new empty output directory (default: TEMP)",
    )
    args = parser.parse_args(argv)
    try:
        summary = run_example(args.output_dir)
    except SystemExit as error:
        print(json.dumps({"status": "failed"}, sort_keys=True))
        return int(error.code or 1)
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
