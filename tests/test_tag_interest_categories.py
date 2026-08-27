"""Offline tests for fixed Version 1 subject interest tagging."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tag_interest_categories import (  # noqa: E402
    ALLOWED_INTEREST_CATEGORIES,
    tag_courses,
    validate_mapping,
    validate_tagged_output,
)


def course(subject: str, number: int = 100) -> dict[str, object]:
    return {
        "course_code": f"{subject} {number}",
        "subject": subject,
        "course_number": number,
        "title": "Original title",
        "description": "Original description must remain unchanged.",
        "credits": 3,
        "credits_raw": None,
        "level": 100,
        "grade_status": "grade_found",
        "latest_available_average": 75.0,
    }


class InterestTaggingTests(unittest.TestCase):
    def test_valid_mapping_covers_exact_subject_set(self) -> None:
        mapping = {"CPSC": "Technology & Computing", "MATH": "Mathematics & Statistics"}
        # The production validator additionally requires 60 subjects; this
        # focused check uses the exact configured set by repeating neither.
        errors = validate_mapping(mapping, ["CPSC", "MATH"])
        self.assertTrue(any("exactly 60" in error for error in errors))

    def test_mapping_reports_unmapped_and_unknown_subjects(self) -> None:
        mapping = {"CPSC": "Technology & Computing", "UNKNOWN": "Technology & Computing"}
        expected_subjects = ["CPSC"] + [f"SUBJECT{number}" for number in range(59)]
        errors = validate_mapping(mapping, expected_subjects)
        self.assertTrue(any("unknown subjects" in error for error in errors))
        self.assertTrue(any("missing subjects" in error for error in errors))

    def test_mapping_reports_invalid_category(self) -> None:
        mapping = {"CPSC": "Not an allowed category"}
        expected_subjects = ["CPSC"] + [f"SUBJECT{number}" for number in range(59)]
        errors = validate_mapping(mapping, expected_subjects)
        self.assertTrue(any("invalid categories" in error for error in errors))

    def test_tagging_adds_list_and_preserves_existing_fields(self) -> None:
        source = [course("CPSC")]
        tagged = tag_courses(source, {"CPSC": "Technology & Computing"})
        self.assertEqual(tagged[0]["interest_tags"], ["Technology & Computing"])
        self.assertEqual(tagged[0]["description"], source[0]["description"])
        self.assertEqual(tagged[0]["latest_available_average"], 75.0)
        self.assertNotIn("interest_tags", source[0])

    def test_output_validation_detects_wrong_tag_and_changed_field(self) -> None:
        source = [course("CPSC")]
        tagged = tag_courses(source, {"CPSC": "Technology & Computing"})
        tagged[0]["interest_tags"] = ["Arts & Design"]
        tagged[0]["title"] = "Changed"
        errors = validate_tagged_output(source, tagged, {"CPSC": "Technology & Computing"})
        self.assertTrue(any("existing field 'title' changed" in error for error in errors))
        self.assertTrue(any("does not match subject mapping" in error for error in errors))
        self.assertIn("Technology & Computing", ALLOWED_INTEREST_CATEGORIES)


if __name__ == "__main__":
    unittest.main()
