"""Profile suggest/chart on disposable synthetic state, never the configured database."""
from pathlib import Path
import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import db


async def profile(players, snapshots):
    with tempfile.TemporaryDirectory(prefix="hilleye-profile-") as tmp:
        await db.init_db(str(Path(tmp) / "tracker.db"))
        try:
            conn = await db.get_connection()
            now = time.time()
            await conn.execute("INSERT INTO servers(identifier,title,ip,port,last_seen) VALUES ('byond://127.0.0.1:1234','Profile','127.0.0.1',1234,?)", (now,))
            await conn.executemany("INSERT INTO players VALUES (?,?,?)", [(f"player{i:08}", now-3600, now) for i in range(players)])
            await conn.executemany("INSERT INTO sessions(ckey,server_id,started_at,last_seen_at,ended_at,duration_seconds) VALUES (?,1,?,?,?,1200)",
                                   [(f"player{i:08}", now-1200, now, now) for i in range(players)])
            await conn.executemany("INSERT INTO server_snapshots(server_id,timestamp,player_count,round_duration) VALUES (1,?,?,?)",
                                   [(now-(snapshots-i)*60, i % 100, i % 7200) for i in range(snapshots)])
            await conn.commit()
            output = {"players": players, "snapshots": snapshots, "python": sys.version.split()[0], "samples": {}}
            async def measure(name, fn):
                durations = []
                result = None
                for _ in range(5):
                    start = time.perf_counter()
                    result = await fn()
                    durations.append(time.perf_counter()-start)
                output["samples"][name] = {"median_seconds": round(statistics.median(durations), 4),
                    "max_seconds": round(max(durations), 4), "response_bytes": len(json.dumps(result).encode())}
            await measure("suggest_common", lambda: db.search_players("player", limit=8))
            await measure("suggest_fuzzy", lambda: db.search_players("plxyer00000001", limit=8))
            await measure("server_chart_all", lambda: db.get_server_details(1, now_ts=now, time_range="all"))
            return output
        finally:
            await db.close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--players", type=int, default=10000)
    parser.add_argument("--snapshots", type=int, default=100000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(profile(args.players, args.snapshots))
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
