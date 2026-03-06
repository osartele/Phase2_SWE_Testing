from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from phase2.benchmark import parse_approaches


class BenchmarkTests(unittest.TestCase):
    def test_parse_approaches_preserves_order_and_dedupes(self) -> None:
        parsed = parse_approaches("healing, regen, human, healing")
        self.assertEqual(parsed, ["healing", "regen", "human"])

    def test_parse_approaches_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            parse_approaches("healing,foo")


if __name__ == "__main__":
    unittest.main()

