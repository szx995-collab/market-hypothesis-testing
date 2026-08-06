"""Research agent orchestrator (v0.4.0 Phase 4).

Drives the existing domain lifecycle deterministically: verify artifacts,
advance only safe steps automatically, stop at every user commitment
(confirmations, authorizations, network, files), and record every state
change as an immutable session revision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from market_validator.agent.models import (
    ArtifactReference,
    NextAction,
    ResearchAgentErrorCode,
    ResearchSession,
    _derive_session_id,
    fail_agent,
    parse_research_session,
    serialize_research_session,
)
from market_validator.agent.session import (
    load_latest_research_session,
    load_verified_artifact,
    make_artifact_reference,
    persist_session_revision,
    register_artifact_parser,
)
from market_validator.agent.presentation import (
    describe_blockers,
    human_stage_name,
)

MAX_TRANSITIONS = 32

ACQUISITION_ATTEMPT_ID = "agent-{session_id:.16}-acq"
ANALYSIS_ATTEMPT_ID = "agent-{session_id:.16}-exec"


def _register_all_parsers() -> None:
    """Register the strict parsers used to verify artifact references."""
    from market_validator.hypothesis.serialization import (
        parse_research_hypothesis_proposal,
    )
    from market_validator.hypothesis.confirmation import (
        parse_research_hypothesis_confirmation,
    )
    from market_validator.research.serialization import parse_research_spec
    from market_validator.data.serialization import parse_data_plan
    from market_validator.data.data_plan_review import (
        parse_data_plan_confirmation,
    )
    from market_validator.data.source_selection import (
        parse_source_selection,
        parse_source_selection_confirmation,
    )
    from market_validator.data.acquisition_request import (
        parse_acquisition_request_plan,
    )
    from market_validator.data.access_authorization import (
        parse_data_access_authorization,
    )
    from market_validator.data.snapshot import (
        parse_snapshot_manifest,
    )
    from market_validator.data.readiness import (
        parse_data_readiness_assessment,
        parse_data_ready_manifest,
    )
    from market_validator.analysis.planning import parse_analysis_plan
    from market_validator.analysis.authorization import (
        parse_analysis_authorization,
        parse_analysis_plan_confirmation,
    )
    from market_validator.analysis.execution_models import (
        parse_analysis_result,
    )

    register_artifact_parser(
        "hypothesis_proposal", parse_research_hypothesis_proposal
    )
    register_artifact_parser(
        "hypothesis_confirmation", parse_research_hypothesis_confirmation
    )
    register_artifact_parser("research_spec", parse_research_spec)
    register_artifact_parser("data_plan", parse_data_plan)
    register_artifact_parser(
        "data_plan_confirmation", parse_data_plan_confirmation
    )
    register_artifact_parser("source_selection", parse_source_selection)
    register_artifact_parser(
        "source_selection_confirmation", parse_source_selection_confirmation
    )
    register_artifact_parser(
        "acquisition_request_plan", parse_acquisition_request_plan
    )
    register_artifact_parser(
        "data_access_authorization", parse_data_access_authorization
    )
    register_artifact_parser(
        "acquisition_snapshot", parse_snapshot_manifest
    )
    register_artifact_parser(
        "readiness_assessment", parse_data_readiness_assessment
    )
    register_artifact_parser(
        "data_ready_manifest", parse_data_ready_manifest
    )
    register_artifact_parser("analysis_plan", parse_analysis_plan)
    register_artifact_parser(
        "analysis_plan_confirmation", parse_analysis_plan_confirmation
    )
    register_artifact_parser(
        "analysis_authorization", parse_analysis_authorization
    )
    register_artifact_parser("analysis_result", parse_analysis_result)
    register_artifact_parser("validation_report", lambda payload: payload)


_register_all_parsers()


@dataclass
class AgentConfig:
    """Everything the orchestrator needs that must not live in the session."""

    workspace: str
    language: str = "zh-CN"
    allow_llm_network: bool = False
    allow_data_network: bool = False
    proposal_backend: object | None = None      # StructuredGenerationBackend
    expected_backend: str | None = None
    expected_model: str | None = None
    providers: dict[str, object] = field(default_factory=dict)  # provider_id -> instance
    session_adapters: dict[str, object] = field(default_factory=dict)
    public_parameter_templates: dict[str, object] = field(default_factory=dict)
    schedule_snapshots: dict[str, object] = field(default_factory=dict)
    interpretation_client: object | None = None
    interpretation_model: str = "fixture-v1"
    clock: object | None = None

    def now(self) -> datetime:
        if self.clock is not None:
            return self.clock()
        return datetime.now(timezone.utc)


def _workspace(config: AgentConfig) -> Path:
    return Path(config.workspace)


def _artifacts_dir(config: AgentConfig) -> Path:
    path = _workspace(config) / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_artifact(config: AgentConfig, name: str, payload: bytes) -> Path:
    path = _artifacts_dir(config) / name
    for part in [path, *path.parents]:
        if part.is_symlink():
            fail_agent(
                ResearchAgentErrorCode.AGENT_OUTPUT_ERROR,
                "artifact output path must not contain symlinks",
            )
        if part == _artifacts_dir(config):
            break
    if path.exists():
        existing = path.read_bytes()
        if existing != payload:
            fail_agent(
                ResearchAgentErrorCode.AGENT_OUTPUT_CONFLICT,
                "an artifact already exists with different content",
            )
        return path
    path.write_bytes(payload)
    return path


def _next_revision(config: AgentConfig) -> int:
    try:
        latest = load_latest_research_session(config.workspace)
        return latest.revision + 1
    except Exception:
        return 1


def _new_session(
    config: AgentConfig,
    *,
    question: str,
    parent_session_id: str | None,
) -> ResearchSession:
    created = config.now()
    session = ResearchSession(
        session_schema_version="1.0",
        session_id=_derive_session_id(question, created, parent_session_id),
        revision=1,
        parent_session_id=parent_session_id,
        created_at=created,
        updated_at=created,
        language=config.language,
        status="active",
        current_stage="hypothesis_proposal",
        original_question=question,
        artifact_references=[],
        current_next_action=None,
        last_error=None,
        completed_report=None,
        history_summary=["session created"],
    )
    return session


def _commit(
    config: AgentConfig,
    session: ResearchSession,
    *,
    stage: str | None = None,
    status: str | None = None,
    next_action: NextAction | None = None,
    last_error: str | None = None,
    history: str | None = None,
    artifacts: list[ArtifactReference] | None = None,
    completed_report: str | None = None,
) -> ResearchSession:
    """Write the next immutable revision of the session."""
    updated = session.model_copy(
        update={
            "revision": session.revision + 1,
            "updated_at": config.now(),
            "current_stage": stage or session.current_stage,
            "status": status or session.status,
            "current_next_action": next_action,
            "last_error": last_error if last_error is not None else None,
            "completed_report": (
                completed_report
                if completed_report is not None
                else session.completed_report
            ),
            "history_summary": (
                session.history_summary + [history] if history else session.history_summary
            ),
            "artifact_references": (
                artifacts if artifacts is not None else session.artifact_references
            ),
        }
    )
    persist_session_revision(updated, config.workspace)
    return updated


def _artifact(
    session: ResearchSession, artifact_type: str
) -> ArtifactReference | None:
    for reference in session.artifact_references:
        if reference.artifact_type == artifact_type:
            return reference
    return None


def _load_artifact(config: AgentConfig, session: ResearchSession, artifact_type: str):
    reference = _artifact(session, artifact_type)
    if reference is None:
        return None
    artifact, _payload = load_verified_artifact(config.workspace, reference)
    return artifact


def _append_artifact(
    config: AgentConfig,
    session: ResearchSession,
    artifact_type: str,
    relative_path: str,
    payload: bytes,
    schema_version: str,
) -> ResearchSession:
    reference = make_artifact_reference(
        artifact_type,
        relative_path,
        payload,
        schema_version,
        created_at=config.now(),
    )
    return session.model_copy(
        update={
            "artifact_references": session.artifact_references
            + [reference]
        }
    )


# ---------------------------------------------------------------------------
# Stage handlers
# ---------------------------------------------------------------------------

def _stage_hypothesis_proposal(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    proposal = _load_artifact(config, session, "hypothesis_proposal")
    if proposal is None:
        if not config.allow_llm_network or config.proposal_backend is None:
            action = NextAction(
                action_type="request_llm_access",
                title="需要 LLM 访问权限来生成研究假设草案",
                message=(
                    "系统需要调用 LLM 将自然语言问题结构化为研究假设草案。"
                    "请提供 --allow-llm-network 并配置 provider/model，"
                    "或提供已生成的 proposal 文件。"
                ),
                why_needed="假设草案必须由结构化生成服务产生",
                required_inputs=["allow_llm_network", "provider", "model"],
            )
            return _commit(
                config, session, status="needs_user", next_action=action,
                history="等待 LLM 授权",
            )
        from market_validator.hypothesis.service import (
            HypothesisProposalService,
        )
        from market_validator.hypothesis.serialization import (
            serialize_research_hypothesis_proposal,
        )

        try:
            service = HypothesisProposalService(
                config.proposal_backend,
                expected_backend=config.expected_backend or "",
                expected_model=config.expected_model or "",
            )
            generated = service.generate(session.original_question)
        except Exception as error:
            action = NextAction(
                action_type="blocked",
                title="假设草案生成失败",
                message="LLM 服务不可用或返回结构不合法。",
                why_needed="无法从自然语言生成结构化假设",
                source_error_code=getattr(error, "code", None),
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                last_error="proposal generation failed",
                history="假设草案生成失败",
            )
        payload = serialize_research_hypothesis_proposal(generated.proposal)
        _write_artifact(config, "hypothesis-proposal.json", payload)
        session = _append_artifact(
            config,
            session,
            "hypothesis_proposal",
            "hypothesis-proposal.json",
            payload,
            generated.proposal.proposal_schema_version,
        )
        return _commit(
            config, session,
            history="假设草案已生成",
            artifacts=session.artifact_references,
        )
    return _stage_hypothesis_review(config, session, proposal)


def _stage_hypothesis_review(
    config: AgentConfig, session: ResearchSession, proposal
) -> ResearchSession:
    from market_validator.hypothesis.clarification import (
        proposal_ambiguity_references,
    )

    ambiguities = proposal_ambiguity_references(proposal)
    if ambiguities:
        action = NextAction(
            action_type="answer_hypothesis_questions",
            title="需要澄清假设中的歧义",
            message="\n".join(
                f"- {item.text}" for item in ambiguities
            ),
            why_needed="歧义必须由用户决定后才能编译 ResearchSpec",
            required_inputs=["clarification_answers_path"],
            artifact_summary={
                "proposal": session.original_question,
            },
        )
        return _commit(
            config, session, stage="hypothesis_clarification",
            status="needs_user", next_action=action,
            history="等待用户澄清",
        )
    if not proposal.ready_for_spec_review:
        action = NextAction(
            action_type="complete_research_spec",
            title="需要补全 ResearchSpec 输入",
            message="提案缺少执行级定义，需要用户提供补全答案。",
            why_needed="research_spec_inputs 不完整无法编译 ResearchSpec",
            required_inputs=["completion_answers_path"],
        )
        return _commit(
            config, session, stage="research_spec",
            status="needs_user", next_action=action,
            history="等待 ResearchSpec 补全",
        )
    action = NextAction(
        action_type="confirm_hypothesis",
        title="请确认研究假设提案",
        message=(
            f"归一化问题：{proposal.normalized_research_question}\n"
            f"假设：{proposal.statistical_hypothesis.model_dump(mode='json')}\n"
            "输入 confirm 确认，输入 cancel 停止。"
        ),
        why_needed="确认后才可编译 ResearchSpec",
        required_inputs=["confirm|cancel"],
    )
    return _commit(
        config, session, stage="hypothesis_confirmation",
        status="needs_user", next_action=action,
        history="等待假设确认",
    )


def _stage_hypothesis_confirmation(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    confirmation = _load_artifact(
        config, session, "hypothesis_confirmation"
    )
    if confirmation is None:
        action = NextAction(
            action_type="confirm_hypothesis",
            title="请确认研究假设提案",
            message="输入 confirm 确认，输入 cancel 停止。",
            why_needed="确认后才可编译 ResearchSpec",
            required_inputs=["confirm|cancel"],
        )
        return _commit(
            config, session, status="needs_user", next_action=action,
            history="等待假设确认",
        )
    return _stage_compile_spec(config, session)


def _stage_compile_spec(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    proposal = _load_artifact(config, session, "hypothesis_proposal")
    confirmation = _load_artifact(
        config, session, "hypothesis_confirmation"
    )
    if proposal is None or confirmation is None:
        fail_agent(
            ResearchAgentErrorCode.AGENT_STATE_INVALID,
            "hypothesis artifacts are missing",
        )
    from market_validator.hypothesis.research_spec_compiler import (
        compile_confirmed_research_spec,
    )
    from market_validator.research.serialization import (
        serialize_research_spec,
    )

    try:
        compiled = compile_confirmed_research_spec(proposal, confirmation)
    except Exception as error:
        action = NextAction(
            action_type="blocked",
            title="ResearchSpec 编译失败",
            message="确认后的提案无法编译为 ResearchSpec。",
            why_needed="编译失败阻断后续流程",
            source_error_code=getattr(error, "code", None),
        )
        return _commit(
            config, session, status="blocked", next_action=action,
            last_error="spec compilation failed",
            history="ResearchSpec 编译失败",
        )
    payload = serialize_research_spec(compiled.research_spec)
    _write_artifact(config, "research-spec.json", payload)
    session = _append_artifact(
        config, session, "research_spec", "research-spec.json", payload, "1.0"
    )
    return _stage_data_plan(config, session)


def _stage_data_plan(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    data_plan = _load_artifact(config, session, "data_plan")
    if data_plan is None:
        spec = _load_artifact(config, session, "research_spec")
        if spec is None:
            fail_agent(
                ResearchAgentErrorCode.AGENT_STATE_INVALID,
                "research spec artifact is missing",
            )
        from market_validator.data.data_plan_review import (
            generate_data_plan,
            persist_generated_data_plan,
        )

        try:
            generated = generate_data_plan(
                spec, _instrument_registry(config), _calendar_registry(config)
            )
        except Exception as error:
            action = NextAction(
                action_type="blocked",
                title="DataPlan 生成失败",
                message="存在未注册的 instrument 或无法映射的变量。",
                why_needed="数据计划无法生成",
                source_error_code=getattr(error, "code", None),
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                last_error="data plan generation failed",
                history="DataPlan 生成失败",
            )
        persist_generated_data_plan(
            generated,
            _artifacts_dir(config) / "data-plan.json",
        )
        import json as _json

        (_artifacts_dir(config) / "data-plan-generated.json").write_text(
            _json.dumps(
                generated.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        payload = (_artifacts_dir(config) / "data-plan.json").read_bytes()
        session = _append_artifact(
            config, session, "data_plan", "data-plan.json", payload, "1.0"
        )
        return _commit(
            config, session, stage="data_plan",
            history="DataPlan 已生成",
            artifacts=session.artifact_references,
        )
    return _stage_data_plan_confirmation(config, session)


def _stage_data_plan_confirmation(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    confirmation = _load_artifact(
        config, session, "data_plan_confirmation"
    )
    if confirmation is None:
        data_plan = _load_artifact(config, session, "data_plan")
        lines = []
        for requirement in data_plan.requirements:
            lines.append(
                f"- {requirement.variable_id}: {requirement.instrument_id} "
                f"{requirement.field} {requirement.frequency} "
                f"{requirement.start_date}..{requirement.end_date} "
                f"lag={requirement.lag_periods} "
                f"pre_sample={requirement.required_pre_sample_periods}"
            )
        action = NextAction(
            action_type="confirm_data_plan",
            title="请确认数据计划",
            message="\n".join(lines),
            why_needed="数据计划确认后才可选择数据源",
            required_inputs=["confirm|cancel"],
        )
        return _commit(
            config, session, stage="data_plan_confirmation",
            status="needs_user", next_action=action,
            history="等待数据计划确认",
        )
    return _stage_source_selection(config, session)


def _stage_source_selection(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    selection = _load_artifact(config, session, "source_selection")
    if selection is None:
        from market_validator.data.source_selection import (
            SelectedProviderSource,
        )

        registry = _instrument_registry(config)
        data_plan = _load_artifact(config, session, "data_plan")
        candidates: list[SelectedProviderSource] = []
        for requirement in data_plan.requirements:
            entry = registry.get(requirement.instrument_id)
            if entry is None:
                continue
            for mapping in entry.provider_mappings:
                if mapping.verified:
                    candidates.append(
                        SelectedProviderSource(
                            requirement_id=requirement.requirement_id,
                            variable_id=requirement.variable_id,
                            instrument_id=requirement.instrument_id,
                            provider_id=mapping.provider_id,
                            provider_symbol=mapping.provider_symbol,
                            dataset_or_endpoint=mapping.dataset_or_endpoint,
                            market=entry.market,
                            mapping_verified_on=mapping.verified_on,
                            mapping_verification_source_uri=(
                                mapping.verification_source_uri
                            ),
                        )
                    )
        if not candidates:
            return _configure_data_interface(
                config, session, stage="source_selection",
                history="缺少数据源映射",
            )
        choices = [
            (
                f"{index}: {item.provider_id} {item.provider_symbol} "
                f"{item.dataset_or_endpoint} (verified "
                f"{item.mapping_verified_on})"
            )
            for index, item in enumerate(candidates)
        ]
        action = NextAction(
            action_type="choose_data_source",
            title="请选择数据源",
            message="\n".join(choices),
            why_needed="每个变量必须选择一个已验证来源",
            required_inputs=["choice_index"],
            available_choices=[str(index) for index in range(len(candidates))],
        )
        return _commit(
            config, session, status="needs_user", next_action=action,
            history="等待数据源选择",
        )
    return _stage_source_selection_confirmation(config, session)


def _stage_source_selection_confirmation(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    confirmation = _load_artifact(
        config, session, "source_selection_confirmation"
    )
    if confirmation is None:
        action = NextAction(
            action_type="confirm_source_selection",
            title="请确认数据源选择",
            message="输入 confirm 确认所选数据源，输入 cancel 停止。",
            why_needed="确认后才可规划获取请求",
            required_inputs=["confirm|cancel"],
        )
        return _commit(
            config, session, stage="source_selection_confirmation",
            status="needs_user", next_action=action,
            history="等待数据源确认",
        )
    return _stage_acquisition_planning(config, session)


def _stage_acquisition_planning(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    plan = _load_artifact(config, session, "acquisition_request_plan")
    if plan is None:
        from market_validator.data.acquisition_request import (
            generate_acquisition_request_plan,
        )

        selection = _load_generated_source_selection(config, session)
        selection_confirmation = _load_artifact(
            config, session, "source_selection_confirmation"
        )
        data_plan = _load_artifact(config, session, "data_plan")
        capability_snapshots = _capability_snapshots(config)
        try:
            generated = generate_acquisition_request_plan(
                selection,
                selection_confirmation,
                data_plan,
                _instrument_registry(config),
                _calendar_registry(config),
                capability_snapshots,
                session_adapters=config.session_adapters or None,
                public_parameter_templates=(
                    config.public_parameter_templates or None
                ),
            )
        except Exception as error:
            action = NextAction(
                action_type="blocked",
                title="获取请求规划失败",
                message="获取请求无法生成（pre-sample 或参数模板缺失）。",
                why_needed="无法规划数据获取",
                source_error_code=getattr(error, "code", None),
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                last_error="acquisition planning failed",
                history="获取请求规划失败",
            )
        from market_validator.data.acquisition_request import (
            serialize_acquisition_request_plan,
        )

        payload = serialize_acquisition_request_plan(
            generated.acquisition_request_plan
        )
        import json as _json

        (_artifacts_dir(config) / "acquisition-request-plan-generated.json").write_text(
            _json.dumps(
                generated.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        _write_artifact(config, "acquisition-request-plan.json", payload)
        session = _append_artifact(
            config, session, "acquisition_request_plan",
            "acquisition-request-plan.json", payload, "1.2",
        )
        return _commit(
            config, session, stage="acquisition_planning",
            history="获取请求已规划",
            artifacts=session.artifact_references,
        )
    return _stage_data_access_authorization(config, session)


def _stage_data_access_authorization(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    authorization = _load_artifact(
        config, session, "data_access_authorization"
    )
    if authorization is None:
        action = NextAction(
            action_type="authorize_data_access",
            title="请授权一次数据获取",
            message=(
                "将创建 single-use DataAccessAuthorization 并执行一次获取。"
                "输入 confirm 授权，输入 cancel 停止。"
            ),
            why_needed="数据获取必须显式授权",
            required_inputs=["confirm|cancel"],
        )
        return _commit(
            config, session, stage="data_access_authorization",
            status="needs_user", next_action=action,
            history="等待数据获取授权",
        )
    return _stage_data_acquisition(config, session)


def _stage_data_acquisition(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    snapshot = _load_artifact(config, session, "acquisition_snapshot")
    if snapshot is None:
        from market_validator.data.execution import (
            execute_authorized_acquisition,
        )

        plan = _load_generated_acquisition_request_plan(config, session)
        (_workspace(config) / "snapshots").mkdir(parents=True, exist_ok=True)
        authorization = _load_artifact(
            config, session, "data_access_authorization"
        )
        if authorization is None:
            fail_agent(
                ResearchAgentErrorCode.AGENT_STATE_INVALID,
                "data access authorization is missing",
            )
        data_plan = _load_artifact(config, session, "data_plan")
        capability_snapshots = _capability_snapshots(config)
        providers = _providers(config)
        network_needed = any(
            request.access_mode == "network"
            for request in plan.acquisition_request_plan.requests
        )
        if network_needed and not config.allow_data_network:
            action = NextAction(
                action_type="blocked",
                title="需要数据网络授权",
                message=(
                    "该数据源需要网络访问（FRED）。请使用 "
                    "--allow-data-network 重新启动。"
                ),
                why_needed="Provider 网络请求必须显式授权",
                required_inputs=["--allow-data-network"],
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                history="等待数据网络授权",
            )
        try:
            snapshot = execute_authorized_acquisition(
                generated_plan=plan,
                authorization=authorization,
                data_plan=data_plan,
                instrument_registry=_instrument_registry(config),
                calendar_registry=_calendar_registry(config),
                capability_snapshots=capability_snapshots,
                attempt_id=ACQUISITION_ATTEMPT_ID.format(
                    session_id=session.session_id
                ),
                receipt_path=_workspace(config) / "receipt.json",
                snapshot_root=_workspace(config) / "snapshots",
                adapters=_execution_adapters(config, providers),
            )
        except Exception as error:
            action = NextAction(
                action_type="blocked",
                title="数据获取失败",
                message="数据获取执行失败，授权已消费，不会自动重试。",
                why_needed="需要用户决定是否重新创建授权",
                source_error_code=getattr(error, "code", None),
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                last_error="data acquisition failed",
                history="数据获取失败",
            )
        payload = (
            _workspace(config)
            / "snapshots"
            / snapshot.snapshot_id
            / "manifest.json"
        ).read_bytes()
        _write_artifact(config, "acquisition-snapshot.json", payload)
        session = _append_artifact(
            config, session, "acquisition_snapshot",
            "acquisition-snapshot.json", payload, "1.0",
        )
        return _commit(
            config, session, stage="data_acquisition",
            history="数据获取完成",
            artifacts=session.artifact_references,
        )
    return _stage_data_readiness(config, session)


def _stage_data_readiness(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    manifest = _load_artifact(config, session, "data_ready_manifest")
    if manifest is None:
        from market_validator.data.readiness import (
            assess_data_readiness,
            create_data_ready_manifest,
        )

        plan = _load_generated_acquisition_request_plan(config, session)
        data_plan = _load_artifact(config, session, "data_plan")
        snapshot = _load_artifact(config, session, "acquisition_snapshot")
        authorization = _load_artifact(
            config, session, "data_access_authorization"
        )
        try:
            from market_validator.data.access_authorization import (
                calculate_data_access_authorization_sha256,
                calculate_data_access_authorization_receipt_sha256,
                parse_data_access_authorization_receipt,
            )

            authorization_sha256 = (
                calculate_data_access_authorization_sha256(authorization)
            )
            receipt_payload = (
                _workspace(config) / "receipt.json"
            ).read_bytes()
            receipt_sha256 = (
                calculate_data_access_authorization_receipt_sha256(
                    parse_data_access_authorization_receipt(
                        receipt_payload
                    )
                )
            )
        except Exception:
            fail_agent(
                ResearchAgentErrorCode.AGENT_STATE_INVALID,
                "data access authorization or receipt is missing",
            )
        try:
            assessment = assess_data_readiness(
                generated_plan=plan,
                data_plan=data_plan,
                instrument_registry=_instrument_registry(config),
                calendar_registry=_calendar_registry(config),
                snapshot_path=(
                    _workspace(config)
                    / "snapshots"
                    / snapshot.snapshot_id
                ),
                session_schedules=config.schedule_snapshots,
                expected_authorization_sha256=authorization_sha256,
                expected_receipt_sha256=receipt_sha256,
            )
        except Exception as error:
            action = NextAction(
                action_type="blocked",
                title="数据就绪评估失败",
                message="Data Ready 评估无法执行（可能需要 session schedule 数据）。",
                why_needed="readiness 需要已验证的日历 session 数据",
                source_error_code=getattr(error, "code", None),
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                last_error="readiness failed",
                history="数据就绪评估失败",
            )
        blockers = [
            f"{item.code}: {item.message}" for item in assessment.blockers
        ]
        if assessment.status != "ready" or blockers:
            action = NextAction(
                action_type="blocked",
                title="数据未达到 Ready",
                message=describe_blockers(assessment),
                why_needed="blockers 必须解决才能继续",
                source_error_code=blockers[0].split(":")[0]
                if blockers
                else None,
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                last_error="data readiness blocked",
                history="数据未达到 Ready",
            )
        try:
            generated_manifest = create_data_ready_manifest(
                assessment=assessment, generated_plan=plan
            )
        except Exception as error:
            action = NextAction(
                action_type="blocked",
                title="Data Ready 清单生成失败",
                message="无法生成 DataReadyManifest。",
                why_needed="manifest 是后续分析的绑定基础",
                source_error_code=getattr(error, "code", None),
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                last_error="manifest failed",
                history="DataReadyManifest 失败",
            )
        from market_validator.data.readiness import (
            persist_data_ready_manifest,
            persist_data_readiness_assessment,
            serialize_data_readiness_assessment,
        )

        persist_data_readiness_assessment(
            assessment,
            _artifacts_dir(config) / "readiness-assessment.json",
        )
        assessment_payload = (
            _artifacts_dir(config) / "readiness-assessment.json"
        ).read_bytes()
        session = _append_artifact(
            config, session, "readiness_assessment",
            "readiness-assessment.json", assessment_payload, "1.1",
        )
        persist_data_ready_manifest(
            generated_manifest,
            _artifacts_dir(config) / "data-ready-manifest.json",
        )
        payload = (_artifacts_dir(config) / "data-ready-manifest.json").read_bytes()
        session = _append_artifact(
            config, session, "data_ready_manifest",
            "data-ready-manifest.json", payload, "1.1",
        )
        return _commit(
            config, session, stage="data_readiness",
            history="Data Ready 完成",
            artifacts=session.artifact_references,
        )
    return _stage_analysis_planning(config, session)


def _stage_analysis_planning(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    plan = _load_artifact(config, session, "analysis_plan")
    if plan is None:
        from market_validator.analysis.planning import (
            AnalysisPlanDecisions,
            TransformationDecision,
            TRANSFORMATION_PROFILE_LEVEL,
            METHOD_PROFILE_OLS_HC1,
            METHOD_PROFILE_PEARSON,
            METHOD_PROFILE_SPEARMAN,
        )
        from market_validator.analysis.planning_generator import (
            generate_analysis_plan,
        )
        from market_validator.research.enums import ModelMethod

        spec = _load_artifact(config, session, "research_spec")
        readiness_assessment = _load_artifact(
            config, session, "readiness_assessment"
        )
        data_ready_manifest = _load_artifact(
            config, session, "data_ready_manifest"
        )
        plan_ = _load_generated_acquisition_request_plan(config, session)
        data_plan = _load_artifact(config, session, "data_plan")
        method = spec.model.method
        if method in (
            ModelMethod.PEARSON_CORRELATION,
            ModelMethod.SPEARMAN_CORRELATION,
        ):
            profile = (
                METHOD_PROFILE_PEARSON
                if method is ModelMethod.PEARSON_CORRELATION
                else METHOD_PROFILE_SPEARMAN
            )
            include_intercept = None
            covariance = None
            max_lags = None
        else:
            profile = METHOD_PROFILE_OLS_HC1
            include_intercept = True
            covariance = "hc1"
            max_lags = None
        transformation_decisions = {}
        for variable in [spec.outcome] + spec.predictors + spec.controls:
            transformation_decisions[variable.variable_id] = (
                TransformationDecision(
                    variable_id=variable.variable_id,
                    profile=TRANSFORMATION_PROFILE_LEVEL,
                    lag_periods=variable.lag_periods,
                    availability_lag_periods=(
                        variable.availability_lag_periods
                    ),
                    required_pre_sample_periods=0,
                    rolling_window_periods=None,
                )
            )
        decisions = AnalysisPlanDecisions(
            method_profile=profile,
            primary_test_variable_ids=(
                [spec.predictors[0].variable_id]
                if method
                in (
                    ModelMethod.PEARSON_CORRELATION,
                    ModelMethod.SPEARMAN_CORRELATION,
                )
                else [v.variable_id for v in spec.predictors]
            ),
            include_intercept=include_intercept,
            covariance_estimator=covariance,
            newey_west_max_lags=max_lags,
            same_market_join_policy="strict_same_session_v1",
            same_market_missing_data_policy="drop_observation_v1",
            transformation_decisions=transformation_decisions,
        )
        try:
            generated = generate_analysis_plan(
                research_spec=spec,
                readiness_assessment=readiness_assessment,
                data_ready_manifest=data_ready_manifest,
                generated_plan=plan_,
                data_plan=data_plan,
                instrument_registry=_instrument_registry(config),
                calendar_registry=_calendar_registry(config),
                decisions=decisions,
            )
        except Exception as error:
            action = NextAction(
                action_type="blocked",
                title="AnalysisPlan 生成失败",
                message="无法从 Data Ready 生成可执行分析计划。",
                why_needed="分析计划无法生成",
                source_error_code=getattr(error, "code", None),
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                last_error="analysis planning failed",
                history="AnalysisPlan 失败",
            )
        from market_validator.analysis.planning import (
            serialize_analysis_plan,
        )

        payload = serialize_analysis_plan(generated.analysis_plan)
        _write_artifact(config, "analysis-plan.json", payload)
        session = _append_artifact(
            config, session, "analysis_plan",
            "analysis-plan.json", payload, "1.0",
        )
        return _commit(
            config, session, stage="analysis_planning",
            history="AnalysisPlan 建议已生成",
            artifacts=session.artifact_references,
        )
    return _stage_analysis_confirmation(config, session)


def _stage_analysis_confirmation(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    confirmation = _load_artifact(
        config, session, "analysis_plan_confirmation"
    )
    if confirmation is None:
        action = NextAction(
            action_type="authorize_analysis_plan",
            title="请确认 AnalysisPlan",
            message=(
                "确认的是最终 AnalysisPlan（非 Agent 解释文本）。"
                "输入 confirm 确认，输入 cancel 停止。"
            ),
            why_needed="只有用户确认的分析计划才能被授权执行",
            required_inputs=["confirm|cancel"],
        )
        return _commit(
            config, session, stage="analysis_confirmation",
            status="needs_user", next_action=action,
            history="等待 AnalysisPlan 确认",
        )
    return _stage_analysis_authorization(config, session)


def _stage_analysis_authorization(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    authorization = _load_artifact(
        config, session, "analysis_authorization"
    )
    if authorization is None:
        action = NextAction(
            action_type="execute_analysis",
            title="请授权执行分析",
            message=(
                "将创建 single-use AnalysisAuthorization 并执行一次"
                "确定性统计。输入 confirm 授权，输入 cancel 停止。"
            ),
            why_needed="统计执行必须显式授权",
            required_inputs=["confirm|cancel"],
        )
        return _commit(
            config, session, stage="analysis_authorization",
            status="needs_user", next_action=action,
            history="等待分析授权",
        )
    return _stage_analysis_execution(config, session)


def _stage_analysis_execution(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    try:
        result = _load_artifact(config, session, "analysis_result")
    except Exception:
        if (
            _workspace(config) / "analysis-receipt.json"
        ).is_file():
            action = NextAction(
                action_type="blocked",
                title="分析结果缺失且授权已消费",
                message=(
                    "single-use 分析授权已被消费，但结果文件缺失；"
                    "不会自动重试，需要用户重新确认并创建新的授权。"
                ),
                why_needed="授权已消费，缺失结果无法恢复",
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                last_error="analysis result missing after consumption",
                history="分析结果缺失",
            )
        raise
    if result is None:
        from market_validator.analysis.authorization import (
            confirm_analysis_plan,
            create_analysis_authorization,
        )
        from market_validator.analysis.execution import (
            execute_authorized_analysis,
        )

        plan = _load_artifact(config, session, "analysis_plan")
        confirmation = _load_artifact(
            config, session, "analysis_plan_confirmation"
        )
        authorization = _load_artifact(
            config, session, "analysis_authorization"
        )
        spec = _load_artifact(config, session, "research_spec")
        assessment = _load_artifact(
            config, session, "readiness_assessment"
        )
        manifest = _load_artifact(config, session, "data_ready_manifest")
        acq_plan = _load_generated_acquisition_request_plan(
            config, session
        )
        data_plan = _load_artifact(config, session, "data_plan")
        snapshot_candidates = [
            item
            for item in (_workspace(config) / "snapshots").iterdir()
            if item.is_dir()
        ]
        if len(snapshot_candidates) != 1:
            fail_agent(
                ResearchAgentErrorCode.AGENT_STATE_INVALID,
                "the workspace must contain exactly one snapshot directory",
            )
        snapshot_dir = snapshot_candidates[0]
        try:
            receipt, outcome = execute_authorized_analysis(
                plan=plan,
                confirmation=confirmation,
                authorization=authorization,
                research_spec=spec,
                readiness_assessment=assessment,
                data_ready_manifest=manifest,
                generated_plan=acq_plan,
                data_plan=data_plan,
                instrument_registry=_instrument_registry(config),
                calendar_registry=_calendar_registry(config),
                schedule_snapshots=config.schedule_snapshots,
                snapshot_path=snapshot_dir,
                attempt_id=ANALYSIS_ATTEMPT_ID.format(
                    session_id=session.session_id
                ),
                receipt_path=_workspace(config) / "analysis-receipt.json",
                result_root=_workspace(config),
                consumed_at=config.now(),
                outcome_created_at=config.now(),
            )
        except Exception as error:
            action = NextAction(
                action_type="blocked",
                title="分析执行失败",
                message=(
                    "分析执行失败；authorization 已消费，不会自动重试。"
                ),
                why_needed="需要用户决定是否重新确认并创建新授权",
                source_error_code=getattr(error, "code", None),
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                last_error="analysis execution failed",
                history="分析执行失败",
            )
        from market_validator.analysis.execution import (
            verify_persisted_analysis_execution,
        )
        from market_validator.analysis.execution_models import (
            parse_analysis_result,
        )

        verify_persisted_analysis_execution(
            _workspace(config),
            ANALYSIS_ATTEMPT_ID.format(session_id=session.session_id),
        )
        result = parse_analysis_result(
            (
                _workspace(config)
                / "analysis-runs"
                / ANALYSIS_ATTEMPT_ID.format(session_id=session.session_id)
                / "analysis-result.json"
            ).read_bytes()
        )
        payload = result.model_dump_json().encode("utf-8")
        _write_artifact(config, "analysis-result.json", payload)
        session = _append_artifact(
            config, session, "analysis_result",
            "analysis-result.json", payload, "1.0",
        )
        return _commit(
            config, session, stage="analysis_execution",
            history="分析执行完成",
            artifacts=session.artifact_references,
        )
    return _stage_interpretation(config, session)


def _stage_interpretation(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    report = _load_artifact(config, session, "validation_report")
    if report is None:
        if config.interpretation_client is None:
            action = NextAction(
                action_type="configure_interpretation_llm",
                title="需要解释 LLM 配置",
                message=(
                    "AnalysisResult 已完成；需要 LLM 配置才能生成自然语言"
                    "解释。可提供 endpoint/model/api key（仅内存），"
                    "或接受无解释的统计结果。"
                ),
                why_needed="解释需要 LLM（或显式选择不生成解释）",
                required_inputs=["interpretation_llm 配置"],
            )
            return _commit(
                config, session, stage="interpretation",
                status="needs_user", next_action=action,
                history="等待解释配置",
            )
        from market_validator.interpretation import (
            interpret_analysis_result,
        )

        spec = _load_artifact(config, session, "research_spec")
        plan = _load_artifact(config, session, "analysis_plan")
        result = _load_artifact(config, session, "analysis_result")
        manifest = _load_artifact(config, session, "data_ready_manifest")
        try:
            outcome = interpret_analysis_result(
                research_spec=spec,
                analysis_plan=plan,
                analysis_result=result,
                data_ready_manifest_sha256=(
                    result.data_ready_manifest_sha256
                ),
                llm_client=config.interpretation_client,
                language=config.language,
                style="concise",
                model_identifier=config.interpretation_model,
                result_root=_workspace(config),
                created_at=config.now(),
            )
        except Exception as error:
            action = NextAction(
                action_type="blocked",
                title="结果解释失败",
                message="LLM 解释生成失败；统计结果仍然有效。",
                why_needed="解释失败不阻塞统计结果",
                source_error_code=getattr(error, "code", None),
            )
            return _commit(
                config, session, status="blocked", next_action=action,
                last_error="interpretation failed",
                history="解释失败",
            )
        report_bytes = outcome["report"]
        report_path = _workspace(config) / "final" / "validation-report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        if report_path.exists():
            if report_path.read_bytes() != report_bytes:
                fail_agent(
                    ResearchAgentErrorCode.AGENT_OUTPUT_CONFLICT,
                    "final report already exists with different content",
                )
        else:
            report_path.write_bytes(report_bytes)
        payload = report_bytes
        _write_artifact(config, "validation-report.md", payload)
        session = _append_artifact(
            config, session, "validation_report",
            "validation-report.md", payload, "1.0",
        )
        completed = _commit(
            config, session, stage="completed", status="completed",
            next_action=NextAction(
                action_type="review_result",
                title="研究完成",
                message=f"最终报告：{report_path}",
                why_needed="报告已生成",
            ),
            completed_report=str(report_path),
            history="解释与报告完成",
            artifacts=session.artifact_references,
        )
        return completed
    action = NextAction(
        action_type="review_result",
        title="研究完成",
        message="最终报告已生成，可查看 final/validation-report.md。",
        why_needed="研究流程已结束",
    )
    return _commit(
        config, session, status="completed", next_action=action,
        history="研究完成",
    )


# ---------------------------------------------------------------------------
# Registry / provider helpers
# ---------------------------------------------------------------------------

def _instrument_registry(config: AgentConfig):
    from market_validator.data.registry import InstrumentRegistry

    return InstrumentRegistry.from_json_file(
        _workspace(config) / "config" / "instruments.json",
        _calendar_registry(config),
    )


def _calendar_registry(config: AgentConfig):
    from market_validator.data.calendars import CalendarRegistry

    return CalendarRegistry.from_json_file(
        _workspace(config) / "config" / "calendars.json"
    )


def _providers(config: AgentConfig) -> dict[str, object]:
    if config.providers:
        return config.providers
    return {}


def _capability_snapshots(config: AgentConfig) -> dict[str, object]:
    from market_validator.data.acquisition_request import (
        snapshot_provider_capabilities,
    )

    return {
        provider_id: snapshot_provider_capabilities(provider.capabilities())
        for provider_id, provider in _providers(config).items()
    }


def _execution_adapters(config: AgentConfig, providers: dict[str, object]):
    from market_validator.data.execution import (
        FredExecutionAdapter,
        LocalFileExecutionAdapter,
    )

    adapters = {}
    for provider_id, provider in providers.items():
        if provider_id == "fred":
            adapters[provider_id] = FredExecutionAdapter(provider)
        elif provider_id == "local_csv":
            adapters[provider_id] = LocalFileExecutionAdapter(provider)
    return adapters


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_research_session(
    *,
    question: str,
    config: AgentConfig,
) -> ResearchSession:
    session = _new_session(config, question=question, parent_session_id=None)
    persist_session_revision(session, config.workspace)
    return advance_until_blocked(config, session)


def load_latest_research_session_public(
    workspace: str,
) -> ResearchSession:
    return load_latest_research_session(workspace)


def advance_research_session(
    config: AgentConfig,
    session: ResearchSession,
) -> ResearchSession:
    """Advance one deterministic step; stop at a user boundary."""
    stage = session.current_stage
    if stage == "completed":
        return _stage_completed(config, session)
    handlers = {
        "hypothesis_proposal": _stage_hypothesis_proposal,
        "hypothesis_clarification": _stage_hypothesis_review_wrapper,
        "research_spec": _stage_hypothesis_review_wrapper,
        "hypothesis_confirmation": _stage_hypothesis_confirmation,
        "data_plan": _stage_data_plan,
        "data_plan_confirmation": _stage_data_plan_confirmation,
        "source_selection": _stage_source_selection,
        "source_selection_confirmation": (
            _stage_source_selection_confirmation
        ),
        "acquisition_planning": _stage_acquisition_planning,
        "data_access_authorization": _stage_data_access_authorization,
        "data_acquisition": _stage_data_acquisition,
        "data_readiness": _stage_data_readiness,
        "analysis_planning": _stage_analysis_planning,
        "analysis_confirmation": _stage_analysis_confirmation,
        "analysis_authorization": _stage_analysis_authorization,
        "analysis_execution": _stage_analysis_execution,
        "interpretation": _stage_interpretation,
    }
    handler = handlers.get(stage)
    if handler is None:
        fail_agent(
            ResearchAgentErrorCode.AGENT_STATE_INVALID,
            f"unknown stage {stage}",
        )
    return handler(config, session)


def _stage_hypothesis_review_wrapper(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    proposal = _load_artifact(config, session, "hypothesis_proposal")
    if proposal is None:
        return _stage_hypothesis_proposal(config, session)
    return _stage_hypothesis_review(config, session, proposal)


def _stage_completed(
    config: AgentConfig, session: ResearchSession
) -> ResearchSession:
    action = NextAction(
        action_type="review_result",
        title="研究完成",
        message=(
            session.completed_report
            or "最终报告已生成，可查看 final/validation-report.md。"
        ),
        why_needed="研究流程已结束",
    )
    if session.current_next_action is not None:
        return session
    return _commit(
        config, session, status="completed", next_action=action,
    )


def advance_until_blocked(
    config: AgentConfig,
    session: ResearchSession,
    *,
    max_transitions: int = MAX_TRANSITIONS,
) -> ResearchSession:
    """Advance deterministically until a user boundary or a blocker."""
    current = session
    for _ in range(max_transitions):
        if current.status in ("needs_user", "blocked", "completed", "failed"):
            return current
        advanced = advance_research_session(config, current)
        if advanced.revision == current.revision:
            return advanced
        current = advanced
    fail_agent(
        ResearchAgentErrorCode.AGENT_TRANSITION_LIMIT,
        "the agent exceeded the maximum transition count",
    )


def apply_research_agent_response(
    config: AgentConfig,
    session: ResearchSession,
    *,
    action_type: str,
    payload: dict[str, object],
) -> ResearchSession:
    """Apply one user response and advance until the next boundary."""
    next_action = session.current_next_action
    if next_action is None or next_action.action_type != action_type:
        fail_agent(
            ResearchAgentErrorCode.AGENT_INPUT_REQUIRED,
            "the response does not match the current next action",
        )
    current = _apply_response(config, session, action_type, payload)
    return advance_until_blocked(config, current)


def _apply_response(
    config: AgentConfig,
    session: ResearchSession,
    action_type: str,
    payload: dict[str, object],
) -> ResearchSession:
    if action_type == "request_llm_access":
        if config.proposal_backend is not None:
            return _commit(
                config, session, status="active", history="LLM 访问已授权",
            )
        proposal_path = payload.get("proposal_path")
        if not isinstance(proposal_path, str):
            fail_agent(
                ResearchAgentErrorCode.AGENT_INPUT_REQUIRED,
                "a proposal path is required",
            )
        from market_validator.hypothesis.serialization import (
            parse_research_hypothesis_proposal,
        )

        path = Path(proposal_path)
        payload_bytes = path.read_bytes()
        proposal = parse_research_hypothesis_proposal(payload_bytes)
        _write_artifact(
            config, "hypothesis-proposal.json", payload_bytes
        )
        session = _append_artifact(
            config, session, "hypothesis_proposal",
            "hypothesis-proposal.json", payload_bytes,
            proposal.proposal_schema_version,
        )
        return _commit(
            config, session, status="active", history="假设草案已导入",
            artifacts=session.artifact_references,
        )
    if action_type == "answer_hypothesis_questions":
        answers_path = payload.get("answers_path")
        if not isinstance(answers_path, str):
            fail_agent(
                ResearchAgentErrorCode.AGENT_INPUT_REQUIRED,
                "a clarification answers path is required",
            )
        from market_validator.hypothesis.clarification import (
            apply_clarification_answers,
            parse_clarification_answers,
        )
        from market_validator.hypothesis.serialization import (
            serialize_research_hypothesis_proposal,
        )

        proposal = _load_artifact(config, session, "hypothesis_proposal")
        answers = parse_clarification_answers(
            Path(answers_path).read_bytes()
        )
        applied = apply_clarification_answers(proposal, answers)
        payload_bytes = serialize_research_hypothesis_proposal(
            applied.proposal
        )
        _write_artifact(config, "clarified-proposal.json", payload_bytes)
        session = _replace_proposal_reference(
            config, session, "clarified-proposal.json", payload_bytes
        )
        return _commit(
            config, session, status="active", history="澄清已应用",
            artifacts=session.artifact_references,
        )
    if action_type == "complete_research_spec":
        answers_path = payload.get("answers_path")
        if not isinstance(answers_path, str):
            fail_agent(
                ResearchAgentErrorCode.AGENT_INPUT_REQUIRED,
                "a completion answers path is required",
            )
        from market_validator.hypothesis.completion import (
            apply_research_spec_completion_answers,
            parse_research_spec_completion_answers,
        )
        from market_validator.hypothesis.serialization import (
            serialize_research_hypothesis_proposal,
        )

        proposal = _load_artifact(config, session, "hypothesis_proposal")
        answers = parse_research_spec_completion_answers(
            Path(answers_path).read_bytes()
        )
        applied = apply_research_spec_completion_answers(proposal, answers)
        payload_bytes = serialize_research_hypothesis_proposal(
            applied.proposal
        )
        _write_artifact(config, "completed-proposal.json", payload_bytes)
        session = _replace_proposal_reference(
            config, session, "completed-proposal.json", payload_bytes
        )
        return _commit(
            config, session, status="active",
            history="ResearchSpec 补全已应用",
            artifacts=session.artifact_references,
        )
    if action_type in (
        "confirm_hypothesis",
        "confirm_data_plan",
        "confirm_source_selection",
        "authorize_data_access",
        "authorize_analysis_plan",
        "execute_analysis",
    ):
        answer = payload.get("answer")
        if answer not in ("confirm", "cancel"):
            fail_agent(
                ResearchAgentErrorCode.AGENT_INPUT_REQUIRED,
                "answer must be exactly confirm or cancel",
            )
        if answer == "cancel":
            return _commit(
                config, session, status="blocked",
                next_action=NextAction(
                    action_type="blocked",
                    title="用户取消",
                    message="用户取消了当前操作。",
                    why_needed="用户决定停止",
                ),
                history="用户取消",
            )
        return _confirm_stage(config, session, action_type)
    if action_type == "choose_data_source":
        index = payload.get("choice_index")
        try:
            choice = int(index)
        except (TypeError, ValueError):
            fail_agent(
                ResearchAgentErrorCode.AGENT_INPUT_REQUIRED,
                "a numeric choice index is required",
            )
        return _apply_source_choice(config, session, choice)
    if action_type == "configure_interpretation_llm":
        # interpretation client must be provided through AgentConfig by the
        # caller; here we only record that the configuration is expected
        if config.interpretation_client is None:
            fail_agent(
                ResearchAgentErrorCode.AGENT_INPUT_REQUIRED,
                "interpretation LLM configuration is required",
            )
        return _commit(
            config, session, status="active", history="解释配置已就绪",
        )
    fail_agent(
        ResearchAgentErrorCode.AGENT_INPUT_REQUIRED,
        f"unsupported response for {action_type}",
    )


def _configure_data_interface(
    config: AgentConfig,
    session: ResearchSession,
    *,
    stage: str,
    history: str,
) -> ResearchSession:
    data_plan = _load_artifact(config, session, "data_plan")
    requirement = data_plan.requirements[0]
    action = NextAction(
        action_type="configure_data_interface",
        title="缺少已验证的数据源映射",
        message=(
            "当前 InstrumentRegistry 中没有与需求匹配的已验证 "
            "Provider mapping。需要：\n"
            f"- requirement_id: {requirement.requirement_id}\n"
            f"- variable_id: {requirement.variable_id}\n"
            f"- instrument_id: {requirement.instrument_id}\n"
            f"- field: {requirement.field}\n"
            f"- frequency: {requirement.frequency}\n"
            f"- window: {requirement.start_date}.."
            f"{requirement.end_date}\n"
            f"- timezone: {requirement.timezone}\n"
            f"- currency: {requirement.currency}\n"
            f"- unit: {requirement.unit}\n"
            f"- required_pre_sample_periods: "
            f"{requirement.required_pre_sample_periods}\n"
            "可选做法：\n"
            "A. 在 InstrumentRegistry 增加 verified provider mapping\n"
            "B. 使用 local CSV 提供数据"
        ),
        why_needed="没有已验证映射就无法获取数据",
        required_inputs=["mapping 或 CSV 文件"],
    )
    return _commit(
        config, session, stage=stage,
        status="needs_user", next_action=action,
        history=history,
    )


def _replace_proposal_reference(
    config: AgentConfig,
    session: ResearchSession,
    relative_path: str,
    payload: bytes,
) -> ResearchSession:
    """Replace the active hypothesis_proposal reference with the new file."""
    reference = make_artifact_reference(
        "hypothesis_proposal",
        relative_path,
        payload,
        "1.0",
        created_at=config.now(),
    )
    remaining = [
        item
        for item in session.artifact_references
        if item.artifact_type != "hypothesis_proposal"
    ]
    return session.model_copy(
        update={"artifact_references": remaining + [reference]}
    )


def _confirm_stage(
    config: AgentConfig, session: ResearchSession, action_type: str
) -> ResearchSession:
    if action_type == "confirm_hypothesis":
        from market_validator.hypothesis.confirmation import (
            confirm_research_hypothesis_proposal,
            serialize_research_hypothesis_confirmation,
        )

        proposal = _load_artifact(config, session, "hypothesis_proposal")
        confirmation = confirm_research_hypothesis_proposal(
            proposal, confirmed_at=config.now()
        )
        payload = serialize_research_hypothesis_confirmation(confirmation)
        _write_artifact(
            config, "hypothesis-confirmation.json", payload
        )
        session = _append_artifact(
            config, session, "hypothesis_confirmation",
            "hypothesis-confirmation.json", payload, "1.0",
        )
        return _commit(
            config, session, status="active",
            stage="hypothesis_confirmation",
            history="假设已确认",
            artifacts=session.artifact_references,
        )
    if action_type == "confirm_data_plan":
        from market_validator.data.data_plan_review import (
            confirm_data_plan,
            persist_data_plan_confirmation,
        )

        generated = _load_generated_data_plan(config, session)
        try:
            confirmation = confirm_data_plan(
                generated, _instrument_registry(config),
                confirmed_at=config.now(),
            )
        except Exception as error:
            message = getattr(
                getattr(error, "failure", None), "message", ""
            ) or ""
            if "no verified provider mapping" in message or (
                "no verified provider" in message
            ):
                return _configure_data_interface(
                    config, session, stage="data_plan_confirmation",
                    history="缺少数据源映射（DataPlan 未确认）",
                )
            fail_agent(
                ResearchAgentErrorCode.AGENT_STATE_INVALID,
                "data plan confirmation failed",
            )
        persist_data_plan_confirmation(
            confirmation,
            _artifacts_dir(config) / "data-plan-confirmation.json",
        )
        payload = (_artifacts_dir(config) / "data-plan-confirmation.json").read_bytes()
        session = _append_artifact(
            config, session, "data_plan_confirmation",
            "data-plan-confirmation.json", payload, "1.0",
        )
        return _commit(
            config, session, status="active",
            stage="data_plan_confirmation",
            history="数据计划已确认",
            artifacts=session.artifact_references,
        )
    if action_type == "confirm_source_selection":
        from market_validator.data.source_selection import (
            confirm_source_selection,
            persist_source_selection_confirmation,
        )

        generated = _load_generated_source_selection(config, session)
        confirmation = confirm_source_selection(
            generated, _instrument_registry(config),
            _calendar_registry(config), confirmed_at=config.now(),
        )
        persist_source_selection_confirmation(
            confirmation,
            _artifacts_dir(config) / "source-selection-confirmation.json",
        )
        payload = (
            _artifacts_dir(config) / "source-selection-confirmation.json"
        ).read_bytes()
        session = _append_artifact(
            config, session, "source_selection_confirmation",
            "source-selection-confirmation.json", payload, "1.0",
        )
        return _commit(
            config, session, status="active",
            stage="source_selection_confirmation",
            history="数据源已确认",
            artifacts=session.artifact_references,
        )
    if action_type == "authorize_data_access":
        from market_validator.data.access_authorization import (
            create_data_access_authorization,
            serialize_data_access_authorization,
        )

        plan = _load_generated_acquisition_request_plan(config, session)
        authorization = create_data_access_authorization(
            plan,
            _instrument_registry(config),
            _calendar_registry(config),
            _capability_snapshots(config),
            [r.requirement_id for r in plan.acquisition_request_plan.requests],
            authorized_at=config.now(),
        )
        payload = serialize_data_access_authorization(authorization)
        _write_artifact(
            config, "data-access-authorization.json", payload
        )
        session = _append_artifact(
            config, session, "data_access_authorization",
            "data-access-authorization.json", payload, "1.0",
        )
        return _commit(
            config, session, status="active",
            stage="data_access_authorization",
            history="数据访问已授权",
            artifacts=session.artifact_references,
        )
    if action_type == "authorize_analysis_plan":
        from market_validator.analysis.authorization import (
            confirm_analysis_plan,
            serialize_analysis_plan_confirmation,
        )

        plan = _load_artifact(config, session, "analysis_plan")
        confirmation = confirm_analysis_plan(plan, confirmed_at=config.now())
        payload = serialize_analysis_plan_confirmation(confirmation)
        _write_artifact(
            config, "analysis-plan-confirmation.json", payload
        )
        session = _append_artifact(
            config, session, "analysis_plan_confirmation",
            "analysis-plan-confirmation.json", payload, "1.0",
        )
        return _commit(
            config, session, status="active",
            stage="analysis_confirmation",
            history="AnalysisPlan 已确认",
            artifacts=session.artifact_references,
        )
    if action_type == "execute_analysis":
        from market_validator.analysis.authorization import (
            create_analysis_authorization,
            serialize_analysis_authorization,
        )

        plan = _load_artifact(config, session, "analysis_plan")
        confirmation = _load_artifact(
            config, session, "analysis_plan_confirmation"
        )
        authorization = create_analysis_authorization(
            plan, confirmation, authorized_at=config.now()
        )
        payload = serialize_analysis_authorization(authorization)
        _write_artifact(
            config, "analysis-authorization.json", payload
        )
        session = _append_artifact(
            config, session, "analysis_authorization",
            "analysis-authorization.json", payload, "1.0",
        )
        return _commit(
            config, session, status="active",
            stage="analysis_authorization",
            history="分析已授权",
            artifacts=session.artifact_references,
        )
    fail_agent(
        ResearchAgentErrorCode.AGENT_STATE_INVALID,
        f"unknown confirmation action {action_type}",
    )


def _apply_source_choice(
    config: AgentConfig, session: ResearchSession, choice: int
) -> ResearchSession:
    from market_validator.data.source_selection import (
        SourceSelectionDecision,
        generate_source_selection,
        persist_generated_source_selection,
    )

    registry = _instrument_registry(config)
    data_plan = _load_artifact(config, session, "data_plan")
    decisions = []
    for requirement in data_plan.requirements:
        entry = registry.get(requirement.instrument_id)
        verified = [
            m for m in entry.provider_mappings if m.verified
        ]
        if not verified:
            fail_agent(
                ResearchAgentErrorCode.AGENT_STATE_INVALID,
                "no verified mapping for a requirement",
            )
        if choice < 0 or choice >= len(verified):
            fail_agent(
                ResearchAgentErrorCode.INVALID_AGENT_INPUT,
                "the choice index is out of range",
            )
        mapping = verified[choice]
        decisions.append(
            SourceSelectionDecision(
                requirement_id=requirement.requirement_id,
                provider_id=mapping.provider_id,
                provider_symbol=mapping.provider_symbol,
                dataset_or_endpoint=mapping.dataset_or_endpoint,
            )
        )
    generated = generate_source_selection(
        _load_generated_data_plan(config, session),
        _load_artifact(config, session, "data_plan_confirmation"),
        registry,
        _calendar_registry(config),
        decisions,
    )
    persist_generated_source_selection(
        generated, _artifacts_dir(config) / "source-selection.json"
    )
    import json as _json

    (_artifacts_dir(config) / "source-selection-generated.json").write_text(
        _json.dumps(
            generated.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    payload = (_artifacts_dir(config) / "source-selection.json").read_bytes()
    session = _append_artifact(
        config, session, "source_selection",
        "source-selection.json", payload, "1.0",
    )
    return _commit(
        config, session, status="active", stage="source_selection",
        history=f"数据源选择已应用（#{choice}）",
        artifacts=session.artifact_references,
    )


def _load_generated_acquisition_request_plan(
    config: AgentConfig, session: ResearchSession
):
    from market_validator.data.acquisition_request import (
        GeneratedAcquisitionRequestPlan,
    )

    generated_payload = (
        _artifacts_dir(config) / "acquisition-request-plan-generated.json"
    ).read_bytes()
    return GeneratedAcquisitionRequestPlan.model_validate_json(
        generated_payload
    )


def _load_generated_data_plan(config: AgentConfig, session: ResearchSession):
    from market_validator.data.data_plan_review import GeneratedDataPlan

    generated_payload = (
        _artifacts_dir(config) / "data-plan-generated.json"
    ).read_bytes()
    return GeneratedDataPlan.model_validate_json(generated_payload)


def _load_generated_source_selection(
    config: AgentConfig, session: ResearchSession
):
    from market_validator.data.source_selection import (
        GeneratedSourceSelection,
    )

    generated_payload = (
        _artifacts_dir(config) / "source-selection-generated.json"
    ).read_bytes()
    return GeneratedSourceSelection.model_validate_json(generated_payload)


def revise_research_session(
    *,
    parent_workspace: str,
    instruction: str,
    config: AgentConfig,
) -> ResearchSession:
    """Create a child session; nothing is inherited."""
    parent = load_latest_research_session(parent_workspace)
    question = (
        f"{parent.original_question}\n"
        f"[修订指令] {instruction}"
    )
    session = _new_session(
        config,
        question=question,
        parent_session_id=parent.session_id,
    )
    persist_session_revision(session, config.workspace)
    return advance_until_blocked(config, session)


__all__ = [
    "AgentConfig",
    "MAX_TRANSITIONS",
    "advance_research_session",
    "advance_until_blocked",
    "apply_research_agent_response",
    "create_research_session",
    "load_latest_research_session_public",
    "revise_research_session",
]
