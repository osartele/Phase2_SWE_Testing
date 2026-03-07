from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from phase2.rq3_eval import _aggregate_jacoco_metrics, _aggregate_pit_metrics, _infer_test_root_rel


class RQ3EvalTests(unittest.TestCase):
    def test_infer_test_root_rel(self) -> None:
        rel = _infer_test_root_rel("module/src/test/java/org/example/FooTest.java")
        self.assertEqual(rel.as_posix(), "module/src/test/java")

        fallback = _infer_test_root_rel("tests/FooTest.java")
        self.assertEqual(fallback.as_posix(), "tests")

    def test_aggregate_jacoco_metrics_prefers_line_counter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "jacoco.xml"
            report.write_text(
                """\
<report name="demo">
  <counter type="INSTRUCTION" missed="10" covered="30"/>
  <counter type="LINE" missed="2" covered="8"/>
</report>
""",
                encoding="utf-8",
            )
            metrics = _aggregate_jacoco_metrics([report])
            self.assertEqual(metrics["counter_type"], "line")
            self.assertEqual(metrics["covered"], 8)
            self.assertEqual(metrics["total"], 10)
            self.assertEqual(metrics["coverage_pct"], 80.0)

    def test_aggregate_pit_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "mutations.xml"
            report.write_text(
                """\
<mutations>
  <mutation detected="true" status="KILLED"/>
  <mutation detected="false" status="SURVIVED"/>
  <mutation detected="true" status="TIMED_OUT"/>
</mutations>
""",
                encoding="utf-8",
            )
            metrics = _aggregate_pit_metrics([report])
            self.assertEqual(metrics["killed"], 2)
            self.assertEqual(metrics["total"], 3)
            self.assertAlmostEqual(float(metrics["mutation_score_pct"]), 66.6667, places=3)


if __name__ == "__main__":
    unittest.main()

