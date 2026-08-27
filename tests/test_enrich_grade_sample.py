"""Offline tests for Step 8B deterministic batched enrichment logic."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from enrich_grade_sample import (  # noqa: E402
    SubjectSessionFetcher,
    enrich_sample_courses,
    select_subject_sample,
    split_exact_base_course_rows,
)


def catalog_record(subject: str, number: int) -> dict[str, object]:
    return {
        "course_code": f"{subject} {number}",
        "subject": subject,
        "course_number": number,
        "title": "Example",
        "credits": 3,
        "credits_raw": None,
        "level": (number // 100) * 100,
        "faculty_school": "Faculty of Test",
        "source_url": "https://example.test/course",
    }


def overall_row(subject: str, number: int, average: float = 75.0) -> dict[str, object]:
    return {
        "subject": subject,
        "course": str(number),
        "detail": "",
        "section": "OVERALL",
        "average": average,
        "reported": 20,
    }


class EnrichGradeSampleTests(unittest.TestCase):
    def test_batch_endpoint_404_means_no_rows_not_fetch_error(self) -> None:
        response = Mock(status_code=404)
        error = __import__("requests").HTTPError("Not Found", response=response)
        fetcher = SubjectSessionFetcher(Mock())
        with patch("enrich_grade_sample.fetch_json", side_effect=error):
            self.assertEqual(fetcher.fetch("MUSC", "2025S"), [])
        self.assertEqual(fetcher.http_requests, 1)

    def test_selection_represents_levels_before_filling_five_slots(self) -> None:
        records = [
            catalog_record("TEST", number)
            for number in (100, 110, 200, 210, 300, 310, 400, 410)
        ]
        selected = select_subject_sample(records)
        self.assertEqual(len(selected), 5)
        self.assertEqual({record["level"] for record in selected}, {100, 200, 300, 400})
        self.assertEqual(len({record["course_code"] for record in selected}), 5)

    def test_exact_base_matching_rejects_detail_modifier_rows(self) -> None:
        rows = [
            overall_row("CPSC", 436),
            {**overall_row("CPSC", 436), "detail": "A", "average": 95.0},
            overall_row("CPSC", 320),
        ]
        exact, detail = split_exact_base_course_rows(rows, "CPSC", 436)
        self.assertEqual(exact, [rows[0]])
        self.assertEqual(detail, [rows[1]])

    def test_batched_lookup_selects_latest_and_classifies_detail_only_history(self) -> None:
        base = catalog_record("TEST", 200)
        detail_only = catalog_record("TEST", 300)
        calls: list[tuple[str, str]] = []

        def fetch(subject: str, grade_session: str) -> list[dict[str, object]]:
            calls.append((subject, grade_session))
            if grade_session == "2025W":
                return [{**overall_row("TEST", 200), "average": 80.0}]
            return [{**overall_row("TEST", 300), "detail": "A", "average": 90.0}]

        result = enrich_sample_courses(
            [base, detail_only], ["2025W", "2024W"], fetch, subjects=("TEST",)
        )
        self.assertEqual(calls, [("TEST", "2025W"), ("TEST", "2024W")])
        self.assertEqual(result[0]["grade_status"], "grade_found")
        self.assertEqual(result[0]["grade_session"], "2025W")
        self.assertEqual(result[0]["grade_match_detail"], "")
        self.assertEqual(result[1]["grade_status"], "only_detail_modifiers")
        self.assertIsNone(result[1]["latest_available_average"])
        self.assertEqual(result[1]["detail_modifiers_observed"], ["A"])

    def test_missing_history_is_classified_without_a_grade(self) -> None:
        result = enrich_sample_courses(
            [catalog_record("TEST", 200)],
            ["2025W", "2024W"],
            lambda _subject, _session: [],
            subjects=("TEST",),
        )
        self.assertEqual(result[0]["grade_status"], "no_grade_history")
        self.assertIsNone(result[0]["grade_session"])

    def test_base_history_without_usable_overall_is_not_selected(self) -> None:
        result = enrich_sample_courses(
            [catalog_record("TEST", 200)],
            ["2025W"],
            lambda _subject, _session: [
                {**overall_row("TEST", 200), "average": None},
                {"subject": "TEST", "course": "200", "detail": "", "section": "001", "average": 75, "reported": 20},
            ],
            subjects=("TEST",),
        )
        self.assertEqual(result[0]["grade_status"], "no_usable_overall")
        self.assertIsNone(result[0]["latest_available_average"])


if __name__ == "__main__":
    unittest.main()
