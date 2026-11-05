# MWExtract loop contract

MWExtract fetches **one study per invocation**. The orchestrating agent owns
the loop, the parse step between iterations, and the deletion. This document is
the contract between them.

Why one study: the product-ion corpus is 1,134 GB uncompressed across 298
studies, but the largest single study is 49.0 GB and the median is 1.93 GB.
Batch extraction does not fit on a 100 GB budget; a one-study loop fits with
room to spare. Batching the loop defeats it -- the ten largest studies alone
total 259 GB -- so if throughput ever demands batching, size batches by
summed `uncompressed_bytes` under the disk reserve, never by study count.

## The loop

    1. work list      MWExtract --list <filters>        (once, no network)
    2. for each accession:
         a. fetch      MWExtract --study <ID> <filters>
         b. verify     <output>/<ID>/spectra and <output>/<ID>/<ID>.parquet
         c. parse      mwparse-mzml and/or mwparse-mzxml for <output>/<ID>
         d. verify     the parse output
         e. delete     <output>/<ID>/           ONLY if (b) and (d) both passed
         f. ledger     record the outcome

## Exit codes

MWExtract distinguishes three outcomes so the loop can branch on them rather
than on log text:

    0   success -- files extracted into the study's spectra directory
    1   configuration or run error -- missing --study, unknown accession,
        study over the per-study ceiling, no contact address, FTP failure
    2   the filter chain matched no files in this study

**Code 2 is not a failure.** It means this study has nothing matching the
filters -- a negative-mode-only study under `--polarity positive`, say. The
loop should record it and continue. Treating it as an error stops a campaign
on a non-event.

## The delete-safety check

Deletion is the one irreversible step. Re-fetching a study costs minutes;
deleting a payload whose parse silently truncated costs a study that will be
missing from the corpus with nothing to indicate it.

Exit code 0 from the parse step is **not** sufficient evidence. Require all
four:

    1. MWExtract exited 0
    2. `<output>/<ID>/spectra` contains at least one payload file
    3. each applicable `processed/<format>/spectra.parquet` exists and is non-empty
    4. the parse output's spectrum count is consistent with the manifest's
       ms2_spectrum_count for that study

Check 4 is the one that catches a truncated parse, and it is the only one that
cannot be satisfied by a process that died partway. Allow a tolerance: the
index's `ms2_spectrum_count` is the audit's count of MS2 scans in the file,
while the parse output is post-QC, so the parse count will be LOWER by the
rejected fraction. Compare against a floor, not for equality.

If any check fails: keep the payload, record the study as `parse_failed` in
the ledger, and continue. A later pass can retry it without re-downloading.

## Ledger

One row per study, written after every iteration so an interrupted campaign
resumes rather than restarts.

    study_id        accession
    state           pending | fetched | parsed | deleted | no_files | failed
    files_extracted number of files in the spectra directory
    ms2_expected    from the index
    ms2_parsed      from the parse output
    download_gb     actual
    extracted_gb    actual
    fetched_utc     timestamp
    parsed_utc      timestamp
    deleted_utc     timestamp
    error           stage and message, if any

Resume rule: skip any study whose state is `parsed`, `deleted` or `no_files`.
Retry `pending` and `failed`. A study in state `fetched` has a payload on disk
and an unfinished parse -- retry the parse, do not re-fetch.

## Disk guard before each iteration

Check free space against the study's `extracted_gb` from the work list plus a
margin, before fetching. The work list reports the largest study precisely so
this check has a number to use. A fetch that fills the disk fails partway and
leaves a partial payload that looks like a successful one to a naive check.

## What the agent must NOT do

- Delete on exit code alone (see above)
- Re-fetch a study already in state `fetched` -- parse the payload on disk
- Fetch more than one study concurrently without re-checking disk; two 49 GB
  studies in flight exceed the budget that one comfortably fits
- Point the parse image at the baked index. Use `<output>/<ID>/<ID>.parquet`,
  which contains every corpus-index row for that study.
