"""Real-data API tests for the small Phase 3 FastAPI backend."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import LOCAL_DEVELOPMENT_ORIGINS, app, configured_allowed_origins  # noqa: E402
from src.filter_courses import ALLOWED_INTEREST_CATEGORIES, filter_courses  # noqa: E402
from src.search_courses import PREFIX, search_match_quality  # noqa: E402


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)

    def post_search(self, **body: object):
        return self.client.post("/courses/search", json=body)

    def test_health_uses_the_loaded_real_dataset(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "course_count": 5722})

    def test_interests_matches_the_phase_two_taxonomy(self) -> None:
        response = self.client.get("/interests")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["interests"], list(ALLOWED_INTEREST_CATEGORIES))
        self.assertEqual(len(response.json()["interests"]), 18)

    def test_no_filter_search_uses_defaults_and_preserves_source_records(self) -> None:
        before = copy.deepcopy(app.state.courses)
        response = self.post_search()
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["total_results"], 5722)
        self.assertEqual(body["returned_results"], 50)
        self.assertEqual(body["limit"], 50)
        self.assertEqual(body["offset"], 0)
        self.assertEqual(app.state.courses, before)
        self.assertNotIn("matched_interests", app.state.courses[0])

    def test_psychology_interest_filter(self) -> None:
        response = self.post_search(interests=["Psychology & Behaviour"], limit=200)
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertTrue(results)
        self.assertTrue(
            all("Psychology & Behaviour" in item["interest_tags"] for item in results)
        )
        self.assertTrue(
            all(item["matched_interests"] == ["Psychology & Behaviour"] for item in results)
        )

    def test_technology_high_gpa_agrees_with_the_phase_two_engine(self) -> None:
        response = self.post_search(
            interests=["Technology & Computing"], high_gpa=True, limit=200
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        expected = filter_courses(
            app.state.courses, interests=["Technology & Computing"], high_gpa=True
        )
        self.assertEqual(body["total_results"], len(expected))
        self.assertEqual(
            [item["course_code"] for item in body["results"]],
            [item["course_code"] for item in expected],
        )
        self.assertTrue(
            all(
                "Technology & Computing" in item["interest_tags"]
                and item["grade_status"] == "grade_found"
                and item["latest_available_average"] >= 80.0
                for item in body["results"]
            )
        )

    def test_higher_level_and_all_filters_apply_to_every_result(self) -> None:
        higher_response = self.post_search(higher_level=True, limit=200)
        self.assertEqual(higher_response.status_code, 200)
        self.assertEqual(higher_response.json()["total_results"], 4490)
        self.assertTrue(all(item["level"] in {300, 400} for item in higher_response.json()["results"]))

        high_gpa_response = self.post_search(high_gpa=True, limit=200)
        self.assertEqual(high_gpa_response.status_code, 200)
        self.assertEqual(high_gpa_response.json()["total_results"], 1838)
        self.assertTrue(
            all(
                item["grade_status"] == "grade_found"
                and item["latest_available_average"] >= 80.0
                for item in high_gpa_response.json()["results"]
            )
        )

        all_response = self.post_search(
            interests=["Psychology & Behaviour"], higher_level=True, high_gpa=True, limit=200
        )
        self.assertEqual(all_response.status_code, 200)
        self.assertTrue(all_response.json()["results"])
        self.assertTrue(
            all(
                "Psychology & Behaviour" in item["interest_tags"]
                and item["level"] in {300, 400}
                and item["grade_status"] == "grade_found"
                and item["latest_available_average"] >= 80.0
                for item in all_response.json()["results"]
            )
        )

    def test_pagination_preserves_the_filter_engine_order(self) -> None:
        first_page = self.post_search(limit=5, offset=0)
        second_page = self.post_search(limit=5, offset=5)
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(second_page.status_code, 200)
        first_codes = [item["course_code"] for item in first_page.json()["results"]]
        second_codes = [item["course_code"] for item in second_page.json()["results"]]
        expected_codes = [item["course_code"] for item in filter_courses(app.state.courses)[:10]]
        self.assertEqual(first_codes + second_codes, expected_codes)
        self.assertNotEqual(first_codes, second_codes)

    def test_offset_beyond_results_returns_an_empty_page(self) -> None:
        response = self.post_search(offset=6000)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_results"], 5722)
        self.assertEqual(response.json()["returned_results"], 0)
        self.assertEqual(response.json()["results"], [])

    def test_invalid_pagination_values_use_fastapi_validation(self) -> None:
        for body in ({"limit": 201}, {"limit": 0}, {"offset": -1}):
            with self.subTest(body=body):
                self.assertEqual(self.post_search(**body).status_code, 422)

    def test_unknown_interest_is_a_clear_client_error(self) -> None:
        response = self.post_search(interests=["Computer Stuff"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown interest category", response.json()["detail"])

    def test_normalized_exact_course_search_ranks_the_exact_course_first(self) -> None:
        for query in ("CPSC 320", "CPSC320", "cpsc 320", " CPSC 320 "):
            with self.subTest(query=query):
                response = self.post_search(query=query, sort_by="highest_average", limit=20)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["results"][0]["course_code"], "CPSC 320")

    def test_subject_prefix_alias_substring_and_subsequence_searches(self) -> None:
        checks = {
            "CPSC 320": "CPSC",
            "CPSC": "CPSC",
            "CP": "CPSC",
            "SOC": "SOCI",
            "SOCI": "SOCI",
            "LAW": "LAW",
            "AI": "AI",
            "PSC": "CPSC",
            "CC": "CPSC",
            "CS": "CPSC",
        }
        for query, expected_subject in checks.items():
            with self.subTest(query=query):
                response = self.post_search(query=query, sort_by="course_code", limit=200)
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected_subject, {item["subject"] for item in response.json()["results"]})

    def test_one_character_prefix_matches_rank_before_weaker_matches(self) -> None:
        response = self.post_search(query="S", sort_by="course_code", limit=200)
        self.assertEqual(response.status_code, 200)
        qualities = [search_match_quality(item, "S") for item in response.json()["results"]]
        self.assertIn(PREFIX, qualities)
        first_non_prefix = next((index for index, value in enumerate(qualities) if value != PREFIX), len(qualities))
        self.assertTrue(all(value == PREFIX for value in qualities[:first_non_prefix]))

    def test_query_combines_with_all_hard_filters(self) -> None:
        response = self.post_search(
            query="CPSC",
            interests=["Technology & Computing"],
            higher_level=True,
            high_gpa=True,
            sort_by="course_code",
            limit=200,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["results"])
        self.assertTrue(
            all(
                item["subject"] == "CPSC"
                and "Technology & Computing" in item["interest_tags"]
                and item["level"] in {300, 400}
                and item["grade_status"] == "grade_found"
                and item["latest_available_average"] >= 80.0
                for item in response.json()["results"]
            )
        )

    def test_global_sorts_and_pagination_are_applied_before_page_slicing(self) -> None:
        full_response = self.post_search(sort_by="highest_average", limit=200)
        page_one = self.post_search(sort_by="highest_average", limit=20, offset=0)
        page_two = self.post_search(sort_by="highest_average", limit=20, offset=20)
        self.assertEqual(full_response.status_code, 200)
        self.assertEqual(page_one.status_code, 200)
        self.assertEqual(page_two.status_code, 200)
        full_codes = [item["course_code"] for item in full_response.json()["results"]]
        page_codes = [item["course_code"] for item in page_one.json()["results"] + page_two.json()["results"]]
        self.assertEqual(page_codes, full_codes[:40])

        code_response = self.post_search(sort_by="course_code", limit=200)
        code_values = [item["course_code"] for item in code_response.json()["results"]]
        self.assertEqual(code_values, sorted(code_values))

    def test_invalid_sort_and_local_cors_are_clear(self) -> None:
        invalid = self.post_search(sort_by="relevance")
        self.assertEqual(invalid.status_code, 422)
        cors = self.client.get("/health", headers={"Origin": "http://127.0.0.1:5500"})
        self.assertEqual(cors.headers.get("access-control-allow-origin"), "http://127.0.0.1:5500")

    def test_local_frontend_preflight_is_allowed(self) -> None:
        response = self.client.options(
            "/courses/search",
            headers={
                "Origin": "http://127.0.0.1:5501",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://127.0.0.1:5501")
        self.assertIn("POST", response.headers.get("access-control-allow-methods", ""))

    def test_production_origins_are_added_from_a_clear_comma_separated_value(self) -> None:
        configured = configured_allowed_origins(
            " https://example-one.test ,https://example-two.test/ , https://example-one.test "
        )
        self.assertEqual(
            configured,
            [*LOCAL_DEVELOPMENT_ORIGINS, "https://example-one.test", "https://example-two.test"],
        )
        with self.assertRaisesRegex(ValueError, "explicit origins"):
            configured_allowed_origins("*")


if __name__ == "__main__":
    unittest.main()
