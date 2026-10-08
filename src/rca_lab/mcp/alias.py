"""Per-episode aliasing of target UUIDs to short handles.

The base student cannot copy 36-character UUIDs reliably under sampling (observed:
"a0a6668c-f22e-429f-Bleu"). Every UUID the model sees — in the seed and in every tool envelope
— is replaced by a stable handle `T<n>`; every handle in the model's tool arguments and in its
final answer is mapped back before it reaches the tools or the scorer. The mapping is bijective
within an episode and recorded next to the trajectory, so teacher trajectories can be aliased
the same way for training and every stored message stays reversible.
"""

from __future__ import annotations

import re
from typing import Any

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
HANDLE_RE = re.compile(r"\bT(\d{1,4})\b")


class UUIDAliaser:
    def __init__(self) -> None:
        self._to_handle: dict[str, str] = {}
        self._to_uuid: dict[str, str] = {}

    @property
    def mapping(self) -> dict[str, str]:
        return dict(self._to_handle)

    def handle_for(self, value: str) -> str:
        key = value.lower()
        handle = self._to_handle.get(key)
        if handle is None:
            handle = f"T{len(self._to_handle) + 1}"
            self._to_handle[key] = handle
            self._to_uuid[handle] = key
        return handle

    def alias_text(self, text: str) -> str:
        return UUID_RE.sub(lambda match: self.handle_for(match.group(0)), text)

    def alias(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.alias_text(value)
        if isinstance(value, list):
            return [self.alias(item) for item in value]
        if isinstance(value, dict):
            return {self.alias_text(key) if isinstance(key, str) else key: self.alias(item) for key, item in value.items()}
        return value

    def resolve_text(self, text: str) -> str:
        return HANDLE_RE.sub(lambda match: self._to_uuid.get(match.group(0), match.group(0)), text)

    def resolve_sql_literals(self, query: str) -> tuple[str, set[str]]:
        """Resolve target handles only when a SQL string literal is exactly that handle.

        Raw SQL tools expose the query text to PostgreSQL/ClickHouse. Reusing resolve_text()
        there is too broad: it rewrites table aliases such as ``T1`` and comments before the
        database parser sees them. This scanner deliberately touches only quoted values.
        """
        out: list[str] = []
        unknown: set[str] = set()
        index = 0
        length = len(query)
        while index < length:
            char = query[index]
            next_char = query[index + 1] if index + 1 < length else ""
            if char == "-" and next_char == "-":
                end = query.find("\n", index + 2)
                if end == -1:
                    out.append(query[index:])
                    break
                out.append(query[index:end])
                index = end
                continue
            if char == "/" and next_char == "*":
                end, depth = index + 2, 1
                while end < length and depth:
                    if query.startswith("/*", end):
                        depth += 1
                        end += 2
                    elif query.startswith("*/", end):
                        depth -= 1
                        end += 2
                    else:
                        end += 1
                out.append(query[index:end])
                index = end
                continue
            if char in {'"', '`'}:
                end = index + 1
                while end < length:
                    if query[end] == char:
                        if end + 1 < length and query[end + 1] == char:
                            end += 2
                            continue
                        end += 1
                        break
                    end += 1
                out.append(query[index:end])
                index = end
                continue
            if char == "$":
                tag_end = index + 1
                while tag_end < length and (query[tag_end].isalnum() or query[tag_end] == "_"):
                    tag_end += 1
                if tag_end < length and query[tag_end] == "$":
                    tag = query[index : tag_end + 1]
                    end = query.find(tag, tag_end + 1)
                    if end == -1:
                        out.append(query[index:])
                        break
                    body_start = tag_end + 1
                    body = query[body_start:end]
                    resolved_body, body_unknown = self._resolve_exact_literal(body)
                    out.append(tag + resolved_body + tag)
                    unknown.update(body_unknown)
                    index = end + len(tag)
                    continue
            if char == "'":
                literal, end = self._read_single_quoted(query, index)
                if literal is None:
                    out.append(query[index:end])
                else:
                    resolved_literal, literal_unknown = self._resolve_exact_literal(literal)
                    out.append(query[index:end] if resolved_literal == literal else "'" + resolved_literal + "'")
                    unknown.update(literal_unknown)
                index = end
                continue
            out.append(char)
            index += 1
        return "".join(out), unknown

    @staticmethod
    def _read_single_quoted(query: str, start: int) -> tuple[str | None, int]:
        value: list[str] = []
        index = start + 1
        length = len(query)
        while index < length:
            char = query[index]
            if char == "\\" and index + 1 < length:
                value.extend(query[index : index + 2])
                index += 2
                continue
            if char == "'":
                if index + 1 < length and query[index + 1] == "'":
                    value.append("'")
                    index += 2
                    continue
                return "".join(value), index + 1
            value.append(char)
            index += 1
        return None, length

    def _resolve_exact_literal(self, value: str) -> tuple[str, set[str]]:
        if not HANDLE_RE.fullmatch(value):
            return value, set()
        resolved = self._to_uuid.get(value)
        if resolved is None:
            return value, {value}
        return resolved, set()

    def resolve(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.resolve_text(value)
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if isinstance(value, dict):
            return {key: self.resolve(item) for key, item in value.items()}
        return value

    def unknown_handles(self, value: Any) -> set[str]:
        """Handles the model used that were never issued — hallucinated targets."""
        found: set[str] = set()
        stack = [value]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                found.update(handle for handle in HANDLE_RE.findall(node) if f"T{handle}" not in self._to_uuid)
            elif isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                stack.extend(node.values())
        return {f"T{handle}" for handle in found}
