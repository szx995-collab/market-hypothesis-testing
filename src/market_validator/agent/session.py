"""Session persistence and artifact verification (v0.4.0 Phase 4).

Sessions are immutable revision files (000001.json, 000002.json, ...)
under session-revisions/. Artifact references are re-verified on every
load: no traversal, no symlinks, strict parse, canonical hash comparison.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from market_validator.agent.models import (
    ArtifactReference,
    ResearchAgentErrorCode,
    ResearchSession,
    calculate_research_session_sha256,
    canonical_bytes,
    fail_agent,
    parse_research_session,
    serialize_research_session,
    sha256_hex,
)
from market_validator.hypothesis.lifecycle import (
    HypothesisLifecycleErrorCode,
    persist_immutable_bytes,
)


def _session_revisions_dir(workspace: Path) -> Path:
    return workspace / "session-revisions"


def _revision_path(workspace: Path, revision: int) -> Path:
    return _session_revisions_dir(workspace) / f"{revision:06d}.json"


def load_latest_research_session(workspace: str | Path) -> ResearchSession:
    """Load the highest valid revision; session_id must match the first."""
    root = Path(workspace)
    revisions_dir = _session_revisions_dir(root)
    if not revisions_dir.is_dir():
        fail_agent(
            ResearchAgentErrorCode.SESSION_NOT_FOUND,
            "no research session exists in this workspace",
        )
    revisions = sorted(
        path for path in revisions_dir.glob("*.json")
        if path.name.endswith(".json")
    )
    if not revisions:
        fail_agent(
            ResearchAgentErrorCode.SESSION_NOT_FOUND,
            "no research session exists in this workspace",
        )
    first_session: ResearchSession | None = None
    loaded: ResearchSession | None = None
    for path in revisions:
        try:
            session = parse_research_session(path.read_bytes())
        except Exception:
            fail_agent(
                ResearchAgentErrorCode.SESSION_ARTIFACT_MISMATCH,
                "a session revision failed strict validation",
            )
        if first_session is None:
            first_session = session
        if session.session_id != first_session.session_id:
            fail_agent(
                ResearchAgentErrorCode.SESSION_ARTIFACT_MISMATCH,
                "session revisions disagree on the session id",
            )
        loaded = session
    assert loaded is not None
    return loaded


def persist_session_revision(
    session: ResearchSession,
    workspace: str | Path,
) -> Path:
    """Create-only append of the next revision; never overwrite history."""
    root = Path(workspace)
    revisions_dir = _session_revisions_dir(root)
    revisions_dir.mkdir(parents=True, exist_ok=True)
    payload = serialize_research_session(session)
    path = _revision_path(root, session.revision)
    try:
        return persist_immutable_bytes(payload, path)
    except Exception as error:
        code = getattr(getattr(error, "failure", None), "code", None)
        if code is HypothesisLifecycleErrorCode.OUTPUT_CONFLICT:
            fail_agent(
                ResearchAgentErrorCode.SESSION_REVISION_CONFLICT,
                "the session revision already exists with different content",
            )
        fail_agent(
            ResearchAgentErrorCode.AGENT_OUTPUT_ERROR,
            "the session revision could not be persisted",
        )


# ---------------------------------------------------------------------------
# Artifact verification
# ---------------------------------------------------------------------------

_ARTIFACT_PARSERS: dict[str, object] = {}


def register_artifact_parser(artifact_type: str, parser) -> None:
    _ARTIFACT_PARSERS[artifact_type] = parser


def _artifact_path(workspace: Path, reference: ArtifactReference) -> Path:
    path = workspace / "artifacts" / reference.relative_path
    resolved = path.resolve()
    artifacts_root = (workspace / "artifacts").resolve()
    if artifacts_root not in resolved.parents:
        fail_agent(
            ResearchAgentErrorCode.SESSION_ARTIFACT_MISMATCH,
            "artifact path escapes the artifacts directory",
        )
    for part in [path, *path.parents]:
        if part.is_symlink():
            fail_agent(
                ResearchAgentErrorCode.SESSION_ARTIFACT_MISMATCH,
                "artifact path must not contain symlinks",
            )
        if part == artifacts_root:
            break
    return path


def load_verified_artifact(
    workspace: str | Path,
    reference: ArtifactReference,
) -> tuple[object, bytes]:
    """Load + strict parse + canonical-hash verify an artifact reference."""
    root = Path(workspace)
    path = _artifact_path(root, reference)
    if not path.is_file():
        fail_agent(
            ResearchAgentErrorCode.SESSION_ARTIFACT_MISMATCH,
            "a referenced artifact is missing",
        )
    payload = path.read_bytes()
    actual_sha256 = sha256_hex(payload)
    if actual_sha256 != reference.sha256:
        fail_agent(
            ResearchAgentErrorCode.SESSION_ARTIFACT_MISMATCH,
            "a referenced artifact hash does not match the session",
        )
    parser = _ARTIFACT_PARSERS.get(reference.artifact_type)
    if parser is None:
        fail_agent(
            ResearchAgentErrorCode.SESSION_ARTIFACT_MISMATCH,
            "no verifier is registered for the artifact type",
        )
    try:
        artifact = parser(payload)
    except Exception:
        fail_agent(
            ResearchAgentErrorCode.SESSION_ARTIFACT_MISMATCH,
            "a referenced artifact failed strict validation",
        )
    return artifact, payload


def make_artifact_reference(
    artifact_type: str,
    relative_path: str,
    payload: bytes,
    schema_version: str,
    *,
    created_at: datetime | None = None,
) -> ArtifactReference:
    return ArtifactReference(
        artifact_type=artifact_type,
        relative_path=relative_path,
        sha256=sha256_hex(payload),
        schema_version=schema_version,
        created_at=created_at or datetime.now(timezone.utc),
    )


__all__ = [
    "load_latest_research_session",
    "load_verified_artifact",
    "make_artifact_reference",
    "persist_session_revision",
    "register_artifact_parser",
]
