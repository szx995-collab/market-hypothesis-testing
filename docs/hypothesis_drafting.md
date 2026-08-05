# Statistical hypothesis drafting

## What this stage does

The hypothesis-drafting layer turns a natural-language market question into an
untrusted, machine-readable `ResearchHypothesisProposal`. It is a review aid,
not a confirmed `ResearchSpec`, a data request, or an analysis result.

```text
natural-language question
→ optional LLM emits an untrusted proposal
→ deterministic Python strictly parses and validates it
→ user supplies structured answers for stable ambiguity IDs
→ Python applies only whitelisted fields and reruns full validation
→ user explicitly confirms the canonical proposal SHA-256
→ Python deterministically compiles the existing ResearchSpec contract
```

Ready is not confirmed, and confirmed is not executed. ResearchSpec generation
does not authorize network data access, data download, workflow execution, or
analysis artifact publication.

## Model structure

`ResearchHypothesisProposal` contains:

- the original question and a normalized research question;
- `association` or `predictive` as the claim type—never `causal`;
- exactly one outcome, one or more predictors, and optional controls;
- provider-neutral variable IDs, conceptual names, market context, asset type,
  transformation, proxy/roll notes, and time relation;
- optional sample dates and daily frequency;
- same-market, cross-market, or unresolved alignment, including a structured
  information cutoff when known;
- one supported method: Pearson correlation, Spearman correlation, OLS, or
  lead-lag regression;
- the tested predictor, parameter kind, direction, significance level, effect
  threshold, and deterministic H0/H1 text;
- assumptions, ambiguities, unsupported requests, and review readiness.

Unknown fields are forbidden. All types are strict. Empty text, negative lags,
duplicate variable IDs, unknown target references, invalid IANA timezones, NaN,
Infinity, duplicate JSON keys, Markdown fences, and extra prose are rejected.
Generated fields may not contain local paths, credentials, provider symbols,
Bundle or artifact identities, hashes, commands, URLs, or executable code. The
original question is retained as untrusted data and is never executed, even if
it resembles a prompt injection or shell instruction.

## Deterministic mathematical hypotheses

The LLM selects the supported method, target predictor, parameter kind, and
direction. Python is authoritative for H0/H1. With reference value fixed at 0:

| Direction | H0 | H1 |
|---|---|---|
| `two_sided` | `theta = 0` | `theta != 0` |
| `positive` | `theta <= 0` | `theta > 0` |
| `negative` | `theta >= 0` | `theta < 0` |

For Pearson and Spearman, `theta` is the population correlation coefficient
`rho`. For OLS and lead-lag regression, it is the selected predictor's
population coefficient `beta`. The JSON stores the tested predictor ID, so a
plain `beta` in the rendered H0/H1 is not an unbound free-text reference. If
stored H0/H1 differs by even one operator from the deterministic rendering, the
proposal is invalid.

An association proposal does not establish prediction or causation. Its
outcome, predictors, and controls must all have explicit contemporaneous or
lead/lag timing; `unspecified` timing blocks readiness. A predictive proposal
must explicitly mark every predictor and control—not only the tested
predictor—as preceding and available before the outcome information cutoff.
This prevents a secondary input from introducing future information. Causal
inference, backtesting, trading, buying or selling, order/position execution,
live or automated trading, data downloads, and unsupported methods must remain in
`unsupported_requests`; they cannot be silently converted into correlation or
regression.

## Ambiguities and readiness

The LLM must not guess outcome/predictor identity, level versus return/change or
volatility, contemporaneous versus lead/lag timing, one- versus two-sided test,
sample dates, session/calendar/cutoff, index versus ETF proxy, continuous
futures roll method, controls, significance, or minimum effect size.

Unknown choices remain `null` where the schema allows it and are named in
`ambiguities`. Any ambiguity, unsupported request, or structurally incomplete
required review item forces `ready_for_spec_review=false`. This is still a
valid proposal: “valid” means safe and structurally honest, not complete or
confirmed.

`ready_for_spec_review=true` is a Proposal-layer statement: the Proposal is
complete enough for user review and explicit confirmation. It does not
guarantee that every mandatory ResearchSpec mapping already exists, and
confirmation does not make generation mandatory — compilation can still return
structured `unresolved_requirements`. A relation such as `follows_outcome`
cannot be losslessly represented as a non-negative lag in the existing
ResearchSpec, so it is rejected at compilation rather than reinterpreted,
guessed, or silently converted.

## Structured clarification

Each current ambiguity receives a deterministic ID derived from its position
and exact text. `ClarificationAnswers` binds answers to the canonical source
proposal SHA-256 and accepts only whitelisted updates to variables, sample,
alignment, statistical settings, controls, or explicit ResearchSpec compilation
inputs. Unknown or duplicate IDs, conflicting field updates, duplicate JSON
keys, non-finite numbers, extra text, and unknown fields fail closed.

Python applies the answers without a second model call, regenerates H0/H1, and
reruns the complete proposal validator. Partial answers produce a new proposal
that remains not ready. Clarification cannot remove an unsupported request or
turn causal, trading, order-execution, or backtest intent into supported
research. The source proposal is never modified in place.

## Explicit confirmation and deterministic compilation

`ResearchHypothesisProposalConfirmation` records an explicit fixed statement,
`confirmed=true`, a timezone-aware audit time, and the exact canonical proposal
SHA-256. A proposal with ambiguities, unsupported requests, or readiness
blockers cannot be confirmed. Changing any effective proposal field changes the
canonical hash and invalidates the old confirmation. A confirmation record is
not a digital signature and grants no data, network, workflow, or execution
authority.

Proposal identity is the current canonical serialized representation. Whether
a default-valued field was explicitly provided is part of that representation:
explicitly providing a default and omitting it can produce different canonical
hashes for semantically equal models. This is a conservative anti-drift rule,
not a semantic-equivalence hash; a future switch to semantic canonicalization
requires its own schema/version and confirmation migration design. Because
every confirmation records a fresh timezone-aware audit time, re-confirming
the same proposal writes a different record and conflicts with an existing
immutable confirmation file instead of silently overwriting it. A confirmation
is a consistency binding and audit record, not identity authentication.

The compiler maps only confirmed values into the existing `ResearchSpec`:
claim type, roles, transformations, non-negative lags, sample, alignment,
method, deterministic H0/H1, direction, significance level, minimum effect,
assumptions, and explicitly supplied compilation inputs. It never invents an
instrument, field, provider symbol, sample bound, adjustment/revision rule,
join policy, robustness check, or limitation. Missing mandatory mappings return
structured `unresolved_requirements`; no placeholder ResearchSpec is emitted.

Canonical ResearchSpec bytes are persisted with a strict provenance sidecar
containing proposal hash, confirmation hash and audit metadata. Both files are
create-only, strictly reloaded, and never trigger data planning or analysis.

## LLM and Python responsibilities

The LLM receives only the original question, fixed system prompt, JSON Schema,
and explicit supported-capability catalog. It may identify concepts and propose
a draft. It receives no local path, Bundle, request ID, hash, artifact identity,
credential, tool, or workflow authority.

`HypothesisProposalService` depends only on `StructuredGenerationBackend`.
Python verifies backend/model identity, preserves the trimmed original question,
strictly validates the result, renders/verifies H0/H1, canonicalizes JSON,
computes SHA-256, and performs atomic create-only persistence. Model generation
is nondeterministic; only a particular canonical proposal byte sequence and its
SHA-256 are reproducible.

The optional DeepSeek path requires an explicit model and `--allow-network`,
reads only `DEEPSEEK_API_KEY`, permits the official HTTPS origin, makes at most
one request with no retry, rejects every HTTP redirect before a second request
can be sent, and enforces timeout and response-size limits. Authorization is
therefore never forwarded to a redirect destination. Tests use fake
backends/transports. Codex Plus generation remains unimplemented.

## Offline commands and example

Print the JSON Schema without a model call:

```powershell
python -m market_validator hypothesis schema
```

Validate and summarize the checked-in oil/A-share example:

```powershell
python -m market_validator hypothesis validate-proposal `
  examples/hypothesis_proposals/oil_to_a_share_energy.proposal.json
```

Inspect stable ambiguity IDs and validate the checked-in seven answers:

```powershell
python -m market_validator hypothesis validate-proposal `
  examples/hypothesis_proposals/oil_to_a_share_energy.proposal.json
python -m market_validator hypothesis clarification-schema
python -m market_validator hypothesis validate-clarifications `
  --proposal examples/hypothesis_proposals/oil_to_a_share_energy.proposal.json `
  --answers examples/hypothesis_proposals/oil_to_a_share_energy.clarifications.json
```

Create new files without changing the source proposal:

```powershell
python -m market_validator hypothesis apply-clarifications `
  --proposal <draft-proposal.json> `
  --answers <clarification-answers.json> `
  --output <clarified-proposal.json>
python -m market_validator hypothesis confirm-proposal `
  --proposal <clarified-proposal.json> `
  --output <confirmation.json>
python -m market_validator hypothesis compile-research-spec `
  --proposal <clarified-proposal.json> `
  --confirmation <confirmation.json> `
  --output <research-spec.json>
```

The oil example's seven answers make the conceptual Proposal ready and
confirmable, but intentionally do not invent provider-neutral instrument/field
metadata, minimum observations, join and missing-data policies, robustness
checks, or limitations. Its compile step therefore returns those fields as
structured unresolved requirements instead of fabricating a ResearchSpec.

The example identifies a positive predictive lead-lag regression and the oil
predictor's `beta`, with `H0: beta <= 0` and `H1: beta > 0`. It intentionally
remains not ready because the date range, oil instrument, return definitions,
energy-sector proxy, cross-market cutoff/calendar, controls, significance, and
effect threshold still require user clarification.

An optional real proposal request is separate and never confirms or executes:

```powershell
python -m market_validator propose-hypothesis `
  --question-file examples/hypothesis_proposals/oil_to_a_share_energy.question.txt `
  --provider deepseek_api `
  --model <explicit-current-model> `
  --output <new-proposal.json> `
  --allow-network
```

The output path must not exist. Missing network authorization, missing Key, or
an existing output fails before a model request. A successful call writes only
one canonical proposal file. It does not create confirmation, ResearchSpec,
WorkflowPlan, data, analysis, report, or artifact files.

## Remaining boundary

This stage ends at a strictly validated ResearchSpec. It does not select or
download data, calculate market calendars, compile a generic analysis workflow,
execute correlation/regression, backtest, publish a report, or place trades.
