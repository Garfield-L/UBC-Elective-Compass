"""Small parser tests that do not make network requests."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scrape_courses import (  # noqa: E402
    ExtractionMetrics,
    derive_level,
    extract_courses_from_subject_html,
    find_subject_page_links,
    parse_credits,
    remove_schedule_notation,
)
from validate_subjects import validate_records  # noqa: E402


SUBJECT_HTML = """
<h1 class="page-title">Adult and Higher Education, Faculty of Education</h1>
<article class="node node--type-course"><h3>ADHE_V 327 (3) <strong>Teaching Adults</strong></h3>
<p>Planning adult instruction. [3-0-0]</p></article>
<article class="node node--type-course"><h3>ADHE_V 375 (variable) <strong>Seminar</strong></h3>
<p>[3-0-0]</p></article>
<article class="node node--type-course"><h3>ADHE_V 512 (3) <strong>Graduate Course</strong></h3>
<p>This is not undergraduate output.</p></article>
<article class="node node--type-course"><h3>ADHE_V 327 (3) <strong>Duplicate</strong></h3>
<p>Duplicate entry.</p></article>
"""


class CourseParserTests(unittest.TestCase):
    def test_extracts_normal_record_and_handles_exceptions(self) -> None:
        metrics = ExtractionMetrics()
        records = extract_courses_from_subject_html(
            SUBJECT_HTML, "https://example.test/course-descriptions/subject/adhev", metrics
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["course_code"], "ADHE 327")
        self.assertEqual(records[0]["description"], "Planning adult instruction.")
        self.assertEqual(records[0]["faculty_school"], "Faculty of Education")
        self.assertEqual(records[1]["credits"], None)
        self.assertEqual(records[1]["credits_raw"], "variable")
        self.assertEqual(metrics.excluded_outside_undergraduate_range, 1)
        self.assertEqual(metrics.duplicate_course_codes, 1)
        self.assertEqual(metrics.unusual_credit_formats, 1)
        self.assertEqual(metrics.missing_descriptions, 1)

    def test_collects_malformed_heading_examples(self) -> None:
        metrics = ExtractionMetrics()
        html = '<article class="node--type-course"><h3>ADHE_V 3A7 (3) Invalid</h3></article>'

        records = extract_courses_from_subject_html(html, "https://example.test/adhev", metrics)

        self.assertEqual(records, [])
        self.assertEqual(metrics.malformed_course_headings, 1)
        self.assertEqual(metrics.malformed_heading_examples, ["ADHE_V 3A7 (3) Invalid"])

    def test_derives_undergraduate_levels_only(self) -> None:
        self.assertEqual(derive_level(101), 100)
        self.assertEqual(derive_level(499), 400)
        self.assertIsNone(derive_level(500))

    def test_finds_subject_page_links(self) -> None:
        master_html = '<a href="/course-descriptions/subject/adhev">ADHE_V - Adult Education</a>'
        links = find_subject_page_links(master_html, "https://vancouver.calendar.ubc.ca")
        self.assertEqual(
            links,
            {"ADHE": "https://vancouver.calendar.ubc.ca/course-descriptions/subject/adhev"},
        )

    def test_removes_schedule_notation_at_the_end(self) -> None:
        self.assertEqual(
            remove_schedule_notation("An introduction to programming. [3-0-0]"),
            "An introduction to programming.",
        )

    def test_removes_schedule_notation_before_remaining_text(self) -> None:
        self.assertEqual(
            remove_schedule_notation("[3-2-0] Prerequisite: CPSC 103."),
            "Prerequisite: CPSC 103.",
        )

    def test_removes_multiple_schedule_segments(self) -> None:
        self.assertEqual(
            remove_schedule_notation("A two-part course. [3-0-0; 3-0-0]"),
            "A two-part course.",
        )

    def test_removes_decimal_en_dash_schedule(self) -> None:
        self.assertEqual(
            remove_schedule_notation("Studio course. [1.5–1.5-1]"),
            "Studio course.",
        )

    def test_removes_schedule_notation_with_an_asterisk(self) -> None:
        self.assertEqual(
            remove_schedule_notation("Laboratory course. [3-1*-0] Corequisite: MATH 101."),
            "Laboratory course. Corequisite: MATH 101.",
        )

    def test_preserves_non_schedule_bracketed_text(self) -> None:
        self.assertEqual(
            remove_schedule_notation("Prerequisite: [CPSC349]"),
            "Prerequisite: [CPSC349]",
        )

    def test_represents_fixed_decimal_credits(self) -> None:
        self.assertEqual(parse_credits("1.5", "MUSC 205"), 1.5)

    def test_validation_requires_raw_credits_when_numeric_credits_are_null(self) -> None:
        record = {
            "course_code": "TEST 200",
            "subject": "TEST",
            "course_number": 200,
            "title": "Example",
            "description": "Prerequisite: [TEST100]",
            "credits": None,
            "credits_raw": "1-3",
            "level": 200,
            "source_url": "https://example.test/testv",
        }

        errors, remaining_schedules, bracketed_references = validate_records([record])

        self.assertEqual(errors, [])
        self.assertEqual(remaining_schedules, 0)
        self.assertEqual(bracketed_references, 1)


if __name__ == "__main__":
    unittest.main()
