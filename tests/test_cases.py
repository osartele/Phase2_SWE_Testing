from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from phase2.cases import (
    Case,
    ensure_case_schema_file,
    list_cases,
    load_case,
    load_case_by_id,
    save_case,
    validate_case_payload,
)


class CaseModelTests(unittest.TestCase):
    def _sample_payload(self) -> dict[str, object]:
        return {
            "repo_id": "13899",
            "base_commit": "aaaa1111",
            "modified_commit": "bbbb2222",
            "focal_file_path": "src/main/java/pkg/Focal.java",
            "test_file_path": "src/test/java/pkg/FocalTest.java",
            "mapped_focal_method": "safeRead",
            "mapped_test_method": "testSafeRead",
            "build_commands": ["mvn -B test"],
        }

    def test_save_and_load_case_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            payload = self._sample_payload()
            case_obj, path = save_case(payload, workdir=workdir)

            self.assertTrue(path.exists())
            self.assertEqual(path.name, "case.json")
            self.assertEqual(path.parent.name, case_obj.case_id)
            loaded = load_case(path)
            self.assertEqual(loaded.case_id, case_obj.case_id)
            self.assertEqual(loaded.repo_id, "13899")
            self.assertEqual(loaded.build_commands, ["mvn -B test"])

            loaded_by_id = load_case_by_id(case_obj.case_id, workdir=workdir)
            self.assertEqual(loaded_by_id.case_id, case_obj.case_id)

            cases = list_cases(workdir=workdir)
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0].case_id, case_obj.case_id)

    def test_deterministic_case_id_without_explicit_id(self) -> None:
        payload = self._sample_payload()
        normalized_1 = validate_case_payload(payload)
        normalized_2 = validate_case_payload(payload)
        self.assertEqual(normalized_1["case_id"], normalized_2["case_id"])

    def test_validate_rejects_bad_build_commands(self) -> None:
        payload = self._sample_payload()
        payload["build_commands"] = []  # type: ignore[assignment]
        with self.assertRaises(ValueError):
            validate_case_payload(payload)

    def test_ensure_schema_file_creates_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            schema_path = ensure_case_schema_file(workdir)
            self.assertTrue(schema_path.exists())
            text = schema_path.read_text(encoding="utf-8")
            self.assertIn('"title": "Phase2ChangeCase"', text)


if __name__ == "__main__":
    unittest.main()
