"""Bounded per-process operational samples; route patterns avoid player/query cardinality."""
import asyncio
from collections import defaultdict, deque
import logging
import time

logger = logging.getLogger(__name__)


class Telemetry:
    def __init__(self):
        self.routes = defaultdict(lambda: deque(maxlen=512))
        self.loop_lag_seconds = 0.0
        self.log_audit_write_failures = 0

    def record(self, route, elapsed, status=None):
        self.routes[route].append(elapsed)
        logger.info("http_duration route=%s status=%s elapsed=%.4f", route, status, elapsed)

    def snapshot(self):
        def summary(values):
            data = sorted(values)
            return {"samples": len(data), "p50_seconds": round(data[(len(data)-1)//2], 4),
                    "p95_seconds": round(data[min(len(data)-1, int(len(data)*.95))], 4)}
        return {"loop_lag_seconds": round(self.loop_lag_seconds, 4),
                "log_audit_write_failures": self.log_audit_write_failures,
                "http": {route: summary(values) for route, values in self.routes.items() if values}}


telemetry = Telemetry()


async def monitor_loop():
    while True:
        start = time.monotonic()
        await asyncio.sleep(.5)
        telemetry.loop_lag_seconds = max(0, time.monotonic() - start - .5)
