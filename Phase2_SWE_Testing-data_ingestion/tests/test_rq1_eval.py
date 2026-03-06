from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from phase2.rq1_eval import _ast_similarity, _line_similarity, _parse_patch_stats


DIFF_A = """\
diff --git a/src/test/java/XTest.java b/src/test/java/XTest.java
index 1111111..2222222 100644
--- a/src/test/java/XTest.java
+++ b/src/test/java/XTest.java
@@ -10 +10 @@ public class XTest {
-    assertEquals(1, value);
+    assertEquals(2, value);
}
"""

DIFF_B = """\
diff --git a/src/test/java/XTest.java b/src/test/java/XTest.java
index 1111111..3333333 100644
--- a/src/test/java/XTest.java
+++ b/src/test/java/XTest.java
@@ -10 +10 @@ public class XTest {
-    assertEquals(1, value);
+    assertEquals(3, value);
}
"""


class RQ1EvalTests(unittest.TestCase):
    def test_parse_patch_stats_counts_files_and_lines(self) -> None:
        stats = _parse_patch_stats(DIFF_A)
        self.assertEqual(stats["files_changed"], 1)
        self.assertEqual(stats["lines_added"], 1)
        self.assertEqual(stats["lines_removed"], 1)
        self.assertEqual(stats["changed_files"], ["src/test/java/XTest.java"])

    def test_line_similarity_returns_fraction(self) -> None:
        similarity = _line_similarity(DIFF_A, DIFF_B)
        self.assertIsNotNone(similarity)
        self.assertGreater(float(similarity), 0.0)
        self.assertLess(float(similarity), 1.0)

    def test_ast_similarity_optional(self) -> None:
        disabled = _ast_similarity(DIFF_A, DIFF_B, enabled=False)
        self.assertIsNone(disabled)
        enabled = _ast_similarity(DIFF_A, DIFF_B, enabled=True)
        self.assertIsNotNone(enabled)
        self.assertGreaterEqual(float(enabled), 0.0)
        self.assertLessEqual(float(enabled), 1.0)


if __name__ == "__main__":
    unittest.main()
