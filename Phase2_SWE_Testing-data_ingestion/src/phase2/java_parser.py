from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser


METHOD_NODE_TYPES = {"method_declaration", "constructor_declaration"}
PARAMETER_NODE_TYPES = {
    "formal_parameter",
    "spread_parameter",
    "receiver_parameter",
    "inferred_parameter",
}
ANNOTATION_NODE_TYPES = {"annotation", "marker_annotation"}


@lru_cache(maxsize=1)
def _java_language() -> Language:
    lang = tsjava.language()
    if isinstance(lang, Language):
        return lang
    return Language(lang)


def _new_parser() -> Parser:
    parser = Parser()
    language = _java_language()
    if hasattr(parser, "language"):
        parser.language = language
    else:  # pragma: no cover - compatibility branch for old parser API
        parser.set_language(language)
    return parser


def _node_text(source_bytes: bytes, node: Node | None) -> str:
    if node is None:
        return ""
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _iter_descendants(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _first_identifier(node: Node) -> Node | None:
    for descendant in _iter_descendants(node):
        if descendant.type == "identifier":
            return descendant
    return None


def _method_name(node: Node, source_bytes: bytes) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        name_node = _first_identifier(node)
    return _node_text(source_bytes, name_node).strip()


def _method_signature(node: Node, source_bytes: bytes) -> str:
    body = node.child_by_field_name("body")
    signature_end = body.start_byte if body is not None else node.end_byte
    raw = source_bytes[node.start_byte:signature_end].decode("utf-8", errors="replace")
    return re.sub(r"\s+", " ", raw).strip()


def _extract_parameters(parameters_node: Node | None, source_bytes: bytes) -> list[dict[str, Any]]:
    if parameters_node is None:
        return []

    params: list[dict[str, Any]] = []
    for child in parameters_node.named_children:
        if child.type not in PARAMETER_NODE_TYPES:
            continue
        name_node = child.child_by_field_name("name")
        if name_node is None:
            name_node = _first_identifier(child)
        type_node = child.child_by_field_name("type")
        params.append(
            {
                "name": _node_text(source_bytes, name_node).strip(),
                "type": _node_text(source_bytes, type_node).strip() or None,
                "text": re.sub(r"\s+", " ", _node_text(source_bytes, child)).strip(),
            }
        )
    return params


def _extract_annotations(method_node: Node, source_bytes: bytes) -> list[dict[str, Any]]:
    annotations: list[dict[str, Any]] = []
    for child in method_node.children:
        if child.type != "modifiers":
            continue
        for candidate in _iter_descendants(child):
            if candidate.type not in ANNOTATION_NODE_TYPES:
                continue
            text = _node_text(source_bytes, candidate).strip()
            name_node = candidate.child_by_field_name("name")
            full_name = _node_text(source_bytes, name_node).strip() if name_node else ""
            if not full_name:
                full_name = text.lstrip("@").split("(", 1)[0].strip()
            short_name = full_name.split(".")[-1] if full_name else ""
            annotations.append(
                {
                    "name": short_name,
                    "full_name": full_name,
                    "text": text,
                    "is_test": short_name == "Test",
                }
            )
    return annotations


def _fallback_invocation_parts(invocation_text: str) -> tuple[str, str | None]:
    raw = invocation_text.strip()
    call_head = raw.split("(", 1)[0].strip()
    if "." not in call_head:
        return call_head, None
    qualifier, callee = call_head.rsplit(".", 1)
    return callee.strip(), qualifier.strip() or None


def _extract_invocations(method_node: Node, source_bytes: bytes) -> list[dict[str, Any]]:
    invocations: list[dict[str, Any]] = []
    for candidate in _iter_descendants(method_node):
        if candidate.type != "method_invocation":
            continue

        full_text = _node_text(source_bytes, candidate)
        name_node = candidate.child_by_field_name("name")
        object_node = candidate.child_by_field_name("object")
        arguments_node = candidate.child_by_field_name("arguments")

        callee_name = _node_text(source_bytes, name_node).strip()
        qualifier = _node_text(source_bytes, object_node).strip() or None
        if not callee_name:
            fallback_name, fallback_qualifier = _fallback_invocation_parts(full_text)
            callee_name = fallback_name
            qualifier = qualifier or fallback_qualifier

        arg_count = len(arguments_node.named_children) if arguments_node is not None else 0
        line, column = candidate.start_point
        invocations.append(
            {
                "callee_name": callee_name,
                "qualifier": qualifier,
                "arg_count": arg_count,
                "line": line + 1,
                "column": column + 1,
                "text": re.sub(r"\s+", " ", full_text).strip(),
            }
        )
    return invocations


def parse_java_source(source: str | bytes, path: str | Path = "<memory>") -> dict[str, Any]:
    source_bytes = source.encode("utf-8") if isinstance(source, str) else source
    parser = _new_parser()
    tree = parser.parse(source_bytes)

    methods: list[dict[str, Any]] = []
    for node in _iter_descendants(tree.root_node):
        if node.type not in METHOD_NODE_TYPES:
            continue
        name = _method_name(node, source_bytes)
        parameters = _extract_parameters(node.child_by_field_name("parameters"), source_bytes)
        annotations = _extract_annotations(node, source_bytes)
        line, column = node.start_point
        methods.append(
            {
                "kind": node.type,
                "name": name,
                "signature": _method_signature(node, source_bytes),
                "params": parameters,
                "annotations": annotations,
                "is_test": any(annotation["is_test"] for annotation in annotations),
                "invocations": _extract_invocations(node, source_bytes),
                "line": line + 1,
                "column": column + 1,
                "end_line": node.end_point[0] + 1,
                "end_column": node.end_point[1] + 1,
                "source_text": _node_text(source_bytes, node),
            }
        )

    methods.sort(key=lambda method: (method["line"], method["column"], method["name"]))
    return {"path": str(path), "methods": methods}


def parse_java_file(path: str | Path) -> dict[str, Any]:
    file_path = Path(path).expanduser().resolve()
    source_bytes = file_path.read_bytes()
    return parse_java_source(source_bytes, path=file_path)
