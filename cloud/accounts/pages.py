"""Small server-rendered setup surface; no scripts, trackers or credential URLs."""
from html import escape

CSS = '''
:root { color-scheme: light dark; font-family: system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
body { margin: 0; background: Canvas; color: CanvasText; }
main { box-sizing: border-box; max-width: 38rem; margin: 8dvh auto; padding: 2rem; }
h1 { font-size: 2rem; line-height: 1.15; text-wrap: balance; }
p { line-height: 1.55; text-wrap: pretty; }
form { margin: 1.25rem 0; }
button, .button { display: inline-block; box-sizing: border-box; padding: .75rem 1rem; min-height: 44px;
  font: inherit; border: 1px solid ButtonText; border-radius: .5rem; color: ButtonText; background: ButtonFace; }
button { cursor: pointer; }
button:focus-visible, a:focus-visible { outline: 3px solid Highlight; outline-offset: 4px; }
code { display: block; padding: 1rem 0; font-size: 1.1rem; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
footer { margin-top: 3rem; font-size: .9rem; }
.identity { overflow-wrap: anywhere; }
'''


def document(title, content):
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{escape(title)} · Sotto</title><link rel="stylesheet" href="/setup.css">'
            f'<main><p>Sotto Cloud</p><h1>{escape(title)}</h1>{content}</main></html>')


def form(path, label, csrf, **fields):
    inputs = ''.join(f'<input type="hidden" name="{escape(k, quote=True)}" value="{escape(str(v), quote=True)}">'
                     for k, v in {'csrf': csrf, **fields}.items())
    return f'<form method="post" action="{escape(path, quote=True)}">{inputs}<button>{escape(label)}</button></form>'


def setup(session, journey=None, intent=None):
    csrf = session['secret']['csrf']
    if not session['account_id']:
        return document('Your day, a little lighter.', '<p>Connect Google so Sotto can help with your email and calendar. '
                        'This preview is available to invited accounts.</p>' + form('/browser/signin', 'Connect Google', csrf) +
                        '<p>You choose Google permissions during sign-in. Mac sources are a separate step in Bridge.</p>'
                        '<footer>Cloud processes selected context on your hosted instance. '
                        '<a href="sotto-bridge://cloud">Open Sotto Bridge</a> to set up your Mac or choose self-host.</footer>')
    action = journey['next_action']
    states = {
        'wait_for_google': ('Connecting Google', 'Your sign-in succeeded. Sotto is finishing the connection to your instance.'),
        'retry_status': ('Checking your connection', 'Your instance could not be reached. Your setup is saved; check again shortly.'),
        'reconnect_google': ('Reconnect Google', 'Your last connection did not finish. Sign in again to continue your saved setup.'),
        'wait_for_messages': ('Google is connected', 'Messages setup is still being prepared. Your account is saved; check back here when it is available.'),
        'connect_messages': ('Meet Sotto in Messages', 'Send a short setup message, then confirm your Messages identity here.'),
        'wait_for_activation': ('Messages identity verified', 'Your conversation is verified. Sotto is finishing the delivery connection.'),
        'connect_source': ('Choose a context source', 'You can chat with Sotto. Connect email, calendar or a Mac source to receive briefs.'),
        'wait_for_brief': ('Preparing your first brief', 'Sotto will send it in Messages when it is ready. You can close this page.'),
        'ready': ('You’re connected', 'Sotto is ready in Messages. You can add Mac context in Bridge whenever you like.'),
    }
    title, description = states[action]
    content = f'<p>{escape(description)}</p>'
    if action == 'connect_messages':
        if not intent:
            content += form('/browser/link', 'Connect Messages', csrf)
        elif intent['status'] == 'observed':
            handle = escape(intent['candidate']['sender'])
            content += (f'<p class="identity">We received the setup message from <strong>{handle}</strong>. '
                        'Confirm only if this is your Messages identity.</p>' + form('/browser/confirm',
                        'Confirm my Messages identity', csrf, intent_id=intent['intent_id'], proof_revision=intent['proof_revision']))
            content += form('/browser/retry-link', 'Use a different Messages identity', csrf, intent_id=intent['intent_id'])
        else:
            number = escape(intent['number'], quote=True)
            content += (f'<p>Send this exact message to {number}:</p><code>{escape(intent["message"])}</code>'
                        f'<p><a class="button" href="sms:{number}">Open Messages</a></p>'
                        '<p>The link opens a composer. If the text is not filled in, copy the setup message above. '
                        'Return here after sending it.</p>')
    if action in ('connect_source', 'reconnect_google'):
        content += form('/browser/signin', 'Connect Google sources', csrf)
    content += '<p><a href="/setup">Check status</a></p>'
    content += '<footer><p><a href="sotto-bridge://cloud">Open Sotto Bridge</a> to add Mac sources.</p>'
    content += form('/browser/logout', 'Sign out', csrf) + '</footer>'
    return document(title, content)


def device_confirmation(code):
    return document('Finish connecting in Sotto Bridge',
        '<p>Google sign-in succeeded. Enter this code in the Sotto Bridge app on the Mac '
        'you are connecting. No Mac is connected until you do.</p>'
        f'<code>{escape(code)}</code>'
        '<p>Keep this code private. Do not send it to another person or enter it on a website.</p>')
