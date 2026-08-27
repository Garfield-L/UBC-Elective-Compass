"""Build the bounded Version 1 UBC Course Finder catalog from a config file."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import requests

from scrape_courses import configure_logging
from validate_subjects import validate_subject_codes


EXPECTED_SUBJECT_COUNT = 60


def read_subject_codes(config_path: Path) -> tuple[str, ...]:
    """Read one subject code per line and reject an accidental config change."""
    subject_codes = tuple(
        line.strip()
        for line in config_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if len(subject_codes) != EXPECTED_SUBJECT_COUNT:
        raise ValueError(
            f"Expected exactly {EXPECTED_SUBJECT_COUNT} subject codes, found {len(subject_codes)}."
        )
    if len(set(subject_codes)) != len(subject_codes):
        raise ValueError("The Version 1 subject config contains duplicate subject codes.")
    if any(not code.isupper() or not code.isalnum() for code in subject_codes):
        raise ValueError("Subject codes must be uppercase letters/numbers with no spaces.")
    return subject_codes


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the bounded 60-subject Version 1 course catalog.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/subjects_v1.txt"),
        help="One subject code per line; must contain exactly 60 codes.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/ubc_courses_v1.json"),
        help="Combined undergraduate JSON output path.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("data/ubc_courses_v1_report.json"),
        help="Machine-readable validation report path.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show parser warnings as they occur.")
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    configure_logging(args.verbose)
    try:
        subject_codes = read_subject_codes(args.config)
        report = validate_subject_codes(subject_codes, args.report_path, args.output)
    except (OSError, ValueError) as error:
        logging.error("Could not read the Version 1 subject config: %s", error)
        return 1
    except requests.RequestException as error:
        logging.error("Could not fetch the UBC Calendar master subject page: %s", error)
        return 1

    summary = report["summary"]
    logging.info(
        "Fetched %d of %d requested subjects and wrote %d undergraduate records.",
        summary["subjects_successfully_fetched"],
        summary["subjects_requested"],
        summary["total_undergraduate_records"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
