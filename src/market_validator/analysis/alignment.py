"""Strict same-session alignment (v0.4.0 Phase 2).

Aligns transformed variable series onto the target schedule using session
identity (not calendar days and not row numbers). Enforces the information
cutoff with full audit fields so look-ahead is provably absent.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Mapping

from market_validator.analysis.execution_models import (
    AlignedAnalysisDataset,
    AlignedAnalysisRow,
    AnalysisExecutionErrorCode,
    calculate_aligned_analysis_dataset_sha256,
    fail_execution,
)
from market_validator.analysis.planning import (
    ALIGNMENT_PROFILE_STRICT_SAME_SESSION,
    AlignmentPlan,
    AnalysisPlan,
    AnalysisVariableBinding,
    calculate_analysis_plan_sha256,
)
from market_validator.data.session_schedule import (
    ExplicitSessionScheduleSnapshot,
    SessionScheduleError,
)

SUPPORTED_ALIGNMENT_PROFILE = ALIGNMENT_PROFILE_STRICT_SAME_SESSION


def _iso_time_to_cutoff(
    target_session_date: date,
    local_time: str | None,
    timezone_name: str | None,
) -> str:
    """Render an aware cutoff instant for a target session.

    information_cutoff=null is handled by the caller (outcome available
    time). specified_local_time is rendered as a timezone-aware ISO
    instant; the calendar registry has no open/close contracts, so only
    local-time cutoffs are executable.
    """
    if local_time is None or timezone_name is None:
        fail_execution(
            AnalysisExecutionErrorCode.ALIGNMENT_FAILED,
            "specified_local_time requires an explicit local time and "
            "timezone",
        )
    try:
        parsed_time = time.fromisoformat(local_time)
    except ValueError:
        fail_execution(
            AnalysisExecutionErrorCode.ALIGNMENT_FAILED,
            "declared local time is not a valid time",
        )
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(timezone_name)
    except Exception:
        fail_execution(
            AnalysisExecutionErrorCode.ALIGNMENT_FAILED,
            "declared timezone cannot be resolved",
        )
    instant = datetime.combine(target_session_date, parsed_time, tzinfo=zone)
    return instant.isoformat()


def _binding_schedule_sha256(
    plan: AnalysisPlan, binding: AnalysisVariableBinding
) -> str:
    return binding.session_schedule_sha256


def _resolve_schedule(
    schedule_sha256: str,
    schedule_snapshots: Mapping[str, ExplicitSessionScheduleSnapshot],
) -> ExplicitSessionScheduleSnapshot:
    from market_validator.data.session_schedule import (
        calculate_explicit_session_schedule_snapshot_sha256,
    )

    for snapshot in schedule_snapshots.values():
        if (
            calculate_explicit_session_schedule_snapshot_sha256(snapshot)
            == schedule_sha256
        ):
            return snapshot
    fail_execution(
        AnalysisExecutionErrorCode.ALIGNMENT_FAILED,
        "no session schedule snapshot matches the plan binding",
    )


def _compute_effective_shift(binding: AnalysisVariableBinding) -> int:
    plan = binding.transformation_plan
    return plan.lag_periods + plan.availability_lag_periods


def align_analysis_series(
    *,
    plan: AnalysisPlan,
    transformed_series: Mapping[str, object],
    schedule_snapshots: Mapping[str, ExplicitSessionScheduleSnapshot],
    sample_start: date,
    sample_end: date,
) -> AlignedAnalysisDataset:
    """Build the strict same-session aligned dataset."""
    alignment: AlignmentPlan | None = plan.alignment_plan
    if alignment is None:
        fail_execution(
            AnalysisExecutionErrorCode.ALIGNMENT_FAILED,
            "the analysis plan has no alignment contract",
        )
    if (
        alignment.alignment_profile != SUPPORTED_ALIGNMENT_PROFILE
        or alignment.join_policy != "strict_match"
        or alignment.max_staleness_days != 0
    ):
        fail_execution(
            AnalysisExecutionErrorCode.ALIGNMENT_FAILED,
            "only strict same-session alignment is executable",
        )
    bindings = {
        binding.variable_id: binding
        for binding in plan.variable_bindings
    }
    if alignment.target_variable_id not in bindings:
        fail_execution(
            AnalysisExecutionErrorCode.ALIGNMENT_FAILED,
            "the alignment target is not a bound variable",
        )
    target_binding = bindings[alignment.target_variable_id]
    target_schedule = _resolve_schedule(
        _binding_schedule_sha256(plan, target_binding), schedule_snapshots
    )
    session_dates = [
        session
        for session in target_schedule.sessions
        if sample_start <= session <= sample_end
    ]
    if not session_dates:
        fail_execution(
            AnalysisExecutionErrorCode.ALIGNMENT_FAILED,
            "no target sessions fall inside the sample window",
        )
    by_session: dict[str, dict[str, object]] = {}
    for variable_id, series in transformed_series.items():
        by_session[variable_id] = {
            observation.session_date.isoformat(): observation
            for observation in series.observations
        }

    variables = [
        binding.variable_id for binding in plan.variable_bindings
    ]
    rows: list[AlignedAnalysisRow] = []
    dropped: dict[str, int] = {}
    candidate_count = 0

    for target_session_date in session_dates:
        candidate_count += 1
        shift_by_variable: dict[str, int] = {}
        for variable_id in variables:
            shift_by_variable[variable_id] = _compute_effective_shift(
                bindings[variable_id]
            )
        shifted_dates: dict[str, date] = {}
        session_index = target_schedule.sessions.index(target_session_date)
        for variable_id, shift in shift_by_variable.items():
            shifted_index = session_index - shift
            if shifted_index < 0:
                shifted_dates[variable_id] = None
                continue
            shifted_dates[variable_id] = target_schedule.sessions[
                shifted_index
            ]
        outcome = by_session.get(alignment.target_variable_id, {})
        outcome_observation = outcome.get(
            target_session_date.isoformat()
        )
        if outcome_observation is None:
            dropped["missing_outcome"] = dropped.get("missing_outcome", 0) + 1
            continue
        if alignment.information_cutoff is None:
            cutoff = outcome_observation.available_time
        else:
            cutoff_type = alignment.information_cutoff.get("type")
            if cutoff_type in ("before_target_open", "before_target_close"):
                fail_execution(
                    AnalysisExecutionErrorCode.ALIGNMENT_FAILED,
                    "before-open and before-close cutoffs are not "
                    "executable",
                )
            cutoff = _iso_time_to_cutoff(
                target_session_date,
                alignment.information_cutoff.get("local_time"),
                alignment.information_cutoff.get("timezone"),
            )
        row_values: dict[str, object] = {
            "target_session_date": target_session_date,
            "outcome": outcome_observation.value,
            "predictors": {},
            "controls": {},
            "source_session_dates": {},
            "available_times": {},
        }
        row_valid = True
        for variable_id in variables:
            if variable_id == alignment.target_variable_id:
                shifted = target_session_date
            else:
                shifted = shifted_dates[variable_id]
            if shifted is None:
                dropped["insufficient_pre_sample"] = (
                    dropped.get("insufficient_pre_sample", 0) + 1
                )
                row_valid = False
                break
            observation = by_session[variable_id].get(shifted.isoformat())
            if observation is None:
                if (
                    plan.alignment_plan.missing_data_policy == "error"
                    and variable_id != alignment.target_variable_id
                ):
                    fail_execution(
                        AnalysisExecutionErrorCode.MISSING_DATA_ERROR,
                        "a variable observation is missing under the error "
                        "policy",
                    )
                dropped["missing_variable_observation"] = (
                    dropped.get("missing_variable_observation", 0) + 1
                )
                row_valid = False
                break
            try:
                observation_instant = datetime.fromisoformat(
                    observation.available_time
                )
                cutoff_instant = datetime.fromisoformat(cutoff)
            except (TypeError, ValueError):
                fail_execution(
                    AnalysisExecutionErrorCode.ALIGNMENT_FAILED,
                    "observation or cutoff time is not an ISO instant",
                )
            if observation_instant > cutoff_instant:
                if plan.alignment_plan.missing_data_policy == "error":
                    fail_execution(
                        AnalysisExecutionErrorCode.LOOKAHEAD_DETECTED,
                        "a variable observation becomes available after the "
                        "information cutoff",
                    )
                dropped["available_after_cutoff"] = (
                    dropped.get("available_after_cutoff", 0) + 1
                )
                row_valid = False
                break
            role = bindings[variable_id].role
            if role == "outcome":
                row_values["outcome"] = observation.value
            elif role == "predictor":
                row_values["predictors"][variable_id] = observation.value
            elif role == "control":
                row_values["controls"][variable_id] = observation.value
            row_values["source_session_dates"][variable_id] = (
                shifted.isoformat()
            )
            row_values["available_times"][variable_id] = (
                observation.available_time
            )
        if not row_valid:
            continue
        rows.append(
            AlignedAnalysisRow(
                target_session_date=target_session_date,
                outcome_value=row_values["outcome"],
                predictor_values=row_values["predictors"],
                control_values=row_values["controls"],
                source_session_dates={
                    key: date.fromisoformat(value)
                    for key, value in row_values[
                        "source_session_dates"
                    ].items()
                },
                available_times=row_values["available_times"],
            )
        )
    rows.sort(key=lambda row: row.target_session_date)
    if len({row.target_session_date for row in rows}) != len(rows):
        fail_execution(
            AnalysisExecutionErrorCode.ALIGNMENT_FAILED,
            "aligned rows contain duplicate target sessions",
        )
    if len(rows) < plan.minimum_usable_observations:
        fail_execution(
            AnalysisExecutionErrorCode.INSUFFICIENT_USABLE_OBSERVATIONS,
            "retained observations are below the plan minimum",
        )
    dataset = AlignedAnalysisDataset(
        dataset_schema_version="1.0",
        analysis_plan_sha256=calculate_analysis_plan_sha256(plan),
        target_variable_id=alignment.target_variable_id,
        variable_ids=sorted(variables),
        candidate_row_count=candidate_count,
        retained_row_count=len(rows),
        dropped_row_count=candidate_count - len(rows),
        drop_reason_counts=dropped,
        sample_start=sample_start,
        sample_end=sample_end,
        rows=rows,
        dataset_sha256="0" * 64,
    )
    dataset = dataset.model_copy(
        update={
            "dataset_sha256": calculate_aligned_analysis_dataset_sha256(
                dataset
            )
        }
    )
    return dataset


__all__ = ["SUPPORTED_ALIGNMENT_PROFILE", "align_analysis_series"]
