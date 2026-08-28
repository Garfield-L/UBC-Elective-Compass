"""Offline tests for the isolated full-catalog candidate builder."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from build_full_catalog_candidate import (  # noqa: E402
    EXPECTED_FULL_SUBJECT_COUNT,
    base_schema_summary,
    compare_with_v1,
    read_subject_codes,
)


def record(subject: str, number: int, credits: int | None = 3, credits_raw: str | None = None) -> dict[str, object]:
    return {
        "course_code": f"{subject} {number}",
        "subject": subject,
        "course_number": number,
        "title": "Example",
        "description": "Prerequisite: [TEST100]",
        "credits": credits,
        "credits_raw": credits_raw,
        "level": (number // 100) * 100,
        "faculty_school": None,
        "source_url": "https://example.test/course-descriptions/subject/testv",
    }


class FullCatalogCandidateTests(unittest.TestCase):
    def test_full_candidate_config_has_exactly_the_audited_264_unique_codes(self) -> None:
        config = Path(__file__).resolve().parents[1] / "config" / "subjects_full.txt"
        codes = read_subject_codes(config, EXPECTED_FULL_SUBJECT_COUNT)

        self.assertEqual(len(codes), 264)
        self.assertEqual(len(set(codes)), 264)
        self.assertEqual(codes[0], "AANB")
        self.assertEqual(codes[-1], "ZOOL")

    def test_existing_v1_comments_are_not_counted_as_subject_codes(self) -> None:
        config = Path(__file__).resolve().parents[1] / "config" / "subjects_v1.txt"
        codes = read_subject_codes(config, 60)

        self.assertEqual(len(codes), 60)

    def test_schema_summary_accepts_valid_variable_credits(self) -> None:
        summary = base_schema_summary([record("TEST", 200, credits=None, credits_raw="3-6")])

        self.assertEqual(summary["total_records"], 1)
        self.assertEqual(summary["null_credits_with_valid_credits_raw"], 1)
        self.assertEqual(summary["unexpected_500_plus_or_non_undergraduate_records"], 0)
        self.assertEqual(summary["schema_validation_failures"], [])

    def test_v1_identity_comparison_ignores_non_identity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            v1_path = Path(temporary_directory) / "v1.json"
            v1_path.write_text(
                json.dumps([
                    {**record("TEST", 100), "interest_tags": ["Technology & Computing"], "grade_status": "grade_found"},
                    record("TEST", 200),
                ]),
                encoding="utf-8",
            )
            comparison = compare_with_v1(
                [record("TEST", 100), record("TEST", 300), record("NEW", 100)],
                ("TEST",),
                v1_path,
            )

        self.assertEqual(comparison["v1_course_identities_found_in_candidate"], 1)
        self.assertEqual(comparison["v1_course_identities_missing_from_candidate"], ["TEST 200"])
        self.assertEqual(comparison["new_course_identities_in_original_60_subjects"], ["TEST 300"])
        self.assertEqual(comparison["course_identities_contributed_by_new_subject_codes"], 1)


if __name__ == "__main__":
    unittest.main()
