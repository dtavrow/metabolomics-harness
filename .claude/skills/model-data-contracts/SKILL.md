---
name: model-data-contracts
description: "Use when debugging a run-model or sirius error involving candidates.parquet, per-spectrum candidate scoping, adduct format, or an eligibility skip reason. Points at the exact files that define and enforce the contract instead of re-deriving it from scratch."
---

# Model data contracts: candidates, adducts, eligibility

## Per-spectrum candidate scoping

`candidates.parquet` is a flat table: one row per `(spectrum_id, candidate_id)`
pair, schema in `models/common/contracts.py` `CANDIDATE_SCHEMA`. There is no
separate "candidate list per spectrum" structure to manage by hand.

- `models/common/candidates.py:read_candidates` loads it and raises
  `ValueError` if any `spectrum_id` in it isn't one of the study's own
  spectra, or if required columns are missing.
- `models/common/model_runner.py:run` (the shared harness for
  flare/glacier/iceberg/jestr) does
  `for candidate in candidates: spectrum = by_id[candidate["spectrum_id"]]`.
  A 5000-spectrum study with thousands of candidate rows is just iterated
  row by row; nothing needs pre-grouping, trimming, or picking "the right
  candidates for this spectrum" manually - the dict lookup makes
  cross-matching impossible.
- Per-candidate eligibility is computed by `_reason()` in the same file and
  skips (never silently drops) candidates missing `precursor_mz`, peaks,
  SMILES, a multi-component SMILES (`"." in smiles`), or a missing adduct -
  each with a specific string written to `eligibility.parquet`.

## Adduct conversion (there is no lookup table)

`models/sirius/main.py:run` is what generates `candidates.parquet` in the
first place: it parses SIRIUS's `structure_identifications*.tsv`, maps each
`featureId` back to its originating `spectrum_id` via `feature_mapping`,
and writes `CANDIDATE_SCHEMA` rows directly.

The only conversion applied is `_normalize_adduct()` in that same file:
strip spaces from SIRIUS's `"[M + Na]+"` to `"[M+Na]+"`. That's the entire
transform - SIRIUS and the scoring models (via ms_pred's `ion2mass`/
`ion2onehot_pos` tables) already use the same adduct vocabulary once
whitespace is removed, so nothing else is needed.

From there the adduct string flows verbatim:
`model_runner.py:run` reads `candidate.get("adduct")` (falling back to
`spectrum.get("declared_adduct")`), and passes it straight into
`BACKEND_INPUT_SCHEMA`'s `adduct` field with no further translation.

## Debugging a candidate/adduct-shaped error

1. Read the model's `run_report.json` counts and `eligibility.parquet`
   reasons first - the CLI's JSON already surfaces these.
2. `missing_adduct` in eligibility reasons means the candidate's `adduct`
   field is genuinely null/empty for that row, not a format mismatch - check
   the candidate's source (SIRIUS structure_identifications output, or
   `prepare_workbench_candidates.py` for the manual-candidate studies) for
   that spectrum specifically.
3. `Candidate Parquet is missing: ...` means `candidates.parquet` doesn't
   satisfy `CANDIDATE_SCHEMA` - usually the parser's own candidate sidecar
   (different, narrower schema) was used instead of a real model-facing one.
4. `Candidates reference unknown spectrum_id` means `candidates.parquet` was
   built against a different `spectra.parquet` than the one being scored
   (stale file, wrong study, or wrong run).
