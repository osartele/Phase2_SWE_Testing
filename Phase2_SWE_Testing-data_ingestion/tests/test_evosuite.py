from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from phase2.evosuite import focal_class_fqcn, resolve_evosuite_jar_path


class EvoSuiteUtilsTests(unittest.TestCase):
    def test_resolve_evosuite_jar_path_from_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            jar = workdir / "tools" / "evosuite.jar"
            jar.parent.mkdir(parents=True, exist_ok=True)
            jar.write_bytes(b"fake-jar")

            result = resolve_evosuite_jar_path(
                workdir=workdir,
                jar_path=jar,
                config={},
            )
            self.assertEqual(Path(result["jar_path"]), jar.resolve())
            self.assertEqual(result["source"], "argument")
            self.assertFalse(result["downloaded"])

    def test_resolve_evosuite_jar_path_requires_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                resolve_evosuite_jar_path(workdir=workdir, config={})

    def test_focal_class_fqcn_reads_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            src = repo / "src" / "main" / "java" / "org" / "example"
            src.mkdir(parents=True, exist_ok=True)
            java_file = src / "Thing.java"
            java_file.write_text(
                "package org.example;\npublic class Thing {}\n",
                encoding="utf-8",
            )
            fqcn = focal_class_fqcn(repo_path=repo, focal_file_path="src/main/java/org/example/Thing.java")
            self.assertEqual(fqcn, "org.example.Thing")


if __name__ == "__main__":
    unittest.main()
