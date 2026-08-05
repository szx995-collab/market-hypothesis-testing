"""JSON-only command handlers for offline data contracts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pydantic import ValidationError

from market_validator.credentials import CredentialResolutionError
from market_validator.data.calendars import CalendarRegistry, CalendarRegistryError
from market_validator.data.models import DataRequirement, QualityStatus
from market_validator.data.planner import DataPlanningError, plan_data_requirements
from market_validator.data.providers.csv_provider import CSVProvider, CSVProviderError
from market_validator.data.providers.fred_provider import (
    FredProvider,
    FredProviderError,
)
from market_validator.data.providers.fred_transport import FredTransportError
from market_validator.data.registry import InstrumentRegistry, InstrumentRegistryError
from market_validator.research.models import ResearchSpec

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CALENDAR_CONFIG = REPOSITORY_ROOT / "config" / "calendars.json"
INSTRUMENT_CONFIG = REPOSITORY_ROOT / "config" / "instruments.json"


def add_data_parser(subparsers: argparse._SubParsersAction) -> None:
    data_parser = subparsers.add_parser("data", help="offline data contracts")
    data_commands = data_parser.add_subparsers(dest="data_command", required=True)

    plan_parser = data_commands.add_parser(
        "plan", help="create a deterministic DataPlan from a ResearchSpec"
    )
    plan_parser.add_argument("research_spec", type=Path)

    registry_parser = data_commands.add_parser(
        "registry", help="inspect the normalized instrument registry"
    )
    registry_commands = registry_parser.add_subparsers(
        dest="registry_command", required=True
    )
    registry_commands.add_parser("validate", help="validate instrument mappings")

    data_commands.add_parser("calendars", help="list calendar identity metadata")
    data_commands.add_parser("providers", help="list implemented data providers")

    csv_parser = data_commands.add_parser("csv", help="inspect local CSV data")
    csv_commands = csv_parser.add_subparsers(dest="csv_command", required=True)
    inspect_parser = csv_commands.add_parser(
        "inspect", help="inspect one explicit local long-table CSV"
    )
    inspect_parser.add_argument("csv_path", type=Path)

    fred_parser = data_commands.add_parser("fred", help="inspect or fetch FRED data")
    fred_commands = fred_parser.add_subparsers(dest="fred_command", required=True)
    fred_commands.add_parser("status", help="check local FRED configuration only")
    fetch_parser = fred_commands.add_parser(
        "fetch", help="dry-run a FRED requirement unless --live is explicit"
    )
    fetch_parser.add_argument("requirement_json", type=Path)
    fetch_parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly authorize a real FRED HTTPS request",
    )
    fetch_parser.add_argument(
        "--interactive",
        action="store_true",
        help="allow a secure GUI or TTY credential prompt when live access lacks a key",
    )


def _load_registries() -> tuple[CalendarRegistry, InstrumentRegistry]:
    calendars = CalendarRegistry.from_json_file(CALENDAR_CONFIG)
    instruments = InstrumentRegistry.from_json_file(
        INSTRUMENT_CONFIG, calendars
    )
    return calendars, instruments


def _print_error(code: str, message: str, *, unexpected: bool = False) -> int:
    print(
        json.dumps(
            {
                "valid": False,
                "code": code,
                "errors": [{"code": code, "message": message}],
            },
            sort_keys=True,
        )
    )
    return 1 if unexpected else 2


def _print_public_error(error: object) -> int:
    detail = error.public_error()
    print(
        json.dumps(
            {
                "valid": False,
                "code": detail["code"],
                "errors": [detail],
            },
            sort_keys=True,
        )
    )
    return 2


def handle_data_command(args: argparse.Namespace) -> int:
    """Execute a parsed data command with structured domain failures."""
    try:
        if args.data_command == "registry":
            calendars, instruments = _load_registries()
            mapping_count = sum(
                len(entry.provider_mappings) for entry in instruments.entries
            )
            unresolved = sorted(
                entry.instrument_id
                for entry in instruments.entries
                if not entry.provider_mappings
            )
            print(
                json.dumps(
                    {
                        "valid": True,
                        "instrument_count": len(instruments.entries),
                        "provider_mapping_count": mapping_count,
                        "unresolved_instruments": unresolved,
                        "calendar_count": len(calendars.definitions),
                    },
                    sort_keys=True,
                )
            )
            return 0

        if args.data_command == "calendars":
            calendars = CalendarRegistry.from_json_file(CALENDAR_CONFIG)
            print(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "calendars": [
                            definition.model_dump(mode="json")
                            for definition in calendars.definitions
                        ],
                        "schedule_calculation_implemented": False,
                    },
                    sort_keys=True,
                )
            )
            return 0

        if args.data_command == "providers":
            _, instruments = _load_registries()
            providers = [
                CSVProvider(Path("__local_file_not_selected__")),
                FredProvider(instruments),
            ]
            print(
                json.dumps(
                    {
                        "providers": [
                            {
                                "status": provider.status().model_dump(mode="json"),
                                "capabilities": provider.capabilities().model_dump(
                                    mode="json"
                                ),
                            }
                            for provider in providers
                        ]
                    },
                    sort_keys=True,
                )
            )
            return 0

        if args.data_command == "plan":
            raw_spec = args.research_spec.read_text(encoding="utf-8")
            research_spec = ResearchSpec.model_validate_json(raw_spec)
            calendars, instruments = _load_registries()
            plan = plan_data_requirements(research_spec, instruments, calendars)
            print(json.dumps(plan.model_dump(mode="json"), sort_keys=True))
            return 0

        if args.data_command == "csv" and args.csv_command == "inspect":
            inspection = CSVProvider(args.csv_path).inspect()
            print(
                json.dumps(
                    {
                        "valid": inspection.quality.status is not QualityStatus.FAIL,
                        "provider_id": CSVProvider.provider_id,
                        "source": inspection.source.model_dump(mode="json"),
                        "quality": inspection.quality.model_dump(mode="json"),
                    },
                    sort_keys=True,
                )
            )
            return 2 if inspection.quality.status is QualityStatus.FAIL else 0

        if args.data_command == "fred":
            _, instruments = _load_registries()
            if args.fred_command == "status":
                status = FredProvider(instruments).status()
                print(json.dumps(status.model_dump(mode="json"), sort_keys=True))
                return 0

            if args.fred_command == "fetch":
                raw_requirement = args.requirement_json.read_text(encoding="utf-8")
                requirement = DataRequirement.model_validate_json(raw_requirement)
                provider = FredProvider(
                    instruments,
                    allow_network=bool(args.live),
                    interactive=bool(args.interactive),
                    environment=os.environ,
                )
                if not args.live:
                    print(json.dumps(provider.dry_run(requirement), sort_keys=True))
                    return 0

                bundle = provider.fetch(requirement)
                snapshot = provider.last_snapshot
                if snapshot is None:
                    raise FredProviderError(
                        "FRED fetch did not produce an auditable snapshot"
                    )
                print(
                    json.dumps(
                        {
                            "provider_id": provider.provider_id,
                            "dry_run": False,
                            "network_requested": True,
                            "observation_count": len(bundle.observations),
                            "quality_status": bundle.quality.status.value,
                            "source": bundle.source.model_dump(mode="json"),
                            "snapshot": snapshot.public_summary(),
                        },
                        sort_keys=True,
                    )
                )
                return 0
    except ValidationError as error:
        messages = "; ".join(
            detail["msg"]
            for detail in error.errors(
                include_url=False, include_context=False, include_input=False
            )
        )
        return _print_error("validation_error", messages)
    except (
        OSError,
        UnicodeError,
        CalendarRegistryError,
        InstrumentRegistryError,
        DataPlanningError,
        CSVProviderError,
    ) as error:
        return _print_error("data_contract_error", str(error))
    except (FredProviderError, FredTransportError) as error:
        return _print_public_error(error)
    except CredentialResolutionError as error:
        return _print_public_error(error)
    except Exception:
        return _print_error(
            "internal_error", "unexpected internal data-command error", unexpected=True
        )

    return _print_error("unsupported_command", "unsupported data command")
