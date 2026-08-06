"""Research agent (v0.4.0 Phase 4): personal research session orchestration."""

from market_validator.agent.models import (
    ArtifactReference,
    NextAction,
    ResearchAgentError,
    ResearchAgentErrorCode,
    ResearchSession,
)
from market_validator.agent.orchestrator import (
    AgentConfig,
    MAX_TRANSITIONS,
    advance_research_session,
    advance_until_blocked,
    apply_research_agent_response,
    create_research_session,
    load_latest_research_session_public,
    revise_research_session,
)
from market_validator.agent.presentation import (
    describe_blockers,
    format_session_json,
    format_session_status,
    human_stage_name,
)
from market_validator.agent.session import (
    load_latest_research_session,
    load_verified_artifact,
    make_artifact_reference,
    persist_session_revision,
    register_artifact_parser,
)

__all__ = [
    "AgentConfig",
    "ArtifactReference",
    "MAX_TRANSITIONS",
    "NextAction",
    "ResearchAgentError",
    "ResearchAgentErrorCode",
    "ResearchSession",
    "advance_research_session",
    "advance_until_blocked",
    "apply_research_agent_response",
    "create_research_session",
    "describe_blockers",
    "format_session_json",
    "format_session_status",
    "human_stage_name",
    "load_latest_research_session",
    "load_latest_research_session_public",
    "load_verified_artifact",
    "make_artifact_reference",
    "persist_session_revision",
    "register_artifact_parser",
    "revise_research_session",
]
