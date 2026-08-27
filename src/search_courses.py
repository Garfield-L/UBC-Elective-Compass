"""Deterministic code search and global sorting for the course API.

This module composes the Phase 2 filter instead of reimplementing its hard
filters. Search examines course and subject codes only; it is intentionally not
semantic search and never reads descriptions.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from .filter_courses import filter_courses, is_valid_average


SortBy = Literal["course_code", "highest_average"]

# Keep the small, explicit alias map in the backend as the real source of
# truth. The browser only sends the user's query to this implementation.
SUBJECT_ALIASES = {
    "CS": "CPSC",
    "COMPUTER SCIENCE": "CPSC",
    "COMP SCI": "CPSC",
    "STATS": "STAT",
}

EXACT_COURSE_CODE = 0
EXACT_SUBJECT = 1
ALIAS = 2
PREFIX = 3
SUBSTRING = 4
SUBSEQUENCE = 5


def normalize_query(value: str) -> str:
    """Uppercase a query and collapse incidental whitespace."""
    return " ".join(value.upper().split())


def normalize_course_code(value: str) -> str:
    """Uppercase a course code and ignore spacing for code comparisons."""
    return "".join(value.upper().split())


def course_subject(course: Mapping[str, Any]) -> str:
    """Read the stored subject safely for matching and deterministic sorting."""
    return str(course.get("subject", "")).upper()


def course_code(course: Mapping[str, Any]) -> str:
    """Read the stored display course code safely for matching and sorting."""
    return str(course.get("course_code", ""))


def is_ordered_subsequence(query: str, candidate: str) -> bool:
    """Return whether query characters occur in candidate in the same order."""
    position = 0
    for character in candidate:
        if position < len(query) and character == query[position]:
            position += 1
    return position == len(query)


def subject_portion_of_code_query(compact_query: str) -> str:
    """Extract a leading subject token from a query such as ``CPSC320``."""
    letters: list[str] = []
    for character in compact_query:
        if not character.isalpha():
            break
        letters.append(character)
    return "".join(letters) if len(letters) < len(compact_query) else ""


def search_match_quality(course: Mapping[str, Any], query: str) -> int | None:
    """Classify the best deterministic code/subject match for one course.

    Lower values are stronger matches. A one-character query intentionally
    stops at normal prefix/substring matching to avoid broad subsequence noise.
    """
    normalized_query = normalize_query(query)
    if not normalized_query:
        return EXACT_COURSE_CODE
    compact_query = normalize_course_code(normalized_query)
    subject = course_subject(course)
    normalized_code = normalize_course_code(course_code(course))

    if compact_query == normalized_code:
        return EXACT_COURSE_CODE
    if normalized_query == subject:
        return EXACT_SUBJECT
    if SUBJECT_ALIASES.get(normalized_query) == subject:
        return ALIAS
    if subject.startswith(compact_query) or normalized_code.startswith(compact_query):
        return PREFIX
    # A full code query still retains its subject's other courses as lower-
    # relevance fallback results. For example, CPSC 320 ranks first, followed
    # by weaker CPSC matches rather than an abruptly one-item result list.
    if subject_portion_of_code_query(compact_query) == subject:
        return SUBSTRING
    if compact_query in subject or compact_query in normalized_code:
        return SUBSTRING
    if len(compact_query) > 1 and (
        is_ordered_subsequence(compact_query, subject)
        or is_ordered_subsequence(compact_query, normalized_code)
    ):
        return SUBSEQUENCE
    return None


def usable_average(course: Mapping[str, Any]) -> float | None:
    """Return a sortable historical average only when it represents grade data."""
    average = course.get("latest_available_average")
    if course.get("grade_status") != "grade_found" or not is_valid_average(average):
        return None
    value = float(average)
    return value if math.isfinite(value) else None


def ranked_courses(
    courses: Sequence[Mapping[str, Any]],
    *,
    query: str = "",
    interests: Iterable[str] | None = None,
    higher_level: bool = False,
    high_gpa: bool = False,
    sort_by: SortBy | None = None,
) -> list[dict[str, Any]]:
    """Apply Phase 2 hard filters, then real search ranking and global sorting.

    Omitting both query and ``sort_by`` intentionally preserves the existing
    Phase 2 order for backward-compatible API clients.
    """
    filtered = filter_courses(courses, interests, higher_level, high_gpa)
    normalized_query = normalize_query(query)
    selected_interests = tuple(interests or ())

    if normalized_query:
        matched: list[tuple[int, dict[str, Any]]] = []
        for course in filtered:
            quality = search_match_quality(course, normalized_query)
            if quality is not None:
                matched.append((quality, course))
    else:
        matched = [(EXACT_COURSE_CODE, course) for course in filtered]

    # Existing clients that omit the new fields retain the Phase 2 sequence.
    if not normalized_query and sort_by is None:
        return [course for _, course in matched]

    selected_sort = sort_by or "course_code"

    def ranking_key(item: tuple[int, dict[str, Any]]) -> tuple[Any, ...]:
        quality, course = item
        key: list[Any] = []
        if normalized_query:
            key.append(quality)
        if selected_interests:
            key.append(-int(course.get("matched_interest_count", 0)))
        if selected_sort == "highest_average":
            average = usable_average(course)
            key.extend((0 if average is not None else 1, -(average or 0.0)))
        key.append(course_code(course))
        return tuple(key)

    return [course for _, course in sorted(matched, key=ranking_key)]
