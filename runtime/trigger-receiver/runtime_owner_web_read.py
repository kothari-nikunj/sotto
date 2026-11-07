"""Closed original-owner URL read, never an arbitrary script or crawler."""
import socket
import re
from urllib.parse import urlsplit

from runtime_read_admission import read_cancelled, owner_read_request, contextual_read_request

from owner_tool_admission import validate_tool_admission, UnavailableError

_TOKEN = re.compile(r'https?://[^\s<>"\'`]+', re.IGNORECASE)


def owner_urls(text):
    result = []
    for token in _TOKEN.findall(text):
        while token:
            last = token[-1]
            if last in '.,!;:' or (last in ')]}' and token.count(last) > token.count({')': '(', ']': '[', '}': '{'}[last])):
                token = token[:-1]
            else:
                break
        result.append(token)
    return result


def _matches_owner_token(tokens, url):
    # Empty-query prose punctuation only; original args, hashes and receipts stay exact.
    return url in tokens or ('?' not in url and '#' not in url and url + '?' in tokens)


def validate_args(args, text):
    if type(args) is dict and args.get('operation') in ('docsend', 'propose_docsend', 'confirm_docsend'):
        return validate_docsend_args(args, text)
    if type(args) is not dict or set(args) not in ({'url', 'source_revision'}, {'url', 'source_revision', 'reply_target'}):
        raise ValueError('web read arguments invalid')
    revision = args['source_revision']
    if revision is not None and (type(revision) is not str or re.fullmatch('[a-f0-9]{64}', revision) is None):
        raise ValueError('web revision invalid')
    url = args['url']
    if type(url) is not str:
        raise ValueError('web URL invalid')
    if read_cancelled(text):
        raise ValueError('web read was cancelled')
    tokens = owner_urls(text)
    reply_target = args.get('reply_target')
    if 'reply_target' in args:
        if (type(reply_target) is not dict
                or set(reply_target) != {'accepted_dispatch_id', 'provider_message_id', 'text', 'source_revision'}
                or revision is None or reply_target.get('source_revision') != revision or tokens
                or not (contextual_read_request(text) or owner_read_request(text))):
            raise ValueError('web reply target invalid')
        for key in ('accepted_dispatch_id', 'provider_message_id'):
            value = reply_target[key]
            if (type(value) is not str or not value
                    or any(ord(c) < 32 or 127 <= ord(c) <= 159 or ord(c) in (0x2028, 0x2029)
                           or 0xD800 <= ord(c) <= 0xDFFF for c in value)
                    or len(value.encode('utf-16-le')) // 2 > 512):
                raise ValueError('web reply identity invalid')
        quote = reply_target['text']
        if (type(quote) is not str or not quote or any(0xD800 <= ord(c) <= 0xDFFF for c in quote)
                or len(quote.encode('utf-16-le')) // 2 > 16000
                or len(set(owner_urls(quote))) != 1 or not _matches_owner_token(owner_urls(quote), url)):
            raise ValueError('web reply payload invalid')
        tokens = owner_urls(quote)
    if (type(url) is not str or not url or len(url) > 2048 or not _matches_owner_token(tokens, url)
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in url)):
        raise ValueError('web URL invalid')
    parsed = urlsplit(url)
    host = (parsed.hostname or '').rstrip('.').lower()
    if (parsed.scheme.lower() not in ('http', 'https') or not host or parsed.username is not None
            or parsed.password is not None or host == 'localhost' or host.endswith(('.localhost', '.local'))
            or host == 'docsend.com' or host.endswith('.docsend.com') or '.' not in host and ':' not in host):
        raise ValueError('web destination invalid')
    parsed.port  # validate the literal port without resolving a destination
    try:
        socket.inet_aton(host)  # local numeric parser, never DNS; includes legacy short/hex spellings
    except OSError:
        pass
    else:
        raise ValueError('web destination invalid')
    labels = host.encode('idna').decode('ascii').split('.')
    if (len(labels) < 2 or any(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label) is None for label in labels)
            or re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', labels[-1], re.IGNORECASE)):
        raise ValueError('web destination invalid')



def validate_docsend_args(args, text):
    operation = args.get("operation")
    if operation == "confirm_docsend":
        if set(args) != {"operation", "review", "proposal"}:
            raise ValueError("DocSend confirmation invalid")
        return
    if operation == "docsend" and read_cancelled(text):
        raise ValueError("web read was cancelled")
    keys = {"operation", "url", "source_revision"}
    if operation == "propose_docsend":
        keys.add("fresh")
        if type(args.get("fresh")) is not bool:
            raise ValueError("DocSend freshness invalid")
    if set(args) not in (keys, keys | {"passcode"}):
        raise ValueError("DocSend arguments invalid")
    if type(args.get("source_revision")) is not str or not re.fullmatch("[a-f0-9]{64}", args["source_revision"]):
        raise ValueError("DocSend revision invalid")
    url = args.get("url")
    if (type(url) is not str or not url or len(url) > 2048
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in url)
            or not re.fullmatch(r"https?://(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*docsend\.com/view/[A-Za-z0-9_-]+/?(?:[?#][^\s]*)?", url, re.I)
            or operation == "docsend" and url not in owner_urls(text)):
        raise ValueError("DocSend target invalid")
    if "passcode" in args:
        value = args["passcode"]
        if (type(value) is not str or not value or len(value.encode("utf-8")) > 512
                or value not in text or any(ord(c) < 32 or 127 <= ord(c) <= 159
                    or 0xD800 <= ord(c) <= 0xDFFF or c in "\u2028\u2029" for c in value)):
            raise ValueError("DocSend passcode invalid")

def perform(body, *, bearer, root, policy, data_root, capability, activation, reader,
            telegram_verifier=None, chat_client=None, cancelled=None):
    proof = validate_tool_admission(body, operations=('execute', 'inspect', 'preview'), kind='owner_web_read',
        domain='sotto.owner-web-read.v1', bearer=bearer, root=root, capability=capability,
        policy=policy, data_root=data_root, activation=activation,
        telegram_verifier=telegram_verifier, chat_client=chat_client)
    validate_args(body['args'], body['text'])
    if body['operation'] == 'preview' and body['args'].get('operation') != 'propose_docsend':
        raise ValueError('web preview invalid')
    if body['operation'] == 'execute' and body['args'].get('operation') == 'propose_docsend':
        raise ValueError('DocSend proposal invalid')
    if body['identity']['channel'] != 'photon':
        raise UnavailableError('web read profile unavailable')
    return reader.perform(data_root, proof, operation=body['operation'],
                          authority=policy['_source_authority'], cancelled=cancelled, owner_text=body['text'])
