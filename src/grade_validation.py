"""Small, non-enriching investigation helpers for public UBCGrades data.

This module deliberately supports only a controlled Step 8A sample.  It does
not modify the Version 1 catalog or issue a request per catalog course.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import requests


API_BASE_URL = "https://ubcgrades.com/api/v3"
API_TIMEOUT_SECONDS = 20
API_USER_AGENT = "UBC-Course-Finder/0.1 (Step 8A grade-data validation)"
SESSION_PATTERN = re.compile(r"^(?P<year>\d{4})(?P<term>[WS])$")
COURSE_PATTERN = re.compile(r"^\d{3}(?:[A-Z])?$")
SAMPLE_COURSES = (
    ("MATH", "100"),
    ("CPSC", "320"),
    ("CPSC", "416"),
    ("STAT", "302"),
    ("PHYS", "100"),
    ("ENGL", "110"),
)
# CPSC is one of the 60 configured Version 1 subjects. The public course-label
# endpoint shows CPSC 312 historically but not in 2025W, making it a bounded
# real fallback probe rather than a catalog-wide availability scan.
FALLBACK_CANDIDATES = (("CPSC", "312"),) + SAMPLE_COURSES
TARGET_SESSION = "2025W"


def make_api_session() -> requests.Session:
    """Create a session that identifies this limited validation use."""
    session = requests.Session()
    session.headers.update({"User-Agent": API_USER_AGENT})
    return session


def session_sort_key(session: str) -> tuple[int, int]:
    """Return a chronological key where Winter is newer than Summer that year."""
    match = SESSION_PATTERN.fullmatch(session)
    if match is None:
        raise ValueError(f"Invalid UBCGrades session: {session!r}")
    return int(match.group("year")), {"S": 0, "W": 1}[match.group("term")]


def newest_sessions_first(sessions: Iterable[str]) -> list[str]:
    """Sort unique valid UBCGrades session labels from newest to oldest."""
    return sorted(set(sessions), key=session_sort_key, reverse=True)


def numeric_value(value: Any) -> float | None:
    """Return finite numeric data values, rejecting booleans, nulls, and strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def positive_reported(value: Any) -> int | float | None:
    """Accept a positive numeric Reported count only."""
    numeric = numeric_value(value)
    if numeric is None or numeric <= 0:
        return None
    return int(numeric) if numeric.is_integer() else numeric


def weighted_section_average(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Calculate a diagnostic Reported-weighted average from non-OVERALL rows.

    It intentionally does *not* claim that sections are mutually exclusive.
    Labs and lectures can represent overlapping students, so this result is
    only safe as a comparison against an API-provided ``OVERALL`` row.
    """
    row_list = list(rows)
    numerator = 0.0
    denominator = 0.0
    skipped_rows = 0

    for row in row_list:
        if row.get("section") == "OVERALL":
            continue
        average = numeric_value(row.get("average"))
        reported = positive_reported(row.get("reported"))
        if average is None or reported is None:
            skipped_rows += 1
            continue
        numerator += average * reported
        denominator += reported

    return {
        "average": numerator / denominator if denominator else None,
        "reported_students": int(denominator) if denominator.is_integer() else denominator,
        "included_section_rows": sum(
            row.get("section") != "OVERALL"
            and numeric_value(row.get("average")) is not None
            and positive_reported(row.get("reported")) is not None
            for row in row_list
        ),
        "skipped_section_rows": skipped_rows,
    }


def analyze_course_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe the API course response without silently aggregating sections."""
    overall_rows = [row for row in rows if row.get("section") == "OVERALL"]
    diagnostic = weighted_section_average(rows)
    usable_overall_rows = [
        row
        for row in overall_rows
        if numeric_value(row.get("average")) is not None
        and positive_reported(row.get("reported")) is not None
    ]
    non_empty_details = sorted(
        {str(row["detail"]) for row in rows if row.get("detail") not in (None, "")}
    )

    result: dict[str, Any] = {
        "rows_returned": len(rows),
        "section_rows_returned": len(rows) - len(overall_rows),
        "overall_rows_returned": len(overall_rows),
        "non_empty_detail_modifiers": non_empty_details,
        "weighted_sections_diagnostic": diagnostic,
        "latest_available_average": None,
        "grade_reported_students": None,
        "grade_source": "unavailable_no_single_usable_overall",
        "aggregation_note": (
            "No single usable OVERALL row. The weighted-section value is diagnostic only "
            "and is not selected because section populations may overlap."
        ),
    }

    if len(usable_overall_rows) == 1 and len(overall_rows) == 1:
        overall = usable_overall_rows[0]
        result.update(
            {
                "latest_available_average": numeric_value(overall["average"]),
                "grade_reported_students": positive_reported(overall["reported"]),
                "grade_source": "ubcgrades_v3_overall",
                "aggregation_note": (
                    "Selected the API-provided OVERALL row. The weighted-section result is "
                    "reported only as a cross-check, not as a replacement."
                ),
            }
        )
        weighted = diagnostic["average"]
        if weighted is not None:
            result["overall_minus_weighted_sections"] = (
                result["latest_available_average"] - weighted
            )
    return result


def select_latest_available(summaries: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the newest summary that has a course-wide, usable OVERALL value."""
    usable = [
        summary
        for summary in summaries
        if isinstance(summary.get("grade_session"), str)
        and numeric_value(summary.get("latest_available_average")) is not None
        and positive_reported(summary.get("grade_reported_students")) is not None
        and summary.get("grade_source") == "ubcgrades_v3_overall"
    ]
    return max(usable, key=lambda summary: session_sort_key(summary["grade_session"])) if usable else None


def fetch_json(session: requests.Session, endpoint: str) -> Any:
    """Retrieve one documented public API endpoint as JSON."""
    response = session.get(f"{API_BASE_URL}{endpoint}", timeout=API_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def fetch_course_summary(
    session: requests.Session, subject: str, course: str, grade_session: str
) -> dict[str, Any]:
    """Fetch and analyze one exact API course identifier for one session."""
    if not COURSE_PATTERN.fullmatch(course):
        raise ValueError(f"Invalid API course identifier: {course!r}")
    endpoint = f"/grades/UBCV/{grade_session}/{subject}/{course}"
    rows = fetch_json(session, endpoint)
    if not isinstance(rows, list):
        raise ValueError(f"Expected a list from {endpoint}, received {type(rows).__name__}")

    expected_detail = course[3:] if len(course) == 4 else ""
    exact_rows = [
        row
        for row in rows
        if row.get("subject") == subject
        and row.get("course") == course[:3]
        and row.get("detail", "") == expected_detail
    ]
    summary = analyze_course_rows(exact_rows)
    summary.update(
        {
            "subject": subject,
            "course": course,
            "grade_session": grade_session,
            "source_url": f"{API_BASE_URL}{endpoint}",
            "unexpected_identifier_rows_ignored": len(rows) - len(exact_rows),
        }
    )
    return summary


def find_fallback_candidate(
    session: requests.Session, available_sessions: Iterable[str], candidates: Iterable[tuple[str, str]]
) -> dict[str, Any] | None:
    """Find one controlled-sample course absent/unusable in 2025W with earlier data."""
    earlier_sessions = [
        session_name
        for session_name in newest_sessions_first(available_sessions)
        if session_sort_key(session_name) < session_sort_key(TARGET_SESSION)
    ]
    for subject, course in candidates:
        current_status = "found"
        try:
            current = fetch_course_summary(session, subject, course, TARGET_SESSION)
        except requests.HTTPError as error:
            if error.response is None or error.response.status_code != 404:
                raise
            current = None
            current_status = "not_found_404"
        if current is not None and select_latest_available([current]) is not None:
            continue

        for session_name in earlier_sessions:
            try:
                earlier = fetch_course_summary(session, subject, course, session_name)
            except requests.HTTPError as error:
                if error.response is None or error.response.status_code != 404:
                    raise
                continue
            if select_latest_available([earlier]) is not None:
                earlier["fallback_reason"] = "no usable 2025W OVERALL course value"
                earlier["target_session_status"] = current_status
                return earlier
    return None


def write_json(value: Any, output_path: Path) -> None:
    """Write a readable machine-readable investigation result."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_investigation(output_path: Path) -> dict[str, Any]:
    """Fetch only the requested Step 8A sample and one bounded fallback search."""
    session = make_api_session()
    available_sessions = fetch_json(session, "/yearsessions/UBCV")
    if not isinstance(available_sessions, list):
        raise ValueError("Expected the UBCGrades yearsessions endpoint to return a list")

    course_results: list[dict[str, Any]] = []
    for subject, course in SAMPLE_COURSES:
        try:
            course_results.append(fetch_course_summary(session, subject, course, TARGET_SESSION))
        except requests.HTTPError as error:
            if error.response is None or error.response.status_code != 404:
                raise
            course_results.append(
                {
                    "subject": subject,
                    "course": course,
                    "grade_session": TARGET_SESSION,
                    "status": "not_found",
                    "source_url": f"{API_BASE_URL}/grades/UBCV/{TARGET_SESSION}/{subject}/{course}",
                }
            )

    # A single label request documents real detail-modifier structure without
    # requesting grade rows for every CPSC course.
    current_cpsc_labels = fetch_json(session, f"/courses/UBCV/{TARGET_SESSION}/CPSC")
    if not isinstance(current_cpsc_labels, list):
        raise ValueError("Expected the UBCGrades CPSC course-label endpoint to return a list")
    detail_modifier_probe = [
        {
            "course": item.get("course"),
            "detail": item.get("detail"),
            "course_title": item.get("course_title"),
        }
        for item in current_cpsc_labels
        if isinstance(item, dict) and item.get("detail") not in (None, "")
    ]

    fallback = find_fallback_candidate(session, available_sessions, FALLBACK_CANDIDATES)
    report = {
        "scope": "Step 8A controlled grade-data investigation only; no catalog enrichment",
        "api_base_url": API_BASE_URL,
        "campus": "UBCV",
        "available_sessions_newest_first": newest_sessions_first(available_sessions),
        "sample_course_results": course_results,
        "detail_modifier_probe": detail_modifier_probe,
        "fallback_candidate": fallback,
        "policy_observation": (
            "A future enrichment should select the newest session with one usable API OVERALL "
            "row for the exact subject and three-digit course. It must not merge detail modifiers "
            "such as 230A into 230 without separate evidence."
        ),
    }
    write_json(report, output_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bounded Step 8A UBCGrades investigation.")
    parser.add_argument(
        "--output-path",
        type=Path,
        default=Path("data/step8a_grade_investigation.json"),
        help="JSON report output path.",
    )
    args = parser.parse_args()
    try:
        report = run_investigation(args.output_path)
    except (requests.RequestException, ValueError) as error:
        logging.error("Step 8A investigation failed: %s", error)
        return 1
    logging.info("Wrote controlled Step 8A report to %s", args.output_path)
    logging.info("Retrieved %d requested course results.", len(report["sample_course_results"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
