"""Sotto's in-process boundary for the pinned Hermes CLI and filesystem layout."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

_SIBLINGS = {}


def sibling(name):
    """Load each shipped, trusted adapter once; hot paths must not re-execute module top levels."""
    if name in _SIBLINGS:
        return _SIBLINGS[name]
    spec = importlib.util.spec_from_file_location('sotto_hermes_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _SIBLINGS[name] = module
    return module


def run_argv(runner, prompt, usage_path=None, toolsets=''):
    argv = list(runner)
    if not argv:
        raise ValueError('A skill runner is required')
    if os.path.basename(argv[0]) == 'hermes':
        extra = []
        if toolsets:
            extra += ['-t', toolsets]
        if usage_path:
            extra += ['--usage-file', str(usage_path)]
        argv[1:1] = extra  # -z consumes the next token; keep the prompt beside it.
    return [*argv, prompt]


def send_receipt(body, target, timeout=60):
    return sibling('send').send_receipt(body, target, timeout)


def send_capability(run=None):
    return sibling('send').send_capability(run)


def home_path(name=''):
    return os.path.expanduser(os.path.join('~', '.hermes', name))


def discovery_roots():
    """Runtime installation roots; callers may prepend their own explicit skill override."""
    return [home_path(), '/usr/local/lib/hermes-agent']


def whatsapp_credentials_paths(data_root):
    """The persistent volume is authoritative; local installs may only have the home path."""
    relative = 'platforms/whatsapp/session/creds.json'
    return [str(Path(data_root) / 'hermes' / relative), home_path(relative)]


def telegram_bot_token():
    """Read the pinned runtime's environment convention without exposing its configuration."""
    token = (os.environ.get('TELEGRAM_BOT_TOKEN') or '').strip()
    if token:
        return token
    try:
        with open(home_path('.env'), encoding='utf-8') as stream:
            for line in stream:
                if line.startswith('TELEGRAM_BOT_TOKEN='):
                    return line.split('=', 1)[1].strip()
    except OSError:
        pass
    return ''


_USAGE_FIELDS = {"model": ("model",),
                 "cost": ("cost", "cost_usd", "total_cost"),
                 "input_tokens": ("input_tokens", "prompt_tokens"),
                 "output_tokens": ("output_tokens", "completion_tokens")}


def read_usage(path: str | None) -> dict | None:
    """Best-effort read of the runner's `--usage-file` JSON → {model, cost, input_tokens,
    output_tokens}. A missing, empty, or unparseable file yields None (no `usage` key on the
    receipt) and NEVER an error: cost is a nice-to-have on a receipt, the delivery is not."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    # Totals may sit at the top level or under a `usage`/`totals` envelope; top level wins.
    src = {}
    for envelope in ("totals", "usage"):
        if isinstance(doc.get(envelope), dict):
            src.update(doc[envelope])
    src.update({k: v for k, v in doc.items() if not isinstance(v, dict)})
    out = {}
    for field, aliases in _USAGE_FIELDS.items():
        for alias in aliases:
            v = src.get(alias)
            if field == "model" and isinstance(v, str) and v.strip():
                out[field] = v.strip()[:80]
                break
            if field != "model" and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[field] = v
                break
    return out or None


def google_setup_path(managed=False):
    if managed:
        return str(Path(__file__).with_name('google_setup.py'))
    for base in map(Path, discovery_roots()):
        for path in base.glob('**/google-workspace/scripts/setup.py'):
            return str(path)
    return None


def google_api_path():
    for base in map(Path, discovery_roots()):
        for path in base.glob('**/google-workspace/scripts/google_api.py'):
            return str(path)
    return None


def set_timezone(zone, run=None):
    try:
        result = (run or subprocess.run)(['hermes', 'config', 'set', 'timezone', zone],
                                        capture_output=True, text=True, timeout=20)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def personal_routines(run=None):
    try:
        result = (run or subprocess.run)(['hermes', 'cron', 'list'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode:
        return []
    out = []
    for _, block in sibling('reconcile_crons').blocks(result.stdout):
        match = re.search(r'(?<![A-Za-z0-9_-])(user-[a-z0-9][A-Za-z0-9_-]*)', block)
        if not match:
            continue
        fields = {}
        for key in ('Schedule', 'Prompt', 'Deliver'):
            found = re.search(rf'(?im)^\s*{key}:\s*(.+)$', block)
            fields[key.lower()] = found.group(1).strip() if found else ''
        out.append({'name': match.group(1), **fields})
    return out[:10]


def session_ids(listing):
    out = []
    for line in (listing or '').splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        identifier = parts[-1]
        if (identifier == 'ID' or not any(ch.isdigit() for ch in identifier)
                or set(identifier) <= set('─-=') or identifier.startswith('cron_') or identifier in out):
            continue
        out.append(identifier)
    return out


def archive_sessions(run=None):
    run = run or subprocess.run
    try:
        result = run(['hermes', 'sessions', 'list'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    archived = 0
    for identifier in session_ids(result.stdout):
        try:
            result = run(['hermes', 'sessions', 'archive', identifier], capture_output=True, text=True, timeout=30)
            archived += int(result.returncode == 0)
        except (OSError, subprocess.SubprocessError):
            continue
    return archived
