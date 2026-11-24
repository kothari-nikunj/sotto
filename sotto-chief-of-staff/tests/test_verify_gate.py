"""The shared gate (tools/verify.py) runs every hermetic suite the tree carries."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('release_verify_gate', ROOT / 'sotto-chief-of-staff/tools/verify.py')
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


def test_every_smoke_unit_suite_runs_in_the_gate(monkeypatch):
    monkeypatch.setattr(VERIFY, 'pytest_shards', lambda *_args: [])
    gated = {argv[-2] for steps in VERIFY.lanes(ROOT).values() for _cwd, argvs in steps
             for argv in argvs if '-m' in argv and 'pytest' in argv}
    carried = {str(path.parent.relative_to(ROOT)) for path in (ROOT / 'smoke').glob('*/test_*.py')}
    assert carried <= gated, sorted(carried - gated)


def test_test_module_basenames_are_unique(tmp_path):
    VERIFY.verify_unique_test_basenames(ROOT)
    for directory in ('a', 'b'):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / 'test_same.py').write_text('')
    with pytest.raises(SystemExit, match='share a test module name'):
        VERIFY.verify_unique_test_basenames(tmp_path)


def test_node_shards_run_every_test_file_exactly_once():
    chat = ROOT / 'runtime/sotto-chat'
    if not (chat / 'package.json').is_file():
        pytest.skip('this tree carries no chat runtime')
    node = sorted(str(p.relative_to(chat)) for p in (chat / 'test').glob('*.test.ts'))
    for count in (1, 3, VERIFY.SHARDS):
        shards = VERIFY.node_test_shards(chat, count)
        assert all('--test-concurrency=1' in argv for argv in shards)
        dealt = [arg for argv in shards for arg in argv if arg.endswith('.test.ts')]
        assert sorted(dealt) == node and len(dealt) == len(set(dealt))


def test_python_shards_match_recursive_pytest_collection(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[tool.pytest.ini_options]\nnorecursedirs = ["ignored"]\n')
    fixtures = (*VERIFY.GENERATOR_TEST_FILES,
                'tests/nested/test_nested.py', 'tests/nested/suffix_test.py', 'tests/ignored/test_fixture.py')
    for name in fixtures:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('def test_fixture():\n    pass\n')

    run = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', 'tests'],
                         cwd=tmp_path, text=True, stdout=subprocess.PIPE, check=True)
    expected = [line.split('::', 1)[0] for line in run.stdout.splitlines() if line.startswith('tests/')]
    assert sorted(expected) == sorted(fixtures[:-1])
    shards = VERIFY.pytest_shards(tmp_path, 3)
    dealt = [arg for argv in shards for arg in argv if arg.startswith('tests/')]
    assert sorted(dealt) == sorted(expected)
    assert all(sum(name in argv for name in VERIFY.GENERATOR_TEST_FILES) <= 1 for argv in shards)


def test_a_failing_shard_fails_the_gate_with_its_whole_output(tmp_path, capfd):
    def say(text, code=0):
        return [sys.executable, '-c', f'print({text!r}); raise SystemExit({code})']
    later = tmp_path / 'later'
    failures = VERIFY.run_lanes({
        'a': [(tmp_path, [say('shard one'), say('FAILED tests/test_x.py::test_y - boom', 3)]),
              (tmp_path, [[sys.executable, '-c', f'open({str(later)!r}, "w")']])],
        'b': [(tmp_path, [say('other lane')])]})
    assert len(failures) == 1 and failures[0].startswith('a shard 2/2:')
    assert failures[0].endswith('\n    FAILED tests/test_x.py::test_y - boom'), 'the tail names the failure'
    assert not later.exists(), 'no new step starts after a failure'
    out = capfd.readouterr().out
    assert 'test_y - boom\n[verify] a shard 2/2: FAILED (exit 3)' in out and 'other lane' in out


def test_the_node_lane_is_npm_run_check_with_its_tests_sharded(monkeypatch):
    monkeypatch.setattr(VERIFY, 'pytest_shards', lambda *_args: [])
    chat = ROOT / 'runtime/sotto-chat'
    if not (chat / 'package.json').is_file():
        pytest.skip('this tree carries no chat runtime')
    check = json.loads((chat / 'package.json').read_text())['scripts']['check']
    assert check == 'npm run typecheck && npm test && npm run build', 'update verify.lanes with the check script'
    steps = VERIFY.lanes(ROOT)['node']
    assert [argvs for _cwd, argvs in steps][::2] == [[['npm', 'run', 'typecheck']], [['npm', 'run', 'build']]]
    assert steps[1][1] == VERIFY.node_test_shards(chat, VERIFY.SHARDS)


def test_the_failure_summary_keeps_pytest_and_node_result_lines():
    output = ('noise\n.F.\nFAILED tests/test_a.py::test_b - AssertionError\n1 failed, 2 passed in 3.10s\n'
              '\u2714 fine (1ms)\n\u2716 broken (2ms)\n\u2139 tests 3\n\u2139 pass 2\n\u2139 fail 1\n'
              '\u2716 failing tests:\n\u2716 broken (2ms)\n').encode()
    assert VERIFY._summary(output) == [
        'FAILED tests/test_a.py::test_b - AssertionError', '1 failed, 2 passed in 3.10s', '\u2716 broken (2ms)',
        '\u2139 tests 3', '\u2139 pass 2', '\u2139 fail 1', '\u2716 failing tests:']


def test_the_fast_check_runs_only_cheap_gate_commands(monkeypatch):
    monkeypatch.setattr(VERIFY, 'RUNNER', False)
    def flat(lanes):
        return [argv for steps in lanes.values() for _cwd, argvs in steps for argv in argvs]
    full, fast = flat(VERIFY.lanes(ROOT)), flat(VERIFY.fast_lanes(ROOT))
    drift = [argv for argv in fast if argv[-1] == 'tests/test_docs_drift.py']
    assert len(drift) == 1 and ['npm', 'run', 'typecheck'] in fast
    assert any('ruff' in argv for argv in fast) and any('tools/check-file-size.mjs' in argv for argv in fast)
    # Everything else is a command the full gate also runs, and no test shard or build is among them.
    assert all(argv in full for argv in fast if argv not in drift)
    shards = flat({'x': [(ROOT, VERIFY.pytest_shards(ROOT / 'sotto-chief-of-staff', 2)),
                         (ROOT, VERIFY.node_test_shards(ROOT / 'runtime/sotto-chat', 2))]})
    assert not any(argv in shards for argv in fast) and ['npm', 'run', 'build'] not in fast


def test_the_other_python_suites_are_a_lane_of_their_own(monkeypatch):
    monkeypatch.setattr(VERIFY, 'RUNNER', False)
    lanes = VERIFY.lanes(ROOT)
    def suites_in(name):
        return {argv[-2] for _cwd, argvs in lanes[name] for argv in argvs if argv[-1] == '-q' and 'pytest' in argv}
    assert 'runtime/trigger-receiver' in suites_in('suites')
    assert not suites_in('python') & suites_in('suites')


@pytest.mark.parametrize(('runner', 'workers', 'expected_processes'), [(False, 8, 1), (True, 1, 1), (True, 8, 2)])
def test_receiver_gate_preserves_discovery_and_limits_processes(tmp_path, monkeypatch, runner, workers, expected_processes):
    receiver = 'runtime/trigger-receiver'
    for name in ('sotto-chief-of-staff/tests/test_pack.py', f'{receiver}/test_first.py',
                 f'{receiver}/nested/second_test.py'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('def test_fixture(): pass\n')
    monkeypatch.setattr(VERIFY, 'RUNNER', runner)
    lanes = VERIFY.lanes(tmp_path, workers)
    [commands] = [argvs for cwd, argvs in lanes['suites'] if any(receiver in arg for arg in argvs[0])]
    assert len(commands) == expected_processes
    if expected_processes == 1:
        assert commands == [[sys.executable, '-m', 'pytest', receiver, '-q']]
    else:
        files = [arg for argv in commands for arg in argv if arg.startswith(receiver)]
        assert sorted(files) == [f'{receiver}/nested/second_test.py', f'{receiver}/test_first.py']
    assert all(receiver not in arg for _, argvs in lanes['python'] for argv in argvs for arg in argv)


def test_failed_node_shard_reports_real_tap_output(tmp_path):
    script = tmp_path / 'fixture.test.mjs'
    script.write_text("import { test } from 'node:test';\ntest('fixture failed', () => { throw Error('test'); });\n")
    failures = VERIFY.run_lanes({'node': [(tmp_path, [['node', '--test', '--test-reporter=tap', str(script)]])]})
    assert len(failures) == 1
    assert '\n    not ok 1 - fixture failed' in failures[0]
    assert '\n    # fail 1' in failures[0]


def test_no_gate_command_runs_twice_and_the_provider_contracts_run_once(monkeypatch):
    monkeypatch.setattr(VERIFY, 'RUNNER', False)
    full = [argv for steps in VERIFY.lanes(ROOT).values() for _cwd, argvs in steps for argv in argvs]
    assert len({tuple(argv) for argv in full}) == len(full)
    if (ROOT / 'runtime/sotto-chat/package.json').is_file():
        words = [word for argv in full for word in argv]
        for name in ('test/agent.selfhost-chat-provider.test.ts', 'test/agent.hosted-speech.test.ts'):
            assert words.count(name) == 1, name
    fast = [word for steps in VERIFY.fast_lanes(ROOT).values() for _cwd, argvs in steps
            for argv in argvs for word in argv]
    assert 'cargo' not in fast and 'unittest' not in fast and 'runtime/local-reader' not in str(fast)


def test_the_bridge_lane_runs_on_linux_with_cargo_and_records_a_skip(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(VERIFY, 'SKIPPED', [])
    assert VERIFY.bridge_steps(tmp_path) == []  # The public tree carries no Bridge.
    (tmp_path / 'sotto-bridge/core').mkdir(parents=True)
    (tmp_path / 'sotto-bridge/core/Cargo.toml').write_text('')
    monkeypatch.setattr(VERIFY.sys, 'platform', 'linux')
    monkeypatch.setattr(VERIFY.shutil, 'which', lambda name: f'/usr/bin/{name}')
    [(cwd, [argv])] = VERIFY.bridge_steps(tmp_path)
    assert cwd == tmp_path / 'sotto-bridge/core' and argv[-3:] == ['cargo', 'test', '--locked']
    assert '--locked-target' in argv
    monkeypatch.setattr(VERIFY.shutil, 'which', lambda _name: None)
    assert VERIFY.bridge_steps(tmp_path) == []
    assert 'bridge: skipped, cargo is not on PATH' in capsys.readouterr().out
    assert VERIFY.SKIPPED[-1] == 'bridge (cargo is not on PATH)'
    monkeypatch.setattr(VERIFY.sys, 'platform', 'darwin')
    assert VERIFY.bridge_steps(tmp_path) == []
    assert 'bridge: skipped, it runs on Linux only' in capsys.readouterr().out


def test_the_foundation_contracts_need_the_reader_install(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(VERIFY, 'SKIPPED', [])
    assert VERIFY.foundation_steps(tmp_path) == []
    (tmp_path / 'runtime/local-reader').mkdir(parents=True)
    (tmp_path / 'runtime/local-reader/package.json').write_text('{}')
    (tmp_path / 'runtime/local-speech/tests').mkdir(parents=True)
    [(cwd, [argv])] = VERIFY.foundation_steps(tmp_path)
    assert cwd == tmp_path and argv[-4:] == ['unittest', 'discover', '-s', 'runtime/local-speech/tests']
    assert 'foundations: skipped, runtime/local-reader has no node_modules' in capsys.readouterr().out
    assert VERIFY.SKIPPED == ['foundations (runtime/local-reader has no node_modules (npm ci --ignore-scripts there))']
    (tmp_path / 'runtime/local-reader/node_modules').mkdir()
    steps = VERIFY.foundation_steps(tmp_path)
    assert [argv for _cwd, [argv] in steps[:2]] == [['npm', 'run', 'check'], ['npm', 'test']]


def test_the_cargo_target_lock_wraps_the_whole_command(tmp_path):
    target = tmp_path / 'target'
    code = 3
    assert VERIFY.locked_target(str(target), [sys.executable, '-c', f'raise SystemExit({code})']) == code
    assert (target / '.sotto-check.lock').is_file()


def test_the_runner_fails_the_gate_when_a_lane_was_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(VERIFY, 'RUNNER', True)
    monkeypatch.setattr(VERIFY, 'SKIPPED', [])
    monkeypatch.setattr(VERIFY, 'preflight', lambda: None)
    monkeypatch.setattr(VERIFY, 'verify_locks', lambda _root: None)
    monkeypatch.setattr(VERIFY, 'verify_unique_test_basenames', lambda _root: None)
    monkeypatch.setattr(VERIFY, 'lanes', lambda _root: VERIFY.skip('bridge', 'cargo is not on PATH') or {})
    monkeypatch.setattr(VERIFY.sys, 'argv', ['verify.py'])
    with pytest.raises(SystemExit, match=r'every lane must run on the CI runner; skipped: bridge \(cargo'):
        VERIFY.main()


def test_the_failure_summary_keeps_cargo_failures():
    output = (b"test documents::tests::a ... ok\ntest documents::tests::b ... FAILED\n"
              b"thread 'documents::tests::b' (42) panicked at src/documents.rs:9:5:\n"
              b"test result: FAILED. 1 passed; 1 failed; 0 ignored\n")
    assert VERIFY._summary(output) == [
        'test documents::tests::b ... FAILED', "thread 'documents::tests::b' (42) panicked at src/documents.rs:9:5:",
        'test result: FAILED. 1 passed; 1 failed; 0 ignored']
