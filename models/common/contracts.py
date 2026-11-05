"""Versioned Arrow contracts for annotation-model inputs and outputs."""

from __future__ import annotations

import pyarrow as pa

MODEL_SCHEMA_VERSION = "1.0.0"

CANDIDATE_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), nullable=False),
    pa.field("study_id", pa.string(), nullable=False),
    pa.field("spectrum_id", pa.string(), nullable=False),
    pa.field("candidate_id", pa.string(), nullable=False),
    pa.field("canonical_smiles", pa.string()),
    pa.field("standardized_smiles", pa.string()),
    pa.field("inchikey", pa.string()),
    pa.field("molecular_formula", pa.string()),
    pa.field("adduct", pa.string()),
    pa.field("charge", pa.int16()),
    pa.field("candidate_source", pa.string(), nullable=False),
    pa.field("source_database", pa.string()),
    pa.field("source_rank", pa.int32()),
    pa.field("is_reference", pa.bool_(), nullable=False),
    pa.field("formula_source", pa.string()),
    pa.field("adduct_source", pa.string()),
])

PREDICTION_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), nullable=False),
    pa.field("study_id", pa.string(), nullable=False),
    pa.field("spectrum_id", pa.string(), nullable=False),
    pa.field("candidate_id", pa.string(), nullable=False),
    pa.field("model_name", pa.string(), nullable=False),
    pa.field("run_id", pa.string(), nullable=False),
    pa.field("checkpoint_fingerprint", pa.string()),
    pa.field("status", pa.string(), nullable=False),
    pa.field("eligibility_reason", pa.string()),
    pa.field("raw_score", pa.float64()),
    pa.field("rank", pa.int32()),
    pa.field("candidate_count", pa.int32()),
    pa.field("rank_score", pa.float64()),
    pa.field("adduct", pa.string()),
    pa.field("collision_energy", pa.float64()),
    pa.field("collision_energy_source", pa.string()),
])

CE_SEARCH_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), nullable=False),
    pa.field("study_id", pa.string(), nullable=False),
    pa.field("spectrum_id", pa.string(), nullable=False),
    pa.field("candidate_id", pa.string(), nullable=False),
    pa.field("model_name", pa.string(), nullable=False),
    pa.field("run_id", pa.string(), nullable=False),
    pa.field("collision_energy", pa.float64(), nullable=False),
    pa.field("raw_score", pa.float64()),
    pa.field("selected", pa.bool_(), nullable=False),
])

ELIGIBILITY_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), nullable=False),
    pa.field("study_id", pa.string(), nullable=False),
    pa.field("spectrum_id", pa.string(), nullable=False),
    # Candidate-scoring models populate this field. Spectrum-level discovery
    # models such as SIRIUS leave it null.
    pa.field("candidate_id", pa.string()),
    pa.field("model_name", pa.string(), nullable=False),
    pa.field("status", pa.string(), nullable=False),
    pa.field("reason", pa.string()),
])
