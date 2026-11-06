# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The one entry point an agent (Claude Code, a human, or anything else)
drives this harness through: ``python -m app.cli <subcommand> ...``.

Each subcommand prints exactly one JSON object to stdout and exits 0 --
including for outcomes like "no matching files" or "refused: disk budget"
that are legitimate, non-error results per MWExtract/LOOP_CONTRACT.md, not
exceptions. A nonzero exit means the command itself couldn't run at all
(bad arguments, an unexpected crash); read the JSON `success`/`status`
fields for the actual outcome, the same contract app/tools/*.py already
returns.

See CLAUDE.md for how these subcommands compose into a full study run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")


def _print_result(result: dict[str, Any]) -> None:
    print(json.dumps(result, indent=2, sort_keys=True))


def _add_catalog_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--polarity", nargs="+", default=None)
    parser.add_argument("--instrument-types", nargs="+", default=None)


def cmd_list_studies(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.extraction_tools import list_workbench_studies

    return list_workbench_studies(
        polarity=args.polarity,
        instrument_types=args.instrument_types,
        acquisition_types=args.acquisition_types,
        min_ms2_spectra=args.min_ms2_spectra,
    )


def cmd_estimate_cost(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.extraction_tools import estimate_study_cost

    return estimate_study_cost(
        args.study_id, polarity=args.polarity, instrument_types=args.instrument_types,
    )


def cmd_check_disk_budget(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.extraction_tools import check_disk_budget_before_fetch

    return check_disk_budget_before_fetch(required_bytes=args.required_bytes)


def cmd_fetch(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.extraction_tools import fetch_workbench_study

    return fetch_workbench_study(
        args.study_id,
        polarity=args.polarity,
        instrument_types=args.instrument_types,
        acquisition_types=args.acquisition_types,
        min_ms2_spectra=args.min_ms2_spectra,
        max_download_bytes=args.max_download_bytes,
        max_uncompressed_bytes=args.max_uncompressed_bytes,
    )


def cmd_parse(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.processing_tools import parse_study_payload

    return parse_study_payload(
        args.study_id, file_format=args.file_format,
        candidates_sidecar=args.candidates_sidecar,
    )


def cmd_verify_parse(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.processing_tools import verify_parse_output

    return verify_parse_output(
        args.study_id, file_format=args.file_format,
        manifest_ms2_expected=args.manifest_ms2_expected,
    )


def cmd_delete_raw(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.processing_tools import delete_raw_payload_if_verified

    return delete_raw_payload_if_verified(
        args.study_id, file_format=args.file_format,
        manifest_ms2_expected=args.manifest_ms2_expected,
    )


def cmd_check_model_ready(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.analysis_tools import check_model_ready

    return check_model_ready(args.model_name)


def cmd_run_model(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.analysis_tools import run_analysis_model

    return run_analysis_model(
        args.model_name, args.study_id, args.run_id,
        device=args.device, batch_size=args.batch_size,
        instrument_type=args.instrument_type,
    )


def cmd_generate_run_id(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.run_identifiers import generate_run_id

    return {"run_id": generate_run_id(args.study_id)}


def cmd_aggregate(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.aggregation_tools import aggregate_model_campaign_results

    return aggregate_model_campaign_results(
        args.study_id, model_names=args.models, run_id=args.run_id,
    )


def cmd_study_status(args: argparse.Namespace) -> dict[str, Any]:
    from app.tools.status_tools import (
        write_all_study_status_markdown,
        write_study_status_markdown,
    )

    if args.study_id is None:
        return write_all_study_status_markdown()
    return write_study_status_markdown(args.study_id)


def build_parser() -> argparse.ArgumentParser:
    from app.tools.analysis_tools import WORKER_SPECS

    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("list-studies", help="List candidate studies by catalog filter.")
    _add_catalog_filters(p)
    p.add_argument("--acquisition-types", nargs="+", default=None)
    p.add_argument("--min-ms2-spectra", type=int, default=None)
    p.set_defaults(handler=cmd_list_studies)

    p = subparsers.add_parser("estimate-cost", help="Report a study's download/extracted size without fetching it.")
    p.add_argument("study_id")
    _add_catalog_filters(p)
    p.set_defaults(handler=cmd_estimate_cost)

    p = subparsers.add_parser("check-disk-budget", help="Check whether a fetch of this size fits the disk budget.")
    p.add_argument("--required-bytes", type=float, default=0.0)
    p.set_defaults(handler=cmd_check_disk_budget)

    p = subparsers.add_parser("fetch", help="Fetch one study's MS/MS payload over FTP.")
    p.add_argument("study_id")
    _add_catalog_filters(p)
    p.add_argument("--acquisition-types", nargs="+", default=None)
    p.add_argument("--min-ms2-spectra", type=int, default=None)
    p.add_argument("--max-download-bytes", type=float, default=None)
    p.add_argument("--max-uncompressed-bytes", type=float, default=None)
    p.set_defaults(handler=cmd_fetch)

    p = subparsers.add_parser("parse", help="Parse a fetched payload into spectra/candidates Parquet.")
    p.add_argument("study_id")
    p.add_argument("--file-format", required=True, choices=["mzml", "mzxml"])
    p.add_argument("--candidates-sidecar", default=None)
    p.set_defaults(handler=cmd_parse)

    p = subparsers.add_parser("verify-parse", help="Run the 4-point delete-safety check without deleting anything.")
    p.add_argument("study_id")
    p.add_argument("--file-format", required=True, choices=["mzml", "mzxml"])
    p.add_argument("--manifest-ms2-expected", type=int, default=None)
    p.set_defaults(handler=cmd_verify_parse)

    p = subparsers.add_parser("delete-raw", help="Delete the raw payload, but only if verify-parse passes.")
    p.add_argument("study_id")
    p.add_argument("--file-format", required=True, choices=["mzml", "mzxml"])
    p.add_argument("--manifest-ms2-expected", type=int, default=None)
    p.set_defaults(handler=cmd_delete_raw)

    p = subparsers.add_parser("check-model-ready", help="Check a model's Docker image/login prerequisites.")
    p.add_argument("model_name", choices=sorted(WORKER_SPECS))
    p.set_defaults(handler=cmd_check_model_ready)

    p = subparsers.add_parser("run-model", help="Run one scoring model against one study's parsed output.")
    p.add_argument("model_name", choices=sorted(WORKER_SPECS))
    p.add_argument("study_id")
    p.add_argument("run_id")
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--instrument-type", default="Orbitrap")
    p.set_defaults(handler=cmd_run_model)

    p = subparsers.add_parser("generate-run-id", help="Generate a run id to share across one study's model calls.")
    p.add_argument("study_id")
    p.set_defaults(handler=cmd_generate_run_id)

    p = subparsers.add_parser("aggregate", help="Merge every requested model's predictions into one comparison table.")
    p.add_argument("study_id")
    p.add_argument("--models", nargs="+", required=True)
    p.add_argument("--run-id", required=True)
    p.set_defaults(handler=cmd_aggregate)

    p = subparsers.add_parser(
        "study-status",
        help="Regenerate a study's (or every study's) markdown status from the ledger.",
    )
    p.add_argument("study_id", nargs="?", default=None, help="Omit to regenerate every study.")
    p.set_defaults(handler=cmd_study_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except Exception as error:  # always hand Claude parseable JSON instead of a traceback
        _print_result({"success": False, "status": "error", "error": str(error)})
        return 1
    _print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
