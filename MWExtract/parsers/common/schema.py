"""Stable Arrow schemas shared by the mzML and mzXML parser images."""

from __future__ import annotations

import pyarrow as pa

SCHEMA_VERSION = "1.0.0"

PRECURSOR_TYPE = pa.struct([
    pa.field("mz", pa.float64()),
    pa.field("charge", pa.int16()),
    pa.field("charge_raw", pa.string()),
    pa.field("intensity", pa.float64()),
    pa.field("isolation_target_mz", pa.float64()),
    pa.field("isolation_lower_offset", pa.float64()),
    pa.field("isolation_upper_offset", pa.float64()),
    pa.field("activation_method", pa.string()),
    pa.field("collision_energy", pa.float64()),
    pa.field("collision_energy_unit", pa.string()),
    pa.field("collision_energy_raw", pa.string()),
    pa.field("declared_adduct", pa.string()),
    pa.field("declared_formula", pa.string()),
    pa.field("candidate_adducts", pa.list_(pa.string())),
    pa.field("adduct_source", pa.string()),
])

SPECTRA_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), nullable=False),
    pa.field("study_id", pa.string(), nullable=False),
    pa.field("source_file", pa.string(), nullable=False),
    pa.field("source_format", pa.string(), nullable=False),
    pa.field("spectrum_id", pa.string(), nullable=False),
    pa.field("native_id", pa.string(), nullable=False),
    pa.field("scan_number", pa.int64()),
    pa.field("ms_level", pa.int8(), nullable=False),
    pa.field("polarity", pa.string()),
    pa.field("retention_time_seconds", pa.float64()),
    pa.field("centroid_status", pa.string()),
    pa.field("precursors", pa.list_(PRECURSOR_TYPE), nullable=False),
    pa.field("precursor_mz", pa.float64()),
    pa.field("precursor_charge", pa.int16()),
    pa.field("collision_energy", pa.float64()),
    pa.field("collision_energy_unit", pa.string()),
    pa.field("activation_method", pa.string()),
    pa.field("declared_adduct", pa.string()),
    pa.field("declared_formula", pa.string()),
    pa.field("candidate_adducts", pa.list_(pa.string()), nullable=False),
    pa.field("mz_array", pa.list_(pa.float64()), nullable=False),
    pa.field("intensity_array", pa.list_(pa.float64()), nullable=False),
    pa.field("peak_count", pa.int32(), nullable=False),
])

CANDIDATE_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), nullable=False),
    pa.field("study_id", pa.string(), nullable=False),
    pa.field("source_file", pa.string(), nullable=False),
    pa.field("candidate_id", pa.string(), nullable=False),
    pa.field("molecular_formula", pa.string()),
    pa.field("compound_name", pa.string()),
    pa.field("inchikey", pa.string()),
    pa.field("canonical_smiles", pa.string()),
    pa.field("hmdb_id", pa.string()),
    pa.field("pubchem_cid", pa.string()),
    pa.field("source", pa.string()),
    pa.field("source_scope", pa.string(), nullable=False),
])
