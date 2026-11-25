"""Verified SQLite/file backups and portable restoration: python -m core.maintenance."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
from stat import S_ISREG
import tarfile
import tempfile
import time
import uuid

from core.config import DEFAULT_CONFIG_PATH, load_config
from core.file_lock import FileLock, lock_path


def _hash(path, size=None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while size is None or size > 0:
            read_size = 1024 * 1024 if size is None else min(1024 * 1024, size)
            chunk = stream.read(read_size)
            if not chunk:
                if size is not None:
                    raise ValueError(f"Indexed prefix exceeds the backed up file: {path.name}")
                break
            digest.update(chunk)
            if size is not None:
                size -= len(chunk)
    return digest.hexdigest()


def _sqlite_check(path):
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"SQLite integrity check failed: {path.name}")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError(f"SQLite foreign key check failed: {path.name}")


def _copy_prefix(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, target.open("xb") as dst:
        initial = os.fstat(src.fileno())
        remaining = initial.st_size
        while remaining:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"Source was truncated during backup: {source.name}")
            dst.write(chunk)
            remaining -= len(chunk)
        final = os.fstat(src.fileno())
        if not os.path.samestat(source.stat(), final) or final.st_size < initial.st_size or (
            final.st_size == initial.st_size and final.st_mtime_ns != initial.st_mtime_ns
        ):
            raise ValueError(f"Source changed while it was copied: {source.name}")


def _copy_tree(source, target, *, excluded=()):
    if not source.is_dir():
        raise ValueError(f"Backup source is unavailable: {source}")
    def fail(exc):
        raise exc
    for directory, dirs, files in os.walk(source, onerror=fail):
        dirs[:] = [
            name for name in dirs
            if not name.startswith(".") and name.lower() != "runtime"
            and (Path(directory) / name).relative_to(source).as_posix() not in excluded
        ]
        for name in dirs + files:
            if (Path(directory) / name).is_symlink():
                raise ValueError("Backup sources must not contain symlinks")
        for name in files:
            original = Path(directory) / name
            if not S_ISREG(original.stat().st_mode):
                raise ValueError(f"Backup source is not a regular file: {original.name}")
            _copy_prefix(original, target / original.relative_to(source))


def _wait_lock(stack, path):
    lock = FileLock(path)
    deadline = time.monotonic() + 60
    while not lock.acquire():
        if time.monotonic() >= deadline:
            raise TimeoutError("Maintenance lock is busy; backup was not published")
        time.sleep(0.05)
    stack.callback(lock.release)


def verify_archive(archive):
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        by_name = {item.name: item for item in members}
        if len(members) != len(by_name):
            raise ValueError("Duplicate backup members")
        for member in members:
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts or "\\" in member.name or ":" in member.name:
                raise ValueError("Unsafe backup member")
        meta_member = by_name.get("manifest.json")
        if meta_member is None:
            raise ValueError("Backup manifest is missing")
        if meta_member.size > 16 * 1024 * 1024:
            raise ValueError("Backup manifest is too large")
        manifest = json.load(stream.extractfile(meta_member))
        if manifest.get("format") != 1 or set(by_name) != {"manifest.json", *manifest["files"]}:
            raise ValueError("Backup manifest does not match its contents")
        for name, expected in manifest["files"].items():
            member = by_name[name]
            if member.size != expected["size"]:
                raise ValueError(f"Backup size mismatch: {name}")
            digest = hashlib.sha256()
            with stream.extractfile(member) as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != expected["sha256"]:
                raise ValueError(f"Backup checksum mismatch: {name}")
        return manifest


def create_backup(config, destination=None):
    root = Path(destination or os.getenv("HILLEYE_BACKUP_DIR") or Path(config.resolved_db_path()).parent / "backups").resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for source in (config.resolved_logs_uploads_dir(), config.resolved_logs_root_dir()):
        if source is not None and root.is_relative_to(source.resolve()):
            raise ValueError("Backup destination must be outside the source directories")
    logs_path = config.resolved_logs_db_path()
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=root) as tmp:
        stage = Path(tmp)
        with ExitStack() as stack:
            _wait_lock(stack, lock_path(logs_path, "uploads"))
            _wait_lock(stack, lock_path(logs_path, "index-write"))
            for label, path in (("tracker.db", config.resolved_db_path()), ("logs.db", logs_path)):
                if path == ":memory:" or not Path(path).is_file():
                    raise ValueError(f"Required database is unavailable: {label}")
                with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(stage / label)) as target:
                        source.backup(target)
            uploads = config.resolved_logs_uploads_dir()
            if uploads.is_dir():
                with closing(sqlite3.connect(stage / "logs.db")) as conn:
                    has_tombstones = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='log_upload_deletions'",
                    ).fetchone()
                    excluded = {row[0] for row in conn.execute(
                        "SELECT relative_path FROM log_upload_deletions",
                    )} if has_tombstones else set()
                _copy_tree(uploads, stage / "uploaded_logs", excluded=excluded)
        for label in ("tracker.db", "logs.db"):
            _sqlite_check(stage / label)
        # The live game server does not share the log database locks.
        log_root = config.resolved_logs_root_dir()
        if log_root is not None:
            _copy_tree(log_root, stage / "raw_logs")
        with closing(sqlite3.connect(stage / "logs.db")) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(log_rounds)")}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            round_paths = {}
            prefixes = []
            for row in conn.execute("SELECT * FROM log_rounds"):
                source_root = uploads if row["source_type"] == "upload" else log_root
                if source_root is None:
                    # The operator disabled this source. Its live rows remain untouched.
                    conn.execute("DELETE FROM log_rounds WHERE round_uid=?", (row["round_uid"],))
                    continue
                relative = Path(row["dir_path"]).resolve().relative_to(source_root.resolve())
                prefix = "uploaded_logs" if row["source_type"] == "upload" else "raw_logs"
                staged_round = stage / prefix / relative
                if not staged_round.is_dir():
                    if "missing_since" in columns and row["missing_since"] is not None:
                        conn.execute("DELETE FROM log_rounds WHERE round_uid=?", (row["round_uid"],))
                        continue
                    raise ValueError("An indexed round is missing from the copied source; backup was not published")
                round_paths[row["round_uid"]] = (Path(prefix) / relative).as_posix()
                for flag, name in (("has_game_log", "game.log"), ("has_qdel_log", "qdel.log"),
                                   ("has_initialize_log", "initialize.log")):
                    if flag in columns and row[flag] and not (staged_round / name).is_file():
                        raise ValueError(f"An indexed round is missing {name} from the copied source")
                if "log_index_checkpoints" in tables and row["has_game_log"]:
                    checkpoint_row = conn.execute(
                        "SELECT checkpoint_json FROM log_index_checkpoints WHERE round_uid=? AND generation=?",
                        (row["round_uid"], row["active_generation"]),
                    ).fetchone()
                    if checkpoint_row:
                        checkpoint = json.loads(checkpoint_row[0])
                        if checkpoint.get("prefix_sha256"):
                            prefixes.append((staged_round / "game.log", checkpoint["offset"], checkpoint["prefix_sha256"]))
        for path, size, digest in prefixes:
            if _hash(path, size) != digest:
                raise ValueError("A backed up log differs from the active index; refresh the index before retrying")
        created = time.time()
        manifest = {"format": 1, "created_at": created, "has_raw_logs": log_root is not None,
                    "round_paths": round_paths, "files": {}}
        for path in stage.rglob("*"):
            if path.is_file():
                manifest["files"][path.relative_to(stage).as_posix()] = {"size": path.stat().st_size, "sha256": _hash(path)}
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        archive = stage / "archive.partial"
        with tarfile.open(archive, "w:gz") as stream:
            for name in ["manifest.json", *manifest["files"]]:
                stream.add(stage / name, arcname=name, recursive=False)
        verify_archive(archive)
        archive.chmod(0o600)
        published = root / f"hilleye-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(created))}-{uuid.uuid4().hex[:8]}.tar.gz"
        archive.replace(published)
    for old in root.glob("hilleye-*.tar.gz"):
        if old != published and old.stat().st_mtime < time.time() - 7 * 86400:
            old.unlink()
    with closing(sqlite3.connect(logs_path)) as conn, conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='log_maintenance_state'").fetchone():
            conn.execute("INSERT INTO log_maintenance_state VALUES ('last_backup',?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json", (json.dumps({"completed_at": time.time()}),))
    return published


def restore_backup(archive, destination):
    archive, destination = Path(archive).resolve(), Path(destination).resolve()
    manifest = verify_archive(archive)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Restore destination must be empty")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=destination.parent) as tmp:
        stage = Path(tmp)
        with tarfile.open(archive, "r:gz") as stream:
            by_name = {item.name: item for item in stream.getmembers()}
            for name in manifest["files"]:
                target = (stage / name).resolve()
                if not target.is_relative_to(stage.resolve()):
                    raise ValueError("Unsafe restore path")
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(by_name[name]) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                expected = manifest["files"][name]
                if target.stat().st_size != expected["size"] or _hash(target) != expected["sha256"]:
                    raise ValueError(f"Restored file checksum mismatch: {name}")
        (stage / "uploaded_logs").mkdir(exist_ok=True)
        for name in ("tracker.db", "logs.db"):
            _sqlite_check(stage / name)
        with closing(sqlite3.connect(stage / "logs.db")) as conn, conn:
            for uid, relative in manifest["round_paths"].items():
                source = (stage / relative).resolve()
                if not source.is_relative_to(stage.resolve()) or not source.is_dir():
                    raise ValueError("A backed up round directory is missing")
                conn.execute("UPDATE log_rounds SET dir_path=?,indexed_at=NULL WHERE round_uid=?", (str(destination / relative), uid))
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "log_index_checkpoints" in tables:
                conn.execute("DELETE FROM log_index_checkpoints")
            if "log_sources" in tables:
                conn.execute(
                    "UPDATE log_sources SET status='uninitialized',last_success_at=NULL,pending_bytes=0 "
                    "WHERE status!='disabled'"
                )
            if "log_jobs" in tables:
                conn.execute("UPDATE log_jobs SET state='queued',heartbeat=NULL WHERE state='running'")
        overrides = {"HILLEYE_DB_PATH": str(destination / "tracker.db"),
                     "HILLEYE_LOGS_DB_PATH": str(destination / "logs.db"),
                     "HILLEYE_LOGS_UPLOADS_DIR": str(destination / "uploaded_logs"),
                     "HILLEYE_MAPS_CACHE_DIR": str(destination / "map_cache")}
        overrides["HILLEYE_LOGS_DIR"] = str(destination / "raw_logs") if manifest["has_raw_logs"] else ""
        (stage / "restore-config.json").write_text(json.dumps(overrides, indent=2), encoding="utf-8")
        if destination.exists():
            destination.rmdir()
        ready = stage / "ready"
        ready.mkdir(mode=0o700)
        for item in list(stage.iterdir()):
            if item != ready:
                item.rename(ready / item.name)
        ready.rename(destination)
    return overrides


def prune_logs(config, *, apply=False, optimize_index=False):
    """Report retention candidates and optionally remove them under the logs writer locks."""
    from core.logs_db.uploads import _delete_upload
    from core.map_renderer.renderer import expired_map_versions

    path = config.resolved_logs_db_path()
    if path == ":memory:" or not Path(path).is_file():
        raise ValueError("Logs database is unavailable")
    root = config.resolved_logs_uploads_dir().resolve()
    map_root = config.resolved_maps_cache_dir().resolve()
    cutoffs = (
        ("uploads", config.logs_upload_retention_days, "log_rounds", "round_uid",
         "source_type='upload' AND import_batch IS NULL AND uploaded_at < ? "
         "AND NOT EXISTS (SELECT 1 FROM log_celadon_aliases WHERE old_uid=log_rounds.round_uid) "
         "AND NOT EXISTS (SELECT 1 FROM log_celadon_rounds WHERE round_uid=log_rounds.round_uid AND NOT present)"),
        ("audit_events", config.logs_audit_retention_days, "log_audit_events", "id", "created_at < ?"),
        ("jobs", config.logs_job_retention_days, "log_jobs", "id",
         "state IN ('succeeded','partial','failed') AND finished_at < ?"),
    )
    now = time.time()
    result = {"applied": apply, "optimized_index": False, "candidates": {}, "removed": {}}
    with ExitStack() as stack:
        if apply:
            _wait_lock(stack, lock_path(path, "uploads"))
            _wait_lock(stack, lock_path(path, "index-write"))
            if config.maps_retention_days:
                _wait_lock(stack, lock_path(str(map_root / "map-cache"), "map-write"))
        conn = stack.enter_context(closing(sqlite3.connect(
            f"{Path(path).resolve().as_uri()}?mode={'rw' if apply else 'ro'}", uri=True, timeout=15,
        )))
        conn.execute("PRAGMA foreign_keys=ON")
        for label, days, table, key, clause in cutoffs:
            cutoff = now - days * 86400
            count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {clause}", (cutoff,)).fetchone()[0] if days else 0
            result["candidates"][label] = count
            result["removed"][label] = 0
        if apply and result["candidates"]["uploads"] and not root.is_dir():
            raise ValueError("Uploads directory is unavailable")
        expired_maps = []
        if config.maps_retention_days:
            revisions = [row[0] for row in conn.execute(
                "SELECT DISTINCT map_revision FROM log_rounds WHERE map_revision!=''",
            )]
            expired_maps = expired_map_versions(map_root, now - config.maps_retention_days * 86400, revisions)
        result["candidates"]["map_versions"] = len(expired_maps)
        result["removed"]["map_versions"] = 0
        if apply:
            for label, days, table, key, clause in cutoffs:
                if not days:
                    continue
                cutoff = now - days * 86400
                while True:
                    rows = conn.execute(
                        f"SELECT {key} FROM {table} WHERE {clause} ORDER BY {key} LIMIT 250", (cutoff,),
                    ).fetchall()
                    if not rows:
                        break
                    if label == "uploads":
                        for (uid,) in rows:
                            result["removed"][label] += int(_delete_upload(conn, uid, root))
                    else:
                        with conn:
                            conn.executemany(f"DELETE FROM {table} WHERE {key}=?", rows)
                        result["removed"][label] += len(rows)
            if optimize_index:
                with conn:
                    conn.execute("INSERT INTO log_entries_fts(log_entries_fts) VALUES ('optimize')")
                result["optimized_index"] = True
            for folder in expired_maps:
                if folder.resolve() != folder or folder.parent != map_root / "versions":
                    raise ValueError("Map version is outside the configured cache")
                shutil.rmtree(folder)
                result["removed"]["map_versions"] += 1
        result["sqlite_page_bytes"] = conn.execute("PRAGMA page_size").fetchone()[0]
        result["sqlite_free_pages"] = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--destination")
    restore = commands.add_parser("restore")
    restore.add_argument("archive")
    restore.add_argument("--destination", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("archive")
    prune = commands.add_parser("prune-logs")
    prune.add_argument("--apply", action="store_true")
    prune.add_argument("--optimize-index", action="store_true")
    args = parser.parse_args()
    if args.command == "backup":
        print(create_backup(load_config(args.config), args.destination))
    elif args.command == "restore":
        print(json.dumps(restore_backup(args.archive, args.destination), indent=2))
    elif args.command == "prune-logs":
        if args.optimize_index and not args.apply:
            parser.error("--optimize-index requires --apply")
        print(json.dumps(prune_logs(load_config(args.config), apply=args.apply, optimize_index=args.optimize_index), indent=2))
    else:
        verify_archive(args.archive)
        print("Backup verified")


if __name__ == "__main__":
    main()
