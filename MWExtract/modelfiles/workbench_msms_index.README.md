# workbench_msms_index

One row per payload-verified MS/MS file in the Metabolomics Workbench.
20,764 files, 175,700,331 MS2 spectra, 347 studies.
Replaces msms_content_audit.tsv (1,103 MB), msms_file_catalog_v2.sqlite and
msms_first_pass.tsv for all run-time selection. Those stay build-time inputs.

## The one trap: archive_bytes is SHARED across an archive's members

`archive_bytes` is the size of the ZIP the file lives in, repeated on
every member row. 20,764 files live in only 461 archives
(median 23 members each), so summing it per file over-counts by ~68x:
40,057 GB instead of the real 589 GB.

Always deduplicate WITHIN the filtered slice before summing:

    sel = idx[(idx.acquisition_type == "product_ion") & (idx.polarity == "negative")]
    download_gb = sel.drop_duplicates(["study_id", "archive_name"]).archive_bytes.sum() / 1e9

Deduplicating against the whole table instead undercounts, because a filter
can select a member that is not the first row of its archive.

`uncompressed_bytes` is per file and sums normally.

## Natural key

(study_id, archive_name, logical_input). NOT (study_id, logical_input):
1,232 rows share a study and filename across two archives.

## Population rates

    study_id                 100.0%
    archive_name             100.0%
    logical_input            100.0%
    basename                 100.0%
    format                   100.0%
    validation_status        100.0%
    has_msms                 100.0%
    ms2_spectrum_count       100.0%
    archive_bytes     100.0%
    uncompressed_bytes       100.0%
    polarity                  66.2%
    polarity_route            70.1%
    instrument_type           94.4%
    instrument_route          94.4%
    ionization                 5.3%
    collision_energy          10.7%
    fragmentation_method      10.0%
    acquisition_type          96.7%
    acquisition_confidence   100.0%
    archive_member_count     100.0%

## Verified end to end

`select_storage.py --audit-tsv workbench_msms_index.parquet` runs unchanged
against this file (via `pipeline_common.read_table`, which dispatches on
suffix). A 50 GB download / 150 GB disk budget over product-ion files selected
130 of 299 eligible studies at 49.4 GB / 149.1 GB / 20,071,624 MS2 spectra,
both budgets respected. Full index load: 19 ms.

`summarize_audit` already deduplicates archives per study before summing
`download_bytes`, so the shared-bytes trap does not affect it. The trap is
real for any NEW consumer that sums the column directly.

## What this file does NOT contain

- per-spectrum data of any kind (precursor m/z, peaks, scan ids)
- candidate lists or structures — separate artifacts
- the study-level mwTab metadata the audit derives from
- spectra for studies that are not payload-verified MS/MS

It is a selection index, not a data file.
