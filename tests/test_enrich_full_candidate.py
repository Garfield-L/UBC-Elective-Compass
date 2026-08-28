"""Offline preservation tests for Phase 7.5F full candidate enrichment."""

import sys
import json
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from enrich_full_candidate import grade_field_names, merge_preserved_and_new  # noqa: E402


def base(code: str, subject: str) -> dict[str, object]:
    return {
        "course_code": code,
        "subject": subject,
        "course_number": 200,
        "title": "Candidate title",
        "description": "Candidate description",
        "credits": 3,
        "credits_raw": None,
        "level": 200,
        "faculty_school": None,
        "source_url": "https://example.test/course",
        "interest_tags": ["Technology & Computing"],
    }


class FullCandidateEnrichmentTests(unittest.TestCase):
    def test_merge_preserves_only_v1_grade_fields_over_candidate_base_data(self) -> None:
        candidate_v1 = base("V1 200", "V1")
        candidate_new = base("NEW 200", "NEW")
        v1 = {
            **candidate_v1,
            "title": "Older title that must not overwrite candidate base data",
            "grade_status": "grade_found",
            "latest_available_average": 82.0,
            "grade_session": "2025W",
        }
        new_enriched = {
            **candidate_new,
            "grade_status": "no_grade_history",
            "latest_available_average": None,
            "grade_session": None,
        }
        fields = grade_field_names([candidate_v1, candidate_new], [v1])
        merged = merge_preserved_and_new([candidate_v1, candidate_new], [v1], [new_enriched], fields)

        self.assertEqual(fields, ("grade_session", "grade_status", "latest_available_average"))
        self.assertEqual(merged[0]["title"], "Candidate title")
        self.assertEqual(merged[0]["grade_status"], "grade_found")
        self.assertEqual(merged[0]["latest_available_average"], 82.0)
        self.assertEqual(merged[1], new_enriched)

    def test_real_full_candidate_report_confirms_v1_preservation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        report = json.loads((root / "data" / "full_candidate_grade_report.json").read_text())
        validation = report["validation"]

        self.assertTrue(validation["is_valid"])
        self.assertEqual(validation["total_courses"], 5722)
        self.assertEqual(validation["original_v1_identities_checked"], 3491)
        self.assertEqual(validation["original_v1_grade_field_mismatches"], [])
        self.assertEqual(validation["original_v1_interest_tag_mismatches"], [])


if __name__ == "__main__":
    unittest.main()
