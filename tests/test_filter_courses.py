"""Offline tests for the pure Phase 2 Version 1 filtering engine."""

from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from filter_courses import HIGH_GPA_THRESHOLD, filter_courses, validate_filtered_results  # noqa: E402


def course(
    code: str,
    level: int = 100,
    tags: list[str] | None = None,
    average: object = 75.0,
    grade_status: str = "grade_found",
) -> dict[str, object]:
    """Create a small record shaped like the fields used by the filter."""
    return {
        "course_code": code,
        "level": level,
        "interest_tags": tags if tags is not None else ["Technology & Computing"],
        "latest_available_average": average,
        "grade_status": grade_status,
    }


class FilterCoursesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).resolve().parents[1] / "data/ubc_courses_full_final.json"
        cls.real_courses = json.loads(path.read_text(encoding="utf-8"))

    def test_no_filters_returns_all_real_courses_without_mutating_source(self) -> None:
        before = copy.deepcopy(self.real_courses)
        results = filter_courses(self.real_courses)
        self.assertEqual(len(results), 5722)
        self.assertEqual([item["course_code"] for item in results], sorted(item["course_code"] for item in before))
        self.assertEqual(self.real_courses, before)
        self.assertNotIn("matched_interests", self.real_courses[0])

    def test_one_interest_and_or_matching(self) -> None:
        courses = [
            course("A 100", tags=["Psychology & Behaviour"]),
            course("B 100", tags=["Business & Management"]),
            course("C 100", tags=["Arts & Design"]),
        ]
        self.assertEqual(
            [item["course_code"] for item in filter_courses(courses, ["Psychology & Behaviour"])],
            ["A 100"],
        )
        self.assertEqual(
            [
                item["course_code"]
                for item in filter_courses(courses, ["Business & Management", "Psychology & Behaviour"])
            ],
            ["A 100", "B 100"],
        )

    def test_future_multi_tag_match_metadata_and_ranking(self) -> None:
        courses = [
            course("B 100", tags=["Psychology & Behaviour"]),
            course("A 100", tags=["Psychology & Behaviour", "Business & Management"]),
            course("C 100", tags=["Business & Management"]),
        ]
        results = filter_courses(courses, ["Psychology & Behaviour", "Business & Management"])
        self.assertEqual([item["course_code"] for item in results], ["A 100", "B 100", "C 100"])
        self.assertEqual(results[0]["matched_interests"], ["Psychology & Behaviour", "Business & Management"])
        self.assertEqual(results[0]["matched_interest_count"], 2)
        self.assertEqual(results[1]["matched_interests"], ["Psychology & Behaviour"])
        self.assertEqual(results[1]["matched_interest_count"], 1)

    def test_higher_level_only_allows_300_and_400(self) -> None:
        courses = [course("A 100", 100), course("B 200", 200), course("C 300", 300), course("D 400", 400)]
        results = filter_courses(courses, higher_level=True)
        self.assertEqual([item["course_code"] for item in results], ["C 300", "D 400"])
        self.assertTrue(all(item["level"] in {300, 400} for item in results))

    def test_high_gpa_boundary_and_unusable_grades(self) -> None:
        courses = [
            course("A 100", average=80.0),
            course("B 100", average=79.999),
            course("C 100", average=91.2),
            course("D 100", average=None),
            course("E 100", average=88.0, grade_status="not_found"),
            course("F 100", average=math.nan),
        ]
        results = filter_courses(courses, high_gpa=True)
        self.assertEqual([item["course_code"] for item in results], ["C 100", "A 100"])
        self.assertEqual(HIGH_GPA_THRESHOLD, 80.0)

    def test_filter_combinations_all_enforce_their_hard_rules(self) -> None:
        courses = [
            course("A 300", 300, ["Psychology & Behaviour"], 83.0),
            course("B 300", 300, ["Psychology & Behaviour"], 79.0),
            course("C 200", 200, ["Psychology & Behaviour"], 90.0),
            course("D 400", 400, ["Business & Management"], 92.0),
            course("E 400", 400, ["Psychology & Behaviour", "Business & Management"], 86.0),
        ]
        cases = [
            (["Psychology & Behaviour"], True, False),
            (["Psychology & Behaviour"], False, True),
            (None, True, True),
            (["Psychology & Behaviour"], True, True),
            (["Psychology & Behaviour", "Business & Management"], False, True),
        ]
        for interests, higher_level, high_gpa in cases:
            with self.subTest(interests=interests, higher_level=higher_level, high_gpa=high_gpa):
                results = filter_courses(courses, interests, higher_level, high_gpa)
                self.assertFalse(
                    validate_filtered_results(results, interests, higher_level, high_gpa)
                )
                if higher_level:
                    self.assertTrue(all(item["level"] in {300, 400} for item in results))
                if high_gpa:
                    self.assertTrue(all(item["latest_available_average"] >= 80.0 for item in results))

    def test_sorting_with_interests_and_high_gpa(self) -> None:
        courses = [
            course("C 100", tags=["Psychology & Behaviour"], average=90.0),
            course("B 100", tags=["Psychology & Behaviour", "Business & Management"], average=82.0),
            course("A 100", tags=["Psychology & Behaviour"], average=90.0),
        ]
        results = filter_courses(courses, ["Psychology & Behaviour", "Business & Management"], high_gpa=True)
        self.assertEqual([item["course_code"] for item in results], ["B 100", "A 100", "C 100"])

    def test_sorting_without_interests(self) -> None:
        courses = [course("C 100", average=85.0), course("B 100", average=90.0), course("A 100", average=90.0)]
        self.assertEqual(
            [item["course_code"] for item in filter_courses(courses)], ["A 100", "B 100", "C 100"]
        )
        self.assertEqual(
            [item["course_code"] for item in filter_courses(courses, high_gpa=True)],
            ["A 100", "B 100", "C 100"],
        )

    def test_unknown_or_empty_interests(self) -> None:
        courses = [course("A 100")]
        with self.assertRaisesRegex(ValueError, "Unknown interest category 'Tech'"):
            filter_courses(courses, ["Tech"])
        self.assertEqual(
            [item["course_code"] for item in filter_courses(courses, [])],
            [item["course_code"] for item in filter_courses(courses)],
        )


if __name__ == "__main__":
    unittest.main()
