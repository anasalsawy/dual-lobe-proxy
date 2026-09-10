import json

import httpx
import pytest

from dual_lobe.client import reply
from dual_lobe.demo import run


def test_offline_demo_explicitly_uses_fixtures_and_passes():
    result = run()
    assert "OFFLINE FIXTURES" in result["mode"]
    assert all(result["checks"].values())


@pytest.mark.parametrize("ending,valid", [("stop", True), (None, False)])
async def test_client_displays_receipt_and_rejects_unterminated_output(capsys, ending, valid):
    def handler(request):
        assert request.headers["x-dl-run-id"] == "test-run"
        assert json.loads(request.content)["messages"] == [{"role": "user", "content": "hello"}]
        chunk = {"choices": [{"index": 0, "delta": {"content": "hello back"}, "finish_reason": ending}]}
        return httpx.Response(200, headers={"x-dual-lobe-memory": "v2", "x-dual-lobe-claims": "none",
                                           "x-dual-lobe-monitoring": "on"},
                              content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    async with httpx.AsyncClient(base_url="https://example.invalid", transport=httpx.MockTransport(handler)) as client:
        if valid:
            assert await reply(client, [{"role": "user", "content": "hello"}], "test-run") == {
                "role": "assistant", "content": "hello back"}
        else:
            with pytest.raises(RuntimeError, match="terminal result"):
                await reply(client, [{"role": "user", "content": "hello"}], "test-run")
    assert "memory=v2, claims=none, monitoring=on" in capsys.readouterr().out
