# metabolomics-harness

An agentic, containerized pipeline that turns a Metabolomics Workbench
study accession into annotated MS/MS spectra. Point it at a study (e.g.
`ST004496`), and [Claude Code](https://code.claude.com/) drives extraction,
processing, and scoring end to end -- fetching the payload over anonymous
FTP, parsing it into a shared Parquet format, generating candidate
structures with SIRIUS, and scoring them against up to five independent
spectrum-annotation models.

There is no agent server, no SDK, and no LLM API key baked into this repo.
`CLAUDE.md` is the actual contract the agent follows -- every operation is
one typed CLI subcommand (`python -m app.cli ...`) that returns a single
JSON object, so the agent's job is to read that JSON and decide the next
step, not to improvise shell commands.

## How it works

```
 fetch           parse            delete-raw         run-model         aggregate
(FTP, disk   →  (shared Parquet →  (4-point safety →  (SIRIUS +    →   (campaign
 budget         format, candidate   check before       up to 4            comparison
 guard)         sidecar)            delete)            scorers)           table)
```

1. **Extraction** -- fetches one study's MS/MS payload from Metabolomics
   Workbench, respecting a one-study-at-a-time contract and a disk budget
   checked before every fetch.
2. **Processing** -- parses the payload into a shared spectra/candidates
   Parquet format, then deletes the raw payload once a 4-point
   delete-safety check passes.
3. **Candidate generation** -- SIRIUS performs structure discovery
   per spectrum (formula ID + CSI:FingerID), producing the candidate
   structures every downstream scorer ranks.
4. **Scoring** -- FLARE, GLACIER, ICEBERG, and JESTR independently score
   those candidates, running concurrently under a cross-process
   concurrency cap; an `evidence` model merges all of them into one
   consensus table.
5. **Aggregation** -- results are merged into a single comparison TSV,
   keyed by a deterministic `<study>-<pipeline-version>` id so a rerun at
   the same version lands back in the same place instead of fragmenting.

## Engineering notes

- **One chokepoint for Docker.** Every container invocation goes through a
  single typed wrapper (`app/docker_environment.py`) -- no raw shell
  strings, every host path validated before mounting, root-owned output
  trees handled explicitly via throwaway root containers rather than
  silently failing on a host-side permission error.
- **Crash-safe state.** An append-only, atomically-written JSON ledger
  (`app/ledger.py`) is the single source of truth for what happened to
  each study; status pages are always regenerated from it, never
  hand-maintained.
- **Guardrails before destructive actions.** A disk-budget check runs
  before every fetch; a 4-point safety check runs before every raw-payload
  delete; a cross-process lock caps concurrent model containers.
- **Deterministic, versioned reproducibility.** Run ids are derived from
  the pipeline's own version (`st005046-1-0-0`, not a random timestamp),
  so results aggregate by model version rather than by invocation.
- **Tested.** Unit and integration tests cover the CLI and the Docker
  orchestration layer with no real Docker daemon required (`uv run pytest
  tests/unit tests/integration`).

## Models

| Model | Role |
| --- | --- |
| [SIRIUS](https://doi.org/10.1038/s41592-019-0344-8) | Per-spectrum structure discovery (candidate generation) |
| [FLARE](https://doi.org/10.64898/2026.01.27.702086) | Candidate scoring |
| [GLACIER](https://doi.org/10.48550/arXiv.2606.29161) | Candidate scoring |
| [ICEBERG 2.1](https://doi.org/10.1101/2025.05.28.656653) | Candidate scoring |
| [JESTR](https://doi.org/10.1093/bioinformatics/btaf354) | Candidate scoring |
| `evidence` | This harness's own consensus model -- merges the others' predictions into one evidence table |

Full citations are in `.claude/skills/setup-harness/SKILL.md`. All credit
for these models goes to the labs and authors who built them.

## Project layout

```
metabolomics-harness/
├── CLAUDE.md         # The orchestration contract Claude Code reads
├── app/
│   ├── cli.py        # The one entry point: python -m app.cli <subcommand>
│   ├── tools/        # extraction/processing/analysis/aggregation logic
│   ├── docker_environment.py  # The one place every `docker run` goes through
│   └── app_utils/    # Optional live-progress dashboard backend
├── MWExtract/        # Extraction + parsing Docker images and source
├── models/           # The six scoring-model Docker images and source
├── tests/            # Unit and integration tests
└── pipeline_runs/    # Created at runtime: ledger, raw/processed data (gitignored)
```

`MWExtract/README.md` and `models/README.md` are the authoritative build
docs for those two trees.

## Dependencies

| What | Required for | Where it goes |
| --- | --- | --- |
| Anthropic / Claude Code account | Running the agent | `claude login` (not `.env`) |
| Metabolomics Workbench contact email | `fetch` | `.env` → `MWB_CONTACT_EMAIL=you@your-institution.edu` |
| SIRIUS academic account | Only the `sirius` model | `.env` → `SIRIUS_HOME_DIR=/absolute/path`, via a one-time login (below) |

**SIRIUS first-time login** (after building `mwannotate-sirius`):

```bash
mkdir -p ~/.sirius-home
docker run --rm -it --entrypoint sirius -v ~/.sirius-home:/root/.sirius-6.5 \
  mwannotate-sirius:6.5.4 login -u you@your-institution.edu -p
```

This needs a real terminal (not a piped/non-interactive shell) and stores
only a refresh token under `~/.sirius-home` -- never in `.env`. Then set
`SIRIUS_HOME_DIR` to that absolute path in `.env`.

## Quick start

```bash
git clone <this-repo-url> && cd metabolomics-harness
uv sync
cp .env.example .env            # then set MWB_CONTACT_EMAIL

# Always required:
cd MWExtract
docker build -f extractor/Dockerfile -t mwextract:2026-09-26 .
docker build -f parsers/mzml/Dockerfile -t mwparse-mzml:1 .
docker build -f parsers/mzxml/Dockerfile -t mwparse-mzxml:1 .
cd ..

# Whichever scoring models you want (CPU shown; see models/README.md for CUDA):
docker build -f models/flare/Dockerfile -t mwannotate-flare:cpu .
docker build -f models/glacier/Dockerfile -t mwannotate-glacier:cpu .
docker build -f models/iceberg/Dockerfile -t mwannotate-iceberg:cpu .
docker build -f models/jestr/Dockerfile.cpu -t mwannotate-jestr:cpu .
docker build -f models/sirius/Dockerfile -t mwannotate-sirius:6.5.4 .
docker build -f models/evidence/Dockerfile -t mwannotation-evidence:1 .

uv run pytest tests/unit tests/integration   # verify

claude
> fetch and score ST004496 with iceberg and jestr
```

`CLAUDE.md` governs everything from there. A `setup-harness` Claude Code
skill (`.claude/skills/setup-harness/`) also walks through this
interactively, checking prerequisites as it goes -- it triggers
automatically on a fresh clone.

## Calling the pipeline directly (without Claude Code)

Every stage is also a plain CLI subcommand:

```bash
uv run python -m app.cli fetch ST004496 --polarity negative
uv run python -m app.cli parse ST004496 --file-format mzml
uv run python -m app.cli delete-raw ST004496 --file-format mzml
uv run python -m app.cli generate-run-id ST004496
uv run python -m app.cli run-model iceberg ST004496 <run-id>
uv run python -m app.cli aggregate ST004496 --models iceberg --run-id <run-id>
```

Each subcommand prints one JSON object to stdout; run
`uv run python -m app.cli --help` for the full list.



```bash
uv run uvicorn app.dashboard_app:app --host 0.0.0.0 --port 8000
```
