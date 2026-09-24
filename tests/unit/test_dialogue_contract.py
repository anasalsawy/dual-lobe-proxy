from dual_lobe.coauthor.handler import _normalize_downstream, _b_visible_message, _b_private_message
from dual_lobe.coauthor.prompts import COAUTHOR_B_UPSTREAM, COAUTHOR_B_DOWNSTREAM


def test_b_always_visible_on_green_pass():
    d = _normalize_downstream({
        'action':'PASS',
        'verification': {'level':'GREEN','to_user':'','to_a':'','rationale':'clean','concerns':[]}
    })
    assert d['verification']['to_user']
    assert 'checked A' in d['verification']['to_user']
    assert _b_visible_message(d)


def test_downstream_has_distinct_conversation_channels():
    d = _normalize_downstream({
        'action':'ADD',
        'reply_to_a':'Yes, A, I agree.',
        'reply_to_user':'My direct answer to you.',
        'coauthor_to_a':'Check the timeout.',
        'coauthor_to_user':'Also consider provider latency.',
        'verification': {
            'level':'YELLOW',
            'to_a':'Do not claim success yet.',
            'to_user':'Verification: success remains unverified.',
            'rationale':'No tool result.',
            'concerns':[]
        }
    })
    visible = _b_visible_message(d)
    private = _b_private_message(d)
    assert 'Yes, A' in visible
    assert 'My direct answer' in visible
    assert 'provider latency' in visible
    assert 'success remains unverified' in visible
    assert 'Check the timeout' in private
    assert 'Do not claim success yet' in private


def test_prompts_encode_free_conversation_and_guard():
    assert 'answer the user DIRECTLY as B' in COAUTHOR_B_DOWNSTREAM
    assert 'If A directly spoke to you' in COAUTHOR_B_DOWNSTREAM
    assert 'ALWAYS verify' in COAUTHOR_B_DOWNSTREAM
    assert 'MUST NEVER be empty' in COAUTHOR_B_DOWNSTREAM
    assert 'Remove only material whose conversational purpose is specifically talking' in COAUTHOR_B_UPSTREAM
