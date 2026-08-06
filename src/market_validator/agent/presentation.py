"""Human-readable presentation and error translation (v0.4.0 Phase 4).

Structured domain failures become concise user-facing NextActions; the
original safe error code is always preserved as source_error_code.
"""

from __future__ import annotations

from market_validator.agent.models import (
    NextAction,
    ResearchSession,
)

_STAGE_NAMES: dict[str, str] = {
    "hypothesis_proposal": "假设提案",
    "hypothesis_clarification": "假设澄清",
    "hypothesis_confirmation": "假设确认",
    "research_spec": "ResearchSpec 补全",
    "data_plan": "数据计划",
    "data_plan_confirmation": "数据计划确认",
    "source_selection": "数据源选择",
    "source_selection_confirmation": "数据源确认",
    "acquisition_planning": "获取请求规划",
    "data_access_authorization": "数据访问授权",
    "data_acquisition": "数据获取",
    "data_readiness": "数据就绪",
    "analysis_planning": "分析计划",
    "analysis_confirmation": "分析计划确认",
    "analysis_authorization": "分析授权",
    "analysis_execution": "分析执行",
    "interpretation": "结果解释",
    "completed": "已完成",
}


def human_stage_name(stage: str) -> str:
    return _STAGE_NAMES.get(stage, stage)


def describe_blockers(assessment) -> str:
    """Translate readiness blockers into plain language."""
    if not assessment.blockers:
        return "无"
    lines = []
    for blocker in assessment.blockers:
        lines.append(f"- {blocker.message}（{blocker.code}）")
    return "\n".join(lines)


def format_session_status(session: ResearchSession) -> str:
    lines = [
        f"当前阶段：{human_stage_name(session.current_stage)}",
        f"状态：{session.status}",
        f"session id：{session.session_id}",
        f"revision：{session.revision}",
    ]
    action = session.current_next_action
    if action is not None:
        lines.append("")
        lines.append(f"下一动作：{action.title}")
        if action.message:
            lines.append(action.message)
        if action.why_needed:
            lines.append(f"（{action.why_needed}）")
        if action.source_error_code:
            lines.append(f"错误码：{action.source_error_code}")
    if session.completed_report:
        lines.append("")
        lines.append(f"最终报告：{session.completed_report}")
    return "\n".join(lines)


def format_session_json(session: ResearchSession) -> dict[str, object]:
    action = session.current_next_action
    return {
        "valid": True,
        "session_id": session.session_id,
        "revision": session.revision,
        "status": session.status,
        "current_stage": session.current_stage,
        "next_action": (
            {
                "action_type": action.action_type,
                "title": action.title,
                "message": action.message,
                "required_inputs": action.required_inputs,
                "available_choices": action.available_choices,
                "source_error_code": action.source_error_code,
            }
            if action is not None
            else None
        ),
        "artifact_paths": [
            reference.relative_path
            for reference in session.artifact_references
        ],
        "report_path": session.completed_report,
    }


def error_to_next_action(
    *,
    title: str,
    message: str,
    source_error_code: str | None,
) -> NextAction:
    return NextAction(
        action_type="blocked",
        title=title,
        message=message,
        why_needed="结构化错误需要用户决定下一步",
        source_error_code=source_error_code,
    )


__all__ = [
    "describe_blockers",
    "error_to_next_action",
    "format_session_json",
    "format_session_status",
    "human_stage_name",
]
