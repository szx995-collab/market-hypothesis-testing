# AI planning contract

The planning layer separates an untrusted AI suggestion from an executable
workflow plan. An optional provider layer may make one explicitly authorized
model request, but it can only generate proposal text. Strict parsing,
confirmation, Bundle binding, compilation, workflow execution, and artifact
verification remain separate deterministic boundaries.

This document describes the fixed `price_change_volatility` planning contract.
The independent general statistical-hypothesis draft contract is documented in
[`hypothesis_drafting.md`](hypothesis_drafting.md); it does not compile to this
WorkflowPlan and currently stops before confirmation or ResearchSpec creation.

```text
natural-language question
→ optional AI provider emits an untrusted proposal
→ the existing strict parser and capability checks
→ user reviews the complete proposal and confirms its SHA-256
→ compile-plan deterministically loads and identifies one explicit Bundle
→ compile-plan emits a strict WorkflowPlan and stops
→ user separately invokes run
→ artifact is strictly reloaded
→ AI may explain only the verified result
```

## Optional proposal provider

`PlanProposalProvider` is provider-neutral and deliberately narrower than the
workflow API. Its request has exactly four fields: the normalized original
market question, the fixed system prompt, the proposal JSON Schema, and the
current supported-capability descriptions. It receives no Bundle, path,
request ID, hash, artifact identity, or credential field. The model request
defines no tools, so the model cannot access files, network data, or workflow
operations through this application.

The first optional adapter is `deepseek_api`. It reads `DEEPSEEK_API_KEY` only
from the process environment and accepts the credential-free official HTTPS
origin from `DEEPSEEK_BASE_URL` (default `https://api.deepseek.com`). The model
must be explicit at the CLI. The key is used only in the Authorization header;
it is never placed in the prompt, request JSON, proposal, output metadata,
exception message, or path.

The adapter uses non-streaming `/chat/completions` with JSON Output and no
tools. DeepSeek JSON Output guarantees JSON syntax, not this project's complete
domain contract, and the service can also return empty or truncated content.
Consequently every raw model response is always passed to
`parse_market_validation_plan_proposal()`; only its canonical result may be
saved. See the official [JSON Output guide](https://api-docs.deepseek.com/guides/json_mode/)
and [Chat Completions reference](https://api-docs.deepseek.com/api/create-chat-completion).

The AI provider is not a market-data source, statistical engine, user
confirmation mechanism, Bundle binder, workflow executor, or report verifier.

Provider failures remain distinct and script-readable:

- `provider_configuration_missing`: required safe provider configuration is
  absent or invalid;
- `provider_request_failed`: transport, response-envelope, empty-output, or
  truncation failure;
- `provider_timeout`: the bounded request timed out;
- `provider_refused`: the provider explicitly filtered or refused the answer;
- `provider_invalid_proposal`: raw content failed the existing strict parser or
  changed the original question;
- `proposal_output_conflict`: an existing output has different bytes;
- `proposal_output_error`: the output path or atomic publication failed.

Failures never become a confirmation, plan, workflow result, or statistical
`insufficient_evidence` conclusion.

## Proposal boundary

`MarketValidationPlanProposal` uses strict Pydantic models with unknown fields
forbidden. It contains the original question, the only supported analysis type
(`price_change_volatility`), reviewable hypothesis/H0/H1/decision rule, the
complete fixed `PriceChangeVolatilityParameters`, a data requirement,
assumptions, ambiguities, unsupported requests, and a readiness flag.

The proposal does not contain local paths, request IDs, Bundle hashes, artifact
IDs, Manifest hashes, credentials, commands, function names, or executable
code. The original question remains untrusted data even if its text resembles
an instruction; deterministic Python never executes it.

The parser accepts exactly one UTF-8 JSON object. It rejects Markdown fences,
leading or trailing prose, duplicate keys at any nesting level, unknown fields,
wrong types, and non-standard numbers such as NaN or Infinity. Canonical
serialization is deterministic, and the proposal SHA-256 is calculated over
those canonical serialized bytes rather than the raw AI response.

The proposal JSON Schema and deterministic system-prompt template are
exportable without calling a model:

```python
from market_validator.planning import (
    market_validation_plan_proposal_json_schema,
    market_validation_plan_proposal_system_prompt,
)

schema = market_validation_plan_proposal_json_schema()
system_prompt = market_validation_plan_proposal_system_prompt()
```

The prompt explicitly names the sole current capability. It tells a future AI
to put unmappable requests in `unsupported_requests` and unresolved choices in
`ambiguities`, set `ready_for_confirmation=false`, and never guess.

## Confirmation boundary

`PlanProposalConfirmation` contains a schema version, the exact canonical
proposal SHA-256, and `confirmed=true`. Any proposal change produces a different
hash and requires a new confirmation. Compilation is blocked when confirmation
is missing or false, the hash differs, the proposal is not ready, ambiguities
remain, or unsupported requests remain.

A confirmation file records that the user reviewed that exact proposal. It is
not a digital signature, does not prove who confirmed it, and does not grant
network, artifact-write, or workflow-execution permission.

## Deterministic Bundle binding

`compile_confirmed_workflow_plan()` uses the existing `load_data_bundle()`
boundary. It verifies the Bundle against the confirmed provider/data-series,
instrument, field, date range, frequency, timezone, currency, unit, revision,
adjustment, and contract-roll requirements. It reads the request ID from the
safe Bundle filename and computes SHA-256 from the actual Bundle bytes. These
identities cannot come from the AI proposal.

The compiled `MarketValidationWorkflowPlan` reuses the confirmed parameters
without modification. The optional external artifact Manifest SHA-256 may only
come from the caller's explicit trusted argument. Compilation does not analyze
data, persist an analysis artifact, contact FRED, or invoke the workflow.

## Examples and CLI

The WTI example set is:

- `examples/ai_planning/wti_price_change_volatility.proposal.json`
- `examples/ai_planning/wti_price_change_volatility.confirmation.json`
- `examples/ai_planning/wti_price_change_volatility.workflow_plan.json`
- `examples/ai_planning/wti_question.txt`

Explicitly request a new untrusted proposal (this is the only command here that
may use the network):

```powershell
python -m market_validator propose-plan `
  --question-file examples/ai_planning/wti_question.txt `
  --provider deepseek_api `
  --model deepseek-v4-flash `
  --output <new-proposal.json> `
  --allow-network
```

Omitting `--allow-network` stops at argument parsing. The command does not
confirm, compile, analyze, or create an artifact. It atomically creates only the
canonical proposal JSON; identical bytes are idempotent and different existing
content is a conflict.

Validate the untrusted proposal:

```powershell
python -m market_validator validate-proposal examples/ai_planning/wti_price_change_volatility.proposal.json
```

Compile a plan without running it:

```powershell
python -m market_validator compile-plan `
  --proposal examples/ai_planning/wti_price_change_volatility.proposal.json `
  --confirmation examples/ai_planning/wti_price_change_volatility.confirmation.json `
  --bundle <bundle.json> `
  --output <workflow-plan.json>
```

Plan output uses deterministic UTF-8 JSON and an atomic create-only publish.
Writing identical bytes again is idempotent and preserves the file; different
existing content is never overwritten. Running the plan remains a separate,
explicit `run` command.

## Current limitations

There is no automatic provider selection, automatic confirmation, network
market-data acquisition, or automatic execution. Model generation is
nondeterministic: only the canonical proposal bytes and their SHA-256 are
deterministic after strict parsing. Any changed generation requires fresh user
review and confirmation. The fixed WTI proposal remains a test fixture; a live
model is not expected to reproduce its SHA-256. The planning contract still
supports only the fixed WTI price-change-volatility capability and adds no
regression, correlation, backtesting, causal inference, prediction, or trading.
