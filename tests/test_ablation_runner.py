from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from phase2.ablation_runner import _standardized_stop_reason, _variant_matrix


class AblationRunnerTests(unittest.TestCase):
    def test_variant_matrix_contains_four_controlled_variants(self) -> None:
        variants = _variant_matrix()
        ids = {item["variant_id"] for item in variants}
        self.assertEqual(
            ids,
            {
                "healing_mapper_on",
                "healing_mapper_off",
                "regen_mapper_on",
                "regen_mapper_off",
            },
        )

    def test_standardized_stop_reason(self) -> None:
        self.assertEqual(_standardized_stop_reason("healing", "success", True), "success")
        self.assertEqual(
            _standardized_stop_reason("healing", "no_progress_no_patch", False),
            "no_progress",
        )
        self.assertEqual(
            _standardized_stop_reason("regen", "time_budget_exhausted", False),
            "budget_exhausted",
        )


if __name__ == "__main__":
    unittest.main()

