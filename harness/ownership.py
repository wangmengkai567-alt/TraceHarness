"""运行记录目录的跨进程独占权；锁文件始终保留，关闭句柄才释放锁。"""
from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path


class RunRootError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RunRootLock:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._file = None
        self._pid = None

    def acquire(self) -> RunRootLock:
        if self._file is not None:
            raise RunRootError("run_root_lock_already_held")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            stream = (self.root / ".owner.lock").open("a+b", buffering=0)
        except OSError:
            raise RunRootError("run_root_lock_unavailable") from None
        try:
            # Windows 允许锁定 EOF 之外的字节；空文件也锁定同一个固定区域。
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            busy = error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
            raise RunRootError("run_root_in_use" if busy else "run_root_lock_unavailable") from None
        except BaseException:
            stream.close()
            raise
        self._file, self._pid = stream, os.getpid()
        return self

    def require(self, root: Path) -> None:
        if self._file is None or self._file.closed or self._pid != os.getpid():
            raise RunRootError("run_root_lock_not_held")
        if Path(root).resolve() != self.root:
            raise RunRootError("run_root_lock_mismatch")

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file, self._pid = None, None

    def __enter__(self) -> RunRootLock:
        return self.acquire()

    def __exit__(self, *_):
        self.close()


@contextmanager
def locked_run_root(root: Path, existing: RunRootLock | None = None):
    """独立调用取得锁；Web 任务借用服务持有的锁，不重复获取或提前释放。"""
    if existing is not None:
        existing.require(root)
        yield existing
    else:
        with RunRootLock(root) as owner:
            yield owner
