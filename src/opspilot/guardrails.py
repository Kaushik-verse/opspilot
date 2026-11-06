"""Safety policy for every command the agent wants to run.

Design principles (what an SRE team would demand before letting an LLM touch prod):
  * Allow-list, not block-list: only ``kubectl`` plus a few read-only text filters.
  * No shell: commands are tokenized and executed without a shell, so ``;``, ``&&``,
    redirects and command substitution are rejected outright.
  * Least privilege per task: detection/localization tasks are strictly read-only;
    write verbs are allowed only during mitigation, and destructive verbs never.
  * Blast radius: writes must target the application namespace, one named object at a time.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

READ_VERBS = {"get", "describe", "logs", "top", "explain", "api-resources", "version",
              "cluster-info", "events"}
WRITE_VERBS = {"scale", "patch", "set", "rollout", "label", "annotate", "delete"}
NEVER_VERBS = {"exec", "cp", "drain", "cordon", "uncordon", "taint", "run", "attach",
               "port-forward", "proxy", "replace", "create", "apply", "edit", "debug", "auth",
               "certificate", "config", "plugin", "kustomize", "wait"}
ROLLOUT_WRITE = {"restart", "undo", "pause", "resume"}
FILTERS = {"grep", "head", "tail", "wc", "sort", "uniq"}
FORBIDDEN_CHARS = (";", "&", ">", "<", "`", "$(", "\n")


@dataclass
class Decision:
    allowed: bool
    category: str  # read | write | blocked
    reason: str = ""
    pipeline: list[list[str]] = field(default_factory=list)

    @property
    def is_write(self) -> bool:
        return self.allowed and self.category == "write"


class GuardrailPolicy:
    def __init__(self, namespace: str, allow_writes: bool) -> None:
        self.namespace = namespace
        self.allow_writes = allow_writes

    def check(self, command: str) -> Decision:
        cmd = command.strip()
        if not cmd:
            return Decision(False, "blocked", "empty command")
        for ch in FORBIDDEN_CHARS:
            if ch in _strip_quoted(cmd):
                return Decision(False, "blocked",
                                f"shell operator {ch!r} is not allowed; run one kubectl command at a time "
                                "(you may pipe into grep/head/tail/wc/sort/uniq)")
        try:
            stages = [shlex.split(s) for s in _split_pipes(cmd)]
        except ValueError as e:
            return Decision(False, "blocked", f"could not parse command: {e}")
        if not stages or not stages[0]:
            return Decision(False, "blocked", "empty command")
        head = stages[0]
        if head[0] != "kubectl":
            return Decision(False, "blocked",
                            f"only kubectl is allowed (got {head[0]!r}); no curl, bash, or package tools")
        for st in stages[1:]:
            if not st or st[0] not in FILTERS:
                return Decision(False, "blocked",
                                f"pipe target {st[0] if st else ''!r} not allowed; use one of {sorted(FILTERS)}")
        argv = head[1:]
        verb = _verb(argv)
        if verb in NEVER_VERBS:
            return Decision(False, "blocked", f"'kubectl {verb}' is never permitted for this agent")
        if verb in READ_VERBS:
            return Decision(True, "read", pipeline=stages)
        if verb == "rollout":
            sub = _after(argv, "rollout")
            if sub in ("status", "history"):
                return Decision(True, "read", pipeline=stages)
            if sub not in ROLLOUT_WRITE:
                return Decision(False, "blocked", f"unknown rollout sub-command {sub!r}")
        if verb not in WRITE_VERBS and verb != "rollout":
            return Decision(False, "blocked", f"unknown or unsupported kubectl verb {verb!r}")

        # ---- write path
        if not self.allow_writes:
            return Decision(False, "blocked",
                            "this is a read-only task: changes to the cluster are not permitted. "
                            "Investigate with get/describe/logs and then submit your answer")
        if len(stages) > 1:
            return Decision(False, "blocked", "do not pipe the output of a mutating command")
        if any(a in ("-A", "--all-namespaces", "--all") for a in argv) or any(
                a.startswith("--all=") for a in argv) or "-l" in argv or "--selector" in argv:
            return Decision(False, "blocked", "bulk writes (--all, -A, label selectors) are not permitted")
        if verb == "delete":
            target = _after(argv, "delete")
            kind = target.split("/")[0] if target else ""
            if kind not in ("pod", "pods", "po"):
                return Decision(False, "blocked",
                                "only deleting individual pods (to force a restart) is permitted; "
                                "never delete deployments, services or namespaces")
            if "--force" in argv or "--grace-period=0" in argv:
                return Decision(False, "blocked", "forced deletion is not permitted")
        ns = _flag(argv, "-n", "--namespace")
        if ns != self.namespace:
            return Decision(False, "blocked",
                            f"writes must explicitly target the application namespace with -n {self.namespace}")
        if verb == "scale":
            reps = _flag(argv, "--replicas")
            if reps is not None and reps.isdigit() and int(reps) == 0:
                return Decision(False, "blocked", "scaling a workload to zero is not permitted")
            if reps is not None and reps.isdigit() and int(reps) > 10:
                return Decision(False, "blocked", "scaling above 10 replicas requires a human")
        return Decision(True, "write", pipeline=stages)


_GLOBAL_VALUE_FLAGS = {"-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user",
                       "-s", "--server", "--request-timeout"}


def _verb(argv: list[str]) -> str:
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in _GLOBAL_VALUE_FLAGS:
            skip = True
            continue
        if a.startswith("-"):
            continue
        return a
    return ""


def _strip_quoted(s: str) -> str:
    out, quote = [], None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        out.append(ch)
    return "".join(out)


def _split_pipes(s: str) -> list[str]:
    parts, cur, quote = [], [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == "|":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts]


def _flag(argv: list[str], *names: str) -> str | None:
    for i, a in enumerate(argv):
        for n in names:
            if a == n and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(n + "=") and n.startswith("--"):
                return a.split("=", 1)[1]
            if n == "-n" and a.startswith("-n") and len(a) > 2 and not a.startswith("--"):
                return a[2:].lstrip("=")
    return None


def _after(argv: list[str], word: str) -> str:
    seen = False
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("-n", "--namespace"):
            skip = True
            continue
        if a.startswith("-"):
            continue
        if seen:
            return a
        if a == word:
            seen = True
    return ""
