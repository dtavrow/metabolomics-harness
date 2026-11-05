# metabolomics-harness

A Claude Code-driven pipeline for annotating Metabolomics Workbench MS/MS
studies. Point it at a study accession (e.g. `ST004496`), and it drives
three stages end to end, each running in its own Docker container:

1. **Extraction** -- fetches one study's MS/MS payload from Metabolomics
   Workbench over anonymous FTP (`MWExtract/`), respecting a one-study-at-a-time
   contract and a disk budget.
2. **Processing** -- parses the fetched payload into a shared
   spectra/candidates Parquet format, runs a 4-point delete-safety check, and
   deletes the raw payload once verified (`MWExtract/parsers/`).
3. **Scoring** -- runs any of six spectrum-annotation models (FLARE, GLACIER,
   ICEBERG, JESTR, SIRIUS, an MS/MS evidence model) against the parsed
   candidates, concurrently, bounded by a concurrency cap (`models/`).

There is no agent server, no SDK, and no LLM API key in this repo. You drive
the pipeline by opening a `claude` (Claude Code) session in this directory
and talking to it -- `CLAUDE.md` tells it exactly how to call
`python -m app.cli` for each stage. It runs on your own Claude Code
subscription, not a metered API key, and under your own Metabolomics
Workbench contact email -- nothing shared, nothing hosted.

## Project layout

```
metabolomics-harness/
├── CLAUDE.md         # The actual orchestration instructions Claude Code reads
├── app/
│   ├── cli.py        # The one entry point: python -m app.cli <subcommand>
│   ├── tools/        # extraction/processing/analysis/aggregation logic
│   └── app_utils/    # Optional live-progress dashboard backend (not required to run the harness)
├── MWExtract/        # Extraction + parsing Docker images and source
├── models/           # The six scoring-model Docker images and source
├── tests/            # Unit and integration tests
└── pipeline_runs/    # Created at runtime: ledger, locks, raw/processed data (gitignored)
```

`MWExtract/README.md` and `models/README.md` are the authoritative build docs
for those two trees -- read them before building images, especially for GPU
builds or SIRIUS's extra login step.

## Requirements

- **Docker** -- every pipeline stage runs in a container; nothing calls a
  tool on your host directly.
- **[Claude Code](https://code.claude.com/)** -- `npm install -g
  @anthropic-ai/claude-code`, then `claude login` with your Anthropic
  account (a Pro/Max subscription covers this; no separate API key needed
  to use this repo).
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** -- Python
  package manager used for all dependency management here.
- **A contact email** -- Metabolomics Workbench's anonymous FTP uses this as
  the login password. Use your own institutional or personal email; nothing
  else is sent.

## Setup

1. Clone the repo and install Python dependencies:

   ```bash
   git clone <this-repo-url>
   cd metabolomics-harness
   uv sync
   ```

2. Configure your own Workbench contact email:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and set `MWB_CONTACT_EMAIL`. SIRIUS additionally needs a
   one-time login workspace (`SIRIUS_HOME_DIR`) -- see `models/README.md`.
   Skip this if you don't plan to run the `sirius` model; every other model
   works without it.

3. Build the Docker images you need. At minimum, extraction + processing:

   ```bash
   cd MWExtract
   docker build -f extractor/Dockerfile -t mwextract:2026-09-26 .
   docker build -f parsers/mzml/Dockerfile -t mwparse-mzml:latest .
   docker build -f parsers/mzxml/Dockerfile -t mwparse-mzxml:latest .
   cd ..
   ```

   Then whichever scoring models you want (CPU builds shown; see
   `models/README.md` for CUDA builds and the SIRIUS checksum/login steps):

   ```bash
   docker build -f models/flare/Dockerfile -t mwannotate-flare:cpu .
   docker build -f models/glacier/Dockerfile -t mwannotate-glacier:cpu .
   docker build -f models/iceberg/Dockerfile -t mwannotate-iceberg:cpu .
   docker build -f models/jestr/Dockerfile.cpu -t mwannotate-jestr:cpu .
   docker build -f models/sirius/Dockerfile -t mwannotate-sirius:6.5.4 .
   docker build -f models/evidence/Dockerfile -t mwannotation-evidence:1 .
   ```

   (Each image tag here matches what `app/pipeline_config.py` expects by
   default. If you retag a build, override it via the matching
   `MWEXTRACT_IMAGE` / `MWPARSE_MZML_IMAGE` / `MWANNOTATE_<MODEL>_IMAGE` env
   var in `.env`.)

4. Run it -- just open Claude Code in the repo and ask for what you want:

   ```bash
   claude
   > fetch and score ST004496 with iceberg and jestr
   ```

   `CLAUDE.md` has the full step-by-step contract it follows. Progress lines
   (`extraction of study X done`, `evaluation of study X on jestr done`, ...)
   print to the terminal as each Docker stage finishes.

5. Run the test suite:

   ```bash
   uv run pytest tests/unit tests/integration
   ```

## Calling the pipeline directly (without Claude Code)

Every stage is also a plain CLI subcommand, in case you want to script a run
yourself instead of asking Claude Code to drive it:

```bash
uv run python -m app.cli fetch ST004496 --polarity negative
uv run python -m app.cli parse ST004496 --file-format mzml
uv run python -m app.cli delete-raw ST004496 --file-format mzml
uv run python -m app.cli generate-run-id ST004496
uv run python -m app.cli run-model iceberg ST004496 <run-id>
uv run python -m app.cli aggregate ST004496 --models iceberg --run-id <run-id>
```

Each subcommand prints one JSON object to stdout. Run
`uv run python -m app.cli --help` for the full list.

## Optional: live progress dashboard

`app/dashboard_app.py` is a small, separate FastAPI server that tails the
same progress log the CLI already writes and serves it as Server-Sent
Events at `/dashboard/events`, for a standalone frontend to show a run live
(e.g. behind a Cloudflare tunnel, with a Next.js frontend on Vercel). It is
entirely optional -- the harness above works with it running or not.

```bash
uv run uvicorn app.dashboard_app:app --host 0.0.0.0 --port 8000
```
