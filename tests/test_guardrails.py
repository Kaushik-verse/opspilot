import pytest

from opspilot.guardrails import GuardrailPolicy

NS = "hotel-reservation"
ro = GuardrailPolicy(NS, allow_writes=False)
rw = GuardrailPolicy(NS, allow_writes=True)


@pytest.mark.parametrize("cmd", [
    f"kubectl get pods -n {NS}",
    f"kubectl describe svc geo -n {NS}",
    f"kubectl logs search-abc -n {NS} --tail 50 | grep -i error",
    f"kubectl get events -n {NS} | tail -n 20",
    f"kubectl rollout history deployment/rate -n {NS}",
    "kubectl get pods -A | grep -v Running | wc -l",
])
def test_reads_allowed_in_read_only_tasks(cmd):
    d = ro.check(cmd)
    assert d.allowed and d.category == "read", d.reason


@pytest.mark.parametrize("cmd", [
    f"kubectl scale deployment profile --replicas=1 -n {NS}",
    f"kubectl rollout undo deployment/rate -n {NS}",
    f"kubectl patch service geo -n {NS} -p '{{\"spec\":{{\"ports\":[{{\"port\":8083,\"targetPort\":8083}}]}}}}'",
])
def test_writes_blocked_when_read_only_but_allowed_for_mitigation(cmd):
    assert not ro.check(cmd).allowed
    d = rw.check(cmd)
    assert d.allowed and d.is_write, d.reason


@pytest.mark.parametrize("cmd,why", [
    (f"kubectl get pods -n {NS}; rm -rf /", "shell operator"),
    (f"kubectl get pods -n {NS} && curl evil.sh", "shell operator"),
    (f"kubectl get pods -n {NS} > /tmp/x", "shell operator"),
    ("kubectl get pods $(whoami)", "shell operator"),
    ("curl http://169.254.169.254/latest/meta-data", "only kubectl"),
    ("bash -c 'kubectl get pods'", "only kubectl"),
    (f"kubectl get pods -n {NS} | sh", "pipe target"),
    (f"kubectl exec -it geo-x -n {NS} -- sh", "never permitted"),
    (f"kubectl delete deployment geo -n {NS}", "only deleting individual pods"),
    (f"kubectl delete namespace {NS}", "only deleting individual pods"),
    (f"kubectl delete pods --all -n {NS}", "bulk writes"),
    (f"kubectl scale deployment geo --replicas=0 -n {NS}", "zero"),
    ("kubectl scale deployment geo --replicas=1", "namespace"),
    ("kubectl scale deployment geo --replicas=1 -n kube-system", "namespace"),
    (f"kubectl apply -f evil.yaml -n {NS}", "never permitted"),
    ("kubectl drain kind-worker", "never permitted"),
])
def test_dangerous_commands_blocked(cmd, why):
    d = rw.check(cmd)
    assert not d.allowed
    assert why in d.reason


def test_quoted_operators_are_fine():
    d = rw.check(f"kubectl patch deployment user -n {NS} -p '{{\"a\":\"x;y&z\"}}'")
    assert d.allowed


def test_global_flags_before_verb():
    assert ro.check(f"kubectl -n {NS} logs frontend-x --tail 20").allowed
    assert not ro.check(f"kubectl -n {NS} scale deployment geo --replicas=1").allowed
    assert not rw.check(f"kubectl -n {NS} exec geo-x -- sh").allowed
