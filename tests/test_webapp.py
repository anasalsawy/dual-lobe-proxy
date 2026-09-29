import json

from fastapi.testclient import TestClient

from dual_lobe_crewai import webapp


def _client(monkeypatch, seen):
    async def fake_chat(req):
        seen.append(req)
        return {"answer": "hi", "visible_text": "hi\n\nDual-Lobe meter: [GREEN] verified"}

    monkeypatch.setattr(webapp, "chat", fake_chat)
    return TestClient(webapp.app)


def test_health():
    assert TestClient(webapp.app).get("/health").json()["status"] == "ok"


def test_models_lists_this_service(monkeypatch):
    body = _client(monkeypatch, []).get("/v1/models").json()
    assert body["data"][0]["id"] == webapp.MODEL_ID


def test_chat_completion_includes_meter(monkeypatch):
    seen = []
    body = _client(monkeypatch, seen).post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "Hi"}],
    }).json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"].endswith("Dual-Lobe meter: [GREEN] verified")
    assert seen[0].message == "Hi"


def test_streaming_ends_with_done(monkeypatch):
    text = _client(monkeypatch, []).post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "Hi"}], "stream": True,
    }).text
    chunks = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
    assert chunks[-1] == "[DONE]"
    assert "Dual-Lobe meter" in json.loads(chunks[0])["choices"][0]["delta"]["content"]


def test_api_key_is_enforced_when_set(monkeypatch):
    monkeypatch.setenv("DUAL_LOBE_SERVER_API_KEY", "secret")
    client = _client(monkeypatch, [])
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_last_message_must_be_user(monkeypatch):
    r = _client(monkeypatch, []).post("/v1/chat/completions", json={"messages": [{"role": "assistant", "content": "x"}]})
    assert r.status_code == 400
