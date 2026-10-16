"""Owned process trees. Windows commands enter a job before any code runs."""
from __future__ import annotations

import asyncio
import os
import signal


class TerminationUnconfirmed(Exception):
    pass


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes as c
        from ctypes import wintypes as w
        k = c.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.OpenProcess(0x1000, False, pid)
        if not handle:
            return c.get_last_error() == 5  # Access denied does not establish process exit.
        try:
            code = w.DWORD()
            return not k.GetExitCodeProcess(handle, c.byref(code)) or code.value == 259
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class WindowsJob:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c = c
        self.k = k = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([c.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Thread32First": ([w.HANDLE, c.c_void_p], w.BOOL),
            "Thread32Next": ([w.HANDLE, c.c_void_p], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(k, name)
            fn.argtypes, fn.restype = args, result

        class Basic(c.Structure):
            _fields_ = [("times", c.c_int64 * 2), ("flags", w.DWORD),
                        ("working", c.c_size_t * 2), ("active", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class Extended(c.Structure):
            _fields_ = [("basic", Basic), ("io", c.c_uint64 * 6), ("memory", c.c_size_t * 4)]

        self.handle = k.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            self.require(self.handle)
            self.require(k.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def require(self, value):
        if not value:
            raise self.c.WinError(self.c.get_last_error())
        return value

    def attach_and_resume(self, pid):
        c, k = self.c, self.k
        process = self.require(k.OpenProcess(0x101, False, pid))
        try:
            self.require(k.AssignProcessToJobObject(self.handle, process))
        finally:
            k.CloseHandle(process)

        class Thread(c.Structure):
            _fields_ = [("size", c.c_ulong), ("usage", c.c_ulong), ("id", c.c_ulong),
                        ("pid", c.c_ulong), ("base", c.c_long), ("delta", c.c_long), ("flags", c.c_ulong)]

        snapshot = k.CreateToolhelp32Snapshot(4, 0)
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            entry = Thread()
            entry.size = c.sizeof(entry)
            more = k.Thread32First(snapshot, c.byref(entry))
            while more:
                if entry.pid == pid:
                    thread = self.require(k.OpenThread(2, False, entry.id))
                    try:
                        if k.ResumeThread(thread) == 0xffffffff:
                            raise c.WinError(c.get_last_error())
                        return
                    finally:
                        k.CloseHandle(thread)
                more = k.Thread32Next(snapshot, c.byref(entry))
            raise OSError("suspended thread not found")
        finally:
            k.CloseHandle(snapshot)

    def terminate(self):
        self.require(self.k.TerminateJobObject(self.handle, 1))

    def empty(self):
        # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: four times and four DWORDs.
        data = self.c.create_string_buffer(48)
        self.require(self.k.QueryInformationJobObject(self.handle, 1, data, 48, None))
        return int.from_bytes(data.raw[40:44], "little") == 0

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


async def run_process(argv, workspace, timeout, output_limit, emit):
    job = WindowsJob() if os.name == "nt" else None
    process = None
    reader = None
    attached = False
    output = bytearray()
    total = 0

    async def drain():
        nonlocal total
        while chunk := await process.stdout.read(4096):
            total += len(chunk)
            output.extend(chunk[:max(0, output_limit - len(output))])

    async def cleanup():
        if process is None:
            return
        if job:
            if not attached and process.returncode is None:
                process.kill()
            job.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        end = asyncio.get_running_loop().time() + 5
        while job and not job.empty():
            if asyncio.get_running_loop().time() >= end:
                raise TerminationUnconfirmed()
            await asyncio.sleep(0.02)
        try:
            await asyncio.wait_for(process.wait(), 5)
            if reader:
                await asyncio.wait_for(asyncio.shield(reader), 5)
        except TimeoutError:
            raise TerminationUnconfirmed() from None
        if not job:
            while True:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                if asyncio.get_running_loop().time() >= end:
                    raise TerminationUnconfirmed()
                await asyncio.sleep(0.02)
        emit("shell_process_stopped", pid=process.pid, tree_terminated=True)

    try:
        options = {"creationflags": 0x4 | 0x08000000} if job else {"start_new_session": True}
        # Shield creation so cancellation cannot lose the handle to a newly created process.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, cwd=workspace, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **options))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            raise
        if job:
            job.attach_and_resume(process.pid)
            attached = True
        emit("shell_process_started", pid=process.pid)
        reader = asyncio.create_task(drain())
        timed_out = False
        try:
            async with asyncio.timeout(timeout):
                # Poll returncode: wait() can wait for inherited pipes after shell exit.
                while process.returncode is None:
                    await asyncio.sleep(0.02)
        except TimeoutError:
            timed_out = True
        return_code = process.returncode
    finally:
        try:
            try:
                await cleanup()
            except (OSError, TimeoutError):
                raise TerminationUnconfirmed() from None
        finally:
            if reader and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if job:
                job.close()
    return {"exit_code": return_code, "output": output.decode("utf-8", errors="replace"),
            "output_bytes": total, "truncated": total > output_limit, "timed_out": timed_out}
