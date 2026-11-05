# MWExtract decision record

This file records the planning decisions retained from the project conversation.
It is a decision log, not a verbatim export of hidden planning state. The visible
conversation and retained conversation summary are the sources. Implementation
details are included only where they clarify an agreed boundary.

## Extraction layout

**User direction:** Paths below a Workbench study accession are not uniform, so
the Workbench directory hierarchy should not be reproduced under `extracted`.

**Decision:** Flatten the extracted files into a single `spectra` directory for
each study.

**User direction:** The extraction container should only extract MS/MS files.
File-specific metadata collection will be handled by a separate image.

**Decision:** A study deliverable is organized under `outputfiles/ST00****/` and
contains:

- `spectra/`, containing every extracted spectral file without the variable
  Workbench path suffix.
- `ST00****.parquet`, containing only the study-wide metadata rows selected
  from the source Parquet data for that study.

**Decision:** Ionization mode, collision energy, instrument type, precursor ion,
and similar scan-specific information are not treated as study-wide metadata
when they occur inside individual mzML or mzXML files. Parsing those fields is
the responsibility of later format-specific processing images.

## Format-specific processing images

**User direction:** Build separate processing images for mzML and mzXML.

**Decision:** Maintain mzML and mzXML as separate Docker image directories and
Dockerfiles for clear ownership and long-term maintenance. Each parser extracts
spectra and available file-specific metadata, including:

- Precursor m/z and charge
- Collision energy and its unit, when present
- Ionization or adduct evidence, when present
- Instrument information, when present
- Precursor or molecular formula evidence, when present
- Peak m/z and intensity arrays

**Decision:** Missing metadata remains explicitly missing. The parser must not
present inferred values as values observed in the source file.

**Decision:** Both parsers emit a common model-neutral Parquet contract so model
containers do not need to understand mzML or mzXML directly.

## Annotation model scope

**User direction:** Start the model-container batch with FLARE, GLACIER,
ICEBERG, and SIRIUS, including model-specific preprocessors.

**Decision:** Model-specific format conversion belongs inside each model image,
behind a common Parquet input and output contract.

**User direction:** Do not proceed with SIRIUS yet.

**Decision:** Implement and validate FLARE, GLACIER, and ICEBERG first. Leave the
existing SIRIUS work unchanged until it is explicitly resumed.

## Model weights

**User direction:** Use each model's MassSpecGym weights by default, excluding
SIRIUS.

**Decision:** FLARE, GLACIER, and ICEBERG accept exactly one explicit weight
source per invocation:

- `--weights massspecgym` selects the official released default.
- `--checkpoint-bundle` selects a user-provided bundle.

The options are mutually exclusive and one is required. This avoids silently
downloading or silently changing weights.

**Decision:** Official weights are downloaded on demand at runtime and are not
baked into Docker images. `/model-cache` is the persistent cache location.

**Decision:** Downloads use pinned sources and SHA-256 checksums. Provisioning
uses a lock, an atomic temporary directory, safe archive extraction, and
revalidates cached files before every inference invocation.

**Decision:** The official default assets are:

- FLARE: released MassSpecGym `flare.ckpt`.
- GLACIER: released MassSpecGym checkpoint.
- ICEBERG: ICEBERG 2.1 `msg_all` generation and contrastive-intensity
  checkpoints.

**Decision:** SIRIUS does not use a learned checkpoint in the same way as these
neural models. Its databases, scoring system, and optional predictors require a
separate design decision later.

## Invocation and orchestration boundary

**User direction:** Do not create scripts that repeatedly call the containers.
An ADK agentic harness will provide orchestration.

**Decision:** Every image performs one deterministic invocation. Images do not
implement campaign loops, workflow scheduling, retries, model sequencing, or
an agent control plane.

**Decision:** The future ADK harness may select studies, invoke containers,
retry failures, aggregate evidence, and prioritize review. Deterministic tasks
remain in code and containers:

- Parsing and preprocessing
- Weight verification
- Candidate scoring
- Ranking
- Collision-energy search
- Leakage checks
- Output validation

## Candidate scoring

**Question:** What do the reported smoke-test scores represent?

**Decision:** FLARE emits its native retrieval compatibility score. GLACIER and
ICEBERG predict spectra for candidate structures, after which the adapter
computes spectral-entropy similarity between predicted and observed spectra.
Higher scores are better within a model, but raw scores are not comparable
between different models.

**Decision:** Rank candidates only against competing candidates for the same
spectrum and model. A single-candidate run always produces rank 1 and therefore
does not validate the candidate identity.

**Decision:** Preserve native score, within-spectrum rank, candidate-set size,
top-candidate margin, collision energy, collision-energy source, checkpoint
fingerprint, and preprocessing version.

**Decision:** If collision energy is missing, search the configured energy grid
and label the selected value `PREDICTED_GRID_SEARCH`. Never report a searched
energy as observed metadata.

**Decision:** Instrument type is an explicit invocation input. It currently
defaults to `Orbitrap`, matching the released MassSpecGym checkpoint inputs,
and is recorded in the run report.

## Discovery and benchmark-building goal

**User goal:** Process every suitable Workbench study and produce a large list,
potentially around 10,000, of strong spectrum-molecule matches that could
support future model evaluation benchmarks.

**Decision:** Model agreement creates candidates for review, not ground truth.
FLARE, GLACIER, and ICEBERG may share training data, representations, candidate
databases, and systematic errors.

**Decision:** Each proposed pair should retain:

- Study, source file, scan, and stable spectrum identifiers
- Candidate structure, molecular formula, adduct, precursor m/z, and charge
- Observed or inferred collision energy and its provenance
- Per-model raw scores, ranks, candidate counts, and margins
- Model agreement and disagreement
- Spectrum-quality evidence
- Checkpoint hashes and preprocessing versions
- Training and benchmark leakage results
- Supporting identification evidence
- Confidence tier and inclusion rationale

**Decision:** Use confidence tiers such as:

- Gold: authenticated standard or comparably strong orthogonal confirmation
- Silver: strong curated metadata, spectral evidence, and model support
- Candidate: computationally promising but not independently confirmed
- Rejected or uncertain: retained for auditability but excluded from evaluation

**Decision:** A smaller defensible collection is more valuable than 10,000
weak predictions. Benchmark admission should be controlled by a fixed policy or
human review, not by an agent's unconstrained judgment.

## Training-data strategy

**Question:** Should each model be trained on a different source such as
MassSpecGym, NIST23, MassIVE, or MoNA?

**Decision:** Not for the primary architecture comparison. Train compatible
models on the same controlled training split and evaluate them on the same
untouched test set. Otherwise architecture and training-data effects are
confounded.

**Decision:** Use source-specific training as a secondary ablation and ensemble
diversity experiment. The intended experiment families are:

1. Common MassSpecGym-trained baselines for fair model comparison.
2. Source-specific variants for measuring dataset effects.
3. A deduplicated, license-compatible multi-source production model.
4. Leave-one-source-out variants for quantifying each source's contribution.

**Decision:** Treat the sources according to their actual roles:

- MassSpecGym supplies a cleaned benchmark and structure-aware predefined split.
- NIST23 supplies a large curated commercial library, subject to its license.
- MoNA supplies an open spectral library that requires independent
  deduplication and leakage-resistant splitting.
- MassIVE supplies heterogeneous experimental data, much of which is unlabeled
  or putatively annotated and cannot automatically be treated as supervised
  ground truth.

**Decision:** Freeze a Workbench evaluation subset before retraining models.
Remove exact spectra, exact structures, connectivity matches, close structural
analogs under a declared rule, and spectra mirrored across repositories from
all relevant training data.

**Decision:** Report official-weight results separately from retrained
multi-source results. This preserves a reproducible baseline and shows whether
additional training data genuinely improves performance.

## Current implementation status

The agreed FLARE, GLACIER, and ICEBERG path is implemented and validated with
official released assets. The Docker images remain single-invocation tools.
SIRIUS planning and implementation are intentionally deferred.
