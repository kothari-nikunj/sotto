"""Build a disposable conversation scenario from an exact cross_source_plan probe report."""

import argparse
import base64
from datetime import datetime, timedelta
import json
from pathlib import Path


FIXTURE = Path(__file__).resolve().parents[3] / 'sotto-chief-of-staff/evals/fixtures/cross_source_plan.json'
MAX_BACKGROUND_CHARS = 16000


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Use a timezone-aware cutoff')
    return result


def build_scenario(case_id, report, *, read_cutoff=None, gallery=False):
    case = json.loads(FIXTURE.read_text())['cases'][case_id]
    prompt = report.get('prompt')
    sender = f"SenderEmail: {case['email']}"
    if not isinstance(prompt, str) or sender not in prompt.splitlines():
        raise ValueError('Probe report does not match the requested case')
    cutoff = instant(report['cutoff'])
    brief, arm = report['brief'], report['arm']
    if not isinstance(brief, str) or not brief.strip() or len(brief) > MAX_BACKGROUND_CHARS:
        raise ValueError('Expected exact nonempty brief delivery text within the conversation bound')
    if arm not in ('email-only', 'combined') or report.get('bindable_offer'):
        raise ValueError('Expected email-only or combined ordinary brief without typed offer metadata')
    later_offset = timedelta(hours=1)
    if read_cutoff is not None and (arm != 'combined' or read_cutoff < cutoff + later_offset):
        raise ValueError('Use a combined report and a read cutoff at least one hour after the brief cutoff')
    mail_time = cutoff - timedelta(hours=4)
    mail = {
        'id': 'fixture-plan', 'threadId': 'fixture-plan-thread', 'labelIds': ['INBOX'],
        'internalDate': str(int(mail_time.timestamp() * 1000)), 'snippet': case['email_text'],
        'payload': {'mimeType': 'text/plain', 'headers': [
            {'name': 'From', 'value': f"{case['name']} <{case['email']}>"},
            {'name': 'To', 'value': 'owner@example.test'},
            {'name': 'Subject', 'value': case['subject']},
            {'name': 'Date', 'value': mail_time.strftime('%a, %d %b %Y %H:%M:%S %z')},
        ], 'body': {'data': base64.urlsafe_b64encode(case['email_text'].encode()).decode()}},
    }
    sources = ['gmail']
    world = {'savedSourceGrants': sources, 'gmailMessages': {mail['id']: mail}}
    if arm == 'combined':
        sources.extend(('whatsapp', 'contacts'))
        phone, namesake = case['phone'], '+12025550142'
        world['contacts'] = [
            {'name': case['name'], 'phones': [phone], 'emails': [case['email']]},
            {'name': case['name'], 'phones': [namesake], 'emails': ['namesake@example.test']},
        ]
        messages = [(1, phone, 'whatsapp_text', timedelta(hours=-2)),
                    (2, phone, 'unrelated_text', timedelta(hours=-1)),
                    (3, namesake, 'namesake_text', timedelta(hours=-1))]
        if read_cutoff is not None:
            messages.append((4, phone, 'later_text', later_offset))
        world['whatsappMessages'] = [
            {'source_id': ident, 'contact_jid': number[1:] + '@s.whatsapp.net',
             'partner_name': case['name'], 'timestamp': (cutoff + offset).isoformat(),
             'text': case[field], 'is_from_me': False, 'is_group_chat': False}
            for ident, number, field, offset in messages
        ]
    world['files'] = {'config/managed-capabilities.json': json.dumps({
        'tenant_id': 'eval-tenant',
        'sources': {source: {'consented': True, 'connected': True} for source in sources},
    })}
    delivery = 'backgroundGalleryBefore' if gallery else 'backgroundBefore'
    checks = {'canonicalUnchanged': True, 'bridgeSends': 0, 'publicReads': 0}
    if arm == 'combined' and read_cutoff is None:
        checks['excludes'] = case['superseded_plan_wording']
    return {'name': f'cross-source-plan-{case_id}-{arm}', 'world': world,
            'turns': [{delivery: brief, 'text': 'yes', **checks},
                      {'text': 'Shorter please.', **checks}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, choices=json.loads(FIXTURE.read_text())['cases'])
    parser.add_argument('--brief-report', type=Path, required=True, help='Existing fictional probe JSON with exact brief, arm and cutoff')
    parser.add_argument('--read-cutoff', type=instant, help='Later frozen source cutoff, without changing the delivered brief')
    parser.add_argument('--gallery', action='store_true', help='Deliver the exact brief with the production four-card renderer')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    scenario = build_scenario(args.case, json.loads(args.brief_report.read_text()), read_cutoff=args.read_cutoff, gallery=args.gallery)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(scenario, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


if __name__ == '__main__':
    main()
