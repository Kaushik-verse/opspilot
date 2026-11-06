import yaml

from opspilot.live import manifest_yaml
from opspilot.sim.app import SERVICES


def test_manifest_covers_every_service():
    docs = list(yaml.safe_load_all(manifest_yaml()))
    deps = {d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"}
    svcs = {d["metadata"]["name"] for d in docs if d["kind"] == "Service"}
    assert deps == set(SERVICES) == svcs


def test_containers_enforce_required_env_and_call_dependencies():
    docs = {d["metadata"]["name"]: d for d in yaml.safe_load_all(manifest_yaml()) if d["kind"] == "Deployment"}
    script = docs["user"]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "required environment variable DB_URL is not set" in script
    assert "mongodb-user:27017" in script
    search = docs["search"]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "geo:8083" in search and "rate:8084" in search
