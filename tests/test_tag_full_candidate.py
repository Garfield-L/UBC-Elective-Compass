"""Offline tests for isolated Phase 7.5D candidate tagging."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tag_full_candidate import validate_tagged_candidate  # noqa: E402
from tag_interest_categories import tag_courses  # noqa: E402


def record(subject: str, number: int = 200) -> dict[str, object]:
    return {
        "course_code": f"{subject} {number}",
        "subject": subject,
        "course_number": number,
        "title": "Example",
        "description": "Description",
        "credits": 3,
        "credits_raw": None,
        "level": 200,
        "faculty_school": None,
        "source_url": "https://example.test/course-descriptions/subject/testv",
    }


class FullCandidateTaggingTests(unittest.TestCase):
    def test_real_tagged_candidate_preserves_all_v1_interest_tags(self) -> None:
        root = Path(__file__).resolve().parents[1]
        report = json.loads((root / "data" / "full_tagged_candidate_report.json").read_text())

        validation = report["validation"]
        self.assertTrue(validation["is_valid"])
        self.assertEqual(validation["tagged_course_count"], 5722)
        self.assertEqual(validation["original_v1_interest_tags_changed"], [])

    def test_validation_detects_a_changed_base_field(self) -> None:
        source = [record("TEST")]
        mapping = {"TEST": "Technology & Computing"}
        tagged = tag_courses(source, mapping)
        tagged[0]["title"] = "Changed"

        validation = validate_tagged_candidate(source, tagged, mapping, [])
        self.assertTrue(validation["base_field_changes"])


if __name__ == "__main__":
    unittest.main()
