"""External DocSend HTTP/model payloads for the actual attended reader."""
import base64
import io
import json
import os
import pathlib
from urllib.parse import urlsplit

JPEG = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD3+iiigD//2Q==')


def configure(receiver):
    root = pathlib.Path(os.environ['SOTTO_DATA'])
    os.environ['SOTTO_USER_EMAIL'] = 'owner@example.com'
    os.environ['GOOGLE_AI_API_KEY'] = 'synthetic-gemini'
    os.environ['SOTTO_MODEL_PROXY_URL'] = 'https://proxy.synthetic.invalid'
    os.environ['SOTTO_MODEL_PROXY_TOKEN'] = 'synthetic-proxy-token'
    receiver._runtime_source_authority = lambda: {'version':2,'complete':True,'revision':'a'*64,
        'bridge_sources':[],'google_sources':[],'connector_sources':[]}
    deck = receiver.RUNTIME_WEB_READER._deck()

    def note(kind, **metadata):
        with (root/'docsend-provider.jsonl').open('a') as log:
            log.write(json.dumps({'kind':kind, **metadata})+'\n')

    class Http:
        def __init__(self, target, guard):
            self.target = deck.normalized_url(target)
            self.guard = guard
            self.submitted = False

        def remaining(self):
            return 180

        def get(self, url, accept='text/html'):
            self.guard()
            if url == self.target:
                note('view')
                if (root/'docsend-unknown').exists():
                    raise deck.DocSendHeldError('synthetic response loss')
                if self.submitted:
                    return b'<title>Cedar deck | DocSend</title>Deck'
                return b'<title>Cedar deck | DocSend</title><form action="/gate"><input name="authenticity_token" value="synthetic"><input name="link_auth_form[email]" value=""></form>'
            if '/page_data/' in url:
                return json.dumps({'imageUrl':'https://assets.synthetic.invalid/cedar.jpg'}).encode() if urlsplit(url).path.endswith('/1') else b'{}'
            assert url == 'https://assets.synthetic.invalid/cedar.jpg'
            note('image')
            return JPEG

        def post_form(self, url, fields, referer):
            self.guard()
            assert url == 'https://docsend.com/gate'
            assert referer == self.target
            assert fields['link_auth_form[email]'] == 'owner@example.com'
            note('gate',configured_email_matches=True,passcode_present='link_auth_form[passcode]' in fields)
            self.submitted = True
            return b''

    deck.AttendedHttp = Http

    class Response(io.BytesIO):
        def read1(self, count=-1):
            return self.read(count)

    def model_http(request, timeout):
        assert request.full_url.startswith('https://proxy.synthetic.invalid/')
        body = json.loads(request.data)
        assert len(body['contents'][0]['parts']) == 2
        assert base64.b64decode(body['contents'][0]['parts'][1]['inline_data']['data']) == JPEG
        note('vision',inline_pages=1)
        return Response(json.dumps({'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'Page 1: Cedar revenue is $10. SUMMARY: Cedar deck evidence.'}]}}], 'usageMetadata':{'promptTokenCount':10,'candidatesTokenCount':12}}).encode())

    deck.urllib.request.urlopen = model_http
    return receiver.DASHBOARD.mint_session()
