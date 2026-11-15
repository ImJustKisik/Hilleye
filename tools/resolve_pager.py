"""
Windows BYOND Pager Endpoint Resolver for Hilleye.

Resolves `byond://BYOND.world.<id>` pager addresses into direct `ip:port`
endpoints by launching Dream Seeker against each hub server, capturing the
resulting non-hub TCP connection via psutil, optionally verifying BYOND Topic
connectivity, and writing `pager_endpoints.json` for VPS/GitHub sync.

Prerequisites:
  - Windows with BYOND installed (e.g. C:\\Program Files (x86)\\BYOND\\bin\\dreamseeker.exe)
  - Logged-in BYOND Pager session (byond.exe)
  - psutil (`pip install psutil`)
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import psutil

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.byond_topic import probe_server_topic
from core.config import DEFAULT_TOPIC_PROBE_QUERIES
from core.hub_scraper import fetch_hub_servers

DEFAULT_DREAMSEEKER_PATHS = [
    Path(r"C:\Program Files (x86)\BYOND\bin\dreamseeker.exe"),
    Path(r"C:\Program Files\BYOND\bin\dreamseeker.exe"),
]
BYOND_CONTROL_PORTS = {80, 443, 6001, 20002}


def find_dreamseeker(explicit: Optional[str] = None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise FileNotFoundError(f"dreamseeker.exe not found at {explicit}")
    for candidate in DEFAULT_DREAMSEEKER_PATHS:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Could not locate dreamseeker.exe in standard BYOND directories.")


def is_valid_game_endpoint(ip: str, port: int, allow_private: bool = False) -> bool:
    if not ip or port <= 0 or port > 65535 or port in BYOND_CONTROL_PORTS:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified:
        return False
    if not allow_private and not addr.is_global:
        return False
    return True


def resolve_single_via_dreamseeker(
    dreamseeker_path: Path,
    identifier: str,
    timeout: float = 8.0,
    allow_private: bool = False,
) -> Optional[Tuple[str, int]]:
    """Launches dreamseeker.exe for `identifier` and captures its game TCP endpoint."""
    proc = subprocess.Popen(
        [str(dreamseeker_path), identifier],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    try:
        ps_proc = psutil.Process(proc.pid)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                conns = ps_proc.net_connections(kind="tcp")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
            for c in conns:
                if not c.raddr:
                    continue
                ip, port = c.raddr.ip, int(c.raddr.port)
                if is_valid_game_endpoint(ip, port, allow_private=allow_private):
                    return ip, port
            time.sleep(0.2)
    finally:
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
        except Exception:
            pass
    return None


async def fetch_pager_targets(api_url: Optional[str]) -> List[Dict[str, Any]]:
    """Returns list of {identifier, title} for servers with BYOND.world.<id>."""
    targets: List[Dict[str, Any]] = []
    if api_url:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(api_url)
            resp.raise_for_status()
            servers = resp.json().get("servers", [])
        for s in servers:
            ident = str(s.get("identifier") or "")
            if "BYOND.world." in ident:
                targets.append({"identifier": ident, "title": str(s.get("title") or ident)})
        return targets

    async with httpx.AsyncClient(timeout=15.0) as client:
        servers = await fetch_hub_servers(client=client, custom_servers=[])
    for s in servers:
        if "BYOND.world." in s.identifier:
            targets.append({"identifier": s.identifier, "title": s.title})
    return targets


async def run_resolver(args: argparse.Namespace) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    dreamseeker_path = find_dreamseeker(args.dreamseeker)
    out_path = Path(args.output)

    existing_endpoints: Dict[str, Dict[str, Any]] = {}
    if out_path.is_file() and not args.prune:
        try:
            raw = json.loads(out_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("endpoints"), dict):
                existing_endpoints = dict(raw["endpoints"])
            elif isinstance(raw, dict):
                existing_endpoints = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except Exception as exc:
            print(f"[!] Warning: could not read existing {out_path}: {exc}")

    targets = await fetch_pager_targets(args.api_url)
    if args.only_missing:
        targets = [t for t in targets if t["identifier"] not in existing_endpoints]
    if args.limit > 0:
        targets = targets[: args.limit]

    print(f"[*] Found {len(targets)} BYOND.world targets to resolve using {dreamseeker_path}")
    resolved_count = 0

    def save_output() -> None:
        payload_out = {
            "updated_at": int(time.time()),
            "count": len(existing_endpoints),
            "endpoints": existing_endpoints,
        }
        out_path.write_text(json.dumps(payload_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    try:
        for idx, srv in enumerate(targets, start=1):
            ident = srv["identifier"]
            title = srv["title"]
            print(f"[{idx}/{len(targets)}] Resolving {title} ({ident})...", end=" ", flush=True)
            endpoint = resolve_single_via_dreamseeker(
                dreamseeker_path=dreamseeker_path,
                identifier=ident,
                timeout=args.timeout,
                allow_private=args.allow_private,
            )
            if endpoint is None:
                print("TIMEOUT/FAILED")
            else:
                ip, port = endpoint
                topic_query = None
                if args.verify_topic:
                    payload, winner, _ = await probe_server_topic(
                        ip=ip,
                        port=port,
                        queries=DEFAULT_TOPIC_PROBE_QUERIES,
                        timeout=3.0,
                    )
                    topic_query = winner if payload is not None else None
                existing_endpoints[ident] = {
                    "ip": ip,
                    "port": port,
                    "title": title,
                    "topic_query": topic_query,
                    "updated_at": int(time.time()),
                }
                resolved_count += 1
                print(f"OK -> {ip}:{port} (topic={topic_query or 'none'})")
                if resolved_count % 10 == 0:
                    save_output()

            if idx < len(targets) and args.delay > 0:
                time.sleep(args.delay)
    finally:
        save_output()

    print(f"[+] Saved {len(existing_endpoints)} endpoints ({resolved_count} newly resolved) to {out_path}")

    if args.git_push:
        subprocess.run(["git", "add", str(out_path)], cwd=PROJECT_ROOT, check=True)
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=PROJECT_ROOT,
        )
        if status.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", "chore: update pager_endpoints.json"],
                cwd=PROJECT_ROOT,
                check=True,
            )
            subprocess.run(["git", "push"], cwd=PROJECT_ROOT, check=True)
            print("[+] Pushed updated pager_endpoints.json to git remote.")
        else:
            print("[*] No changes to commit.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve BYOND.world.<id> endpoints via local Dream Seeker.")
    parser.add_argument("--output", default="pager_endpoints.json", help="Output JSON file path")
    parser.add_argument("--api-url", default="", help="Optional local Hilleye /api/v1/servers URL")
    parser.add_argument("--dreamseeker", default="", help="Explicit path to dreamseeker.exe")
    parser.add_argument("--timeout", type=float, default=8.0, help="Seconds to wait per server")
    parser.add_argument("--delay", type=float, default=2.5, help="Delay in seconds between servers")
    parser.add_argument("--limit", type=int, default=0, help="Max servers to resolve (0 = all)")
    parser.add_argument("--only-missing", action="store_true", help="Skip identifiers already in output JSON")
    parser.add_argument("--prune", action="store_true", help="Do not merge with existing output JSON")
    parser.add_argument("--verify-topic", action="store_true", help="Probe BYOND Topic on resolved endpoints")
    parser.add_argument("--allow-private", action="store_true", help="Allow private/RFC1918 IP addresses")
    parser.add_argument("--git-push", action="store_true", help="Git add/commit/push output file if changed")
    args = parser.parse_args()
    return asyncio.run(run_resolver(args))


if __name__ == "__main__":
    raise SystemExit(main())
