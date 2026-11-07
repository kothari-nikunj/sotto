"""Every runtime image build context excludes what the root .gitignore keeps private.

Docker reads a build context from disk, not from git, so a gitignored file that sits in a
working checkout (a filled-in adapters/sotto/.env, the Golden Corpus under evals/corpus/, an
identity map, a signing key) enters any context whose .dockerignore re-includes its directory,
and a broad `COPY` then bakes it into an image. This checks each runtime/sotto-chat
Dockerfile.*.dockerignore with Docker's own matching rules (last matching pattern wins; a
pattern also matches every path below a directory it matches), and checks that every COPY
source each Dockerfile needs is still in its context.
"""
from pathlib import Path
import re

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[2]
CHAT = AGENT_ROOT / 'runtime' / 'sotto-chat'

# Each private .gitignore entry, with paths a working checkout could hold for it. A new
# .gitignore line must be classified here (private sample or hygiene), so the contexts cannot
# silently fall behind it.
PRIVATE = {
    'adapters/sotto/.env': ['adapters/sotto/.env', 'adapters/sotto/.env.local',
                             'adapters/sotto/.env', 'sotto-chief-of-staff/.env.production'],
    '**/bridge-signing.key': ['adapters/sotto/bridge-signing.key'],
    '**/*signing.key': ['sotto-chief-of-staff/release-signing.key', 'runtime/trigger-receiver/x-signing.key'],
    '**/bridge-access-codes.log': ['runtime/trigger-receiver/bridge-access-codes.log'],
    'sotto-chief-of-staff/evals/corpus/': ['sotto-chief-of-staff/evals/corpus/corpus-v1/days/day-00.json',
                                           'sotto-chief-of-staff/evals/corpus/README.md'],
    '*.map.json': ['sotto-chief-of-staff/evals/identity.map.json', 'adapters/sotto/owner.map.json'],
}
# The publish CORPUS GUARD's names (tools/prepare-public-repo.sh) that no .gitignore line covers.
CORPUS_GUARD = ['sotto-chief-of-staff/evals/corpus-v2/days/day-01.json',
                'sotto-chief-of-staff/evals/labels.yaml', 'sotto-chief-of-staff/evals/labels.draft.yaml']
HYGIENE = {'**/.pytest_cache/', '**/__pycache__/', '*.pyc', 'sotto-bridge/core/target/', '.DS_Store',
           'sotto-bridge/app/Sotto Bridge.app/', 'sotto-bridge/app/Sotto-Bridge-*.dmg',
           'sotto-bridge/app/.build/', 'sotto-bridge/.last-ship', 'sotto-bridge/.notary-profile',
           '.venv/'}
CONTEXTS = [AGENT_ROOT / 'Dockerfile.dockerignore', *sorted(CHAT.glob('Dockerfile.*.dockerignore'))]


def _regex(pattern):
    """moby/patternmatcher's translation, for the syntax these files use."""
    pattern = pattern.strip('/')
    out, index = '', 0
    while index < len(pattern):
        if pattern.startswith('**/', index):
            out, index = out + '(?:.*/)?', index + 3
        elif pattern.startswith('**', index):
            out, index = out + '.*', index + 2
        elif pattern[index] == '*':
            out, index = out + '[^/]*', index + 1
        elif pattern[index] == '?':
            out, index = out + '[^/]', index + 1
        else:
            out, index = out + re.escape(pattern[index]), index + 1
    return re.compile(out + r'\Z')


def _rules(path):
    rules = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            negated = line.startswith('!')
            rules.append((negated, _regex(line[1:] if negated else line)))
    return rules


def _excluded(rules, relative):
    parts = relative.split('/')
    candidates = ['/'.join(parts[:end]) for end in range(1, len(parts) + 1)]
    excluded = False
    for negated, rule in rules:
        if any(rule.match(candidate) for candidate in candidates):
            excluded = not negated
    return excluded


def _gitignore_entries():
    return [line.strip() for line in (AGENT_ROOT / '.gitignore').read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def test_every_gitignore_entry_is_classified():
    unclassified = [entry for entry in _gitignore_entries() if entry not in PRIVATE and entry not in HYGIENE]
    assert not unclassified, ('classify these .gitignore entries as private (with sample paths) or '
                              f'hygiene in {Path(__file__).name}: {unclassified}')
    assert set(PRIVATE) <= set(_gitignore_entries())


def test_runtime_dockerfiles_each_have_a_context_filter():
    assert (AGENT_ROOT / 'Dockerfile.dockerignore').is_file()
    dockerfiles = sorted(path.name for path in CHAT.glob('Dockerfile.*') if path.suffix != '.dockerignore')
    assert dockerfiles and all((CHAT / (name + '.dockerignore')).is_file() for name in dockerfiles), dockerfiles


@pytest.mark.parametrize('context', CONTEXTS, ids=lambda path: path.name)
def test_runtime_context_excludes_private_files(context):
    rules = _rules(context)
    samples = [path for paths in PRIVATE.values() for path in paths] + CORPUS_GUARD
    leaked = [sample for sample in samples if not _excluded(rules, sample)]
    assert not leaked, f'{context.name} lets private files into the build context: {leaked}'


@pytest.mark.parametrize('context', CONTEXTS, ids=lambda path: path.name)
def test_runtime_context_still_holds_every_copy_source(context):
    rules = _rules(context)
    dockerfile = context.with_name(context.name[:-len('.dockerignore')])
    missing = []
    for line in dockerfile.read_text().splitlines():
        if not line.startswith('COPY ') or '--from=' in line:
            continue
        fields = [field for field in line.split()[1:] if not field.startswith('--')]
        for source in fields[:-1]:
            path = AGENT_ROOT / source
            files = [path] if path.is_file() else [item for item in path.rglob('*') if item.is_file()]
            if not any(not _excluded(rules, str(item.relative_to(AGENT_ROOT))) for item in files):
                missing.append(source)
    assert not missing, f'{context.name} excludes sources {dockerfile.name} copies: {missing}'
    if context.name == 'Dockerfile.test.dockerignore':
        # The shipped env template is a real tracked file, unlike the filled-in .env beside it.
        assert not _excluded(rules, 'adapters/sotto/.env.template')
