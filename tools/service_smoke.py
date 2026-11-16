"""Isolated Linux/systemd smoke check. --runtime requires root and system Python's version."""
from __future__ import annotations

import argparse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import site
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]


def child():
    sys.path[:0] = [str(PROJECT), str(PROJECT / ".deps")]
    import uvicorn
    from core.config import get_config
    config = get_config()
    uvicorn.run("web.app:app", host="127.0.0.1", port=config.web["port"], workers=2, log_level="warning")


def verify_unit():
    text = (PROJECT / "hilleye.service").read_text(encoding="utf-8")
    text = "\n".join(f"ExecStart={sys.executable} --version" if line.startswith("ExecStart=") else line for line in text.splitlines())
    with tempfile.TemporaryDirectory(prefix="hilleye-unit-") as tmp:
        unit = Path(tmp) / "hilleye-check.service"
        unit.write_text(text, encoding="utf-8")
        subprocess.run(["systemd-analyze", "verify", str(unit)], check=True)
    print("systemd unit verified")


def runtime():
    if os.geteuid() != 0:
        raise RuntimeError("Use sudo with the current venv's Python for the isolated runtime smoke test")
    version = subprocess.check_output(["/usr/bin/python3", "-c", "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"], text=True).strip()
    if version != f"{sys.version_info.major}.{sys.version_info.minor}":
        raise RuntimeError("Run this check in a venv created with /usr/bin/python3")
    tag = "hilleye-smoke-" + uuid.uuid4().hex[:10]
    class Hub(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"[]")
        def log_message(self, *_): pass
    hub = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    unit_path = Path("/run/systemd/system") / f"{tag}.service"
    with tempfile.TemporaryDirectory(prefix=tag + "-", dir="/run") as tmp:
        stage = Path(tmp)
        stage.chmod(0o755)
        source = stage / "source"
        source.mkdir(mode=0o755)
        for directory in ("core", "web", "tools"):
            shutil.copytree(PROJECT / directory, source / directory, ignore=shutil.ignore_patterns("__pycache__"))
        deps = source / ".deps"
        deps.mkdir()
        for directory in site.getsitepackages():
            if Path(directory).is_dir():
                shutil.copytree(directory, deps, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        maps = source / "maps_repo" / "maps" / "sierra"
        maps.mkdir(parents=True)
        (maps / "z1-z4_sierra.dmm").write_text('"a" = (/turf/space,/area/space)\n(1,1,1) = {"\naa\naa\n"}\n', encoding="utf-8")
        state = Path("/var/lib") / tag
        config = dict(db_path=str(state / "tracker.db"), logs_db_path=str(state / "logs.db"),
                      logs_uploads_dir=str(state / "uploaded_logs"), maps_cache_dir=str(state / "map_cache"),
                      custom_servers=[], hub_url=f"http://127.0.0.1:{hub.server_port}/", hub_text_url=f"http://127.0.0.1:{hub.server_port}/",
                      poll_interval=1, logs_periodic_scan=True, logs_scan_interval_seconds=1,
                      maps_auto_build_on_startup=False, maps_repo_dir=str(source / "maps_repo"),
                      discord_client_id="smoke", discord_client_secret="smoke", discord_redirect_uri="https://smoke.test/callback",
                      discord_session_secret="isolated-smoke-only", discord_secure_cookies=False,
                      discord_superadmin_ids=["111111111111111111"], web={"port": port})
        config_path = source / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        text = (PROJECT / "hilleye.service").read_text(encoding="utf-8")
        text = text.replace("User=hilleye", f"User={tag}").replace("Group=hilleye", f"Group={tag}")
        text = text.replace("StateDirectory=hilleye", f"StateDirectory={tag}").replace("/var/lib/hilleye", str(state))
        text = "\n".join(
            f"WorkingDirectory={source}" if line.startswith("WorkingDirectory=") else
            f"ExecStart=/usr/bin/python3 {source / 'tools/service_smoke.py'} --child" if line.startswith("ExecStart=") else line
            for line in text.splitlines() if not line.startswith("EnvironmentFile=")
        )
        text += f'\n[Service]\nDynamicUser=yes\nEnvironment="HILLEYE_CONFIG_PATH={config_path}"\n'
        unit_path.write_text(text, encoding="utf-8")
        started = False
        try:
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "start", tag], check=True)
            started = True
            import httpx
            sys.path.insert(0, str(PROJECT))
            from core.config import AppConfig
            from core.discord_auth import create_session_cookie, SESSION_COOKIE_NAME, verify_payload
            cfg = AppConfig(**config)
            cookie = create_session_cookie(dict(discord_id="111111111111111111", username="smoke", display_name="Smoke"), cfg)
            csrf = verify_payload(cookie, cfg)["csrf"]
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30,
                              cookies={SESSION_COOKIE_NAME: cookie}) as client:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        if client.get("/ready").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError("Service did not become ready")
                    time.sleep(.2)
                line = b"[2026-11-16 12:00:00] SAY: smoke-marker\n"
                upload = client.post("/logs/upload", data={"csrf_token": csrf}, files={"file": ("game.log", line)}, headers={"Accept": "application/json"})
                upload.raise_for_status()
                uid = upload.json()["rounds"][0]["round_uid"]
                download = client.get(f"/api/v1/logs/rounds/{uid}/download?mode=raw")
                download.raise_for_status()
                assert download.content == line
                rebuild = client.post("/logs/admin/maps/rebuild", data={"csrf_token": csrf, "background": "1"}, headers={"Accept": "application/json"})
                rebuild.raise_for_status()
                job_id = rebuild.json()["job_id"]
                deadline = time.monotonic() + 30
                while True:
                    job = client.get(f"/api/v1/logs/jobs/{job_id}").json()
                    if job["state"] == "succeeded": break
                    if job["state"] == "failed" or time.monotonic() > deadline: raise RuntimeError(f"Map build failed: {job}")
                    time.sleep(.2)
                assert client.get("/logs/maps/z/1.png").status_code == 200
            print("systemd runtime verified: protected startup, upload/download, background map build")
        except BaseException:
            subprocess.run(["journalctl", "-u", tag, "-n", "60", "--no-pager"], check=False)
            raise
        finally:
            if started: subprocess.run(["systemctl", "stop", tag], check=False)
            subprocess.run(["systemctl", "clean", "--what=state", tag], check=False, capture_output=True)
            unit_path.unlink(missing_ok=True)
            subprocess.run(["systemctl", "daemon-reload"], check=False)
            hub.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--verify-unit", action="store_true")
    parser.add_argument("--runtime", action="store_true")
    options = parser.parse_args()
    if options.child: child()
    elif options.runtime: runtime()
    else: verify_unit()
