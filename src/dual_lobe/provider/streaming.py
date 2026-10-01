"""OpenAI-compatible streaming response accumulation utilities."""
from __future__ import annotations

import json


class Completion:
    """Collect one Chat Completions choice, including fragmented tool arguments."""
    def __init__(self):
        self.content = ""
        self.refusal = ""
        self.tools: dict[int, dict] = {}
        self.finish = None
        self.usage = None

    def add(self, chunk: dict) -> str:
        if "error" in chunk:
            raise ValueError("Upstream error")
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if len(choices) > 1:
            raise ValueError("Director mode requires a single completion choice")
        text = ""
        for c in choices:
            if c.get("index", 0) != 0:
                raise ValueError("Unexpected choice or data after completion")
            if self.finish is not None:
                # Some providers (e.g. DeepInfra) send a second terminal chunk
                # after finish_reason that only carries usage data. Accept it
                # silently as long as it has no new content or tool calls.
                delta = c.get("delta") or {}
                if not delta.get("content") and not delta.get("tool_calls") and not delta.get("refusal"):
                    continue
                raise ValueError("Unexpected choice or data after completion")
            delta = c.get("delta") or {}
            text = delta.get("content") or ""
            self.content += text
            self.refusal += delta.get("refusal") or ""
            # These require provider-specific replay support, which this mode
            # cannot safely discard. Ordinary passthrough is unaffected.
            if delta.get("reasoning_details") or delta.get("signature"):
                raise ValueError("Provider-specific signed state is not supported in memory-capture mode")
            for tool in delta.get("tool_calls") or []:
                index = tool.get("index", 0)
                if not isinstance(index, int) or index < 0 or index > 127:
                    raise ValueError("Invalid tool index")
                item = self.tools.setdefault(index, {"id": "", "type": "function",
                                                       "function": {"name": "", "arguments": ""}})
                for key in ("id", "type"):
                    if tool.get(key):
                        if key == "id" and item[key] and item[key] != tool[key]:
                            raise ValueError("Tool ID changed during streaming")
                        item[key] = tool[key]
                for key in ("name", "arguments"):
                    item["function"][key] += (tool.get("function") or {}).get(key) or ""
            if c.get("finish_reason") is not None:
                self.finish = c["finish_reason"]
        return text

    def message(self, available_tools: list[dict] | None = None) -> dict:
        if self.finish not in ("stop", "tool_calls"):
            raise ValueError("Upstream completion was truncated or interrupted")
        result = {"role": "assistant", "content": self.content or None}
        if self.refusal:
            result["refusal"] = self.refusal
        if self.tools:
            if self.finish != "tool_calls":
                raise ValueError("Tool calls without a tool_calls finish reason")
            tools = [self.tools[i] for i in sorted(self.tools)]
            ids = [t["id"] for t in tools]
            names = {(t.get("function") or {}).get("name") for t in available_tools or []}
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate tool IDs")
            for t in tools:
                if not t["id"] or t["type"] != "function" or t["function"]["name"] not in names:
                    raise ValueError("Unknown or malformed tool request")
                if not isinstance(json.loads(t["function"]["arguments"]), dict):
                    raise ValueError("Tool arguments must be a JSON object")
            result["tool_calls"] = tools
        elif self.finish == "tool_calls" or not (self.content or self.refusal):
            raise ValueError("Empty or incomplete completion")
        return result
