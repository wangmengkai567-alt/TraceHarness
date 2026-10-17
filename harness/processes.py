"""管理本次命令所属的进程树，确保完成、超时或取消后都执行清理。

Windows 先挂起创建进程，加入 Job Object 后再恢复；其他平台创建独立进程组。
清理仅针对本模块创建的进程树；无法确认清理成功时抛出 TerminationUnconfirmed。
"""
from __future__ import annotations

import asyncio
import os
import signal


class TerminationUnconfirmed(Exception):
    # 区别于普通命令失败：子进程可能仍存活，上层应阻止继续启动新任务。
    pass


def process_alive(pid: int) -> bool:
    # 为历史记录提供 PID 存活提示；访问被拒绝时不能认定进程已经退出。
    if os.name == "nt":
        import ctypes as c
        from ctypes import wintypes as w
        k = c.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.OpenProcess(0x1000, False, pid)
        if not handle:
            return c.get_last_error() == 5  # 错误码 5 为拒绝访问，不能据此判定进程退出。
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
    # Windows Job Object 将根进程及其后代归到一起，便于整体终止和检查残留。
    def __init__(self):
        # 显式声明 Win32 函数签名，保证 ctypes 在 64 位环境下正确传递句柄和指针。
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
            # 对应 Win32 的基础限制结构；字段顺序和大小必须与系统定义一致。
            _fields_ = [("times", c.c_int64 * 2), ("flags", w.DWORD),
                        ("working", c.c_size_t * 2), ("active", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class Extended(c.Structure):
            # 扩展限制包含基础限制、IO 统计和内存限制，交给 SetInformationJobObject。
            _fields_ = [("basic", Basic), ("io", c.c_uint64 * 6), ("memory", c.c_size_t * 4)]

        self.handle = k.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE：关闭 Job 时终止其中进程。
        try:
            self.require(self.handle)
            self.require(k.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def require(self, value):
        # 将 Win32 API 的失败返回值转成 Python 异常，保留系统错误供内部控制使用。
        if not value:
            raise self.c.WinError(self.c.get_last_error())
        return value

    def attach_and_resume(self, pid):
        # 先将仍处于挂起状态的进程纳入 Job，防止它提前创建未受管理的子进程。
        c, k = self.c, self.k
        process = self.require(k.OpenProcess(0x101, False, pid))
        try:
            self.require(k.AssignProcessToJobObject(self.handle, process))
        finally:
            k.CloseHandle(process)

        class Thread(c.Structure):
            # 线程枚举记录；根据所属 PID 找到目标进程线程并恢复执行。
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
        # 终止 Job 内全部进程，而非仅关闭最外层 Shell。
        self.require(self.k.TerminateJobObject(self.handle, 1))

    def empty(self):
        # 读取 JOBOBJECT_BASIC_ACCOUNTING_INFORMATION（四个时间值和四个 DWORD）。
        # 第 40～43 字节为 ActiveProcesses，归零才表示整个 Job 已无活动进程。
        data = self.c.create_string_buffer(48)
        self.require(self.k.QueryInformationJobObject(self.handle, 1, data, 48, None))
        return int.from_bytes(data.raw[40:44], "little") == 0

    def close(self):
        # 释放系统句柄；已关闭时可重复调用。
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


async def run_process(argv, workspace, timeout, output_limit, emit):
    # 对外统一入口：合并标准输出/错误，限制保留字节数，并在所有退出路径清理进程树。
    job = WindowsJob() if os.name == "nt" else None
    process = None
    reader = None
    attached = False
    output = bytearray()
    total = 0

    async def drain():
        # 即使已达到输出保留上限仍持续排空管道，避免子进程因管道塞满而阻塞。
        nonlocal total
        while chunk := await process.stdout.read(4096):
            total += len(chunk)
            output.extend(chunk[:max(0, output_limit - len(output))])

    async def cleanup():
        # 正常退出也要清理遗留子进程，不允许借一次工具调用留下常驻后台服务。
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
            # 发送终止请求后仍需确认没有活动进程；超过清理时限就上报不确定状态。
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
        # Windows 标志为 CREATE_SUSPENDED 和 CREATE_NO_WINDOW；POSIX 使用独立会话。
        options = {"creationflags": 0x4 | 0x08000000} if job else {"start_new_session": True}
        # 屏蔽创建阶段的取消传播，确保即使取消也能拿到新进程句柄并进行清理。
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
                # 轮询退出码，避免 Shell 已退出但子进程仍持有管道，导致 wait 一直等待。
                while process.returncode is None:
                    await asyncio.sleep(0.02)
        except TimeoutError:
            timed_out = True
        return_code = process.returncode
    finally:
        # 无论成功、超时、取消还是创建失败，都尝试清理并释放 reader/Job 资源。
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
