from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NATS_DIR = ROOT / "infra" / "railway-nats"


def test_railway_nats_service_fails_closed_with_persistent_jetstream() -> None:
    dockerfile = (NATS_DIR / "Dockerfile").read_text(encoding="utf-8")
    start_script = (NATS_DIR / "start-nats.sh").read_text(encoding="utf-8")
    manifest = json.loads((NATS_DIR / "railway.json").read_text(encoding="utf-8"))

    assert "FROM nats:" in dockerfile
    assert "CMD [\"/usr/local/bin/start-nats.sh\"]" in dockerfile

    assert "NATS_TOKEN is required" in start_script
    assert "RAILWAY_VOLUME_MOUNT_PATH is required" in start_script
    assert "NATS token auth enabled" in start_script
    assert "authorization {" in start_script
    assert "token:" in start_script
    assert "--auth" not in start_script
    assert "-auth" not in start_script
    assert "RAILWAY_TCP_APPLICATION_PORT" in start_script
    assert "http_port:" in start_script
    assert "jetstream {" in start_script
    assert "store_dir:" in start_script

    assert manifest["deploy"]["healthcheckPath"] == "/healthz"
    assert manifest["deploy"]["restartPolicyType"] == "ON_FAILURE"
    assert manifest["deploy"]["restartPolicyMaxRetries"] == 10
