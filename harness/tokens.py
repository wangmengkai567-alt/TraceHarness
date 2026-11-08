"""Tokenize serialized request contents; provider-rendered usage remains separate."""
import json

import tiktoken

from .contracts import ModelError


class TokenCounter:
    def __init__(self, encoding):
        try:
            self.encoding = tiktoken.get_encoding(encoding)
        except Exception:
            raise ModelError("tokenizer_unavailable") from None

    def count(self, messages, tools):
        # Includes roles, tool call arguments, tool declarations, rules and state.
        # This is a tokenizer estimate of the serialized input, not the vendor's chat template.
        wire = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
        return len(self.encoding.encode(wire, disallowed_special=()))
