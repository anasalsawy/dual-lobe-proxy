"""Visible A/B turns within an HTTP segment, with durable host-tool handoff."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from contextlib import aclosing
from dataclasses import replace

from ..b.prompts import COLOR_POLICY, ENRICHMENT_POLICY, head_tail
from ..b.host_tools import offered_tools, make_plan, candidates
from ..core.redact import redact_payload
from ..provider.adapters import NormalizedRequest, response_dict
from .protocol import (B_INSTRUCTIONS, Completion, byte_size, observed_messages,
                       parse_decision)

LOG = logging.getLogger("dual_lobe.director")


class DirectorLoop:
    def __init__(self, state, store, token, req, a, b, settings, compose_a, admit,
                 record_a, *, admit_b=None):
        self.state, self.store, self.token = state, store, token
        self.req, self.a, self.b, self.s = req, a, b, settings
        self.compose_a, self.admit, self.record_a = compose_a, admit, record_a
        self.admit_b = admit_b or admit
        self.released = False
        self.visible = ""
        self.response_id = "chatcmpl-director-" + uuid.uuid4().hex
        self.created = int(time.time())
        self.segment_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.usage_complete = True

    async def save(self, *, release=False):
        async with asyncio.timeout(5):
            await self.store.save(self.token, self.state, release=release)
        self.released = release

    def text(self, text):
        self.visible += text
        return {"kind": "text", "text": text}

    def usage(self, value):
        keys = self.segment_usage.keys()
        if not value or not all(isinstance(value.get(k), int) and value[k] >= 0 for k in keys):
            self.usage_complete = self.state["usage_complete"] = False
            return
        for key in keys:
            self.segment_usage[key] += value[key]
            self.state["usage"][key] += value[key]

    async def finish(self, status: str, reason: str, tools=None):
        message = {"role": "assistant", "content": self.visible or None}
        if tools:
            message["tool_calls"] = tools
        self.state["wire"].append(message)
        self.state.update(status=status, phase="idle", reason=reason)
        # This commit MUST succeed before exposing executable tool calls or a
        # successful terminal event. Failure leaves an explicit incomplete result.
        await self.save(release=True)

    async def abort(self, reason: str):
        if self.released:
            return
        self.state.update(status="cancelled" if reason == "cancelled" else "failed",
                          reason=reason, phase="interrupted")
        try:
            await self.save(release=True)
        except Exception:
            # An unreleased lease is deliberately not replayable after a crash.
            LOG.warning("Director checkpoint failed; lease will prevent replay")

    async def events(self):
        try:
            remaining = self.state["deadline"] - time.time()
            if remaining <= 0:
                yield self.text("\n[Director stopped: time limit reached. This is not a completion verdict.]\n")
                await self.finish("stopped", "time_limit")
                yield {"kind": "end", "finish_reason": "stop"}
                return
            async with asyncio.timeout(remaining), aclosing(self._turns()) as turns:
                async for event in turns:
                    yield event
        except (asyncio.CancelledError, GeneratorExit):
            await self.abort("cancelled")
            raise
        except Exception as exc:
            reason = "time_limit" if isinstance(exc, TimeoutError) else type(exc).__name__
            await self.abort(reason)
            # Never forward raw exceptions, provider credentials, or fake stop.
            yield {"kind": "error", "error": {"type": "director_incomplete",
                   "message": "Director interrupted (" + reason + "). Partial exchange only; no automatic retry."}}
        finally:
            if not self.released:
                await self.abort("cancelled")

    async def _turns(self):
        while True:
            if self.state["a_calls"] >= self.state["max_a_calls"]:
                yield self.text("\n[Director stopped: A-call limit reached. Work may remain.]\n")
                await self.finish("stopped", "call_limit")
                yield {"kind": "end", "finish_reason": "stop"}
                return
            if byte_size(self.state) > self.s.director_max_state_bytes // 2:
                yield self.text("\n[Director stopped: session context limit reached. Start a new run using the same memory space.]\n")
                await self.finish("stopped", "context_limit")
                yield {"kind": "end", "finish_reason": "stop"}
                return
            # Reload both completed observer channels and shared memory at every
            # A call, including calls immediately following real tool results.
            messages = await self.compose_a(self.state["transcript"])
            await self.admit(messages)
            req = replace(self.req, messages=messages, stream=True,
                          timeout=min(self.s.a_timeout, max(.01, self.state["deadline"] - time.time())))
            if req.max_completion_tokens is not None:
                req.max_completion_tokens = min(req.max_completion_tokens, self.s.director_a_max_tokens)
                req.max_tokens = None
            else:
                req.max_tokens = min(req.max_tokens or self.s.director_a_max_tokens, self.s.director_a_max_tokens)
            # Preserve the caller's stream options. Do not add an unsupported
            # provider option merely to get accounting; missing usage stays unknown.
            self.state["a_calls"] += 1
            self.state["phase"] = "a_inflight"
            await self.save()
            call_id = str(uuid.uuid4())
            started = time.monotonic()
            result = Completion()
            yield self.text(f"\nA (turn {self.state['a_calls']}):\n")
            async with asyncio.timeout(req.timeout), aclosing(self.a.stream(req)) as upstream:
                async for chunk in upstream:
                    part = result.add(response_dict(chunk))
                    if byte_size([result.content, result.refusal, result.tools]) > self.s.director_max_state_bytes // 4:
                        raise ValueError("A output exceeds the director state budget")
                    if part:
                        yield self.text(part)
            message = result.message(req.tools)
            self.usage(result.usage)
            if result.refusal:
                yield self.text(result.refusal)
            await self.record_a(call_id, copy.deepcopy(self.state["transcript"]), message,
                                result.usage, int((time.monotonic() - started) * 1000))
            self.state["transcript"].append(message)
            if message.get("tool_calls"):
                calls = message["tool_calls"]
                self.state["pending_tools"] = [t["id"] for t in calls]
                yield self.text("\n[Director waiting for your agent application's tool results.]\n")
                await self.finish("waiting_tools", "tool_handoff", calls)
                # Complete ordinary tool_calls response. The caller can NOW run
                # its tools and make a new request carrying their real results.
                yield {"kind": "tools", "tool_calls": calls}
                yield {"kind": "end", "finish_reason": "tool_calls"}
                return
            if result.refusal:
                yield self.text("\n[Director stopped: A returned a refusal.]\n")
                await self.finish("stopped", "refusal")
                yield {"kind": "end", "finish_reason": "stop"}
                return
            # Review the goal and visible transcript, retaining the first user
            # request and latest output. Long history is explicitly excerpted.
            data = observed_messages(messages) + [message]
            exposed = offered_tools(self.req.tools, self.s.max_shadow_input_chars // 4) if self.s.b_host_tools_enabled else []
            observation = {"TRANSCRIPT": head_tail(json.dumps(redact_payload(data), ensure_ascii=False),
                self.s.max_shadow_input_chars // 2), "HOST_TOOLS": redact_payload(exposed)}
            while len(json.dumps(observation, ensure_ascii=False)) > self.s.max_shadow_input_chars:
                observation["TRANSCRIPT"] = head_tail(observation["TRANSCRIPT"], len(observation["TRANSCRIPT"]) // 2)
            prompt = json.dumps(observation, ensure_ascii=False)
            instructions = B_INSTRUCTIONS + "\n\n" + COLOR_POLICY
            if self.s.context_memory_enabled and self.s.context_enrichment_enabled:
                instructions += "\n\n" + ENRICHMENT_POLICY
            b_req = NormalizedRequest(messages=[{"role": "system", "content": instructions},
                                                {"role": "user", "content": prompt}],
                                      temperature=0, max_tokens=self.s.director_b_max_tokens,
                                      timeout=self.s.b_timeout)
            await self.admit_b(b_req.messages)
            self.state["b_calls"] += 1
            self.state["phase"] = "b_inflight"
            await self.save()
            async with asyncio.timeout(self.s.b_timeout):
                b_data = response_dict(await self.b.buffered(b_req))
            decision = parse_decision(b_data)
            self.usage(b_data.get("usage"))
            yield self.text("\n\nB (director):\n" + decision.message + "\n")
            self.state["transcript"].append({"role": "user", "name": "director_b",
                                              "content": decision.message})
            self.state["last_b_assessment"] = {"source_call": call_id, "observed_at": time.time(),
                "deception_level": decision.deception_level, "reason": decision.deception_reason}
            if decision.action == "stop":
                yield self.text("\n[Director stopped: B chose to end the exchange; this is an assessment, not independent verification.]\n")
                await self.finish("stopped", "b_stop")
                yield {"kind": "end", "finish_reason": "stop"}
                return
            # One optional B tool batch per real-user invocation. Host execution
            # resumes A with the actual results through the existing checkpoint.
            extra = []
            if exposed and not self.state.get("b_tool_batches", 0):
                try:
                    extra = candidates(make_plan(decision, exposed), self.req, {"content": ""})
                except Exception:
                    pass
            if extra:
                self.state["b_tool_batches"] = 1
                self.state["transcript"].append({"role": "assistant", "content": None, "tool_calls": extra})
                self.state["pending_tools"] = [c["id"] for c in extra]
                yield self.text("\n[Returning B's information requests to your app's tool loop.]\n")
                await self.finish("waiting_tools", "observer_tool_handoff", extra)
                yield {"kind": "tools", "tool_calls": extra}
                yield {"kind": "end", "finish_reason": "tool_calls"}
                return


async def with_heartbeats(source, interval=10):
    """Keep SSE responsive while B/DB/provider is waiting; propagate cancellation."""
    pending = None
    async with aclosing(source):
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(source))
                done, _ = await asyncio.wait({pending}, timeout=interval)
                if not done:
                    yield None
                    continue
                completed, pending = pending, None
                try:
                    yield completed.result()
                except StopAsyncIteration:
                    return
        finally:
            if pending is not None:
                pending.cancel()
                try:
                    await pending
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass


async def stream(loop: DirectorLoop, public_model: str, include_usage: bool):
    def chunk(delta, finish=None):
        result = {"id": loop.response_id, "created": loop.created, "model": public_model,
                  "object": "chat.completion.chunk", "choices": [
                      {"index": 0, "delta": delta, "finish_reason": finish}]}
        if include_usage:
            result["usage"] = None
        return result
    try:
        yield "data: " + json.dumps(chunk({"role": "assistant"})) + "\n\n"
        async with aclosing(with_heartbeats(loop.events())) as events:
            async for event in events:
                if event is None:
                    yield ": director heartbeat\n\n"
                    continue
                if event["kind"] == "error":
                    yield "data: " + json.dumps({"error": event["error"]}) + "\n\n"
                elif event["kind"] == "text":
                    yield "data: " + json.dumps(chunk({"content": event["text"]}), ensure_ascii=False) + "\n\n"
                elif event["kind"] == "tools":
                    yield "data: " + json.dumps(chunk({"tool_calls": [
                        {"index": i, **t} for i, t in enumerate(event["tool_calls"])]}), ensure_ascii=False) + "\n\n"
                elif event["kind"] == "end":
                    yield "data: " + json.dumps(chunk({}, event["finish_reason"])) + "\n\n"
                    if include_usage and loop.usage_complete:
                        usage = chunk({})
                        usage.update(choices=[], usage=loop.segment_usage)
                        yield "data: " + json.dumps(usage) + "\n\n"
    finally:
        if not loop.released:
            await loop.abort("cancelled")
    yield "data: [DONE]\n\n"
