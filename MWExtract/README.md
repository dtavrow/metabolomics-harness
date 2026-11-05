# MWExtract

Fetch the MS/MS payload of **one** Metabolomics Workbench study.

    Corpus audit date : 2026-09-26
    Corpus            : 20,764 payload-verified MS/MS files
                        175,700,331 MS2 spectra across 347 studies

One study per invocation -- not a mode, the contract. The product-ion corpus is
1,134 GB uncompressed across 298 studies, but the largest single study is
49.0 GB and the median is 1.93 GB, so a one-study loop turns a figure that does
not fit on the machine into one that comfortably does. The orchestrating agent
loops, parses each payload, and deletes it. See `LOOP_CONTRACT.md`.

Parsing is a separate image: the parser has no business sharing a rebuild cycle
with the only component needing network egress.

The corpus index baked in at `/modelfiles/workbench_msms_index.parquet` is a
**snapshot as of the audit date above**. Studies deposited later are invisible
to it -- deliberate, because a run must be reproducible against a fixed corpus,
but it makes the audit date part of the image's identity. Tag with it.

## Build

    docker build -f extractor/Dockerfile -t mwextract:2026-09-26 .

The build fails rather than the first run if a dependency is missing or the
baked index is corrupt: it imports every stage and asserts the index still has
20,764 rows and 175,700,331 spectra.

## Run

Get the work list -- index only, no network, no credentials:

    docker run --rm mwextract:2026-09-26 --list \
      --polarity negative --instrument-types Orbitrap

    # 53 studies, 1,501 files, 9,907,537 MS2 spectra
    # 71.0 GB to download, 94.7 GB extracted, if every study below is fetched
    # largest single study: 10.0 GB extracted -- peak disk for the loop
    # accession  files  ms2_spectra  extracted_gb
    ST001738	24	137629	2.45
    ST002145	10	118712	1.84
    ...

Bare accessions on stdout, everything else commented, so it pipes straight into
a loop.

Fetch one study:

    docker run --rm \
      -v /local/output:/output \
      -v /local/mwb_email.txt:/run/secrets/mwb_email:ro \
      mwextract:2026-09-26 \
      --email-file /run/secrets/mwb_email \
      --study ST004496 --polarity negative

Select without fetching, to see what a study would cost:

    docker run --rm -v /local/output:/output mwextract:2026-09-26 \
      --study ST004496 --dry-run

## Exit codes

    0   files extracted into the study's spectra directory
    1   configuration or run error
    2   the filter chain matched no files in THIS study

Code 2 is not a failure -- a negative-mode-only study under `--polarity
positive` returns it. The loop should record and continue.

## Output

Everything for a study lands under `<output>/<STUDY_ID>/`, so cleanup is one
recursive delete of a path predictable before the run starts.

    /output/ST004496/
    ├── ST004496.parquet              every corpus-index row for this study
    └── spectra/
        └── neg_01.mzML               payload, ORIGINAL format, unmodified

Payloads are never re-encoded, and archive directory names are not retained:
every payload lands directly in `spectra/`. A run fails before extraction if
selected payloads have colliding filenames. Planning and transfer manifests
are temporary implementation details and are not included in the delivered
study directory. The downloaded ZIP is deleted once its members are out.

## Keeping the contact address private

First, what it actually is: the Workbench FTP logs in as **`anonymous`** and
takes an email address as the password. It is a courtesy contact, not a
credential -- it grants nothing and protects nothing. Still worth not
scattering: it is personal information, and the image should not carry it.

It is never written to any manifest, log or output file. The only use is the
`ftp.login()` call.

**`docker buildx --secret` is the wrong tool here.** BuildKit secrets exist for
values needed *while building* -- a token to clone a private repo, say --
mounted at `/run/secrets/<id>` for one `RUN` step and never committed to a
layer. This address is needed at **run** time, so a build secret would not be
present when it is wanted. Reach for buildx secrets only if a future build step
needs a credential.

Run-time options, worst to best:

    # 1. visible in `docker inspect`, the host process list, and shell history
    docker run -e MWB_EMAIL=you@institution.edu mwextract:TAG ...

    # 2. better: not in argv or shell history, still in `docker inspect`
    printf 'MWB_EMAIL=you@institution.edu\n' > mwb.env   # chmod 600
    docker run --env-file mwb.env mwextract:TAG ...

    # 3. best for a one-off: file mounted read-only, never in the environment
    printf 'you@institution.edu' > mwb_email.txt          # chmod 600
    docker run --rm \
      -v "$PWD/mwb_email.txt:/run/secrets/mwb_email:ro" \
      -v /local/output:/output \
      mwextract:TAG --email-file /run/secrets/mwb_email ...

    # 4. best under Swarm or Compose, which mount secrets at /run/secrets
    #    automatically; point --email-file at the mount.
    docker secret create mwb_email mwb_email.txt

`--email-file` takes precedence over `--email` and `MWB_EMAIL`, so mounting a
file is enough to override an address inherited from the environment. A missing
or empty file fails immediately with a clear message rather than at the FTP
call.

One caveat on option 1 that applies to every image, not just this one: anything
passed with `-e` is readable by any user who can run `docker inspect` on the
container, and it persists in the container's config until the container is
removed.

## Network

The container reaches the Workbench over its normal NAT'd egress; no special
Docker networking is needed. Two details matter:

**FTP passive mode is set explicitly.** The Workbench serves archives over FTP
only. In *active* mode the server opens the data connection back to the
client, which cannot work from inside a container -- the client advertises its
private bridge address and the server's connect attempt is dropped. `ftplib`
defaults to passive, but `workbench_peek._open_ftp` and
`workbench_study.ftp_session` now call `set_pasv(True)` explicitly so a future
edit cannot silently break it.

**Corporate firewalls are the likely failure.** Passive FTP needs outbound
access to the server's ephemeral data ports, not just port 21. If the download
stage hangs after a successful login, that is the cause, and it is a site
network policy rather than an image problem.

## Known limitations

**`mwtab` is deliberately not installed.** It was used for a single URL
constructor. It must be installed `--no-deps` (its pins conflict with the
solved environment) and then imports `jsonschema` at module load anyway, so a
`--no-deps` install fails at import -- which is exactly how the first build of
this image failed. Satisfying it properly would add `docopt`, `jsonschema` and
`setuptools_scm` to the one image that needs network egress.
`workbench_study.mwtab_id_field()` reproduces the constructor in two lines and
is verified to emit byte-identical URLs for ST and AN accessions.



**mzdata cannot be converted, and the conversion stage is gone.** 35 of 20,764
files (0.2%) are mzdata, which needs a ProteoWizard conversion. That path
shelled out to a ProteoWizard *container*, and a container cannot start another
without the host docker socket -- deliberately not requested. The stage was
therefore removed from this image entirely rather than left in as a no-op: it
did nothing for the 99.8% that are directly readable and could not work for the
rest. The default `--formats` excludes mzdata. Selecting it with
`--formats mzdata` will fetch the files, but no parser downstream will read
them until they are converted elsewhere.

**`--dry-run` is offline; the download stage's own planning is not.**
`build_download_plan` re-peeks the study's archive over FTP to confirm its
members before fetching, so planning needs network even though it transfers
nothing. MWExtract's `--dry-run` stops before that point and is genuinely
network-free, as is `--list`.

**The index is a selection index, not a data file.** No per-spectrum data, no
candidate lists, no structures. Parsing and candidate composition are later
images.

## Spectrum parser images

The repository also builds two format-specific parser images with one shared
output contract:

    docker build -f parsers/mzml/Dockerfile -t mwparse-mzml:1 .
    docker build -f parsers/mzxml/Dockerfile -t mwparse-mzxml:1 .

Each invocation processes one MWExtract study directory. For example:

    docker run --rm -v /local/output:/data mwparse-mzxml:1 \
      --study-dir /data/ST004067 --output-root /data

The mzML equivalent uses `mwparse-mzml:1`. An optional molecular-candidate
sidecar may be supplied with `--candidates /input/candidates.parquet`; an
optional replacement for the versioned adduct rules may be supplied with
`--adduct-config /input/adducts.json`.

Results are format-specific so mixed-format studies can be processed safely:

    ST004067/processed/mzxml/
    ├── spectra.parquet      one row per MS2 spectrum, including peak arrays
    ├── candidates.parquet   file-wide candidates, empty when none were supplied
    └── parse_report.json    counts, versions, provenance, and missing metadata

The parsers preserve decoded peak arrays without filtering, normalization,
rounding, sorting, or centroiding. A molecular formula is retained only when
the source declares it. Adduct candidates may be inferred from polarity and
charge, but are labeled as inferred rather than treated as identifications.

## Annotation model images

Independent images for FLARE, GLACIER, ICEBERG, JESTR, SIRIUS, and evidence assembly
consume the parser contract above. They keep acquisition-quality evidence
separate from candidate-ranking evidence, require checksummed checkpoint
manifests for learned models, record every collision-energy grid score, and
produce explicit eligibility and training-set leakage tables. Build and run
instructions are in [models/README.md](../models/README.md).

**Per-study ceilings bound one study, not a campaign.** `--max-download-bytes`
and `--max-uncompressed-bytes` default to 25 GB and 60 GB, sized so the largest
study in the corpus passes. They are not a budget for the whole loop -- the
agent bounds that by deciding how many studies to fetch, and by checking free
disk before each iteration against the work list's `extracted_gb`.

## Verified

Tested natively (host conda, module tree identical to the image). The container
itself was NOT built: the test host has no reachable Docker daemon and no
egress to pull a base image.

    --list with three attribute filters   53 studies / 1,501 files, bare
                                          accessions on stdout
    --study ST004496 --dry-run            selected, 4 files, costs reported
    --study ST999999                      exit 1, explains index snapshot
    no --study and no --list              exit 1, explains the one-study contract
    --study with an impossible filter     exit 2, names the emptying predicate
    --study over the per-study ceiling    exit 1, reports need vs ceiling
    --study without a contact address     exit 1, lists the four ways to supply
    --email-file missing / empty          exit 1 with the path
    extraction + archive self-deletion    verified on a synthetic archive
    FTP transfer                          NOT EXERCISED -- no egress on the test
                                          host; failures land per study in
                                          download_failures.tsv and the run
                                          continues

The FTP leg is the only untested path.
