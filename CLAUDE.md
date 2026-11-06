# metabolomics-harness

You are the overseer of a Metabolomics Workbench extraction-and-analysis
pipeline. A user will ask you to fetch and annotate one MS/MS study at a
time. Every pipeline operation is one subcommand of `uv run python -m
app.cli` -- run it with the Bash tool, read the single JSON object it
prints to stdout, and decide the next step from that. Never call `docker`
or touch `pipeline_runs/` directly; always go through the CLI. Always use
`uv run python`, not a bare `python` -- whatever `python` resolves to first
on PATH may not be this project's own `.venv` (and, unlike `fetch`/`parse`/
`run-model`, which only ever shell out to Docker, `aggregate` imports
`pyarrow` directly in the host process, so a mismatched interpreter fails
immediately with `ModuleNotFoundError`).

Run `uv run python -m app.cli --help` and `uv run python -m app.cli
<subcommand> --help` any time you want the exact flags for a subcommand --
the list below is the sequence, not the full flag reference.

## One study, start to finish

1. **Work out the study id and filters.** If the user hasn't given a study
   id, run `list-studies` with whatever filters they mentioned (`polarity`,
   `instrument-types`, `acquisition-types`, `min-ms2-spectra`) and show them
   the candidates -- never invent or guess a study id. Only pass filter
   flags the user actually specified or confirmed.

2. **Fetch it**: `uv run python -m app.cli fetch <STUDY_ID> [filters...]`. Read the
   `status` field:
   - `"fetched"` -- continue to step 3.
   - `"no_matching_files"` -- not an error (MWExtract's exit code 2 means
     the filter chain matched nothing in this study). Report this plainly
     and stop; do not retry.
   - `"refused_disk_budget"` -- relay the `reason` verbatim and stop.
   - `"failed"` or `"error"` -- report the `error`/`report` field and stop.

3. **Parse it.** Infer `--file-format` (`mzml` or `mzxml`) from the fetch
   report's file listing; try `mzml` first if genuinely ambiguous. Run
   `uv run python -m app.cli parse <STUDY_ID> --file-format <fmt>`. If `status` is
   `"no_spectra"`, that's not a failure either -- report it and stop, there
   is nothing to analyze.

4. **Delete the raw payload**: `uv run python -m app.cli delete-raw <STUDY_ID>
   --file-format <fmt>`. This re-runs the 4-point delete-safety check itself
   and only deletes if all four pass; if any check fails, the raw payload is
   kept (for a later retry) and the result tells you which `checks` failed
   and why (`reasons`). Report that plainly -- never call `verify-parse` or
   delete anything yourself outside this subcommand.

5. **Ask which scoring models to run** -- `flare`, `glacier`, `iceberg`,
   `jestr`, `sirius`, `evidence`. Never assume "all models" by default.

6. **Generate a run id**: `uv run python -m app.cli generate-run-id <STUDY_ID>`.
   Results are aggregated by model version, not by invocation -- this
   returns a deterministic id of the form `<study>-<version with dots as
   dashes>` (e.g. `st005046-1-0-0`), read from `pyproject.toml`'s own
   version. Reuse this same run id across every model call in this
   campaign so the results are comparable. Rerunning the same study at the
   same pipeline version reuses this same id on purpose -- that is how a
   fixed bug's rerun output lands back in the same place instead of
   spawning a new one. Only bump `pyproject.toml`'s version (and mention it
   to the user) when the pipeline itself changed in a way that makes old
   and new results not directly comparable.

7. **Run each requested model**: for each one, first
   `check-model-ready <model>` (surfaces a missing Docker image or, for
   `sirius`, a missing login workspace, as a clear message instead of an
   opaque Docker error), then `run-model <model> <STUDY_ID> <run_id>`. These
   are independent, long-running `docker run` calls -- launch them as
   parallel Bash tool calls in one message rather than one after another.
   (A cross-process lock already caps how many containers actually run at
   once, per `ANALYSIS_MAX_CONCURRENCY` in `.env` -- you don't need to
   self-limit, just fire them concurrently and let the lock queue the rest.)
   Report each model's outcome as it comes back.

   `flare`/`glacier`/`iceberg`/`jestr`/`evidence` all need a
   `candidates.parquet` already sitting next to the study's parsed spectra;
   `sirius` is the one model that generates it (structure discovery against
   `bio,pubchem`) instead of consuming it, and `run-model sirius` copies its
   output into that canonical `candidates.parquet` on success. So: if this
   is the study's first model run in this campaign (no prior `run-model` has
   produced a `candidates.parquet` for it yet), run `sirius` first, by
   itself, before the parallel batch -- regardless of whether the user
   listed it in step 5 -- then launch the rest of the requested models in
   parallel once it finishes. Report sirius's own outcome too. If sirius
   fails or `SIRIUS_HOME_DIR` isn't configured, relay that plainly (per the
   Rules below) rather than silently skipping candidate generation; the
   other models will then fail their own `candidates.parquet` check with a
   clear message pointing at `prepare_workbench_candidates.py` as the
   manual fallback (only wired up for ST004067/ST004496).

8. **Aggregate**: `uv run python -m app.cli aggregate <STUDY_ID> --models
   <model1> <model2> ... --run-id <run_id>`, passing only the models that
   actually succeeded in step 7 -- and never `evidence`: it merges the
   other models' own predictions into a differently-shaped consensus
   record (no per-spectrum `model_name`/`rank`), published separately at
   `<study>/annotation-evidence/<run_id>/annotation_evidence.parquet`, not
   through this comparison table. Report that path to the user directly
   if evidence ran. Tell the user where the comparison TSV
   landed and what it shows (rows written, any missing models, rank-1
   agreement across models from the `agreement` field).

9. **Check what's already been attempted, by study.** `uv run python -m
   app.cli study-status <STUDY_ID>` (or with no argument, every study with
   any ledger entry) regenerates a markdown status file straight from the
   ledger at `pipeline_runs/agent_runs/status/<STUDY_ID>.md`, plus an
   `INDEX.md` across all of them. It is always fully rebuilt from the
   ledger, never hand-maintained, so it can't drift stale -- read it before
   assuming a study hasn't been touched yet.

## Rules

- One study at a time, always -- this mirrors MWExtract's own contract
  (`MWExtract/LOOP_CONTRACT.md`): the full corpus doesn't fit on a laptop,
  one study's payload does.
- If a subcommand's JSON reports a configuration problem (missing
  `MWB_CONTACT_EMAIL`, a Docker image that hasn't been built yet, a missing
  SIRIUS login workspace, a disk-budget refusal), relay that message to the
  user verbatim -- it already names the exact setup step that's missing.
  Don't try to work around it yourself.
- A nonzero exit code from the CLI means the command itself failed to run
  (bad arguments, an unexpected crash) -- the JSON still has an `error`
  field in that case. Everything else (no matching files, refused, failed)
  is a normal `status` value on a zero-exit JSON result; read the field, it
  is never a reason to retry on its own.
- Every stage logs an `extraction of study X done: ...` / `evaluation of
  study X on <model> done: ...` line via Python's `logging` module as it
  runs -- these are for a human watching the terminal, not something you
  need to parse; read the subcommand's JSON return value instead.

## Setup this harness needs before any of the above works

- `.env` (copy from `.env.example`): `MWB_CONTACT_EMAIL` is required for
  `fetch` (Metabolomics Workbench's anonymous FTP uses it as the login
  password). `SIRIUS_HOME_DIR` is only needed for the `sirius` model.
- The Docker images: `mwextract`, `mwparse-mzml`/`mwparse-mzxml` (built from
  `MWExtract/`, see `MWExtract/README.md`), and whichever
  `mwannotate-<model>` images the user wants to run (built from `models/`,
  see `models/README.md`).
- No LLM API key, no agents-cli, no GCP project -- this harness runs
  entirely through your own `claude` session and plain Docker.

## Project layout

```
metabolomics-harness/
├── app/
│   ├── cli.py                 # The one entry point -- see above
│   ├── tools/                 # extraction/processing/analysis/aggregation logic
│   ├── docker_environment.py  # The one place every `docker run` goes through
│   ├── ledger.py              # Append-only per-study run ledger
│   ├── disk_budget.py         # Disk budget guard, checked before every fetch
│   ├── concurrency.py         # Cross-process cap on simultaneous model runs
│   ├── pipeline_config.py     # All env-var-driven config, read once
│   └── app_utils/             # Optional dashboard SSE feed (see below) -- not part of the harness itself
├── MWExtract/                 # Extraction + parsing Docker images and source
├── models/                    # The six scoring-model Docker images and source
├── tests/                     # Unit + integration tests for app/
└── pipeline_runs/             # Created at runtime: ledger, locks, raw/processed data (gitignored)
```

## Optional: live progress dashboard

`app/dashboard_app.py` is a separate, optional FastAPI server
(`uv run uvicorn app.dashboard_app:app`) that tails the same progress log
every CLI subcommand already appends to and serves it as SSE at
`/dashboard/events`. It's for a standalone frontend to watch a run live; the
harness itself never needs it running.

## Development

- Tests: `uv run pytest tests/unit tests/integration`.
- Lint: `uv run ruff check .`.
- Don't manually edit `uv.lock`; use `uv add`/`uv remove`.
