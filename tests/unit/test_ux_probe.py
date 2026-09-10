import httpx
import pytest
from tools.ux_probe import stream_turn


def event(data):
    import json
    return 'data: ' + json.dumps(data) + '\n\n'


@pytest.mark.parametrize('response', [
    httpx.Response(500, json={'error':'down'}),
    httpx.Response(200, content=event({'error': {'message': 'interrupted'}})+'data: [DONE]\n\n'),
    httpx.Response(200, content=event({'choices':[{'delta':{'content':'partial'}}]})+'data: [DONE]\n\n'),
    httpx.Response(200, content=event({'choices':[{'delta':{'content':'partial'},'finish_reason':'length'}]})+'data: [DONE]\n\n'),
    httpx.Response(200, content=event({'choices':[{'delta':{'content':'partial'},'finish_reason':'stop'}]})),
])
async def test_errors_and_incomplete_streams_are_not_successful_measurements(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response), base_url='http://test') as client:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            await stream_turn(client, '/chat/completions', [], 'model')


async def test_null_usage_is_optional_and_zero_usage_is_preserved():
    wire = event({'usage':None,'choices':[{'delta':{'content':'OK'},'finish_reason':'stop'}]})
    wire += event({'usage':{'prompt_tokens':0,'completion_tokens':0},'choices':[]}) + 'data: [DONE]\n\n'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=wire)), base_url='http://test') as client:
        result, _ = await stream_turn(client, '/chat/completions', [], 'model')
    assert result['ok'] and result['text'] == 'OK' and result['tokens_in'] == result['tokens_out'] == 0
