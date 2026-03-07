from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from phase2.cases import Case
from phase2.healing import (
    _apply_exception_expectation_repairs,
    repair_assert_array_equals_line,
    repair_assert_equals_line,
    repair_assert_iterable_equals_line,
    repair_assert_throws_line,
)


FIXTURES_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "healing"


class ReassertStyleRepairTests(unittest.TestCase):
    def test_repair_assert_equals_delta_overload(self) -> None:
        line = "assertEquals(1.0, actualValue, 0.001);"
        updated, changed, reason = repair_assert_equals_line(line, expected="1.0", actual="2.5")
        self.assertTrue(changed)
        self.assertEqual(reason, "assert_equals_expected_updated")
        self.assertEqual(updated, "assertEquals(2.5, actualValue, 0.001);")

    def test_repair_assert_throws_exception_type(self) -> None:
        line = "assertThrows(IllegalArgumentException.class, () -> service.run());"
        updated, changed, reason = repair_assert_throws_line(
            line,
            expected_exception="IllegalArgumentException",
            actual_exception="java.lang.NullPointerException",
        )
        self.assertTrue(changed)
        self.assertEqual(reason, "assert_throws_expected_exception_updated")
        self.assertEqual(updated, "assertThrows(java.lang.NullPointerException.class, () -> service.run());")

    def test_collection_assert_repairs(self) -> None:
        array_line = "assertArrayEquals(new int[] {1, 2, 5}, actual);"
        array_updated, array_changed, _ = repair_assert_array_equals_line(array_line, actual="[1, 3, 5]")
        self.assertTrue(array_changed)
        self.assertEqual(array_updated, "assertArrayEquals(new int[]{1, 3, 5}, actual);")

        iterable_line = 'assertIterableEquals(java.util.Arrays.asList("red", "green"), actual);'
        iterable_updated, iterable_changed, _ = repair_assert_iterable_equals_line(
            iterable_line,
            actual="[red, blue]",
        )
        self.assertTrue(iterable_changed)
        self.assertEqual(
            iterable_updated,
            'assertIterableEquals(java.util.Arrays.asList("red", "blue"), actual);',
        )

    def test_expected_exception_annotation_repair_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            fixture = FIXTURES_ROOT / "ExpectedExceptionTest.java"
            target_rel = Path("src/test/java/com/example/ExpectedExceptionTest.java")
            target_path = repo_path / target_rel
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fixture, target_path)

            case = Case(
                case_id="case_fixture",
                repo_id="repo_fixture",
                base_commit="a",
                modified_commit="b",
                focal_file_path="src/main/java/com/example/Focal.java",
                test_file_path=str(target_rel).replace("\\", "/"),
                mapped_focal_method="doWork",
                mapped_test_method="shouldThrowInvalidArg",
                build_commands=["mvn -B test"],
            )

            failing_tests = [
                {
                    "test_method": "shouldThrowInvalidArg",
                    "message": (
                        "Unexpected exception type thrown ==> expected: "
                        "<java.lang.IllegalArgumentException> but was: <java.lang.NullPointerException>"
                    ),
                    "stack_trace": "",
                }
            ]

            repairs, touched = _apply_exception_expectation_repairs(
                failing_tests=failing_tests,
                repo_path=repo_path,
                case=case,
                file_snapshots=None,
            )

            self.assertEqual(len(repairs), 1)
            self.assertEqual(len(touched), 1)
            text = target_path.read_text(encoding="utf-8")
            self.assertIn("@Test(expected = java.lang.NullPointerException.class)", text)


if __name__ == "__main__":
    unittest.main()
