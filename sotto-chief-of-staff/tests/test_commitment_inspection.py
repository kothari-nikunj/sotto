"""The artifact probe must reject an unexpected connection even when code catches it."""
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize('event', ['socket.connect', 'socket.sendto', 'socket.getaddrinfo'])
def test_inspection_fails_on_caught_external_connection(event):
    checkout = Path(__file__).resolve().parents[3]
    script = checkout / 'sotto-agent/sotto-chief-of-staff/evals/inspect_commitment_journey.py'
    code = '''
import runpy, sys
from pathlib import Path
script, checkout, event = sys.argv[1:]
sys.path.insert(0, str(Path(checkout) / 'sotto-agent/sotto-chief-of-staff/tests'))
import conftest
import commitment_journey_fixture as fixture
original = fixture.run_journey
def caught_connection(*args, **kwargs):
    try:
        sys.audit(event, None, ('unexpected.example.test', 443))
    except AssertionError:
        pass  # Simulate a source reader swallowing its provider error.
    return original(*args, **kwargs)
fixture.run_journey = caught_connection
sys.argv = [script, checkout]
runpy.run_path(script, run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-c', code, str(script), str(checkout), event],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert f"AssertionError: ['{event}'" in result.stderr
    assert '"unexpectedNetwork": []' not in result.stdout
