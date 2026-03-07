from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from phase2.java_parser import parse_java_file, parse_java_source


JAVA_SNIPPET = textwrap.dedent(
    """\
    import org.junit.Test;

    public class Sample {
        @Test
        public void testAdd() {
            Calculator calc = new Calculator();
            calc.add(1, 2);
            this.helper(3, "z");
            assertEquals(3, calc.add(1, 2));
        }

        private int helper(int x, String label) {
            Utils.log(label);
            return x;
        }
    }
    """
)


class JavaParserTests(unittest.TestCase):
    def test_parse_java_source_extracts_methods_annotations_and_parameters(self) -> None:
        parsed = parse_java_source(JAVA_SNIPPET, path="<embedded>")
        methods = {method["name"]: method for method in parsed["methods"]}

        self.assertEqual(parsed["path"], "<embedded>")
        self.assertIn("testAdd", methods)
        self.assertIn("helper", methods)

        test_method = methods["testAdd"]
        annotation_names = [annotation["name"] for annotation in test_method["annotations"]]
        self.assertIn("Test", annotation_names)
        self.assertTrue(test_method["is_test"])
        self.assertIn("public void testAdd()", test_method["signature"])

        helper_method = methods["helper"]
        self.assertEqual([param["name"] for param in helper_method["params"]], ["x", "label"])
        self.assertEqual([param["type"] for param in helper_method["params"]], ["int", "String"])
        self.assertIn("private int helper(int x, String label)", helper_method["signature"])

    def test_parse_java_source_extracts_method_invocations(self) -> None:
        parsed = parse_java_source(JAVA_SNIPPET, path="<embedded>")
        methods = {method["name"]: method for method in parsed["methods"]}

        test_invocations = methods["testAdd"]["invocations"]
        helper_invocations = methods["helper"]["invocations"]

        self.assertTrue(
            any(
                inv["callee_name"] == "add" and inv["qualifier"] == "calc" and inv["arg_count"] == 2
                for inv in test_invocations
            )
        )
        self.assertTrue(
            any(
                inv["callee_name"] == "helper"
                and inv["qualifier"] == "this"
                and inv["arg_count"] == 2
                for inv in test_invocations
            )
        )
        self.assertTrue(
            any(
                inv["callee_name"] == "log" and inv["qualifier"] == "Utils" and inv["arg_count"] == 1
                for inv in helper_invocations
            )
        )
        for invocation in test_invocations + helper_invocations:
            self.assertGreaterEqual(invocation["line"], 1)
            self.assertGreaterEqual(invocation["column"], 1)

    def test_parse_java_file_reads_from_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            java_path = Path(tmp_dir) / "Sample.java"
            java_path.write_text(JAVA_SNIPPET, encoding="utf-8")

            parsed = parse_java_file(java_path)

        method_names = [method["name"] for method in parsed["methods"]]
        self.assertEqual(parsed["path"], str(java_path.resolve()))
        self.assertIn("testAdd", method_names)
        self.assertIn("helper", method_names)


if __name__ == "__main__":
    unittest.main()
