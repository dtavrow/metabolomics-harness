# Annotation model images

This directory is deliberately separate from `MWExtract`. These images consume
the model-neutral `spectra.parquet` emitted by either parser. Model-specific
conversion stays in each model's `preprocess.py`.

## Images

Build from the repository root:

```text
docker build -f models/flare/Dockerfile -t mwannotate-flare:cpu .
docker build -f models/glacier/Dockerfile -t mwannotate-glacier:cpu .
docker build -f models/iceberg/Dockerfile -t mwannotate-iceberg:cpu .
docker build -f models/jestr/Dockerfile.cpu -t mwannotate-jestr:cpu .
docker build -f models/jestr/Dockerfile -t mwannotate-jestr:cuda .
docker build -f models/sirius/Dockerfile -t mwannotate-sirius:6.5.4 .
docker build -f models/evidence/Dockerfile -t mwannotation-evidence:1 .
```

The SIRIUS image installs the official `sirius-ms` 6.5.4 Linux x64 release
archive during the image build and verifies its pinned SHA-256 checksum
(`9556237eb8306f27dede2b7e3404952e02729a052b87fe942857bd26d20c9d63`). Runtime
containers do not clone the SIRIUS repository or download the executable.

For FLARE CUDA images, provide the matching PyTorch CUDA wheel index with
`--build-arg TORCH_INDEX_URL=...`. For GLACIER and ICEBERG, set
`--build-arg MSPRED_EXTRA=cu124` or `cu118`. A CUDA-capable base may also be
selected with `--build-arg BASE_IMAGE=...`. Tag GPU builds with `:cuda` and run
them with `docker run --gpus ... --device cuda`. CPU remains the CLI default.

Each learned-model image downloads and checksum-verifies its official public
MassSpecGym weights during `docker build`. Normal runs never clone repositories
or download checkpoints. Use `--checkpoint-bundle /checkpoint` to override the
baked default. The legacy `--weights massspecgym` flag remains accepted.

JESTR uses the released MassSpecGym molecule and spectrum encoders with stamp
`1741546103623`, pinned to JESTR1 commit
`a5619c18a85a49171d60ead079684ae667cc0dd0`.

Use `--instrument-type` when the acquisition instrument is known. It defaults
to `Orbitrap`, matching the MassSpecGym checkpoint inputs, and is recorded in
the run report.

```text
docker run --rm \
  -v /local/output:/data \
  mwannotate-iceberg:cpu \
  --spectra /data/ST004067/processed/mzxml/spectra.parquet \
  --candidates /data/candidates.parquet \
  --output-root /data --run-id iceberg-msg-all-001
```

FLARE uses the released `flare.ckpt`, GLACIER uses its released MassSpecGym
checkpoint, and ICEBERG 2.1 uses the `msg_all` generation and contrastive
intensity checkpoints. Build downloads are atomic, and all assets are checked
against pinned SHA-256 values. Baked bundles are checked again before every
inference run.

## Preprocessing

Each model owns an executable preprocessing module:

- `flare/preprocess.py` writes the query TSV and formula candidate JSON.
- `glacier/preprocess.py` writes GLACIER's ms-pred labels.
- `iceberg/preprocess.py` writes ICEBERG's ms-pred labels.
- `jestr/preprocess.py` filters peaks above 1000 m/z and applies the training
  normalization `intensity / max * 999`.
- `sirius/preprocess.py` writes `.ms` features and stable feature mappings.

`prepare_workbench_candidates.py` is the audited campaign adapter for
ST004067 and ST004496. It matches deposited features to parsed spectra by
precursor m/z and retention time, resolves ST004067 accurate-mass candidates
through MW-MetRef and PubChem, and converts ST004496's submitted metabolites
to the shared candidate parquet contract. Its network resolution cache is
persistent, so interrupted metadata resolution resumes without repeating
completed requests.

All single-molecule scoring models explicitly skip salts and other
multi-component SMILES while retaining those records in the source candidate
set and eligibility output. GLACIER inference disables upstream structure
deduplication so every requested spectrum-candidate identity receives its own
backend result.

Entry points invoke these conversions internally. The modules are also
standalone CLIs for inspecting model-native inputs.

A custom checkpoint directory must contain `model-manifest.json`. The manifest
is validated before inference, including the checksum of every listed file.
Its `inference_command` is executed without a shell and may use these
placeholders:

```text
{input}              prepared backend_input.parquet
{output}             required backend_predictions.parquet
{checkpoint_bundle}  mounted checkpoint directory
{device}              cpu or cuda
{batch_size}          requested batch size
```

This command is the small version-specific bridge between the stable container
contract and an upstream checkpoint. It must write exactly one row for every
requested `(spectrum_id, candidate_id, collision_energy)` tuple, with columns
`spectrum_id`, `candidate_id`, `collision_energy`, and `raw_score`. Bundle files
that implement the bridge must also be listed and checksummed in the manifest.
This keeps arbitrary checkpoint layouts modular without silently guessing how
a weight file was trained.

## SIRIUS discovery

SIRIUS first converts each eligible spectrum to a `.ms` feature and keeps a
mapping back to the parser's stable spectrum ID. A normal academic discovery
run is:

```text
docker run --rm \
  -v /local/output:/data \
  -v /local/sirius-home:/root/.sirius-6.5:ro \
  mwannotate-sirius:6.5.4 \
  --spectra /data/ST004067/processed/mzxml/spectra.parquet \
  --output-root /data --run-id discovery-001
```

SIRIUS keeps its workspace, including the login refresh token, in a
version-pinned directory (`<HOME>/.sirius-<major>.<minor>`), not a bare
`.sirius`. For the pinned `SIRIUS_VERSION` above that is `/root/.sirius-6.5`;
if that mount path and the installed SIRIUS version ever drift apart, the
container finds no workspace and every discovery run fails with a login
error even though you are already logged in. Produce the login state once
against the same image so the version always matches:

```text
docker run --rm -it --entrypoint sirius \
  -v /local/sirius-home:/root/.sirius-6.5 \
  mwannotate-sirius:6.5.4 login -u you@your-institution.edu -p
```

The default databases are `bio,pubchem`. Login state is mounted at runtime and
is never copied or reported. `--prepare-only` tests conversion and eligibility
without credentials or network access.

SIRIUS writes `candidate_sets.parquet`. Pass that file, or a validation set
using the same schema, to the other models:

```text
docker run --rm -v /local/output:/data -v /local/glacier:/checkpoint:ro \
  mwannotate-glacier:cpu \
  --spectra /data/ST004067/processed/mzxml/spectra.parquet \
  --candidates /data/ST004067/annotations/sirius/discovery-001/candidate_sets.parquet \
  --checkpoint-bundle /checkpoint --output-root /data --run-id open-msg-001
```

These are single-invocation images. Campaign sequencing, retries, scheduling,
and cross-model orchestration belong to the calling agent harness and are not
implemented here.

Missing or unusable collision energy triggers the default 10, 20, 30, 40, and
50 eV search. Every tested value and score is retained. The selected value is
always labeled `PREDICTED_GRID_SEARCH`, never observed.

## Evidence output

After model runs finish, the evidence image accepts one run directory per
model and the checkpoint bundles used for the neural models. It writes:

- `annotation_evidence.parquet`, including native scores, ranks, and the mean
  within-candidate-set rank score across available models.
- `acquisition_quality.parquet`, preserving SIRIUS GOOD, DECENT, or BAD labels
  and objective peak-list metrics without inventing a numeric conversion.
- `eligibility.parquet`, with explicit skipped and failed reasons.
- `leakage.parquet`, with exact structure, connectivity, and spectrum checks.
  A check is `unknown` when the checkpoint lacks a training identity table.

The output is evidence for later human review. It never admits a pair to a
benchmark or applies an acceptance threshold.
