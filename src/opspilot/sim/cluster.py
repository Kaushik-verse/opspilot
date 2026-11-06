"""An in-memory Kubernetes cluster that answers a realistic subset of ``kubectl``.

Desired state (Deployments, Services, Nodes) is stored as plain Kubernetes-shaped dicts.
Observed state (Pods, Endpoints, Events, logs) is *derived* from desired state on every
call, the same way controllers reconcile a real cluster. Faults are therefore injected
with ordinary kubectl mutations, and an agent fixes them with ordinary kubectl mutations.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

import yaml

from opspilot.sim.app import NAMESPACE, NODES, SERVICES, ServiceSpec


class KubectlError(Exception):
    """Raised for commands kubectl itself would reject. ``str(e)`` is the error output."""


def _h(text: str, n: int = 5) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:n]


def _table(headers: list[str], rows: list[list[str]], no_headers: bool = False) -> str:
    data = rows if no_headers else [headers] + rows
    if not data:
        return ""
    widths = [max(len(str(r[i])) for r in data) for i in range(len(data[0]))]
    lines = ["   ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in data]
    return "\n".join(lines)


RESOURCE_ALIASES = {
    "po": "pods", "pod": "pods", "pods": "pods",
    "svc": "services", "service": "services", "services": "services",
    "deploy": "deployments", "deployment": "deployments", "deployments": "deployments",
    "deployment.apps": "deployments", "deployments.apps": "deployments",
    "ep": "endpoints", "endpoint": "endpoints", "endpoints": "endpoints",
    "no": "nodes", "node": "nodes", "nodes": "nodes",
    "ev": "events", "event": "events", "events": "events",
    "ns": "namespaces", "namespace": "namespaces", "namespaces": "namespaces",
    "rs": "replicasets", "replicaset": "replicasets", "replicasets": "replicasets",
    "cm": "configmaps", "configmap": "configmaps", "configmaps": "configmaps",
    "secret": "secrets", "secrets": "secrets",
    "all": "all",
}

VALUE_FLAGS = {
    "-n", "--namespace", "-o", "--output", "-l", "--selector", "--tail", "-c", "--container",
    "--replicas", "-p", "--patch", "--type", "--to-revision", "--since", "--field-selector",
    "--sort-by", "--timeout", "--context", "--kubeconfig", "--revision",
}
BOOL_FLAGS = {
    "-A", "--all-namespaces", "--previous", "-p_prev", "--show-labels", "--no-headers",
    "--all-containers", "-w", "--watch", "--timestamps", "-f", "--follow", "--wide",
}

KUBE_SYSTEM_PODS = [
    ("coredns-7db6d8ff4d-8xk2p", "kind-control-plane"),
    ("coredns-7db6d8ff4d-qz5vn", "kind-control-plane"),
    ("etcd-kind-control-plane", "kind-control-plane"),
    ("kube-apiserver-kind-control-plane", "kind-control-plane"),
    ("kube-controller-manager-kind-control-plane", "kind-control-plane"),
    ("kube-proxy-4tq9w", "kind-worker"),
    ("kube-proxy-n7c2l", "kind-worker2"),
    ("kube-scheduler-kind-control-plane", "kind-control-plane"),
]


@dataclass
class ParsedArgs:
    positional: list[str]
    flags: dict[str, str | bool]

    def get(self, *names: str, default=None):
        for n in names:
            if n in self.flags:
                return self.flags[n]
        return default


def parse_kubectl_args(argv: list[str]) -> ParsedArgs:
    positional: list[str] = []
    flags: dict[str, str | bool] = {}
    verb = argv[0] if argv else ""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("-") and tok != "-":
            if "=" in tok and tok.startswith("--"):
                k, v = tok.split("=", 1)
                flags[k] = v
            elif tok.startswith("-o") and len(tok) > 2 and not tok.startswith("--"):
                flags["-o"] = tok[2:].lstrip("=")
            elif tok.startswith("-n") and len(tok) > 2 and not tok.startswith("--"):
                flags["-n"] = tok[2:].lstrip("=")
            elif tok in ("-f",) and verb == "logs":
                flags["--follow"] = True
            elif tok == "-p" and verb == "logs":
                flags["--previous"] = True
            elif tok in VALUE_FLAGS:
                if i + 1 >= len(argv):
                    raise KubectlError(f"error: flag needs an argument: {tok}")
                flags[tok] = argv[i + 1]
                i += 1
            elif tok in BOOL_FLAGS:
                flags[tok] = True
            else:
                flags[tok] = True
        else:
            positional.append(tok)
        i += 1
    return ParsedArgs(positional, flags)


def _merge_patch(target, patch, list_keys=("port", "name")):
    """JSON merge patch (RFC 7386) with strategic-merge behaviour for keyed lists."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    if not isinstance(target, dict):
        target = {}
    out = copy.deepcopy(target)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, list) and isinstance(out.get(k), list) and v and all(
            isinstance(x, dict) for x in v
        ):
            key = next((lk for lk in list_keys if all(lk in x for x in v)), None)
            if key is None:
                out[k] = copy.deepcopy(v)
            else:
                merged = copy.deepcopy(out[k])
                for item in v:
                    idx = next((j for j, e in enumerate(merged) if e.get(key) == item[key]), None)
                    if idx is None:
                        merged.append(copy.deepcopy(item))
                    else:
                        merged[idx] = _merge_patch(merged[idx], item, list_keys)
                out[k] = merged
        elif isinstance(v, dict):
            out[k] = _merge_patch(out.get(k, {}), v, list_keys)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _json_pointer_parts(path: str) -> list[str]:
    if not path.startswith("/"):
        raise KubectlError(f"error: invalid JSON patch path {path!r}")
    return [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]


def _json_patch(doc: dict, ops: list[dict]) -> dict:
    doc = copy.deepcopy(doc)
    for op in ops:
        kind, parts = op.get("op"), _json_pointer_parts(op.get("path", ""))
        parent = doc
        for p in parts[:-1]:
            parent = parent[int(p)] if isinstance(parent, list) else parent.setdefault(p, {})
        last = parts[-1]
        if kind in ("add", "replace"):
            if isinstance(parent, list):
                if last == "-":
                    parent.append(op["value"])
                elif kind == "add":
                    parent.insert(int(last), op["value"])
                else:
                    parent[int(last)] = op["value"]
            else:
                parent[last] = op["value"]
        elif kind == "remove":
            if isinstance(parent, list):
                parent.pop(int(last))
            else:
                if last not in parent:
                    raise KubectlError(
                        f"The request is invalid: the server rejected our request due to an "
                        f"error in our request: path {op['path']} not found")
                parent.pop(last)
        else:
            raise KubectlError(f"error: unsupported JSON patch op {kind!r}")
    return doc


class Cluster:
    """A small, deterministic Kubernetes cluster running the hotel-reservation app."""

    def __init__(self) -> None:
        self.namespace = NAMESPACE
        self.nodes = [
            {"name": n["name"], "role": n["role"],
             "labels": {"kubernetes.io/hostname": n["name"], "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64"}}
            for n in NODES
        ]
        self.deployments: dict[str, dict] = {}
        self.services: dict[str, dict] = {}
        self.revisions: dict[str, list[dict]] = {}
        self.generation: dict[str, int] = {}
        self.changed: set[str] = set()
        self.events: list[dict] = []
        for spec in SERVICES.values():
            self.deployments[spec.name] = self._make_deployment(spec)
            self.services[spec.name] = self._make_service(spec)
            self.revisions[spec.name] = [copy.deepcopy(self.deployments[spec.name]["spec"]["template"])]
            self.generation[spec.name] = 0

    # ------------------------------------------------------------------ desired state
    def _make_deployment(self, spec: ServiceSpec) -> dict:
        container = {
            "name": spec.name,
            "image": spec.image,
            "ports": [{"containerPort": spec.port, "protocol": "TCP"}],
            "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
                          "limits": {"cpu": "500m", "memory": "256Mi"}},
        }
        if spec.env:
            container["env"] = [{"name": k, "value": v} for k, v in spec.env.items()]
        return {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": spec.name, "namespace": self.namespace,
                         "labels": {"app": spec.name, "tier": spec.kind}},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": spec.name}},
                "template": {
                    "metadata": {"labels": {"app": spec.name, "tier": spec.kind}},
                    "spec": {"containers": [container]},
                },
            },
        }

    def _make_service(self, spec: ServiceSpec) -> dict:
        ip = f"10.96.{(int(_h(spec.name, 4), 16) % 200) + 10}.{(int(_h(spec.name + 'x', 4), 16) % 240) + 10}"
        return {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": spec.name, "namespace": self.namespace, "labels": {"app": spec.name}},
            "spec": {
                "type": "ClusterIP", "clusterIP": ip,
                "selector": {"app": spec.name},
                "ports": [{"name": "grpc" if spec.kind == "app" else "mongo", "port": spec.port,
                           "targetPort": spec.port, "protocol": "TCP"}],
            },
        }

    def _event(self, etype: str, reason: str, obj: str, msg: str) -> None:
        self.events.append({"type": etype, "reason": reason, "object": obj, "message": msg,
                            "age": "12s"})

    def _template_changed(self, name: str) -> None:
        tmpl = copy.deepcopy(self.deployments[name]["spec"]["template"])
        if tmpl != self.revisions[name][-1]:
            self.revisions[name].append(tmpl)
            self.generation[name] += 1
            self.changed.add(name)
            self._event("Normal", "ScalingReplicaSet", f"deployment/{name}",
                        f"Scaled up replica set {name}-{_h(json.dumps(tmpl, sort_keys=True), 9)} to 1")

    # ------------------------------------------------------------------ observed state
    def _container(self, dep: dict) -> dict:
        return dep["spec"]["template"]["spec"]["containers"][0]

    def _env(self, dep: dict) -> dict[str, str]:
        return {e["name"]: e.get("value", "") for e in self._container(dep).get("env", []) or []}

    def pods(self) -> list[dict]:
        pods = []
        for name, dep in self.deployments.items():
            spec = SERVICES.get(name)
            tmpl = dep["spec"]["template"]
            rs = _h(json.dumps(tmpl, sort_keys=True), 9)
            node_selector = tmpl["spec"].get("nodeSelector") or {}
            c = self._container(dep)
            env = self._env(dep)
            for i in range(int(dep["spec"].get("replicas", 1) or 0)):
                suffix = _h(f"{name}-{i}-{self.generation[name]}", 5)
                pod_name = f"{name}-{rs}-{suffix}"
                workers = [n for n in self.nodes if n["role"] != "control-plane"]
                matching = [n for n in workers
                            if all(n["labels"].get(k) == v for k, v in node_selector.items())]
                node = matching[(int(_h(pod_name, 4), 16)) % len(matching)]["name"] if matching else None
                ip = f"10.244.{1 + (int(_h(pod_name, 2), 16) % 2)}.{(int(_h(pod_name, 3), 16) % 230) + 10}"
                missing = [v for v in (spec.required_env if spec else ()) if not env.get(v)]
                pod = {
                    "name": pod_name, "app": name, "node": node, "ip": ip if node else None,
                    "image": c["image"], "container": c["name"],
                    "ports": [p["containerPort"] for p in c.get("ports", [])],
                    "env": env, "labels": dict(tmpl["metadata"]["labels"]),
                    "age": "15s" if name in self.changed else "42m",
                    "restarts": 0, "ready": False, "phase": "Running", "status": "Running",
                    "reason": "", "message": "", "missing_env": missing,
                    "node_selector": node_selector,
                }
                if node is None:
                    pod.update(phase="Pending", status="Pending", reason="FailedScheduling",
                               message=("0/3 nodes are available: 1 node(s) had untolerated taint "
                                        "{node-role.kubernetes.io/control-plane: }, 2 node(s) didn't "
                                        "match Pod's node affinity/selector. preemption: 0/3 nodes "
                                        "are available: 3 Preemption is not helpful for scheduling."))
                elif spec and c["image"] not in spec.valid_images:
                    pod.update(phase="Pending", status="ImagePullBackOff", reason="Failed",
                               message=(f'Failed to pull image "{c["image"]}": rpc error: code = '
                                        f'NotFound desc = failed to pull and unpack image '
                                        f'"docker.io/{c["image"]}": not found'))
                elif missing:
                    pod.update(status="CrashLoopBackOff", restarts=6, reason="BackOff",
                               message="Back-off restarting failed container "
                                       f"{c['name']} in pod {pod_name}")
                else:
                    pod.update(ready=True)
                pods.append(pod)
        return pods

    def endpoints(self, svc_name: str, pods: list[dict] | None = None) -> list[str]:
        svc = self.services.get(svc_name)
        if not svc:
            return []
        pods = self.pods() if pods is None else pods
        sel = svc["spec"].get("selector") or {}
        tp = svc["spec"]["ports"][0].get("targetPort", svc["spec"]["ports"][0]["port"])
        return [f"{p['ip']}:{tp}" for p in pods
                if p["ready"] and sel and all(p["labels"].get(k) == v for k, v in sel.items())]

    def service_ok(self, svc_name: str, pods: list[dict] | None = None) -> bool:
        """True if traffic sent to the Service actually reaches a listening, ready container."""
        svc = self.services.get(svc_name)
        if not svc:
            return False
        pods = self.pods() if pods is None else pods
        sel = svc["spec"].get("selector") or {}
        port = svc["spec"]["ports"][0]
        tp = port.get("targetPort", port["port"])
        backing = [p for p in pods if p["ready"] and sel
                   and all(p["labels"].get(k) == v for k, v in sel.items())]
        return bool(backing) and any(int(tp) in p["ports"] for p in backing if str(tp).isdigit())

    def degraded(self, name: str, pods: list[dict], seen: frozenset = frozenset()) -> bool:
        spec = SERVICES.get(name)
        if not spec or name in seen:
            return False
        for d in spec.depends_on:
            if not self.service_ok(d, pods) or self.degraded(d, pods, seen | {name}):
                return True
        return False

    def health(self) -> dict:
        """Mitigation oracle: is every service of the app serving traffic again?"""
        problems = []
        pods = self.pods()
        for name in SERVICES:
            dep = self.deployments.get(name)
            if dep is None:
                problems.append(f"deployment/{name} is missing")
                continue
            if int(dep["spec"].get("replicas", 0) or 0) < 1:
                problems.append(f"deployment/{name} has 0 replicas")
            not_ready = [p["name"] for p in pods if p["app"] == name and not p["ready"]]
            if not_ready:
                problems.append(f"deployment/{name} has non-ready pods: {', '.join(not_ready)}")
            if name not in self.services:
                problems.append(f"service/{name} is missing")
            elif not self.service_ok(name, pods):
                problems.append(f"service/{name} does not route to a ready, listening pod")
        return {"healthy": not problems, "problems": problems}

    # ------------------------------------------------------------------ logs
    def _logs(self, pod: dict, tail: int | None, previous: bool) -> str:
        spec = SERVICES[pod["app"]]
        if pod["status"] in ("Pending", "ImagePullBackOff"):
            why = "ContainerCreating" if pod["status"] == "Pending" else "trying and failing to pull image"
            raise KubectlError(f'Error from server (BadRequest): container "{pod["container"]}" in pod '
                               f'"{pod["name"]}" is waiting to start: {why}')
        lines = list(spec.startup_logs)
        if pod["missing_env"]:
            lines = lines[:1] + [
                f'level=fatal msg="invalid configuration" error="required environment variable '
                f'{pod["missing_env"][0]} is not set"',
                "panic: config: missing required setting",
                "", "goroutine 1 [running]:", "main.main()",
                f"\t/go/src/hotel/{pod['app']}/main.go:47 +0x2f1",
            ]
            return "\n".join(lines[-tail:] if tail else lines)
        if previous:
            raise KubectlError(f'Error from server (BadRequest): previous terminated container '
                               f'"{pod["container"]}" in pod "{pod["name"]}" not found')
        pods = self.pods()
        for i in range(6):
            for d in spec.depends_on:
                dsvc = self.services.get(d)
                if dsvc is not None and not self.service_ok(d, pods):
                    port = dsvc["spec"]["ports"][0]["port"]
                    lines.append(
                        f'level=error msg="rpc error: code = Unavailable desc = connection error: '
                        f'desc = \\"transport: Error while dialing: dial tcp {dsvc["spec"]["clusterIP"]}:'
                        f'{port}: connect: connection refused\\"" target={d}:{port} attempt={i + 1}')
                elif dsvc is None:
                    lines.append(f'level=error msg="dns lookup failed: lookup {d} on 10.96.0.10:53: '
                                 f'no such host" target={d}')
                elif self.degraded(d, pods):
                    lines.append(f'level=error msg="rpc error: code = Internal desc = upstream '
                                 f'{d} returned error" method=/{d}.{d.title()}/Get')
            if spec.name == "frontend" and self.degraded("frontend", pods):
                lines.append('level=warning msg="request failed" path=/hotels status=500 latency=31ms')
            if i in (1, 4) and spec.kind == "app":
                lines.append(f'level=info msg="request served" latency={11 + i * 3}ms')
        if spec.name in ("search", "profile"):
            lines.append('level=warning msg="slow query" duration=212ms')
        return "\n".join(lines[-tail:] if tail else lines)

    # ------------------------------------------------------------------ kubectl
    def kubectl(self, argv: list[str]) -> str:
        if argv and argv[0] == "kubectl":
            argv = argv[1:]
        if not argv:
            raise KubectlError("error: You must specify a command. See 'kubectl --help'.")
        args = parse_kubectl_args(argv)
        verb, pos = args.positional[0] if args.positional else "", args.positional[1:]
        handler = getattr(self, f"_cmd_{verb.replace('-', '_')}", None)
        if handler is None:
            raise KubectlError(f'error: unknown command "{verb}" for "kubectl"')
        return handler(pos, args)

    def _ns(self, args: ParsedArgs) -> str | None:
        if args.get("-A", "--all-namespaces"):
            return None
        return str(args.get("-n", "--namespace", default="default"))

    def _resolve(self, pos: list[str]) -> tuple[str, str | None]:
        if not pos:
            raise KubectlError("error: You must specify the type of resource to get.")
        first = pos[0]
        if "/" in first:
            kind, name = first.split("/", 1)
        else:
            kind, name = first, (pos[1] if len(pos) > 1 else None)
        if "," in kind:
            return kind, name
        if kind.lower() not in RESOURCE_ALIASES:
            raise KubectlError(f'error: the server doesn\'t have a resource type "{kind}"')
        return RESOURCE_ALIASES[kind.lower()], name

    def _not_found(self, kind: str, name: str) -> KubectlError:
        singular = {"pods": "pods", "services": "services", "deployments": "deployments.apps",
                    "endpoints": "endpoints", "nodes": "nodes"}.get(kind, kind)
        return KubectlError(f'Error from server (NotFound): {singular} "{name}" not found')

    def _pod_by_name(self, name: str, ns: str | None) -> dict:
        if ns not in (None, self.namespace):
            raise self._not_found("pods", name)
        for p in self.pods():
            if p["name"] == name:
                return p
        raise self._not_found("pods", name)

    # -- get
    def _cmd_get(self, pos: list[str], args: ParsedArgs) -> str:
        kind, name = self._resolve(pos)
        if "," in kind or kind == "all":
            kinds = ["pods", "services", "deployments"] if kind == "all" else [
                RESOURCE_ALIASES.get(k.lower(), k) for k in kind.split(",")]
            parts = []
            for k in kinds:
                out = self._cmd_get([k] + ([name] if name else []), args)
                parts.append(out)
            return "\n\n".join(p for p in parts if p)
        ns = self._ns(args)
        output = str(args.get("-o", "--output", default="")) or ""
        selector = args.get("-l", "--selector")
        no_headers = bool(args.get("--no-headers"))
        sel = dict(kv.split("=", 1) for kv in str(selector).split(",") if "=" in kv) if selector else {}
        if output and output.split("=")[0] not in ("yaml", "json", "wide", "name"):
            raise KubectlError(f"error: output format {output.split('=')[0]!r} is not supported by this "
                               "cluster's API proxy; use -o yaml, -o json, -o wide or -o name")

        if kind == "namespaces":
            rows = [[n, "Active", "6h"] for n in
                    ["default", "kube-node-lease", "kube-public", "kube-system", self.namespace]]
            return _table(["NAME", "STATUS", "AGE"], rows, no_headers)
        if kind == "nodes":
            if output in ("yaml", "json"):
                return self._dump(self.nodes, output)
            rows = [[n["name"], "Ready", n["role"], "6h", "v1.30.0"] for n in self.nodes
                    if not name or n["name"] == name]
            if name and not rows:
                raise self._not_found("nodes", name)
            return _table(["NAME", "STATUS", "ROLES", "AGE", "VERSION"], rows, no_headers)

        in_app_ns = ns in (None, self.namespace)
        if ns == "kube-system" and kind == "pods":
            rows = [[p, "1/1", "Running", "0", "6h"] for p, _ in KUBE_SYSTEM_PODS]
            return _table(["NAME", "READY", "STATUS", "RESTARTS", "AGE"], rows, no_headers)
        if not in_app_ns or kind in ("replicasets", "configmaps", "secrets"):
            if kind in ("replicasets",) and in_app_ns:
                return self._get_replicasets(no_headers)
            if kind == "configmaps" and in_app_ns:
                return _table(["NAME", "DATA", "AGE"], [["kube-root-ca.crt", "1", "42m"]], no_headers)
            if name:
                raise self._not_found(kind, name)
            return f"No resources found in {ns} namespace."

        if kind == "pods":
            pods = [p for p in self.pods() if (not name or p["name"] == name)
                    and all(p["labels"].get(k) == v for k, v in sel.items())]
            if name and not pods:
                raise self._not_found("pods", name)
            if output in ("yaml", "json"):
                return self._dump([self._pod_obj(p) for p in pods], output, single=bool(name))
            if output == "name":
                return "\n".join(f"pod/{p['name']}" for p in pods)
            headers = ["NAME", "READY", "STATUS", "RESTARTS", "AGE"]
            if output == "wide":
                headers += ["IP", "NODE", "NOMINATED NODE", "READINESS GATES"]
            if ns is None:
                headers = ["NAMESPACE"] + headers
            rows = []
            for p in pods:
                r = [p["name"], "1/1" if p["ready"] else "0/1", p["status"],
                     f"{p['restarts']} (31s ago)" if p["restarts"] else "0", p["age"]]
                if output == "wide":
                    r += [p["ip"] or "<none>", p["node"] or "<none>", "<none>", "<none>"]
                rows.append(([self.namespace] if ns is None else []) + r)
            if ns is None and not name:
                rows = [["kube-system", p, "1/1", "Running", "0", "6h"]
                        + (["10.244.0.2", node, "<none>", "<none>"] if output == "wide" else [])
                        for p, node in KUBE_SYSTEM_PODS] + rows
            if not rows:
                return f"No resources found in {ns} namespace."
            return _table(headers, rows, no_headers)

        if kind == "services":
            svcs = [s for n, s in self.services.items() if not name or n == name]
            if name and not svcs:
                raise self._not_found("services", name)
            if output in ("yaml", "json"):
                return self._dump(svcs, output, single=bool(name))
            rows = [[s["metadata"]["name"], s["spec"]["type"], s["spec"]["clusterIP"], "<none>",
                     ",".join(f"{p['port']}/{p.get('protocol', 'TCP')}" for p in s["spec"]["ports"]),
                     "42m"] + ([",".join(f"{k}={v}" for k, v in (s["spec"].get("selector") or {}).items())]
                               if output == "wide" else [])
                    for s in svcs]
            headers = ["NAME", "TYPE", "CLUSTER-IP", "EXTERNAL-IP", "PORT(S)", "AGE"] + (
                ["SELECTOR"] if output == "wide" else [])
            return _table(headers, rows, no_headers)

        if kind == "deployments":
            deps = [d for n, d in self.deployments.items() if not name or n == name]
            if name and not deps:
                raise self._not_found("deployments", name)
            if output in ("yaml", "json"):
                return self._dump([self._dep_obj(d) for d in deps], output, single=bool(name))
            pods = self.pods()
            rows = []
            for d in deps:
                n = d["metadata"]["name"]
                want = int(d["spec"].get("replicas", 0) or 0)
                ready = sum(1 for p in pods if p["app"] == n and p["ready"])
                row = [n, f"{ready}/{want}", str(want), str(ready), "42m"]
                if output == "wide":
                    c = self._container(d)
                    row += [c["name"], c["image"], f"app={n}"]
                rows.append(row)
            headers = ["NAME", "READY", "UP-TO-DATE", "AVAILABLE", "AGE"] + (
                ["CONTAINERS", "IMAGES", "SELECTOR"] if output == "wide" else [])
            return _table(headers, rows, no_headers)

        if kind == "endpoints":
            pods = self.pods()
            names = [n for n in self.services if not name or n == name]
            if name and not names:
                raise self._not_found("endpoints", name)
            rows = [[n, ",".join(self.endpoints(n, pods)) or "<none>", "42m"] for n in names]
            return _table(["NAME", "ENDPOINTS", "AGE"], rows, no_headers)

        if kind == "events":
            return self._get_events(no_headers)
        raise KubectlError(f'error: the server doesn\'t have a resource type "{kind}"')

    def _get_replicasets(self, no_headers: bool) -> str:
        pods = self.pods()
        rows = []
        for n, d in self.deployments.items():
            for idx, tmpl in enumerate(self.revisions[n]):
                current = idx == len(self.revisions[n]) - 1
                want = int(d["spec"].get("replicas", 0) or 0) if current else 0
                ready = sum(1 for p in pods if p["app"] == n and p["ready"]) if current else 0
                rows.append([f"{n}-{_h(json.dumps(tmpl, sort_keys=True), 9)}", str(want), str(want),
                             str(ready), "42m" if idx == 0 else "15s"])
        return _table(["NAME", "DESIRED", "CURRENT", "READY", "AGE"], rows, no_headers)

    def _get_events(self, no_headers: bool) -> str:
        rows = [[e["age"], e["type"], e["reason"], e["object"], e["message"]] for e in self.events]
        for p in self.pods():
            obj = f"pod/{p['name']}"
            if p["status"] == "Pending":
                rows.append(["38s", "Warning", "FailedScheduling", obj, p["message"]])
            elif p["status"] == "ImagePullBackOff":
                rows.append(["41s", "Warning", "Failed", obj, p["message"]])
                rows.append(["14s", "Normal", "BackOff", obj, f'Back-off pulling image "{p["image"]}"'])
            elif p["status"] == "CrashLoopBackOff":
                rows.append(["9s", "Warning", "BackOff", obj, p["message"]])
        if not rows:
            return f"No resources found in {self.namespace} namespace."
        return _table(["LAST SEEN", "TYPE", "REASON", "OBJECT", "MESSAGE"], rows, no_headers)

    def _pod_obj(self, p: dict) -> dict:
        cstate = ({"running": {"startedAt": "2026-11-05T09:12:00Z"}} if p["ready"] else
                  {"waiting": {"reason": p["status"] if p["status"] != "Pending" else "ContainerCreating",
                               "message": p["message"]}})
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": p["name"], "namespace": self.namespace, "labels": p["labels"]},
            "spec": {"nodeName": p["node"], "nodeSelector": p["node_selector"] or None,
                     "containers": [{"name": p["container"], "image": p["image"],
                                     "ports": [{"containerPort": x} for x in p["ports"]],
                                     "env": [{"name": k, "value": v} for k, v in p["env"].items()]}]},
            "status": {"phase": p["phase"], "podIP": p["ip"],
                       "containerStatuses": [{"name": p["container"], "ready": p["ready"],
                                              "restartCount": p["restarts"], "state": cstate}]},
        }

    def _dep_obj(self, d: dict) -> dict:
        out = copy.deepcopy(d)
        n = d["metadata"]["name"]
        ready = sum(1 for p in self.pods() if p["app"] == n and p["ready"])
        out["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": str(len(self.revisions[n]))}
        out["status"] = {"replicas": d["spec"].get("replicas", 0), "readyReplicas": ready,
                         "availableReplicas": ready}
        return out

    def _dump(self, items: list, output: str, single: bool = False):
        obj = items[0] if single and len(items) == 1 else {"apiVersion": "v1", "kind": "List", "items": items}
        if output == "json":
            return json.dumps(obj, indent=2)
        return yaml.safe_dump(obj, sort_keys=False).rstrip()

    # -- describe
    def _cmd_describe(self, pos: list[str], args: ParsedArgs) -> str:
        kind, name = self._resolve(pos)
        ns = self._ns(args)
        if kind == "nodes":
            nodes = [n for n in self.nodes if not name or n["name"] == name]
            if not nodes:
                raise self._not_found("nodes", name or "")
            return "\n\n".join(
                f"Name:               {n['name']}\nRoles:              {n['role']}\nLabels:             "
                + "\n                    ".join(f"{k}={v}" for k, v in n["labels"].items())
                + "\nTaints:             "
                + ("node-role.kubernetes.io/control-plane:NoSchedule" if n["role"] == "control-plane" else "<none>")
                + "\nConditions:\n  Type    Status  Reason\n  Ready   True    KubeletReady"
                for n in nodes)
        if ns not in (None, self.namespace):
            if name:
                raise self._not_found(kind, name)
            return f"No resources found in {ns} namespace."
        if kind == "pods":
            pods = [p for p in self.pods() if not name or p["name"] == name or p["name"].startswith(name + "-")]
            if not pods:
                raise self._not_found("pods", name or "")
            return "\n\n".join(self._describe_pod(p) for p in pods)
        if kind == "deployments":
            deps = [d for n, d in self.deployments.items() if not name or n == name]
            if not deps:
                raise self._not_found("deployments", name or "")
            return "\n\n".join(self._describe_dep(d) for d in deps)
        if kind == "services":
            svcs = [s for n, s in self.services.items() if not name or n == name]
            if not svcs:
                raise self._not_found("services", name or "")
            pods = self.pods()
            out = []
            for s in svcs:
                n = s["metadata"]["name"]
                p = s["spec"]["ports"][0]
                out.append(
                    f"Name:              {n}\nNamespace:         {self.namespace}\nLabels:            app={n}\n"
                    f"Selector:          {','.join(f'{k}={v}' for k, v in (s['spec'].get('selector') or {}).items())}\n"
                    f"Type:              {s['spec']['type']}\nIP:                {s['spec']['clusterIP']}\n"
                    f"Port:              {p.get('name', '<unset>')}  {p['port']}/{p.get('protocol', 'TCP')}\n"
                    f"TargetPort:        {p.get('targetPort', p['port'])}/{p.get('protocol', 'TCP')}\n"
                    f"Endpoints:         {','.join(self.endpoints(n, pods)) or '<none>'}\n"
                    f"Session Affinity:  None\nEvents:            <none>")
            return "\n\n".join(out)
        if kind == "endpoints":
            return self._cmd_get(["endpoints"] + ([name] if name else []), args)
        raise KubectlError(f'error: the server doesn\'t have a resource type "{kind}"')

    def _describe_pod(self, p: dict) -> str:
        env = "\n".join(f"      {k}:  {v}" for k, v in p["env"].items()) or "      <none>"
        if p["ready"]:
            state = "    State:          Running\n      Started:      Wed, 05 Nov 2026 09:12:00 +0000"
        elif p["status"] == "CrashLoopBackOff":
            state = ("    State:          Waiting\n      Reason:       CrashLoopBackOff\n"
                     "    Last State:     Terminated\n      Reason:       Error\n      Exit Code:    2")
        else:
            reason = "ImagePullBackOff" if p["status"] == "ImagePullBackOff" else "ContainerCreating"
            state = f"    State:          Waiting\n      Reason:       {reason}"
        events = "  <none>"
        if p["status"] != "Running" or not p["ready"]:
            reason = {"Pending": "FailedScheduling", "ImagePullBackOff": "Failed"}.get(p["status"], "BackOff")
            src = "default-scheduler" if p["status"] == "Pending" else "kubelet"
            events = (f"  Type     Reason            Age   From               Message\n"
                      f"  ----     ------            ----  ----               -------\n"
                      f"  Warning  {reason:<16}  38s   {src:<17}  {p['message']}")
        ns = ", ".join(f"{k}={v}" for k, v in (p["node_selector"] or {}).items()) or "<none>"
        return (
            f"Name:             {p['name']}\nNamespace:        {self.namespace}\n"
            f"Node:             {(p['node'] + '/172.18.0.3') if p['node'] else '<none>'}\n"
            f"Labels:           {', '.join(f'{k}={v}' for k, v in p['labels'].items())}\n"
            f"Status:           {p['phase']}\nIP:               {p['ip'] or ''}\n"
            f"Containers:\n  {p['container']}:\n    Image:          {p['image']}\n"
            f"    Port:           {', '.join(f'{x}/TCP' for x in p['ports'])}\n{state}\n"
            f"    Ready:          {p['ready']!s}\n    Restart Count:  {p['restarts']}\n"
            f"    Environment:\n{env}\n"
            f"Conditions:\n  Type              Status\n  PodScheduled      {p['node'] is not None!s}\n"
            f"  Ready             {p['ready']!s}\n"
            f"Node-Selectors:   {ns}\nEvents:\n{events}"
        )

    def _describe_dep(self, d: dict) -> str:
        n = d["metadata"]["name"]
        c = self._container(d)
        pods = [p for p in self.pods() if p["app"] == n]
        want = int(d["spec"].get("replicas", 0) or 0)
        ready = sum(1 for p in pods if p["ready"])
        env = "\n".join(f"      {e['name']}:  {e.get('value', '')}" for e in c.get("env", []) or []) or "      <none>"
        ns = ", ".join(f"{k}={v}" for k, v in
                       (d["spec"]["template"]["spec"].get("nodeSelector") or {}).items()) or "<none>"
        avail = "True    MinimumReplicasAvailable" if ready >= want else "False   MinimumReplicasUnavailable"
        return (
            f"Name:                   {n}\nNamespace:              {self.namespace}\n"
            f"Labels:                 app={n}\nAnnotations:            deployment.kubernetes.io/revision: "
            f"{len(self.revisions[n])}\nSelector:               app={n}\n"
            f"Replicas:               {want} desired | {len(pods)} updated | {len(pods)} total | "
            f"{ready} available | {len(pods) - ready} unavailable\nStrategyType:           RollingUpdate\n"
            f"Pod Template:\n  Labels:  app={n}\n  Containers:\n   {c['name']}:\n"
            f"    Image:      {c['image']}\n    Port:       "
            f"{', '.join(str(p['containerPort']) + '/TCP' for p in c.get('ports', []))}\n"
            f"    Environment:\n{env}\n  Node-Selectors:  {ns}\n"
            f"Conditions:\n  Type           Status  Reason\n  Available      {avail}"
        )

    # -- logs / top
    def _cmd_logs(self, pos: list[str], args: ParsedArgs) -> str:
        if not pos:
            raise KubectlError("error: expected 'logs [-f] [-p] (POD | TYPE/NAME) [-c CONTAINER]'.")
        target = pos[0]
        ns = self._ns(args)
        if target.startswith(("deployment/", "deploy/", "deployments/")):
            app = target.split("/", 1)[1]
            cands = [p for p in self.pods() if p["app"] == app]
            if ns not in (None, self.namespace) or app not in self.deployments:
                raise self._not_found("deployments", app)
            if not cands:
                raise KubectlError(f"error: timed out waiting for the condition (deployment/{app} has no pods)")
            pod = cands[0]
        else:
            pod = self._pod_by_name(target.replace("pod/", ""), ns)
        tail = args.get("--tail")
        tail_n = int(tail) if tail not in (None, True) and str(tail).lstrip("-").isdigit() and int(tail) > 0 else None
        return self._logs(pod, tail_n, bool(args.get("--previous")))

    def _cmd_top(self, pos: list[str], args: ParsedArgs) -> str:
        kind = RESOURCE_ALIASES.get(pos[0].lower(), pos[0]) if pos else ""
        if kind == "nodes":
            rows = [[n["name"], "212m", "5%", "1488Mi", "18%"] for n in self.nodes]
            return _table(["NAME", "CPU(cores)", "CPU%", "MEMORY(bytes)", "MEMORY%"], rows)
        if kind != "pods":
            raise KubectlError('error: unknown command "top ' + (pos[0] if pos else "") + '"')
        if self._ns(args) not in (None, self.namespace):
            return f"No resources found in {self._ns(args)} namespace."
        rows = [[p["name"], f"{2 + int(_h(p['name'], 1), 16)}m", f"{18 + int(_h(p['name'], 2), 16) % 40}Mi"]
                for p in self.pods() if p["ready"]]
        return _table(["NAME", "CPU(cores)", "MEMORY(bytes)"], rows)

    # -- mutations
    def _dep_target(self, pos: list[str], args: ParsedArgs) -> str:
        kind, name = self._resolve(pos)
        if kind != "deployments" or not name:
            raise KubectlError("error: this simulator supports this command for deployments only")
        if self._ns(args) != self.namespace:
            raise self._not_found("deployments", name)
        if name not in self.deployments:
            raise self._not_found("deployments", name)
        return name

    def _cmd_scale(self, pos: list[str], args: ParsedArgs) -> str:
        name = self._dep_target(pos, args)
        reps = args.get("--replicas")
        if reps in (None, True) or not str(reps).isdigit():
            raise KubectlError("error: --replicas=COUNT is required, and COUNT must be greater than or equal to 0")
        old = self.deployments[name]["spec"].get("replicas", 1)
        self.deployments[name]["spec"]["replicas"] = int(reps)
        self.changed.add(name)
        rs = _h(json.dumps(self.deployments[name]["spec"]["template"], sort_keys=True), 9)
        verb = "down" if int(reps) < int(old or 0) else "up"
        self._event("Normal", "ScalingReplicaSet", f"deployment/{name}",
                    f"Scaled {verb} replica set {name}-{rs} to {reps} from {old}")
        return f"deployment.apps/{name} scaled"

    def _cmd_patch(self, pos: list[str], args: ParsedArgs) -> str:
        kind, name = self._resolve(pos)
        patch_s = args.get("-p", "--patch")
        if not name or patch_s in (None, True):
            raise KubectlError("error: must specify -p to patch")
        if self._ns(args) != self.namespace:
            raise self._not_found(kind, name)
        try:
            patch = json.loads(str(patch_s))
        except json.JSONDecodeError:
            try:
                patch = yaml.safe_load(str(patch_s))
            except yaml.YAMLError as e:
                raise KubectlError(f"error: unable to parse \"{patch_s}\": {e}") from e
        ptype = str(args.get("--type", default="strategic"))
        store = {"deployments": self.deployments, "services": self.services}.get(kind)
        if store is None:
            raise KubectlError(f"error: patching {kind} is not supported by this simulator")
        if name not in store:
            raise self._not_found(kind, name)
        before = copy.deepcopy(store[name])
        if ptype == "json":
            if not isinstance(patch, list):
                raise KubectlError("error: unable to parse JSON patch: expected a list of operations")
            new = _json_patch(store[name], patch)
        else:
            new = _merge_patch(store[name], patch if isinstance(patch, dict) else {})
        if kind == "services":
            for p in new["spec"].get("ports", []):
                tp = p.get("targetPort")
                if isinstance(tp, str) and tp.isdigit():
                    p["targetPort"] = int(tp)
        store[name] = new
        if kind == "deployments":
            self._template_changed(name)
        if new == before:
            return f"{'deployment.apps' if kind == 'deployments' else 'service'}/{name} patched (no change)"
        return f"{'deployment.apps' if kind == 'deployments' else 'service'}/{name} patched"

    def _cmd_set(self, pos: list[str], args: ParsedArgs) -> str:
        if not pos:
            raise KubectlError("error: Specify a sub-command: env, image, resources")
        sub, rest = pos[0], pos[1:]
        name = self._dep_target(rest[:1] if rest and "/" in rest[0] else rest[:2], args)
        assigns = rest[1:] if rest and "/" in rest[0] else rest[2:]
        c = self._container(self.deployments[name])
        if sub == "image":
            if not assigns:
                raise KubectlError("error: at least one image update is required")
            for a in assigns:
                cname, img = a.split("=", 1)
                if cname not in (c["name"], "*"):
                    raise KubectlError(f'error: unable to find container named "{cname}"')
                c["image"] = img
            self._template_changed(name)
            return f"deployment.apps/{name} image updated"
        if sub == "env":
            env = c.setdefault("env", [])
            extra = [str(v) for k, v in args.flags.items() if k == "--env"]
            for a in assigns + extra:
                if a.endswith("-") and "=" not in a:
                    env[:] = [e for e in env if e["name"] != a[:-1]]
                elif "=" in a:
                    k, v = a.split("=", 1)
                    for e in env:
                        if e["name"] == k:
                            e["value"] = v
                            break
                    else:
                        env.append({"name": k, "value": v})
                else:
                    raise KubectlError(f"error: invalid env assignment {a!r}")
            self._template_changed(name)
            return f"deployment.apps/{name} env updated"
        raise KubectlError(f'error: unknown command "set {sub}"')

    def _cmd_rollout(self, pos: list[str], args: ParsedArgs) -> str:
        if not pos:
            raise KubectlError("error: Specify a sub-command: history, restart, status, undo")
        sub = pos[0]
        name = self._dep_target(pos[1:], args)
        if sub == "restart":
            self.generation[name] += 1
            self.changed.add(name)
            return f"deployment.apps/{name} restarted"
        if sub == "status":
            pods = [p for p in self.pods() if p["app"] == name]
            want = int(self.deployments[name]["spec"].get("replicas", 0) or 0)
            ready = sum(1 for p in pods if p["ready"])
            if ready >= want:
                return f'deployment "{name}" successfully rolled out'
            return (f'Waiting for deployment "{name}" rollout to finish: {ready} of {want} updated '
                    f'replicas are available...\nerror: timed out waiting for the condition')
        if sub == "history":
            rows = [[str(i + 1), "<none>"] for i in range(len(self.revisions[name]))]
            return f"deployment.apps/{name}\n" + _table(["REVISION", "CHANGE-CAUSE"], rows)
        if sub == "undo":
            revs = self.revisions[name]
            to = args.get("--to-revision")
            if to not in (None, True):
                idx = int(str(to)) - 1
                if not 0 <= idx < len(revs):
                    raise KubectlError(f"error: unable to find specified revision {to} in history")
            else:
                if len(revs) < 2:
                    raise KubectlError(f"error: no rollout history found for deployment \"{name}\"")
                idx = len(revs) - 2
            self.deployments[name]["spec"]["template"] = copy.deepcopy(revs[idx])
            self.revisions[name].append(copy.deepcopy(revs[idx]))
            self.generation[name] += 1
            self.changed.add(name)
            return f"deployment.apps/{name} rolled back"
        raise KubectlError(f'error: unknown command "rollout {sub}"')

    def _cmd_delete(self, pos: list[str], args: ParsedArgs) -> str:
        kind, name = self._resolve(pos)
        if self._ns(args) != self.namespace or not name:
            raise KubectlError("error: resource(s) were provided, but no name was specified")
        if kind == "pods":
            pod = self._pod_by_name(name, self.namespace)
            self.generation[pod["app"]] += 1
            self.changed.add(pod["app"])
            return f'pod "{name}" deleted'
        store = {"deployments": self.deployments, "services": self.services}.get(kind)
        if store is None or name not in store:
            raise self._not_found(kind, name)
        del store[name]
        return f'{"deployment.apps" if kind == "deployments" else "service"} "{name}" deleted'

    def _cmd_version(self, pos, args) -> str:
        return "Client Version: v1.30.0\nKustomize Version: v5.0.4\nServer Version: v1.30.0"

    def _cmd_cluster_info(self, pos, args) -> str:
        return ("Kubernetes control plane is running at https://127.0.0.1:6443\n"
                "CoreDNS is running at https://127.0.0.1:6443/api/v1/namespaces/kube-system/services/kube-dns:dns/proxy")

    def _cmd_api_resources(self, pos, args) -> str:
        return _table(["NAME", "SHORTNAMES", "APIVERSION", "NAMESPACED", "KIND"], [
            ["endpoints", "ep", "v1", "true", "Endpoints"], ["events", "ev", "v1", "true", "Event"],
            ["nodes", "no", "v1", "false", "Node"], ["pods", "po", "v1", "true", "Pod"],
            ["services", "svc", "v1", "true", "Service"],
            ["deployments", "deploy", "apps/v1", "true", "Deployment"]])

    def _cmd_edit(self, pos, args) -> str:
        raise KubectlError("error: 'kubectl edit' needs an interactive editor; use 'kubectl patch' instead")

    def _cmd_apply(self, pos, args) -> str:
        raise KubectlError("error: no manifest files are available to this agent; use 'kubectl patch' instead")
