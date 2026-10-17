"""加载和校验运行配置，集中管理模型、预算、文件、联网及验收限制。

优先级由低到高：默认值 → JSON 配置 → 指定的环境变量 → overrides。
密钥单独从环境读取，不放进 Config，以免随配置快照写入运行记录。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    # frozen 禁止运行中重新赋值字段；checks 本身是列表，Runner 会另做深拷贝。
    # 模型服务连接信息；目前只支持兼容 Chat Completions 的服务。
    provider: str = "openai_compatible"  # 模型接口类型。
    model: str = ""  # 服务商提供的模型名称，启动前必须设置。
    base_url: str = ""  # API 根地址，适配器会在其后拼接 /chat/completions。
    # turn 是一次模型响应轮次；model_calls 还包含失败后的请求重试。
    max_turns: int = 8  # 最多模型响应轮次，最后一轮预留给最终回答。
    max_model_calls: int = 16  # 含请求重试的模型调用总次数上限。
    max_tool_calls_per_turn: int = 4  # 一条模型回复最多携带的工具调用数。
    # 总时限覆盖模型请求、工具执行、审批等待和重试；其余为各阶段的时限。
    total_timeout_seconds: float = 360  # 整次任务总时限，单位秒。
    request_timeout_seconds: float = 90  # 单次模型请求时限。
    tool_timeout_seconds: float = 5  # 普通工具的执行时限，Shell/联网另有专用值。
    approval_timeout_seconds: float = 60  # 一次 Shell 审批最多等待多久。
    shell_timeout_seconds: float = 60  # 单条 Shell 命令允许申请的最大执行时限。
    # 暂时性请求错误按指数退避重试，等待时间为 retry_base_seconds * 2**attempt。
    max_retries: int = 2  # 首次请求失败后最多再尝试几次。
    retry_base_seconds: float = 0.5  # 退避等待的基础秒数。
    # 字符、字节和 token 是不同预算：分别限制序列化内容、HTTP 响应和模型生成量。
    max_input_chars: int = 64000  # 模型请求中消息和工具声明的序列化字符上限。
    max_output_chars: int = 16000  # 单条模型消息/工具反馈的序列化字符上限。
    max_response_bytes: int = 262144  # 模型 HTTP 响应体的字节上限。
    max_completion_tokens: int = 4096  # 单次请求的模型生成 token 上限。
    completion_token_parameter: str = "max_tokens"  # 服务商接受的生成上限参数名称。
    # 运行记录和文件访问限制；history 只保留最近若干组完整的对话与工具反馈。
    runs_dir: str = "runs"  # 每次运行的元数据、事件、状态和结果保存位置。
    max_file_bytes: int = 65536  # 文件工具单次读取/写入的文件大小上限。
    max_rules_bytes: int = 16384  # 根目录 AGENTS.md 规范快照的大小上限。
    max_directory_entries: int = 200  # 列目录最多返回的条目数。
    recent_history_turns: int = 4  # 每轮上下文保留的最近完整对话组数。
    max_state_chars: int = 16000  # 单次更新计划/记忆的参数字符串长度上限。
    # 全程工具次数、模型格式修复次数、连续相同失败及独立验收的次数上限。
    max_tool_calls: int = 64  # 包含验收命令在内的工具分发总次数上限。
    max_model_repairs: int = 2  # 允许模型修正回复格式或重复调用 ID 的次数。
    max_consecutive_failures: int = 3  # 连续以相同工具和参数失败的停止阈值。
    max_verification_attempts: int = 2  # 模型最终回答触发的验收批次上限。
    # 联网默认关闭；开启后还需要 TAVILY_API_KEY，搜索和正文抽取分别计数。
    network_enabled: bool = False  # 是否启用联网能力。
    search_timeout_seconds: float = 30  # 一次搜索工具操作的外层时限及 HTTP 请求时限。
    extract_timeout_seconds: float = 45  # 一次正文读取操作的外层时限及 HTTP 请求时限。
    max_search_requests: int = 5  # 全程搜索 HTTP 请求上限，重试也计数。
    max_extract_requests: int = 10  # 全程正文抽取 HTTP 请求上限，缓存续读不计数。
    search_max_results: int = 5  # 每次搜索允许返回的最大结果数。
    network_max_response_bytes: int = 2097152  # 搜索/抽取响应体字节上限。
    max_web_document_chars: int = 200000  # 单篇抽取正文最多保存的字符数。
    max_web_storage_chars: int = 500000  # 当前运行所有正文合计保存的字符数上限。
    web_chunk_chars: int = 4000  # 每次交给模型的正文片段字符数上限。
    # 由操作者配置的验收条件，支持文件包含文本和 Shell 检查，模型不能自行替换。
    checks: list[dict] = field(default_factory=list)

    def validate(self) -> None:
        # 启动前拒绝不合法配置；精确检查类型，避免把 Python 的 bool 当成 int。
        if type(self.network_enabled) is not bool:
            raise ValueError("network_enabled must be boolean")
        if self.provider != "openai_compatible":
            raise ValueError("provider must be openai_compatible; simulated providers are not supported")
        for name in ("model", "base_url", "runs_dir"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be text")
        if not self.runs_dir.strip():
            raise ValueError("runs_dir must not be empty")
        integers = (
            "max_turns", "max_model_calls", "max_tool_calls_per_turn",
            "max_input_chars", "max_output_chars", "max_response_bytes",
            "max_completion_tokens",
            "max_file_bytes", "max_rules_bytes", "max_directory_entries",
            "recent_history_turns", "max_state_chars",
            "max_tool_calls", "max_consecutive_failures", "max_verification_attempts",
            "max_search_requests", "max_extract_requests", "search_max_results",
            "network_max_response_bytes", "max_web_document_chars", "max_web_storage_chars", "web_chunk_chars",
        )
        for name in integers:
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_output_chars < 128:
            raise ValueError("max_output_chars must be at least 128")
        if self.network_enabled and self.max_output_chars < 4096:
            raise ValueError("network tools require max_output_chars >= 4096")
        if self.search_max_results > 10:
            raise ValueError("search_max_results must be <= 10")
        if type(self.max_model_repairs) is not int or not 0 <= self.max_model_repairs <= 10:
            raise ValueError("max_model_repairs must be between 0 and 10")
        if self.completion_token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("completion_token_parameter must be max_tokens or max_completion_tokens")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 10:
            raise ValueError("max_retries must be between 0 and 10")
        for name in ("total_timeout_seconds", "request_timeout_seconds",
                     "tool_timeout_seconds", "retry_base_seconds",
                     "approval_timeout_seconds", "shell_timeout_seconds", "search_timeout_seconds",
                     "extract_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        from .verification import validate_checks
        validate_checks(self.checks, self.shell_timeout_seconds)
        # 远程地址要求 HTTPS；仅回环地址允许 HTTP，便于本地模型服务和测试。
        if self.provider == "openai_compatible":
            if not self.model.strip():
                raise ValueError("set HARNESS_MODEL or model in config")
            url = urlsplit(self.base_url)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username or url.password or url.query or url.fragment):
                raise ValueError("base_url must be an API URL without credentials/query/fragment")
            if url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("remote model endpoints require HTTPS")

    def snapshot(self) -> dict:
        # 转成可保存的普通字典，便于复盘某次运行使用的实际配置。
        return asdict(self)


def load_config(path: Path | None = None, env_file: Path | None = None,
                overrides: dict | None = None) -> Config:
    # 仅加载显式指定的 .env，避免误读上级项目；已有进程环境变量优先。
    if env_file is not None:
        if not env_file.is_file():
            raise ValueError("specified env file does not exist")
        load_dotenv(env_file, override=False)
    values = {}
    if path is not None:
        # utf-8-sig 同时兼容带 BOM 和不带 BOM 的 UTF-8 配置；未知字段直接拒绝。
        values = json.loads(path.read_text(encoding="utf-8-sig"))
        allowed = {field.name for field in fields(Config)}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError("config must be an object with documented fields; secrets belong in env")
    for name in ("provider", "model", "base_url"):
        # 仅这三个配置字段支持 HARNESS_ 环境变量覆盖，其他字段从文件或参数读取。
        value = os.environ.get(f"HARNESS_{name.upper()}")
        if value:
            values[name] = value
    if overrides:
        values.update(overrides)
    config = Config(**values)
    config.validate()
    return config
