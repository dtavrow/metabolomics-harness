---
name: setup-harness
description: "Use this at the start of a session in a fresh clone of this repo, or whenever the user wants to set up, install, configure, or get started running this harness for the first time (e.g. \"set this up\", \"get this running\", \"how do I start\", \"I just cloned this\"). Walks the user through every setup step from README.md's \"Quick start\" and \"Dependencies\" sections and verifies each one before moving on -- do not consider setup finished until every required step's verification check actually passes."
---

# Setup Harness

Drive the user through first-time setup of this repo, one step at a time,
actually running each verification check yourself rather than taking the
user's word for it. **Do not stop or declare setup complete until every
required step below has passed its own check.** If a step fails, explain
what's missing in plain language, tell the user exactly what to do, wait
for them to act, then re-run the check -- loop on that step until it
passes before moving to the next one.

Read `README.md`'s "Dependencies" and "Quick start" sections if you need
the full rationale or exact commands behind any step; this skill is the
condensed, agent-driven version of the same checklist.

## Before you start

Tell the user the following message:
"Hi scientist! Welcome to version 1.0.0 of the metabolomics harness. I am Lomy (Low-Me), an agent that will help
you extract MS2 data from the metabolomics workbench or your personal files and run inference on a suite of machine learning annotation tools. Here is a list of models that I currently support:

- **FLARE** -- Chen YZ, Rushing B, Hassoun S. "FLARE: Fine-grained Learning for Alignment of spectra-molecule REPresentation Enhances Metabolite Annotation." *bioRxiv*. 2026. https://doi.org/10.64898/2026.01.27.702086
- **GLACIER** -- Wang R-X, Wang R, Coley CW. "GLACIER: Rethinking Mass Spectrum Prediction as an Object Detection Problem." *Advances in Neural Information Processing Systems (NeurIPS 2026)*. 2026. https://doi.org/10.48550/arXiv.2606.29161
- **ICEBERG 2.1** -- Wang R, Manjrekar M, Mahjour B, Avila-Pacheco J, Provenzano J, Reynolds E, Lederbauer M, Mashin E, Goldman SL, Wang M, Weng J-K, Plata DL, Clish CB, Coley CW. "Neural Spectral Prediction for Structure Elucidation with Tandem Mass Spectrometry." *bioRxiv*. 2025:2025.05.28.656653. https://doi.org/10.1101/2025.05.28.656653
- **JESTR** -- Kalia A, Chen YZ, Krishnan D, Hassoun S. "JESTR: Joint Embedding Space Technique for Ranking Candidate Molecules for the Annotation of Untargeted Metabolomics Data." *Bioinformatics*. 2025;41(7):btaf354. https://doi.org/10.1093/bioinformatics/btaf354
- **SIRIUS** (structure discovery via CSI:FingerID) -- Dührkop, K., Fleischauer, M., Ludwig, M. et al. "SIRIUS 4: a rapid tool for turning tandem mass spectra into metabolite structure information." *Nature Methods* 16, 299-302 (2019). https://doi.org/10.1038/s41592-019-0344-8
- **evidence** -- this harness's own consensus model, not an external published model; it merges the other models' predictions and SIRIUS's acquisition-quality labels into one evidence table.

This is an open source tool and all credit for these models goes to the engineers and scientists who designed them! 

To get started, I will walk you through the setup. We will need to do the following: check your prerequisites (Docker, uv, Claude Code), sync Python dependencies, set your Metabolomics Workbench contact email in `.env`, build the extraction/processing Docker images, pick and build whichever scoring models you want, optionally log you into SIRIUS if you want that one, and run the test suite to confirm everything actually works before we start."

## The checklist

For each step: run the check. If it passes, say so briefly and move on. If
it fails, explain the gap, give the exact fix, and ask the user to confirm
once they've done it (or do it yourself if it's a pure file edit you can
make, like writing `.env`) -- then re-check before proceeding.

1. **Prerequisites installed.**
   Run `docker ps`, `uv --version`, `claude --version`.
   - `docker ps` failing usually means Docker isn't installed or the daemon
     isn't running -- point the user at https://docs.docker.com/get-docker/.
   - `uv --version` failing means `uv` isn't installed -- point at
     https://docs.astral.sh/uv/getting-started/installation/.
   - `claude --version` failing means the Claude Code CLI isn't installed
     (unlikely if you're running inside it, but check anyway).
   Also ask: has the user run `claude login`? If this session is already
   running, Claude Code is already authenticated -- no need to re-check.

2. **Python dependencies installed.**
   Run `uv sync` from the repo root. Confirm it exits 0 and `.venv/` exists.

3. **`.env` exists with a real contact email.**
   Check whether `.env` exists; if not, run `cp .env.example .env`. Read it
   and check `MWB_CONTACT_EMAIL` is set to a real address, not the example
   placeholder (`you@your-institution.edu`). If it's still the placeholder
   or missing, ask the user for their contact email and write it in
   yourself -- don't just tell them to do it and move on.
   Never ask the user to paste a password here; this field is an email
   address only, used as Metabolomics Workbench's anonymous-FTP password.

4. **Extraction/processing images built.**
   Check `docker image inspect mwextract:2026-09-26`,
   `mwparse-mzml:1`, `mwparse-mzxml:1`. These three are always
   required -- no model runs without them. If missing, offer to run the
   three `docker build` commands from README step 4 (these can take real
   time/disk/network; tell the user that before starting).

5. **Scoring model images -- ask which ones the user actually wants.**
   Never assume "all of them." Ask which of `flare`, `glacier`, `iceberg`,
   `jestr`, `sirius`, `evidence` they want to use now (they can always come
   back and build more later). For each one requested, check
   `docker image inspect <tag>`; if missing, offer to run that model's
   `docker build` command from README step 5. These builds clone external
   repos and download pinned checkpoints -- expect real time, disk, and
   network use, and say so.

6. **SIRIUS login -- only if `sirius` was requested in step 5.**
   If the user doesn't want `sirius`, skip this step entirely and say so.
   If they do:
   - Run `uv run python -m app.cli check-model-ready sirius`. If it already
     reports `"ready": true`, this step is done.
   - If not, the login step needs a real interactive terminal (it will NOT
     work through a piped/non-interactive relay -- if you can't get a real
     TTY in your current execution context, tell the user to run it
     themselves in their own terminal). Walk them through:
     ```bash
     mkdir -p ~/.sirius-home
     docker run --rm -it --entrypoint sirius \
       -v ~/.sirius-home:/root/.sirius-6.5 \
       mwannotate-sirius:6.5.4 login -u <their-email> -p
     ```
   - Verify with `docker run --rm -v ~/.sirius-home:/root/.sirius-6.5
     --entrypoint sirius mwannotate-sirius:6.5.4 login --show`, expecting
     `Logged in as: <their-email>`.
   - Once verified, set `SIRIUS_HOME_DIR=` to the absolute host path (not
     `~`) in `.env` yourself, then re-run `check-model-ready sirius` and
     confirm `"ready": true`.

7. **Test suite passes.**
   Run `uv run pytest tests/unit tests/integration`. All tests must pass.
   If anything fails, that's a real problem to investigate and fix before
   calling setup done -- don't wave it off as unrelated without checking.

8. **Ready to run.**
   Once every step above that applies has passed its check, tell the user
   setup is complete, summarize what's ready to use (which models are
   built and ready), and ask what study they'd like to start with -- this
   hands off directly into `CLAUDE.md`'s normal one-study workflow.
