from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from phase2.healing import (
    _filter_failing_tests_for_mapper_scope,
    repair_assert_equals_line,
    repair_assert_not_null_line,
    repair_assert_true_line,
)
from phase2.cases import Case


class HealingRepairTests(unittest.TestCase):
    def test_repair_assert_equals_updates_expected_literal(self) -> None:
        line = "assertEquals(1, value);"
        updated, changed, reason = repair_assert_equals_line(
            line,
            expected="1",
            actual="2",
        )
        self.assertTrue(changed)
        self.assertEqual(reason, "assert_equals_expected_updated")
        self.assertEqual(updated, "assertEquals(2, value);")

    def test_repair_assert_equals_message_overload(self) -> None:
        line = 'assertEquals("msg", expected(), actual());'
        updated, changed, reason = repair_assert_equals_line(
            line,
            expected="foo",
            actual="bar",
        )
        self.assertTrue(changed)
        self.assertEqual(reason, "assert_equals_expected_updated")
        self.assertEqual(updated, 'assertEquals("msg", "bar", actual());')

    def test_repair_assert_true_flips_assertion(self) -> None:
        line = "assertTrue(flag);"
        updated, changed, reason = repair_assert_true_line(line)
        self.assertTrue(changed)
        self.assertEqual(reason, "assert_true_to_assert_false")
        self.assertEqual(updated, "assertFalse(flag);")

    def test_repair_assert_not_null_flips_assertion(self) -> None:
        line = "assertNotNull(result);"
        updated, changed, reason = repair_assert_not_null_line(line)
        self.assertTrue(changed)
        self.assertEqual(reason, "assert_not_null_to_assert_null")
        self.assertEqual(updated, "assertNull(result);")

    def test_mapper_scope_filters_by_test_method_and_class(self) -> None:
        case = Case(
            case_id="case_x",
            repo_id="repo_x",
            base_commit="a",
            modified_commit="b",
            focal_file_path="src/main/java/org/example/Foo.java",
            test_file_path="src/test/java/org/example/FooTest.java",
            mapped_focal_method="focalMethod",
            mapped_test_method="testTarget",
            build_commands=["mvn -B test"],
            metadata={},
        )
        failing_tests = [
            {"test_class": "org.example.FooTest", "test_method": "testTarget"},
            {"test_class": "org.example.FooTest", "test_method": "testOther"},
            {"test_class": "org.example.OtherTest", "test_method": "testTarget"},
        ]
        scoped = _filter_failing_tests_for_mapper_scope(failing_tests, case)
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]["test_method"], "testTarget")
        self.assertEqual(scoped[0]["test_class"], "org.example.FooTest")


if __name__ == "__main__":
    unittest.main()
