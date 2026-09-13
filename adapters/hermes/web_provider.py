"""Hermes web-tool protocol over Sotto's shared research capabilities.

Installed as a user plugin by web_config.py; provider credentials and routing remain in Sotto.
"""
from pathlib import Path
import os
import sys

from agent.web_search_provider import WebSearchProvider


def research_module():
    path = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))) / 'skills/sotto/_shared/scripts'
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    import web_research  # noqa: PLC0415 — skills are installed after the adapter on self-host
    return web_research


class SottoWebProvider(WebSearchProvider):
    @property
    def name(self):
        return 'sotto'

    def is_available(self):
        try:
            return bool(research_module().provider_chain('web_search'))
        except (ImportError, OSError, RuntimeError, ValueError):
            return False

    def supports_extract(self):
        return True

    def search(self, query, limit=5):
        result = research_module().research(query)
        if not result.get('text'):
            return {'success': False, 'error': 'Sotto web search is unavailable. No verified result was returned.'}
        # Keep the shared answer and its citations together. A synthesized answer is not
        # attributed verbatim to the first citation or presented as that page's extracted text.
        citations = result.get('citations') or []
        web = [{'title': c.get('title') or c['uri'], 'url': c['uri'],
                'description': c.get('title') or '', 'position': i + 1}
               for i, c in enumerate(citations[:max(1, min(int(limit), 100))]) if c.get('uri')]
        return {'success': True, 'data': {'web': web, 'answer': result['text'],
                                         'provider': result.get('provider')}}

    def extract(self, urls, **kwargs):
        results = []
        for url in urls:
            result = research_module().fetch_url(url)
            text = result.get('text') or ''
            row = {'url': url, 'title': result.get('title') or '', 'content': text,
                   'metadata': {'provider': result.get('provider')}}
            if not text:
                row['error'] = 'Sotto could not read this page.'
            results.append(row)
        return results


def register(ctx):
    ctx.register_web_search_provider(SottoWebProvider())
