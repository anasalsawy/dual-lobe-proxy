import json

from fastapi.testclient import TestClient

from dual_lobe_crewai import webapp

OUT = {
    "answer": "hello from dual-lobe",
    "visible_text": "hello from dual-lobe\n\nDual-Lobe meter: [GREEN] verified",
    "verdict": {"deception_level": "GREEN", "rationale": "verified"},
    "logical_model_calls": 3,
}


def _client(monkeypatch, seen, out=OUT):
    async def fake_chat(req):
        seen.append(req)
        return out

    monkeypatch.setattr(webapp, "chat", fake_chat)
    return TestClient(webapp.app)


def test_models_lists_this_service(monkeypatch):
    body = _client(monkeypatch, []).get("/v1/models").json()
    assert body["data"][0]["id"] == webapp.MODEL_ID


def test_only_latest_user_message_reaches_engine(monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_SERVICE_MODE", "normal")
    seen = []
    _client(monkeypatch, seen).post("/v1/chat/completions", json={"messages": [
        {"role": "system", "content": "HUGE CLIENT SYSTEM PROMPT"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]})
    assert seen[0].message == "second"


def test_reply_is_engine_output_with_meter_and_structured_fields(monkeypatch):
    body = _client(monkeypatch, []).post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "Hi"}],
    }).json()
    assert body["choices"][0]["message"]["content"] == OUT["visible_text"]
    assert body["dual_lobe"]["verdict"] == OUT["verdict"]
    assert body["dual_lobe"]["logical_model_calls"] == 3


def test_unsupported_fields_are_reported_not_silently_dropped(monkeypatch):
    r = _client(monkeypatch, []).post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [{"type": "function", "function": {"name": "get_weather"}}],
        "temperature": 0.2,
    })
    assert r.json()["dual_lobe"]["ignored_request_fields"] == ["temperature", "tools"]
    assert r.headers["X-Dual-Lobe-Ignored"] == "temperature,tools"


def test_streaming_sends_final_answer_and_structured_data(monkeypatch):
    r = _client(monkeypatch, []).post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "Hi"}], "stream": True,
    })
    assert r.headers["X-Dual-Lobe-Streaming"] == "final-answer-only"
    chunks = [line[6:] for line in r.text.splitlines() if line.startswith("data: ")]
    assert chunks[-1] == "[DONE]"
    assert json.loads(chunks[0])["choices"][0]["delta"]["content"] == OUT["visible_text"]
    assert json.loads(chunks[1])["dual_lobe"]["verdict"] == OUT["verdict"]


def test_streaming_sends_keepalive_while_engine_works(monkeypatch):
    import asyncio

    async def slow_chat(req):
        await asyncio.sleep(0.3)
        return OUT

    monkeypatch.setattr(webapp, "chat", slow_chat)
    monkeypatch.setattr(webapp, "_HEARTBEAT_SECONDS", 0.05)
    r = TestClient(webapp.app).post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "Hi"}], "stream": True,
    })
    assert ": dual-lobe working" in r.text


def test_tool_result_message_is_rejected_clearly(monkeypatch):
    r = _client(monkeypatch, []).post("/v1/chat/completions", json={"messages": [
        {"role": "user", "content": "Hi"}, {"role": "tool", "content": "{}", "tool_call_id": "x"},
    ]})
    assert r.status_code == 400
    assert "Tool-result messages are not supported" in r.json()["detail"]


def test_api_key_is_enforced_when_set(monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_SERVER_API_KEY", "secret")
    client = _client(monkeypatch, [])
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_health():
    assert TestClient(webapp.app).get("/health").json()["status"] == "ok"
