"""Apply approved subject tags to the full candidate without touching production data."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from tag_interest_categories import ALLOWED_INTEREST_CATEGORIES, load_mapping, tag_courses, write_json
from validate_subjects import validate_records


EXPECTED_CANDIDATE_COURSE_COUNT = 5722
EXPECTED_CANDIDATE_SUBJECT_COUNT = 186


def load_records(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(record, dict) for record in value):
        raise ValueError(f"Expected a JSON list of course records in {path}")
    return value


def validate_tagged_candidate(
    source_records: list[dict[str, Any]],
    tagged_records: list[dict[str, Any]],
    mapping: dict[str, str],
    v1_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check full-candidate tag coverage and unchanged base/V1 identities."""
    source_codes = [str(record.get("course_code")) for record in source_records]
    tagged_codes = [str(record.get("course_code")) for record in tagged_records]
    source_by_code = {str(record.get("course_code")): record for record in source_records}
    tagged_by_code = {str(record.get("course_code")): record for record in tagged_records}
    active_subjects = {str(record.get("subject")) for record in source_records}
    source_invariant_errors, _remaining_schedules, _references = validate_records(source_records)
    tagged_invariant_errors, _tagged_remaining_schedules, _tagged_references = validate_records(tagged_records)
    base_field_changes: list[str] = []
    invalid_tags: list[str] = []
    missing_tags: list[str] = []
    tag_length_errors: list[str] = []
    for course_code, source in source_by_code.items():
        tagged = tagged_by_code.get(course_code)
        if tagged is None:
            continue
        for field, value in source.items():
            if tagged.get(field) != value:
                base_field_changes.append(f"{course_code}: base field {field!r} changed")
        tags = tagged.get("interest_tags")
        if not isinstance(tags, list) or not tags:
            missing_tags.append(course_code)
            continue
        if len(tags) != 1:
            tag_length_errors.append(course_code)
            continue
        if tags[0] not in ALLOWED_INTEREST_CATEGORIES:
            invalid_tags.append(f"{course_code}: {tags[0]!r}")
        if tags[0] != mapping.get(source.get("subject")):
            invalid_tags.append(f"{course_code}: does not match its subject mapping")

    v1_tag_changes: list[str] = []
    v1_missing_from_candidate: list[str] = []
    for v1 in v1_records:
        course_code = str(v1.get("course_code"))
        tagged = tagged_by_code.get(course_code)
        if tagged is None:
            v1_missing_from_candidate.append(course_code)
            continue
        if tagged.get("interest_tags") != v1.get("interest_tags"):
            v1_tag_changes.append(course_code)

    duplicate_codes = len(tagged_codes) - len(set(tagged_codes))
    categories = {
        category: sum(record.get("interest_tags") == [category] for record in tagged_records)
        for category in ALLOWED_INTEREST_CATEGORIES
    }
    validation_errors = (
        source_invariant_errors
        + tagged_invariant_errors
        + base_field_changes
        + invalid_tags
        + missing_tags
        + tag_length_errors
        + v1_tag_changes
        + v1_missing_from_candidate
    )
    if len(source_records) != EXPECTED_CANDIDATE_COURSE_COUNT:
        validation_errors.append(
            f"Source candidate count {len(source_records)} is not {EXPECTED_CANDIDATE_COURSE_COUNT}"
        )
    if len(active_subjects) != EXPECTED_CANDIDATE_SUBJECT_COUNT:
        validation_errors.append(
            f"Source candidate subject count {len(active_subjects)} is not {EXPECTED_CANDIDATE_SUBJECT_COUNT}"
        )
    if len(tagged_records) != len(source_records) or Counter(source_codes) != Counter(tagged_codes):
        validation_errors.append("Tagged records do not preserve source course identities one-to-one")
    if duplicate_codes:
        validation_errors.append("Tagged records contain duplicate course codes")
    if active_subjects != set(mapping):
        validation_errors.append("Mapping keys do not exactly match candidate subjects")
    return {
        "source_course_count": len(source_records),
        "tagged_course_count": len(tagged_records),
        "represented_subjects": len(active_subjects),
        "missing_tags": len(missing_tags),
        "invalid_tags": len(invalid_tags),
        "courses_with_non_single_tag": len(tag_length_errors),
        "duplicate_course_codes": duplicate_codes,
        "base_field_changes": base_field_changes,
        "unexpected_500_plus_or_non_undergraduate_records": sum(
            not isinstance(record.get("course_number"), int)
            or not 100 <= record["course_number"] <= 499
            for record in tagged_records
        ),
        "original_v1_courses_checked": len(v1_records),
        "original_v1_courses_missing_from_tagged_candidate": v1_missing_from_candidate,
        "original_v1_interest_tags_changed": v1_tag_changes,
        "courses_per_interest_category": categories,
        "validation_errors": validation_errors,
        "is_valid": not validation_errors,
    }


def run_tagging(
    source_path: Path, mapping_path: Path, v1_final_path: Path, output_path: Path, report_path: Path
) -> dict[str, Any]:
    """Tag copied candidate records and write only candidate-named artifacts."""
    source_records = load_records(source_path)
    mapping = load_mapping(mapping_path)
    v1_records = load_records(v1_final_path)
    tagged_records = tag_courses(source_records, mapping)
    validation = validate_tagged_candidate(source_records, tagged_records, mapping, v1_records)
    report = {
        "scope": "Phase 7.5D full candidate interest tagging only; no grades or production artifacts changed.",
        "source_dataset": str(source_path),
        "mapping": str(mapping_path),
        "output_dataset": str(output_path),
        "validation": validation,
    }
    write_json(report, report_path)
    if not validation["is_valid"]:
        raise ValueError("Tagged candidate validation failed; tagged dataset was not written")
    write_json(tagged_records, output_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply approved tags to the full course candidate.")
    parser.add_argument("--source-path", type=Path, default=Path("data/ubc_courses_full_base_candidate.json"))
    parser.add_argument("--mapping-path", type=Path, default=Path("config/subject_interest_map_full_candidate.json"))
    parser.add_argument("--v1-final-path", type=Path, default=Path("data/ubc_courses_v1_final.json"))
    parser.add_argument("--output-path", type=Path, default=Path("data/ubc_courses_full_tagged_candidate.json"))
    parser.add_argument("--report-path", type=Path, default=Path("data/full_tagged_candidate_report.json"))
    args = parser.parse_args()
    try:
        report = run_tagging(args.source_path, args.mapping_path, args.v1_final_path, args.output_path, args.report_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(f"Tagged {report['validation']['tagged_course_count']} candidate courses; production data was not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
