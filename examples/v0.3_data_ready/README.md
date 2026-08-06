# TEST-ONLY Synthetic Offline Example (v0.3.0)

> **TEST-ONLY SYNTHETIC EXAMPLE** · **NOT MARKET DATA** · **NOT ANALYSIS** ·
> **NOT INVESTMENT ADVICE**

This directory contains a fully synthetic, fully offline example that walks
the formal v0.3 lifecycle from `DataPlan Confirmed` to `Data Ready` using
only the repository's own fixtures and the explicit local CSV provider.

It proves the lifecycle works end to end without any network access, without
credentials, and without a real provider.

## What it contains

```
fixtures/
  research-spec.json          TEST-ONLY synthetic association spec
  instrument-registry.json    two synthetic instruments mapped to local CSV
  calendar-registry.json      one synthetic calendar referencing a schedule adapter
  session-schedule.json       explicit verified session dates (exact set)
  local-data.csv              synthetic local CSV fixture (9 required columns)
  source-selection-decisions.json   reference decisions (TEST-ONLY fixture behavior)
run_example.py                formal domain-API driver
```

## How to run

```powershell
python examples\v0.3_data_ready\run_example.py --output-dir <new-empty-dir>
```

Without `--output-dir` the example writes into a fresh TEMP directory and
never touches tracked repository files. The output contains:

- `artifacts/` — every canonical lifecycle artifact (data plan,
  confirmations, source selection, acquisition request plan 1.2,
  authorization, receipt, snapshot manifest, session schedule,
  readiness assessment 1.1, data ready manifest 1.1)
- `snapshots/` — the transactional acquisition snapshot

The final JSON summary includes `status: "data_ready"`, the
`data_ready_id`, the manifest SHA-256, and explicit flags:

```json
"TEST_ONLY_SYNTHETIC_EXAMPLE": true,
"NOT_MARKET_DATA": true,
"NOT_ANALYSIS": true,
"NOT_INVESTMENT_ADVICE": true
```

## Trust boundaries honored by the example

- No network access, no credentials, no environment secrets.
- The provider is the explicit `local_csv` provider; nothing is
  auto-selected.
- The session schedule is an explicit snapshot; no dates are derived from
  calendar names, markets, or weekdays.
- Confirmation and authorization are real domain operations; the fixture
  auto-generates them only as documented TEST-ONLY fixture behavior and it
  prints that fact in the summary. This does not represent a real user
  confirmation.
- No analysis, robustness, or report artifacts are produced.

## CI / release acceptance

```powershell
python scripts\verify_v0_3_offline_example.py
```

The verifier runs the example in a fresh TEMP directory and asserts that the
example reaches `data_ready` with two requirements, produces no analysis
artifact, and carries the TEST-ONLY flags.
