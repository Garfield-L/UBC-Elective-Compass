"""Offline selection checks for the bounded new-subject grade benchmark."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from benchmark_full_candidate_grades import BENCHMARK_SUBJECTS, select_benchmark_records  # noqa: E402


class GradeBenchmarkSelectionTests(unittest.TestCase):
    def test_benchmark_selects_only_the_eight_approved_new_subjects(self) -> None:
        candidate_path = Path(__file__).resolve().parents[1] / "data" / "ubc_courses_full_tagged_candidate.json"
        selected = select_benchmark_records(json.loads(candidate_path.read_text(encoding="utf-8")))

        self.assertEqual({record["subject"] for record in selected}, set(BENCHMARK_SUBJECTS))
        self.assertEqual(len(selected), 472)
        self.assertTrue(all("interest_tags" in record for record in selected))


if __name__ == "__main__":
    unittest.main()
