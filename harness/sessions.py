"""Bounded Redis session documents. No command queues, recovery or alternate store."""
from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import redis.asyncio as redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError, OutOfMemoryError, AuthenticationError, NoPermissionError
from redis.asyncio.retry import Retry

from .store import RUN_ID, utc_now
from .files import digest

SESSION_ID = re.compile(r"[0-9a-f]{32}")
PREFIX = "harness:sessions:"
TERMINAL = {"completed", "unverified", "failed", "cancelled", "budget_exceeded", "verification_failed", "interrupted"}
CAS = """
local previous = redis.call('GET', KEYS[1])
if ARGV[1] == '' then
  if previous then return 0 end
elseif not previous then return -1
elseif previous ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
return 1
"""


class SessionError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def validate_group(messages):
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
        raise SessionError("session_data_corrupt")
    assistant = messages[0]
    if (set(assistant) - {"role", "content", "tool_calls"} or assistant.get("role") != "assistant"
            or not isinstance(assistant.get("content"), (str, type(None)))):
        raise SessionError("session_data_corrupt")
    calls = assistant.get("tool_calls", [])
    if not isinstance(calls, list):
        raise SessionError("session_data_corrupt")
    if not calls and (not assistant.get("content") or not assistant["content"].strip()):
        raise SessionError("session_data_corrupt")
    identifiers = []
    for call in calls:
        if (not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]
                or call.get("type") != "function" or not isinstance(call.get("function"), dict)
                or not isinstance(call["function"].get("name"), str)
                or not isinstance(call["function"].get("arguments"), str)):
            raise SessionError("session_data_corrupt")
        identifiers.append(call["id"])
    if len(identifiers) != len(set(identifiers)) or len(messages) != len(identifiers) + 1:
        raise SessionError("session_data_corrupt")
    for message, identifier in zip(messages[1:], identifiers):
        if (not isinstance(message, dict) or message.get("role") != "tool"
                or message.get("tool_call_id") != identifier or not isinstance(message.get("content"), str)):
            raise SessionError("session_data_corrupt")
        try:
            result = json.loads(message["content"])
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise SessionError("session_data_corrupt") from None
    return identifiers


def valid_pending(items):
    return (isinstance(items, list) and all(isinstance(item, dict)
        and set(item) == {"call_id", "name", "outcome"} and isinstance(item["call_id"], str)
        and bool(item["call_id"]) and isinstance(item["name"], str) and bool(item["name"])
        and item["outcome"] == "unknown" for item in items)
        and len({item["call_id"] for item in items}) == len(items))


class Sessions:
    def __init__(self, config):
        self.config = config
        self.password = os.environ.get("HARNESS_REDIS_PASSWORD", "")
        self.client = redis.Redis(host="127.0.0.1", port=config.redis_port, username="harness", password=self.password,
                                  decode_responses=False, socket_connect_timeout=2, socket_timeout=2,
                                  retry=Retry(NoBackoff(), 0), max_connections=4)

    async def close(self):
        await self.client.aclose()

    async def command(self, name, *args, **kwargs):
        if not self.password:
            raise SessionError("redis_password_missing")
        try:
            return await getattr(self.client, name)(*args, **kwargs)
        except OutOfMemoryError:
            raise SessionError("redis_capacity_exceeded") from None
        except AuthenticationError:
            raise SessionError("redis_authentication_failed") from None
        except NoPermissionError:
            raise SessionError("redis_permission_denied") from None
        except RedisError:
            raise SessionError("redis_unavailable") from None

    @staticmethod
    def key(identifier):
        if not isinstance(identifier, str) or not SESSION_ID.fullmatch(identifier):
            raise SessionError("invalid_session_id")
        return PREFIX + identifier

    def decode(self, raw, identifier):
        try:
            if len(raw) > self.config.max_session_bytes:
                raise ValueError
            doc = json.loads(raw)
            if (not isinstance(doc, dict) or set(doc) != {"version", "session_id", "project", "created_at", "updated_at", "expires_at", "revision", "groups", "state", "last_run", "interruptions", "summary", "requests"}
                    or type(doc["version"]) is not int or doc["version"] != 1 or doc["session_id"] != identifier
                    or not isinstance(doc["project"], str) or not doc["project"]
                    or type(doc["revision"]) is not int or doc["revision"] < 0
                    or not isinstance(doc["groups"], list) or len(doc["groups"]) > self.config.max_session_groups
                    or not isinstance(doc["interruptions"], list) or not isinstance(doc["requests"], dict)
                    or doc["summary"] != {"version": 0, "covered_group_seq": 0, "text": ""}):
                raise ValueError
            for request_id, request in doc["requests"].items():
                if (not SESSION_ID.fullmatch(request_id) or not isinstance(request, dict)
                        or set(request) != {"run_id", "signature"} or not RUN_ID.fullmatch(request["run_id"])
                        or not re.fullmatch(r"[0-9a-f]{64}", request["signature"])):
                    raise ValueError
            for field in ("created_at", "updated_at", "expires_at"):
                if datetime.fromisoformat(doc[field]).tzinfo is None:
                    raise ValueError
            if doc["state"] is not None and (not isinstance(doc["state"], dict)
                    or set(doc["state"]) != {"plan", "memory"} or not isinstance(doc["state"]["plan"], list)
                    or not isinstance(doc["state"]["memory"], dict)):
                raise ValueError
            for interruption in doc["interruptions"]:
                if (not isinstance(interruption, dict) or set(interruption) != {"run_id", "status", "pending", "note"}
                        or not RUN_ID.fullmatch(interruption["run_id"]) or interruption["status"] not in TERMINAL
                        or not valid_pending(interruption["pending"]) or not isinstance(interruption["note"], str)):
                    raise ValueError
            last = doc["last_run"]
            if last is not None and (not isinstance(last, dict) or set(last) != {"run_id", "owner", "task", "status", "started_at", "pending"}
                    or not RUN_ID.fullmatch(last["run_id"]) or not isinstance(last["owner"], str)
                    or not isinstance(last["task"], str) or last["status"] not in TERMINAL | {"running"}
                    or not valid_pending(last["pending"])):
                raise ValueError
            for seq, group in enumerate(doc["groups"], 1):
                if (not isinstance(group, dict) or set(group) != {"session_id", "run_id", "group_seq", "task", "messages", "call_ids", "complete"}
                        or group["session_id"] != identifier or not RUN_ID.fullmatch(group["run_id"])
                        or type(group["group_seq"]) is not int or group["group_seq"] != seq
                        or group["complete"] is not True or not isinstance(group["task"], str)
                        or validate_group(group["messages"]) != group["call_ids"]):
                    raise ValueError
            return doc
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            raise SessionError("session_data_corrupt") from None

    async def load(self, identifier):
        raw = await self.command("get", self.key(identifier))
        if raw is None:
            raise SessionError("session_missing_or_expired")
        return self.decode(raw, identifier), raw

    async def write(self, previous, doc):
        updated = copy.deepcopy(doc)
        now = datetime.now(timezone.utc)
        updated.update(updated_at=now.isoformat(), expires_at=(now + timedelta(seconds=self.config.session_ttl_seconds)).isoformat(),
                       revision=doc["revision"] + (1 if previous is not None else 0))
        raw = json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > self.config.max_session_bytes:
            raise SessionError("session_byte_limit")
        self.decode(raw, updated["session_id"])
        result = await self.command("eval", CAS, 1, self.key(updated["session_id"]), previous or b"", raw,
                                    int(self.config.session_ttl_seconds * 1000))
        if result != 1:
            raise SessionError("session_missing_or_expired" if result == -1 else "session_update_conflict")
        return updated

    async def create(self, project):
        identifier = uuid4().hex
        doc = {"version": 1, "session_id": identifier, "project": project, "created_at": utc_now(),
               "updated_at": utc_now(), "expires_at": utc_now(), "revision": 0, "groups": [], "state": None,
               "last_run": None, "interruptions": [], "requests": {}, "summary": {"version": 0, "covered_group_seq": 0, "text": ""}}
        return await self.write(None, doc)

    @staticmethod
    def public(doc):
        return {key: doc[key] for key in ("session_id", "project", "created_at", "updated_at", "expires_at", "revision")} | {
            "group_count": len(doc["groups"]), "last_run": doc["last_run"], "interruptions": doc["interruptions"]}

    async def begin(self, identifier, project, run_id, task, owner, request_id, *, previous_run_finished=False, request_signature=None):
        doc, raw = await self.load(identifier)
        if doc["project"] != project:
            raise SessionError("session_project_mismatch")
        if request_id in doc["requests"]:
            raise SessionError("session_request_duplicate")
        if len(doc["groups"]) >= self.config.max_session_groups:
            raise SessionError("session_group_limit")
        last = doc["last_run"]
        if last and last["status"] == "running":
            if last["owner"] == owner and not previous_run_finished:
                raise SessionError("session_run_active")
            last["status"] = "interrupted"
        if last and (last["pending"] or last["status"] == "interrupted"):
            doc["interruptions"].append({"run_id": last["run_id"], "status": last["status"],
                "pending": last["pending"], "note": "Incomplete interaction; outcomes unknown. Never replay or infer success."})
        history = copy.deepcopy(doc)
        doc["requests"][request_id] = {"run_id": run_id, "signature": request_signature or digest(task.encode("utf-8"))}
        doc["last_run"] = {"run_id": run_id, "owner": owner, "task": task, "status": "running", "started_at": utc_now(), "pending": []}
        doc = await self.write(raw, doc)
        return doc, history

    async def update(self, identifier, run_id, *, pending=None, messages=None, group_seq=None, state=None, status=None):
        doc, raw = await self.load(identifier)
        last = doc["last_run"]
        if not last or last["run_id"] != run_id:
            raise SessionError("session_run_mismatch")
        if messages is not None:
            call_ids = validate_group(messages)
            group = {"session_id": identifier, "run_id": run_id, "group_seq": group_seq, "task": last["task"],
                     "messages": messages, "call_ids": call_ids, "complete": True}
            if type(group_seq) is not int or group_seq < 1:
                raise SessionError("session_group_sequence")
            if group_seq <= len(doc["groups"]):
                if doc["groups"][group_seq - 1] == group:
                    return doc, True
                raise SessionError("session_group_conflict")
            if group_seq != len(doc["groups"]) + 1:
                raise SessionError("session_group_sequence")
            if len(doc["groups"]) >= self.config.max_session_groups:
                raise SessionError("session_group_limit")
            if last["status"] != "running":
                raise SessionError("session_run_not_active")
            doc["groups"].append(group)
            doc["state"], last["pending"] = state, []
        if pending is not None:
            if last["status"] != "running":
                raise SessionError("session_run_not_active")
            last["pending"] = pending
        if status is not None:
            if status not in TERMINAL:
                raise SessionError("invalid_session_run_status")
            last["status"] = status
            if state is not None:
                doc["state"] = state
        return await self.write(raw, doc), False
