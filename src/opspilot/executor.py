"""Executes guard-railed kubectl pipelines without ever invoking a shell.

The kubectl stage goes to a pluggable backend (the simulator, or a real cluster via
``subprocess`` with an argv list). Pipe stages (grep/head/tail/...) are implemented in
Python, so behaviour is identical on both backends and nothing reaches /bin/sh.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Protocol

from opspilot.sim.cluster import Cluster, KubectlError

MAX_OUTPUT_CHARS = 3000


class Backend(Protocol):
    def kubectl(self, argv: list[str]) -> tuple[int, str]: ...


class SimBackend:
    def __init__(self, cluster: Cluster) -> None:
        self.cluster = cluster

    def kubectl(self, argv: list[str]) -> tuple[int, str]:
        try:
            return 0, self.cluster.kubectl(argv)
        except KubectlError as e:
            return 1, str(e)
        except Exception as e:  # never let a simulator bug crash an episode
            return 1, f"error: {type(e).__name__}: {e}"


class LiveBackend:
    """Runs real kubectl against the current kube-context (used by ``opspilot diagnose``)."""

    def __init__(self, timeout_s: int = 30) -> None:
        if shutil.which("kubectl") is None:
            raise RuntimeError("kubectl not found on PATH")
        self.timeout_s = timeout_s

    def kubectl(self, argv: list[str]) -> tuple[int, str]:
        try:
            p = subprocess.run(["kubectl", *argv], capture_output=True, text=True,
                               timeout=self.timeout_s, check=False)
        except subprocess.TimeoutExpired:
            return 124, f"error: kubectl timed out after {self.timeout_s}s"
        return p.returncode, (p.stdout + p.stderr).strip()


def _apply_filter(stage: list[str], text: str) -> str:
    cmd, args = stage[0], stage[1:]
    lines = text.splitlines()
    if cmd == "grep":
        flags = {a for a in args if a.startswith("-")}
        pats = [a for a in args if not a.startswith("-")]
        if not pats:
            return "grep: missing pattern"
        expanded = set()
        for f in flags:
            expanded |= set(f[1:]) if not f.startswith("--") else {f}
        pat = pats[0]
        reflags = re.IGNORECASE if "i" in expanded else 0
        if "E" in expanded or "P" in expanded:
            source = pat
        else:  # basic regex: treat everything literally except the common "\|" alternation
            source = "|".join(re.escape(p) for p in pat.split("\\|"))
        try:
            rx = re.compile(source, reflags)
        except re.error:
            rx = re.compile(re.escape(pat), reflags)
        keep = [ln for ln in lines if bool(rx.search(ln)) != ("v" in expanded)]
        if "c" in expanded:
            return str(len(keep))
        return "\n".join(keep)
    if cmd in ("head", "tail"):
        n = 10
        for i, a in enumerate(args):
            if a == "-n" and i + 1 < len(args) and args[i + 1].lstrip("+-").isdigit():
                n = int(args[i + 1].lstrip("+-"))
            elif a.startswith("-") and a[1:].isdigit():
                n = int(a[1:])
            elif a.startswith("-n") and a[2:].isdigit():
                n = int(a[2:])
        return "\n".join(lines[:n] if cmd == "head" else lines[-n:])
    if cmd == "wc":
        return str(len(lines)) if "-l" in args else f"{len(lines)} {len(text.split())} {len(text)}"
    if cmd == "sort":
        out = sorted(lines, reverse="-r" in args)
        return "\n".join(out)
    if cmd == "uniq":
        out, prev = [], object()
        for ln in lines:
            if ln != prev:
                out.append(ln)
            prev = ln
        return "\n".join(out)
    return f"{cmd}: not supported"


def run_pipeline(backend: Backend, pipeline: list[list[str]]) -> tuple[int, str]:
    head = pipeline[0]
    argv = head[1:] if head and head[0] == "kubectl" else head
    rc, out = backend.kubectl(argv)
    for stage in pipeline[1:]:
        out = _apply_filter(stage, out)
    return rc, out


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-limit // 3:]
    return f"{head}\n... [{len(text) - limit} chars truncated; use grep/--tail to narrow] ...\n{tail}"
