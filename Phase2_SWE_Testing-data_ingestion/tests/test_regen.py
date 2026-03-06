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

from phase2.regen import (
    GeneratedCandidate,
    _candidate_matches_mapper_scope,
    _collect_generated_candidates,
    _dedupe_candidates,
    _infer_test_source_root,
    _parse_package_name,
)


class RegenUtilsTests(unittest.TestCase):
    def test_parse_package_name(self) -> None:
        source = "package org.example.demo;\n\npublic class A {}"
        self.assertEqual(_parse_package_name(source), "org.example.demo")
        self.assertEqual(_parse_package_name("public class B {}"), "")

    def test_infer_test_source_root_from_case_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            test_root = repo / "module" / "src" / "test" / "java"
            test_root.mkdir(parents=True, exist_ok=True)
            case_path = "module/src/test/java/org/example/FooTest.java"
            inferred = _infer_test_source_root(repo, case_path)
            self.assertEqual(inferred, test_root.resolve())

    def test_collect_and_dedupe_generated_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp)
            package_dir = generated / "org" / "example"
            package_dir.mkdir(parents=True, exist_ok=True)

            content = textwrap.dedent(
                """\
                package org.example;

                public class Foo_ESTest {
                  public void test0() {}
                }
                """
            )
            scaffolding = textwrap.dedent(
                """\
                package org.example;

                public class Foo_ESTest_scaffolding {}
                """
            )
            (package_dir / "Foo_ESTest.java").write_text(content, encoding="utf-8")
            (package_dir / "Foo_ESTest_scaffolding.java").write_text(scaffolding, encoding="utf-8")
            (package_dir / "FooCopy_ESTest.java").write_text(
                content.replace("Foo_ESTest", "FooCopy_ESTest"),
                encoding="utf-8",
            )
            (package_dir / "FooCopy_ESTest_scaffolding.java").write_text(
                scaffolding.replace("Foo_ESTest_scaffolding", "FooCopy_ESTest_scaffolding"),
                encoding="utf-8",
            )

            candidates = _collect_generated_candidates(generated)
            self.assertEqual(len(candidates), 2)
            deduped = _dedupe_candidates(candidates)
            self.assertEqual(len(deduped), 2)
            self.assertEqual(deduped[0].package_name, "org.example")
            self.assertTrue(deduped[0].fqcn.endswith("_ESTest"))

    def test_candidate_mapper_scope_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_file = root / "Foo_ESTest.java"
            test_file.write_text(
                "public class Foo_ESTest { void t(){ obj.safeRead(); } }",
                encoding="utf-8",
            )
            candidate = GeneratedCandidate(
                package_name="",
                fqcn="Foo_ESTest",
                class_name="Foo_ESTest",
                test_file=test_file,
                scaffolding_file=None,
                content_hash="h",
            )
            self.assertTrue(_candidate_matches_mapper_scope(candidate, "safeRead"))
            self.assertFalse(_candidate_matches_mapper_scope(candidate, "otherMethod"))


if __name__ == "__main__":
    unittest.main()
