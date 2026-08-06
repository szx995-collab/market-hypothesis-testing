"""Phase 5 session schedule evidence tests."""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
import tempfile

from market_validator.data.session_schedule import (
    ExplicitSessionScheduleAdapter,
    ExplicitSessionScheduleSnapshot,
    SessionScheduleError,
    calculate_explicit_session_schedule_snapshot_sha256,
    canonical_session_set_sha256,
    parse_explicit_session_schedule_snapshot,
    persist_explicit_session_schedule_snapshot,
    serialize_explicit_session_schedule_snapshot,
)

SESSIONS = [
    date(2020, 1, 2),
    date(2020, 1, 3),
    date(2020, 1, 6),
    date(2020, 1, 7),
    date(2020, 1, 8),
]


def _snapshot(**overrides) -> ExplicitSessionScheduleSnapshot:
    payload = dict(
        schedule_id="test-schedule",
        calendar_id="synthetic.test.equity",
        schedule_adapter_id="test.fixed.daily",
        coverage_start=date(2019, 12, 30),
        coverage_end=date(2020, 1, 31),
        sessions=list(SESSIONS),
        verification_source_uri="https://example.invalid/schedule",
        verified_as_of=date(2026, 8, 1),
    )
    payload.update(overrides)
    return ExplicitSessionScheduleSnapshot(**payload)


class ExactSnapshotTest(unittest.TestCase):
    def test_exact_snapshot_round_trip_and_hash(self):
        snapshot = _snapshot()
        payload = serialize_explicit_session_schedule_snapshot(snapshot)
        self.assertEqual(
            parse_explicit_session_schedule_snapshot(payload), snapshot
        )
        self.assertEqual(
            calculate_explicit_session_schedule_snapshot_sha256(snapshot),
            calculate_explicit_session_schedule_snapshot_sha256(
                parse_explicit_session_schedule_snapshot(payload)
            ),
        )

    def test_duplicate_sessions_rejected(self):
        with self.assertRaises(ValueError):
            _snapshot(sessions=[SESSIONS[0], SESSIONS[0], SESSIONS[1]])

    def test_out_of_order_rejected(self):
        with self.assertRaises(ValueError):
            _snapshot(sessions=[SESSIONS[1], SESSIONS[0]])

    def test_coverage_outside_session_rejected(self):
        with self.assertRaises(ValueError):
            _snapshot(sessions=SESSIONS + [date(2020, 2, 3)])

    def test_empty_sessions_rejected(self):
        with self.assertRaises(ValueError):
            _snapshot(sessions=[])

    def test_uri_with_credentials_rejected(self):
        with self.assertRaises(ValueError):
            _snapshot(
                verification_source_uri=(
                    "https://user:secret@example.invalid/schedule"
                )
            )

    def test_verification_statement_fixed(self):
        with self.assertRaises(ValueError):
            _snapshot(verification_statement="anything else")


class AdapterQueryTest(unittest.TestCase):
    def test_sessions_between_exact(self):
        adapter = ExplicitSessionScheduleAdapter(_snapshot())
        self.assertEqual(
            adapter.sessions_between(date(2020, 1, 2), date(2020, 1, 7)),
            [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6), date(2020, 1, 7)],
        )

    def test_query_beyond_coverage_rejected(self):
        adapter = ExplicitSessionScheduleAdapter(_snapshot())
        with self.assertRaises(SessionScheduleError):
            adapter.sessions_between(date(2020, 2, 1), date(2020, 2, 28))

    def test_previous_sessions_insufficient_rejected(self):
        adapter = ExplicitSessionScheduleAdapter(_snapshot())
        with self.assertRaises(SessionScheduleError):
            adapter.previous_sessions(date(2020, 1, 2), count=3)

    def test_previous_sessions_exact(self):
        adapter = ExplicitSessionScheduleAdapter(_snapshot())
        self.assertEqual(
            adapter.previous_sessions(date(2020, 1, 7), count=2),
            [date(2020, 1, 3), date(2020, 1, 6)],
        )

    def test_canonical_empty_set_hash_is_deterministic(self):
        self.assertEqual(
            canonical_session_set_sha256([]),
            canonical_session_set_sha256([]),
        )


class HashBindingTest(unittest.TestCase):
    def test_middle_session_change_changes_hash_with_same_count_and_edges(self):
        first = _snapshot(
            sessions=[
                date(2020, 1, 2),
                date(2020, 1, 3),
                date(2020, 1, 6),
                date(2020, 1, 7),
                date(2020, 1, 8),
            ]
        )
        second = _snapshot(
            schedule_id="test-schedule-b",
            sessions=[
                date(2020, 1, 2),
                date(2020, 1, 3),
                date(2020, 1, 6),
                date(2020, 1, 7),
                date(2020, 1, 8),
            ]
        )
        middle = _snapshot(
            sessions=[
                date(2020, 1, 2),
                date(2020, 1, 5),
                date(2020, 1, 6),
                date(2020, 1, 7),
                date(2020, 1, 8),
            ]
        )
        # same count and same first/last, different middle session
        self.assertEqual(len(first.sessions), len(middle.sessions))
        self.assertEqual(first.sessions[0], middle.sessions[0])
        self.assertEqual(first.sessions[-1], middle.sessions[-1])
        self.assertNotEqual(
            calculate_explicit_session_schedule_snapshot_sha256(first),
            calculate_explicit_session_schedule_snapshot_sha256(middle),
        )
        # schedule_id is part of identity
        self.assertNotEqual(
            calculate_explicit_session_schedule_snapshot_sha256(first),
            calculate_explicit_session_schedule_snapshot_sha256(second),
        )

    def test_session_set_hash_changes_when_middle_session_changes(self):
        self.assertNotEqual(
            canonical_session_set_sha256(
                [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6)]
            ),
            canonical_session_set_sha256(
                [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 7)]
            ),
        )

    def test_adapter_mapping_order_does_not_change_hash(self):
        a = _snapshot(schedule_adapter_id="first.adapter")
        b = _snapshot(schedule_adapter_id="second.adapter")
        one = {
            "first.adapter": calculate_explicit_session_schedule_snapshot_sha256(a),
            "second.adapter": calculate_explicit_session_schedule_snapshot_sha256(b),
        }
        two = {
            "second.adapter": calculate_explicit_session_schedule_snapshot_sha256(b),
            "first.adapter": calculate_explicit_session_schedule_snapshot_sha256(a),
        }
        self.assertEqual(one, two)


class PersistenceTest(unittest.TestCase):
    def test_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.json"
            snapshot = _snapshot()
            persisted = persist_explicit_session_schedule_snapshot(
                snapshot, path
            )
            restored = parse_explicit_session_schedule_snapshot(
                persisted.read_bytes()
            )
            self.assertEqual(restored, snapshot)

    def test_persist_conflict_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.json"
            persist_explicit_session_schedule_snapshot(_snapshot(), path)
            with self.assertRaises(SessionScheduleError):
                persist_explicit_session_schedule_snapshot(
                    _snapshot(schedule_id="different"), path
                )

    def test_persist_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".." / "escape.json"
            with self.assertRaises(Exception):
                persist_explicit_session_schedule_snapshot(
                    _snapshot(), path
                )


if __name__ == "__main__":
    unittest.main()
