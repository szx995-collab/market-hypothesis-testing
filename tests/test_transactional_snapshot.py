"""Contract tests for the whole-snapshot transaction (Phase 3, F/G/H)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from market_validator.data.execution import (
    ExecutionError,
    ExecutionErrorCode,
    execute_authorized_acquisition,
)
from market_validator.data.snapshot import (
    ExecutionStatus,
    SnapshotError,
    parse_snapshot_manifest,
    serialize_snapshot_manifest,
    verify_acquisition_snapshot,
)
from market_validator.data.models import DataBundle

from tests.test_provider_execution import (
    _SeriesAwareFakeTransport,
    _build_fred_chain,
)

ROOT = Path(__file__).resolve().parents[1]


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _execute_once(
    tmp: Path,
    transport: object | None = None,
    attempt_id: str = "attempt-txn",
) -> tuple[object, object]:
    (tmp / "snapshots").mkdir(exist_ok=True)
    transport = transport or _SeriesAwareFakeTransport()
    chain = _build_fred_chain(transport)
    verified = execute_authorized_acquisition(
        generated_plan=chain["plan"],
        authorization=chain["authorization"],
        data_plan=chain["data_plan"],
        instrument_registry=chain["instruments"],
        calendar_registry=chain["calendars"],
        capability_snapshots=chain["capabilities"],
        attempt_id=attempt_id,
        receipt_path=tmp / "receipt.json",
        snapshot_root=tmp / "snapshots",
        adapters=chain["adapters"],
    )
    return verified, chain


class MultiRequestTransactionTest(unittest.TestCase):
    def test_two_requests_commit_as_one_snapshot(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, chain = _execute_once(tmp)
            self.assertEqual(
                len(verified.manifest.request_records), 2
            )
            self.assertEqual(
                sorted(verified.outcome.executed_request_ids),
                sorted(
                    request.requirement_id
                    for request in chain["plan"].acquisition_request_plan.requests
                ),
            )
            self.assertEqual(
                verified.outcome.status, ExecutionStatus.SUCCEEDED
            )

    def test_second_request_failure_leaves_no_final_snapshot(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            transport = _SeriesAwareFakeTransport()
            # Make the second requirement's observations page malformed:
            transport.pages[0] = {"count": 2, "offset": 0, "limit": 2,
                                  "observations": [{"date": "2020-01-01",
                                                    "value": "1.0"}]}
            # Empty page before count is satisfied must fail closed:
            transport.pages[1] = {"count": 2, "offset": 1, "limit": 2,
                                  "observations": []}
            with self.assertRaises(ExecutionError) as raised:
                _execute_once(tmp, transport=transport)
            self.assertEqual(
                raised.exception.code,
                ExecutionErrorCode.PROVIDER_EXECUTION_FAILED,
            )
            self.assertEqual(
                list((tmp / "snapshots").iterdir()), [],
                "no final snapshot may exist after a failed transaction",
            )

    def test_execution_order_is_deterministic(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            transport = _SeriesAwareFakeTransport()
            verified, chain = _execute_once(tmp, transport=transport)
            observed = [
                parameters.get("series_id")
                for path, parameters in transport.calls
                if path == "/fred/series"
            ]
            expected = [
                mapping.provider_symbol
                for request in chain["plan"].acquisition_request_plan.requests
                for mapping in [
                    chain["instruments"]
                    .get(request.instrument_id)
                    .provider_mappings[0]
                ]
            ]
            self.assertEqual(observed, expected)


class AllOrNoneSnapshotTest(unittest.TestCase):
    def _assert_no_final(self, snapshots: Path) -> None:
        entries = list(snapshots.iterdir())
        self.assertEqual(entries, [])

    def test_failure_on_first_raw_write(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            from market_validator.data import snapshot as snapshot_module

            original = snapshot_module._write_fsynced
            calls = {"count": 0}

            def failing_first(path: Path, content: bytes) -> None:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError("injected write failure")
                return original(path, content)

            with mock.patch.object(
                snapshot_module, "_write_fsynced", failing_first
            ):
                with self.assertRaises(ExecutionError) as raised:
                    _execute_once(tmp)
            self.assertEqual(
                raised.exception.code,
                ExecutionErrorCode.SNAPSHOT_COMMIT_FAILED,
            )
            self._assert_no_final(tmp / "snapshots")

    def test_failure_on_manifest_write(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            from market_validator.data import snapshot as snapshot_module

            original = snapshot_module._write_fsynced
            calls = {"count": 0}

            def failing_manifest(path: Path, content: bytes) -> None:
                if path.name == "manifest.json":
                    raise OSError("injected manifest write failure")
                return original(path, content)

            with mock.patch.object(
                snapshot_module, "_write_fsynced", failing_manifest
            ):
                with self.assertRaises(ExecutionError):
                    _execute_once(tmp)
            self._assert_no_final(tmp / "snapshots")

    def test_failure_on_directory_rename(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            from market_validator.data import snapshot as snapshot_module

            with mock.patch.object(
                snapshot_module.os, "rename", side_effect=OSError("injected")
            ):
                with self.assertRaises(ExecutionError) as raised:
                    _execute_once(tmp)
            self.assertEqual(
                raised.exception.code,
                ExecutionErrorCode.SNAPSHOT_COMMIT_FAILED,
            )
            self._assert_no_final(tmp / "snapshots")

    def test_failure_on_final_verification(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            from market_validator.data import snapshot as snapshot_module

            with mock.patch.object(
                snapshot_module,
                "_verify_final_tree",
                side_effect=SnapshotError("injected verification failure"),
            ):
                with self.assertRaises(ExecutionError):
                    _execute_once(tmp)
            self._assert_no_final(tmp / "snapshots")


class SnapshotTamperingTest(unittest.TestCase):
    def _commit(self, tmp: Path) -> tuple[object, Path, object]:
        verified, chain = _execute_once(tmp)
        return verified, verified.snapshot_path, chain

    def test_raw_hash_change_rejected(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, snapshot_path, chain = self._commit(tmp)
            record = verified.manifest.request_records[0]
            raw = next(
                artifact
                for artifact in record.artifacts
                if artifact.role == "raw"
            )
            target = snapshot_path / "requests" / record.requirement_id / raw.relative_path
            target.write_bytes(b"tampered")
            with self.assertRaises(SnapshotError):
                verify_acquisition_snapshot(
                    snapshot_path=snapshot_path,
                    expected_plan_sha256=(
                        verified.manifest.acquisition_request_plan_sha256
                    ),
                    expected_authorization_sha256=(
                        verified.manifest.authorization_sha256
                    ),
                    expected_receipt_sha256=(
                        verified.manifest.authorization_receipt_sha256
                    ),
                    plan=chain["plan"],
                    data_plan=chain["data_plan"],
                )

    def test_bundle_hash_change_rejected(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, snapshot_path, chain = self._commit(tmp)
            record = verified.manifest.request_records[0]
            bundle = next(
                artifact
                for artifact in record.artifacts
                if artifact.role == "bundle"
            )
            target = (
                snapshot_path
                / "requests"
                / record.requirement_id
                / bundle.relative_path
            )
            target.write_bytes(b"{}")
            with self.assertRaises(SnapshotError):
                verify_acquisition_snapshot(
                    snapshot_path=snapshot_path,
                    expected_plan_sha256=(
                        verified.manifest.acquisition_request_plan_sha256
                    ),
                    expected_authorization_sha256=(
                        verified.manifest.authorization_sha256
                    ),
                    expected_receipt_sha256=(
                        verified.manifest.authorization_receipt_sha256
                    ),
                    plan=chain["plan"],
                    data_plan=chain["data_plan"],
                )

    def test_extra_file_rejected(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, snapshot_path, chain = self._commit(tmp)
            (snapshot_path / "requests" / "extra.json").write_bytes(b"{}")
            with self.assertRaises(SnapshotError):
                verify_acquisition_snapshot(
                    snapshot_path=snapshot_path,
                    expected_plan_sha256=(
                        verified.manifest.acquisition_request_plan_sha256
                    ),
                    expected_authorization_sha256=(
                        verified.manifest.authorization_sha256
                    ),
                    expected_receipt_sha256=(
                        verified.manifest.authorization_receipt_sha256
                    ),
                    plan=chain["plan"],
                    data_plan=chain["data_plan"],
                )

    def test_missing_file_rejected(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, snapshot_path, chain = self._commit(tmp)
            record = verified.manifest.request_records[0]
            bundle = next(
                artifact
                for artifact in record.artifacts
                if artifact.role == "bundle"
            )
            (
                snapshot_path
                / "requests"
                / record.requirement_id
                / bundle.relative_path
            ).unlink()
            with self.assertRaises(SnapshotError):
                verify_acquisition_snapshot(
                    snapshot_path=snapshot_path,
                    expected_plan_sha256=(
                        verified.manifest.acquisition_request_plan_sha256
                    ),
                    expected_authorization_sha256=(
                        verified.manifest.authorization_sha256
                    ),
                    expected_receipt_sha256=(
                        verified.manifest.authorization_receipt_sha256
                    ),
                    plan=chain["plan"],
                    data_plan=chain["data_plan"],
                )

    def test_manifest_change_rejected(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, snapshot_path, chain = self._commit(tmp)
            manifest = parse_snapshot_manifest(
                (snapshot_path / "manifest.json").read_bytes()
            )
            changed = manifest.model_copy(
                update={"attempt_id": "tampered-attempt"}
            )
            (snapshot_path / "manifest.json").write_bytes(
                serialize_snapshot_manifest(changed)
            )
            with self.assertRaises(SnapshotError):
                verify_acquisition_snapshot(
                    snapshot_path=snapshot_path,
                    expected_plan_sha256=(
                        verified.manifest.acquisition_request_plan_sha256
                    ),
                    expected_authorization_sha256=(
                        verified.manifest.authorization_sha256
                    ),
                    expected_receipt_sha256=(
                        verified.manifest.authorization_receipt_sha256
                    ),
                    plan=chain["plan"],
                    data_plan=chain["data_plan"],
                )

    def test_stale_plan_hash_rejected(self) -> None:
        with TemporaryDirectory(dir=ROOT) as directory:
            tmp = Path(directory)
            verified, snapshot_path, chain = self._commit(tmp)
            with self.assertRaises(SnapshotError):
                verify_acquisition_snapshot(
                    snapshot_path=snapshot_path,
                    expected_plan_sha256="0" * 64,
                    expected_authorization_sha256=(
                        verified.manifest.authorization_sha256
                    ),
                    expected_receipt_sha256=(
                        verified.manifest.authorization_receipt_sha256
                    ),
                    plan=chain["plan"],
                    data_plan=chain["data_plan"],
                )


if __name__ == "__main__":
    unittest.main()
