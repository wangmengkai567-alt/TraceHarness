"""为可信本地工作目录提供受限的文本文件读写，不提供操作系统沙箱。

读取返回原始字节的 SHA256；覆盖写入必须携带该哈希，以避免覆盖并发修改。
文件工具不走用户审批；路径、大小、编码、链接及规范文件限制在这里检查。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PureWindowsPath

from .contracts import ToolCall, ToolResult


class FileError(Exception):
    # 只携带可返回给模型的固定错误码，不暴露原始系统异常。
    pass


def digest(raw: bytes) -> str:
    # 对原始字节求哈希，BOM 或换行的变化也会被识别为文件变化。
    return hashlib.sha256(raw).hexdigest()


class FileTools:
    # root 是本次运行选定的工作目录；所有工具路径必须是它内部的相对路径。
    def __init__(self, workspace: Path, max_bytes: int = 65536, max_entries: int = 200):
        # 先解析真实根目录，再保存单文件字节数和目录条目数上限。
        self.root = workspace.resolve(strict=True)
        self.max_bytes, self.max_entries = max_bytes, max_entries

    def path(self, value: str) -> Path:
        # 统一的路径入口：先检查输入形式，再检查解析链接后的真实位置。
        if not isinstance(value, str) or not value or "\x00" in value:
            raise FileError("invalid_path")
        relative = Path(value)
        # 同时拒绝 Windows 设备路径、备用数据流（冒号）及盘符相对路径。
        if relative.is_absolute() or relative.drive or ":" in value or ".." in relative.parts:
            raise FileError("path_outside_workspace")
        if any(PureWindowsPath(part).is_reserved() or part.endswith((".", " "))
               for part in relative.parts if part != "."):
            raise FileError("invalid_path")
        candidate = self.root / relative
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise FileError("path_outside_workspace")
        return resolved

    def read(self, value: str) -> dict:
        # 最多多读一个字节来发现超限；只接收普通 UTF-8 文本，BOM 不放进返回的正文。
        path = self.path(value)
        if not path.is_file():
            raise FileError("not_a_regular_file")
        with path.open("rb") as stream:
            raw = stream.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise FileError("file_too_large")
        if b"\x00" in raw:
            raise FileError("binary_file")
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise FileError("unsupported_encoding") from None
        return {"path": value, "content": content, "sha256": digest(raw), "bytes": len(raw)}

    def rules(self, limit: int) -> dict:
        # 运行开始时读取根目录 AGENTS.md 作为规范快照；文件不存在也有明确记录。
        reader = FileTools(self.root, min(limit, self.max_bytes), self.max_entries)
        path = reader.path("AGENTS.md")
        if not path.exists():
            return {"path": "AGENTS.md", "present": False, "content": "", "sha256": None}
        return {**reader.read("AGENTS.md"), "present": True}

    def schemas(self) -> list[dict]:
        # 仅开放列目录、读文件和写文件，没有删除文件或任意路径访问工具。
        return [
            self.schema("list_files", "List one directory; bounded, no recursion.",
                        {"path": {"type": "string"}}, ["path"]),
            self.schema("read_file", "Read bounded UTF-8 text and raw-byte SHA256; oversized files are rejected.",
                        {"path": {"type": "string"}}, ["path"]),
            self.schema("write_file", "Write UTF-8 text. New file: expected_hash=null; existing: hash from read_file. Preserve existing BOM/newlines. Never edit AGENTS.md.",
                        {"path": {"type": "string"}, "content": {"type": "string"},
                         "expected_hash": {"type": ["string", "null"]}},
                        ["path", "content", "expected_hash"]),
        ]

    @staticmethod
    def schema(name, description, properties, required):
        # 多个模块复用的工具声明构造器；禁止模型添加未声明的参数字段。
        return {"type": "function", "function": {"name": name, "description": description,
                "parameters": {"type": "object", "properties": properties,
                               "required": required, "additionalProperties": False}}}

    def write(self, value: str, content: str, expected_hash: str | None) -> dict:
        # expected_hash=None 表示仅新建；提供哈希表示覆盖已有文件且内容必须仍匹配。
        path = self.path(value)
        if path.name.casefold() == "agents.md":
            raise FileError("rules_are_read_only")
        # 拒绝通过符号链接、目录联接或硬链接写入，防止借别名影响其他位置的文件。
        candidate = self.root / value
        for parent in (candidate, *candidate.parents):
            if parent == self.root:
                break
            if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                raise FileError("write_link_rejected")
        content = content.removeprefix("\ufeff").replace("\r\n", "\n")
        # 先统一模型提交内容的内部格式；覆盖旧文件时再恢复旧文件的 BOM 和换行风格。
        if "\r" in content:
            raise FileError("unsupported_newlines")
        raw = content.encode("utf-8")
        if b"\x00" in raw:
            raise FileError("binary_file")
        if len(raw) > self.max_bytes:
            raise FileError("file_too_large")
        if expected_hash is None:
            # xb 为独占创建，即使文件恰好被其他进程新建，也不会覆盖它。
            try:
                with path.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                raise FileError("file_conflict") from None
        else:
            with path.open("r+b") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise FileError("write_link_rejected")
                # 用文件锁串行化遵守锁约定的写入者，并在锁内重读字节、核对哈希。
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, self.max_bytes + 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                old = stream.read(self.max_bytes + 1)
                if len(old) > self.max_bytes:
                    raise FileError("file_too_large")
                if digest(old) != expected_hash:
                    raise FileError("file_conflict")
                if b"\x00" in old:
                    raise FileError("binary_file")
                text = old.decode("utf-8-sig")
                # 已有文件若混用换行则拒绝写入；合法的 LF/CRLF 和 UTF-8 BOM 会被保留。
                crlf = "\r\n" in text
                if "\r" in text.replace("\r\n", "") or (crlf and "\n" in text.replace("\r\n", "")):
                    raise FileError("mixed_newlines")
                normalized = content.replace("\r\n", "\n")
                raw = normalized.replace("\n", "\r\n").encode("utf-8") if crlf else normalized.encode("utf-8")
                if old.startswith(b"\xef\xbb\xbf"):
                    raw = b"\xef\xbb\xbf" + raw
                if len(raw) > self.max_bytes:
                    raise FileError("file_too_large")
                current = path.stat()
                # 写入前再次核对文件身份和元数据，发现读写期间被替换或修改就报冲突。
                if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (
                        info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size):
                    raise FileError("file_conflict")
                stream.seek(0)
                stream.write(raw)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
        return {"path": value, "sha256": digest(raw), "bytes": len(raw)}

    async def execute(self, call: ToolCall) -> ToolResult:
        # 模型调用入口：校验参数后分发操作，将预期文件错误转换成结构化反馈。
        try:
            args = json.loads(call.arguments)
            required = {"list_files": {"path"}, "read_file": {"path"},
                        "write_file": {"path", "content", "expected_hash"}}
            if call.name not in required:
                return ToolResult(False, error="unknown_tool")
            if not isinstance(args, dict) or set(args) != required[call.name]:
                raise FileError("invalid_file_arguments")
            path = self.path(args["path"])
            if call.name == "read_file":
                return ToolResult(True, self.read(args["path"]))
            if call.name == "list_files":
                entries = []
                with os.scandir(path) as iterator:
                    for entry in iterator:
                        if len(entries) >= self.max_entries:
                            return ToolResult(True, {"entries": sorted(entries)}, truncated=True)
                        entries.append(entry.name)
                return ToolResult(True, {"entries": sorted(entries)})
            if not isinstance(args["content"], str) or not (args["expected_hash"] is None or
                    isinstance(args["expected_hash"], str) and len(args["expected_hash"]) == 64):
                raise FileError("invalid_file_arguments")
            return ToolResult(True, self.write(args["path"], args["content"], args["expected_hash"]))
        except FileError as error:
            return ToolResult(False, error=str(error))
        except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
            return ToolResult(False, error="file_access_error")
