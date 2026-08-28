"""Offline validation for the Phase 7.5C candidate mapping review."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from review_full_candidate_mapping import (  # noqa: E402
    HUMAN_REVIEW_DECISIONS,
    validate_full_candidate_mapping,
)


class FullCandidateMappingReviewTests(unittest.TestCase):
    def test_human_review_decisions_cover_exactly_the_active_ambiguous_subjects(self) -> None:
        root = Path(__file__).resolve().parents[1]
        report = json.loads((root / "data" / "ubc_courses_full_base_candidate_report.json").read_text())

        self.assertEqual(
            set(HUMAN_REVIEW_DECISIONS),
            set(report["summary"]["ambiguous_subjects_with_at_least_one_undergraduate_course"]),
        )

    def test_full_candidate_mapping_has_exact_coverage_and_preserves_v1(self) -> None:
        root = Path(__file__).resolve().parents[1]
        records = json.loads((root / "data" / "ubc_courses_full_base_candidate.json").read_text())
        mapping = json.loads((root / "config" / "subject_interest_map_full_candidate.json").read_text())
        v1_mapping = json.loads((root / "config" / "subject_interest_map.json").read_text())

        validation = validate_full_candidate_mapping(mapping, records, v1_mapping)
        self.assertTrue(validation["is_valid"])
        self.assertEqual(validation["total_mapped_subjects"], 186)
        self.assertEqual(validation["unmapped_subjects"], [])
        self.assertEqual(validation["original_v1_mappings_changed"], [])

    def test_promoted_full_mapping_matches_the_approved_candidate_mapping(self) -> None:
        root = Path(__file__).resolve().parents[1]
        candidate_mapping = json.loads(
            (root / "config" / "subject_interest_map_full_candidate.json").read_text()
        )
        production_mapping = json.loads((root / "config" / "subject_interest_map_full.json").read_text())

        self.assertEqual(len(production_mapping), 186)
        self.assertEqual(production_mapping, candidate_mapping)


if __name__ == "__main__":
    unittest.main()
