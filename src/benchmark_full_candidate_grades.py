"""Run a bounded real UBCGrades benchmark for new full-catalog subjects only."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests

from enrich_full_catalog import run_full_enrichment, write_json_atomically
from grade_validation import fetch_course_summary, make_api_session, session_sort_key


BENCHMARK_SUBJECTS = ("AI", "COGS", "LAW", "DENT", "EDCP", "LLED", "MINE", "SPPH")
BENCHMARK_RATIONALE = {
    "AI": "Small, newly created computing subject with likely recent coverage.",
    "COGS": "Small interdisciplinary cognitive-science program subject.",
    "LAW": "Large professional subject with many variable-credit undergraduate records.",
    "DENT": "Small professional health subject likely to have limited coverage.",
    "EDCP": "Large education subject with conventional undergraduate offerings.",
    "LLED": "Large language-and-literacy education subject.",
    "MINE": "Mid-sized applied engineering subject.",
    "SPPH": "Population-health subject spanning public-health and policy content.",
}


def load_records(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(record, dict) for record in value):
        raise ValueError(f"Expected a JSON list in {path}")
    return value


def select_benchmark_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep all courses in eight chosen new subjects, preserving catalog order."""
    selected = [record for record in records if record.get("subject") in BENCHMARK_SUBJECTS]
    found_subjects = {str(record.get("subject")) for record in selected}
    if found_subjects != set(BENCHMARK_SUBJECTS):
        raise ValueError(f"Benchmark subject mismatch: expected {BENCHMARK_SUBJECTS}, got {sorted(found_subjects)}")
    return selected


def spot_check_grade_found_records(
    enriched_records: list[dict[str, Any]], count: int = 10
) -> list[dict[str, Any]]:
    """Verify a bounded set of selected exact course/session API results directly."""
    found = [record for record in enriched_records if record.get("grade_status") == "grade_found"][:count]
    session = make_api_session()
    results: list[dict[str, Any]] = []
    for record in found:
        summary = fetch_course_summary(
            session,
            str(record["subject"]),
            str(record["course_number"]),
            str(record["grade_session"]),
        )
        matches = (
            summary.get("latest_available_average") == record.get("latest_available_average")
            and summary.get("grade_reported_students") == record.get("grade_reported_students")
            and summary.get("grade_source") == record.get("grade_source")
            and record.get("grade_match_subject") == record.get("subject")
            and record.get("grade_match_course") == str(record.get("course_number"))
            and record.get("grade_match_detail") == ""
        )
        results.append(
            {
                "course_code": record["course_code"],
                "grade_session": record["grade_session"],
                "exact_base_match_verified": matches,
                "api_rows_returned": summary["rows_returned"],
                "api_detail_modifiers_ignored": summary["non_empty_detail_modifiers"],
            }
        )
    return results


def run_benchmark(
    candidate_path: Path,
    benchmark_input_path: Path,
    enriched_output_path: Path,
    enrichment_report_path: Path,
    checkpoint_path: Path,
    benchmark_report_path: Path,
) -> dict[str, Any]:
    """Execute one real bounded run and one checkpoint-resume verification run."""
    source_records = load_records(candidate_path)
    benchmark_records = select_benchmark_records(source_records)
    write_json_atomically(benchmark_records, benchmark_input_path)

    initial_started = time.monotonic()
    initial_report = run_full_enrichment(
        benchmark_input_path, enriched_output_path, enrichment_report_path, checkpoint_path
    )
    initial_elapsed = time.monotonic() - initial_started
    enriched_records = load_records(enriched_output_path)
    initial_summary = initial_report["summary"]

    resume_started = time.monotonic()
    resumed_report = run_full_enrichment(
        benchmark_input_path, enriched_output_path, enrichment_report_path, checkpoint_path
    )
    resume_elapsed = time.monotonic() - resume_started
    resumed_summary = resumed_report["summary"]
    spot_checks = spot_check_grade_found_records(enriched_records)
    grade_found = [record for record in enriched_records if record.get("grade_status") == "grade_found"]
    no_data_examples = [
        {"course_code": record["course_code"], "grade_status": record["grade_status"]}
        for record in enriched_records if record.get("grade_status") != "grade_found"
    ][:5]
    session_count = len(initial_report["available_sessions_newest_first"])
    unique_batches = initial_summary["unique_subject_session_batches"]
    observed_batches_per_subject = unique_batches / len(BENCHMARK_SUBJECTS)
    max_full_batches = 186 * session_count
    projected_batches = round(observed_batches_per_subject * 186)
    report = {
        "scope": "Phase 7.5E bounded real UBCGrades benchmark for eight new subjects only; no production artifact changed.",
        "benchmark_subjects": [
            {"subject": subject, "course_count": sum(record["subject"] == subject for record in benchmark_records), "reason": BENCHMARK_RATIONALE[subject]}
            for subject in BENCHMARK_SUBJECTS
        ],
        "benchmark_course_count": len(benchmark_records),
        "available_sessions_newest_first": initial_report["available_sessions_newest_first"],
        "initial_run": {
            "wall_clock_seconds": initial_elapsed,
            "subject_session_batches_attempted": unique_batches,
            "subject_session_http_requests": initial_summary["subject_session_http_requests"],
            "successful_http_responses": initial_summary["successful_subject_session_responses"],
            "not_found_404_responses": initial_summary["not_found_404_subject_session_responses"],
            "retries": initial_summary["retry_count"],
            "unresolved_errors": initial_report["unresolved_errors"],
            "average_seconds_per_batch": initial_elapsed / unique_batches if unique_batches else None,
        },
        "course_result_summary": {
            "status_counts": dict(Counter(record["grade_status"] for record in enriched_records)),
            "grade_found_percent": 100 * len(grade_found) / len(enriched_records) if enriched_records else 0,
            "oldest_selected_session": min((record["grade_session"] for record in grade_found), key=session_sort_key, default=None),
            "newest_selected_session": max((record["grade_session"] for record in grade_found), key=session_sort_key, default=None),
            "reported_students_average": (
                sum(record["grade_reported_students"] for record in grade_found) / len(grade_found)
                if grade_found else None
            ),
            "historical_average_min": min((record["latest_available_average"] for record in grade_found), default=None),
            "historical_average_max": max((record["latest_available_average"] for record in grade_found), default=None),
            "no_data_examples": no_data_examples,
        },
        "matching_spot_checks": spot_checks,
        "checkpoint_resume": {
            "resume_wall_clock_seconds": resume_elapsed,
            "subjects_reused_from_checkpoint": resumed_summary["subjects_reused_from_checkpoint"],
            "additional_subject_session_batches": (
                resumed_summary["unique_subject_session_batches"] - unique_batches
            ),
            "additional_subject_session_http_requests": (
                resumed_summary["subject_session_http_requests"] - initial_summary["subject_session_http_requests"]
            ),
            "resumed_without_reprocessing_completed_subjects": (
                resumed_summary["subjects_reused_from_checkpoint"] == len(BENCHMARK_SUBJECTS)
                and resumed_summary["unique_subject_session_batches"] == unique_batches
            ),
        },
        "full_scale_estimate": {
            "subjects_with_courses": 186,
            "available_sessions": session_count,
            "observed_batches_per_benchmark_subject": observed_batches_per_subject,
            "observed_rate_projected_batches": projected_batches,
            "absolute_maximum_batches": max_full_batches,
            "observed_seconds_per_batch": initial_elapsed / unique_batches if unique_batches else None,
            "observed_rate_projected_seconds": (
                projected_batches * initial_elapsed / unique_batches if unique_batches else None
            ),
            "maximum_batch_projected_seconds": (
                max_full_batches * initial_elapsed / unique_batches if unique_batches else None
            ),
        },
    }
    write_json_atomically(report, benchmark_report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a bounded new-subject full-catalog grade benchmark.")
    parser.add_argument("--candidate-path", type=Path, default=Path("data/ubc_courses_full_tagged_candidate.json"))
    parser.add_argument("--benchmark-input-path", type=Path, default=Path("data/grade_benchmark_candidate_input.json"))
    parser.add_argument("--output-path", type=Path, default=Path("data/grade_benchmark_candidate.json"))
    parser.add_argument("--enrichment-report-path", type=Path, default=Path("data/grade_benchmark_candidate_enrichment_report.json"))
    parser.add_argument("--checkpoint-path", type=Path, default=Path("data/grade_benchmark_candidate_checkpoint.json"))
    parser.add_argument("--report-path", type=Path, default=Path("data/grade_benchmark_candidate_report.json"))
    args = parser.parse_args()
    try:
        report = run_benchmark(
            args.candidate_path, args.benchmark_input_path, args.output_path,
            args.enrichment_report_path, args.checkpoint_path, args.report_path,
        )
    except (OSError, ValueError, json.JSONDecodeError, requests.RequestException) as error:
        parser.error(str(error))
    print(
        f"Benchmarked {report['benchmark_course_count']} courses across "
        f"{len(report['benchmark_subjects'])} new subjects."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
