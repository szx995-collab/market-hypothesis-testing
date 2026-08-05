# Price-change volatility analysis artifacts

The price-change volatility comparison can be preserved as an immutable,
auditable package after `compare_price_change_volatility(path, parameters)` has
returned a validated `PriceChangeVolatilityResult`. Persistence does not rerun
the transform, calculate statistics, or change a conclusion.

## Directory contract

The caller chooses an artifact root. Each result is stored below it as:

```text
<artifact_root>/price-change-volatility-<result-sha256-prefix>/
├── result.json
├── report.md
└── manifest.json
```

The artifact ID combines the analysis type with the first 128 bits of the
SHA-256 of the exact `result.json` bytes. The Manifest retains the complete
result SHA-256. Therefore the same result produces the same ID and bytes on
repeated runs; neither current time nor a machine-specific identifier is used.
The project's local convention for generated packages is
`.market_validator/analysis_artifacts/`, which remains separate from source
Bundles, Manifests, and raw provider responses.

## File contracts

`result.json` is deterministic UTF-8 JSON written only by
`serialize_price_change_volatility_result()`. Pydantic computed fields are not
persisted. NaN and infinities are prohibited by the strict result model, and
the written bytes must load directly with
`PriceChangeVolatilityResult.model_validate_json()` and equal the input model.

`report.md` is a deterministic, user-readable projection of the validated
result. It displays the fixed hypothesis and decision rule, data facts, all
main sample summaries, statistical inference, both robustness checks,
inclusion and exclusion counts, every warning and error, source identity,
method limitations, and the final conclusion. It does not recalculate,
re-round, or reclassify any model value. In particular, it states that this is
a retrospective per-observation comparison—not a causal test, proof of
predictive ability or trading profitability, or an equal-interval annualized
daily volatility measure.

`manifest.json` is a strict `extra="forbid"` model. It records the artifact
schema and analysis type, artifact ID, source request ID and Bundle SHA-256,
the complete fixed analysis parameters, main and final conclusions, and the
filename, byte count, and SHA-256 for both `result.json` and `report.md`. The
Manifest deliberately does not contain its own hash. The persistence call
returns the SHA-256 of the actual Manifest bytes so a later workflow can keep
that value outside the package as a trust anchor.

## Save and load boundaries

The public entry points are:

```python
persist_price_change_volatility_artifact(result, artifact_root)
load_price_change_volatility_artifact(
    artifact_path,
    expected_manifest_sha256=None,
)
```

Persistence writes all three files into a new sibling temporary directory,
strictly reloads and verifies that temporary package, and then atomically
renames it to its final deterministic directory. A new target is never
overwritten. Repeating the operation is idempotent only when all three existing
files are byte-for-byte identical; otherwise it raises a conflict. Failures
before publication remove the temporary directory and do not expose a partial
final artifact.

Loading requires exactly the three expected regular files. Path traversal,
symbolic links, missing files, extra entries, and non-regular files are
rejected. The loader then:

1. checks an optional external Manifest SHA-256 before parsing the Manifest;
2. strictly parses the Manifest and requires its canonical encoding;
3. verifies result/report byte counts and hashes;
4. directly and strictly parses `result.json` and requires canonical bytes;
5. recomputes the artifact ID from the result bytes;
6. compares source identity, complete parameters, and conclusions between the
   Manifest and result;
7. rerenders the report from the result and requires byte-for-byte equality.

Without `expected_manifest_sha256`, these checks establish package-internal
consistency and detect accidental or uncoordinated changes. They cannot detect
an attacker who coherently replaces the result, report, and Manifest. A
Manifest SHA-256 saved outside the package is needed to detect that coordinated
replacement. The hash is not a digital signature and does not by itself prove
the author or authenticity of the analysis.
