import sys

# Append the current project root to sys.path so we can import the `core` package
import os
sys.path.append(os.path.abspath("."))

from core.db.dossiers import _build_chart_24h
import time

# Create a test case
now = time.time()
points = [
    {"timestamp": now - 3600*20, "player_count": 10},
    {"timestamp": now - 3600*10, "player_count": 20},
    {"timestamp": now, "player_count": 15},
]
chart = _build_chart_24h(points, window_start=now-86400, window_end=now)
print(chart)
