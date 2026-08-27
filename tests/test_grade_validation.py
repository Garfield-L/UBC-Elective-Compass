"""Offline tests for the bounded Step 8A grade-data helpers."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grade_validation import (  # noqa: E402
    analyze_course_rows,
    newest_sessions_first,
    select_latest_available,
    weighted_section_average,
)


class GradeValidationTests(unittest.TestCase):
    def test_orders_sessions_newest_first(self) -> None:
        self.assertEqual(
            newest_sessions_first(["2024W", "2025S", "2024S", "2025W", "2025S"]),
            ["2025W", "2025S", "2024W", "2024S"],
        )

    def test_selects_newest_usable_overall_value(self) -> None:
        summaries = [
            {
                "grade_session": "2024W",
                "latest_available_average": 72.4,
                "grade_reported_students": 100,
                "grade_source": "ubcgrades_v3_overall",
            },
            {
                "grade_session": "2025W",
                "latest_available_average": None,
                "grade_reported_students": None,
                "grade_source": "unavailable_no_single_usable_overall",
            },
            {
                "grade_session": "2025S",
                "latest_available_average": 75.0,
                "grade_reported_students": 20,
                "grade_source": "ubcgrades_v3_overall",
            },
        ]
        self.assertEqual(select_latest_available(summaries), summaries[2])

    def test_calculates_weighted_section_average_and_ignores_overall(self) -> None:
        result = weighted_section_average(
            [
                {"section": "001", "average": 70.0, "reported": 10},
                {"section": "002", "average": 80.0, "reported": 30},
                {"section": "OVERALL", "average": 77.5, "reported": 40},
            ]
        )
        self.assertEqual(result["average"], 77.5)
        self.assertEqual(result["reported_students"], 40)
        self.assertEqual(result["included_section_rows"], 2)

    def test_missing_values_do_not_produce_a_selected_course_average(self) -> None:
        summary = analyze_course_rows(
            [
                {"section": "001", "average": None, "reported": 15, "detail": ""},
                {"section": "OVERALL", "average": None, "reported": 15, "detail": ""},
            ]
        )
        self.assertIsNone(summary["latest_available_average"])
        self.assertEqual(summary["grade_source"], "unavailable_no_single_usable_overall")
        self.assertIsNone(summary["weighted_sections_diagnostic"]["average"])

    def test_non_finite_or_zero_reported_values_are_not_usable(self) -> None:
        summary = analyze_course_rows(
            [
                {"section": "001", "average": float("nan"), "reported": 8, "detail": ""},
                {"section": "OVERALL", "average": 75.0, "reported": 0, "detail": ""},
            ]
        )
        self.assertIsNone(summary["latest_available_average"])
        self.assertIsNone(summary["weighted_sections_diagnostic"]["average"])


if __name__ == "__main__":
    unittest.main()
