"""Run the bounded Step 8B grade-enrichment validation sample.

This program never changes ``ubc_courses_v1.json``. It selects five catalog
courses from each of 20 approved subjects, then uses the UBCGrades v3
subject/session endpoint once per needed subject/session. The endpoint is
batched by subject so several sampled courses share each response.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from grade_validation import (
    API_BASE_URL,
    analyze_course_rows,
    fetch_json,
    make_api_session,
    newest_sessions_first,
    numeric_value,
    positive_reported,
    session_sort_key,
    write_json,
)


STEP8B_SUBJECTS = (
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
UNDERGRADUATE_LEVELS = (100, 200, 300, 400)
GRADE_STATUSES = {
    "grade_found",
    "no_grade_history",
    "only_detail_modifiers",
    "no_usable_overall",
    "fetch_error",
}
CATALOG_IDENTITY_FIELDS = (
    "course_code",
    "subject",
    "course_number",
    "title",
    "credits",
    "credits_raw",
    "level",
    "faculty_school",
    "source_url",
)
SELECTION_METHOD = (
    "For each requested subject, sort Version 1 courses by course_number then course_code. "
    "Select the median course within each available undergraduate level in ascending level "
    "order (100, 200, 300, 400), then select the median of the remaining courses until five "
    "courses are chosen. Finally sort the five by course_number and course_code."
)


class SubjectSessionFetcher:
    """Sequential public-API client that counts each HTTP request."""

    def __init__(self, session: requests.Session) -> None:
        self.session = session
        self.http_requests = 0
        self.requested_subject_sessions: list[tuple[str, str]] = []

    def fetch(self, subject: str, grade_session: str) -> list[dict[str, Any]]:
        self.http_requests += 1
        self.requested_subject_sessions.append((subject, grade_session))
        try:
            rows = fetch_json(self.session, f"/grades/UBCV/{grade_session}/{subject}")
        except requests.HTTPError as error:
            # At the subject/session batch level, a 404 means that subject has
            # no rows in that session. It is normal missing grade history, not
            # a network failure that should invalidate older-session lookup.
            if error.response is None or error.response.status_code != 404:
                raise
            return []
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError(
                f"Expected a list of grade rows for {subject} {grade_session}, got {type(rows).__name__}"
            )
        return rows


def course_sort_key(record: dict[str, Any]) -> tuple[int, str]:
    """Stable catalog ordering used by the reproducible sample selection."""
    return int(record["course_number"]), str(record["course_code"])


def select_subject_sample(records: Iterable[dict[str, Any]], count: int = 5) -> list[dict[str, Any]]:
    """Select a deterministic five-course sample while representing levels first."""
    ordered = sorted(records, key=course_sort_key)
    if len(ordered) < count:
        raise ValueError(f"Subject has only {len(ordered)} catalog courses; needs {count}")

    selected: list[dict[str, Any]] = []
    selected_codes: set[str] = set()
    for level in UNDERGRADUATE_LEVELS:
        level_records = [record for record in ordered if record.get("level") == level]
        if level_records and len(selected) < count:
            candidate = level_records[(len(level_records) - 1) // 2]
            selected.append(candidate)
            selected_codes.add(str(candidate["course_code"]))

    remaining = [record for record in ordered if str(record["course_code"]) not in selected_codes]
    while len(selected) < count:
        candidate = remaining[(len(remaining) - 1) // 2]
        selected.append(candidate)
        selected_codes.add(str(candidate["course_code"]))
        remaining = [record for record in remaining if str(record["course_code"]) != candidate["course_code"]]

    return sorted(selected, key=course_sort_key)


def select_sample_courses(
    catalog_records: Iterable[dict[str, Any]], subjects: Iterable[str] = STEP8B_SUBJECTS
) -> list[dict[str, Any]]:
    """Create the exact 20-subject × 5-course Step 8B sample from Version 1."""
    by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in catalog_records:
        if isinstance(record, dict) and record.get("subject") in subjects:
            by_subject[str(record["subject"])].append(record)

    sample: list[dict[str, Any]] = []
    for subject in subjects:
        sample.extend(select_subject_sample(by_subject[subject]))
    return sample


def split_exact_base_course_rows(
    rows: Iterable[dict[str, Any]], subject: str, course_number: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate exact base rows from detail-modifier rows for one catalog course."""
    base_course = str(course_number)
    same_course = [
        row
        for row in rows
        if row.get("subject") == subject and row.get("course") == base_course
    ]
    exact_base_rows = [row for row in same_course if row.get("detail", "") == ""]
    detail_rows = [row for row in same_course if row.get("detail") not in (None, "")]
    return exact_base_rows, detail_rows


def initial_state(record: dict[str, Any]) -> dict[str, Any]:
    """Maintain audit information while a course is checked through sessions."""
    return {
        "record": record,
        "base_history_observed": False,
        "detail_modifiers_observed": set(),
        "detail_sessions_observed": set(),
        "sessions_searched": [],
        "selected": None,
        "fetch_error": None,
    }


def finalized_record(state: dict[str, Any]) -> dict[str, Any]:
    """Give every input course one status and null grade fields when unavailable."""
    record = state["record"]
    # Preserve every original catalog field, including description, rather
    # than reconstructing a narrower record for an enriched output.
    result = dict(record)
    selected = state["selected"]

    if selected is not None:
        result.update(selected)
        result["grade_status"] = "grade_found"
    elif state["fetch_error"] is not None:
        result.update(
            {
                "latest_available_average": None,
                "grade_session": None,
                "grade_reported_students": None,
                "grade_source": None,
                "grade_status": "fetch_error",
                "grade_error": state["fetch_error"],
            }
        )
    elif state["base_history_observed"]:
        result.update(
            {
                "latest_available_average": None,
                "grade_session": None,
                "grade_reported_students": None,
                "grade_source": None,
                "grade_status": "no_usable_overall",
            }
        )
    elif state["detail_modifiers_observed"]:
        result.update(
            {
                "latest_available_average": None,
                "grade_session": None,
                "grade_reported_students": None,
                "grade_source": None,
                "grade_status": "only_detail_modifiers",
            }
        )
    else:
        result.update(
            {
                "latest_available_average": None,
                "grade_session": None,
                "grade_reported_students": None,
                "grade_source": None,
                "grade_status": "no_grade_history",
            }
        )

    result["detail_modifiers_observed"] = sorted(state["detail_modifiers_observed"])
    result["detail_sessions_observed"] = sorted(
        state["detail_sessions_observed"], key=session_sort_key, reverse=True
    )
    result["sessions_searched"] = state["sessions_searched"]
    return result


def enrich_sample_courses(
    sample_courses: Iterable[dict[str, Any]],
    sessions_newest_first: Iterable[str],
    fetch_subject_session: Callable[[str, str], list[dict[str, Any]]],
    subjects: Iterable[str] = STEP8B_SUBJECTS,
) -> list[dict[str, Any]]:
    """Enrich a bounded sample using one cached batch response per subject/session.

    A failure in a newer subject/session makes a latest-available conclusion
    unsafe for still-pending courses in that subject, so they are marked as a
    fetch error instead of accepting potentially older results.
    """
    sessions = newest_sessions_first(sessions_newest_first)
    sample_by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in sample_courses:
        sample_by_subject[str(record["subject"])].append(record)

    final_records: list[dict[str, Any]] = []
    for subject in subjects:
        states = [initial_state(record) for record in sample_by_subject[subject]]
        pending = list(states)
        for grade_session in sessions:
            if not pending:
                break
            try:
                rows = fetch_subject_session(subject, grade_session)
            except (requests.RequestException, ValueError) as error:
                for state in pending:
                    state["fetch_error"] = f"{grade_session}: {error}"
                break

            for state in list(pending):
                state["sessions_searched"].append(grade_session)
                catalog_record = state["record"]
                exact_rows, detail_rows = split_exact_base_course_rows(
                    rows, subject, int(catalog_record["course_number"])
                )
                if detail_rows:
                    state["detail_modifiers_observed"].update(
                        str(row["detail"]) for row in detail_rows
                    )
                    state["detail_sessions_observed"].add(grade_session)
                if exact_rows:
                    state["base_history_observed"] = True

                summary = analyze_course_rows(exact_rows)
                if summary["grade_source"] == "ubcgrades_v3_overall":
                    state["selected"] = {
                        "latest_available_average": summary["latest_available_average"],
                        "grade_session": grade_session,
                        "grade_reported_students": summary["grade_reported_students"],
                        "grade_source": summary["grade_source"],
                        "grade_match_subject": subject,
                        "grade_match_course": str(catalog_record["course_number"]),
                        "grade_match_detail": "",
                        "grade_rows_returned": summary["rows_returned"],
                        "grade_overall_rows_returned": summary["overall_rows_returned"],
                    }
                    pending.remove(state)
        final_records.extend(finalized_record(state) for state in states)
    return final_records


def validate_step8b(
    catalog_records: Iterable[dict[str, Any]],
    sample_courses: Iterable[dict[str, Any]],
    enriched_records: Iterable[dict[str, Any]],
    available_sessions: Iterable[str],
) -> list[str]:
    """Validate selection, exact joins, grade data, and final statuses offline."""
    catalog_codes = {str(record.get("course_code")) for record in catalog_records}
    samples = list(sample_courses)
    records = list(enriched_records)
    sessions = set(available_sessions)
    errors: list[str] = []

    if len(samples) != 100:
        errors.append(f"Expected 100 sample courses, found {len(samples)}")
    subject_counts = Counter(record.get("subject") for record in samples)
    if set(subject_counts) != set(STEP8B_SUBJECTS):
        errors.append("Sample subjects do not exactly match the required 20 subjects")
    for subject in STEP8B_SUBJECTS:
        if subject_counts[subject] != 5:
            errors.append(f"{subject}: expected 5 sample courses, found {subject_counts[subject]}")
    sample_codes = [str(record.get("course_code")) for record in samples]
    if len(sample_codes) != len(set(sample_codes)):
        errors.append("Sample includes duplicate course codes")
    for code in sample_codes:
        if code not in catalog_codes:
            errors.append(f"Sample course {code} is not in Version 1 catalog")

    output_codes = [str(record.get("course_code")) for record in records]
    if len(records) != len(samples) or Counter(output_codes) != Counter(sample_codes):
        errors.append("Output courses do not correspond one-to-one with input sample courses")
    for record in records:
        status = record.get("grade_status")
        if status not in GRADE_STATUSES:
            errors.append(f"{record.get('course_code')}: invalid grade status {status!r}")
            continue
        if status == "grade_found":
            average = numeric_value(record.get("latest_available_average"))
            reported = positive_reported(record.get("grade_reported_students"))
            if average is None:
                errors.append(f"{record['course_code']}: selected average is not finite numeric")
            if reported is None:
                errors.append(f"{record['course_code']}: selected Reported is not positive")
            if record.get("grade_session") not in sessions:
                errors.append(f"{record['course_code']}: selected session was not returned by API")
            if record.get("grade_source") != "ubcgrades_v3_overall":
                errors.append(f"{record['course_code']}: selected value is not an OVERALL source")
            if record.get("grade_match_subject") != record.get("subject"):
                errors.append(f"{record['course_code']}: grade subject is not exact")
            if record.get("grade_match_course") != str(record.get("course_number")):
                errors.append(f"{record['course_code']}: grade course is not exact")
            if record.get("grade_match_detail") != "":
                errors.append(f"{record['course_code']}: detail modifier was merged into base course")
        elif any(
            record.get(field) is not None
            for field in (
                "latest_available_average",
                "grade_session",
                "grade_reported_students",
                "grade_source",
            )
        ):
            errors.append(f"{record['course_code']}: unavailable status has non-null grade values")
    return errors


def build_report(
    sample_courses: list[dict[str, Any]],
    enriched_records: list[dict[str, Any]],
    available_sessions: list[str],
    fetcher: SubjectSessionFetcher,
    validation_errors: list[str],
    manual_sanity_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize coverage, fallback age, batching, and unusual outcomes."""
    statuses = Counter(record["grade_status"] for record in enriched_records)
    found = [record for record in enriched_records if record["grade_status"] == "grade_found"]
    session_counts = Counter(record["grade_session"] for record in found)
    session_index = {session: index for index, session in enumerate(available_sessions)}
    fallback_indexes = [session_index[record["grade_session"]] for record in found]
    detail_cases = [
        {
            "course_code": record["course_code"],
            "status": record["grade_status"],
            "detail_modifiers_observed": record["detail_modifiers_observed"],
            "detail_sessions_observed": record["detail_sessions_observed"],
        }
        for record in enriched_records
        if record["detail_modifiers_observed"]
    ]
    unusual_cases = [
        {
            "course_code": record["course_code"],
            "status": record["grade_status"],
            "detail_modifiers_observed": record["detail_modifiers_observed"],
        }
        for record in enriched_records
        if record["grade_status"] != "grade_found" or record["detail_modifiers_observed"]
    ]
    level_counts = {
        str(level): {
            "sampled": sum(record["level"] == level for record in enriched_records),
            "grade_found": sum(
                record["level"] == level and record["grade_status"] == "grade_found"
                for record in enriched_records
            ),
        }
        for level in UNDERGRADUATE_LEVELS
    }
    subject_coverage = {
        subject: {
            "sampled": sum(record["subject"] == subject for record in enriched_records),
            "grade_found": sum(
                record["subject"] == subject and record["grade_status"] == "grade_found"
                for record in enriched_records
            ),
            "statuses": dict(
                Counter(
                    record["grade_status"]
                    for record in enriched_records
                    if record["subject"] == subject
                )
            ),
        }
        for subject in STEP8B_SUBJECTS
    }
    oldest = min((record["grade_session"] for record in found), key=session_sort_key, default=None)
    median_session = None
    if found:
        median_rank = int(statistics.median(fallback_indexes))
        median_session = available_sessions[median_rank]

    naive_maximum_requests = 1 + len(sample_courses) * len(available_sessions)
    return {
        "scope": "Step 8B controlled 100-course grade-enrichment validation; not the full catalog",
        "api_base_url": API_BASE_URL,
        "campus": "UBCV",
        "available_sessions_newest_first": available_sessions,
        "selection_method": SELECTION_METHOD,
        "selected_courses_by_subject": {
            subject: [
                record["course_code"]
                for record in sample_courses
                if record["subject"] == subject
            ]
            for subject in STEP8B_SUBJECTS
        },
        "summary": {
            "sample_courses": len(sample_courses),
            "subjects_represented": len({record["subject"] for record in sample_courses}),
            "status_counts": {status: statuses[status] for status in sorted(GRADE_STATUSES)},
            "coverage_percent": (100 * len(found) / len(sample_courses)) if sample_courses else 0,
            "grade_found_by_session": dict(session_counts),
            "newest_available_session": available_sessions[0] if available_sessions else None,
            "using_newest_available_session": sum(index == 0 for index in fallback_indexes),
            "requiring_exactly_one_session_fallback": sum(index == 1 for index in fallback_indexes),
            "requiring_multiple_session_fallback": sum(index > 1 for index in fallback_indexes),
            "oldest_selected_grade_session": oldest,
            "median_selected_session": median_session,
            "counts_by_course_level": level_counts,
            "coverage_by_subject": subject_coverage,
            "http_requests_total": 1 + fetcher.http_requests,
            "http_requests_yearsessions": 1,
            "http_requests_subject_session": fetcher.http_requests,
            "unique_subject_session_requests": len(set(fetcher.requested_subject_sessions)),
            "naive_per_course_per_session_maximum_requests": naive_maximum_requests,
            "requests_saved_against_naive_maximum": naive_maximum_requests - (1 + fetcher.http_requests),
            "batching_rule_satisfied": len(fetcher.requested_subject_sessions)
            == len(set(fetcher.requested_subject_sessions)),
        },
        "detail_modifier_cases": detail_cases,
        "unusual_cases": unusual_cases,
        "ubcfinder_manual_sanity_checks": manual_sanity_checks,
        "validation_errors": validation_errors,
    }


def load_catalog(catalog_path: Path) -> list[dict[str, Any]]:
    """Load the existing Version 1 catalog without writing to it."""
    value = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(record, dict) for record in value):
        raise ValueError(f"Expected a JSON course list in {catalog_path}")
    return value


def load_manual_sanity_checks(checks_path: Path) -> list[dict[str, Any]]:
    """Load manual UBCFinder observations without using them in grade selection."""
    value = json.loads(checks_path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Expected a JSON list of manual checks in {checks_path}")
    return value


def validate_manual_sanity_checks(
    checks: Iterable[dict[str, Any]], enriched_records: Iterable[dict[str, Any]]
) -> list[str]:
    """Ensure manual observations identify real selected grade records and sessions."""
    records_by_code = {str(record["course_code"]): record for record in enriched_records}
    errors: list[str] = []
    for check in checks:
        course_code = str(check.get("course_code"))
        record = records_by_code.get(course_code)
        if record is None:
            errors.append(f"Manual check {course_code} is not in the enriched sample")
        elif record.get("grade_status") != "grade_found":
            errors.append(f"Manual check {course_code} does not have a selected grade")
        elif check.get("grade_session") != record.get("grade_session"):
            errors.append(f"Manual check {course_code} does not match selected grade session")
    return errors


def run_step8b(
    catalog_path: Path,
    sample_path: Path,
    enriched_output_path: Path,
    report_path: Path,
    sanity_checks_path: Path,
) -> dict[str, Any]:
    """Run the fixed 100-course Step 8B validation and write separate artifacts."""
    catalog_records = load_catalog(catalog_path)
    sample_courses = select_sample_courses(catalog_records)
    sample_document = {
        "scope": "Step 8B deterministic 100-course sample; source catalog unchanged",
        "selection_method": SELECTION_METHOD,
        "subjects": list(STEP8B_SUBJECTS),
        "courses": sample_courses,
    }
    write_json(sample_document, sample_path)

    session = make_api_session()
    available_sessions = fetch_json(session, "/yearsessions/UBCV")
    if not isinstance(available_sessions, list) or not all(
        isinstance(item, str) for item in available_sessions
    ):
        raise ValueError("Expected the UBCGrades yearsessions endpoint to return a list of strings")
    ordered_sessions = newest_sessions_first(available_sessions)
    fetcher = SubjectSessionFetcher(session)
    enriched_records = enrich_sample_courses(sample_courses, ordered_sessions, fetcher.fetch)
    validation_errors = validate_step8b(
        catalog_records, sample_courses, enriched_records, ordered_sessions
    )
    manual_sanity_checks = load_manual_sanity_checks(sanity_checks_path)
    validation_errors.extend(validate_manual_sanity_checks(manual_sanity_checks, enriched_records))
    write_json(enriched_records, enriched_output_path)
    report = build_report(
        sample_courses,
        enriched_records,
        ordered_sessions,
        fetcher,
        validation_errors,
        manual_sanity_checks,
    )
    write_json(report, report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bounded Step 8B grade-enrichment sample.")
    parser.add_argument("--catalog-path", type=Path, default=Path("data/ubc_courses_v1.json"))
    parser.add_argument("--sample-path", type=Path, default=Path("data/step8b_sample_courses.json"))
    parser.add_argument(
        "--enriched-output-path", type=Path, default=Path("data/step8b_grade_enriched_sample.json")
    )
    parser.add_argument("--report-path", type=Path, default=Path("data/step8b_grade_report.json"))
    parser.add_argument(
        "--sanity-checks-path",
        type=Path,
        default=Path("data/step8b_ubcfinder_sanity_checks.json"),
    )
    args = parser.parse_args()
    try:
        report = run_step8b(
            args.catalog_path,
            args.sample_path,
            args.enriched_output_path,
            args.report_path,
            args.sanity_checks_path,
        )
    except (OSError, json.JSONDecodeError, requests.RequestException, ValueError) as error:
        logging.error("Step 8B validation failed: %s", error)
        return 1
    logging.info("Step 8B coverage: %.1f%%", report["summary"]["coverage_percent"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
