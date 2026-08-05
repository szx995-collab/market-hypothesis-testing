"""Public contracts for optional, proposal-only AI providers."""

from market_validator.plan_providers.base import (
    PlanProposalGenerationRequest,
    PlanProposalProvider,
    PlanProposalProviderError,
    PlanProposalProviderErrorCode,
    PlanProposalProviderFailure,
    PlanProposalProviderStage,
    RawPlanProposalResponse,
)
from market_validator.plan_providers.deepseek_api import (
    DeepSeekApiPlanProposalProvider,
)
from market_validator.plan_providers.service import (
    GeneratedPlanProposal,
    PersistedGeneratedPlanProposal,
    SUPPORTED_CAPABILITY_DESCRIPTIONS,
    SUPPORTED_PLAN_PROPOSAL_PROVIDERS,
    create_plan_proposal_provider,
    generate_market_validation_plan_proposal,
    persist_generated_plan_proposal,
    validate_plan_proposal_output_path,
)

__all__ = [
    "DeepSeekApiPlanProposalProvider",
    "GeneratedPlanProposal",
    "PersistedGeneratedPlanProposal",
    "PlanProposalGenerationRequest",
    "PlanProposalProvider",
    "PlanProposalProviderError",
    "PlanProposalProviderErrorCode",
    "PlanProposalProviderFailure",
    "PlanProposalProviderStage",
    "RawPlanProposalResponse",
    "SUPPORTED_CAPABILITY_DESCRIPTIONS",
    "SUPPORTED_PLAN_PROPOSAL_PROVIDERS",
    "create_plan_proposal_provider",
    "generate_market_validation_plan_proposal",
    "persist_generated_plan_proposal",
    "validate_plan_proposal_output_path",
]
