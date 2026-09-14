#!/usr/bin/env python3
"""Render an existing brief/prep JSON or text file. Never researches, sends, or changes memory."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import visual_brief


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input')
    parser.add_argument('--kind', choices=('brief', 'prep'), required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--preview', action='store_true')
    args = parser.parse_args()
    text = Path(args.input).read_text()
    if args.input.endswith('.json'):
        value = json.loads(text)
        text = value.get('brief_text') or value.get('prep_markdown') or value.get('brief_markdown') or ''
    deck = visual_brief.build(text, args.kind, args.preview)
    if not deck:
        print(json.dumps({'format': 'text', 'reason': 'no suitable card content'}))
        return
    try:
        manifest = visual_brief.render(deck, args.out)
    except (ImportError, OSError, ValueError):
        print(json.dumps({'format': 'text', 'reason': 'cards could not be rendered legibly'}))
        return
    print(json.dumps({'format': 'cards', 'manifest': str(Path(args.out) / deck['id'] / 'manifest.json'),
                      'summary': manifest['summary'], 'images': manifest['images']}))


if __name__ == '__main__':
    main()
