from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phase2.case_runner import _repo_is_clean, _resolve_case_input, _resolve_path, _run_git
from phase2.cases import Case
from phase2.repo import REPO_INDEX_REL, resolve_repo_path
from phase2.test_runner import (
    _collect_junit_xml_paths,
    _dedupe_failures,
    _parse_console_failures,
    _parse_junit_xml_reports,
    detect_java_build_tool,
)


DEFAULT_REPOS_ROOT_REL = Path("repos")
DEFAULT_HEALING_ROOT_REL = Path("artifacts/healing")
BASELINE_LABEL = "ReAssert-style baseline"

ASSERT_ARRAY_EQUALS_RE = re.compile(r"assertArrayEquals\s*\(")
ASSERT_ITERABLE_EQUALS_RE = re.compile(r"assertIterableEquals\s*\(")
ASSERT_THROWS_RE = re.compile(r"assertThrows\s*\(")
ASSERT_EQUALS_RE = re.compile(r"assertEquals\s*\(")
ASSERT_TRUE_RE = re.compile(r"assertTrue\s*\(")
ASSERT_NOT_NULL_RE = re.compile(r"assertNotNull\s*\(")

METHOD_DECL_TEMPLATE = r"\b[A-Za-z_][\w<>\[\]\s,]*\b{method}\s*\("


@dataclass(slots=True)
class AssertionFailure:
    assertion_type: str
    file_path: Path
    line: int
    expected: str | None
    actual: str | None
    message: str | None
    source: str


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _artifact_iter_dir(workdir: Path, case_id: str, iteration: int, artifacts_root: Path) -> Path:
    root = _resolve_path(workdir, artifacts_root)
    path = root / case_id / f"iter_{iteration}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _split_top_level_args(arg_text: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    escape = False
    for ch in arg_text:
        if quote is not None:
            current.append(ch)
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == quote:
                quote = None
            continue

        if ch in {"'", '"'}:
            quote = ch
            current.append(ch)
            continue
        if ch in "([{":
            depth += 1
            current.append(ch)
            continue
        if ch in ")]}":
            depth = max(depth - 1, 0)
            current.append(ch)
            continue
        if ch == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        args.append("".join(current).strip())
    return args


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_string_literal(value: str) -> bool:
    return bool(re.fullmatch(r'"(?:\\.|[^"\\])*"', value.strip()))


def _looks_bracket_collection(value: str) -> bool:
    text = value.strip()
    return text.startswith("[") and text.endswith("]")


def _java_literal_from_observed(value: str) -> str:
    raw = value.strip()
    if raw in {"null", "true", "false"}:
        return raw
    if re.fullmatch(r"-?\d+(?:\.\d+)?(?:[fFdDlL])?", raw):
        return raw
    if re.fullmatch(r"'(?:\\.|[^\\'])'", raw):
        return raw
    escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _java_collection_expr_from_observed(value: str) -> str:
    raw = value.strip()
    if not _looks_bracket_collection(raw):
        return _java_literal_from_observed(raw)
    inner = raw[1:-1].strip()
    if not inner:
        return "java.util.Collections.emptyList()"
    elements = _split_top_level_args(inner)
    literals = [_java_literal_from_observed(element) for element in elements]
    return f"java.util.Arrays.asList({', '.join(literals)})"


def _infer_array_type(values: list[str]) -> str:
    tokens = [value.strip() for value in values if value.strip()]
    if not tokens:
        return "Object"
    if all(token in {"true", "false"} for token in tokens):
        return "boolean"
    if all(re.fullmatch(r"-?\d+", token) for token in tokens):
        return "int"
    if all(re.fullmatch(r"-?\d+(?:\.\d+)?(?:[fFdD])?", token) for token in tokens):
        return "double"
    if all(re.fullmatch(r"'(?:\\.|[^\\'])'", token) or len(token) == 1 for token in tokens):
        return "char"
    return "String"


def _array_element_literal(raw: str, type_name: str) -> str:
    value = raw.strip()
    if type_name == "char":
        if re.fullmatch(r"'(?:\\.|[^\\'])'", value):
            return value
        if len(value) == 1:
            return f"'{value}'"
    if type_name in {"int", "double", "boolean"}:
        return value
    return _java_literal_from_observed(value)


def _java_array_expr_from_observed(value: str) -> str:
    raw = value.strip()
    if not _looks_bracket_collection(raw):
        return _java_literal_from_observed(raw)
    inner = raw[1:-1].strip()
    if not inner:
        return "new Object[]{}"
    elements = _split_top_level_args(inner)
    array_type = _infer_array_type(elements)
    literal_values = [_array_element_literal(element, array_type) for element in elements]
    return f"new {array_type}[]{{{', '.join(literal_values)}}}"


def _extract_expected_actual(text: str) -> tuple[str | None, str | None]:
    match = re.search(r"expected:<(?P<exp>.*?)>\s*but was:<(?P<act>.*?)>", text, flags=re.DOTALL)
    if not match:
        return None, None
    return match.group("exp"), match.group("act")


def _normalize_exception_name(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().strip("<>").strip()
    text = re.sub(r"^class\s+", "", text)
    text = re.sub(r"\.class$", "", text)
    match = re.search(r"([A-Za-z_][\w.$]*(?:Exception|Error))", text)
    if match:
        return match.group(1)
    match = re.search(r"([A-Za-z_][\w.$]*)", text)
    if match:
        return match.group(1)
    return None


def _extract_expected_actual_exception(text: str) -> tuple[str | None, str | None]:
    patterns = [
        r"expected:\s*<(?P<exp>[A-Za-z_][\w.$]*)>\s*but was:\s*<(?P<act>[A-Za-z_][\w.$]*)>",
        r"Expected exception:\s*(?P<exp>[A-Za-z_][\w.$]*)",
        r"Unexpected exception type thrown.*expected:\s*<(?P<exp>[A-Za-z_][\w.$]*)>\s*but was:\s*<(?P<act>[A-Za-z_][\w.$]*)>",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.DOTALL)
        if not match:
            continue
        expected = _normalize_exception_name(match.groupdict().get("exp"))
        actual = _normalize_exception_name(match.groupdict().get("act"))
        return expected, actual
    return None, None


def _parse_invocation_args(line: str, method_name: str) -> tuple[int, int, list[str]] | None:
    start = line.find(method_name)
    if start < 0:
        return None
    open_paren = line.find("(", start)
    if open_paren < 0:
        return None
    depth = 0
    close_paren = -1
    for idx in range(open_paren, len(line)):
        ch = line[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close_paren = idx
                break
    if close_paren < 0:
        return None
    arg_text = line[open_paren + 1 : close_paren]
    args = _split_top_level_args(arg_text)
    return open_paren, close_paren, args


def _replace_invocation_args(line: str, open_paren: int, close_paren: int, args: list[str]) -> str:
    rebuilt = ", ".join(args)
    return line[: open_paren + 1] + rebuilt + line[close_paren:]


def _expected_arg_index_equals(args: list[str]) -> int | None:
    if len(args) < 2:
        return None
    if len(args) == 2:
        return 0
    if len(args) >= 4:
        return 1 if _is_string_literal(args[0]) else 0
    if len(args) == 3 and _is_string_literal(args[0]):
        return 1
    return 0


def _expected_arg_index_array(args: list[str]) -> int | None:
    if len(args) < 2:
        return None
    if len(args) == 2:
        return 0
    if len(args) >= 4 and _is_string_literal(args[0]):
        return 1
    if len(args) == 3 and _is_string_literal(args[0]):
        return 1
    return 0


def _build_expected_expr(assertion_type: str, actual: str, existing_expected: str) -> str:
    if assertion_type == "assertArrayEquals":
        return _java_array_expr_from_observed(actual)
    if assertion_type == "assertIterableEquals":
        return _java_collection_expr_from_observed(actual)
    if _looks_bracket_collection(actual):
        existing = existing_expected.strip()
        if any(token in existing for token in ("Arrays.asList", "List.of", "Collections.", "new ArrayList")):
            return _java_collection_expr_from_observed(actual)
    return _java_literal_from_observed(actual)


def repair_assert_equals_line(line: str, expected: str | None, actual: str | None) -> tuple[str, bool, str]:
    if "assertEquals" not in line or actual is None:
        return line, False, "not_assert_equals_or_missing_actual"
    parsed = _parse_invocation_args(line, "assertEquals")
    if parsed is None:
        return line, False, "assert_equals_parse_failed"
    open_paren, close_paren, args = parsed
    index = _expected_arg_index_equals(args)
    if index is None:
        return line, False, "assert_equals_insufficient_args"

    new_expected = _build_expected_expr("assertEquals", actual, args[index])
    if args[index] == new_expected:
        return line, False, "assert_equals_expected_already_matches_actual"
    args[index] = new_expected
    new_line = _replace_invocation_args(line, open_paren, close_paren, args)
    return new_line, True, "assert_equals_expected_updated"


def repair_assert_array_equals_line(line: str, actual: str | None) -> tuple[str, bool, str]:
    if "assertArrayEquals" not in line or actual is None:
        return line, False, "not_assert_array_equals_or_missing_actual"
    parsed = _parse_invocation_args(line, "assertArrayEquals")
    if parsed is None:
        return line, False, "assert_array_equals_parse_failed"
    open_paren, close_paren, args = parsed
    index = _expected_arg_index_array(args)
    if index is None:
        return line, False, "assert_array_equals_insufficient_args"

    new_expected = _build_expected_expr("assertArrayEquals", actual, args[index])
    if args[index] == new_expected:
        return line, False, "assert_array_equals_expected_already_matches_actual"
    args[index] = new_expected
    new_line = _replace_invocation_args(line, open_paren, close_paren, args)
    return new_line, True, "assert_array_equals_expected_updated"


def repair_assert_iterable_equals_line(line: str, actual: str | None) -> tuple[str, bool, str]:
    if "assertIterableEquals" not in line or actual is None:
        return line, False, "not_assert_iterable_equals_or_missing_actual"
    parsed = _parse_invocation_args(line, "assertIterableEquals")
    if parsed is None:
        return line, False, "assert_iterable_equals_parse_failed"
    open_paren, close_paren, args = parsed
    if len(args) < 2:
        return line, False, "assert_iterable_equals_insufficient_args"

    new_expected = _build_expected_expr("assertIterableEquals", actual, args[0])
    if args[0] == new_expected:
        return line, False, "assert_iterable_equals_expected_already_matches_actual"
    args[0] = new_expected
    new_line = _replace_invocation_args(line, open_paren, close_paren, args)
    return new_line, True, "assert_iterable_equals_expected_updated"


def repair_assert_true_line(line: str) -> tuple[str, bool, str]:
    if "assertTrue" not in line:
        return line, False, "not_assert_true"
    new_line = ASSERT_TRUE_RE.sub("assertFalse(", line, count=1)
    if new_line == line:
        return line, False, "assert_true_replace_failed"
    return new_line, True, "assert_true_to_assert_false"


def repair_assert_not_null_line(line: str) -> tuple[str, bool, str]:
    if "assertNotNull" not in line:
        return line, False, "not_assert_not_null"
    new_line = ASSERT_NOT_NULL_RE.sub("assertNull(", line, count=1)
    if new_line == line:
        return line, False, "assert_not_null_replace_failed"
    return new_line, True, "assert_not_null_to_assert_null"


def repair_assert_throws_line(
    line: str,
    expected_exception: str | None,
    actual_exception: str | None,
) -> tuple[str, bool, str]:
    if "assertThrows" not in line:
        return line, False, "not_assert_throws"
    target_exception = _normalize_exception_name(actual_exception)
    if target_exception is None:
        return line, False, "assert_throws_missing_actual_exception"

    parsed = _parse_invocation_args(line, "assertThrows")
    if parsed is None:
        return line, False, "assert_throws_parse_failed"
    open_paren, close_paren, args = parsed
    if len(args) < 2:
        return line, False, "assert_throws_insufficient_args"

    class_index = 0
    if len(args) > 1 and ".class" in args[1] and ".class" not in args[0]:
        class_index = 1
    elif ".class" in args[0]:
        class_index = 0
    elif len(args) > 1:
        class_index = 1

    replacement = f"{target_exception}.class"
    if args[class_index].strip() == replacement:
        return line, False, "assert_throws_expected_already_matches_actual"
    args[class_index] = replacement
    new_line = _replace_invocation_args(line, open_paren, close_paren, args)
    return new_line, True, "assert_throws_expected_exception_updated"


def _detect_assertion_type(line: str) -> str | None:
    if "assertArrayEquals" in line:
        return "assertArrayEquals"
    if "assertIterableEquals" in line:
        return "assertIterableEquals"
    if "assertThrows" in line:
        return "assertThrows"
    if "assertEquals" in line:
        return "assertEquals"
    if "assertTrue" in line:
        return "assertTrue"
    if "assertNotNull" in line:
        return "assertNotNull"
    return None


def _resolve_failure_file(repo_path: Path, case: Case, file_name: str | None) -> Path | None:
    if not file_name:
        candidate = repo_path / case.test_file_path
        return candidate if candidate.exists() else None
    case_test = repo_path / case.test_file_path
    if case_test.name == file_name and case_test.exists():
        return case_test
    matches = list(repo_path.rglob(file_name))
    if matches:
        return matches[0]
    return None


def _extract_stack_file_line(stack: str | None) -> tuple[str | None, int | None]:
    if not stack:
        return None, None
    match = re.search(r"\(([^():]+\.java):(\d+)\)", stack)
    if not match:
        return None, None
    return match.group(1), int(match.group(2))


def _extract_assertion_failures(
    failing_tests: list[dict[str, Any]],
    combined_output: str,
    repo_path: Path,
    case: Case,
    restrict_to_case_test_file: bool = False,
) -> list[AssertionFailure]:
    failures: list[AssertionFailure] = []
    case_test_path = (repo_path / case.test_file_path).resolve()

    for item in failing_tests:
        message = str(item.get("message") or "")
        stack = str(item.get("stack_trace") or "")
        file_name, line = _extract_stack_file_line(stack)
        if line is None:
            continue
        file_path = _resolve_failure_file(repo_path, case, file_name)
        if file_path is None or not file_path.exists():
            continue
        if restrict_to_case_test_file and file_path.resolve() != case_test_path:
            continue
        src_lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line < 1 or line > len(src_lines):
            continue
        src_line = src_lines[line - 1]
        assertion_type = _detect_assertion_type(src_line)
        if assertion_type is None:
            continue

        expected, actual = _extract_expected_actual(f"{message}\n{stack}")
        exp_exc, act_exc = _extract_expected_actual_exception(f"{message}\n{stack}")
        if assertion_type == "assertThrows":
            expected = exp_exc
            actual = act_exc

        failures.append(
            AssertionFailure(
                assertion_type=assertion_type,
                file_path=file_path,
                line=line,
                expected=expected,
                actual=actual,
                message=message or None,
                source="junit_or_console_failure_item",
            )
        )

    lines = combined_output.splitlines()
    for idx, line in enumerate(lines):
        expected, actual = _extract_expected_actual(line)
        exp_exc, act_exc = _extract_expected_actual_exception(line)
        if expected is None and actual is None and exp_exc is None and act_exc is None:
            continue
        stack_line = None
        for j in range(idx + 1, min(idx + 7, len(lines))):
            if ".java:" in lines[j]:
                stack_line = lines[j]
                break
        file_name, line_no = _extract_stack_file_line(stack_line or "")
        if line_no is None:
            continue
        file_path = _resolve_failure_file(repo_path, case, file_name)
        if file_path is None or not file_path.exists():
            continue
        if restrict_to_case_test_file and file_path.resolve() != case_test_path:
            continue
        src_lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line_no < 1 or line_no > len(src_lines):
            continue
        assertion_type = _detect_assertion_type(src_lines[line_no - 1])
        if assertion_type is None:
            continue
        if assertion_type == "assertThrows":
            expected = exp_exc
            actual = act_exc
        failures.append(
            AssertionFailure(
                assertion_type=assertion_type,
                file_path=file_path,
                line=line_no,
                expected=expected,
                actual=actual,
                message=line.strip() or None,
                source="console_scan",
            )
        )

    deduped: dict[tuple[str, int, str], AssertionFailure] = {}
    for failure in failures:
        key = (str(failure.file_path), failure.line, failure.assertion_type)
        if key not in deduped:
            deduped[key] = failure
    return list(deduped.values())


def _normalize_test_method_name(method_name: str) -> str:
    cleaned = method_name.strip()
    if not cleaned:
        return ""
    cleaned = cleaned.split("[", 1)[0]
    cleaned = cleaned.split("(", 1)[0]
    return cleaned.strip()


def _filter_failing_tests_for_mapper_scope(
    failing_tests: list[dict[str, Any]],
    case: Case,
) -> list[dict[str, Any]]:
    if not failing_tests:
        return []
    target_method = _normalize_test_method_name(case.mapped_test_method)
    target_class = Path(case.test_file_path).stem
    scoped: list[dict[str, Any]] = []
    for item in failing_tests:
        test_method = _normalize_test_method_name(str(item.get("test_method") or ""))
        test_class = str(item.get("test_class") or "").strip()
        test_class_simple = test_class.split(".")[-1] if test_class else ""

        if target_method and test_method and test_method != target_method:
            continue
        if target_class and test_class_simple and test_class_simple != target_class:
            continue
        scoped.append(item)
    return scoped


def _apply_repairs(
    assertion_failures: list[AssertionFailure],
    file_snapshots: dict[Path, str] | None = None,
) -> tuple[list[dict[str, Any]], list[Path]]:
    by_file: dict[Path, list[AssertionFailure]] = {}
    for failure in assertion_failures:
        by_file.setdefault(failure.file_path, []).append(failure)

    changes: list[dict[str, Any]] = []
    touched_files: list[Path] = []

    for file_path, failures in by_file.items():
        original_text = file_path.read_text(encoding="utf-8", errors="replace")
        if file_snapshots is not None and file_path not in file_snapshots:
            file_snapshots[file_path] = original_text
        lines = original_text.splitlines()
        modified = False

        for failure in sorted(failures, key=lambda f: f.line, reverse=True):
            if failure.line < 1 or failure.line > len(lines):
                continue
            original = lines[failure.line - 1]
            if failure.assertion_type == "assertEquals":
                updated, changed, reason = repair_assert_equals_line(
                    original,
                    expected=failure.expected,
                    actual=failure.actual,
                )
            elif failure.assertion_type == "assertArrayEquals":
                updated, changed, reason = repair_assert_array_equals_line(
                    original,
                    actual=failure.actual,
                )
            elif failure.assertion_type == "assertIterableEquals":
                updated, changed, reason = repair_assert_iterable_equals_line(
                    original,
                    actual=failure.actual,
                )
            elif failure.assertion_type == "assertThrows":
                updated, changed, reason = repair_assert_throws_line(
                    original,
                    expected_exception=failure.expected,
                    actual_exception=failure.actual,
                )
            elif failure.assertion_type == "assertTrue":
                updated, changed, reason = repair_assert_true_line(original)
            elif failure.assertion_type == "assertNotNull":
                updated, changed, reason = repair_assert_not_null_line(original)
            else:
                continue

            changes.append(
                {
                    "file_path": str(file_path),
                    "line": failure.line,
                    "assertion_type": failure.assertion_type,
                    "reason": reason,
                    "changed": changed,
                    "before": original,
                    "after": updated if changed else original,
                    "expected": failure.expected,
                    "actual": failure.actual,
                    "source": failure.source,
                }
            )
            if changed:
                lines[failure.line - 1] = updated
                modified = True

        if modified:
            file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            touched_files.append(file_path)

    return changes, touched_files


def _update_test_annotation(line: str, exception_name: str) -> tuple[str, bool]:
    replacement = f"{exception_name}.class"
    if "@Test" not in line:
        return line, False
    if "expected" in line:
        updated = re.sub(r"expected\s*=\s*[^,)]+", f"expected = {replacement}", line)
        return updated, updated != line
    stripped = line.strip()
    if stripped == "@Test":
        indent = line[: len(line) - len(line.lstrip())]
        return f"{indent}@Test(expected = {replacement})", True
    if stripped.startswith("@Test(") and stripped.endswith(")"):
        insert = line.rfind(")")
        if insert > -1:
            prefix = line[:insert].rstrip()
            suffix = line[insert:]
            joiner = ", " if not prefix.endswith("(") else ""
            updated = f"{prefix}{joiner}expected = {replacement}{suffix}"
            return updated, updated != line
    return line, False


def _find_method_line(lines: list[str], method_name: str) -> int | None:
    pattern = re.compile(METHOD_DECL_TEMPLATE.format(method=re.escape(method_name)))
    for idx, line in enumerate(lines):
        if pattern.search(line):
            return idx
    return None


def _repair_test_expected_annotation(
    file_path: Path,
    method_name: str,
    actual_exception: str,
    file_snapshots: dict[Path, str] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    original_text = file_path.read_text(encoding="utf-8", errors="replace")
    if file_snapshots is not None and file_path not in file_snapshots:
        file_snapshots[file_path] = original_text
    lines = original_text.splitlines()

    method_line = _find_method_line(lines, method_name)
    if method_line is None:
        return None, False
    start = max(0, method_line - 12)
    annotation_line_index: int | None = None
    for idx in range(method_line - 1, start - 1, -1):
        if "@Test" in lines[idx]:
            annotation_line_index = idx
            break
    if annotation_line_index is None:
        return None, False

    before = lines[annotation_line_index]
    after, changed = _update_test_annotation(before, actual_exception)
    if not changed:
        return None, False
    lines[annotation_line_index] = after
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return (
        {
            "file_path": str(file_path),
            "line": annotation_line_index + 1,
            "assertion_type": "expectedExceptionAnnotation",
            "reason": "updated_test_expected_exception_annotation",
            "changed": True,
            "before": before,
            "after": after,
            "expected": None,
            "actual": actual_exception,
            "source": "failure_message_expected_exception",
        },
        True,
    )


def _apply_exception_expectation_repairs(
    failing_tests: list[dict[str, Any]],
    repo_path: Path,
    case: Case,
    file_snapshots: dict[Path, str] | None = None,
) -> tuple[list[dict[str, Any]], list[Path]]:
    changes: list[dict[str, Any]] = []
    touched: list[Path] = []
    seen: set[tuple[str, str, str]] = set()

    for item in failing_tests:
        method_name = str(item.get("test_method") or "").strip()
        if not method_name:
            continue
        message = str(item.get("message") or "")
        stack = str(item.get("stack_trace") or "")
        _, actual_exception = _extract_expected_actual_exception(f"{message}\n{stack}")
        actual_exception = _normalize_exception_name(actual_exception)
        if actual_exception is None:
            continue

        file_name, _ = _extract_stack_file_line(stack)
        file_path = _resolve_failure_file(repo_path, case, file_name)
        if file_path is None or not file_path.exists():
            continue
        key = (str(file_path), method_name, actual_exception)
        if key in seen:
            continue
        seen.add(key)

        change, changed = _repair_test_expected_annotation(
            file_path=file_path,
            method_name=method_name,
            actual_exception=actual_exception,
            file_snapshots=file_snapshots,
        )
        if changed and change is not None:
            changes.append(change)
            touched.append(file_path)
    return changes, touched


def _restore_file_snapshots(file_snapshots: dict[Path, str]) -> None:
    for path, original_text in file_snapshots.items():
        if path.exists():
            path.write_text(original_text, encoding="utf-8")


def _run_case_tests_once(
    repo_path: Path,
    commands: list[str],
    timeout_seconds: int,
    modified_after_epoch: float,
) -> dict[str, Any]:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    command_results: list[dict[str, Any]] = []
    execution_error: str | None = None
    for cmd in commands:
        start = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(repo_path),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                shell=True,
            )
            duration = round(time.monotonic() - start, 3)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            stdout_chunks.append(f"$ {cmd}\n{stdout}")
            stderr_chunks.append(f"$ {cmd}\n{stderr}")
            command_results.append(
                {"command": cmd, "exit_code": completed.returncode, "duration_seconds": duration}
            )
            if completed.returncode != 0:
                break
        except subprocess.TimeoutExpired:
            duration = round(time.monotonic() - start, 3)
            execution_error = f"timeout_after_seconds: {timeout_seconds}"
            command_results.append(
                {"command": cmd, "exit_code": -1, "duration_seconds": duration, "error": execution_error}
            )
            break
        except Exception as exc:  # pragma: no cover
            duration = round(time.monotonic() - start, 3)
            execution_error = f"command_error_{type(exc).__name__}: {exc}"
            command_results.append(
                {"command": cmd, "exit_code": -1, "duration_seconds": duration, "error": execution_error}
            )
            break

    stdout_text = "\n".join(stdout_chunks)
    stderr_text = "\n".join(stderr_chunks)
    combined = f"{stdout_text}\n{stderr_text}".strip()

    detected = detect_java_build_tool(repo_path)
    report_paths = _collect_junit_xml_paths(
        repo_path=repo_path,
        tool=detected.get("tool"),
        modified_after_epoch=modified_after_epoch - 2.0,
    )
    xml_failures = _parse_junit_xml_reports(report_paths)
    console_failures = _parse_console_failures(combined)
    failing_tests = _dedupe_failures([*xml_failures, *console_failures])

    last_exit = command_results[-1]["exit_code"] if command_results else -1
    if execution_error is not None:
        status = "error"
        success = False
    else:
        success = last_exit == 0 and not failing_tests
        status = "pass" if success else "fail"

    return {
        "status": status,
        "success": success,
        "execution_error": execution_error,
        "commands": command_results,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "combined_output": combined,
        "report_paths_original": [str(path) for path in report_paths],
        "failing_tests": failing_tests,
        "failing_test_count": len(failing_tests),
    }


def _copy_reports_to_iter(report_paths: list[str], repo_path: Path, iter_dir: Path) -> list[str]:
    target_root = iter_dir / "junit"
    copied: list[str] = []
    for raw in report_paths:
        source = Path(raw)
        if not source.exists():
            continue
        try:
            rel = source.resolve().relative_to(repo_path.resolve())
            target = target_root / rel
        except Exception:
            target = target_root / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(target))
    return copied


def _resolve_reassert_command(
    config: dict[str, Any] | None,
    explicit_command: str | None,
    enable_direct_reassert: bool,
) -> tuple[str | None, str | None]:
    if not enable_direct_reassert:
        return None, None
    if explicit_command and explicit_command.strip():
        return explicit_command.strip(), "cli"

    env_command = (os.environ.get("REASSERT_COMMAND") or "").strip()
    if env_command:
        return env_command, "env"

    cfg = config or {}
    sync_cfg = cfg.get("sync", {}) if isinstance(cfg, dict) else {}
    heal_cfg = sync_cfg.get("healing", {}) if isinstance(sync_cfg, dict) else {}
    if isinstance(heal_cfg, dict):
        configured = heal_cfg.get("reassert_command")
        if isinstance(configured, str) and configured.strip():
            return configured.strip(), "config"

    if shutil.which("reassert"):
        return "reassert", "path"
    return None, None


def _repo_status_snapshot(repo_path: Path) -> dict[str, str | None]:
    output = _run_git(repo_path, ["status", "--porcelain"], check=False)
    snapshot: dict[str, str | None] = {}
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line or len(line) < 4:
            continue
        path_part = line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        rel = path_part.replace("\\", "/").strip()
        if not rel:
            continue
        abs_path = repo_path / rel
        if abs_path.exists() and abs_path.is_file():
            snapshot[rel] = _hash_file(abs_path)
        elif abs_path.exists():
            snapshot[rel] = str(abs_path.stat().st_mtime_ns)
        else:
            snapshot[rel] = None
    return snapshot


def _detect_touched_paths_since_snapshot(
    repo_path: Path,
    pre_snapshot: dict[str, str | None],
) -> list[Path]:
    post_snapshot = _repo_status_snapshot(repo_path)
    touched: list[Path] = []
    for rel, post_hash in post_snapshot.items():
        pre_hash = pre_snapshot.get(rel)
        if pre_hash != post_hash:
            touched.append(repo_path / rel)
    return touched


def _collect_untracked_paths(repo_path: Path) -> list[Path]:
    output = _run_git(repo_path, ["status", "--porcelain"], check=False)
    paths: list[Path] = []
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line.startswith("?? "):
            continue
        rel = line[3:].replace("\\", "/").strip()
        if not rel:
            continue
        paths.append(repo_path / rel)
    return paths


def _remove_paths(paths: list[Path], repo_path: Path) -> None:
    for path in sorted(paths, key=lambda item: len(str(item)), reverse=True):
        if path.exists() and path.is_file():
            path.unlink()
        elif path.exists() and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)

        parent = path.parent
        while True:
            if parent == repo_path or parent == parent.parent:
                break
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _run_reassert_tool(
    repo_path: Path,
    command: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo_path),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            shell=True,
        )
        return {
            "command": command,
            "exit_code": completed.returncode,
            "error": None,
            "duration_seconds": round(time.monotonic() - start, 3),
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "exit_code": -1,
            "error": f"timeout_after_seconds: {timeout_seconds}",
            "duration_seconds": round(time.monotonic() - start, 3),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    except Exception as exc:  # pragma: no cover
        return {
            "command": command,
            "exit_code": -1,
            "error": f"command_error_{type(exc).__name__}: {exc}",
            "duration_seconds": round(time.monotonic() - start, 3),
            "stdout": "",
            "stderr": "",
        }


def run_iterative_healing(
    workdir: Path,
    case_input: str,
    repos_root: Path = DEFAULT_REPOS_ROOT_REL,
    repo_index_path: Path = REPO_INDEX_REL,
    artifacts_root: Path = DEFAULT_HEALING_ROOT_REL,
    max_iterations: int = 5,
    max_minutes: int = 30,
    timeout_seconds: int = 1800,
    config: dict[str, Any] | None = None,
    reassert_command: str | None = None,
    enable_direct_reassert: bool = True,
    use_mapper_scope: bool = False,
) -> dict[str, Any]:
    case, case_source = _resolve_case_input(case_input, workdir=workdir)
    repo_path = resolve_repo_path(
        workdir=workdir,
        repo_id=case.repo_id,
        repos_root=repos_root,
        index_path=repo_index_path,
    )
    if not (repo_path / ".git").exists():
        raise FileNotFoundError(f"Repository not found for case repo_id={case.repo_id}: {repo_path}")
    if not _repo_is_clean(repo_path):
        raise RuntimeError(f"Repository is dirty; aborting healing run: {repo_path}")

    direct_reassert_cmd, direct_reassert_source = _resolve_reassert_command(
        config=config,
        explicit_command=reassert_command,
        enable_direct_reassert=enable_direct_reassert,
    )

    original_sha = _run_git(repo_path, ["rev-parse", "HEAD"])
    start_time = time.monotonic()
    started_at_utc = _utc_now_iso()
    max_seconds = max(1, max_minutes * 60)
    iteration_summaries: list[dict[str, Any]] = []
    stop_reason = "unknown"
    checked_out_modified: str | None = None
    file_snapshots: dict[Path, str] = {}
    created_untracked: set[Path] = set()

    try:
        _run_git(repo_path, ["checkout", "--detach", case.modified_commit])
        checked_out_modified = _run_git(repo_path, ["rev-parse", "HEAD"])
        if not checked_out_modified.startswith(case.modified_commit):
            raise RuntimeError(
                f"Resolved modified commit '{checked_out_modified}' does not match requested "
                f"'{case.modified_commit}'."
            )

        previous_fingerprint: tuple[str, ...] | None = None
        for iteration in range(1, max_iterations + 1):
            elapsed = time.monotonic() - start_time
            if elapsed > max_seconds:
                stop_reason = "time_budget_exhausted"
                break

            iter_dir = _artifact_iter_dir(workdir, case.case_id, iteration, artifacts_root)
            (iter_dir / "commit_sha.txt").write_text(
                _run_git(repo_path, ["rev-parse", "HEAD"]) + "\n",
                encoding="utf-8",
            )
            run_start = time.time()
            run_result = _run_case_tests_once(
                repo_path=repo_path,
                commands=case.build_commands,
                timeout_seconds=timeout_seconds,
                modified_after_epoch=run_start,
            )

            failing_tests_for_repair = (
                _filter_failing_tests_for_mapper_scope(run_result["failing_tests"], case)
                if use_mapper_scope
                else list(run_result["failing_tests"])
            )
            combined_output_for_repair = "" if use_mapper_scope else run_result["combined_output"]

            (iter_dir / "stdout.log").write_text(run_result["stdout"], encoding="utf-8")
            (iter_dir / "stderr.log").write_text(run_result["stderr"], encoding="utf-8")
            copied_reports = _copy_reports_to_iter(run_result["report_paths_original"], repo_path, iter_dir)

            assertion_failures = _extract_assertion_failures(
                failing_tests=failing_tests_for_repair,
                combined_output=combined_output_for_repair,
                repo_path=repo_path,
                case=case,
                restrict_to_case_test_file=use_mapper_scope,
            )
            repairs: list[dict[str, Any]] = []
            touched_files: list[Path] = []
            repair_strategy = BASELINE_LABEL
            reassert_result: dict[str, Any] | None = None
            direct_reassert_applied = False

            if run_result["status"] != "pass" and direct_reassert_cmd is not None:
                pre_snapshot = _repo_status_snapshot(repo_path)
                reassert_result = _run_reassert_tool(
                    repo_path=repo_path,
                    command=direct_reassert_cmd,
                    timeout_seconds=timeout_seconds,
                )
                (iter_dir / "reassert_stdout.log").write_text(
                    reassert_result["stdout"], encoding="utf-8"
                )
                (iter_dir / "reassert_stderr.log").write_text(
                    reassert_result["stderr"], encoding="utf-8"
                )
                touched_files = _detect_touched_paths_since_snapshot(
                    repo_path=repo_path,
                    pre_snapshot=pre_snapshot,
                )
                if touched_files:
                    direct_reassert_applied = True
                    repair_strategy = "direct_reassert_tool"
                    repairs.append(
                        {
                            "changed": True,
                            "reason": "patched_by_direct_reassert_tool",
                            "source": "direct_reassert_tool",
                            "command": reassert_result["command"],
                            "exit_code": reassert_result["exit_code"],
                            "error": reassert_result["error"],
                        }
                    )
                (iter_dir / "reassert_result.json").write_text(
                    json.dumps(reassert_result, indent=2, sort_keys=True),
                    encoding="utf-8",
                )

            if not direct_reassert_applied:
                local_repairs, local_touched = _apply_repairs(
                    assertion_failures,
                    file_snapshots=file_snapshots,
                )
                exception_repairs, exception_touched = _apply_exception_expectation_repairs(
                    failing_tests=failing_tests_for_repair,
                    repo_path=repo_path,
                    case=case,
                    file_snapshots=file_snapshots,
                )
                repairs = [*local_repairs, *exception_repairs]
                touched_set = {path.resolve() for path in [*local_touched, *exception_touched]}
                touched_files = sorted(touched_set, key=lambda item: str(item))

            for path in _collect_untracked_paths(repo_path):
                created_untracked.add(path)

            changed_files_rel = [
                str(path.resolve().relative_to(repo_path.resolve())).replace("\\", "/")
                for path in touched_files
                if path.exists()
            ]
            if changed_files_rel:
                diff = _run_git(repo_path, ["diff", "--no-color", "--", *changed_files_rel], check=False)
            else:
                diff = ""
            (iter_dir / "patch.diff").write_text(diff, encoding="utf-8")
            (iter_dir / "repairs.json").write_text(json.dumps(repairs, indent=2, sort_keys=True), encoding="utf-8")

            iter_summary = {
                "iteration": iteration,
                "status": run_result["status"],
                "success": run_result["success"],
                "execution_error": run_result["execution_error"],
                "failing_test_count": run_result["failing_test_count"],
                "failing_tests": run_result["failing_tests"],
                "scoped_failing_test_count": len(failing_tests_for_repair),
                "scoped_failing_tests": failing_tests_for_repair,
                "report_paths_original": run_result["report_paths_original"],
                "report_paths_copied": copied_reports,
                "assertion_failure_count": len(assertion_failures),
                "repair_strategy": repair_strategy,
                "direct_reassert_attempted": reassert_result is not None,
                "direct_reassert_applied": direct_reassert_applied,
                "repair_count": len([r for r in repairs if r.get("changed")]),
                "changed_files": changed_files_rel,
                "stdout_path": str(iter_dir / "stdout.log"),
                "stderr_path": str(iter_dir / "stderr.log"),
                "repairs_path": str(iter_dir / "repairs.json"),
                "patch_diff_path": str(iter_dir / "patch.diff"),
            }
            if reassert_result is not None:
                iter_summary["reassert_result_path"] = str(iter_dir / "reassert_result.json")
            (iter_dir / "iteration_result.json").write_text(
                json.dumps(iter_summary, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            iteration_summaries.append(iter_summary)

            if run_result["status"] == "pass":
                stop_reason = "success"
                break
            if len([r for r in repairs if r.get("changed")]) == 0:
                if len(assertion_failures) == 0:
                    stop_reason = "no_progress_no_assertion_failures"
                else:
                    stop_reason = "no_progress_no_patch"
                break

            fingerprint = tuple(
                sorted(
                    f"{item.get('test_class')}::{item.get('test_method')}::{item.get('message')}"
                    for item in failing_tests_for_repair
                )
            )
            if previous_fingerprint is not None and fingerprint == previous_fingerprint:
                stop_reason = "no_progress_same_failures"
                break
            previous_fingerprint = fingerprint
        else:
            stop_reason = "iteration_budget_exhausted"
    finally:
        _restore_file_snapshots(file_snapshots)
        _remove_paths(list(created_untracked), repo_path=repo_path)
        _run_git(repo_path, ["checkout", "--detach", original_sha], check=False)

    summary_root = _resolve_path(workdir, artifacts_root) / case.case_id
    summary_root.mkdir(parents=True, exist_ok=True)
    summary = {
        "case_id": case.case_id,
        "case_source": case_source,
        "repo_id": case.repo_id,
        "repo_path": str(repo_path),
        "baseline_label": BASELINE_LABEL,
        "direct_reassert_tool": {
            "available": direct_reassert_cmd is not None,
            "command": direct_reassert_cmd,
            "source": direct_reassert_source,
            "enabled": enable_direct_reassert,
        },
        "modified_commit": case.modified_commit,
        "checked_out_modified_sha": checked_out_modified,
        "restored_original_sha": original_sha,
        "max_iterations": max_iterations,
        "max_minutes": max_minutes,
        "timeout_seconds": timeout_seconds,
        "use_mapper_scope": use_mapper_scope,
        "stop_reason": stop_reason,
        "iterations": iteration_summaries,
        "started_at_utc": started_at_utc,
        "duration_seconds": round(time.monotonic() - start_time, 3),
        "status": "ok",
    }
    summary_path = summary_root / "healing_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary
