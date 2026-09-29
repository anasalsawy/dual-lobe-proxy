import asyncio

from dual_lobe_crewai import runner


class Spec:
    def __init__(self, label):
        self.label = label


def test_round_robin_is_global_across_roles():
    runner._rr_reset()
    specs = [Spec("p1"), Spec("p2"), Spec("p3")]

    assert [x.label for x in runner._round_robin_specs("A", specs)] == ["p1", "p2", "p3"]
    assert [x.label for x in runner._round_robin_specs("B_VERIFY", specs)] == ["p2", "p3", "p1"]
    assert [x.label for x in runner._round_robin_specs("A", specs)] == ["p3", "p1", "p2"]
    assert [x.label for x in runner._round_robin_specs("A_CHILD", specs)] == ["p1", "p2", "p3"]


def test_round_robin_single_provider_is_unchanged():
    runner._rr_reset()
    specs = [Spec("only")]
    assert [x.label for x in runner._round_robin_specs("A", specs)] == ["only"]


def test_round_robin_four_provider_sequence_mixed_roles():
    runner._rr_reset()
    specs = [Spec("1"), Spec("2"), Spec("3"), Spec("4")]
    roles = ["A", "B_VERIFY", "A", "B_VERIFY", "A_MERGE", "B_WORKER", "A", "B_VERIFY"]
    starts = [runner._round_robin_specs(r, specs)[0].label for r in roles]
    assert starts == ["1", "2", "3", "4", "1", "2", "3", "4"]


def test_role_missing_next_provider_starts_on_next_it_has():
    runner._rr_reset()
    p1, p2, p3 = Spec("p1"), Spec("p2"), Spec("p3")
    runner._round_robin_specs("A", [p1, p2, p3])
    # Cycle is now at p2; B only has p1 and p3, so it starts on p3.
    assert runner._round_robin_specs("B_VERIFY", [p1, p3])[0].label == "p3"
    assert runner._round_robin_specs("A", [p1, p2, p3])[0].label == "p1"


def test_provider_setup_failure_rotates_to_next(monkeypatch):
    runner._rr_reset()
    from dual_lobe_crewai.provider_control import ProviderSpec

    bad = ProviderSpec(model="bad/m", max_tokens=10, label="bad")
    good = ProviderSpec(model="good/m", max_tokens=10, label="good")
    monkeypatch.setattr(runner, "resolve_role_specs", lambda role: [bad, good])

    class L:
        def __init__(self, model):
            self.model = model

    def fake_make_llm(role, spec=None):
        if spec.model == "bad/m":
            raise ImportError("provider not installed")
        return L(spec.model)

    async def fake_call(agent, d, e):
        return agent.llm.model

    monkeypatch.setattr(runner, "make_llm", fake_make_llm)
    monkeypatch.setattr(runner, "_single_call", fake_call)

    class Agent:
        role = "planner"
        llm = None

    out = asyncio.run(runner.run_one(Agent(), "x", "y", role_key="A"))
    assert out == "good/m"


def test_fallback_keeps_its_own_location(monkeypatch):
    from dual_lobe_crewai.llm_factory import resolve_role_specs

    monkeypatch.setenv("DUAL_LOBE_A_MODEL", "openai/a-model")
    monkeypatch.setenv("DUAL_LOBE_A_BASE_URL", "https://a-host.example/v1")
    monkeypatch.setenv("DUAL_LOBE_A_API_KEY", "a-key")
    monkeypatch.setenv("DUAL_LOBE_B_MODEL", "openai/b-model")
    monkeypatch.setenv("DUAL_LOBE_B_BASE_URL", "https://b-host.example/v1")
    monkeypatch.setenv("DUAL_LOBE_B_API_KEY", "b-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("DUAL_LOBE_FALLBACKS", '[{"model":"openrouter/third"}]')

    for role in ("A", "B_VERIFY"):
        third = [s for s in resolve_role_specs(role) if s.model == "openrouter/third"][0]
        assert third.base_url is None
        assert third.api_key == "or-key"

    a = resolve_role_specs("A")[0]
    b = resolve_role_specs("B_VERIFY")[0]
    assert (a.base_url, a.api_key) == ("https://a-host.example/v1", "a-key")
    assert (b.base_url, b.api_key) == ("https://b-host.example/v1", "b-key")


def _four_slot_pool(monkeypatch):
    for name in ("GROQ_API_KEY", "GROQ_BASE_URL", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
                 "OPENAI_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DUAL_LOBE_A_MODEL", "openai/openai/gpt-oss-120b")
    monkeypatch.setenv("DUAL_LOBE_B_MODEL", "openai/openai/gpt-oss-120b")
    monkeypatch.setenv("DUAL_LOBE_A_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("DUAL_LOBE_B_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("DUAL_LOBE_A_API_KEY", "groq-key")
    monkeypatch.setenv("DUAL_LOBE_B_API_KEY", "groq-key")
    monkeypatch.setenv("OR_KEY_1", "or-1")
    monkeypatch.setenv("OR_KEY_2", "or-2")
    monkeypatch.setenv("OR_KEY_3", "or-3")
    m = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    monkeypatch.setenv(
        "DUAL_LOBE_FALLBACKS",
        '[' + ",".join(
            '{"model":"%s","api_key_env":"OR_KEY_%d","label":"openrouter-%d"}' % (m, i, i)
            for i in (1, 2, 3)
        ) + ']',
    )
    monkeypatch.setenv("DUAL_LOBE_CROSS_ROLE_FAILOVER", "false")


def test_fallbacks_keep_their_own_location(monkeypatch):
    from dual_lobe_crewai.llm_factory import resolve_role_specs

    _four_slot_pool(monkeypatch)
    for role in ("A", "B_VERIFY"):
        specs = resolve_role_specs(role)
        assert [(s.base_url, s.api_key) for s in specs] == [
            ("https://api.groq.com/openai/v1", "groq-key"),
            (None, "or-1"),
            (None, "or-2"),
            (None, "or-3"),
        ]


def test_same_model_different_keys_rotate_as_separate_slots(monkeypatch):
    from dual_lobe_crewai.llm_factory import resolve_role_specs

    _four_slot_pool(monkeypatch)
    runner._rr_reset()
    roles = ["A", "B_VERIFY", "A", "B_VERIFY", "A", "B_VERIFY", "A", "B_VERIFY"]
    starts = [runner._round_robin_specs(r, resolve_role_specs(r))[0].api_key for r in roles]
    assert starts == ["groq-key", "or-1", "or-2", "or-3"] * 2
