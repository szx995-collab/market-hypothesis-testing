# Provider Execution & Transactional Snapshot

## Workflow position

```text
hypothesis_draft
→ spec_review
→ data_plan
→ source_selection
→ acquisition_request
→ data_access_authorization
→ provider execution + transactional snapshot   ← this phase
→ data_ready
```

This phase advances:

```text
DataAccess Authorized
→ Authorization Consumed
→ Provider Requests Executed
→ Responses Captured
→ Transactional Snapshot Committed
→ Snapshot Verified
```

It does **not** reach `data_ready`.

## Exact request steps (schema 1.1)

Every provider requirement is now expressed as ordered `PublicRequestStep`
entries inside the acquisition request plan (schema `1.1`). Each step binds:

- `step_id` and `sequence`
- `method` (`GET` / `READ`)
- `endpoint`
- `public_parameters`
- `pagination_policy` (`none` | `fred_count_offset_v1`)

Steps are deterministically sorted by `sequence`; step ids and sequences must
be unique; endpoints and parameters enter the canonical SHA-256. The
`DataAccessAuthorization` binds the new plan hash, so endpoint, parameter,
pagination-policy, or step-order changes invalidate the authorization.

**Old `1.0` artifacts fail closed** — the strict `Literal["1.1"]` schema
rejects them. Phase 2 artifacts must be regenerated; nothing is silently
converted.

### FRED exact steps

Every FRED requirement binds at least:

```text
step 1: GET /fred/series
        series_id, file_type=json
        pagination_policy=none

step 2: GET /fred/series/observations
        series_id, file_type=json, units=lin, sort_order=asc,
        observation_start, observation_end, output_type, limit=100000
        realtime_start / realtime_end (only for initial_release)
        pagination_policy=fred_count_offset_v1
```

Non-network parameters (`revision_policy`, `access_mode`, `requirement_id`,
`request_plan_id`, `authorization_id`) are **never** sent to FRED.
`revision_policy` remains a semantic artifact field but is excluded from the
HTTP query.

## FRED pagination policy `fred_count_offset_v1`

- first page `offset=0`
- `count` stays identical across pages
- next offset equals the number of observations received so far
- returned `offset` must equal the requested offset
- an empty page before `count` is satisfied is a hard failure
- total observations must equal `count`
- no offset is requested twice; pages are sequential (never parallel)
- no automatic retry; no unplanned endpoints

Every actual pagination request enters the execution trace.

## Authorization consumption

`execute_authorized_acquisition` consumes the single-use authorization and
persists the consumption receipt **before** any provider request. If the
receipt cannot be persisted, zero provider calls happen. A second execution
against the same authorization is rejected (`authorization_already_consumed`);
a failed request leaves the authorization consumed — a new authorization is
required for retry.

## Credential boundary (non-interactive only)

- GUI / TTY / interactive prompts are forbidden.
- Credentials are never written to artifacts, logs, public parameters, or the
  transport trace.
- The only allowed path: when the provider explicitly requires
  authentication, the existing resolver runs with `interactive=False`
  (environment or current-process memory).
- Missing credentials fail structurally (`credential_not_configured`) before
  any network call.
- No provider switching, no continuing with other requests.
- Tests use fake secrets and injected fake transports; CI never depends on
  `FRED_API_KEY`.

## Execution coordinator

`execute_authorized_acquisition(...)` follows a fixed order:

1. strict re-parse of the plan
2. all canonical hash checks
3. registries validated
4. capability snapshots validated
5. authorization matches the plan exactly
6. authorized request ids equal plan request ids exactly
7. access flags checked
8. paid / retry / fallback all confirmed `false`
9. authorization consumed
10. receipt persisted create-only first
11. requests executed in requirement-id order
12. each requirement calls only its explicit adapter
13. all raw bytes and request traces captured
14. DataBundles built
15. snapshot commit starts only after all requirements succeeded
16. staged writes
17. staged verification
18. atomic rename
19. final verification
20. `VerifiedAcquisitionSnapshot` returned

## Provider adapters

`ProviderExecutionAdapter` returns an internal `ProviderExecutionCapture`
(requirement id, provider id, timestamps, request trace, raw artifacts,
bundle). Raw bytes are never embedded in public JSON artifacts.

- **FRED adapter** reuses the existing mapping validation, capability
  validation, series metadata validation, pagination validation, observation
  normalization, quality generation, and sanitized error paths. It never
  calls legacy `FredStorage`. The legacy `FredProvider.fetch` keeps its old
  behavior and tests.
- **Local CSV adapter** requires `local_file` access mode and
  `local_file_read_authorized=true`; path identity must match the plan
  exactly; relative paths, `..`, symlink components, directories, devices and
  sockets are rejected; the file is read exactly once; SHA-256 and byte size
  are computed from the captured bytes; the same bytes are used for parsing
  (no second open); stat identity/size/mtime changes during the read fail.

## Transactional snapshot

One authorization's full request results commit as **one** snapshot:

```text
<root>/<snapshot_id>/
  manifest.json
  outcome.json
  requests/<requirement_id>/
    request-trace.json
    raw/...
    bundle.json
```

- `snapshot_id` derives deterministically from attempt id, authorization
  hash, receipt hash, plan hash, and all raw/bundle content hashes (never
  from timestamps alone).
- The manifest binds all upstream hashes (research spec, data plan and
  confirmation, source selection and confirmation, both registry hashes),
  plus per-request step hashes and artifact records (relative path, SHA-256,
  byte size, media type, role).
- All relative paths use `/`, are non-absolute, contain no `..`, are
  unique, and never escape the snapshot root.
- Commit is a staged transaction: sibling staging dir under the same
  filesystem, every file fsynced, staging re-read and hash/size/strict-parse
  verified, fsync of parent, atomic directory rename, then the final tree is
  re-read and verified again.
- Any failure removes the staging directory; the final snapshot never
  exists partially, never overwrites an existing snapshot, and is never
  reported as success.

## Failure outcome

`AcquisitionExecutionOutcome` records `succeeded` or `failed` with safe
fields only: `code`, `stage`, `provider_id`, `requirement_id`,
`safe_message`, `retryable=false`. Raw responses, secrets, API keys,
headers, local file contents, and tracebacks never appear in outcomes. After
a failure the authorization stays consumed; no retry, no fallback, no
receipt reuse, no final snapshot directory; a safe failure outcome may be
persisted create-only.

## Snapshot Verified — precise meaning

Verification covers structure, identity, and persistence integrity only:

- manifest strict parse and its hash
- every artifact exists as a regular file
- relative paths do not escape
- byte sizes and SHA-256s match
- request ids complete and unique
- request traces match the plan steps
- authorization / receipt / plan hashes match
- bundles strict-parse; bundle requirements match the DataPlan exactly;
  source provider matches the request provider; symbols/dataset identity
  match; `is_fallback=false`
- no extra or missing artifacts

This phase does **not** judge:

- whether quality ERROR/WARNING would allow `data_ready`
- whether observations cover the full sample
- whether pre-sample observations are truly sufficient
- whether all DataPlan requirements satisfy analysis conditions
- whether the data is suitable for analysis

```text
Snapshot Verified
≠ Quality Accepted
≠ Coverage Verified
≠ DataBundle Set Verified
≠ Data Ready
```

Quality / coverage / `DataReadyManifest` acceptance is Phase 4.

## Trust boundaries

- no automatic provider selection
- no automatic retry, no fallback, no retry of a failed authorization
- no unplanned endpoints, no modification of authorized parameters
- no pagination guessing, no pre-sample guessing, no date/symbol/field/
  market/calendar/revision guessing
- no paid access, no interactive credentials, no credential persistence,
  no secrets in logs
- no real network in CI, no real FRED key in CI
- no `DataReadyManifest`, no quality/coverage acceptance, no analysis, no
  version change, no release

## Tests

- `tests/test_provider_execution.py` (17): exact-request regression
  (metadata step, observations step, `limit=100000`, no `revision_policy` in
  HTTP, realtime parameters, sequence/endpoint/parameter/pagination hash
  binding, contract mismatch), FRED fake-transport execution (single page,
  multi page, exact call counts, no retry), credential boundaries (missing
  credential → zero calls, secrets never in artifacts), CSV adapter (valid
  absolute file, relative/directory rejection, invalid UTF-8 as quality
  finding), authorization consumption (receipt persistence first, second
  execution rejected, plan mutation rejected).
- `tests/test_transactional_snapshot.py` (13): multi-request one-commit,
  second-request failure leaves no final snapshot, deterministic order,
  injected failures at six stages leave no final snapshot, tampering
  (raw/bundle/manifest/extra/missing/stale plan) all fail closed.
