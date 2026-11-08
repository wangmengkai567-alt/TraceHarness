"""User-managed SQLite entries and bounded retrieval; no model write capability."""
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .store import utc_now


class MemoryError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def project_scope(workspace):
    if not isinstance(workspace, str) or not workspace.strip():
        raise MemoryError("memory_invalid_project")
    try:
        path = Path(workspace).resolve(strict=True)
        if not path.is_dir():
            raise ValueError
        return os.path.normcase(str(path))
    except (OSError, ValueError, RuntimeError):
        raise MemoryError("memory_invalid_project") from None


def validate_refs(refs):
    if (not isinstance(refs, list) or len(refs) > 500 or any(
            not isinstance(ref, dict) or set(ref) != {"id", "version", "scope"}
            or not isinstance(ref["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", ref["id"])
            or type(ref["version"]) is not int or ref["version"] < 1
            or not isinstance(ref["scope"], str) or not ref["scope"] for ref in refs)
            or len({ref["id"] for ref in refs}) != len(refs)):
        raise MemoryError("memory_invalid_references")


class Memory:
    def __init__(self, config, secrets=()):
        self.path = Path(config.memory_db_path).resolve()
        self.limit, self.max_chars = config.memory_retrieval_limit, config.max_memory_chars
        self.secrets = tuple(value for value in secrets if value) + tuple(
            value for key, value in os.environ.items() if value and (
                key.endswith(("_KEY", "_TOKEN", "_PASSWORD", "_SECRET"))))

    @contextmanager
    def database(self):
        connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=1)
            connection.row_factory = sqlite3.Row
            connection.execute("""CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY, topic TEXT NOT NULL, content TEXT NOT NULL,
                kind TEXT NOT NULL, source TEXT NOT NULL, scope TEXT NOT NULL,
                version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                valid_until TEXT, deleted INTEGER NOT NULL, audit TEXT NOT NULL)""")
            connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS active_topics
                ON entries(scope, topic) WHERE deleted = 0""")
            with connection:
                yield connection
        except sqlite3.IntegrityError:
            raise MemoryError("memory_topic_conflict") from None
        except (sqlite3.Error, OSError):
            raise MemoryError("memory_unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    def text(self, value, maximum):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
            raise MemoryError("memory_invalid_text")
        if any(secret in value for secret in self.secrets) or re.search(
                r"(?i)(?:api[_ -]?key|password|passwd|secret|token|密码|密钥)\s*[:=：]\s*\S+"
                r"|\bBearer\s+\S+|-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{12,}"
                r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
                r"|[a-z]+://[^\s/@]+:[^\s/@]+@", value):
            raise MemoryError("memory_credentials_forbidden")
        return value.strip()

    @staticmethod
    def scope(body):
        project = project_scope(body.get("workspace"))
        scope = body.get("scope")
        if not isinstance(scope, str) or scope not in {"project", "global"}:
            raise MemoryError("memory_invalid_scope")
        if body.get("global_authorized") is not (scope == "global"):
            raise MemoryError("memory_explicit_scope_required")
        return project if scope == "project" else "global"

    def values(self, body):
        scope = self.scope(body)
        kind = body.get("kind")
        if not isinstance(kind, str) or kind not in {"fact", "preference"} or scope == "global" and kind != "preference":
            raise MemoryError("memory_invalid_kind")
        until = body.get("valid_until")
        if until is not None:
            try:
                parsed = datetime.fromisoformat(until)
                if parsed.tzinfo is None or parsed <= datetime.fromisoformat(utc_now()):
                    raise ValueError
                until = parsed.isoformat()
            except (ValueError, TypeError):
                raise MemoryError("memory_invalid_expiry") from None
        return {"topic": self.text(body.get("topic"), 120),
                "content": self.text(body.get("content"), 2000), "kind": kind,
                "source": self.text(body.get("source"), 300), "scope": scope, "valid_until": until}

    @staticmethod
    def public(row):
        result = dict(row)
        result["audit"] = json.loads(result["audit"])[-1:]
        return result

    def list(self, body):
        if set(body) != {"workspace", "scope", "global_authorized"}:
            raise MemoryError("memory_unknown_fields")
        scope = self.scope(body)
        with self.database() as db:
            return [self.public(row) for row in db.execute(
                "SELECT * FROM entries WHERE scope = ? AND deleted = 0 ORDER BY updated_at DESC LIMIT 500", (scope,))]

    def mutate(self, action, body):
        fields = {"workspace", "scope", "global_authorized"}
        if action in {"create", "update"}:
            fields |= {"topic", "content", "kind", "source", "valid_until"}
        if action in {"update", "delete"}:
            fields |= {"id", "version"}
        if action not in {"create", "update", "delete"} or set(body) != fields:
            raise MemoryError("memory_unknown_fields")
        scope = self.scope(body)
        values = self.values(body) if action != "delete" else {}
        identifier, version = body.get("id"), body.get("version")
        if action != "create":
            validate_refs([{"id": identifier, "version": version, "scope": scope}])
        now = utc_now()
        with self.database() as db:
            # Serialize capacity checks, conflict checks and mutation in one transaction.
            db.execute("BEGIN IMMEDIATE")
            if action == "create":
                if db.execute("SELECT count(*) FROM entries WHERE deleted = 0").fetchone()[0] >= 500:
                    raise MemoryError("memory_entry_limit")
                identifier, version, created, audit = uuid4().hex, 0, now, []
            else:
                row = db.execute("SELECT * FROM entries WHERE id = ? AND scope = ? AND deleted = 0",
                                 (identifier, scope)).fetchone()
                if row is None:
                    raise MemoryError("memory_not_found")
                if row["version"] != version:
                    raise MemoryError("memory_version_conflict")
                created, audit = row["created_at"], json.loads(row["audit"])
            event = {"action": action, "authorization": "direct_user_page", "operation_id": uuid4().hex,
                     "at": now, "scope": scope, "version": version + 1, "values": values}
            audit.append(event)
            # Reserve a final deletion event, so reaching the history limit cannot make an entry undeletable.
            bounded_audit = audit if action == "delete" else audit + [
                {**event, "action": "delete", "version": version + 2, "values": {}}]
            if len(json.dumps(bounded_audit, ensure_ascii=False)) > 65536:
                raise MemoryError("memory_audit_limit")
            if action == "create":
                db.execute("INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (identifier, values["topic"], values["content"], values["kind"], values["source"], scope,
                     1, created, now, values["valid_until"], 0, json.dumps(audit, ensure_ascii=False)))
            elif action == "update":
                db.execute("""UPDATE entries SET topic=?, content=?, kind=?, source=?, version=?,
                    updated_at=?, valid_until=?, audit=? WHERE id=? AND scope=? AND version=?""",
                    (values["topic"], values["content"], values["kind"], values["source"], version + 1,
                     now, values["valid_until"], json.dumps(audit, ensure_ascii=False), identifier, scope, version))
            else:
                db.execute("UPDATE entries SET deleted=1, version=?, updated_at=?, audit=? WHERE id=? AND scope=? AND version=?",
                           (version + 1, now, json.dumps(audit, ensure_ascii=False), identifier, scope, version))
            return self.public(db.execute("SELECT * FROM entries WHERE id=?", (identifier,)).fetchone())

    @staticmethod
    def alive(row):
        return not row["deleted"] and (row["valid_until"] is None or
            datetime.fromisoformat(row["valid_until"]) > datetime.fromisoformat(utc_now()))

    def current(self, refs, project):
        validate_refs(refs)
        with self.database() as db:
            for ref in refs:
                if ref["scope"] not in {project, "global"}:
                    return False
                row = db.execute("SELECT * FROM entries WHERE id=? AND scope=? AND version=?",
                                 (ref["id"], ref["scope"], ref["version"])).fetchone()
                if row is None or not self.alive(row):
                    return False
        return True

    def retrieve(self, project, task):
        words = re.findall(r"[a-z0-9_-]{2,}|[\u4e00-\u9fff]+", task.lower())
        keywords = list(dict.fromkeys(part for word in words for part in (
            [word] if re.fullmatch(r"[a-z0-9_-]+", word) else [word[i:i+2] for i in range(len(word)-1)])))[:32]
        with self.database() as db:
            rows = db.execute("SELECT * FROM entries WHERE scope IN (?, 'global') AND deleted=0", (project,)).fetchall()
        ranked = []
        for row in rows:
            if not self.alive(row):
                continue
            score = sum(3 * (word in row["topic"].lower()) + (word in row["content"].lower()) for word in keywords)
            if score:
                ranked.append((score, row))
        result = []
        for _, row in sorted(ranked, key=lambda pair: (-pair[0], pair[1]["id"])):
            entry = {key: row[key] for key in ("id", "topic", "content", "kind", "source", "scope",
                                              "version", "created_at", "updated_at", "valid_until")}
            if len(json.dumps(result + [entry], ensure_ascii=False)) <= self.max_chars:
                result.append(entry)
            if len(result) >= self.limit:
                break
        return result

    def filter_history(self, history, project):
        """Keep raw Redis evidence; hide runs with stale direct or inherited dependencies."""
        view = copy.deepcopy(history)
        stale = [run_id for run_id, refs in view["memory_dependencies"].items() if not self.current(refs, project)]
        # Later groups may derive facts from earlier groups even without direct memory injection.
        first = next((group["group_seq"] for group in view["groups"] if group["run_id"] in stale), None)
        if first is not None:
            for group in view["groups"]:
                if group["run_id"] not in stale:
                    continue
                group["task"] = "Historical input invalidated after a saved-memory change."
                group["messages"] = [{"role": "assistant", "content": "Historical derived content withheld; never replay operations."}]
                group["call_ids"] = []
            if set(stale) - set(view["memory_invalidated_runs"]):
                view["state"] = None
            if view["last_run"] and view["last_run"]["run_id"] in stale:
                view["last_run"]["task"] = "Historical task withheld after memory invalidation."
        newly_stale = set(stale) - set(view["memory_invalidated_runs"])
        for run_id in stale:
            del view["memory_dependencies"][run_id]
        return view, sorted(newly_stale)

    @staticmethod
    def dependencies(refs, history):
        combined = {ref["id"]: ref for ref in refs}
        if history:
            for group in history["groups"]:
                for ref in history["memory_dependencies"].get(group["run_id"], []):
                    combined[ref["id"]] = ref
        result = list(combined.values())
        validate_refs(result)
        return result
