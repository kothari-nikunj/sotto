"""Fictional plan-comments selection probe using the existing golden projection and brief composer."""

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import run_golden as golden
from gemini_transport import brief_model_preflight, credential  # noqa: E402


FIXTURE = Path(__file__).with_name('fixtures') / 'cross_source_plan.json'
ARMS = ('email-only', 'combined', 'unavailable', 'revoked', 'completed', 'quiet', 'namesake-only')


def inputs(case, cutoff, arm):
    def stamp(hours):
        return (cutoff + timedelta(hours=hours)).isoformat()

    email = case['email']
    name = case['name']
    phone = case['phone']
    mailbox = 'owner@example.test'
    thread = 'fixture-plan-thread'
    request = {'id': 'fixture-plan', 'threadId': thread, 'senderEmail': email,
               'from': f'{name} <{email}>', 'to': mailbox, 'subject': case['subject'],
               'date': stamp(-4), 'body': case['email_text']}
    response = {'id': 'fixture-reply', 'threadId': thread, 'from': mailbox, 'to': email,
                'subject': 'Re: ' + case['subject'], 'isSent': True,
                'date': stamp(-1 if arm == 'completed' else 1), 'body': case['sent_text']}
    messages = []
    for key, number, body, hours in (
            ('plan', phone, case['whatsapp_text'], -2),
            ('unrelated', phone, case['unrelated_text'], -1),
            ('namesake', '+12025550142', case['namesake_text'], -1),
            ('later', phone, case['later_text'], 1)):
        messages.append({'rowid': key, 'contact_jid': number[1:] + '@s.whatsapp.net',
                         'partner_name': name, 'timestamp': stamp(hours), 'text': body})
    # Freeze the synthetic provider's read boundary before building the prompt.
    emails = [row for row in (request, response) if datetime.fromisoformat(row['date']) <= cutoff]
    observed = [row for row in messages if datetime.fromisoformat(row['timestamp']) <= cutoff]
    if arm == 'namesake-only':
        observed = [row for row in observed if row['rowid'] == 'namesake']
    status = {'unavailable': 'error', 'revoked': 'disabled'}.get(arm, 'ok')
    local = {'contacts': [{'name': name, 'phones': [phone], 'emails': [email]},
                          {'name': name, 'phones': ['+12025550142'], 'emails': ['namesake@example.test']}],
             'source_status': {'whatsapp': status, 'contacts': 'ok'},
             'whatsapp': observed if arm in ('combined', 'revoked', 'completed', 'namesake-only') else []}
    return {'type': 'morning', 'now': cutoff.strftime('%Y-%m-%d %H:%M:%S'),
            'google': {'emails': [] if arm == 'quiet' else emails, 'events': [],
                       'userEmail': mailbox, 'userTimezone': '+00:00'}, 'local': local}


def scripted(case, arm, prompts):
    def model(prompt, _inputs):
        prompts.append(prompt)
        if arm in ('completed', 'quiet'):
            return json.dumps({'brief_markdown': 'Nothing needs a reply.', 'actions': []})
        email, name, subject = (case[key] for key in ('email', 'name', 'subject'))
        ask = f'Send comments on the {subject}'
        return json.dumps({
            'brief_markdown': f'## Should Handle Today\n- **{name}**<!--id:{email}|ch:email-->: {ask}.',
            'actions': [{'type': 'reply', 'channel': 'email', 'contactName': name,
                         'contactIdentifier': email, 'contextAsk': ask, 'helpKind': 'draft'}],
        })
    return model


def run(case, cutoff, arm, *, live=False):
    from source_context import record_bridge_status

    prompts = []
    with TemporaryDirectory(prefix='sotto-fictional-plan-') as root, patch.dict(
            os.environ, {'SOTTO_DATA': root, 'SOTTO_TIMEZONE': '+00:00'}):
        snapshot = inputs(case, cutoff, arm)
        if os.environ.get('SOTTO_DEPLOYMENT_MODE') == 'managed':
            config = Path(root) / 'config'
            config.mkdir()
            (config / 'managed-capabilities.json').write_text(json.dumps({
                'tenant_id': os.environ.get('SOTTO_TENANT_ID'),
                'sources': {source: {'consented': not (source == 'whatsapp' and arm == 'revoked'),
                                     'connected': True} for source in ('gmail', 'contacts', 'whatsapp')}}))
        record_bridge_status(snapshot['local'], data_root=root)
        projected = golden._project_inputs(snapshot)
        if live:
            if not credential():
                raise RuntimeError('Set the configured native brief credential before live replay')
            if os.environ.get('SOTTO_LLM_STUB'):
                raise RuntimeError('Unset SOTTO_LLM_STUB before live replay')
            brief_model_preflight()

            def observed(prompt, request, **kwargs):
                prompts.append(prompt)
                return golden.gemini_call(prompt, request, **kwargs)

            out = golden.cb.compose(projected, llm=observed, critic=True)
        else:
            out = golden.cb.compose(projected, llm=scripted(case, arm, prompts), critic=False)
    return {
        'arm': arm, 'cutoff': cutoff.isoformat(), 'mode': 'live' if live else 'scripted',
        'whatsapp_status': projected['local'].get('source_status', {}).get('whatsapp'),
        'source_ids': {'gmail': [row['id'] for row in projected['google']['emails']],
                       'whatsapp': [row['rowid'] for row in projected['local'].get('whatsapp', [])]},
        'prompt': prompts[0],
        'brief': out.get('brief_text'), 'actions': out.get('actions', []),
        'bindable_offer': {key: out[key] for key in ('_prep_offer', '_contextual_offer') if key in out},
    }


def main():
    fixture = json.loads(FIXTURE.read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, choices=fixture['cases'])
    parser.add_argument('--arm', required=True, choices=ARMS)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--live', action='store_true', help='Call the configured native brief model')
    args = parser.parse_args()
    with args.output.open('x', encoding='utf-8') as output:
        try:
            result = run(fixture['cases'][args.case], datetime.fromisoformat(fixture['cutoff']),
                         args.arm, live=args.live)
        except Exception as error:
            json.dump({'case': args.case, 'arm': args.arm, 'cutoff': fixture['cutoff'],
                       'mode': 'live' if args.live else 'scripted',
                       'error_type': type(error).__name__}, output, indent=2)
            output.write('\n')
            raise
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write('\n')


if __name__ == '__main__':
    main()
