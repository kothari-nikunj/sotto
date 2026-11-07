"""The root Dockerfile is the packaged runtime image plus one self-host overlay, never a fork.

Railway builds the root Dockerfile by default, while the smoke runners and CI build
runtime/sotto-chat/Dockerfile.runtime. Docker has no include instruction, so the root file repeats the
runtime file's build block verbatim; this guard keeps that one implementation in two places.
"""
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[2]
ROOT_DOCKERFILE = AGENT_ROOT / 'Dockerfile'
RUNTIME_DOCKERFILE = AGENT_ROOT / 'runtime' / 'sotto-chat' / 'Dockerfile.runtime'
OVERLAY_INPUTS = ['!VERSION', '!docs/', 'docs/*', '!docs/playground-architecture.html',
                  '!docs/playground-feedback-loops.html']


def _instructions(path):
    return [line for line in path.read_text().splitlines()
            if line.strip() and not line.startswith('#')]


def _runtime_block():
    lines = _instructions(RUNTIME_DOCKERFILE)
    end = max(index for index, line in enumerate(lines) if line.startswith('ENTRYPOINT '))
    return lines[:end]


def test_root_dockerfile_starts_with_the_runtime_build_verbatim():
    block = _runtime_block()
    root = _instructions(ROOT_DOCKERFILE)
    assert root[:len(block)] == block, (
        'Dockerfile drifted from runtime/sotto-chat/Dockerfile.runtime; copy its block from the '
        'first FROM through the image checks')


def test_root_overlay_runs_the_selfhost_bootstrap_as_root_under_tini():
    overlay = _instructions(ROOT_DOCKERFILE)[len(_runtime_block()):]
    assert overlay == [
        'USER 0:0',
        'COPY docs/playground-architecture.html docs/playground-feedback-loops.html /app/trigger-receiver/static/',
        'COPY VERSION /app/VERSION',
        'ENTRYPOINT ["/usr/bin/tini", "--"]',
        'CMD ["python3", "-I", "-B", "/app/adapters/sotto/selfhost_bootstrap.py", "--run"]',
    ]


def test_root_context_is_the_runtime_context_plus_overlay_inputs():
    runtime = _instructions(RUNTIME_DOCKERFILE.with_name('Dockerfile.runtime.dockerignore'))
    root = _instructions(AGENT_ROOT / 'Dockerfile.dockerignore')
    anchor = runtime.index('!requirements.txt') + 1
    assert root == runtime[:anchor] + OVERLAY_INPUTS + runtime[anchor:]
