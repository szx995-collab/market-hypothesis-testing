# Workflow and proposal CLI

The CLI is a stable, script-oriented wrapper around the existing deterministic
workflow, strict artifact loader, and optional proposal-only AI adapter. It does
not implement analysis itself. Commands are offline by default. Network access
requires an existing explicit boundary: FRED uses `--live`, while AI proposal
generation uses `propose-plan --allow-network` or
`propose-hypothesis --allow-network`. Each AI invocation permits at most one
provider request with no retry.

## Installation and equivalent entry points

Install the local package from the repository root:

```powershell
python -m pip install -e .
```

These entry points use the same parser and handlers:

```powershell
market-validator --help
python -m market_validator --help
```

No input path is inferred from the current directory or environment variables.
Every question, provider, model, proposal output, plan, Bundle, artifact root,
artifact directory, and external Manifest hash required by a command must be
explicit. Omitting `--allow-network` from either proposal-generation command
returns exit code 2 before provider construction.

## General hypothesis draft commands

Export the strict proposal JSON Schema offline:

```powershell
market-validator hypothesis schema
```

Validate and summarize an untrusted draft without confirming, compiling, or
executing it:

```powershell
market-validator hypothesis validate-proposal `
  examples/hypothesis_proposals/oil_to_a_share_energy.proposal.json
```

The summary includes the canonical proposal SHA-256, claim type, method,
readiness, stable ambiguity references, and unsupported requests. A
structurally valid proposal may correctly have `ready_for_spec_review=false`.

Review and apply explicit structured answers entirely offline:

```powershell
market-validator hypothesis clarification-schema
market-validator hypothesis validate-clarifications `
  --proposal <draft-proposal.json> `
  --answers <clarification-answers.json>
market-validator hypothesis apply-clarifications `
  --proposal <draft-proposal.json> `
  --answers <clarification-answers.json> `
  --output <new-clarified-proposal.json>
```

`apply-clarifications` never changes the source proposal. Partial answers are
valid but remain not ready. Unknown IDs, conflicting changes, and unsupported
request laundering fail with stable nonzero codes.

Explicit confirmation and ResearchSpec compilation are separate commands:

```powershell
market-validator hypothesis confirmation-schema
market-validator hypothesis confirm-proposal `
  --proposal <ready-proposal.json> `
  --output <confirmation.json>
market-validator hypothesis compile-research-spec `
  --proposal <ready-proposal.json> `
  --confirmation <confirmation.json> `
  --output <research-spec.json>
```

Confirmation binds the exact canonical proposal SHA-256. Compilation writes the
existing strict ResearchSpec plus `<research-spec.json>.provenance.json`; it
does not select a provider, download data, compile a workflow, or execute
statistics. Missing mandatory mappings return machine-readable
`unresolved_requirements` and write no spec.

`ready_for_spec_review=true` is a Proposal-layer statement: the Proposal is
reviewable and confirmable, but compilation may still return
`unresolved_requirements` when existing ResearchSpec mappings are absent or
inexpressible — for example a `follows_outcome` relation that cannot be
represented as a non-negative lag. Such relations are rejected, never guessed
or converted. Re-running `confirm-proposal` for the same proposal writes a new
audit timestamp and therefore conflicts with an existing immutable
confirmation file instead of overwriting it. Proposal hash identity includes
whether default-valued fields were explicitly provided; explicitly adding or
removing such fields changes the hash and invalidates an older confirmation.
A confirmation is a consistency binding and audit record, not identity
authentication.

The only hypothesis command that may call a model is explicit and create-only:

```powershell
market-validator propose-hypothesis `
  --question-file examples/hypothesis_proposals/oil_to_a_share_energy.question.txt `
  --provider deepseek_api `
  --model <explicit-current-model> `
  --output <new-proposal.json> `
  --allow-network
```

The output path must not already exist. Output conflict, missing Key, missing
network permission, unsafe path, or invalid provider output fails without a
partial file. The command creates no confirmation, ResearchSpec, plan, Bundle,
analysis, report, or artifact. It is separate from the fixed WTI workflow
proposal described below.

## Proposal and plan examples

The planning layer examples are in `examples/ai_planning/`. Validate an
untrusted proposal without executing anything:

Generate a new proposal through the optional DeepSeek adapter only after
explicitly allowing one network request:

```powershell
market-validator propose-plan `
  --question-file examples/ai_planning/wti_question.txt `
  --provider deepseek_api `
  --model deepseek-v4-flash `
  --output <proposal.json> `
  --allow-network
```

This command writes only a strictly parsed canonical proposal. It never creates
a confirmation, WorkflowPlan, or artifact and never invokes `compile-plan` or
`run`. Credentials come only from `DEEPSEEK_API_KEY`; they are not written or
printed.

Validate an existing untrusted proposal without executing anything:

```powershell
market-validator validate-proposal examples/ai_planning/wti_price_change_volatility.proposal.json
```

After the user has separately confirmed the exact canonical proposal SHA-256,
bind it to an explicit local Bundle and write only a WorkflowPlan:

```powershell
market-validator compile-plan `
  --proposal examples/ai_planning/wti_price_change_volatility.proposal.json `
  --confirmation examples/ai_planning/wti_price_change_volatility.confirmation.json `
  --bundle <bundle.json> `
  --output <workflow-plan.json>
```

`compile-plan` is create-only, atomic, and idempotent for identical bytes. It
does not call `run` or publish an analysis artifact. A different existing plan
is a conflict. Confirmation is hash-bound but is not a digital signature or
execution authorization. See [`ai_planning.md`](ai_planning.md).

The complete golden WTI plan is
[`examples/workflow_plans/fred_wti_price_change_volatility.json`](../examples/workflow_plans/fred_wti_price_change_volatility.json).
Its essential shape is:

```json
{
  "workflow_schema_version": "1.0",
  "analysis_type": "price_change_volatility",
  "expected_source_request_id": "fred-dcoilwtico-...",
  "expected_source_bundle_sha256": "<64 lowercase hex characters>",
  "parameters": {
    "analysis_as_of": "2025-01-03T00:00:00Z",
    "block_length": 5,
    "repetitions": 10000,
    "random_seed": 20260804
  },
  "expected_artifact_manifest_sha256": "<optional external trust anchor>"
}
```

The real file contains every fixed `PriceChangeVolatilityParameters` field.
Unknown fields and unknown analysis types are rejected. The CLI accepts only a
regular, non-symbolic-link UTF-8 JSON plan file.

## Commands

Validate a plan without executing analysis or writing an artifact:

```powershell
market-validator validate-plan examples/workflow_plans/fred_wti_price_change_volatility.json
```

Run the sole supported offline workflow. All locations are mandatory:

```powershell
market-validator run `
  --plan examples/workflow_plans/fred_wti_price_change_volatility.json `
  --bundle .market_validator/data/bundles/fred/fred-dcoilwtico-20260804T085757136534Z-58aa38ed3b12.json `
  --artifact-root .market_validator/analysis_artifacts
```

Strictly verify an existing artifact without recalculating or repairing it:

```powershell
market-validator verify-artifact `
  --artifact .market_validator/analysis_artifacts/price-change-volatility-0796a66788e5cfd71dff6a3222dd5cab `
  --expected-manifest-sha256 5be9387e8425838931618c585805a19e046a4b4ba9da5950373d83385e960a2e
```

The same arguments work after `python -m market_validator`.

## JSON streams

Successful command results are one deterministic UTF-8 JSON object on stdout;
stderr is empty:

```json
{"data": {"...": "validated data"}, "ok": true}
```

Failures write one JSON object to stderr and leave stdout empty. Tracebacks are
not emitted by default:

```json
{
  "error": {
    "code": "artifact_verification_failed",
    "message": "strict artifact verification failed",
    "stage": "artifact_verification"
  },
  "ok": false
}
```

`insufficient_evidence` is a successful statistical conclusion and therefore
uses exit code 0.

## Stable exit codes

| Exit code | Meaning |
|---:|---|
| 0 | Success, including `insufficient_evidence` |
| 2 | CLI arguments, plan path/UTF-8, or malformed JSON error |
| 3 | `invalid_plan` |
| 4 | `workflow_path_error` |
| 5 | `source_identity_mismatch` |
| 6 | `analysis_contract_mismatch` |
| 7 | `analysis_failed` |
| 8 | `artifact_conflict` |
| 9 | `artifact_verification_failed` |
| 10 | Unexpected internal CLI error |
| 11 | `invalid_proposal` |
| 12 | `invalid_confirmation` |
| 13 | `confirmation_mismatch` |
| 14 | `proposal_not_confirmable` |
| 15 | `proposal_data_contract_mismatch` |
| 16 | `plan_output_conflict` |
| 17 | `plan_output_error` |
| 18 | `provider_configuration_missing` |
| 19 | `provider_request_failed` |
| 20 | `provider_timeout` |
| 21 | `provider_refused` |
| 22 | `provider_invalid_proposal` |
| 23 | `proposal_output_conflict` |
| 24 | `proposal_output_error` |
| 25 | `invalid_hypothesis_proposal` |
| 26 | `hypothesis_backend_unavailable` |
| 27 | `hypothesis_backend_identity_mismatch` |
| 28 | `hypothesis_output_conflict` |
| 29 | `hypothesis_output_error` |
| 30 | `invalid_hypothesis_clarifications` |
| 31 | `hypothesis_clarification_mismatch` |
| 32 | `hypothesis_clarification_conflict` |
| 33 | `hypothesis_proposal_not_ready` |
| 34 | `invalid_hypothesis_confirmation` |
| 35 | `hypothesis_confirmation_mismatch` |
| 36 | `research_spec_unresolved` |
| 37 | `research_spec_invalid` |
| 38 | `hypothesis_review_output_conflict` |
| 39 | `hypothesis_review_output_error` |

## Trust and responsibility boundary

The external Manifest SHA-256 is checked before strict artifact parsing. It can
detect coordinated replacement only when retained separately from the artifact
package; it is not a digital signature or proof of authorship.

The responsibility chain is:

```text
natural-language question
→ optional AI provider emits an untrusted proposal
→ strict parsing and capability checks
→ user confirms the canonical proposal SHA-256
→ deterministic code binds the explicit Bundle and emits a WorkflowPlan
→ user separately invokes run
→ deterministic workflow transforms, analyzes, persists, and strictly reloads
→ AI may explain only the verified result
→ user makes the final judgment
```

The CLI can explicitly ask the optional AI adapter for either a generic
hypothesis draft or fixed WTI workflow proposal, validate a preconstructed
proposal, and compile only the explicitly confirmed fixed workflow proposal
against an existing local Bundle. Only `propose-plan --allow-network` and
`propose-hypothesis --allow-network` may call a model; only the separate `run`
command executes the existing
`price_change_volatility` workflow. The AI provider is not a data source,
statistical engine, confirmer, compiler, or executor. The CLI does not contact
FRED, automatically confirm or execute a proposal, run generic analysis,
regression, correlation, or backtesting, or expose an API service.
