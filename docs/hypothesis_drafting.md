# Statistical hypothesis drafting

## What this stage does

The hypothesis-drafting layer turns a natural-language market question into an
untrusted, machine-readable `ResearchHypothesisProposal`. It is a review aid,
not a confirmed `ResearchSpec`, a data request, or an analysis result.

```text
natural-language question
→ optional LLM emits an untrusted proposal
→ deterministic Python strictly parses and validates it
→ user resolves ambiguities and reviews the mathematics
→ a later stage may create a confirmed ResearchSpec
```

No current command converts this proposal into a ResearchSpec. Validation also
does not authorize network data access, compilation, workflow execution, or
artifact publication.

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

An association proposal does not establish prediction or causation. A
predictive proposal must explicitly mark the tested predictor as preceding and
available before the outcome. Causal inference, backtesting, trading, automatic
orders, data downloads, and unsupported methods must remain in
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
one request with no retry, and enforces timeout and response-size limits. Tests
use fake backends/transports. Codex Plus generation remains unimplemented.

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

## Next-stage boundary

A future implementation may add a hash-bound user clarification/confirmation
record and a deterministic compiler from a complete confirmed hypothesis
proposal to `ResearchSpec`. That compiler must preserve the confirmed variable
roles, mathematics, dates, alignment, assumptions, and limitations, and must
stop rather than guess any remaining field. It is not implemented in this
stage.
