"""Offline unit tests for Phase 5 deterministic course-code search."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.search_courses import (  # noqa: E402
    ALIAS,
    EXACT_COURSE_CODE,
    EXACT_SUBJECT,
    PREFIX,
    SUBSEQUENCE,
    SUBSTRING,
    ranked_courses,
    search_match_quality,
)


def course(
    code: str,
    subject: str,
    average: object = 80.0,
    tags: list[str] | None = None,
) -> dict[str, object]:
    return {
        "course_code": code,
        "subject": subject,
        "level": 300,
        "interest_tags": tags or ["Technology & Computing"],
        "grade_status": "grade_found",
        "latest_available_average": average,
    }


class SmartSearchTests(unittest.TestCase):
    def test_exact_normalized_code_outranks_subject_matches(self) -> None:
        records = [course("CPSC 110", "CPSC"), course("CPSC 320", "CPSC")]
        results = ranked_courses(records, query=" cpsc320 ", sort_by="highest_average")
        self.assertEqual(results[0]["course_code"], "CPSC 320")
        self.assertEqual(results[1]["course_code"], "CPSC 110")
        self.assertEqual(search_match_quality(records[1], "CPSC320"), EXACT_COURSE_CODE)

    def test_subject_alias_prefix_substring_and_subsequence_match_types(self) -> None:
        cpsc = course("CPSC 320", "CPSC")
        self.assertEqual(search_match_quality(cpsc, "CPSC"), EXACT_SUBJECT)
        self.assertEqual(search_match_quality(cpsc, "CS"), ALIAS)
        self.assertEqual(search_match_quality(cpsc, "CP"), PREFIX)
        self.assertEqual(search_match_quality(cpsc, "PSC"), SUBSTRING)
        self.assertEqual(search_match_quality(cpsc, "CC"), SUBSEQUENCE)

    def test_search_is_case_insensitive_and_does_not_read_descriptions(self) -> None:
        cpsc = course("CPSC 320", "CPSC")
        cpsc["description"] = "This word must not affect code search."
        self.assertEqual(search_match_quality(cpsc, "cpsc 320"), EXACT_COURSE_CODE)
        self.assertIsNone(search_match_quality(cpsc, "word"))

    def test_relevance_precedes_selected_sort(self) -> None:
        records = [
            course("CPSC 110", "CPSC", 99.0),
            course("CPSC 320", "CPSC", 70.0),
        ]
        results = ranked_courses(records, query="CPSC320", sort_by="highest_average")
        self.assertEqual(results[0]["course_code"], "CPSC 320")

    def test_highest_average_places_unusable_values_after_usable_ones(self) -> None:
        records = [
            course("CPSC 300", "CPSC", None),
            course("CPSC 301", "CPSC", 85.0),
            course("CPSC 302", "CPSC", 90.0),
        ]
        results = ranked_courses(records, sort_by="highest_average")
        self.assertEqual(
            [record["course_code"] for record in results],
            ["CPSC 302", "CPSC 301", "CPSC 300"],
        )

    def test_selected_interest_count_precedes_selected_sort(self) -> None:
        multi_tag = course(
            "CPSC 301",
            "CPSC",
            81.0,
            ["Technology & Computing", "Business & Management"],
        )
        one_tag = course("CPSC 302", "CPSC", 99.0, ["Technology & Computing"])
        results = ranked_courses(
            [one_tag, multi_tag],
            interests=["Technology & Computing", "Business & Management"],
            sort_by="highest_average",
        )
        self.assertEqual(results[0]["course_code"], "CPSC 301")

    def test_query_and_hard_filters_are_anded(self) -> None:
        records = [
            course("CPSC 200", "CPSC", 95.0),
            course("CPSC 300", "CPSC", 79.0),
            course("CPSC 400", "CPSC", 90.0),
        ]
        records[0]["level"] = 200
        results = ranked_courses(records, query="CPSC", higher_level=True, high_gpa=True)
        self.assertEqual([record["course_code"] for record in results], ["CPSC 400"])


if __name__ == "__main__":
    unittest.main()
