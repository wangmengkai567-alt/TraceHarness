"""Explicit input copies and bounded, byte-based end-of-run manifests."""
from __future__ import annotations

import os
import stat
import time
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath

from .files import FileError, digest

INPUT_FILES = 1000
INPUT_BYTES = 64 * 1024 * 1024
SCAN_FILES = 10000
SCAN_BYTES = 256 * 1024 * 1024
SCAN_DEPTH = 32
SCAN_SECONDS = 10
EXCLUDED = {".git", ".venv", "runs", "__pycache__"}


def relative_name(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or "\0" in value or value.startswith("/")
            or any(ord(c) < 32 for c in value)
            or any(p in {"", ".", ".."} or p.endswith((".", " ")) or PureWindowsPath(p).is_reserved()
                   for p in value.split("/"))):
        raise FileError("invalid_input_path")
    return PurePosixPath(value).as_posix()


def ordinary(root, name):
    """Reject every link component, junction, special file and hard-link alias."""
    name = relative_name(name)
    path = root / name
    for part in (path, *path.parents):
        if part == root:
            break
        try:
            info = part.lstat()
        except FileNotFoundError:
            raise FileError("input_file_missing") from None
        if stat.S_ISLNK(info.st_mode) or (os.name == "nt" and info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise FileError("workspace_link_rejected")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise FileError("workspace_special_file")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise FileError("workspace_hardlink_rejected")
    if not path.resolve().is_relative_to(root.resolve()):
        raise FileError("path_outside_workspace")
    return path


def input_names(source, names):
    if not isinstance(names, list) or len(names) > INPUT_FILES:
        raise FileError("input_files_must_be_explicit_list")
    result = []
    total = 0
    for name in names:
        name = relative_name(name)
        if name.casefold() == "agents.md":
            name = "AGENTS.md"
        if any(p.casefold() in EXCLUDED or p.casefold().startswith(".env") for p in name.split("/")):
            raise FileError("input_file_excluded")
        if name in result or any(n.casefold() == name.casefold() for n in result):
            raise FileError("duplicate_input_file")
        result.append(name)
    if (source / "AGENTS.md").exists() or (source / "AGENTS.md").is_symlink():
        if "AGENTS.md" not in result:
            result.append("AGENTS.md")
    if len(result) > INPUT_FILES:
        raise FileError("input_file_limit")
    for name in result:
        path = ordinary(source, name)
        if not path.is_file():
            raise FileError("input_must_be_regular_file")
        total += path.stat().st_size
        if total > INPUT_BYTES:
            raise FileError("input_byte_limit")
    return result


class Workspace:
    def __init__(self, source, names, store):
        self.store = store
        self.source = source
        self.root = store.directory / "workspace"
        self.root.mkdir()
        self.baseline = {}
        self.rules = None
        total = 0
        for name in input_names(source, names):
            path = ordinary(source, name)
            with path.open("rb") as stream:
                raw = stream.read(INPUT_BYTES - total + 1)
            total += len(raw)
            if total > INPUT_BYTES:
                raise FileError("input_byte_limit")
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            self.baseline[name] = {"sha256": digest(raw), "bytes": len(raw)}
            if name == "AGENTS.md":
                self.rules = store.directory / "rules.frozen"
                self.rules.write_bytes(raw)
        self.manifest = {"source": str(source), "workspace": str(self.root),
                         "inputs": self.baseline, "changes": [], "complete": False,
                         "errors": ["run_not_finished"]}
        self.store.write_json("workspace_manifest.json", self.manifest)
        self.store.emit("workspace_prepared", **self.manifest)

    def collect(self, stopped=True):
        seen, changes, errors = {}, [], []
        started, total, count = time.monotonic(), 0, 0
        if not stopped:
            errors.append("process_termination_unconfirmed")
        else:
            try:
                pending = [(self.root, 0)]
                while pending:
                    directory, depth = pending.pop()
                    if depth > SCAN_DEPTH:
                        raise FileError("scan_depth_limit")
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            count += 1
                            if count > SCAN_FILES or time.monotonic() - started > SCAN_SECONDS:
                                raise FileError("scan_limit")
                            name = Path(entry.path).relative_to(self.root).as_posix()
                            path = ordinary(self.root, name)
                            if path.is_dir():
                                pending.append((path, depth + 1))
                                continue
                            size = path.stat().st_size
                            if total + size > SCAN_BYTES:
                                raise FileError("scan_byte_limit")
                            # Stream hashing keeps the scan memory bounded even for large outputs.
                            h = hashlib.sha256()
                            size = 0
                            with path.open("rb") as stream:
                                while chunk := stream.read(65536):
                                    size += len(chunk)
                                    total += len(chunk)
                                    if total > SCAN_BYTES or time.monotonic() - started > SCAN_SECONDS:
                                        raise FileError("scan_limit")
                                    h.update(chunk)
                            seen[name] = {"sha256": h.hexdigest(), "bytes": size}
            except (OSError, FileError) as error:
                errors.append(str(error) if isinstance(error, FileError) else "scan_access_error")
        for name, value in seen.items():
            old = self.baseline.get(name)
            if old != value:
                changes.append({"path": name, "change": "modified" if old else "added",
                                "before_sha256": old["sha256"] if old else None,
                                "sha256": value["sha256"], "bytes": value["bytes"]})
        if not errors:
            for name, old in self.baseline.items():
                if name not in seen:
                    changes.append({"path": name, "change": "deleted", "before_sha256": old["sha256"],
                                    "sha256": None, "bytes": None})
        self.manifest.update(changes=changes, complete=not errors, errors=errors)
        self.store.write_json("workspace_manifest.json", self.manifest)
        self.store.emit("workspace_collected", **self.manifest)
        return self.manifest
