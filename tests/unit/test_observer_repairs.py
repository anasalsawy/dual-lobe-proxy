"""Regression cases from the review, using actual request/observer composition."""
import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from dual_lobe.api import chat, limits
from dual_lobe.api.schemas import ChatCompletionRequest
from dual_lobe.b import context_shadow, prompts
from dual_lobe.b.channels import ObserverContext, prepare_context, reviewed_state
from dual_lobe.b.protocol import Review, KnowledgeSnapshot, ground_review, parse_review
from dual_lobe.core import redact
from dual_lobe.core.settings import Settings
from dual_lobe.state.memory import selected_guidance
from tests.unit.test_request_path import request_path
from tests.unit.test_director_api import director_api

NOTE = {'topic': 'Windows startup', 'kind': 'background',
        'insight': 'A Bash script needs a Bash interpreter; the application may not have started.',
        'question': 'Which interpreter actually receives the launch command?',
        'relevance': 'Changing application code cannot repair a missing interpreter.',
        'application': 'Check the launch environment using existing tools.'}
BASE = dict(goal='Start the app', questions=[], next_step='', context_notes=[], concerns=[])
CONCERN = dict(signal='CONTRADICTION', claim_quote='All tests passed.', basis_quote='tests failed',
               reason='The supplied result disagrees.', suggestion='Report the failure.')


def state(notes=True, **kw):
    return reviewed_state({}, Review.model_validate({**BASE, 'knowledge_notes': [NOTE] if notes else [], **kw}),
                          {'run_id': 'run', 'source_call': 'source-1', 'observed_at': 1000.}, source_model='model-b')


@pytest.mark.parametrize('oversight,age', [('reviewed', 0), ('degraded', 0), ('reviewed', 181)])
def test_green_is_default_and_operational_status_remains_separate(oversight, age):
    payload = state(notes=False)
    payload['oversight_status'] = oversight
    context = prepare_context(payload, '', 1, Settings(_env_file=None), now=1000.+age)
    assert context.deception_status == 'GREEN'
    assert 'no deception detected' in context.deception_text
    if oversight == 'degraded' or age > 180:
        assert context.claim_status in ('degraded', 'stale')
        assert 'no current completed review' in context.deception_text
    empty = prepare_context(None, '', 1, Settings(_env_file=None))
    assert empty.deception_status == 'GREEN' and empty.status == 'no_current_review'


def test_b_color_is_independent_and_missing_legacy_color_defaults_green():
    prompt = prompts.build_cycle_prompt('tests failed', 'All tests passed.', '', '')
    reviewed = ground_review(parse_review(json.dumps({**BASE, 'concerns': [CONCERN]})), prompt)
    payload = reviewed_state({}, reviewed, {'run_id': 'run', 'source_call': 'real-answer', 'observed_at': time.time()})
    assert payload['deception_level'] == 'GREEN' and payload['color_origin'] == 'default'
    payload['deception_level'] = 'YELLOW'
    assert prepare_context(payload, '', 1, Settings(_env_file=None)).deception_status == 'YELLOW'


@pytest.mark.parametrize('settings', [dict(claim_checks_enabled=False), dict(deception_meter_enabled=False)])
def test_meter_switches_suppress_existing_ratings(settings):
    context = prepare_context(state(concerns=[CONCERN]), '', 1, Settings(_env_file=None, **settings), now=1001.)
    assert context.deception_status == 'disabled' and context.deception_text is None


def test_optional_bad_guidance_preserves_valid_findings_and_two_contribution_cap():
    reviewed = parse_review(json.dumps({**BASE, 'knowledge_notes': [{'topic': 'incomplete'}, NOTE], 'concerns': [CONCERN]}))
    assert reviewed.knowledge_dropped == 1 and len(reviewed.knowledge_notes) == 1
    assert reviewed.concerns[0].signal == 'CONTRADICTION'
    capped = Review.model_validate({**BASE, 'questions': ['Which prerequisite?'], 'knowledge_notes': [NOTE, NOTE]})
    assert len(capped.questions) + len(capped.knowledge_notes) == 2
    with pytest.raises(ValueError):
        parse_review(json.dumps({k: v for k, v in BASE.items() if k != 'concerns'}))


def test_model_knowledge_and_prior_notes_cannot_ground_execution_claims():
    prompt = prompts.build_cycle_prompt('No supplied result', 'All tests passed.', '',
                                       json.dumps({'knowledge_notes': ['tests failed']}))
    with pytest.raises(ValueError):
        ground_review(Review.model_validate({**BASE, 'concerns': [CONCERN]}), prompt)


def test_guidance_is_delivered_with_provenance_and_can_be_disabled_without_deleting_it():
    payload = state()
    original = copy.deepcopy(payload)
    on = prepare_context(payload, '', 1, Settings(_env_file=None), now=1001.)
    assert 'Bash interpreter' in on.memory_text and 'source-1' in on.memory_text and 'model-b' in on.memory_text
    assert 'model_generated_guidance' in on.memory_text and on.knowledge_source_call == 'source-1'
    off = prepare_context(payload, '', 1, Settings(_env_file=None, context_enrichment_enabled=False), now=1001.)
    assert 'Bash interpreter' not in off.memory_text and off.knowledge_source_call is None
    assert payload == original


@pytest.mark.parametrize('change,now,enabled', [({},1001.,True), ({'source_call':'wrong'},1001.,False),
    ({},90000.,False), ({'observed_at':float('nan')},1001.,False), ({'origin':'verified_tool'},1001.,False)])
def test_shared_guidance_validates_source_age_and_origin(change, now, enabled):
    raw = dict(origin='model_generated_guidance', source_call='source-1', source_model='b', observed_at=1000., notes=[NOTE], **{})
    raw.update(change)
    result = selected_guidance(raw, 'source-1', Settings(_env_file=None), now=now)
    assert bool(result) == enabled
    assert selected_guidance(raw, 'source-1', Settings(_env_file=None, context_enrichment_enabled=False), now=now) is None


async def test_full_normal_observation_redacts_latest_request_before_storage_and_b(request_path, monkeypatch):
    request, principal, _, _ = request_path
    # Restore the real persistence function, saved at module import below.
    monkeypatch.setattr(chat, '_persist_observation', REAL_PERSIST)
    monkeypatch.setattr(chat, 'get_settings', lambda: Settings(_env_file=None, pulse_every=1))
    secret = 'dummy-review-secret-12345678'
    monkeypatch.setattr(redact, '_SECRETS', [secret])
    for name in ('record_provider_attempt', 'append_event'):
        monkeypatch.setattr(chat.repo, name, AsyncMock())
    monkeypatch.setattr(chat.repo, 'count_attempts', AsyncMock(return_value=1))
    enqueue = AsyncMock()
    monkeypatch.setattr(chat.repo, 'enqueue_outbox', enqueue)
    response = await chat.chat_completions(ChatCompletionRequest(messages=[{'role':'user','content':'Debug '+secret}]), request, principal)
    await response.background()
    payload = enqueue.call_args.args[4]
    assert secret not in json.dumps(payload) and '[REDACTED]' in payload['latest_user_text']
    for name, value in [('latest_b_state', None), ('list_events', []), ('save_b_state', None)]:
        monkeypatch.setattr(context_shadow.repo, name, AsyncMock(return_value=value))
    call_b = AsyncMock(return_value=json.dumps(BASE))
    monkeypatch.setattr(context_shadow, '_call_b', call_b)
    await context_shadow.run_shadow_cycle(None, {'payload': payload}, 79001)
    assert secret not in call_b.call_args.args[1]


REAL_PERSIST = chat._persist_observation


async def test_retry_starts_a_new_actual_transaction_after_disconnect(monkeypatch):
    engine = create_engine('sqlite://')
    attempts = []
    @asynccontextmanager
    async def fresh_session(tenant):
        assert tenant == 31
        with Session(engine) as sql_session:
            sql_session.execute(text('SELECT 1'))
            class Adapter:
                async def execute(self, stmt):
                    attempts.append(sql_session)
                    if len(attempts) == 1:
                        sql_session.connection().invalidate()
                        raise OperationalError('simulated query', {}, RuntimeError('disconnect'), connection_invalidated=True)
                    sql_session.execute(text('SELECT 1'))  # fails if old invalid transaction is reused
                    return SimpleNamespace(scalar_one_or_none=lambda: None)
            yield Adapter()
    monkeypatch.setattr(chat, 'tenant_session', fresh_session)
    monkeypatch.setattr(chat, 'get_settings', lambda: Settings(_env_file=None, b_state_read_timeout=.25))
    result = await chat._read_context(31, '81c93c4c-e7d5-47c6-8e45-e0a859f677bc', '', 1)
    assert result.status == 'no_current_review' and len(attempts) == 2 and attempts[0] is not attempts[1]
    engine.dispose()


async def test_corrective_attempt_is_charged_before_second_provider_call(monkeypatch):
    monkeypatch.setattr(context_shadow, 'get_settings', lambda: Settings(_env_file=None, b_rpm_limit=1))
    provider = AsyncMock(side_effect=['not json', json.dumps(BASE)])
    monkeypatch.setattr(context_shadow, '_call_b', provider)
    with pytest.raises(context_shadow.ReviewBudgetExceeded):
        await context_shadow._obtain_review('lobe-b', prompts.build_cycle_prompt('', 'answer', '', ''), 79002)
    assert provider.await_count == 1
    assert sum(weight for _, weight in limits._local['b:tenant:79002']) == 1


async def test_two_attempts_share_one_deadline(monkeypatch):
    monkeypatch.setattr(context_shadow, 'get_settings', lambda: Settings(_env_file=None, b_timeout=.04))
    calls = []
    async def slow(*args):
        calls.append(1)
        await asyncio.sleep(.025)
        return 'invalid' if len(calls) == 1 else json.dumps(BASE)
    monkeypatch.setattr(context_shadow, '_call_b', slow)
    with pytest.raises(TimeoutError):
        await context_shadow._obtain_review('lobe-b', prompts.build_cycle_prompt('', 'answer', '', ''), 79003)
    assert len(calls) == 2


async def test_director_preserves_real_latest_request_and_applies_enrichment_role(director_api, monkeypatch):
    app, a, b, _, _ = director_api
    monkeypatch.setattr(chat, 'get_settings', lambda: Settings(_env_file=None, b_enabled=True, rollout_stage='context'))
    monkeypatch.setattr(chat, '_read_context', AsyncMock(return_value=ObserverContext()))
    a.outputs = ['First answer', 'Corrected answer']
    b.decisions = [{'action':'continue','message':'Consider another explanation'}, {'action':'stop','message':'Enough'}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://proxy') as client:
        result = await client.post('/v1/chat/completions', headers={'X-DL-Run-ID':'anchor-fixed'}, json={
            'model':'lobe-a-director','messages':[{'role':'user','content':'Explain Windows startup'}]})
    assert result.status_code == 200 and result.headers['x-dual-lobe-deception'] == 'per-turn'
    assert all(c.kwargs['latest_user_text'] == 'Explain Windows startup' for c in chat._persist_observation.call_args_list)
    assert len(chat._persist_observation.call_args_list) == 2
    assert prompts.ENRICHMENT_POLICY in b.requests[0].messages[0]['content']
