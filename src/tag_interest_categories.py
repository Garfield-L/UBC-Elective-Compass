"""Apply fixed Version 1 subject-to-interest tags to the enriched catalog.

Version 1 deliberately uses subject mapping only. This module does not inspect
course titles or descriptions and does not add multiple tags.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


ALLOWED_INTEREST_CATEGORIES = (
    "Technology & Computing",
    "Mathematics & Statistics",
    "Business & Management",
    "Economics & Finance",
    "Psychology & Behaviour",
    "Biology & Life Sciences",
    "Health & Medicine",
    "Physical Sciences",
    "Environment & Earth",
    "Engineering & Applied Science",
    "Society & Culture",
    "Politics & Law",
    "History & Civilization",
    "Philosophy & Religion",
    "Languages & Linguistics",
    "Literature & Writing",
    "Arts & Design",
    "Education & Teaching",
)
EXPECTED_V1_COURSE_COUNT = 3491

# These are single-category decisions made at the subject level, not a
# description-driven attempt to categorize individual courses.
AMBIGUOUS_MAPPING_DECISIONS = [
    {
        "subject": "APBI",
        "category": "Biology & Life Sciences",
        "reason": "Applied Biology includes agriculture and food systems, but its primary discipline is biology.",
    },
    {
        "subject": "CAPS",
        "category": "Health & Medicine",
        "reason": "Cellular, Anatomical and Physiological Sciences is primarily human health and physiology.",
    },
    {
        "subject": "DSCI",
        "category": "Technology & Computing",
        "reason": "Data Science overlaps statistics; Version 1 places its applied computational discipline here.",
    },
    {
        "subject": "FRE",
        "category": "Economics & Finance",
        "reason": "Food and Resource Economics is mapped by its economics discipline rather than its food context.",
    },
    {
        "subject": "GEOG",
        "category": "Environment & Earth",
        "reason": "Geography spans human and physical fields; its environment-focused primary category is used here.",
    },
    {
        "subject": "HESO",
        "category": "Health & Medicine",
        "reason": "Health and Society has social-science content but is primarily organized around health.",
    },
    {
        "subject": "ISCI",
        "category": "Physical Sciences",
        "reason": "Integrated Sciences is interdisciplinary; the broad science category is the closest permitted single tag.",
    },
    {
        "subject": "LARC",
        "category": "Arts & Design",
        "reason": "Landscape Architecture overlaps environmental planning, but its central discipline is design.",
    },
    {
        "subject": "UFOR",
        "category": "Environment & Earth",
        "reason": "Urban Forestry spans ecology, planning, and design; the environmental discipline is primary.",
    },
    {
        "subject": "URST",
        "category": "Society & Culture",
        "reason": "Urban Studies overlaps planning and geography but is treated as a social-science subject.",
    },
]


def load_subject_codes(subjects_path: Path) -> list[str]:
    """Read the Version 1 list, ignoring comments and blank lines."""
    codes = []
    for line in subjects_path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("#"):
            codes.append(cleaned)
    return codes


def load_mapping(mapping_path: Path) -> dict[str, str]:
    """Load the subject mapping as an object of code-to-category strings."""
    value = json.loads(mapping_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(
        isinstance(subject, str) and isinstance(category, str) for subject, category in value.items()
    ):
        raise ValueError(f"Expected a JSON object of string mappings in {mapping_path}")
    return value


def validate_mapping(mapping: dict[str, str], expected_subjects: list[str]) -> list[str]:
    """Check exact Version 1 coverage and exact taxonomy membership."""
    errors: list[str] = []
    expected_set = set(expected_subjects)
    if len(expected_subjects) != 60 or len(expected_set) != 60:
        errors.append("Version 1 subject configuration must contain exactly 60 unique subjects")
    unknown_subjects = sorted(set(mapping) - expected_set)
    missing_subjects = sorted(expected_set - set(mapping))
    if unknown_subjects:
        errors.append(f"Mapping includes unknown subjects: {', '.join(unknown_subjects)}")
    if missing_subjects:
        errors.append(f"Mapping is missing subjects: {', '.join(missing_subjects)}")
    if len(mapping) != len(expected_set):
        errors.append(f"Mapping has {len(mapping)} entries; expected {len(expected_set)}")
    invalid_categories = sorted(
        f"{subject}: {category}"
        for subject, category in mapping.items()
        if category not in ALLOWED_INTEREST_CATEGORIES
    )
    if invalid_categories:
        errors.append(f"Mapping has invalid categories: {', '.join(invalid_categories)}")
    return errors


def tag_courses(records: list[dict[str, Any]], mapping: dict[str, str]) -> list[dict[str, Any]]:
    """Copy records and append the one-item future-compatible interest_tags list."""
    tagged_records: list[dict[str, Any]] = []
    for record in records:
        subject = record.get("subject")
        if not isinstance(subject, str) or subject not in mapping:
            raise ValueError(f"Cannot tag course with unmapped subject: {record.get('course_code')!r}")
        tagged = dict(record)
        tagged["interest_tags"] = [mapping[subject]]
        tagged_records.append(tagged)
    return tagged_records


def validate_tagged_output(
    source_records: list[dict[str, Any]],
    tagged_records: list[dict[str, Any]],
    mapping: dict[str, str],
) -> list[str]:
    """Validate all course identities, original fields, and fixed V1 tag rules."""
    errors: list[str] = []
    source_codes = [str(record.get("course_code")) for record in source_records]
    tagged_codes = [str(record.get("course_code")) for record in tagged_records]
    if len(source_records) != EXPECTED_V1_COURSE_COUNT:
        errors.append(f"Source record count {len(source_records)} is not expected Version 1 count 3491")
    if len(tagged_records) != len(source_records):
        errors.append("Tagged output count does not match source record count")
    if Counter(source_codes) != Counter(tagged_codes):
        errors.append("Tagged output courses do not map one-to-one with source courses")
    if len(tagged_codes) != len(set(tagged_codes)):
        errors.append("Tagged output contains duplicate course codes")

    tagged_by_code = {str(record.get("course_code")): record for record in tagged_records}
    for source in source_records:
        course_code = str(source.get("course_code"))
        tagged = tagged_by_code.get(course_code)
        if tagged is None:
            errors.append(f"{course_code}: missing from tagged output")
            continue
        for field, value in source.items():
            if tagged.get(field) != value:
                errors.append(f"{course_code}: existing field {field!r} changed")
        tags = tagged.get("interest_tags")
        if not isinstance(tags, list):
            errors.append(f"{course_code}: interest_tags is not a list")
            continue
        if len(tags) != 1:
            errors.append(f"{course_code}: interest_tags must contain exactly one Version 1 tag")
            continue
        if tags[0] not in ALLOWED_INTEREST_CATEGORIES:
            errors.append(f"{course_code}: invalid interest tag {tags[0]!r}")
        expected_tag = mapping.get(source.get("subject"))
        if tags[0] != expected_tag:
            errors.append(f"{course_code}: interest tag does not match subject mapping")
    return errors


def write_json(value: Any, path: Path) -> None:
    """Write a readable local JSON result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_interest_tagging(
    source_path: Path, subjects_path: Path, mapping_path: Path, output_path: Path, report_path: Path
) -> dict[str, Any]:
    """Validate mapping, tag the local enriched catalog, and write separate artifacts."""
    expected_subjects = load_subject_codes(subjects_path)
    mapping = load_mapping(mapping_path)
    mapping_errors = validate_mapping(mapping, expected_subjects)
    if mapping_errors:
        raise ValueError("; ".join(mapping_errors))
    source_value = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(source_value, list) or not all(isinstance(record, dict) for record in source_value):
        raise ValueError(f"Expected a JSON course list in {source_path}")
    tagged_records = tag_courses(source_value, mapping)
    validation_errors = validate_tagged_output(source_value, tagged_records, mapping)
    report = {
        "scope": "Version 1 subject-mapping interest tagging only; no title or description classification",
        "source_dataset": str(source_path),
        "output_dataset": str(output_path),
        "total_courses": len(tagged_records),
        "total_subjects": len(expected_subjects),
        "courses_per_interest_category": dict(
            Counter(record["interest_tags"][0] for record in tagged_records)
        ),
        "subjects_per_interest_category": dict(Counter(mapping.values())),
        "unmapped_subjects": sorted(set(expected_subjects) - set(mapping)),
        "invalid_tags": sorted(
            {category for category in mapping.values() if category not in ALLOWED_INTEREST_CATEGORIES}
        ),
        "subject_interest_mappings": mapping,
        "ambiguous_mapping_decisions": AMBIGUOUS_MAPPING_DECISIONS,
        "validation_errors": validation_errors,
    }
    # Do not present a final output when preservation/tagging invariants fail.
    write_json(report, report_path)
    if validation_errors:
        raise ValueError("Interest-tagging invariants failed; final dataset was not written")
    write_json(tagged_records, output_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Add fixed Version 1 subject interest tags.")
    parser.add_argument(
        "--source-path", type=Path, default=Path("data/ubc_courses_v1_with_grades.json")
    )
    parser.add_argument("--subjects-path", type=Path, default=Path("config/subjects_v1.txt"))
    parser.add_argument(
        "--mapping-path", type=Path, default=Path("config/subject_interest_map.json")
    )
    parser.add_argument("--output-path", type=Path, default=Path("data/ubc_courses_v1_final.json"))
    parser.add_argument(
        "--report-path", type=Path, default=Path("data/interest_tagging_report.json")
    )
    args = parser.parse_args()
    try:
        report = run_interest_tagging(
            args.source_path, args.subjects_path, args.mapping_path, args.output_path, args.report_path
        )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))
    print(f"Tagged {report['total_courses']} courses; report: {args.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
