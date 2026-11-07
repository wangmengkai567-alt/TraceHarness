"""One owned container per command. Never executes model commands on the host."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from .processes import run_process, TerminationUnconfirmed
from .store import utc_now

IMAGE = "harness-sandbox:1"
DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe" if os.name == "nt" else "/usr/bin/docker"
PROJECT = "agent-harness"
VERSION = "1"


class SandboxError(Exception):
    pass


class Sandbox:
    def __init__(self, store, workspace, rules=None):
        self.store, self.workspace, self.rules = store, Path(workspace), rules
        self.records = []
        self.image = None
        self.blocked = False

    async def call(self, *args, timeout=30, limit=262144, execution=False):
        result = await run_process([DOCKER, *args], self.store.directory, timeout, limit,
                                   lambda kind, **data: self.store.emit("docker_cli_" + kind, **data))
        if not execution and (result["timed_out"] or result["truncated"]):
            raise SandboxError("docker_cli_timeout_or_output_limit")
        return result

    def save(self, record, state):
        record.update(state=state, updated_at=utc_now())
        self.store.write_json("sandbox.json", {"version": VERSION, "containers": self.records})
        self.store.emit("sandbox_state", **record)

    async def preflight(self):
        if self.blocked:
            raise TerminationUnconfirmed()
        if self.image:
            return self.image
        info = await self.call("version", "--format", "{{.Server.Os}}")
        if info["exit_code"] or info["output"].strip() != "linux":
            raise SandboxError("docker_linux_engine_unavailable")
        info = await self.call("image", "inspect", IMAGE)
        if info["exit_code"]:
            raise SandboxError("sandbox_image_missing")
        image = json.loads(info["output"])[0]
        if (image["Config"].get("Labels") or {}).get("org.harness.sandbox.version") != VERSION:
            raise SandboxError("sandbox_image_untrusted")
        self.image = image["Id"]
        self.store.emit("sandbox_preflight", image=IMAGE, image_id=self.image)
        return self.image

    async def lookup(self, record):
        result = await self.call("container", "ls", "-a", "--no-trunc", "--filter",
                                 "name=^/" + record["name"] + "$", "--format", "{{.ID}}")
        if result["exit_code"]:
            raise SandboxError("docker_inventory_failed")
        ids = result["output"].split()
        if not ids:
            return None
        if len(ids) != 1:
            raise SandboxError("container_identity_ambiguous")
        result = await self.call("inspect", ids[0])
        if result["exit_code"]:
            raise SandboxError("container_inspect_failed")
        info = json.loads(result["output"])[0]
        labels = info["Config"].get("Labels") or {}
        if (info["Name"] != "/" + record["name"] or any(labels.get(k) != v for k, v in record["labels"].items())
                or (record.get("id") and info["Id"] != record["id"])):
            raise SandboxError("container_ownership_mismatch")
        return info

    async def cleanup(self, record):
        try:
            self.save(record, "cleaning")
            info = await self.lookup(record)
            if info:
                result = await self.call("rm", "--force", info["Id"])
                if result["exit_code"]:
                    raise SandboxError("container_remove_failed")
            if await self.lookup(record) is not None:
                raise SandboxError("container_still_present")
            self.save(record, "removed")
        except BaseException:
            self.blocked = True
            self.save(record, "cleanup_failed")
            raise TerminationUnconfirmed() from None

    async def execute(self, spec, output_limit):
        if self.blocked:
            raise TerminationUnconfirmed()
        number = len(self.records) + 1
        name = f"harness-{self.store.run_id}-{os.getpid()}-{number}"
        # RunStore lives under a specific records root: the full directory distinguishes roots.
        labels = {"org.harness.project": PROJECT, "org.harness.run": self.store.run_id,
                  "org.harness.call": spec["call_id"], "org.harness.owner": str(self.store.directory.resolve())}
        record = {"name": name, "labels": labels, "id": None, "spec": spec}
        self.records.append(record)
        self.save(record, "creating")
        args = ["create", "--pull=never", "--name", name, "--network=none", "--read-only",
                "--user", "10001:10001", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--cpus=1", "--memory=512m", "--memory-swap=512m", "--pids-limit=64",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777", "--workdir=/workspace",
                "--mount", f"type=bind,source={self.workspace},target=/workspace"]
        if self.rules:
            args += ["--mount", f"type=bind,source={self.rules},target=/workspace/AGENTS.md,readonly"]
        for key, value in labels.items():
            args += ["--label", key + "=" + value]
        args += [spec["image_id"], *spec["argv"]]
        try:
            created = await self.call(*args)
            if created["exit_code"]:
                raise SandboxError("container_create_failed")
            identifier = created["output"].strip()
            if len(identifier) != 64 or any(c not in "0123456789abcdef" for c in identifier):
                raise SandboxError("invalid_container_id")
            record["id"] = identifier
            self.save(record, "created")
            # Inspect actual daemon configuration before allowing the process to start.
            info = await self.lookup(record)
            host = info["HostConfig"]
            mounts = info["Mounts"]
            if (info["Image"] != spec["image_id"] or info["Config"]["User"] != "10001:10001"
                    or host["NetworkMode"] != "none" or not host["ReadonlyRootfs"]
                    or host["Privileged"] or host["PortBindings"] or host["Devices"]
                    or host["Memory"] != 512 * 1024 * 1024 or host["MemorySwap"] != 512 * 1024 * 1024
                    or host["NanoCpus"] != 1000000000 or host["PidsLimit"] != 64
                    or "ALL" not in host["CapDrop"] or "no-new-privileges" not in host["SecurityOpt"]
                    or len(mounts) != (2 if self.rules else 1)
                    or not any(m["Destination"] == "/workspace" and m["RW"] for m in mounts)
                    or (self.rules and not any(m["Destination"] == "/workspace/AGENTS.md" and not m["RW"] for m in mounts))):
                raise SandboxError("container_restrictions_mismatch")
            self.save(record, "running")
            data = await self.call("start", "--attach", identifier, timeout=spec["timeout_seconds"], limit=output_limit, execution=True)
            inspected = await self.lookup(record)
            if inspected["State"]["Running"] and not data["timed_out"]:
                raise SandboxError("container_unexpectedly_running")
            data["exit_code"] = None if inspected["State"]["Running"] else inspected["State"]["ExitCode"]
            data["oom_killed"] = inspected["State"].get("OOMKilled", False)
            data["container_id"] = identifier
            return data
        finally:
            # Cleanup has its own deadline, independent of task cancellation and execution budget.
            cleanup = asyncio.create_task(self.cleanup(record))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise


async def recover(root):
    """Reconcile only recorded names with all ownership labels. Never resume commands."""
    from .store import RunStore, RUN_ID
    for folder in Path(root).iterdir():
        if (not RUN_ID.fullmatch(folder.name) or folder.is_symlink() or folder.is_junction()
                or not folder.is_dir()):
            continue
        path = folder / "sandbox.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("containers"), list):
            raise TerminationUnconfirmed()
        pending = [record for record in data["containers"] if record["state"] != "removed"]
        if not pending:
            continue
        # Reopen the evidence writer without allocating a new run or replaying its tools.
        store = object.__new__(RunStore)
        store.directory, store.run_id, store.secrets = folder, folder.name, ()
        events = (folder / "events.jsonl").read_bytes()
        sequence = 0
        for line in events.splitlines():
            try:
                sequence = max(sequence, json.loads(line)["sequence"])
            except ValueError:
                continue
        store.sequence = sequence
        sandbox = Sandbox(store, folder / "workspace")
        sandbox.records = data["containers"]
        for record in pending:
            expected = {"org.harness.project": PROJECT, "org.harness.run": folder.name,
                        "org.harness.call": record["spec"]["call_id"], "org.harness.owner": str(folder.resolve())}
            if record["labels"] != expected or not re.fullmatch("harness-" + folder.name + r"-\d+-\d+", record["name"]):
                raise TerminationUnconfirmed()
            if events and not events.endswith(b"\n"):
                with (folder / "events.jsonl").open("ab") as stream:
                    stream.write(b"\n")
                events += b"\n"
            await sandbox.cleanup(record)
