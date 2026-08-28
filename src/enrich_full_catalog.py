"""Prepare a resumable, sequential full Version 1 UBCGrades enrichment run.

This is a runner only. It is intentionally not executed as part of its
implementation. It enriches a *new* JSON output and relies on the validated
Step 8B subject/session batching and exact base-course rules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests

from enrich_grade_sample import (
    CATALOG_IDENTITY_FIELDS,
    GRADE_STATUSES,
    UNDERGRADUATE_LEVELS,
    enrich_sample_courses,
    load_catalog,
)
from grade_validation import (
    API_BASE_URL,
    fetch_json,
    make_api_session,
    newest_sessions_first,
    numeric_value,
    positive_reported,
    session_sort_key,
)


CHECKPOINT_VERSION = 1


def write_json_atomically(value: Any, output_path: Path) -> None:
    """Write JSON through a sibling temporary file so interruption is safe."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    temporary_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_path, output_path)


def catalog_fingerprint(catalog_records: list[dict[str, Any]]) -> str:
    """Identify the exact catalog a checkpoint was created against."""
    encoded = json.dumps(catalog_records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


class RetryingSubjectSessionFetcher:
    """Sequential batch API client with conservative retries for transient failures."""

    def __init__(
        self, session: requests.Session, max_retries: int = 2, retry_delay_seconds: float = 1.0
    ) -> None:
        self.session = session
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.http_requests = 0
        self.retry_count = 0
        self.successful_responses = 0
        self.not_found_404_responses = 0
        self.requested_subject_sessions: list[tuple[str, str]] = []
        self.unresolved_errors: list[dict[str, str]] = []

    def fetch(self, subject: str, grade_session: str) -> list[dict[str, Any]]:
        """Fetch one batch; a 404 is ordinary no-data and is never retried."""
        pair = (subject, grade_session)
        if pair in self.requested_subject_sessions:
            raise ValueError(f"Duplicate subject/session batch request attempted: {subject} {grade_session}")
        self.requested_subject_sessions.append(pair)

        for attempt in range(self.max_retries + 1):
            self.http_requests += 1
            try:
                rows = fetch_json(self.session, f"/grades/UBCV/{grade_session}/{subject}")
                if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                    raise ValueError(
                        f"Expected a list of grade rows for {subject} {grade_session}, got {type(rows).__name__}"
                    )
                self.successful_responses += 1
                return rows
            except requests.HTTPError as error:
                if error.response is not None and error.response.status_code == 404:
                    self.not_found_404_responses += 1
                    return []
                last_error: Exception = error
            except (requests.RequestException, ValueError) as error:
                last_error = error

            if attempt < self.max_retries:
                self.retry_count += 1
                time.sleep(self.retry_delay_seconds)
                continue
            message = str(last_error)
            self.unresolved_errors.append(
                {"subject": subject, "grade_session": grade_session, "error": message}
            )
            raise last_error

        raise RuntimeError("Unreachable retry loop")


def ordered_catalog_subjects(catalog_records: list[dict[str, Any]]) -> list[str]:
    """Keep the input catalog's subject order while running each subject once."""
    return list(dict.fromkeys(str(record["subject"]) for record in catalog_records))


def group_by_subject(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group records for a subject-at-a-time batch lookup."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record["subject"])].append(record)
    return grouped


def new_checkpoint(fingerprint: str, sessions: list[str]) -> dict[str, Any]:
    """Create the small, durable state used to resume at subject boundaries."""
    return {
        "checkpoint_version": CHECKPOINT_VERSION,
        "catalog_fingerprint": fingerprint,
        "available_sessions_newest_first": sessions,
        "completed_subjects": {},
        "unresolved_subjects": [],
        "metrics": {
            "subject_session_http_requests": 0,
            "unique_subject_session_batches": 0,
            "retry_count": 0,
            "successful_subject_session_responses": 0,
            "not_found_404_subject_session_responses": 0,
            "unresolved_errors": [],
        },
        "complete": False,
    }


def load_or_create_checkpoint(
    checkpoint_path: Path, fingerprint: str, sessions: list[str], resume: bool
) -> dict[str, Any]:
    """Load compatible completed-subject results or make a new checkpoint."""
    if resume and checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if not isinstance(checkpoint, dict):
            raise ValueError(f"Checkpoint {checkpoint_path} is not a JSON object")
        if checkpoint.get("checkpoint_version") != CHECKPOINT_VERSION:
            raise ValueError("Checkpoint version differs; rerun with --restart")
        if checkpoint.get("catalog_fingerprint") != fingerprint:
            raise ValueError("Checkpoint catalog differs; rerun with --restart")
        if checkpoint.get("available_sessions_newest_first") != sessions:
            raise ValueError("Available API sessions changed; rerun with --restart")
        if not isinstance(checkpoint.get("completed_subjects"), dict):
            raise ValueError("Checkpoint completed_subjects is invalid; rerun with --restart")
        if not isinstance(checkpoint.get("unresolved_subjects"), list):
            raise ValueError("Checkpoint unresolved_subjects is invalid; rerun with --restart")
        if not isinstance(checkpoint.get("metrics"), dict):
            raise ValueError("Checkpoint metrics are invalid; rerun with --restart")
        # Keep older candidate/V1 checkpoints readable while adding benchmark
        # observability fields that do not change enrichment results.
        checkpoint["metrics"].setdefault("successful_subject_session_responses", 0)
        checkpoint["metrics"].setdefault("not_found_404_subject_session_responses", 0)
        return checkpoint

    checkpoint = new_checkpoint(fingerprint, sessions)
    write_json_atomically(checkpoint, checkpoint_path)
    return checkpoint


def validate_checkpoint_completed_subjects(
    completed_subjects: dict[str, Any], catalog_by_subject: dict[str, list[dict[str, Any]]]
) -> None:
    """Fail closed if a checkpoint cannot reconstruct every completed subject."""
    for subject, records in completed_subjects.items():
        expected_records = catalog_by_subject.get(subject)
        if expected_records is None or not isinstance(records, list):
            raise ValueError("Checkpoint completed subjects are invalid; rerun with --restart")
        expected_codes = Counter(str(record["course_code"]) for record in expected_records)
        actual_codes = Counter(
            str(record.get("course_code")) for record in records if isinstance(record, dict)
        )
        if actual_codes != expected_codes:
            raise ValueError(
                f"Checkpoint subject {subject} does not match the current catalog; rerun with --restart"
            )


def validate_full_enrichment(
    catalog_records: list[dict[str, Any]],
    enriched_records: list[dict[str, Any]],
    available_sessions: list[str],
) -> list[str]:
    """Verify no original catalog record changed or disappeared during enrichment."""
    errors: list[str] = []
    input_codes = [str(record.get("course_code")) for record in catalog_records]
    output_codes = [str(record.get("course_code")) for record in enriched_records]
    if len(enriched_records) != len(catalog_records):
        errors.append(f"Output count {len(enriched_records)} does not equal input count {len(catalog_records)}")
    if Counter(input_codes) != Counter(output_codes):
        errors.append("Input and output course codes do not map one-to-one")
    if len(output_codes) != len(set(output_codes)):
        errors.append("Duplicate course codes were introduced in enriched output")

    output_by_code = {str(record.get("course_code")): record for record in enriched_records}
    session_set = set(available_sessions)
    for original in catalog_records:
        course_code = str(original["course_code"])
        output = output_by_code.get(course_code)
        if output is None:
            errors.append(f"{course_code}: missing from output")
            continue
        for field, value in original.items():
            if output.get(field) != value:
                errors.append(f"{course_code}: original field {field!r} changed")
        status = output.get("grade_status")
        if status not in GRADE_STATUSES:
            errors.append(f"{course_code}: invalid grade status {status!r}")
            continue
        if status == "grade_found":
            if numeric_value(output.get("latest_available_average")) is None:
                errors.append(f"{course_code}: grade_found average is not finite numeric")
            if positive_reported(output.get("grade_reported_students")) is None:
                errors.append(f"{course_code}: grade_found Reported is not positive")
            if output.get("grade_session") not in session_set:
                errors.append(f"{course_code}: grade_found session is not in API session list")
            if output.get("grade_match_subject") != output.get("subject"):
                errors.append(f"{course_code}: grade subject was not exact")
            if output.get("grade_match_course") != str(output.get("course_number")):
                errors.append(f"{course_code}: grade course was not exact")
            if output.get("grade_match_detail") != "":
                errors.append(f"{course_code}: detail modifier was merged into base course")
        elif any(
            output.get(field) is not None
            for field in (
                "latest_available_average",
                "grade_session",
                "grade_reported_students",
                "grade_source",
            )
        ):
            errors.append(f"{course_code}: unavailable grade status has non-null grade fields")
    return errors


def build_full_report(
    catalog_records: list[dict[str, Any]],
    enriched_records: list[dict[str, Any]],
    sessions: list[str],
    cumulative_metrics: dict[str, Any],
    validation_errors: list[str],
    resumed_subjects: int,
) -> dict[str, Any]:
    """Summarize full-run results only after a complete invariant-checked run."""
    statuses = Counter(record["grade_status"] for record in enriched_records)
    found = [record for record in enriched_records if record["grade_status"] == "grade_found"]
    subject_order = ordered_catalog_subjects(catalog_records)
    session_counts = Counter(record["grade_session"] for record in found)
    oldest_session = min((record["grade_session"] for record in found), key=session_sort_key, default=None)
    counts_by_subject = {
        subject: Counter(
            record["grade_status"] for record in enriched_records if record["subject"] == subject
        )
        for subject in subject_order
    }
    coverage_by_subject = {
        subject: {
            "total": sum(record["subject"] == subject for record in enriched_records),
            "grade_found": counts_by_subject[subject]["grade_found"],
            "coverage_percent": 100
            * counts_by_subject[subject]["grade_found"]
            / sum(record["subject"] == subject for record in enriched_records),
            "statuses": dict(counts_by_subject[subject]),
        }
        for subject in subject_order
    }
    counts_by_level = {
        str(level): {
            "total": sum(record["level"] == level for record in enriched_records),
            "grade_found": sum(
                record["level"] == level and record["grade_status"] == "grade_found"
                for record in enriched_records
            ),
        }
        for level in UNDERGRADUATE_LEVELS
    }
    return {
        "scope": "Full Version 1 grade enrichment; separate from the unchanged base catalog",
        "api_base_url": API_BASE_URL,
        "campus": "UBCV",
        "available_sessions_newest_first": sessions,
        "summary": {
            "total_catalog_courses": len(catalog_records),
            "status_counts": {status: statuses[status] for status in sorted(GRADE_STATUSES)},
            "coverage_percent": 100 * len(found) / len(catalog_records) if catalog_records else 0,
            "grade_found_by_session": dict(session_counts),
            "counts_by_subject": {subject: dict(counts_by_subject[subject]) for subject in subject_order},
            "coverage_by_subject": coverage_by_subject,
            "counts_by_course_level": counts_by_level,
            "oldest_selected_grade_session": oldest_session,
            "subject_session_http_requests": cumulative_metrics["subject_session_http_requests"],
            "unique_subject_session_batches": cumulative_metrics["unique_subject_session_batches"],
            "retry_count": cumulative_metrics["retry_count"],
            "successful_subject_session_responses": cumulative_metrics[
                "successful_subject_session_responses"
            ],
            "not_found_404_subject_session_responses": cumulative_metrics[
                "not_found_404_subject_session_responses"
            ],
            "subjects_reused_from_checkpoint": resumed_subjects,
        },
        "unresolved_errors": cumulative_metrics["unresolved_errors"],
        "validation_errors": validation_errors,
    }


def run_full_enrichment(
    catalog_path: Path,
    output_path: Path,
    report_path: Path,
    checkpoint_path: Path,
    resume: bool = True,
    max_retries: int = 2,
    retry_delay_seconds: float = 1.0,
) -> dict[str, Any]:
    """Run all catalog subjects, checkpointing after each one, then atomically finish."""
    catalog_records = load_catalog(catalog_path)
    fingerprint = catalog_fingerprint(catalog_records)
    session = make_api_session()
    raw_sessions = fetch_json(session, "/yearsessions/UBCV")
    if not isinstance(raw_sessions, list) or not all(isinstance(item, str) for item in raw_sessions):
        raise ValueError("Expected UBCGrades yearsessions endpoint to return a list of strings")
    sessions = newest_sessions_first(raw_sessions)
    checkpoint = load_or_create_checkpoint(checkpoint_path, fingerprint, sessions, resume)
    completed_subjects: dict[str, list[dict[str, Any]]] = checkpoint["completed_subjects"]
    catalog_by_subject = group_by_subject(catalog_records)
    validate_checkpoint_completed_subjects(completed_subjects, catalog_by_subject)
    resumed_subjects = len(completed_subjects)
    subject_results: dict[str, list[dict[str, Any]]] = dict(completed_subjects)

    fetcher = RetryingSubjectSessionFetcher(session, max_retries, retry_delay_seconds)
    for subject in ordered_catalog_subjects(catalog_records):
        if subject in completed_subjects:
            continue
        request_count_before = fetcher.http_requests
        batch_count_before = len(fetcher.requested_subject_sessions)
        retry_count_before = fetcher.retry_count
        success_count_before = fetcher.successful_responses
        not_found_count_before = fetcher.not_found_404_responses
        unresolved_count_before = len(fetcher.unresolved_errors)
        enriched_subject = enrich_sample_courses(
            catalog_by_subject[subject], sessions, fetcher.fetch, subjects=(subject,)
        )
        subject_results[subject] = enriched_subject
        has_fetch_error = any(
            record["grade_status"] == "fetch_error" for record in enriched_subject
        )
        if has_fetch_error:
            if subject not in checkpoint["unresolved_subjects"]:
                checkpoint["unresolved_subjects"].append(subject)
        else:
            completed_subjects[subject] = enriched_subject
            checkpoint["completed_subjects"] = completed_subjects
            if subject in checkpoint["unresolved_subjects"]:
                checkpoint["unresolved_subjects"].remove(subject)
        checkpoint["metrics"]["subject_session_http_requests"] += (
            fetcher.http_requests - request_count_before
        )
        checkpoint["metrics"]["unique_subject_session_batches"] += (
            len(fetcher.requested_subject_sessions) - batch_count_before
        )
        checkpoint["metrics"]["retry_count"] += fetcher.retry_count - retry_count_before
        checkpoint["metrics"]["successful_subject_session_responses"] += (
            fetcher.successful_responses - success_count_before
        )
        checkpoint["metrics"]["not_found_404_subject_session_responses"] += (
            fetcher.not_found_404_responses - not_found_count_before
        )
        checkpoint["metrics"]["unresolved_errors"].extend(
            fetcher.unresolved_errors[unresolved_count_before:]
        )
        write_json_atomically(checkpoint, checkpoint_path)

    enriched_by_code = {
        str(record["course_code"]): record
        for records in subject_results.values()
        for record in records
    }
    enriched_records = [enriched_by_code[str(record["course_code"])] for record in catalog_records]
    validation_errors = validate_full_enrichment(catalog_records, enriched_records, sessions)
    has_fetch_errors = any(record["grade_status"] == "fetch_error" for record in enriched_records)
    checkpoint["complete"] = not validation_errors and not has_fetch_errors
    checkpoint["validation_errors"] = validation_errors
    write_json_atomically(checkpoint, checkpoint_path)
    report = build_full_report(
        catalog_records,
        enriched_records,
        sessions,
        checkpoint["metrics"],
        validation_errors,
        resumed_subjects,
    )
    # The report is still useful when validation fails, while the final data
    # file remains untouched until every invariant has passed.
    write_json_atomically(report, report_path)
    if validation_errors:
        raise ValueError("Full enrichment invariants failed; final output was not written")

    write_json_atomically(enriched_records, output_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run resumable full Version 1 UBCGrades enrichment.")
    parser.add_argument("--catalog-path", type=Path, default=Path("data/ubc_courses_v1.json"))
    parser.add_argument(
        "--output-path", type=Path, default=Path("data/ubc_courses_v1_with_grades.json")
    )
    parser.add_argument(
        "--report-path", type=Path, default=Path("data/full_grade_enrichment_report.json")
    )
    parser.add_argument(
        "--checkpoint-path", type=Path, default=Path("data/full_grade_enrichment_checkpoint.json")
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Ignore an existing checkpoint and start a new subject-by-subject run.",
    )
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-delay-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if args.max_retries < 0 or args.retry_delay_seconds < 0:
        parser.error("--max-retries and --retry-delay-seconds must be non-negative")
    try:
        report = run_full_enrichment(
            args.catalog_path,
            args.output_path,
            args.report_path,
            args.checkpoint_path,
            resume=not args.restart,
            max_retries=args.max_retries,
            retry_delay_seconds=args.retry_delay_seconds,
        )
    except (OSError, json.JSONDecodeError, requests.RequestException, ValueError) as error:
        logging.error("Full grade enrichment did not complete: %s", error)
        return 1
    if report["summary"]["status_counts"]["fetch_error"]:
        logging.error(
            "Full grade enrichment has unresolved fetch errors; rerun to resume those subjects."
        )
        return 1
    logging.info("Full grade enrichment completed with %.1f%% coverage.", report["summary"]["coverage_percent"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
