"""Parse the agent's reply into a structured action (AIOpsLab-compatible syntax).

Parsing is strict about *what* an action may be (only ``exec_shell``/``submit`` with literal
arguments, never arbitrary code) but lenient about *formatting*: if a model writes several
code blocks the first is used, and a bare ``submit([...])`` line without fences is accepted.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any

_BLOCK = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\n(.*?)\n?[ \t]*```", re.DOTALL)
_BARE = re.compile(r"^\s*((?:exec_shell|submit)\(.*\))\s*$", re.MULTILINE)
FORMAT_HELP = ('Reply with one short reasoning line and exactly one code block, e.g.\n'
               '```\nexec_shell("kubectl get pods -n <namespace>")\n```')


@dataclass
class Action:
    kind: str  # exec_shell | submit | invalid
    arg: Any = None
    has_arg: bool = False
    error: str = ""
    code: str = ""
    warning: str = ""

    @property
    def command(self) -> str:
        return self.arg if self.kind == "exec_shell" else ""

    @property
    def canonical(self) -> str:
        """Single-block rendering accepted by strict parsers such as AIOpsLab's."""
        return f"```\n{self.code}\n```"


def extract_blocks(text: str) -> list[str]:
    return [b.strip() for b in _BLOCK.findall(text or "")]


def parse_action(text: str) -> Action:
    blocks = extract_blocks(text)
    warning = ""
    if not blocks:
        bare = _BARE.findall(text or "")
        if not bare:
            return Action("invalid", error=f"Format error: found no code block. {FORMAT_HELP}")
        blocks, warning = bare[:1], "action written without a code block"
    elif len(blocks) > 1:
        warning = f"{len(blocks)} code blocks; only the first was executed"
    code = blocks[0]
    try:
        node = ast.parse(code, mode="eval").body
    except SyntaxError:
        return Action("invalid", error=f"Format error: could not parse {code[:120]!r} as a call. {FORMAT_HELP}")
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.keywords:
        return Action("invalid", error=f"Format error: the code block must be a single call. {FORMAT_HELP}")
    name = node.func.id
    try:
        args = [ast.literal_eval(a) for a in node.args]
    except ValueError:
        return Action("invalid", error="Format error: arguments must be literal strings/lists.")
    if name == "exec_shell":
        if len(args) != 1 or not isinstance(args[0], str):
            return Action("invalid", error='exec_shell takes exactly one string, e.g. exec_shell("kubectl get pods -n ns")')
        return Action("exec_shell", args[0], True, code=code, warning=warning)
    if name == "submit":
        if len(args) > 1:
            return Action("invalid", error="submit takes at most one argument")
        return Action("submit", args[0] if args else None, bool(args), code=code, warning=warning)
    return Action("invalid", error=f"Unknown action {name!r}. Use exec_shell(...) or submit(...).")


def normalize_for_aiopslab(text: str) -> str:
    """AIOpsLab's parser requires a bare ``` fence; strip language tags like ```python."""
    return re.sub(r"```[a-zA-Z0-9_-]+[ \t]*\n", "```\n", text or "")
