"""Validate writable runtime directories without modifying existing databases."""
from pathlib import Path
import tempfile


def validate_storage(config):
    locations = {"uploaded logs": config.resolved_logs_uploads_dir(), "map cache": config.resolved_maps_cache_dir()}
    for label, path in (("tracker database", config.resolved_db_path()), ("logs database", config.resolved_logs_db_path())):
        if path != ":memory:":
            locations[label] = Path(path).parent
    for label, path in locations.items():
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=path) as probe:
                probe.write(b"hilleye")
                probe.flush()
        except OSError as exc:
            raise RuntimeError(f"Cannot write {label} directory '{path}': {exc}") from exc
