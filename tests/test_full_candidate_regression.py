"""Isolated real-data regression tests for the 7.5G expanded candidate.

These tests deliberately keep the deployed application's Version 1 dataset
unchanged.  The API test temporarily points its startup loader at the candidate
only inside its ``TestClient`` lifespan.
"""

from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import api  # noqa: E402
from src.filter_courses import (  # noqa: E402
    ALLOWED_INTEREST_CATEGORIES,
    HIGH_GPA_THRESHOLD,
    filter_courses,
    has_high_historical_average,
    validate_filtered_results,
)
from src.search_courses import (  # noqa: E402
    EXACT_COURSE_CODE,
    ranked_courses,
    search_match_quality,
    usable_average,
)


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = ROOT / "data" / "ubc_courses_full_enriched_candidate.json"
TAGGED_CANDIDATE_PATH = ROOT / "data" / "ubc_courses_full_tagged_candidate.json"
V1_PATH = ROOT / "data" / "ubc_courses_v1_final.json"
EXPECTED_CATEGORY_COUNTS = {
    "Technology & Computing": 83,
    "Mathematics & Statistics": 127,
    "Business & Management": 236,
    "Economics & Finance": 114,
    "Psychology & Behaviour": 96,
    "Biology & Life Sciences": 325,
    "Health & Medicine": 486,
    "Physical Sciences": 227,
    "Environment & Earth": 502,
    "Engineering & Applied Science": 524,
    "Society & Culture": 575,
    "Politics & Law": 304,
    "History & Civilization": 305,
    "Philosophy & Religion": 121,
    "Languages & Linguistics": 633,
    "Literature & Writing": 229,
    "Arts & Design": 474,
    "Education & Teaching": 361,
}
EXPECTED_GRADE_STATUS_COUNTS = {
    "grade_found": 3348,
    "no_grade_history": 1867,
    "only_detail_modifiers": 419,
    "no_usable_overall": 88,
    "fetch_error": 0,
}


def preserved_grade_fields(tagged_candidate: list[dict[str, object]], v1_courses: list[dict[str, object]]) -> set[str]:
    """Mirror the candidate merge's dynamic identification of grade fields."""
    candidate_fields = set().union(*(course.keys() for course in tagged_candidate))
    v1_fields = set().union(*(course.keys() for course in v1_courses))
    return v1_fields - candidate_fields


class FullCandidateRegressionTests(unittest.TestCase):
    """Exercise candidate-only compatibility with the current V1 application."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.courses = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
        cls.tagged_courses = json.loads(TAGGED_CANDIDATE_PATH.read_text(encoding="utf-8"))
        cls.v1_courses = json.loads(V1_PATH.read_text(encoding="utf-8"))

    def test_candidate_data_invariants_and_v1_preservation(self) -> None:
        self.assertEqual(len(self.courses), 5722)
        self.assertEqual(len({course["subject"] for course in self.courses}), 186)
        status_counts = Counter(course["grade_status"] for course in self.courses)
        self.assertEqual(set(status_counts), set(EXPECTED_GRADE_STATUS_COUNTS) - {"fetch_error"})
        self.assertEqual(
            {status: status_counts[status] for status in EXPECTED_GRADE_STATUS_COUNTS},
            EXPECTED_GRADE_STATUS_COUNTS,
        )

        codes = [course["course_code"] for course in self.courses]
        self.assertEqual(len(codes), len(set(codes)))
        for course in self.courses:
            with self.subTest(course_code=course["course_code"]):
                self.assertTrue(all(course.get(field) for field in ("course_code", "subject", "title", "source_url")))
                self.assertTrue(100 <= course["course_number"] <= 499)
                self.assertEqual(course["level"], (course["course_number"] // 100) * 100)
                self.assertEqual(len(course["interest_tags"]), 1)
                self.assertIn(course["interest_tags"][0], ALLOWED_INTEREST_CATEGORIES)
                if course["grade_status"] == "grade_found":
                    self.assertTrue(has_high_historical_average(course) or course["latest_available_average"] < HIGH_GPA_THRESHOLD)
                    self.assertTrue(math.isfinite(float(course["latest_available_average"])))
                else:
                    self.assertIsNone(course["latest_available_average"])

        candidate_by_code = {course["course_code"]: course for course in self.courses}
        grade_fields = preserved_grade_fields(self.tagged_courses, self.v1_courses)
        for v1_course in self.v1_courses:
            candidate = candidate_by_code.get(v1_course["course_code"])
            self.assertIsNotNone(candidate, v1_course["course_code"])
            assert candidate is not None
            self.assertEqual(candidate["interest_tags"], v1_course["interest_tags"])
            for field, value in v1_course.items():
                if field not in grade_fields:
                    self.assertEqual(candidate.get(field), value, f"{candidate['course_code']}: {field}")
            for field in grade_fields:
                self.assertEqual(candidate.get(field), v1_course.get(field), f"{candidate['course_code']}: {field}")

    def test_filter_engine_candidate_contract_and_source_immutability(self) -> None:
        before = copy.deepcopy(self.courses)
        no_filters = filter_courses(self.courses)
        higher = filter_courses(self.courses, higher_level=True)
        high_gpa = filter_courses(self.courses, high_gpa=True)
        multiple = filter_courses(
            self.courses,
            interests=["Technology & Computing", "Politics & Law"],
        )
        combined = filter_courses(
            self.courses,
            interests=["Technology & Computing", "Politics & Law"],
            higher_level=True,
            high_gpa=True,
        )

        self.assertEqual(len(no_filters), 5722)
        self.assertEqual(len(higher), 4490)
        self.assertTrue(all(course["level"] in {300, 400} for course in higher))
        self.assertTrue(all(has_high_historical_average(course) for course in high_gpa))
        self.assertTrue(all(
            set(course["interest_tags"]) & {"Technology & Computing", "Politics & Law"}
            for course in multiple
        ))
        self.assertEqual(
            validate_filtered_results(
                combined,
                ["Technology & Computing", "Politics & Law"],
                True,
                True,
            ),
            [],
        )
        self.assertEqual(self.courses, before)
        with self.assertRaisesRegex(ValueError, "Unknown interest category"):
            filter_courses(self.courses, interests=["Computer Stuff"])

    def test_every_interest_category_has_its_approved_course_count(self) -> None:
        actual_counts = Counter(course["interest_tags"][0] for course in self.courses)
        self.assertEqual(actual_counts, EXPECTED_CATEGORY_COUNTS)
        self.assertEqual(sum(actual_counts.values()), 5722)
        for interest in ALLOWED_INTEREST_CATEGORIES:
            results = filter_courses(self.courses, interests=[interest])
            self.assertTrue(results, interest)
            self.assertTrue(all(interest in course["interest_tags"] for course in results))

    def test_smart_search_rules_work_for_existing_and_new_subjects(self) -> None:
        exact_queries = ("CPSC 320", "CPSC320", "cpsc 320")
        for query in exact_queries:
            results = ranked_courses(self.courses, query=query, sort_by="highest_average")
            self.assertEqual(results[0]["course_code"], "CPSC 320")
            self.assertEqual(search_match_quality(results[0], query), EXACT_COURSE_CODE)

        expected_subjects = {
            "CPSC": "CPSC", "SOCI": "SOCI", "LAW": "LAW", "AI": "AI",
            "CS": "CPSC", "COMPUTER SCIENCE": "CPSC", "COMP SCI": "CPSC", "STATS": "STAT",
            "CP": "CPSC", "S": "SOCI", "PSC": "CPSC", "CC": "CPSC",
            "DENT": "DENT", "SPPH": "SPPH", "MINE": "MINE", "LLED": "LLED", "COGS": "COGS",
            "LAW 200": "LAW",
        }
        for query, expected_subject in expected_subjects.items():
            with self.subTest(query=query):
                results = ranked_courses(self.courses, query=query, sort_by="course_code")
                self.assertTrue(results)
                self.assertEqual(len({course["course_code"] for course in results}), len(results))
                self.assertIn(expected_subject, {course["subject"] for course in results})
                qualities = [search_match_quality(course, query) for course in results]
                self.assertTrue(all(quality is not None for quality in qualities))
                self.assertEqual(qualities, sorted(qualities))

        law = ranked_courses(self.courses, query="LAW 200", sort_by="course_code")
        self.assertEqual(law[0]["course_code"], "LAW 200")
        combined = ranked_courses(
            self.courses,
            query="CPSC",
            interests=["Technology & Computing"],
            higher_level=True,
            high_gpa=True,
            sort_by="course_code",
        )
        self.assertTrue(combined)
        self.assertTrue(all(
            course["subject"] == "CPSC"
            and course["level"] in {300, 400}
            and has_high_historical_average(course)
            for course in combined
        ))

    def test_global_sorts_and_pagination_precede_slicing(self) -> None:
        scenarios = [
            {},
            {"query": "CPSC"},
            {"interests": ["Technology & Computing"]},
            {"higher_level": True},
            {"high_gpa": True},
            {
                "query": "CPSC",
                "interests": ["Technology & Computing"],
                "higher_level": True,
                "high_gpa": True,
            },
        ]
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                code_sorted = ranked_courses(self.courses, sort_by="course_code", **scenario)
                average_sorted = ranked_courses(self.courses, sort_by="highest_average", **scenario)
                self.assertEqual(
                    [course["course_code"] for course in code_sorted],
                    sorted(course["course_code"] for course in code_sorted),
                )
                averages = [usable_average(course) for course in average_sorted]
                usable = [average for average in averages if average is not None]
                self.assertEqual(usable, sorted(usable, reverse=True))
                self.assertTrue(all(average is None for average in averages[len(usable):]))
                first, second, third = average_sorted[:20], average_sorted[20:40], average_sorted[40:60]
                self.assertFalse({course["course_code"] for course in first} & {course["course_code"] for course in second})
                self.assertFalse({course["course_code"] for course in second} & {course["course_code"] for course in third})
                self.assertEqual(first + second + third, average_sorted[:60])

    def test_candidate_api_contract_isolated_from_production_loader(self) -> None:
        production_loader = api.load_course_dataset
        self.addCleanup(setattr, api.app.state, "courses", production_loader())
        with patch.object(
            api,
            "load_course_dataset",
            side_effect=lambda: production_loader(CANDIDATE_PATH),
        ):
            with TestClient(api.app) as client:
                self.assertEqual(client.get("/health").json(), {"status": "ok", "course_count": 5722})
                self.assertEqual(client.get("/interests").json()["interests"], list(ALLOWED_INTEREST_CATEGORIES))

                no_filters = client.post("/courses/search", json={})
                self.assertEqual(no_filters.status_code, 200)
                self.assertEqual(no_filters.json()["total_results"], 5722)
                self.assertEqual(no_filters.json()["returned_results"], 50)

                exact = client.post(
                    "/courses/search",
                    json={"query": "LAW 200", "sort_by": "course_code", "limit": 20},
                )
                self.assertEqual(exact.status_code, 200)
                self.assertEqual(exact.json()["results"][0]["course_code"], "LAW 200")

                first = client.post(
                    "/courses/search",
                    json={"query": "CPSC", "sort_by": "highest_average", "limit": 20, "offset": 0},
                ).json()
                second = client.post(
                    "/courses/search",
                    json={"query": "CPSC", "sort_by": "highest_average", "limit": 20, "offset": 20},
                ).json()
                self.assertEqual(first["total_results"], second["total_results"])
                self.assertFalse({item["course_code"] for item in first["results"]} & {item["course_code"] for item in second["results"]})
                beyond = client.post("/courses/search", json={"limit": 20, "offset": 6000})
                self.assertEqual(beyond.status_code, 200)
                self.assertEqual(beyond.json()["total_results"], 5722)
                self.assertEqual(beyond.json()["returned_results"], 0)
                self.assertEqual(beyond.json()["results"], [])
                combined = client.post(
                    "/courses/search",
                    json={
                        "query": "CPSC",
                        "interests": ["Technology & Computing"],
                        "higher_level": True,
                        "high_gpa": True,
                        "sort_by": "highest_average",
                        "limit": 200,
                    },
                )
                self.assertEqual(combined.status_code, 200)
                expected = ranked_courses(
                    self.courses,
                    query="CPSC",
                    interests=["Technology & Computing"],
                    higher_level=True,
                    high_gpa=True,
                    sort_by="highest_average",
                )
                self.assertEqual(combined.json()["total_results"], len(expected))
                self.assertEqual(
                    [item["course_code"] for item in combined.json()["results"]],
                    [item["course_code"] for item in expected],
                )
                self.assertEqual(
                    client.post("/courses/search", json={"interests": ["Unknown"]}).status_code,
                    400,
                )
                self.assertEqual(client.post("/courses/search", json={"limit": 201}).status_code, 422)
                self.assertEqual(client.post("/courses/search", json={"limit": "many"}).status_code, 422)
                required_result_fields = {
                    "course_code", "subject", "course_number", "title", "description", "credits",
                    "credits_raw", "level", "faculty_school", "source_url", "interest_tags",
                    "latest_available_average", "grade_session", "grade_reported_students", "grade_status",
                    "matched_interests", "matched_interest_count",
                }
                self.assertTrue(required_result_fields <= set(no_filters.json()["results"][0]))

    def test_frontend_uses_api_metadata_not_catalog_size_or_subject_lists(self) -> None:
        frontend = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn('apiRequest("/interests")', frontend)
        self.assertIn("response.total_results", frontend)
        self.assertNotRegex(frontend, r"\b3491\b|3,491|\b5722\b|5,722|\b60\s+subjects?\b")
        self.assertNotIn("subjects_v1", frontend)


if __name__ == "__main__":
    unittest.main()
