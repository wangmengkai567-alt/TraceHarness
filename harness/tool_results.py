"""工具反馈的通用输出限制，保持截断后的协议内容可解析。"""
from __future__ import annotations

import json

from .contracts import ToolResult


def bound_result(result: ToolResult, limit: int) -> ToolResult:
    # 限制序列化后的字符数，避免超大工具反馈耗尽下一轮输入预算。
    text = json.dumps(result.wire(), ensure_ascii=False)
    if len(text) <= limit:
        return result
    # 截断的是数据预览，再重新序列化；不直接截断 JSON，以免得到无效协议内容。
    preview_size = max(0, limit // 2 - 100)
    bounded = ToolResult(result.ok, data=text[:preview_size],
                         error="output_truncated", truncated=True)
    while len(json.dumps(bounded.wire(), ensure_ascii=False)) > limit and preview_size:
        preview_size //= 2
        bounded = ToolResult(result.ok, data=text[:preview_size],
                             error="output_truncated", truncated=True)
    return bounded
