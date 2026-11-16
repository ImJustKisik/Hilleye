# Plan to address the request

## Context
The user wants to add time range selection (all time, week, day, etc.) and trend calculation to the server activity graph in the Hilleye monitor.

## Current State
- The sparkline graph is rendered using inline SVG (`sparkline-svg`) in `web/templates/server.html`.
- Data is prepared by `core/db/dossiers.py` in the `get_server_details` function, which fetches 24 hours of data.
- The `_build_chart_24h` function in `core/db/dossiers.py` generates the chart data.
- The chart is restricted to 24 hours in the backend logic.

## Proposed Changes
1.  **Backend (`core/db/dossiers.py`)**:
    *   Modify `get_server_details` to accept a `time_range` parameter (e.g., 'day', 'week', 'all').
    *   Adjust the SQL query in `get_server_details` to fetch historical data for the selected range instead of hardcoding `cutoff_24h = ref_snap_ts - 86400.0`.
    *   Update `_build_chart_24h` to handle different time ranges and potentially downsample data for longer periods if needed.

2.  **Frontend (`web/templates/server.html`)**:
    *   Add a dropdown/selector for the time range.
    *   Update the chart container to dynamically refresh via HTMX when the range is changed.
    *   Implement trend calculations (e.g., percentage change in average players compared to previous period).

## Next Steps
1.  Examine `core/db/dossiers.py` to see how to generalize `get_server_details` for different time ranges.
2.  Plan the UI component for range selection in `web/templates/server.html`.
3.  Implement changes and test.
