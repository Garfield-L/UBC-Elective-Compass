"""Phase 6 real-data regression coverage for the completed Version 1 app."""

from __future__ import annotations

import json
import math
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.filter_courses import (  # noqa: E402
    ALLOWED_INTEREST_CATEGORIES,
    HIGH_GPA_THRESHOLD,
    filter_courses,
    has_high_historical_average,
    is_valid_average,
    validate_filtered_results,
)
from src.search_courses import (  # noqa: E402
    EXACT_COURSE_CODE,
    PREFIX,
    ranked_courses,
    search_match_quality,
    usable_average,
)


DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "ubc_courses_v1_final.json"
COURSE_CODE_PATTERN = re.compile(r"^(?P<subject>[A-Z]+) (?P<number>[1-4]\d{2})$")
MISSING_GRADE_STATUSES = {"no_grade_history", "only_detail_modifiers", "no_usable_overall"}


class PhaseSixRealDataTests(unittest.TestCase):
    """Exercise V1 invariants using the actual 3,491-record catalog."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.courses = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

    def test_dataset_identity_and_interest_invariants(self) -> None:
        self.assertEqual(len(self.courses), 3491)
        course_codes = [course["course_code"] for course in self.courses]
        self.assertEqual(len(course_codes), len(set(course_codes)))

        for course in self.courses:
            with self.subTest(course_code=course["course_code"]):
                self.assertTrue(all(course.get(field) for field in ("course_code", "subject", "title", "source_url")))
                self.assertTrue(course["subject"].isupper())
                self.assertIsInstance(course["course_number"], int)
                self.assertIn(course["level"], {100, 200, 300, 400})
                self.assertEqual(course["level"], (course["course_number"] // 100) * 100)
                matched = COURSE_CODE_PATTERN.fullmatch(course["course_code"])
                self.assertIsNotNone(matched)
                assert matched is not None
                self.assertEqual(matched.group("subject"), course["subject"])
                self.assertEqual(int(matched.group("number")), course["course_number"])
                self.assertIsInstance(course["interest_tags"], list)
                self.assertEqual(len(course["interest_tags"]), 1)
                self.assertIn(course["interest_tags"][0], ALLOWED_INTEREST_CATEGORIES)

    def test_dataset_grade_fields_are_consistent(self) -> None:
        statuses = {course["grade_status"] for course in self.courses}
        self.assertEqual(statuses, {"grade_found", *MISSING_GRADE_STATUSES})
        for course in self.courses:
            with self.subTest(course_code=course["course_code"]):
                if course["grade_status"] == "grade_found":
                    self.assertTrue(is_valid_average(course["latest_available_average"]))
                    self.assertTrue(math.isfinite(float(course["latest_available_average"])))
                    self.assertIsInstance(course["grade_session"], str)
                    self.assertGreater(course["grade_reported_students"], 0)
                else:
                    self.assertIn(course["grade_status"], MISSING_GRADE_STATUSES)
                    self.assertIsNone(course["latest_available_average"])

    def test_known_phase_four_mock_records_are_not_present(self) -> None:
        mock_records = {
            ("CS 4120", "Introduction to Compilers"),
            ("INFO 3300", "Data-Driven Web Applications"),
            ("PHIL 2310", "Introduction to Deductive Logic"),
            ("CS 314", "Data Structures"),
            ("MATH 425", "Applied Probability"),
            ("CS 370", "Intro to AI"),
        }
        actual_pairs = {(course["course_code"], course["title"]) for course in self.courses}
        self.assertFalse(actual_pairs & mock_records)

    def test_exact_search_and_whitespace_are_normalized(self) -> None:
        for query in ("CPSC 320", "CPSC320", "cpsc 320", "  CpSc 320  "):
            with self.subTest(query=query):
                results = ranked_courses(self.courses, query=query, sort_by="highest_average")
                self.assertEqual(results[0]["course_code"], "CPSC 320")
                self.assertEqual(search_match_quality(results[0], query), EXACT_COURSE_CODE)

        self.assertEqual(len(ranked_courses(self.courses, query="", sort_by="course_code")), 3491)
        self.assertEqual(len(ranked_courses(self.courses, query="   ", sort_by="course_code")), 3491)

    def test_search_matrix_only_returns_matching_courses(self) -> None:
        expected_subjects = {
            "CPSC": "CPSC",
            "SOCI": "SOCI",
            "MATH": "MATH",
            "PSYC": "PSYC",
            "COMM": "COMM",
            "C": "CPSC",
            "CP": "CPSC",
            "SOC": "SOCI",
            "STA": "STAT",
            "MA": "MATH",
            "PSC": "CPSC",
            "SOC": "SOCI",
            "CC": "CPSC",
            "CPC": "CPSC",
            "CS": "CPSC",
            "COMPUTER SCIENCE": "CPSC",
            "COMP SCI": "CPSC",
            "STATS": "STAT",
        }
        for query, expected_subject in expected_subjects.items():
            with self.subTest(query=query):
                results = ranked_courses(self.courses, query=query, sort_by="course_code")
                self.assertTrue(results)
                self.assertIn(expected_subject, {course["subject"] for course in results})
                self.assertTrue(all(search_match_quality(course, query) is not None for course in results))

    def test_search_relevance_outranks_sort_and_remains_monotonic(self) -> None:
        results = ranked_courses(self.courses, query="CPSC320", sort_by="highest_average")
        qualities = [search_match_quality(course, "CPSC320") for course in results]
        self.assertEqual(results[0]["course_code"], "CPSC 320")
        self.assertEqual(qualities[0], EXACT_COURSE_CODE)
        self.assertEqual(qualities, sorted(qualities))

        prefix_results = ranked_courses(self.courses, query="CP", sort_by="course_code")
        prefix_qualities = [search_match_quality(course, "CP") for course in prefix_results]
        first_weaker = next((index for index, quality in enumerate(prefix_qualities) if quality != PREFIX), len(prefix_qualities))
        self.assertTrue(all(quality == PREFIX for quality in prefix_qualities[:first_weaker]))

    def test_filter_combination_matrix(self) -> None:
        scenarios = [
            ("CPSC higher", "CPSC", None, True, False),
            ("CPSC average", "CPSC", None, False, True),
            ("CPSC technology", "CPSC", ["Technology & Computing"], False, False),
            ("CPSC all", "CPSC", ["Technology & Computing"], True, True),
            ("SOCI higher", "SOCI", None, True, False),
            ("SOCI average", "SOCI", None, False, True),
            ("SOCI culture", "SOCI", ["Society & Culture"], False, False),
            ("SOCI all", "SOCI", ["Society & Culture"], True, True),
            ("PSYC", "PSYC", ["Psychology & Behaviour"], False, True),
            ("MATH", "MATH", ["Mathematics & Statistics"], True, False),
            ("multiple interests", "", ["Technology & Computing", "Business & Management"], False, False),
            ("multiple interests", "", ["Psychology & Behaviour", "Society & Culture"], False, False),
        ]
        for name, query, interests, higher_level, high_gpa in scenarios:
            with self.subTest(name=name):
                results = ranked_courses(
                    self.courses,
                    query=query,
                    interests=interests,
                    higher_level=higher_level,
                    high_gpa=high_gpa,
                    sort_by="course_code",
                )
                self.assertTrue(results)
                self.assertEqual(
                    validate_filtered_results(results, interests, higher_level, high_gpa),
                    [],
                )
                if query:
                    self.assertTrue(all(search_match_quality(course, query) is not None for course in results))

    def test_every_interest_category_filters_real_courses(self) -> None:
        for interest in ALLOWED_INTEREST_CATEGORIES:
            with self.subTest(interest=interest):
                results = filter_courses(self.courses, interests=[interest])
                self.assertTrue(results)
                self.assertTrue(all(interest in course["interest_tags"] for course in results))

    def test_high_average_threshold_and_missing_grades(self) -> None:
        exactly_eighty = next(
            course
            for course in self.courses
            if course["grade_status"] == "grade_found" and course["latest_available_average"] == HIGH_GPA_THRESHOLD
        )
        below_eighty = next(
            course
            for course in self.courses
            if course["grade_status"] == "grade_found" and course["latest_available_average"] < HIGH_GPA_THRESHOLD
        )
        missing_grade = next(course for course in self.courses if course["grade_status"] != "grade_found")
        self.assertTrue(has_high_historical_average(exactly_eighty))
        self.assertFalse(has_high_historical_average(below_eighty))
        self.assertFalse(has_high_historical_average(missing_grade))
        self.assertIn(missing_grade["course_code"], {course["course_code"] for course in filter_courses(self.courses)})
        self.assertNotIn(
            missing_grade["course_code"],
            {course["course_code"] for course in filter_courses(self.courses, high_gpa=True)},
        )

    def test_global_sorting_and_pagination(self) -> None:
        by_code = ranked_courses(self.courses, sort_by="course_code")
        self.assertEqual([course["course_code"] for course in by_code], sorted(course["course_code"] for course in by_code))

        by_average = ranked_courses(self.courses, sort_by="highest_average")
        averages = [usable_average(course) for course in by_average]
        usable = [average for average in averages if average is not None]
        self.assertEqual(usable, sorted(usable, reverse=True))
        self.assertTrue(all(average is None for average in averages[len(usable) :]))

        first_page = by_average[:20]
        second_page = by_average[20:40]
        self.assertEqual(len(first_page), 20)
        self.assertEqual(len(second_page), 20)
        self.assertFalse({course["course_code"] for course in first_page} & {course["course_code"] for course in second_page})
        self.assertEqual(by_average[5000:5020], [])


if __name__ == "__main__":
    unittest.main()
