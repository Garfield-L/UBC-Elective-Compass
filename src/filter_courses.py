"""Pure, deterministic Version 1 filtering for UBC Course Finder courses.

The functions here work with loaded course dictionaries and never write to the
catalogue.  Returned dictionaries are shallow copies with search-result-only
interest-match fields.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

try:  # Support both ``python src/filter_courses.py`` and package imports later.
    from .tag_interest_categories import ALLOWED_INTEREST_CATEGORIES
except ImportError:  # pragma: no cover - exercised by the command-line script.
    from tag_interest_categories import ALLOWED_INTEREST_CATEGORIES


HIGH_GPA_THRESHOLD = 80.0
HIGHER_LEVELS = frozenset({300, 400})
EXPECTED_PRODUCTION_COURSE_COUNT = 5722


def normalize_interests(interests: Iterable[str] | None) -> tuple[str, ...]:
    """Validate selected interests and remove repeated selections.

    The order supplied by the caller is retained so the result is predictable.
    A single string is rejected because it is usually an accidental substitute
    for a list of selected categories.
    """
    if interests is None:
        return ()
    if isinstance(interests, str):
        raise ValueError("interests must be an iterable of exact interest category strings, not one string")

    selected: list[str] = []
    for interest in interests:
        if not isinstance(interest, str):
            raise ValueError("Each selected interest must be an exact interest category string")
        if interest not in ALLOWED_INTEREST_CATEGORIES:
            allowed = ", ".join(ALLOWED_INTEREST_CATEGORIES)
            raise ValueError(f"Unknown interest category {interest!r}. Allowed categories: {allowed}")
        if interest not in selected:
            selected.append(interest)
    return tuple(selected)


def is_valid_average(value: object) -> bool:
    """Return whether a value is a finite numeric historical average."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def has_high_historical_average(course: Mapping[str, Any]) -> bool:
    """Apply the V1 high-GPA rule without treating unknown grades as low."""
    average = course.get("latest_available_average")
    return (
        course.get("grade_status") == "grade_found"
        and is_valid_average(average)
        and float(average) >= HIGH_GPA_THRESHOLD
    )


def matched_interests_for_course(
    course: Mapping[str, Any], selected_interests: tuple[str, ...]
) -> list[str]:
    """Return selected categories present in the course's tag list, in tag order."""
    tags = course.get("interest_tags")
    if not isinstance(tags, list):
        return []
    selected_set = set(selected_interests)
    matched: list[str] = []
    for tag in tags:
        if isinstance(tag, str) and tag in selected_set and tag not in matched:
            matched.append(tag)
    return matched


def course_code_sort_value(course: Mapping[str, Any]) -> str:
    """Provide a stable final sort value even for incomplete synthetic records."""
    return str(course.get("course_code", ""))


def filter_courses(
    courses: Sequence[Mapping[str, Any]],
    interests: Iterable[str] | None = None,
    higher_level: bool = False,
    high_gpa: bool = False,
) -> list[dict[str, Any]]:
    """Return copied course records that meet all active Version 1 filters.

    Interest selections use OR matching.  The higher-level and high-GPA
    switches are hard filters, so a returned record always satisfies every
    enabled condition.
    """
    selected_interests = normalize_interests(interests)
    results: list[dict[str, Any]] = []

    for course in courses:
        matched_interests = matched_interests_for_course(course, selected_interests)
        if selected_interests and not matched_interests:
            continue
        if higher_level and course.get("level") not in HIGHER_LEVELS:
            continue
        if high_gpa and not has_high_historical_average(course):
            continue

        result = dict(course)
        result["matched_interests"] = matched_interests
        result["matched_interest_count"] = len(matched_interests)
        results.append(result)

    if selected_interests:
        if high_gpa:
            results.sort(
                key=lambda course: (
                    -int(course["matched_interest_count"]),
                    -float(course["latest_available_average"]),
                    course_code_sort_value(course),
                )
            )
        else:
            results.sort(
                key=lambda course: (-int(course["matched_interest_count"]), course_code_sort_value(course))
            )
    elif high_gpa:
        results.sort(
            key=lambda course: (-float(course["latest_available_average"]), course_code_sort_value(course))
        )
    else:
        results.sort(key=course_code_sort_value)
    return results


def validate_filtered_results(
    results: Sequence[Mapping[str, Any]],
    interests: Iterable[str] | None = None,
    higher_level: bool = False,
    high_gpa: bool = False,
) -> list[str]:
    """Check that every returned result meets the enabled hard-filter rules."""
    selected_interests = normalize_interests(interests)
    errors: list[str] = []
    for course in results:
        course_code = course_code_sort_value(course)
        matched = matched_interests_for_course(course, selected_interests)
        if selected_interests and not matched:
            errors.append(f"{course_code}: does not match a selected interest")
        if course.get("matched_interests") != matched:
            errors.append(f"{course_code}: matched_interests is inconsistent")
        if course.get("matched_interest_count") != len(matched):
            errors.append(f"{course_code}: matched_interest_count is inconsistent")
        if higher_level and course.get("level") not in HIGHER_LEVELS:
            errors.append(f"{course_code}: is not a 300- or 400-level course")
        if high_gpa and not has_high_historical_average(course):
            errors.append(f"{course_code}: does not meet the high-GPA rule")
    return errors


def run_real_data_validation(courses: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Run the nine required Phase 2 searches and return a JSON-ready report."""
    scenarios = [
        ("no_filters", None, False, False),
        ("psychology_only", ["Psychology & Behaviour"], False, False),
        ("technology_only", ["Technology & Computing"], False, False),
        (
            "business_or_psychology",
            ["Business & Management", "Psychology & Behaviour"],
            False,
            False,
        ),
        ("higher_level_only", None, True, False),
        ("high_gpa_only", None, False, True),
        ("psychology_and_higher_level", ["Psychology & Behaviour"], True, False),
        ("technology_and_high_gpa", ["Technology & Computing"], False, True),
        ("psychology_higher_level_and_high_gpa", ["Psychology & Behaviour"], True, True),
    ]
    scenario_reports: list[dict[str, Any]] = []
    all_errors: list[str] = []
    for name, interests, higher_level, high_gpa in scenarios:
        results = filter_courses(courses, interests, higher_level, high_gpa)
        errors = validate_filtered_results(results, interests, higher_level, high_gpa)
        if name == "no_filters" and len(results) != EXPECTED_PRODUCTION_COURSE_COUNT:
            errors.append(
                f"no_filters returned {len(results)} courses; expected {EXPECTED_PRODUCTION_COURSE_COUNT}"
            )
        all_errors.extend(f"{name}: {error}" for error in errors)
        scenario_report: dict[str, Any] = {
            "name": name,
            "active_filters": {
                "interests": list(normalize_interests(interests)),
                "higher_level": higher_level,
                "high_gpa": high_gpa,
            },
            "result_count": len(results),
            "first_10_course_codes": [course_code_sort_value(course) for course in results[:10]],
            "validation_errors": errors,
        }
        if high_gpa:
            scenario_report["first_10_historical_averages"] = [
                course["latest_available_average"] for course in results[:10]
            ]
        scenario_reports.append(scenario_report)
    return {
        "dataset_course_count": len(courses),
        "expected_production_course_count": EXPECTED_PRODUCTION_COURSE_COUNT,
        "high_gpa_threshold": HIGH_GPA_THRESHOLD,
        "scenarios": scenario_reports,
        "validation_errors": all_errors,
    }


def write_json(value: Any, path: Path) -> None:
    """Write the validation report without changing the source catalog."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    """Run the required real-data checks and save their small report."""
    parser = argparse.ArgumentParser(description="Validate Phase 2 course filtering.")
    parser.add_argument("--input-path", type=Path, default=Path("data/ubc_courses_full_final.json"))
    parser.add_argument(
        "--report-path", type=Path, default=Path("data/phase2_filter_validation_report.json")
    )
    args = parser.parse_args()
    try:
        courses = json.loads(args.input_path.read_text(encoding="utf-8"))
        if not isinstance(courses, list) or not all(isinstance(course, dict) for course in courses):
            raise ValueError(f"Expected a JSON list of course records in {args.input_path}")
        report = run_real_data_validation(courses)
        write_json(report, args.report_path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))
    if report["validation_errors"]:
        print(f"Validation failed; report: {args.report_path}")
        return 1
    print(f"Validated {len(report['scenarios'])} searches; report: {args.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
