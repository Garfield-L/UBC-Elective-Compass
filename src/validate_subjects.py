"""Run a polite, fixed-size validation sample of UBC Calendar subjects.

This is intentionally not a catalog-wide crawler. It fetches the master page
once, resolves the twenty subject codes below, then visits only those pages in
sequence with the scraper's existing polite delay between requests.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

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


REQUESTED_SUBJECTS = (
    "CPSC",
    "STAT",
    "MATH",
    "BIOL",
    "CHEM",
    "PHYS",
    "PSYC",
    "PHIL",
    "ECON",
    "ANTH",
    "HIST",
    "LING",
    "ENGL",
    "GEOG",
    "SOCI",
    "FREN",
    "MUSC",
    "ARTH",
    "COMM",
    "KIN",
)
BRACKETED_COURSE_REFERENCE_PATTERN = re.compile(r"\[[A-Z]{2,}\d+\]")


def write_json(value: Any, output_path: Path) -> None:
    """Write a readable UTF-8 JSON report or combined dataset."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(value, output_file, ensure_ascii=False, indent=2)
        output_file.write("\n")


def validate_records(records: list[dict[str, Any]]) -> tuple[list[str], int, int]:
    """Return invariant errors, leftover schedule count, and bracket-reference count."""
    errors: list[str] = []
    remaining_schedules = 0
    bracketed_references = 0
    seen_course_codes: set[str] = set()

    for record in records:
        course_code = record.get("course_code")
        subject = record.get("subject")
        course_number = record.get("course_number")
        level = record.get("level")

        if not isinstance(course_number, int) or not 100 <= course_number <= 499:
            errors.append(f"{course_code!r}: course_number is not between 100 and 499")
            continue
        if course_code != f"{subject} {course_number}":
            errors.append(f"{course_code!r}: course_code does not match subject and course_number")
        if level != (course_number // 100) * 100:
            errors.append(f"{course_code!r}: level does not match course_number")
        if not isinstance(record.get("title"), str) or not record["title"].strip():
            errors.append(f"{course_code!r}: title is empty")
        if not isinstance(record.get("source_url"), str) or not record["source_url"].strip():
            errors.append(f"{course_code!r}: source_url is missing")
        if course_code in seen_course_codes:
            errors.append(f"{course_code!r}: duplicate course code in output")
        seen_course_codes.add(course_code)

        credits = record.get("credits")
        credits_raw = record.get("credits_raw")
        if credits is None and (not isinstance(credits_raw, str) or not credits_raw.strip()):
            errors.append(f"{course_code!r}: null credits has no credits_raw value")
        if credits is not None and not isinstance(credits, (int, float)):
            errors.append(f"{course_code!r}: credits is not numeric or null")

        description = record.get("description", "")
        if not isinstance(description, str):
            errors.append(f"{course_code!r}: description is not text")
            continue
        if SCHEDULE_PATTERN.search(description):
            remaining_schedules += 1
            errors.append(f"{course_code!r}: numeric schedule notation remains in description")
        if BRACKETED_COURSE_REFERENCE_PATTERN.search(description):
            bracketed_references += 1

    return errors, remaining_schedules, bracketed_references


def choose_spot_check_candidate(records: list[dict[str, Any]]) -> dict[str, str] | None:
    """Prefer an edge case for manual inspection, otherwise use the first course."""
    for record in records:
        if record["credits"] is None:
            return {"course_code": record["course_code"], "reason": "variable/unrepresentable credits"}
        if record["credits"] == 0:
            return {"course_code": record["course_code"], "reason": "zero credits"}
        if not record["description"]:
            return {"course_code": record["course_code"], "reason": "missing description"}
        if BRACKETED_COURSE_REFERENCE_PATTERN.search(record["description"]):
            return {"course_code": record["course_code"], "reason": "bracketed course reference"}
    if records:
        return {"course_code": records[0]["course_code"], "reason": "first extracted course"}
    return None


def validate_subject_codes(
    subject_codes: tuple[str, ...], report_path: Path, combined_output_path: Path
) -> dict[str, Any]:
    """Fetch and validate only the provided subject codes, in sequence."""
    session = make_http_session()
    master_html, final_master_url = fetch_html(session, MASTER_SUBJECT_URL)
    subject_urls = find_subject_page_links(master_html, final_master_url)

    report: dict[str, Any] = {
        "master_subject_url": final_master_url,
        "requested_subjects": list(subject_codes),
        "subjects": {},
    }
    combined_records: list[dict[str, Any]] = []
    made_subject_request = False

    for subject_code in subject_codes:
        subject_url = subject_urls.get(subject_code)
        if subject_url is None:
            report["subjects"][subject_code] = {
                "status": "not_found_on_master_page",
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
                "source_url": subject_url,
                "network_fetch_failures": 1,
                "fetch_error": str(error),
            }
            continue

        metrics = ExtractionMetrics()
        records = extract_courses_from_subject_html(subject_html, final_subject_url, metrics)
        invariant_errors, remaining_schedules, bracketed_references = validate_records(records)
        combined_records.extend(records)

        report["subjects"][subject_code] = {
            "status": "processed",
            "source_url": final_subject_url,
            "undergraduate_records_extracted": len(records),
            "courses_excluded_outside_100_to_499": metrics.excluded_outside_undergraduate_range,
            "malformed_unparseable_course_headings": metrics.malformed_course_headings,
            "malformed_heading_examples": metrics.malformed_heading_examples,
            "unusual_variable_credit_formats": metrics.unusual_credit_formats,
            "duplicate_course_codes": metrics.duplicate_course_codes,
            "missing_titles": metrics.missing_titles,
            "missing_descriptions": metrics.missing_descriptions,
            "null_or_unreliable_faculty_school_values": metrics.null_faculty_school_records,
            "network_fetch_failures": 0,
            "other_parsing_errors": metrics.other_parsing_errors,
            "descriptions_with_ellipsis": sum("…" in record["description"] for record in records),
            "numeric_schedule_notation_remaining": remaining_schedules,
            "preserved_bracketed_course_references": bracketed_references,
            "invariant_errors": invariant_errors,
            "spot_check_candidate": choose_spot_check_candidate(records),
        }

    combined_errors, combined_remaining_schedules, combined_bracketed_references = validate_records(
        combined_records
    )
    report["combined_validation"] = {
        "undergraduate_records_extracted": len(combined_records),
        "duplicate_course_codes": len(combined_records) - len(
            {record["course_code"] for record in combined_records}
        ),
        "numeric_schedule_notation_remaining": combined_remaining_schedules,
        "preserved_bracketed_course_references": combined_bracketed_references,
        "invariant_errors": combined_errors,
    }
    processed_subjects = [
        item for item in report["subjects"].values() if item["status"] == "processed"
    ]
    resolved_subjects = [
        item for item in report["subjects"].values() if item["status"] != "not_found_on_master_page"
    ]
    report["summary"] = {
        "subjects_requested": len(subject_codes),
        "subjects_successfully_resolved": len(resolved_subjects),
        "subjects_successfully_fetched": len(processed_subjects),
        "total_undergraduate_records": len(combined_records),
        "total_500_plus_exclusions": sum(
            item.get("courses_excluded_outside_100_to_499", 0)
            for item in processed_subjects
        ),
        "malformed_unparseable_course_headings": sum(
            item.get("malformed_unparseable_course_headings", 0) for item in processed_subjects
        ),
        "unusual_variable_credit_formats": sum(
            item.get("unusual_variable_credit_formats", 0) for item in processed_subjects
        ),
        "duplicate_course_codes": sum(
            item.get("duplicate_course_codes", 0) for item in processed_subjects
        ),
        "missing_titles": sum(item.get("missing_titles", 0) for item in processed_subjects),
        "missing_descriptions": sum(
            item.get("missing_descriptions", 0) for item in processed_subjects
        ),
        "null_faculty_school_values": sum(
            item.get("null_or_unreliable_faculty_school_values", 0)
            for item in processed_subjects
        ),
        "network_fetch_failures": sum(
            item.get("network_fetch_failures", 0) for item in report["subjects"].values()
        ),
        "other_parsing_errors": sum(
            item.get("other_parsing_errors", 0) for item in processed_subjects
        ),
        "descriptions_with_ellipsis": sum(
            item.get("descriptions_with_ellipsis", 0) for item in processed_subjects
        ),
        "counts_by_level": {
            str(level): sum(record["level"] == level for record in combined_records)
            for level in (100, 200, 300, 400)
        },
        "counts_by_subject": {
            subject_code: sum(record["subject"] == subject_code for record in combined_records)
            for subject_code in subject_codes
        },
        "counts_by_faculty_school": {
            faculty_school: sum(record["faculty_school"] == faculty_school for record in combined_records)
            for faculty_school in sorted(
                {record["faculty_school"] for record in combined_records if record["faculty_school"]}
            )
        },
    }

    write_courses_json(combined_records, combined_output_path)
    write_json(report, report_path)
    return report


def validate_requested_subjects(
    report_path: Path, combined_output_path: Path
) -> dict[str, Any]:
    """Run the fixed Step 6 validation sample through the generic runner."""
    return validate_subject_codes(REQUESTED_SUBJECTS, report_path, combined_output_path)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the fixed Step 6 UBC subject validation sample.")
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("data/step6_validation_report.json"),
        help="JSON validation report path.",
    )
    parser.add_argument(
        "--combined-output-path",
        type=Path,
        default=Path("data/step6_combined_courses.json"),
        help="Combined undergraduate JSON output path.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show parser warnings as they occur.")
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    configure_logging(args.verbose)
    try:
        report = validate_requested_subjects(args.report_path, args.combined_output_path)
    except requests.RequestException as error:
        logging.error("Could not fetch the master subject page: %s", error)
        return 1

    processed = sum(item["status"] == "processed" for item in report["subjects"].values())
    logging.info("Processed %d of %d requested subjects.", processed, len(REQUESTED_SUBJECTS))
    logging.info("Wrote report to %s", args.report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
