"""持久化运行记录：每次运行一个目录，JSON 保存快照，JSONL 追加事件。

所有写入都先脱敏；单次运行由一个写入者维护事件序号，前端按序号增量读取。
本模块是框架内部的记录设施，不走模型文件工具，也不触发 Shell 审批。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

RUN_ID_PATTERN = r"(?:[0-9]{8}[0-9]{3,}|[0-9a-f]{32})"
# 同时接受“日期 + 当日序号”和旧版 UUID；供 Web、CLI 校验记录目录名。
RUN_ID = re.compile(RUN_ID_PATTERN)


def utc_now() -> str:
    # 事件时间统一为带时区的 UTC ISO 字符串，跨时区读取时仍可准确比较。
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    """每次运行只允许一个写入者；事件在后续动作开始前完成刷新。"""

    def __init__(self, root: Path, secrets: tuple[str, ...] = ()):
        # 目录名使用本地日期和递增序号；事件时间仍使用 UTC，两者用途不同。
        root.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        daily_id = re.compile(day + r"([0-9]{3,})")
        sequence = max((int(match[1]) for entry in root.iterdir()
                        if (match := daily_id.fullmatch(entry.name))), default=0) + 1
        while True:
            self.run_id = f"{day}{sequence:03d}"
            self.directory = root / self.run_id
            try:
                # 以原子创建目录的方式占用编号，防止 CLI/Web 多进程生成相同 ID。
                self.directory.mkdir(exist_ok=False)
                break
            except FileExistsError:
                sequence += 1
        self.sequence = 0
        self.secrets = tuple(value for value in secrets if value)

    def redact(self, value):
        # 递归替换已知密钥，包括字典键和嵌套列表；这是已知字符串脱敏，并非通用隐私识别。
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {self.redact(str(key)): self.redact(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.redact(item) for item in value]
        return value

    def write_json(self, name: str, payload: dict) -> None:
        # 先完整写临时文件再原子替换，避免读者在覆盖过程中读到半份 JSON。
        destination = self.directory / name
        temporary = destination.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(self.redact(payload), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)

    def emit(self, kind: str, **data) -> None:
        # kind 是事件类型，**data 是附带字段；每条事件占一行，便于增量读取。
        event = {"sequence": self.sequence + 1, "run_id": self.run_id,
                 "timestamp": utc_now(), "type": kind, "data": data}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(self.redact(event), ensure_ascii=False) + "\n")
            # flush 清空 Python 缓冲，fsync 要求操作系统同步到磁盘，降低异常退出丢日志的风险。
            stream.flush()
            os.fsync(stream.fileno())
        # 仅在写入和同步成功后递增；中途异常会向调用方传播。
        self.sequence += 1
