"""Offline tests for full-run checkpoint and retry safety."""

import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from enrich_full_catalog import (  # noqa: E402
    RetryingSubjectSessionFetcher,
    catalog_fingerprint,
    load_or_create_checkpoint,
    run_full_enrichment,
)


class FullCatalogEnrichmentTests(unittest.TestCase):
    def test_offline_full_runner_preserves_records_and_resumes_checkpoint(self) -> None:
        catalog = [
            {
                "course_code": "TESTA 100",
                "subject": "TESTA",
                "course_number": 100,
                "title": "First",
                "description": "Preserve me.",
                "credits": 3,
                "credits_raw": None,
                "level": 100,
                "faculty_school": None,
                "source_url": "https://example.test/a",
            },
            {
                "course_code": "TESTB 200",
                "subject": "TESTB",
                "course_number": 200,
                "title": "Second",
                "description": "Also preserve me.",
                "credits": 3,
                "credits_raw": None,
                "level": 200,
                "faculty_school": None,
                "source_url": "https://example.test/b",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog_path = root / "catalog.json"
            output_path = root / "output.json"
            report_path = root / "report.json"
            checkpoint_path = root / "checkpoint.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

            def rows(subject: str, number: int) -> list[dict[str, object]]:
                return [
                    {
                        "subject": subject,
                        "course": str(number),
                        "detail": "",
                        "section": "OVERALL",
                        "average": 75.0,
                        "reported": 10,
                    }
                ]

            with patch(
                "enrich_full_catalog.fetch_json",
                side_effect=[["2025W"], rows("TESTA", 100), rows("TESTB", 200)],
            ):
                report = run_full_enrichment(
                    catalog_path, output_path, report_path, checkpoint_path, max_retries=0
                )
            output = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["validation_errors"], [])
            self.assertEqual(output[0]["description"], "Preserve me.")
            self.assertEqual(output[1]["grade_status"], "grade_found")
            self.assertEqual(report["summary"]["subject_session_http_requests"], 2)

            with patch("enrich_full_catalog.fetch_json", return_value=["2025W"]) as fetch:
                run_full_enrichment(catalog_path, output_path, report_path, checkpoint_path, max_retries=0)
            self.assertEqual(fetch.call_count, 1, "resume should request sessions but no completed subjects")

    def test_checkpoint_resumes_only_when_catalog_and_sessions_match(self) -> None:
        catalog = [{"course_code": "TEST 100", "subject": "TEST", "course_number": 100}]
        fingerprint = catalog_fingerprint(catalog)
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint_path = Path(temporary_directory) / "checkpoint.json"
            checkpoint = load_or_create_checkpoint(checkpoint_path, fingerprint, ["2025W"], False)
            checkpoint["completed_subjects"]["TEST"] = [{"course_code": "TEST 100"}]
            checkpoint_path.write_text(__import__("json").dumps(checkpoint), encoding="utf-8")

            resumed = load_or_create_checkpoint(checkpoint_path, fingerprint, ["2025W"], True)
            self.assertIn("TEST", resumed["completed_subjects"])
            with self.assertRaisesRegex(ValueError, "catalog differs"):
                load_or_create_checkpoint(checkpoint_path, "different", ["2025W"], True)
            with self.assertRaisesRegex(ValueError, "sessions changed"):
                load_or_create_checkpoint(checkpoint_path, fingerprint, ["2025S"], True)

    def test_transient_failure_retries_once_then_returns_batch(self) -> None:
        fetcher = RetryingSubjectSessionFetcher(Mock(), max_retries=2, retry_delay_seconds=0)
        with patch(
            "enrich_full_catalog.fetch_json",
            side_effect=[requests.ConnectionError("temporary"), []],
        ):
            self.assertEqual(fetcher.fetch("TEST", "2025W"), [])
        self.assertEqual(fetcher.http_requests, 2)
        self.assertEqual(fetcher.retry_count, 1)

    def test_subject_session_404_is_empty_without_retry(self) -> None:
        response = Mock(status_code=404)
        error = requests.HTTPError("Not Found", response=response)
        fetcher = RetryingSubjectSessionFetcher(Mock(), max_retries=2, retry_delay_seconds=0)
        with patch("enrich_full_catalog.fetch_json", side_effect=error):
            self.assertEqual(fetcher.fetch("TEST", "2025S"), [])
        self.assertEqual(fetcher.http_requests, 1)
        self.assertEqual(fetcher.retry_count, 0)

    def test_fetch_error_subject_is_not_marked_completed_and_is_retried_on_resume(self) -> None:
        catalog = [
            {
                "course_code": "TEST 100",
                "subject": "TEST",
                "course_number": 100,
                "title": "Retry me",
                "description": "",
                "credits": 3,
                "credits_raw": None,
                "level": 100,
                "faculty_school": None,
                "source_url": "https://example.test",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog_path = root / "catalog.json"
            output_path = root / "output.json"
            report_path = root / "report.json"
            checkpoint_path = root / "checkpoint.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with patch(
                "enrich_full_catalog.fetch_json",
                side_effect=[["2025W"], requests.ConnectionError("temporary")],
            ):
                failed_report = run_full_enrichment(
                    catalog_path, output_path, report_path, checkpoint_path, max_retries=0
                )
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(failed_report["summary"]["status_counts"]["fetch_error"], 1)
            self.assertNotIn("TEST", checkpoint["completed_subjects"])
            self.assertEqual(checkpoint["unresolved_subjects"], ["TEST"])

            valid_rows = [
                {
                    "subject": "TEST",
                    "course": "100",
                    "detail": "",
                    "section": "OVERALL",
                    "average": 80.0,
                    "reported": 10,
                }
            ]
            with patch("enrich_full_catalog.fetch_json", side_effect=[["2025W"], valid_rows]):
                resumed_report = run_full_enrichment(
                    catalog_path, output_path, report_path, checkpoint_path, max_retries=0
                )
            self.assertEqual(resumed_report["summary"]["status_counts"]["fetch_error"], 0)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))[0]["grade_status"], "grade_found")


if __name__ == "__main__":
    unittest.main()
