"""The consent page says exactly what Allow grants: today's read tools in plain words, that the
read:all grant includes read tools added later, the 30-day limit, and no choice it does not offer."""
import html
from http import HTTPStatus
import json
from urllib.parse import urlencode

import client_mcp
import client_oauth as oauth
from test_client_oauth import BASE, CALLBACK, CHALLENGE, server  # noqa: F401 - pytest fixture


def _consent_page(call):
    status, _, raw = call('/client-oauth/register', 'POST', {
        'client_name': 'Hosted fixture', 'redirect_uris': [CALLBACK], 'token_endpoint_auth_method': 'none'})
    query = dict(client_id=json.loads(raw)['client_id'], redirect_uri=CALLBACK, response_type='code',
        code_challenge_method='S256', code_challenge=CHALLENGE, resource=BASE + '/client-mcp',
        state='s', scope=oauth.SCOPE)
    _, headers, _ = call('/client-oauth/authorize?' + urlencode(query))
    _, headers, _ = call('/app/login', 'POST', {'code': 'owner-test-code'},
                         {'Cookie': headers['Set-Cookie'].split(';')[0]}, form=True)
    status, _, page = call(headers['Location'], headers={'Cookie': headers['Set-Cookie'].split(';')[0]})
    assert status == HTTPStatus.OK
    return html.unescape(page)


def test_consent_lists_todays_tools_future_tools_and_expiry(server):  # noqa: F811
    page = _consent_page(server[0])
    tools = client_mcp.read_tools()
    assert f'The {len(tools)} read-only tools this grants today' in page
    for name in tools:
        assert oauth.plain_tool_summary(name) in page, name
    assert 'read:all' in page and 'includes read-only tools Sotto adds later' in page
    assert 'Access lasts up to 30 days' in page


def test_consent_makes_no_claim_of_a_choice_the_page_does_not_offer(server):  # noqa: F811
    page = _consent_page(server[0])
    assert 'You choose what Sotto shares' not in page
    assert 'only the sources you have turned on in Sotto Settings' in page
