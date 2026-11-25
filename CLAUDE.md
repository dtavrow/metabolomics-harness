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
   (Two independent cross-process locks already cap concurrency, per
   `app/concurrency.py`: `sirius` gets exactly one slot, always, not
   configurable; every other model shares a second pool sized by
   `ANALYSIS_MAX_CONCURRENCY` in `.env` (default 1). So at most one sirius
   container and one other-model container ever run at the same time --
   you don't need to self-limit, just fire them concurrently and let the
   locks queue the rest.) Report each model's outcome as it comes back.

   **Check for GPU access before running `flare`/`glacier`/`iceberg`/`jestr`.**
   Run `nvidia-smi` once per session. If it succeeds (a GPU is present),
   always run these four models on GPU rather than CPU -- CPU-only mode
   disproportionately hurts `glacier`/`iceberg` in particular, since both
   are GNN-based fragment-tree models that lean heavily on GPU parallelism
   for what is otherwise their main wall-clock cost. Concretely, when a GPU
   is available:
   - Check `docker image inspect mwannotate-<model>:cuda` for each of the
     four. For any that's missing, build it yourself before running that
     model -- don't ask the user to do it and don't silently fall back to
     `:cpu`. Use the exact `--build-arg` flags in `models/README.md` for
     that model. For `glacier`/`iceberg` specifically this is
     `--build-arg MSPRED_EXTRA=cu118` -- stay on `cu118`, not a newer
     `cu124`/etc. extra, unless you've first confirmed the `data.dgl.ai`
     outage documented in `models/common/patch_mspred_cu118.py` is
     resolved (the `cu118` path has a working conda-based fallback for that
     outage; other extras don't). Tag every GPU build `:cuda`.
   - Pass `--device cuda` **and** set that model's `MWANNOTATE_<MODEL>_IMAGE`
     env var to its `:cuda` tag (e.g. `MWANNOTATE_GLACIER_IMAGE=mwannotate-glacier:cuda`)
     on every `run-model` call once the `:cuda` image exists -- `--device`
     alone only toggles the in-container flag and `docker run --gpus=all`;
     it does **not** change which image tag gets pulled (`app/pipeline_config.py`'s
     `DEFAULT_MODEL_IMAGES` does that, overridable only via this env var).
     The `:cpu` image has CPU-only torch baked in, so running it with
     `--device cuda` fails or silently gets no GPU benefit -- both flags are
     required together, never just one.
   - If `nvidia-smi` fails (no GPU, no driver), use the default `:cpu`
     images and `--device cpu` as normal -- this is the common case, not an
     error.
   - `--batch-size` defaults to 8 for `glacier`/`iceberg` and 32 for
     `flare`/`jestr` (set in `app/tools/analysis_tools.py`, chosen for this
     box's 6GB GTX 1060 -- glacier/iceberg's GNN batches are far heavier per
     item). If a GPU run OOMs, lower `--batch-size` for that model first
     before touching anything else.

   **`glacier`/`iceberg` also accept `--num-cpu-workers <N>`** on
   `run-model`, independent of the GPU decision above -- it parallelizes
   their upstream `ms-pred` fragment-enumeration preprocessing step (serial
   by default: 0) across `N` host CPU workers. Only raise it when the host
   actually has spare CPU headroom right now (check current load/`nproc`
   first); 4 is a reasonable upper bound on a shared machine, not a value to
   always pass. `run-model` rejects this flag for every other model
   (`flare`/`jestr`/`sirius`/`evidence`) -- it has no effect there.

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

   `sirius`'s own stdout (the `report` field) names two self-correcting
   guardrails when they fire -- relay them to the user plainly, they are
   informational, not failures, and never a reason to retry:
   - `filtered N candidates with chemically invalid SMILES` -- SIRIUS/
     CSI:FingerID occasionally reports a structure RDKit can't parse (e.g.
     a hypervalent atom); these are dropped before `candidates.parquet` is
     written, at the source, instead of reaching flare/glacier/iceberg/
     jestr. A handful per study is normal background noise from the
     PubChem lookup; treat a rate of more than a few percent of a study's
     candidates as worth investigating (grep `source_database` on the
     dropped SMILES to see which upstream database they came from).
   - `capped N spectra to the top <k> candidates` -- a spectrum whose
     molecular-formula hypotheses collectively produced more structures
     than `--top-k` (default 50) had its candidate pool truncated to the
     best `--top-k` by SIRIUS's own rank/confidence, same as if SIRIUS had
     only ever returned that many. This is the per-spectrum candidate cap
     that keeps one pathological spectrum from ballooning a whole study's
     `candidates.parquet` (and every downstream model's runtime); it is
     always on and does not need configuring per study.

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
- Only ever one `sirius` container and one other-model container run at a
  time, full stop -- even across different studies or sessions. This is
  enforced in code (`app/concurrency.py`'s two separate lock pools), not
  just by convention, so launching several `run-model` calls concurrently
  is safe; the locks queue whatever doesn't fit.
- A study whose raw + processed footprint already exceeds
  `STUDY_DISK_BUDGET_BYTES` (default 200GB) is refused by `fetch` and every
  `run-model` with `status: "oversized_skip"`, and recorded in
  `pipeline_runs/agent_runs/oversized_studies.json` plus its own ledger.
  Treat this as a permanent skip, not a transient refusal -- don't retry it,
  and don't work around it by raising the budget without the user's say.
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

## Known issues

- **SIRIUS project-space bloat.** `models/sirius/main.py`'s `--top-k-summary`
  (passed as `args.top_k`, default 50) only truncates the exported TSV
  summaries and `candidates.parquet` -- not what SIRIUS writes into the
  `.sirius` project file (a MapDB-backed store) while computing. Confirmed:
  ST004966 (22,066 eligible spectra, only ~3x the next-largest study) produced
  a 207G project file -- ~9.4MB/compound vs. ~0.2-0.3MB/compound for every
  other study -- so this was a real blowup, not just "a bigger dataset."
  Checked `sirius formulas --help` / `sirius structures --help` directly
  (`docker run --rm --entrypoint sirius mwannotate-sirius:6.5.4 <step>
  --help`) rather than guessing:
  - `formulas` has its own `-c, --candidates=<N>` (undocumented default: 10).
    `main.py` wasn't passing it at all. **Fixed**: now passes
    `--candidates <top_k>` so the formula-candidate cap is explicit and
    tracks `--top-k-summary` instead of silently relying on SIRIUS's own
    default.
  - `structures` (the CSI:FingerID database search step) has **no**
    equivalent per-compound candidate-cap flag -- only `-d/--db` and
    `-e/--exp`. There is no CLI knob to shrink what it writes into the
    project space. The 50-candidate cap for structures only exists
    downstream, via `--top-k-summary`/`candidates.parquet` (which already
    carries a `canonical_smiles` column per candidate -- confirmed still
    correct after this change, since `write-summaries`'s flags weren't
    touched).
  - If project-space bloat recurs after this fix, the `formulas --candidates`
    cap isn't the full story -- look at `structures`/CSI:FingerID itself, or
    at whether a study got recomputed/retried into the same project file
    (ST004966 also had a stale, uncompacted temp workspace next to the
    published one, suggesting an interrupted prior attempt).

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
