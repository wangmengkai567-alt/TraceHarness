"""A deliberately narrow static allow set; complex shell syntax requires approval."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

from .files import FileError
from .workspace import ordinary

VERSION = "1"
BINARIES = {name: "/usr/bin/" + name for name in ("pwd", "ls", "cat", "head", "wc")}


def classify(command, root):
    decision = {"decision": "require_approval", "reason": "unrecognized_or_effectful_command", "rule_version": VERSION}
    # Never interpret complex syntax as a trusted simple command.
    if not re.fullmatch(r"[A-Za-z0-9_./ '" + '"' + r"\-]+", command):
        return decision
    try:
        words = shlex.split(command)
        if not words:
            return {**decision, "decision": "deny", "reason": "empty_command"}
        name = words[0]
        if name not in BINARIES and name not in BINARIES.values():
            return decision
        name = name.rsplit("/", 1)[-1]
        args = words[1:]
        paths = []
        if name == "pwd":
            if args:
                return decision
        elif name == "head":
            if len(args) < 3 or args[0] != "-n" or not args[1].isdigit() or not 1 <= int(args[1]) <= 200:
                return decision
            paths = args[2:]
        else:
            flags = {"ls": {"-a", "-l", "-h"}, "wc": {"-l", "-w", "-c"}, "cat": set()}[name]
            for arg in args:
                if arg.startswith("-"):
                    if arg not in flags or paths:
                        return decision
                else:
                    paths.append(arg)
            if name in {"cat", "wc"} and not paths:
                return decision
        for value in paths:
            if value == "." and name == "ls":
                continue
            path = ordinary(Path(root), value)
            if name != "ls" and not path.is_file():
                return decision
        # Execute a trusted absolute binary; a workspace program called ls is never trusted.
        return {"decision": "allow", "reason": "static_read_only", "rule_version": VERSION,
                "argv": [BINARIES[name], *args]}
    except (ValueError, FileError, OSError):
        return {**decision, "decision": "deny", "reason": "invalid_or_unauthorized_path"}
