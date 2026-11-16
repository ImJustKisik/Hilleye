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
import tarfile
import tempfile
import time
import uuid

from core.config import DEFAULT_CONFIG_PATH, load_config
from core.file_lock import FileLock, lock_path


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
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
        remaining = os.fstat(src.fileno()).st_size
        while remaining:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"Source was truncated during backup: {source.name}")
            dst.write(chunk)
            remaining -= len(chunk)


def _copy_tree(source, target):
    if not source.is_dir():
        raise ValueError(f"Backup source is unavailable: {source}")
    def fail(exc):
        raise exc
    for directory, dirs, files in os.walk(source, onerror=fail):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        for name in dirs + files:
            if (Path(directory) / name).is_symlink():
                raise ValueError("Backup sources must not contain symlinks")
        for name in files:
            original = Path(directory) / name
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
        names = [item.name for item in members]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate backup members")
        for member in members:
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts or "\\" in member.name or ":" in member.name:
                raise ValueError("Unsafe backup member")
        meta_member = stream.getmember("manifest.json")
        if meta_member.size > 16 * 1024 * 1024:
            raise ValueError("Backup manifest is too large")
        manifest = json.load(stream.extractfile(meta_member))
        if manifest.get("format") != 1 or set(names) != {"manifest.json", *manifest["files"]}:
            raise ValueError("Backup manifest does not match its contents")
        for name, expected in manifest["files"].items():
            member = stream.getmember(name)
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
                _sqlite_check(stage / label)
            uploads = config.resolved_logs_uploads_dir()
            if uploads.is_dir():
                _copy_tree(uploads, stage / "uploaded_logs")
            log_root = config.resolved_logs_root_dir()
            if log_root is not None:
                _copy_tree(log_root, stage / "raw_logs")
            with closing(sqlite3.connect(stage / "logs.db")) as conn:
                conn.row_factory = sqlite3.Row
                round_paths = {}
                for row in conn.execute("SELECT round_uid,source_type,dir_path FROM log_rounds"):
                    source_root = uploads if row["source_type"] == "upload" else log_root
                    if source_root is None:
                        raise ValueError("An indexed source has no configured backup root")
                    relative = Path(row["dir_path"]).resolve().relative_to(source_root.resolve())
                    prefix = "uploaded_logs" if row["source_type"] == "upload" else "raw_logs"
                    round_paths[row["round_uid"]] = (Path(prefix) / relative).as_posix()
                    if not (stage / prefix / relative).is_dir():
                        raise ValueError("An indexed round is missing from the copied source; backup was not published")
            created = time.time()
            manifest = {"format": 1, "created_at": created, "has_raw_logs": log_root is not None,
                        "round_paths": round_paths, "files": {}}
            for path in stage.rglob("*"):
                if path.is_file():
                    manifest["files"][path.relative_to(stage).as_posix()] = {"size": path.stat().st_size, "sha256": _hash(path)}
            (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        archive = stage / "archive.partial"
        with tarfile.open(archive, "w:gz") as stream:
            for name in [*manifest["files"], "manifest.json"]:
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
            for name in manifest["files"]:
                target = (stage / name).resolve()
                if not target.is_relative_to(stage.resolve()):
                    raise ValueError("Unsafe restore path")
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(name) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
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
            if "log_jobs" in tables:
                conn.execute("UPDATE log_jobs SET state='queued',heartbeat=NULL WHERE state='running'")
        overrides = {"HILLEYE_DB_PATH": str(destination / "tracker.db"),
                     "HILLEYE_LOGS_DB_PATH": str(destination / "logs.db"),
                     "HILLEYE_LOGS_UPLOADS_DIR": str(destination / "uploaded_logs"),
                     "HILLEYE_MAPS_CACHE_DIR": str(destination / "map_cache")}
        if manifest["has_raw_logs"]:
            overrides["HILLEYE_LOGS_DIR"] = str(destination / "raw_logs")
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
    args = parser.parse_args()
    if args.command == "backup":
        print(create_backup(load_config(args.config), args.destination))
    elif args.command == "restore":
        print(json.dumps(restore_backup(args.archive, args.destination), indent=2))
    else:
        verify_archive(args.archive)
        print("Backup verified")


if __name__ == "__main__":
    main()
