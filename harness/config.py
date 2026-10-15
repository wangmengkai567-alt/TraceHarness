from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    provider: str = "openai_compatible"
    model: str = ""
    base_url: str = ""
    max_turns: int = 8
    max_model_calls: int = 16
    max_tool_calls_per_turn: int = 4
    total_timeout_seconds: float = 120
    request_timeout_seconds: float = 30
    tool_timeout_seconds: float = 5
    max_retries: int = 2
    retry_base_seconds: float = 0.5
    max_input_chars: int = 64000
    max_output_chars: int = 8000
    max_response_bytes: int = 262144
    max_completion_tokens: int = 1024
    completion_token_parameter: str = "max_tokens"
    runs_dir: str = "runs"

    def validate(self) -> None:
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
        )
        for name in integers:
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_output_chars < 128:
            raise ValueError("max_output_chars must be at least 128")
        if self.completion_token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("completion_token_parameter must be max_tokens or max_completion_tokens")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 10:
            raise ValueError("max_retries must be between 0 and 10")
        for name in ("total_timeout_seconds", "request_timeout_seconds",
                     "tool_timeout_seconds", "retry_base_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
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
        return asdict(self)


def load_config(path: Path | None = None, env_file: Path | None = None,
                overrides: dict | None = None) -> Config:
    # Explicit path only: do not discover a parent project's .env implicitly.
    if env_file is not None:
        if not env_file.is_file():
            raise ValueError("specified env file does not exist")
        load_dotenv(env_file, override=False)
    values = {}
    if path is not None:
        values = json.loads(path.read_text(encoding="utf-8-sig"))
        allowed = {field.name for field in fields(Config)}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError("config must be an object with documented fields; secrets belong in env")
    for name in ("provider", "model", "base_url"):
        value = os.environ.get(f"HARNESS_{name.upper()}")
        if value:
            values[name] = value
    if overrides:
        values.update(overrides)
    config = Config(**values)
    config.validate()
    return config
