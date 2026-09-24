import asyncio
import json

import pytest

from dual_lobe.dl import loop
from dual_lobe.dl.threeway import parse_address


def test_parse_address_prefixes():
    assert parse_address('@A explain this') == ('A', 'explain this')
    assert parse_address('@B: challenge that') == ('B', 'challenge that')
    assert parse_address('@both continue') == ('both', 'continue')
    assert parse_address('hello') == ('both', 'hello')
    assert parse_address('@A ignored by explicit', 'B') == ('B', '@A ignored by explicit')


class _Resp:
    def __init__(self, content):
        self.data = {'choices': [{'message': {'content': content}}]}


class _A:
    def __init__(self): self.requests = []
    async def buffered(self, req):
        self.requests.append(req)
        return _Resp(f'A reply {len(self.requests)}')


class _B:
    def __init__(self): self.requests = []
    async def buffered(self, req):
        self.requests.append(req)
        return _Resp(json.dumps({'action': 'pass', 'message': 'B response'}))


class _Registry:
    def __init__(self, a, b): self.a, self.b = a, b
    def adapter(self, name): return self.a if name == 'lobe-a' else self.b


class _Settings:
    dual_lobe_enabled = True
    dl_max_seconds = 5
    max_shadow_input_chars = 16000
    dual_lobe_rounds = 1
    dual_lobe_b_max_tokens = 100
    dual_lobe_a_max_tokens = 100
    dual_lobe_summarize = False
    dual_lobe_summary_max_tokens = 100
    dual_lobe_store_cap = 10
    shared_memory_timeout = 1
    b_timeout = 1
    a_timeout = 1


@pytest.mark.asyncio
@pytest.mark.parametrize(('recipient', 'expected_a_calls'), [('A', 1), ('B', 0)])
async def test_threeway_recipient_controls_next_actor(monkeypatch, recipient, expected_a_calls):
    a, b = _A(), _B()
    monkeypatch.setattr(loop, 'get_registry', lambda: _Registry(a, b))
    monkeypatch.setattr(loop, 'response_dict', lambda response: response.data)
    async def no_persist(*args, **kwargs): return None
    monkeypatch.setattr(loop, '_persist', no_persist)
    calls = 0
    async def interventions(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [{'id': 'e1', 'seq': 1, 'recipient': recipient,
                     'content': f'question for {recipient}', 'at': None}]
        return []
    monkeypatch.setattr(loop, 'interventions_since', interventions)

    events = []
    await loop.run_exchange(
        1, '00000000-0000-0000-0000-000000000001', 'task',
        [{'role': 'user', 'content': 'original'}], 'working', None, [], _Settings(),
        event_sink=lambda e: events.append(e), three_way=True,
        intervention_after_seq=0, intervention_grace_ms=0,
    )
    assert len(a.requests) == expected_a_calls
    assert any(e.get('actor') == 'USER' and e.get('recipient') == recipient for e in events)


@pytest.mark.asyncio
async def test_b_pass_grace_intervention_resumes_same_exchange(monkeypatch):
    a, b = _A(), _B()
    monkeypatch.setattr(loop, 'get_registry', lambda: _Registry(a, b))
    monkeypatch.setattr(loop, 'response_dict', lambda response: response.data)
    async def no_persist(*args, **kwargs): return None
    monkeypatch.setattr(loop, '_persist', no_persist)
    calls = 0
    async def interventions(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:  # first poll inside the pass/intervention window
            return [{'id': 'e1', 'seq': 1, 'recipient': 'B',
                     'content': 'Wait B, explain that', 'at': None}]
        return []
    monkeypatch.setattr(loop, 'interventions_since', interventions)

    events = []
    await loop.run_exchange(
        1, '00000000-0000-0000-0000-000000000001', 'task',
        [{'role': 'user', 'content': 'original'}], 'working', None, [], _Settings(),
        event_sink=lambda e: events.append(e), three_way=True,
        intervention_after_seq=0, intervention_grace_ms=300,
    )
    assert len(b.requests) >= 2
    assert any(e.get('type') == 'threeway_intervention_window' and e.get('intervened') for e in events)
