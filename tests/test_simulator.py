import shlex

import pytest

from opspilot.executor import SimBackend, run_pipeline
from opspilot.guardrails import GuardrailPolicy
from opspilot.scenarios import SCENARIOS
from opspilot.sim import Cluster, KubectlError

NS = "hotel-reservation"


def k(c: Cluster, cmd: str) -> str:
    return c.kubectl(shlex.split(cmd))


def injected(sid: str) -> Cluster:
    c = Cluster()
    for argv in SCENARIOS[sid].inject_argv:
        c.kubectl(argv)
    return c


def test_baseline_cluster_is_healthy():
    c = Cluster()
    assert c.health()["healthy"]
    assert "Running" in k(c, f"get pods -n {NS}")
    assert "No resources found in default namespace." == k(c, "get pods")


@pytest.mark.parametrize("sid", [s for s in SCENARIOS if s != "healthy"])
def test_every_fault_breaks_health(sid):
    assert not injected(sid).health()["healthy"]


def test_symptoms_are_realistic():
    assert "0/0" in k(injected("scale_zero"), f"get deploy profile -n {NS}")
    assert ":9999" in k(injected("target_port"), f"get endpoints geo -n {NS}")
    assert "ImagePullBackOff" in k(injected("bad_image"), f"get pods -n {NS}")
    assert "Pending" in k(injected("node_selector"), f"get pods -n {NS}")
    c = injected("missing_env")
    assert "CrashLoopBackOff" in k(c, f"get pods -n {NS}")
    pod = next(p["name"] for p in c.pods() if p["app"] == "user")
    assert "DB_URL is not set" in k(c, f"logs {pod} -n {NS}")


def test_errors_propagate_to_callers():
    c = injected("target_port")
    search = next(p["name"] for p in c.pods() if p["app"] == "search")
    frontend = next(p["name"] for p in c.pods() if p["app"] == "frontend")
    assert "target=geo:8083" in k(c, f"logs {search} -n {NS}")
    assert "upstream search returned error" in k(c, f"logs {frontend} -n {NS}")


@pytest.mark.parametrize("sid,fix", [
    ("scale_zero", f"scale deployment profile --replicas=1 -n {NS}"),
    ("target_port", f"patch svc geo -n {NS} -p '{{\"spec\":{{\"ports\":[{{\"port\":8083,\"targetPort\":8083}}]}}}}'"),
    ("target_port", f"patch svc geo -n {NS} --type=json -p '[{{\"op\":\"replace\",\"path\":\"/spec/ports/0/targetPort\",\"value\":8083}}]'"),
    ("bad_image", f"set image deployment/rate rate=opspilot/hotel-rate:1.4.2 -n {NS}"),
    ("bad_image", f"rollout undo deployment/rate -n {NS}"),
    ("node_selector", f"patch deployment reservation -n {NS} -p '{{\"spec\":{{\"template\":{{\"spec\":{{\"nodeSelector\":null}}}}}}}}'"),
    ("node_selector", f"patch deployment reservation -n {NS} --type=json -p '[{{\"op\":\"remove\",\"path\":\"/spec/template/spec/nodeSelector\"}}]'"),
    ("missing_env", f"set env deployment/user DB_URL=mongodb://mongodb-user:27017/user -n {NS}"),
    ("missing_env", f"rollout undo deployment/user -n {NS}"),
])
def test_standard_fixes_restore_health(sid, fix):
    c = injected(sid)
    k(c, fix)
    assert c.health()["healthy"], c.health()


def test_wrong_fix_does_not_restore_health():
    c = injected("target_port")
    k(c, f"rollout restart deployment/geo -n {NS}")
    assert not c.health()["healthy"]


def test_unknown_objects_and_verbs():
    c = Cluster()
    with pytest.raises(KubectlError, match="NotFound"):
        k(c, f"get pod nope -n {NS}")
    with pytest.raises(KubectlError, match="unknown command"):
        k(c, "frobnicate")


def test_pipeline_filters():
    c = injected("bad_image")
    d = GuardrailPolicy(NS, False).check(f"kubectl get pods -n {NS} | grep -v Running")
    rc, out = run_pipeline(SimBackend(c), d.pipeline)
    assert rc == 0 and "ImagePullBackOff" in out and "Running" not in out.replace("STATUS", "")
