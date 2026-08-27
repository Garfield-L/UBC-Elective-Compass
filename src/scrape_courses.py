"""Download and normalize undergraduate UBC Vancouver course descriptions.

This module is intentionally limited to one subject page per run.  It includes
the functions needed for a future, polite catalog-wide run, but the command-line
interface defaults to the ADHE_V page and does not crawl all subjects.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag


MASTER_SUBJECT_URL = "https://vancouver.calendar.ubc.ca/course-descriptions/courses-subject"
ADHE_SUBJECT_URL = "https://vancouver.calendar.ubc.ca/course-descriptions/subject/adhev"
USER_AGENT = "UBC-Course-Finder/0.1 (educational course catalog project)"
REQUEST_TIMEOUT_SECONDS = 20
POLITE_DELAY_SECONDS = 1.0

# The title is optional because a malformed header should be reported instead of
# causing every other course on a page to be lost.
COURSE_HEADER_PATTERN = re.compile(
    r"^(?P<subject>[A-Z0-9]+)_V\s+(?P<number>\d+)\s*"
    r"\((?P<credits>[^)]+)\)\s*(?P<title>.+)$"
)
# A Calendar schedule is made of numeric parts (for example, 3-0-0), not just
# any bracketed text.  Requiring at least one hyphen/en dash preserves useful
# non-schedule text such as ``[CPSC349]``.
# Some Calendar schedules mark a component with an asterisk, for example
# ``[3-1*-0]``. It remains schedule notation, not description content.
SCHEDULE_NUMBER_PATTERN = r"\d+(?:\.\d+)?\*?"
SCHEDULE_SEGMENT_PATTERN = (
    rf"{SCHEDULE_NUMBER_PATTERN}(?:\s*[-–]\s*{SCHEDULE_NUMBER_PATTERN})+"
)
SCHEDULE_PATTERN = re.compile(
    rf"\[\s*{SCHEDULE_SEGMENT_PATTERN}(?:\s*;\s*{SCHEDULE_SEGMENT_PATTERN})*\s*\]"
)
UNDERGRADUATE_LEVELS = {100, 200, 300, 400}


@dataclass
class ExtractionMetrics:
    """Counts produced while parsing one subject page.

    Keeping these counts beside the parser makes small validation runs easier
    to review without changing which records are written to JSON.
    """

    excluded_outside_undergraduate_range: int = 0
    malformed_course_headings: int = 0
    malformed_heading_examples: list[str] = field(default_factory=list)
    unusual_credit_formats: int = 0
    duplicate_course_codes: int = 0
    missing_titles: int = 0
    missing_descriptions: int = 0
    missing_or_unrepresentable_credits: int = 0
    null_faculty_school_records: int = 0
    other_parsing_errors: int = 0


def configure_logging(verbose: bool) -> None:
    """Show useful parsing warnings without making normal output too noisy."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )


def make_http_session() -> requests.Session:
    """Create a session with the identifying user agent required for requests."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch_html(session: requests.Session, url: str) -> tuple[str, str]:
    """Fetch one Calendar page with a finite timeout.

    No retry or access-control workaround is used.  A future multi-page caller
    should wait between calls with ``polite_delay``.
    """
    response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text, response.url


def polite_delay() -> None:
    """Pause between future multi-page requests."""
    time.sleep(POLITE_DELAY_SECONDS)


def find_subject_page_links(master_html: str, master_url: str = MASTER_SUBJECT_URL) -> dict[str, str]:
    """Return Calendar subject codes and URLs found on the master subject page.

    This function only reads supplied HTML; it does not fetch every subject.
    """
    soup = BeautifulSoup(master_html, "html.parser")
    subject_links: dict[str, str] = {}

    for link in soup.select('a[href*="/course-descriptions/subject/"]'):
        label = link.get_text(" ", strip=True)
        match = re.match(r"^([A-Z0-9]+)_V\b", label)
        href = link.get("href")
        if not match or not isinstance(href, str):
            continue

        subject_code = match.group(1)
        subject_links[subject_code] = urljoin(master_url, href)

    return subject_links


def extract_faculty_school(soup: BeautifulSoup) -> str | None:
    """Extract the faculty/school only when the page heading states it clearly."""
    heading = soup.select_one("h1.page-title")
    if heading is None:
        return None

    heading_text = heading.get_text(" ", strip=True)
    _subject_name, separator, possible_faculty = heading_text.rpartition(", ")
    if not separator:
        return None

    if re.search(r"\b(Faculty|School|College)\b", possible_faculty, re.IGNORECASE):
        return possible_faculty
    return None


def remove_schedule_notation(description: str) -> str:
    """Remove numeric Calendar schedule markers while preserving other brackets.

    Examples removed include ``[3-0-0]``, ``[3-0-0; 3-0-0]``, and
    ``[1.5–1.5-1]``, and ``[3-1*-0]``. A non-schedule reference like
    ``[CPSC349]`` remains.
    """
    without_schedules = SCHEDULE_PATTERN.sub("", description)
    return " ".join(without_schedules.split())


def parse_credits(
    credits_text: str, course_label: str, metrics: ExtractionMetrics | None = None
) -> int | float | None:
    """Convert fixed numeric credits and preserve other formats through the caller.

    Whole and decimal values (for example, ``3`` and ``1.5``) are safe JSON
    numbers. A range such as ``3-6`` cannot be truthfully represented by one
    number, so this function returns ``None`` and the record stores it in
    ``credits_raw`` instead.
    """
    cleaned_credits = credits_text.strip()
    if cleaned_credits.isdigit():
        return int(cleaned_credits)
    if re.fullmatch(r"\d+\.\d+", cleaned_credits):
        return float(cleaned_credits)

    logging.warning(
        "%s has an unusual credit format %r; storing credits as null.",
        course_label,
        cleaned_credits,
    )
    if metrics is not None:
        metrics.unusual_credit_formats += 1
        metrics.missing_or_unrepresentable_credits += 1
    return None


def derive_level(course_number: int) -> int | None:
    """Map a 100--499 course number to its undergraduate level band."""
    level = (course_number // 100) * 100
    return level if level in UNDERGRADUATE_LEVELS else None


def parse_course_article(
    article: Tag,
    faculty_school: str | None,
    source_url: str,
    metrics: ExtractionMetrics | None = None,
) -> dict[str, Any] | None:
    """Turn one course teaser article into a normalized record, or warn and skip it."""
    header = article.select_one("h3")
    if header is None:
        logging.warning("Skipping a course entry with no heading on %s", source_url)
        if metrics is not None:
            metrics.malformed_course_headings += 1
            metrics.malformed_heading_examples.append("<missing h3 heading>")
        return None

    header_text = header.get_text(" ", strip=True)
    match = COURSE_HEADER_PATTERN.match(header_text)
    if not match:
        logging.warning("Could not parse course heading %r on %s", header_text, source_url)
        if metrics is not None:
            metrics.malformed_course_headings += 1
            metrics.malformed_heading_examples.append(header_text)
            if re.match(r"^[A-Z0-9]+_V\s+\d+\s*\([^)]+\)\s*$", header_text):
                metrics.missing_titles += 1
        return None

    subject = match.group("subject")
    course_number = int(match.group("number"))
    title = match.group("title").strip()
    course_code = f"{subject} {course_number}"
    level = derive_level(course_number)
    if level is None:
        logging.warning(
            "Skipping %s because it is outside the undergraduate 100--499 range.",
            course_code,
        )
        if metrics is not None:
            metrics.excluded_outside_undergraduate_range += 1
        return None

    description_element = article.select_one("p")
    description = ""
    if description_element is not None:
        description = remove_schedule_notation(description_element.get_text(" ", strip=True))

    record: dict[str, Any] = {
        "course_code": course_code,
        "subject": subject,
        "course_number": course_number,
        "title": title,
        "description": description,
        "credits": parse_credits(match.group("credits"), course_code, metrics),
        # Preserve a variable/range credit value rather than inventing one.
        # ``credits_raw`` is null for ordinary fixed numeric credits.
        "credits_raw": None,
        "level": level,
        "faculty_school": faculty_school,
        "source_url": source_url,
    }
    if record["credits"] is None:
        record["credits_raw"] = match.group("credits").strip()

    return record if validate_course_record(record) else None


def validate_course_record(record: dict[str, Any]) -> bool:
    """Check the essential fields before a record is written to JSON."""
    required_strings = ("course_code", "subject", "title", "source_url")
    missing = [field for field in required_strings if not str(record.get(field, "")).strip()]
    if missing:
        logging.warning("Skipping record missing %s: %r", ", ".join(missing), record)
        return False

    course_number = record.get("course_number")
    if not isinstance(course_number, int) or course_number < 1:
        logging.warning("Skipping record with an invalid course number: %r", record)
        return False

    expected_level = derive_level(course_number)
    if expected_level is None or record.get("level") != expected_level:
        logging.warning("Skipping record with an invalid undergraduate level: %r", record)
        return False
    return True


def extract_courses_from_subject_html(
    subject_html: str,
    source_url: str,
    metrics: ExtractionMetrics | None = None,
) -> list[dict[str, Any]]:
    """Extract unique undergraduate records from one downloaded subject page."""
    soup = BeautifulSoup(subject_html, "html.parser")
    faculty_school = extract_faculty_school(soup)
    records: list[dict[str, Any]] = []
    seen_course_codes: set[str] = set()

    for article in soup.select("article.node--type-course"):
        # An unexpected individual entry should not discard the rest of a subject.
        try:
            record = parse_course_article(article, faculty_school, source_url, metrics)
        except (AttributeError, TypeError, ValueError) as error:
            logging.warning("Skipping a malformed course entry on %s: %s", source_url, error)
            if metrics is not None:
                metrics.other_parsing_errors += 1
            continue
        if record is None:
            continue

        course_code = record["course_code"]
        if course_code in seen_course_codes:
            logging.warning("Skipping duplicate course code %s on %s", course_code, source_url)
            if metrics is not None:
                metrics.duplicate_course_codes += 1
            continue
        seen_course_codes.add(course_code)
        records.append(record)

        if metrics is not None and not record["description"]:
            metrics.missing_descriptions += 1
        if metrics is not None and record["faculty_school"] is None:
            metrics.null_faculty_school_records += 1

    return records


def write_courses_json(records: list[dict[str, Any]], output_path: Path) -> None:
    """Write readable UTF-8 JSON, creating the output directory if necessary."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(records, output_file, ensure_ascii=False, indent=2)
        output_file.write("\n")


def scrape_one_subject(subject_url: str, output_path: Path) -> list[dict[str, Any]]:
    """Fetch and save one subject page.  This is the only scraping action in the CLI."""
    session = make_http_session()
    subject_html, final_url = fetch_html(session, subject_url)
    records = extract_courses_from_subject_html(subject_html, final_url)
    write_courses_json(records, output_path)
    logging.info("Wrote %d course records to %s", len(records), output_path)
    return records


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract one UBC Calendar subject page to JSON.")
    parser.add_argument(
        "--subject-url",
        default=ADHE_SUBJECT_URL,
        help="One UBC Calendar subject URL (defaults to the ADHE_V development example).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/adhe_courses.json"),
        help="UTF-8 JSON output path (default: data/adhe_courses.json).",
    )
    parser.add_argument("--verbose", action="store_true", help="Show debug logging.")
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    configure_logging(args.verbose)
    try:
        scrape_one_subject(args.subject_url, args.output)
    except requests.RequestException as error:
        logging.error("Could not fetch %s: %s", args.subject_url, error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
