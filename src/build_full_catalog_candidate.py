"""Build a validated full-Calendar undergraduate *candidate* catalog.

This runner is intentionally separate from ``build_v1_catalog.py``.  It uses
the same Calendar parser and polite sequential fetching, but writes only
candidate-named artifacts so the live Version 1 dataset cannot be replaced by
accident.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from scrape_courses import (
    MASTER_SUBJECT_URL,
    SCHEDULE_PATTERN,
    ExtractionMetrics,
    configure_logging,
    extract_courses_from_subject_html,
    fetch_html,
    find_subject_page_links,
    make_http_session,
    polite_delay,
    write_courses_json,
)
from validate_subjects import BRACKETED_COURSE_REFERENCE_PATTERN, validate_records, write_json


EXPECTED_FULL_SUBJECT_COUNT = 264
V1_SUBJECTS_PATH = Path("config/subjects_v1.txt")
V1_FINAL_DATASET_PATH = Path("data/ubc_courses_v1_final.json")


def read_subject_codes(config_path: Path, expected_count: int) -> tuple[str, ...]:
    """Read a strict candidate config so accidental scope drift is visible."""
    raw_lines = config_path.read_text(encoding="utf-8").splitlines()
    if any(not line.strip() for line in raw_lines):
        raise ValueError(f"{config_path} must not contain blank entries")
    subject_codes = tuple(
        line.strip() for line in raw_lines if not line.lstrip().startswith("#")
    )
    if len(subject_codes) != expected_count:
        raise ValueError(
            f"Expected exactly {expected_count} subject codes in {config_path}, found {len(subject_codes)}."
        )
    if len(set(subject_codes)) != len(subject_codes):
        raise ValueError(f"{config_path} contains duplicate subject codes")
    if any(not re.fullmatch(r"[A-Z0-9]+", code) for code in subject_codes):
        raise ValueError("Subject codes must be uppercase alphanumeric strings")
    return subject_codes


def base_schema_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate final candidate records and expose field-level data-quality counts."""
    invariant_errors, remaining_schedules, bracketed_references = validate_records(records)
    missing = {
        field: sum(not str(record.get(field, "")).strip() for record in records)
        for field in ("course_code", "subject", "title")
    }
    missing["course_number"] = sum(record.get("course_number") is None for record in records)
    missing["description"] = sum(
        not isinstance(record.get("description"), str) or not record["description"].strip()
        for record in records
    )
    malformed_urls = sum(
        not isinstance(record.get("source_url"), str)
        or urlparse(record["source_url"]).scheme not in {"http", "https"}
        or not urlparse(record["source_url"]).netloc
        for record in records
    )
    unexpected_non_undergraduate = sum(
        not isinstance(record.get("course_number"), int)
        or not 100 <= record["course_number"] <= 499
        for record in records
    )
    null_credits_with_raw = sum(
        record.get("credits") is None
        and isinstance(record.get("credits_raw"), str)
        and bool(record["credits_raw"].strip())
        for record in records
    )
    duplicate_codes = len(records) - len({record.get("course_code") for record in records})
    return {
        "total_records": len(records),
        "subjects_represented": len({record["subject"] for record in records}),
        "minimum_course_number": min((record["course_number"] for record in records), default=None),
        "maximum_course_number": max((record["course_number"] for record in records), default=None),
        "counts_by_level": {
            str(level): sum(record.get("level") == level for record in records)
            for level in (100, 200, 300, 400)
        },
        "duplicate_course_codes": duplicate_codes,
        "missing_course_code": missing["course_code"],
        "missing_subject": missing["subject"],
        "missing_course_number": missing["course_number"],
        "missing_title": missing["title"],
        "missing_description": missing["description"],
        "malformed_source_urls": malformed_urls,
        "unexpected_500_plus_or_non_undergraduate_records": unexpected_non_undergraduate,
        "null_credits_with_valid_credits_raw": null_credits_with_raw,
        "numeric_schedule_notation_remaining": remaining_schedules,
        "preserved_bracketed_course_references": bracketed_references,
        "schema_validation_failures": invariant_errors,
    }


def compare_with_v1(
    candidate_records: list[dict[str, Any]], v1_subject_codes: tuple[str, ...], v1_dataset_path: Path
) -> dict[str, Any]:
    """Compare only course identities; tags and grade fields are deliberately ignored."""
    v1_records = json.loads(v1_dataset_path.read_text(encoding="utf-8"))
    if not isinstance(v1_records, list) or not all(isinstance(record, dict) for record in v1_records):
        raise ValueError(f"Expected a JSON list in {v1_dataset_path}")
    v1_codes = {str(record.get("course_code")) for record in v1_records}
    candidate_codes = {str(record.get("course_code")) for record in candidate_records}
    original_subject_set = set(v1_subject_codes)
    candidate_original_codes = {
        str(record["course_code"]) for record in candidate_records if record.get("subject") in original_subject_set
    }
    contributed_new_codes = {
        str(record["course_code"]) for record in candidate_records if record.get("subject") not in original_subject_set
    }
    return {
        "v1_course_identities_total": len(v1_codes),
        "v1_course_identities_found_in_candidate": len(v1_codes & candidate_codes),
        "v1_course_identities_missing_from_candidate": sorted(v1_codes - candidate_codes),
        "new_course_identities_in_original_60_subjects": sorted(candidate_original_codes - v1_codes),
        "course_identities_contributed_by_new_subject_codes": len(contributed_new_codes),
    }


def build_candidate(
    subject_codes: tuple[str, ...],
    output_path: Path,
    report_path: Path,
    v1_subject_codes: tuple[str, ...],
    v1_dataset_path: Path,
) -> dict[str, Any]:
    """Fetch each configured Calendar subject once, sequentially, and validate output."""
    session = make_http_session()
    master_html, final_master_url = fetch_html(session, MASTER_SUBJECT_URL)
    subject_urls = find_subject_page_links(master_html, final_master_url)
    original_subject_set = set(v1_subject_codes)
    report: dict[str, Any] = {
        "scope": "Phase 7.5B full undergraduate base catalog candidate only; production data is not written.",
        "master_subject_url": final_master_url,
        "requested_subjects": list(subject_codes),
        "subjects": {},
    }
    combined_records: list[dict[str, Any]] = []
    global_codes: set[str] = set()
    made_subject_request = False

    for subject_code in subject_codes:
        subject_url = subject_urls.get(subject_code)
        if subject_url is None:
            report["subjects"][subject_code] = {
                "status": "not_found_on_master_page",
                "undergraduate_course_count": 0,
                "was_in_original_v1": subject_code in original_subject_set,
                "is_new_relative_to_v1": subject_code not in original_subject_set,
                "network_fetch_failures": 0,
            }
            continue
        if made_subject_request:
            polite_delay()
        made_subject_request = True
        try:
            subject_html, final_subject_url = fetch_html(session, subject_url)
        except requests.RequestException as error:
            logging.error("Could not fetch %s: %s", subject_code, error)
            report["subjects"][subject_code] = {
                "status": "fetch_failed",
                "requested_url": subject_url,
                "undergraduate_course_count": 0,
                "was_in_original_v1": subject_code in original_subject_set,
                "is_new_relative_to_v1": subject_code not in original_subject_set,
                "network_fetch_failures": 1,
                "fetch_error": str(error),
            }
            continue

        metrics = ExtractionMetrics()
        try:
            records = extract_courses_from_subject_html(subject_html, final_subject_url, metrics)
        except Exception as error:  # Defensive: one incompatible page cannot stop the full candidate audit.
            logging.exception("Could not parse %s", subject_code)
            report["subjects"][subject_code] = {
                "status": "parser_failed",
                "requested_url": subject_url,
                "source_url": final_subject_url,
                "undergraduate_course_count": 0,
                "was_in_original_v1": subject_code in original_subject_set,
                "is_new_relative_to_v1": subject_code not in original_subject_set,
                "parser_exception": f"{type(error).__name__}: {error}",
            }
            continue

        invariant_errors, remaining_schedules, bracketed_references = validate_records(records)
        global_duplicates = 0
        unique_records: list[dict[str, Any]] = []
        for record in records:
            course_code = str(record["course_code"])
            if course_code in global_codes:
                global_duplicates += 1
                continue
            global_codes.add(course_code)
            unique_records.append(record)
        combined_records.extend(unique_records)
        report["subjects"][subject_code] = {
            "status": "processed",
            "requested_url": subject_url,
            "source_url": final_subject_url,
            "redirected": final_subject_url != subject_url,
            "course_articles_encountered": metrics.course_articles_encountered,
            "course_headings_encountered": metrics.course_headings_encountered,
            "undergraduate_course_count": len(unique_records),
            "was_in_original_v1": subject_code in original_subject_set,
            "is_new_relative_to_v1": subject_code not in original_subject_set,
            "courses_excluded_outside_100_to_499": metrics.excluded_outside_undergraduate_range,
            "malformed_unparseable_course_headings": metrics.malformed_course_headings,
            "malformed_heading_examples": metrics.malformed_heading_examples,
            "unusual_variable_credit_formats": metrics.unusual_credit_formats,
            "duplicate_course_codes_within_subject": metrics.duplicate_course_codes,
            "duplicate_course_codes_in_combined_output": global_duplicates,
            "missing_titles": metrics.missing_titles,
            "missing_descriptions": metrics.missing_descriptions,
            "null_or_unreliable_faculty_school_values": metrics.null_faculty_school_records,
            "network_fetch_failures": 0,
            "other_parsing_errors": metrics.other_parsing_errors,
            "descriptions_with_ellipsis": sum("…" in record["description"] for record in unique_records),
            "numeric_schedule_notation_remaining": remaining_schedules,
            "preserved_bracketed_course_references": bracketed_references,
            "invariant_errors": invariant_errors,
        }

    processed = [item for item in report["subjects"].values() if item["status"] == "processed"]
    schema = base_schema_summary(combined_records)
    zero_undergraduate = [
        code for code, item in report["subjects"].items()
        if item["status"] == "processed" and item["undergraduate_course_count"] == 0
    ]
    counts_by_subject = {
        code: report["subjects"][code].get("undergraduate_course_count", 0) for code in subject_codes
    }
    new_subjects_with_courses = [
        code for code in subject_codes
        if report["subjects"][code].get("is_new_relative_to_v1")
        and report["subjects"][code].get("undergraduate_course_count", 0) > 0
    ]
    audit_path = Path("data/full_catalog_expansion_audit.json")
    ambiguous_codes: set[str] = set()
    if audit_path.exists():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        ambiguous_codes = set(audit.get("ambiguous_subjects_requiring_human_review", []))
    report["summary"] = {
        "subjects_requested": len(subject_codes),
        "subjects_successfully_resolved": sum(item["status"] != "not_found_on_master_page" for item in report["subjects"].values()),
        "subjects_successfully_fetched": len(processed),
        "subjects_with_at_least_one_undergraduate_course": len(processed) - len(zero_undergraduate),
        "subjects_with_zero_undergraduate_courses": zero_undergraduate,
        "total_undergraduate_records": len(combined_records),
        "total_500_plus_exclusions": sum(item.get("courses_excluded_outside_100_to_499", 0) for item in processed),
        "total_malformed_unparseable_course_headings": sum(item.get("malformed_unparseable_course_headings", 0) for item in processed),
        "total_unusual_variable_credit_formats": sum(item.get("unusual_variable_credit_formats", 0) for item in processed),
        "total_duplicate_course_codes": sum(item.get("duplicate_course_codes_within_subject", 0) + item.get("duplicate_course_codes_in_combined_output", 0) for item in processed),
        "total_missing_titles": sum(item.get("missing_titles", 0) for item in processed),
        "total_missing_descriptions": sum(item.get("missing_descriptions", 0) for item in processed),
        "total_network_fetch_failures": sum(item.get("network_fetch_failures", 0) for item in report["subjects"].values()),
        "total_parser_failures": sum(item["status"] == "parser_failed" for item in report["subjects"].values()),
        "total_other_parsing_errors": sum(item.get("other_parsing_errors", 0) for item in processed),
        "redirected_subjects": [code for code, item in report["subjects"].items() if item.get("redirected")],
        "counts_by_subject": counts_by_subject,
        "top_20_subjects_by_undergraduate_course_count": [
            {"subject": code, "undergraduate_course_count": count}
            for code, count in sorted(counts_by_subject.items(), key=lambda item: (-item[1], item[0]))[:20]
        ],
        "new_subjects_with_at_least_one_undergraduate_course": new_subjects_with_courses,
        "ambiguous_subjects_with_at_least_one_undergraduate_course": [
            code for code in new_subjects_with_courses if code in ambiguous_codes
        ],
        "counts_by_faculty_school": dict(
            sorted(Counter(
                record["faculty_school"] for record in combined_records if record.get("faculty_school")
            ).items())
        ),
    }
    report["candidate_schema_validation"] = schema
    report["v1_identity_comparison"] = compare_with_v1(combined_records, v1_subject_codes, v1_dataset_path)
    report["candidate_is_valid"] = not (
        schema["duplicate_course_codes"]
        or schema["unexpected_500_plus_or_non_undergraduate_records"]
        or schema["malformed_source_urls"]
        or schema["schema_validation_failures"]
    )
    write_json(report, report_path)
    if not report["candidate_is_valid"]:
        raise ValueError("Candidate schema validation failed; candidate dataset was not written")
    write_courses_json(combined_records, output_path)
    return report


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the full UBC Calendar undergraduate base catalog candidate.")
    parser.add_argument("--config", type=Path, default=Path("config/subjects_full.txt"))
    parser.add_argument("--output", type=Path, default=Path("data/ubc_courses_full_base_candidate.json"))
    parser.add_argument("--report-path", type=Path, default=Path("data/ubc_courses_full_base_candidate_report.json"))
    parser.add_argument("--verbose", action="store_true", help="Show parser warnings as they occur.")
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    configure_logging(args.verbose)
    try:
        subject_codes = read_subject_codes(args.config, EXPECTED_FULL_SUBJECT_COUNT)
        v1_subject_codes = read_subject_codes(V1_SUBJECTS_PATH, 60)
        report = build_candidate(subject_codes, args.output, args.report_path, v1_subject_codes, V1_FINAL_DATASET_PATH)
    except (OSError, ValueError, json.JSONDecodeError, requests.RequestException) as error:
        logging.error("Full candidate build failed: %s", error)
        return 1
    logging.info(
        "Fetched %d of %d subjects and wrote %d undergraduate candidate records.",
        report["summary"]["subjects_successfully_fetched"],
        report["summary"]["subjects_requested"],
        report["summary"]["total_undergraduate_records"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
