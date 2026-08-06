"""Unified JSON-only data-lifecycle CLI (v0.3.0 Phase 5).

Thin adapter over the existing domain APIs. Never auto-confirms,
auto-authorizes, auto-selects providers, or auto-executes anything.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
from pathlib import Path


def _json_out(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _read_strict_json(path: str) -> dict[str, object]:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        _fail(2, "input_error", "input file could not be read")
    try:
        json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        _fail(2, "invalid_json", "input file failed strict JSON validation")
    return json.loads(raw.decode("utf-8", errors="strict"))


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(key)
        result[key] = value
    return result


def _reject_nonstandard(value: str):
    raise ValueError(value)


def _fail(code: int, status: str, message: str) -> None:
    print(_json_out({"valid": False, "status": status, "code": message}))
    sys.exit(code)


def _sha256_hex(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


# --------------------------------------------------------------------------
# artifact type registry: parser + serializer + schema version + json schema
# --------------------------------------------------------------------------

def _artifact_handlers():
    from market_validator.data import source_selection
    from market_validator.data.access_authorization import (
        parse_data_access_authorization,
        parse_data_access_authorization_receipt,
        serialize_data_access_authorization,
        serialize_data_access_authorization_receipt,
    )
    from market_validator.data.acquisition_request import (
        parse_acquisition_request_plan,
        parse_provider_capability_snapshot,
        serialize_acquisition_request_plan,
        serialize_provider_capability_snapshot,
    )
    from market_validator.data.readiness import (
        parse_data_ready_manifest,
        parse_data_readiness_assessment,
        serialize_data_ready_manifest,
        serialize_data_readiness_assessment,
    )
    from market_validator.data.session_schedule import (
        parse_explicit_session_schedule_snapshot,
        serialize_explicit_session_schedule_snapshot,
    )
    from market_validator.data.snapshot import (
        parse_snapshot_manifest,
        serialize_snapshot_manifest,
    )
    from market_validator.data.source_selection import (
        parse_source_selection_confirmation,
        serialize_source_selection_confirmation,
    )

    return {
        "source-selection": (
            source_selection.parse_source_selection,
            source_selection.serialize_source_selection,
            "1.0",
        ),
        "source-selection-confirmation": (
            parse_source_selection_confirmation,
            serialize_source_selection_confirmation,
            "1.0",
        ),
        "provider-capability": (
            parse_provider_capability_snapshot,
            serialize_provider_capability_snapshot,
            "1.0",
        ),
        "acquisition-request": (
            parse_acquisition_request_plan,
            serialize_acquisition_request_plan,
            "1.2",
        ),
        "data-access-authorization": (
            parse_data_access_authorization,
            serialize_data_access_authorization,
            "1.0",
        ),
        "authorization-receipt": (
            parse_data_access_authorization_receipt,
            serialize_data_access_authorization_receipt,
            "1.0",
        ),
        "session-schedule": (
            parse_explicit_session_schedule_snapshot,
            serialize_explicit_session_schedule_snapshot,
            "1.0",
        ),
        "snapshot-manifest": (
            parse_snapshot_manifest,
            serialize_snapshot_manifest,
            "1.0",
        ),
        "readiness-assessment": (
            parse_data_readiness_assessment,
            serialize_data_readiness_assessment,
            "1.1",
        ),
        "data-ready-manifest": (
            parse_data_ready_manifest,
            serialize_data_ready_manifest,
            "1.1",
        ),
    }


def cmd_schema(args: argparse.Namespace) -> None:
    handlers = _artifact_handlers()
    if args.artifact_type not in handlers:
        _fail(2, "unknown_artifact_type", "unknown artifact type")
    _, _, version = handlers[args.artifact_type]
    print(
        _json_out(
            {
                "artifact_type": args.artifact_type,
                "schema_version": version,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                },
            }
        )
    )


def cmd_validate(args: argparse.Namespace) -> None:
    handlers = _artifact_handlers()
    if args.artifact_type not in handlers:
        _fail(2, "unknown_artifact_type", "unknown artifact type")
    parser, serializer, version = handlers[args.artifact_type]
    raw = Path(args.path).read_bytes()
    try:
        model = parser(raw)
    except Exception:
        _fail(2, "invalid_artifact", "artifact failed strict validation")
    canonical = serializer(model)
    print(
        _json_out(
            {
                "valid": True,
                "artifact_type": args.artifact_type,
                "schema_version": version,
                "sha256": _sha256_hex(canonical),
            }
        )
    )


def _load(path: str, parser):
    try:
        return parser(Path(path).read_bytes())
    except Exception:
        _fail(2, "invalid_input", "input artifact failed strict validation")


def _load_registries(instruments_path: str, calendars_path: str):
    from market_validator.data.calendars import CalendarRegistry
    from market_validator.data.registry import InstrumentRegistry

    calendars = CalendarRegistry.from_json_file(Path(calendars_path))
    instruments = InstrumentRegistry.from_json_file(
        Path(instruments_path), calendars
    )
    return instruments, calendars


def cmd_source_selection(args: argparse.Namespace) -> None:
    from market_validator.data.data_plan_review import (
        confirm_data_plan,
        generate_data_plan,
        parse_data_plan,
        parse_data_plan_confirmation,
        serialize_data_plan,
        serialize_data_plan_confirmation,
    )
    from market_validator.data.source_selection import (
        confirm_source_selection,
        generate_source_selection,
        parse_source_selection_confirmation,
        serialize_source_selection,
        serialize_source_selection_confirmation,
        validate_source_selection_confirmation_matches,
    )
    from market_validator.research.serialization import (
        parse_research_spec,
        serialize_research_spec,
    )

    if args.action == "generate":
        instruments, calendars = _load_registries(
            args.instrument_registry, args.calendar_registry
        )
        spec = _load(args.research_spec, parse_research_spec)
        data_plan = _load(args.data_plan, parse_data_plan)
        confirmation = _load(
            args.data_plan_confirmation, parse_data_plan_confirmation
        )
        decisions = _load_decisions(args.decisions)
        generated = generate_source_selection(
            data_plan,
            confirmation,
            instruments,
            calendars,
            decisions,
        )
        from market_validator.hypothesis.lifecycle import (
            persist_immutable_bytes,
        )

        try:
            output = persist_immutable_bytes(
                serialize_source_selection(generated.source_selection),
                Path(args.output),
            )
        except Exception as error:
            if "conflict" in str(error):
                _fail(4, "output_conflict", "output path already exists")
            _fail(2, "output_error", "output could not be written")
        print(
            _json_out(
                {
                    "valid": True,
                    "status": "generated",
                    "artifact_type": "source-selection",
                    "sha256": generated.source_selection_sha256,
                    "output_path": str(output),
                    "blockers": [],
                }
            )
        )
        return
    if args.action == "confirm":
        instruments, calendars = _load_registries(
            args.instrument_registry, args.calendar_registry
        )
        selection = _load(args.source_selection, parse_source_selection)
        confirmation = confirm_source_selection(
            selection,
            instruments,
            calendars,
            confirmed_at=datetime.now(timezone.utc),
        )
        from market_validator.hypothesis.lifecycle import (
            persist_immutable_bytes,
        )

        try:
            output = persist_immutable_bytes(
                serialize_source_selection_confirmation(confirmation),
                Path(args.output),
            )
        except Exception as error:
            if "conflict" in str(error):
                _fail(4, "output_conflict", "output path already exists")
            _fail(2, "output_error", "output could not be written")
        print(
            _json_out(
                {
                    "valid": True,
                    "status": "confirmed",
                    "artifact_type": "source-selection-confirmation",
                    "sha256": confirmation.source_selection_confirmation_sha256,
                    "output_path": str(output),
                }
            )
        )
        return
    if args.action == "validate-confirmation":
        instruments, calendars = _load_registries(
            args.instrument_registry, args.calendar_registry
        )
        selection = _load(args.source_selection, parse_source_selection)
        confirmation = _load(
            args.source_selection_confirmation,
            parse_source_selection_confirmation,
        )
        try:
            validate_source_selection_confirmation_matches(
                selection, instruments, calendars, confirmation
            )
        except Exception:
            _fail(2, "confirmation_mismatch", "confirmation does not match")
        print(
            _json_out(
                {
                    "valid": True,
                    "status": "verified",
                    "artifact_type": "source-selection-confirmation",
                    "sha256": confirmation.source_selection_confirmation_sha256,
                }
            )
        )
        return


def _load_decisions(path: str):
    from market_validator.data.source_selection import (
        parse_source_selection_decisions,
    )

    return _load(path, parse_source_selection_decisions)


def cmd_acquisition(args: argparse.Namespace) -> None:
    from market_validator.data.acquisition_request import (
        generate_acquisition_request_plan,
        parse_acquisition_request_plan,
        serialize_acquisition_request_plan,
        validate_acquisition_request_plan,
    )
    from market_validator.data.source_selection import (
        parse_source_selection,
        parse_source_selection_confirmation,
        serialize_source_selection_confirmation,
    )
    from market_validator.data.data_plan_review import parse_data_plan
    from market_validator.data.session_schedule import (
        ExplicitSessionScheduleAdapter,
        parse_explicit_session_schedule_snapshot,
    )

    if args.action == "generate":
        instruments, calendars = _load_registries(
            args.instrument_registry, args.calendar_registry
        )
        selection = _load(args.source_selection, parse_source_selection)
        confirmation = _load(
            args.source_selection_confirmation,
            parse_source_selection_confirmation,
        )
        data_plan = _load(args.data_plan, parse_data_plan)
        capabilities = {}
        for path in args.capability:
            from market_validator.data.acquisition_request import (
                parse_provider_capability_snapshot,
            )

            snapshot = _load(path, parse_provider_capability_snapshot)
            capabilities[snapshot.provider_id] = snapshot
        session_adapters = {}
        for path in args.session_schedule:
            snapshot = _load(
                path, parse_explicit_session_schedule_snapshot
            )
            adapter = ExplicitSessionScheduleAdapter(snapshot)
            session_adapters[snapshot.schedule_adapter_id] = (
                lambda start, periods, adapter=adapter: (
                    adapter.previous_sessions(start, periods)[0]
                    if periods
                    else None
                )
            )
        plan = generate_acquisition_request_plan(
            selection,
            confirmation,
            data_plan,
            instruments,
            calendars,
            capabilities,
            session_adapters=session_adapters,
        )
        from market_validator.hypothesis.lifecycle import (
            persist_immutable_bytes,
        )

        try:
            output = persist_immutable_bytes(
                serialize_acquisition_request_plan(
                    plan.acquisition_request_plan
                ),
                Path(args.output),
            )
        except Exception as error:
            if "conflict" in str(error):
                _fail(4, "output_conflict", "output path already exists")
            _fail(2, "output_error", "output could not be written")
        print(
            _json_out(
                {
                    "valid": True,
                    "status": "generated",
                    "artifact_type": "acquisition-request",
                    "schema_version": "1.2",
                    "sha256": plan.acquisition_request_plan_sha256,
                    "output_path": str(output),
                    "blockers": [
                        item.code.value
                        for item in plan.acquisition_request_plan
                        .unresolved_requirements
                    ],
                    "unresolved": (
                        len(
                            plan.acquisition_request_plan
                            .unresolved_requirements
                        )
                    ),
                }
            )
        )
        return
    # validate
    from market_validator.data.acquisition_request import (
        acquisition_request_readiness_blockers,
    )

    plan = _load(args.path, parse_acquisition_request_plan)
    if acquisition_request_readiness_blockers(plan):
        _fail(2, "invalid_plan", "acquisition request plan is not ready")
    print(
        _json_out(
            {
                "valid": True,
                "status": "valid",
                "artifact_type": "acquisition-request",
                "schema_version": plan.acquisition_request_schema_version,
                "sha256": _sha256_hex(
                    serialize_acquisition_request_plan(plan)
                ),
            }
        )
    )


def cmd_authorization(args: argparse.Namespace) -> None:
    from market_validator.data.acquisition_request import (
        acquisition_request_readiness_blockers,
        parse_acquisition_request_plan,
        serialize_acquisition_request_plan,
    )
    from market_validator.data.access_authorization import (
        create_data_access_authorization,
        parse_data_access_authorization,
        serialize_data_access_authorization,
    )
    from market_validator.data.calendars import CalendarRegistry
    from market_validator.data.registry import InstrumentRegistry

    if args.action == "create":
        plan_model = _load(
            args.acquisition_plan, parse_acquisition_request_plan
        )
        if plan_model.unresolved_requirements:
            _fail(2, "plan_unresolved", "plan has unresolved requirements")
        has_network = any(
            request.access_mode.value == "network"
            for request in plan_model.requests
        )
        has_local = any(
            request.access_mode.value == "local_file"
            for request in plan_model.requests
        )
        if args.authorize_network and not has_network:
            _fail(2, "invalid_authorization", "no network request in plan")
        if args.authorize_local_file_read and not has_local:
            _fail(
                2,
                "invalid_authorization",
                "no local file request in plan",
            )
        if has_network and not args.authorize_network:
            _fail(
                2,
                "network_not_authorized",
                "network access was not explicitly authorized",
            )
        if has_local and not args.authorize_local_file_read:
            _fail(
                2,
                "local_not_authorized",
                "local file read was not explicitly authorized",
            )
        instruments, calendars = _load_registries(
            args.instrument_registry, args.calendar_registry
        )
        capabilities = {}
        for path in args.capability:
            from market_validator.data.acquisition_request import (
                parse_provider_capability_snapshot,
            )

            snapshot = _load(path, parse_provider_capability_snapshot)
            capabilities[snapshot.provider_id] = snapshot
        if acquisition_request_readiness_blockers(
            plan_artifact_or_model(plan_model, capabilities),
            instruments,
            calendars,
            capabilities,
        ):
            _fail(2, "plan_not_ready", "plan is not ready for authorization")
        request_ids = [request.requirement_id for request in plan_model.requests]
        from market_validator.data.acquisition_request import (
            calculate_provider_capability_snapshot_sha256,
        )

        authorization = create_data_access_authorization(
            plan_artifact_or_model(plan_model, capabilities),
            instruments,
            calendars,
            capabilities,
            request_ids,
            authorized_at=datetime.now(timezone.utc),
        )
        from market_validator.hypothesis.lifecycle import (
            persist_immutable_bytes,
        )

        try:
            output = persist_immutable_bytes(
                serialize_data_access_authorization(authorization),
                Path(args.output),
            )
        except Exception as error:
            if "conflict" in str(error):
                _fail(4, "output_conflict", "output path already exists")
            _fail(2, "output_error", "output could not be written")
        from market_validator.data.access_authorization import (
            calculate_data_access_authorization_sha256,
        )

        print(
            _json_out(
                {
                    "valid": True,
                    "status": "authorized",
                    "artifact_type": "data-access-authorization",
                    "sha256": calculate_data_access_authorization_sha256(
                        authorization
                    ),
                    "output_path": str(output),
                }
            )
        )
        return
    authorization = _load(
        args.path, parse_data_access_authorization
    )
    from market_validator.data.access_authorization import (
        calculate_data_access_authorization_sha256,
    )

    print(
        _json_out(
            {
                "valid": True,
                "status": "valid",
                "artifact_type": "data-access-authorization",
                "sha256": calculate_data_access_authorization_sha256(
                    authorization
                ),
            }
        )
    )


def plan_artifact_or_model(plan_model, capabilities=None):
    """Wrap a parsed plan into the generated-artifact shape the domain API needs."""
    from market_validator.data.acquisition_request import (
        GeneratedAcquisitionRequestPlan,
        calculate_acquisition_request_plan_sha256,
        calculate_provider_capability_snapshot_sha256,
    )

    return GeneratedAcquisitionRequestPlan(
        acquisition_request_plan=plan_model,
        acquisition_request_plan_sha256=calculate_acquisition_request_plan_sha256(
            plan_model
        ),
        research_spec_sha256=plan_model.research_spec_sha256,
        data_plan_sha256=plan_model.data_plan_sha256,
        data_plan_confirmation_sha256=(
            plan_model.data_plan_confirmation_sha256
        ),
        source_selection_sha256=plan_model.source_selection_sha256,
        source_selection_confirmation_sha256=(
            plan_model.source_selection_confirmation_sha256
        ),
        instrument_registry_sha256=(
            plan_model.instrument_registry_sha256
        ),
        calendar_registry_sha256=plan_model.calendar_registry_sha256,
        capability_snapshot_sha256s=dict(
            plan_model.capability_snapshot_sha256s
        ),
    )


def cmd_execution(args: argparse.Namespace) -> None:
    from market_validator.data.acquisition_request import (
        GeneratedAcquisitionRequestPlan,
        calculate_acquisition_request_plan_sha256,
        parse_acquisition_request_plan,
        serialize_acquisition_request_plan,
    )
    from market_validator.data.access_authorization import (
        calculate_data_access_authorization_sha256,
        parse_data_access_authorization,
        serialize_data_access_authorization,
    )
    from market_validator.data.execution import (
        ExecutionError,
        FredExecutionAdapter,
        LocalFileExecutionAdapter,
        execute_authorized_acquisition,
    )
    from market_validator.data.data_plan_review import (
        calculate_data_plan_sha256,
        parse_data_plan,
    )
    from market_validator.data.providers.fred_provider import FredProvider
    from market_validator.data.providers.csv_provider import CSVProvider

    instruments, calendars = _load_registries(
        args.instrument_registry, args.calendar_registry
    )
    plan_model = _load(args.acquisition_plan, parse_acquisition_request_plan)
    authorization = _load(
        args.authorization, parse_data_access_authorization
    )
    data_plan = _load(args.data_plan, parse_data_plan)
    capabilities = {}
    for path in args.capability:
        from market_validator.data.acquisition_request import (
            parse_provider_capability_snapshot,
        )

        snapshot = _load(path, parse_provider_capability_snapshot)
        capabilities[snapshot.provider_id] = snapshot
    if Path(args.receipt_output).exists():
        _fail(4, "authorization_consumed", "authorization was already consumed")
    generated_plan = plan_artifact_or_model(plan_model, capabilities)
    adapters = {}
    for request in plan_model.requests:
        provider_id = request.provider_id
        if provider_id == "fred":
            adapters[provider_id] = FredExecutionAdapter(
                FredProvider(instrument_registry=instruments)
            )
        elif provider_id == "local_csv":
            adapters[provider_id] = LocalFileExecutionAdapter(
                CSVProvider(Path("unused.csv"))
            )
    try:
        verified = execute_authorized_acquisition(
            generated_plan=generated_plan,
            authorization=authorization,
            data_plan=data_plan,
            instrument_registry=instruments,
            calendar_registry=calendars,
            capability_snapshots=capabilities,
            attempt_id=args.attempt_id,
            receipt_path=args.receipt_output,
            snapshot_root=args.snapshot_root,
            adapters=adapters,
        )
    except ExecutionError as error:
        if error.code.value == "authorization_already_consumed":
            _fail(4, "authorization_consumed", error.safe_message)
        _fail(3, "execution_failed", error.safe_message)
    print(
        _json_out(
            {
                "valid": True,
                "status": "snapshot_verified",
                "artifact_type": "snapshot",
                "snapshot_id": verified.snapshot_id,
                "snapshot_path": str(verified.snapshot_path),
                "manifest_sha256": verified.manifest_sha256,
                "blockers": [],
            }
        )
    )


def cmd_session_schedule(args: argparse.Namespace) -> None:
    from market_validator.data.session_schedule import (
        parse_explicit_session_schedule_snapshot,
        persist_explicit_session_schedule_snapshot,
        serialize_explicit_session_schedule_snapshot,
    )

    if args.action == "validate":
        snapshot = _load(args.path, parse_explicit_session_schedule_snapshot)
        print(
            _json_out(
                {
                    "valid": True,
                    "status": "valid",
                    "artifact_type": "session-schedule",
                    "schema_version": snapshot.schedule_schema_version,
                    "sha256": _sha256_hex(
                        serialize_explicit_session_schedule_snapshot(snapshot)
                    ),
                }
            )
        )
        return
    if args.action == "persist":
        snapshot = _load(args.path, parse_explicit_session_schedule_snapshot)
        output = Path(args.output)
        try:
            persisted = persist_explicit_session_schedule_snapshot(
                snapshot, output
            )
        except Exception as error:
            if "conflict" in str(error):
                _fail(4, "output_conflict", "output path already exists")
            _fail(2, "persist_failed", "session schedule could not be persisted")
        print(
            _json_out(
                {
                    "valid": True,
                    "status": "persisted",
                    "artifact_type": "session-schedule",
                    "sha256": _sha256_hex(persisted.read_bytes()),
                    "output_path": str(persisted),
                }
            )
        )
        return


def cmd_readiness(args: argparse.Namespace) -> None:
    from market_validator.data.acquisition_request import (
        GeneratedAcquisitionRequestPlan,
        calculate_acquisition_request_plan_sha256,
        parse_acquisition_request_plan,
    )
    from market_validator.data.data_plan_review import (
        calculate_data_plan_sha256,
        parse_data_plan,
    )
    from market_validator.data.readiness import (
        assess_data_readiness,
        create_data_ready_manifest,
        parse_data_ready_manifest,
        parse_data_readiness_assessment,
        persist_data_readiness_assessment,
        persist_data_ready_manifest,
        serialize_data_readiness_assessment,
        serialize_data_ready_manifest,
        validate_data_ready_manifest_matches,
        verify_persisted_data_ready_manifest,
    )
    from market_validator.data.session_schedule import (
        parse_explicit_session_schedule_snapshot,
    )
    from market_validator.data.source_selection import (
        parse_source_selection,
        parse_source_selection_confirmation,
        serialize_source_selection_confirmation,
    )

    instruments, calendars = _load_registries(
        args.instrument_registry, args.calendar_registry
    )
    plan_model = _load(args.acquisition_plan, parse_acquisition_request_plan)
    data_plan = _load(args.data_plan, parse_data_plan)
    session_schedules = {}
    for path in args.session_schedule:
        snapshot = _load(path, parse_explicit_session_schedule_snapshot)
        session_schedules[snapshot.schedule_adapter_id] = snapshot

    def _generated_plan():
        selection = _load(args.source_selection, parse_source_selection)
        confirmation = _load(
            args.source_selection_confirmation,
            parse_source_selection_confirmation,
        )
        return GeneratedAcquisitionRequestPlan(
            acquisition_request_plan=plan_model,
            acquisition_request_plan_sha256=(
                calculate_acquisition_request_plan_sha256(plan_model)
            ),
            research_spec_sha256=selection.research_spec_sha256,
            data_plan_sha256=calculate_data_plan_sha256(data_plan),
            data_plan_confirmation_sha256=(
                selection.data_plan_confirmation_sha256
            ),
            source_selection_sha256=confirmation.source_selection_sha256,
            source_selection_confirmation_sha256=_sha256_hex(
                serialize_source_selection_confirmation(confirmation)
            ),
            instrument_registry_sha256=(
                selection.instrument_registry_sha256
            ),
            calendar_registry_sha256=selection.calendar_registry_sha256,
            capability_snapshot_sha256s={},
        )

    if args.action == "assess":
        generated = _generated_plan()
        assessment = assess_data_readiness(
            generated_plan=generated,
            data_plan=data_plan,
            instrument_registry=instruments,
            calendar_registry=calendars,
            snapshot_path=args.snapshot,
            session_schedules=session_schedules,
        )
        output = Path(args.output)
        try:
            persist_data_readiness_assessment(assessment, output)
        except Exception as error:
            if "conflict" in str(error):
                _fail(4, "output_conflict", "output path already exists")
            _fail(2, "persist_failed", "assessment could not be persisted")
        blocked = assessment.status.value != "ready"
        print(
            _json_out(
                {
                    "valid": not blocked,
                    "status": assessment.status.value,
                    "artifact_type": "readiness-assessment",
                    "schema_version": assessment.readiness_schema_version,
                    "sha256": serialize_data_readiness_assessment(
                        assessment
                    ),
                    "output_path": str(output),
                    "blockers": [
                        blocker.code for blocker in assessment.blockers
                    ],
                    "warnings": assessment.warnings,
                }
            )
        )
        if blocked:
            sys.exit(2)
        return
    if args.action == "finalize":
        generated = _generated_plan()
        assessment = _load(args.assessment, parse_data_readiness_assessment)
        if assessment.status.value != "ready":
            _fail(2, "not_ready", "assessment is not ready")
        manifest = create_data_ready_manifest(
            assessment=assessment,
            generated_plan=generated,
        )
        output = Path(args.output)
        try:
            persist_data_ready_manifest(
                manifest, output, snapshot_path=args.snapshot
            )
        except Exception as error:
            if "conflict" in str(error):
                _fail(4, "output_conflict", "output path already exists")
            _fail(2, "persist_failed", "manifest could not be persisted")
        print(
            _json_out(
                {
                    "valid": True,
                    "status": "data_ready",
                    "artifact_type": "data-ready-manifest",
                    "schema_version": (
                        manifest.data_ready_manifest.data_ready_schema_version
                    ),
                    "sha256": manifest.data_ready_manifest_sha256,
                    "output_path": str(output),
                    "warnings": manifest.data_ready_manifest.warnings,
                }
            )
        )
        return
    # verify
    generated = _generated_plan()
    assessment = _load(args.assessment, parse_data_readiness_assessment)
    manifest = _load(args.manifest, parse_data_ready_manifest)
    if assessment.session_schedule_sha256s and not args.session_schedule:
        _fail(
            2,
            "session_schedule_missing",
            "the assessment binds session schedules; supply --session-schedule",
        )
    if len(args.session_schedule) != len(
        assessment.session_schedule_sha256s
    ):
        _fail(
            2,
            "session_schedule_incomplete",
            "every schedule bound by the assessment must be supplied",
        )
    for path in args.session_schedule:
        snapshot = _load(path, parse_explicit_session_schedule_snapshot)
        from market_validator.data.session_schedule import (
            calculate_explicit_session_schedule_snapshot_sha256,
        )

        snapshot_hash = (
            calculate_explicit_session_schedule_snapshot_sha256(snapshot)
        )
        if snapshot_hash not in assessment.session_schedule_sha256s.values():
            _fail(
                2,
                "session_schedule_mismatch",
                "session schedule does not match the assessment",
            )
    from market_validator.data.snapshot import parse_snapshot_manifest

    snapshot_manifest = _load(args.snapshot, parse_snapshot_manifest)
    from market_validator.data.snapshot import (
        serialize_snapshot_manifest,
    )

    if (
        _sha256_hex(serialize_snapshot_manifest(snapshot_manifest))
        != assessment.snapshot_manifest_sha256
    ):
        _fail(
            2,
            "snapshot_mismatch",
            "snapshot manifest does not match the assessment",
        )
    from market_validator.data.readiness import (
        GeneratedDataReadyManifest,
        calculate_data_ready_manifest_sha256,
        calculate_data_readiness_assessment_sha256,
    )

    generated_manifest = GeneratedDataReadyManifest(
        data_ready_manifest=manifest,
        data_ready_manifest_sha256=calculate_data_ready_manifest_sha256(
            manifest
        ),
        readiness_assessment_sha256=(
            calculate_data_readiness_assessment_sha256(assessment)
        ),
        snapshot_manifest_sha256="0" * 64,
        data_plan_sha256=calculate_data_plan_sha256(data_plan),
        acquisition_request_plan_sha256=(
            calculate_acquisition_request_plan_sha256(plan_model)
        ),
        research_spec_sha256="0" * 64,
        data_plan_confirmation_sha256="0" * 64,
        source_selection_sha256="0" * 64,
        source_selection_confirmation_sha256="0" * 64,
        instrument_registry_sha256="0" * 64,
        calendar_registry_sha256="0" * 64,
    )
    validate_data_ready_manifest_matches(
        generated_manifest,
        assessment,
        generated,
        data_plan,
        instruments,
        calendars,
    )
    print(
        _json_out(
            {
                "valid": True,
                "status": "verified",
                "artifact_type": "data-ready-manifest",
                "sha256": generated_manifest.data_ready_manifest_sha256,
            }
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="market-validator data-lifecycle",
        description="Unified JSON-only data lifecycle CLI (v0.3.0).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_schema = sub.add_parser("schema")
    p_schema.add_argument("artifact_type")
    p_schema.set_defaults(func=cmd_schema)

    p_validate = sub.add_parser("validate")
    p_validate.add_argument("artifact_type")
    p_validate.add_argument("path")
    p_validate.set_defaults(func=cmd_validate)

    p_ss = sub.add_parser("source-selection")
    ss_sub = p_ss.add_subparsers(dest="action", required=True)
    g = ss_sub.add_parser("generate")
    for name in (
        "research-spec",
        "data-plan",
        "data-plan-confirmation",
        "instrument-registry",
        "calendar-registry",
        "decisions",
        "output",
    ):
        g.add_argument(f"--{name}", required=True)
    g.set_defaults(func=cmd_source_selection)
    c = ss_sub.add_parser("confirm")
    for name in (
        "source-selection",
        "instrument-registry",
        "calendar-registry",
        "output",
    ):
        c.add_argument(f"--{name}", required=True)
    c.set_defaults(func=cmd_source_selection)
    v = ss_sub.add_parser("validate-confirmation")
    for name in (
        "source-selection",
        "source-selection-confirmation",
        "instrument-registry",
        "calendar-registry",
    ):
        v.add_argument(f"--{name}", required=True)
    v.set_defaults(func=cmd_source_selection)

    p_acq = sub.add_parser("acquisition")
    acq_sub = p_acq.add_subparsers(dest="action", required=True)
    g2 = acq_sub.add_parser("generate")
    for name in (
        "source-selection",
        "source-selection-confirmation",
        "data-plan",
        "instrument-registry",
        "calendar-registry",
        "output",
    ):
        g2.add_argument(f"--{name}", required=True)
    g2.add_argument("--capability", action="append", default=[])
    g2.add_argument("--session-schedule", action="append", default=[])
    g2.set_defaults(func=cmd_acquisition)
    v2 = acq_sub.add_parser("validate")
    v2.add_argument("--path", required=True)
    v2.set_defaults(func=cmd_acquisition)

    p_auth = sub.add_parser("authorization")
    auth_sub = p_auth.add_subparsers(dest="action", required=True)
    c2 = auth_sub.add_parser("create")
    for name in (
        "acquisition-plan",
        "instrument-registry",
        "calendar-registry",
        "output",
    ):
        c2.add_argument(f"--{name}", required=True)
    c2.add_argument("--capability", action="append", default=[])
    c2.add_argument("--authorize-network", action="store_true")
    c2.add_argument("--authorize-local-file-read", action="store_true")
    c2.set_defaults(func=cmd_authorization)
    v3 = auth_sub.add_parser("validate")
    v3.add_argument("--path", required=True)
    v3.set_defaults(func=cmd_authorization)

    p_exec = sub.add_parser("execution")
    exec_sub = p_exec.add_subparsers(dest="action", required=True)
    r = exec_sub.add_parser("run")
    for name in (
        "acquisition-plan",
        "authorization",
        "data-plan",
        "instrument-registry",
        "calendar-registry",
        "attempt-id",
        "receipt-output",
        "snapshot-root",
    ):
        r.add_argument(f"--{name}", required=True)
    r.add_argument("--capability", action="append", default=[])
    r.set_defaults(func=cmd_execution)

    p_sched = sub.add_parser("session-schedule")
    sched_sub = p_sched.add_subparsers(dest="action", required=True)
    sv = sched_sub.add_parser("validate")
    sv.add_argument("--path", required=True)
    sv.set_defaults(func=cmd_session_schedule)
    sp = sched_sub.add_parser("persist")
    sp.add_argument("--path", required=True)
    sp.add_argument("--output", required=True)
    sp.set_defaults(func=cmd_session_schedule)

    p_read = sub.add_parser("readiness")
    read_sub = p_read.add_subparsers(dest="action", required=True)
    ra = read_sub.add_parser("assess")
    for name in (
        "acquisition-plan",
        "data-plan",
        "instrument-registry",
        "calendar-registry",
        "source-selection",
        "source-selection-confirmation",
        "snapshot",
        "output",
    ):
        ra.add_argument(f"--{name}", required=True)
    ra.add_argument("--session-schedule", action="append", default=[])
    ra.set_defaults(func=cmd_readiness)
    rf = read_sub.add_parser("finalize")
    for name in (
        "assessment",
        "acquisition-plan",
        "data-plan",
        "instrument-registry",
        "calendar-registry",
        "source-selection",
        "source-selection-confirmation",
        "snapshot",
        "output",
    ):
        rf.add_argument(f"--{name}", required=True)
    rf.add_argument("--session-schedule", action="append", default=[])
    rf.set_defaults(func=cmd_readiness)
    rv = read_sub.add_parser("verify")
    for name in (
        "manifest",
        "assessment",
        "acquisition-plan",
        "data-plan",
        "instrument-registry",
        "calendar-registry",
        "source-selection",
        "source-selection-confirmation",
        "snapshot",
    ):
        rv.add_argument(f"--{name}", required=True)
    rv.add_argument("--session-schedule", action="append", default=[])
    rv.set_defaults(func=cmd_readiness)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code or 2)
    try:
        args.func(args)
    except SystemExit as error:
        return int(error.code or 2)
    except Exception:
        print(
            _json_out(
                {
                    "valid": False,
                    "status": "internal_error",
                    "code": "internal_error",
                }
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
