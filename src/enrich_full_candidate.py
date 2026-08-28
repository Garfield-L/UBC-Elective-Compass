"""Preservation-first full candidate grade enrichment, isolated from production."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests

from enrich_full_catalog import run_full_enrichment, write_json_atomically
from enrich_grade_sample import GRADE_STATUSES
from grade_validation import numeric_value, positive_reported, session_sort_key
from tag_interest_categories import ALLOWED_INTEREST_CATEGORIES
from validate_subjects import validate_records


EXPECTED_FULL_COURSE_COUNT = 5722
EXPECTED_FULL_SUBJECT_COUNT = 186
EXPECTED_V1_COURSE_COUNT = 3491


def load_records(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(record, dict) for record in value):
        raise ValueError(f"Expected a JSON list in {path}")
    return value


def grade_field_names(tagged_candidate: list[dict[str, Any]], v1_records: list[dict[str, Any]]) -> tuple[str, ...]:
    """Identify production grade metadata without copying older base data over candidate data."""
    candidate_fields = set().union(*(record.keys() for record in tagged_candidate))
    v1_fields = set().union(*(record.keys() for record in v1_records))
    return tuple(sorted(v1_fields - candidate_fields))


def merge_preserved_and_new(
    tagged_candidate: list[dict[str, Any]],
    v1_records: list[dict[str, Any]],
    new_enriched_records: list[dict[str, Any]],
    grade_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Return candidate-order records with V1 grades reused and new grades attached."""
    v1_by_code = {str(record["course_code"]): record for record in v1_records}
    new_by_code = {str(record["course_code"]): record for record in new_enriched_records}
    merged: list[dict[str, Any]] = []
    for candidate in tagged_candidate:
        course_code = str(candidate["course_code"])
        if course_code in v1_by_code:
            record = dict(candidate)
            record.update({field: v1_by_code[course_code].get(field) for field in grade_fields})
        else:
            record = new_by_code.get(course_code)
            if record is None:
                raise ValueError(f"New course {course_code} is missing from enrichment output")
        merged.append(record)
    return merged


def select_matching_spot_checks(records: list[dict[str, Any]], count: int = 15) -> list[dict[str, Any]]:
    """Select diverse post-run checks using captured batch-derived enrichment metadata."""
    selected: list[dict[str, Any]] = []
    selected_codes: set[str] = set()
    for status, target in (("grade_found", 10), ("only_detail_modifiers", 3), ("no_grade_history", 2)):
        seen_subjects: set[str] = set()
        for record in records:
            if record.get("grade_status") != status or record["course_code"] in selected_codes:
                continue
            if record["subject"] in seen_subjects:
                continue
            seen_subjects.add(str(record["subject"]))
            selected_codes.add(str(record["course_code"]))
            valid = (
                record.get("grade_match_subject") == record.get("subject")
                and record.get("grade_match_course") == str(record.get("course_number"))
                and record.get("grade_match_detail") == ""
            ) if status == "grade_found" else (
                record.get("latest_available_average") is None
                and record.get("grade_session") is None
                and record.get("grade_reported_students") is None
                and record.get("grade_source") is None
            )
            selected.append({
                "course_code": record["course_code"],
                "subject": record["subject"],
                "grade_status": status,
                "captured_match_metadata_valid": valid,
                "detail_modifiers_observed": record.get("detail_modifiers_observed", []),
                "sessions_searched": record.get("sessions_searched", []),
            })
            if len([item for item in selected if item["grade_status"] == status]) >= target:
                break
    return selected[:count]


def validate_full_candidate_output(
    tagged_candidate: list[dict[str, Any]],
    output_records: list[dict[str, Any]],
    v1_records: list[dict[str, Any]],
    grade_fields: tuple[str, ...],
) -> dict[str, Any]:
    """Check preservation, tags, schema, and new-course enrichment results."""
    input_by_code = {str(record["course_code"]): record for record in tagged_candidate}
    output_by_code = {str(record["course_code"]): record for record in output_records}
    v1_by_code = {str(record["course_code"]): record for record in v1_records}
    input_codes = [str(record["course_code"]) for record in tagged_candidate]
    output_codes = [str(record["course_code"]) for record in output_records]
    errors: list[str] = []
    if len(output_records) != EXPECTED_FULL_COURSE_COUNT:
        errors.append(f"Output count {len(output_records)} is not {EXPECTED_FULL_COURSE_COUNT}")
    if len({record.get("subject") for record in output_records}) != EXPECTED_FULL_SUBJECT_COUNT:
        errors.append("Output does not contain exactly 186 represented subjects")
    if Counter(input_codes) != Counter(output_codes):
        errors.append("Tagged candidate and full enriched output do not have one-to-one course identities")
    duplicate_codes = len(output_codes) - len(set(output_codes))
    if duplicate_codes:
        errors.append("Duplicate course codes found in full enriched output")
    base_field_changes: list[str] = []
    missing_tags: list[str] = []
    invalid_tags: list[str] = []
    for course_code, original in input_by_code.items():
        output = output_by_code.get(course_code)
        if output is None:
            continue
        for field, value in original.items():
            if output.get(field) != value:
                base_field_changes.append(f"{course_code}: {field}")
        tags = output.get("interest_tags")
        if not isinstance(tags, list) or len(tags) != 1:
            missing_tags.append(course_code)
        elif tags[0] not in ALLOWED_INTEREST_CATEGORIES:
            invalid_tags.append(course_code)
        if output.get("grade_status") not in GRADE_STATUSES:
            errors.append(f"{course_code}: invalid grade status {output.get('grade_status')!r}")

    v1_missing: list[str] = []
    v1_grade_mismatches: list[str] = []
    v1_tag_mismatches: list[str] = []
    for course_code, v1 in v1_by_code.items():
        output = output_by_code.get(course_code)
        if output is None:
            v1_missing.append(course_code)
            continue
        if any(output.get(field) != v1.get(field) for field in grade_fields):
            v1_grade_mismatches.append(course_code)
        if output.get("interest_tags") != v1.get("interest_tags"):
            v1_tag_mismatches.append(course_code)

    schema_errors, _schedules, _references = validate_records(output_records)
    unexpected_non_undergraduate = sum(
        not isinstance(record.get("course_number"), int) or not 100 <= record["course_number"] <= 499
        for record in output_records
    )
    if base_field_changes:
        errors.append("Base candidate fields changed during merge")
    if missing_tags or invalid_tags:
        errors.append("Interest tags are missing, multiple, or invalid")
    if v1_missing or v1_grade_mismatches or v1_tag_mismatches:
        errors.append("Original V1 preservation validation failed")
    if unexpected_non_undergraduate:
        errors.append("Non-undergraduate courses exist in output")
    errors.extend(schema_errors)
    new_records = [record for record in output_records if record["course_code"] not in v1_by_code]
    full_statuses = Counter(record["grade_status"] for record in output_records)
    new_statuses = Counter(record["grade_status"] for record in new_records)
    found_new = [record for record in new_records if record["grade_status"] == "grade_found"]
    found_all = [record for record in output_records if record["grade_status"] == "grade_found"]
    return {
        "total_courses": len(output_records),
        "represented_subjects": len({record["subject"] for record in output_records}),
        "duplicate_course_codes": duplicate_codes,
        "missing_required_identity_fields": len(schema_errors),
        "missing_or_multiple_interest_tags": len(missing_tags),
        "invalid_interest_tags": len(invalid_tags),
        "unexpected_500_plus_or_non_undergraduate_records": unexpected_non_undergraduate,
        "base_field_changes_relative_to_tagged_candidate": base_field_changes,
        "original_v1_identities_checked": len(v1_by_code),
        "original_v1_missing_identities": v1_missing,
        "original_v1_grade_field_mismatches": v1_grade_mismatches,
        "original_v1_interest_tag_mismatches": v1_tag_mismatches,
        "new_course_count": len(new_records),
        "new_course_status_counts": {status: new_statuses[status] for status in sorted(GRADE_STATUSES)},
        "new_course_grade_found_percent": 100 * len(found_new) / len(new_records) if new_records else 0,
        "new_courses_missing_grade_data": len(new_records) - len(found_new),
        "new_courses_usable_averages_at_least_80": sum(
            record["grade_status"] == "grade_found"
            and numeric_value(record.get("latest_available_average")) is not None
            and float(record["latest_available_average"]) >= 80.0
            for record in new_records
        ),
        "full_status_counts": {status: full_statuses[status] for status in sorted(GRADE_STATUSES)},
        "full_usable_grade_coverage_percent": 100 * len(found_all) / len(output_records) if output_records else 0,
        "oldest_selected_grade_session": min((record["grade_session"] for record in found_new), key=session_sort_key, default=None),
        "newest_selected_grade_session": max((record["grade_session"] for record in found_new), key=session_sort_key, default=None),
        "validation_errors": errors,
        "is_valid": not errors,
    }


def run_full_candidate_enrichment(
    tagged_candidate_path: Path,
    v1_final_path: Path,
    new_input_path: Path,
    new_enriched_path: Path,
    new_enrichment_report_path: Path,
    checkpoint_path: Path,
    output_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    """Reuse production V1 grade values and enrich only new candidate identities."""
    tagged_candidate = load_records(tagged_candidate_path)
    v1_records = load_records(v1_final_path)
    v1_codes = {str(record["course_code"]) for record in v1_records}
    new_records = [record for record in tagged_candidate if record["course_code"] not in v1_codes]
    if len(v1_codes) != EXPECTED_V1_COURSE_COUNT or len(new_records) != 2231:
        raise ValueError("Candidate/V1 identities do not match the approved 3491 + 2231 split")
    grade_fields = grade_field_names(tagged_candidate, v1_records)
    write_json_atomically(new_records, new_input_path)

    started = time.monotonic()
    new_report = run_full_enrichment(
        new_input_path, new_enriched_path, new_enrichment_report_path, checkpoint_path
    )
    elapsed = time.monotonic() - started
    new_enriched = load_records(new_enriched_path)
    merged = merge_preserved_and_new(tagged_candidate, v1_records, new_enriched, grade_fields)
    validation = validate_full_candidate_output(tagged_candidate, merged, v1_records, grade_fields)
    new_summary = new_report["summary"]
    report = {
        "scope": "Phase 7.5F full candidate enrichment: V1 grades reused exactly; only new identities queried.",
        "tagged_candidate_dataset": str(tagged_candidate_path),
        "v1_grade_source_dataset": str(v1_final_path),
        "new_courses_input": str(new_input_path),
        "new_courses_enriched": str(new_enriched_path),
        "full_enriched_candidate": str(output_path),
        "grade_fields_preserved_from_v1": list(grade_fields),
        "new_subjects_processed": len({record["subject"] for record in new_records}),
        "sessions_considered": new_report["available_sessions_newest_first"],
        "new_subject_enrichment_metrics": {
            "subject_session_batches_attempted": new_summary["unique_subject_session_batches"],
            "subject_session_http_requests": new_summary["subject_session_http_requests"],
            "successful_http_responses": new_summary["successful_subject_session_responses"],
            "not_found_404_responses": new_summary["not_found_404_subject_session_responses"],
            "retries": new_summary["retry_count"],
            "unresolved_errors": new_report["unresolved_errors"],
            "wall_clock_seconds": elapsed,
            "average_seconds_per_batch": (
                elapsed / new_summary["unique_subject_session_batches"]
                if new_summary["unique_subject_session_batches"] else None
            ),
            "subjects_reused_from_checkpoint": new_summary["subjects_reused_from_checkpoint"],
        },
        "matching_spot_checks": select_matching_spot_checks(new_enriched),
        "validation": validation,
    }
    write_json_atomically(report, report_path)
    if not validation["is_valid"] or new_summary["status_counts"]["fetch_error"]:
        raise ValueError("Full candidate enrichment has validation errors or unresolved fetch errors")
    write_json_atomically(merged, output_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich only new full-candidate courses and preserve V1 grades.")
    parser.add_argument("--tagged-candidate-path", type=Path, default=Path("data/ubc_courses_full_tagged_candidate.json"))
    parser.add_argument("--v1-final-path", type=Path, default=Path("data/ubc_courses_v1_final.json"))
    parser.add_argument("--new-input-path", type=Path, default=Path("data/full_candidate_new_courses_grade_input.json"))
    parser.add_argument("--new-enriched-path", type=Path, default=Path("data/full_candidate_new_courses_enriched.json"))
    parser.add_argument("--new-enrichment-report-path", type=Path, default=Path("data/full_candidate_new_courses_grade_report.json"))
    parser.add_argument("--checkpoint-path", type=Path, default=Path("data/full_candidate_grade_checkpoint.json"))
    parser.add_argument("--output-path", type=Path, default=Path("data/ubc_courses_full_enriched_candidate.json"))
    parser.add_argument("--report-path", type=Path, default=Path("data/full_candidate_grade_report.json"))
    args = parser.parse_args()
    try:
        report = run_full_candidate_enrichment(
            args.tagged_candidate_path, args.v1_final_path, args.new_input_path,
            args.new_enriched_path, args.new_enrichment_report_path, args.checkpoint_path,
            args.output_path, args.report_path,
        )
    except (OSError, ValueError, json.JSONDecodeError, requests.RequestException) as error:
        parser.error(str(error))
    print(f"Enriched {report['validation']['new_course_count']} new courses; preserved V1 grades exactly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
