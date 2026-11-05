# Shared spectrum parser contract

Both parser images accept one MWExtract study directory and publish the same
Parquet schemas under `processed/<format>/`.

Build them from the repository root:

    docker build -f parsers/mzml/Dockerfile -t mwparse-mzml:1 .
    docker build -f parsers/mzxml/Dockerfile -t mwparse-mzxml:1 .

Shared code, schemas, dependencies, and adduct rules live in `parsers/common`;
each format directory contains only its image definition.

## Candidate sidecar

The optional `--candidates` input may be Parquet, TSV, or CSV. It requires a
`study_id` column and accepts these nullable columns:

    source_file
    molecular_formula
    compound_name
    inchikey
    canonical_smiles
    hmdb_id
    pubchem_cid
    source

At least one formula, name, or identifier must be present in every candidate
row. A null `source_file` makes the candidate study-scoped. Exact file-scoped
rows take precedence; study-scoped rows are used only for files with no exact
candidate rows.

## Spectrum schema

`spectra.parquet` contains one row per MS2 spectrum. Peak arrays are preserved
as decoded and all selected precursors are stored in the `precursors` list.
Scalar precursor columns mirror the first precursor for consumers that do not
support nested data. Missing source metadata remains null.

`candidate_adducts` is not an identification. It is either the declared adduct
or a versioned list inferred from polarity and charge. Molecular formulas are
never inferred from precursor mass.
