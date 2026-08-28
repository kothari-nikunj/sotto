"""Telegram link engine: token validation, first-sender capture, the 0600 handoff file, and the
settings block the CLI prints.

NO NETWORK, ever: `telegram_link._get` is the single wire seam (same shape as connectors._http —
(status, body)) and every test replaces it with `FakeGet`, which routes by Bot API method name and
records the URLs it was called with. The payloads below are fixture-shaped Telegram `getUpdates`
JSON: real `update_id` / `chat.type` / `from.is_bot` fields, because those three are exactly what
the filter reads.
"""
import importlib.util
import json
import os
import re
import urllib.parse

HERE = os.path.dirname(__file__)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tg = _load("telegram_link_t", "telegram_link.py")

TOKEN = "123456789:FAKE-not-a-real-bot-token-AAbbCC"


class FakeGet:
    """Routes by Bot API method name → (status, body). Each route may be a single response or a
    list consumed one poll at a time. Records every URL so offset advancement is checkable."""

    def __init__(self, routes):
        self.routes = {k: (v if isinstance(v, list) else [v]) for k, v in routes.items()}
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        for method, responses in self.routes.items():
            if f"/{method}" in url:
                return responses.pop(0) if len(responses) > 1 else responses[0]
        return 404, b'{"ok":false,"description":"Not Found"}'

    def offsets(self):
        """The `offset=` query value of each getUpdates call (None when absent)."""
        out = []
        for u in self.urls:
            if "/getUpdates" not in u:
                continue
            q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
            out.append(int(q["offset"][0]) if "offset" in q else None)
        return out


def _ok(payload):
    return 200, json.dumps({"ok": True, "result": payload}).encode()


def _updates(*updates):
    return _ok(list(updates))


def _private_msg(update_id, user_id, first="Nikunj", last="Kothari", text="hi", is_bot=False):
    return {"update_id": update_id,
            "message": {"message_id": update_id, "text": text,
                        "chat": {"id": user_id, "type": "private"},
                        "from": {"id": user_id, "is_bot": is_bot,
                                 "first_name": first, "last_name": last, "username": "nk"}}}


def _group_msg(update_id, user_id=999):
    m = _private_msg(update_id, user_id, text="group chatter")
    m["message"]["chat"] = {"id": -100200300, "type": "group", "title": "Some Group"}
    return m


def _channel_post(update_id):
    return {"update_id": update_id,
            "channel_post": {"message_id": 1, "text": "posted",
                             "chat": {"id": -100999, "type": "channel"}}}


def _use(monkeypatch, fake, tmp_path=None):
    monkeypatch.setattr(tg, "_get", fake)
    if tmp_path is not None:
        monkeypatch.setattr(tg, "DATA", str(tmp_path))
    return fake


# ── validate_token ───────────────────────────────────────────────────────────────────────────────

def test_validate_token_returns_the_bot_username(monkeypatch):
    _use(monkeypatch, FakeGet({"getMe": _ok({"id": 123456789, "is_bot": True,
                                             "first_name": "Sotto", "username": "sotto_brief_bot"})}))
    assert tg.validate_token(TOKEN) == {"ok": True, "username": "sotto_brief_bot", "name": "Sotto"}


def test_validate_token_rejects_a_bad_token_without_echoing_it(monkeypatch):
    _use(monkeypatch, FakeGet({"getMe": (401, b'{"ok":false,"error_code":401,'
                                              b'"description":"Unauthorized"}')}))
    out = tg.validate_token(TOKEN)
    assert out["ok"] is False
    assert "@BotFather" in out["error"]
    assert TOKEN not in out["error"]          # a credential never rides along in an error line
    assert "\n" not in out["error"]           # one short line, printable as-is


def test_validate_token_transport_failure_scrubs_the_token_from_the_detail(monkeypatch):
    # urllib exceptions can quote the URL, and the URL embeds the token in its path.
    _use(monkeypatch, FakeGet({"getMe": (0, f"<urlopen error> {tg._api(TOKEN, 'getMe')}".encode())}))
    out = tg.validate_token(TOKEN)
    assert out["ok"] is False
    assert TOKEN not in out["error"] and "api.telegram.org" in out["error"]


def test_validate_token_rejects_a_bot_with_no_username(monkeypatch):
    _use(monkeypatch, FakeGet({"getMe": _ok({"id": 1, "is_bot": True, "first_name": "X"})}))
    assert tg.validate_token(TOKEN)["ok"] is False


# ── capture_first_sender ─────────────────────────────────────────────────────────────────────────

def test_capture_ignores_channel_posts_groups_and_bots_and_takes_the_human(monkeypatch):
    fake = _use(monkeypatch, FakeGet({"getUpdates": _updates(
        _channel_post(10),                                  # not a `message` at all
        _group_msg(11),                                     # a group the bot was added to
        _private_msg(12, 4242, first="Some", last="Bot", is_bot=True),   # another bot
        _private_msg(13, 8675309, text="hello sotto"),      # <- the human
        _private_msg(14, 111111, first="Later"),            # arrived after; must not win
    )}))
    assert tg.capture_first_sender(TOKEN, timeout_secs=5) == {
        "ok": True, "user_id": 8675309, "name": "Nikunj Kothari", "text": "hello sotto"}
    assert len(fake.offsets()) == 1                         # found on the first poll


def test_capture_advances_the_offset_past_ignored_updates(monkeypatch):
    # Poll 1 returns only noise. Poll 2 must ask for offset = last update_id + 1, or Telegram
    # re-serves the same backlog forever and the noise is re-examined on every poll.
    fake = _use(monkeypatch, FakeGet({"getUpdates": [
        _updates(_group_msg(70), _channel_post(71)),
        _updates(_private_msg(72, 555, first="Ada", last="Lovelace", text="ping")),
    ]}))
    got = tg.capture_first_sender(TOKEN, timeout_secs=5)
    assert got["user_id"] == 555 and got["name"] == "Ada Lovelace" and got["text"] == "ping"
    assert fake.offsets() == [None, 72]                     # first poll unoffset, then 71 + 1


def test_capture_falls_back_to_username_then_id_for_the_display_name(monkeypatch):
    u = _private_msg(1, 77, text="yo")
    u["message"]["from"] = {"id": 77, "is_bot": False, "username": "handle_only"}
    _use(monkeypatch, FakeGet({"getUpdates": _updates(u)}))
    assert tg.capture_first_sender(TOKEN, timeout_secs=5)["name"] == "handle_only"


def test_capture_times_out_with_the_honest_line(monkeypatch):
    # timeout_secs=0 → the deadline is already past once the first (empty) poll is processed.
    _use(monkeypatch, FakeGet({"getUpdates": _updates()}))
    assert tg.capture_first_sender(TOKEN, timeout_secs=0) == {
        "ok": False, "error": "nobody texted the bot in time"}


def test_capture_surfaces_an_api_error_rather_than_calling_it_a_timeout(monkeypatch):
    _use(monkeypatch, FakeGet({"getUpdates": (409, b'{"ok":false,"description":'
                                                   b'"Conflict: terminated by other getUpdates"}')}))
    out = tg.capture_first_sender(TOKEN, timeout_secs=5)
    assert out["ok"] is False
    assert "Conflict" in out["error"] and "nobody texted" not in out["error"]


# ── persist ──────────────────────────────────────────────────────────────────────────────────────

def test_persist_writes_0600_json_that_round_trips(tmp_path):
    path = str(tmp_path / "nested" / "telegram-link.json")
    tg.persist(TOKEN, 8675309, path)
    rec = json.loads(open(path, encoding="utf-8").read())
    assert rec["bot_token"] == TOKEN and rec["allowed_user"] == 8675309
    assert re.match(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z", rec["linked_at"])
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"     # it holds a credential
    # atomic: tmp + os.replace, and the scratch file is gone afterwards
    assert os.listdir(os.path.dirname(path)) == ["telegram-link.json"]


def test_persist_defaults_to_the_sotto_data_path(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "DATA", str(tmp_path))
    assert tg.link_path() == str(tmp_path / "telegram-link.json")
    tg.persist(TOKEN, 42)
    assert os.path.exists(tg.link_path())


def test_persist_overwrites_a_previous_link_in_place(tmp_path):
    path = str(tmp_path / "telegram-link.json")
    tg.persist("old:token", 1, path)
    tg.persist(TOKEN, 2, path)
    rec = json.loads(open(path, encoding="utf-8").read())
    assert rec["bot_token"] == TOKEN and rec["allowed_user"] == 2
    assert os.listdir(str(tmp_path)) == ["telegram-link.json"]


# ── the settings block + the CLI ─────────────────────────────────────────────────────────────────

# Pinned as literals ON PURPOSE. Source of truth: CHANNELS.md § Telegram setup (the token and
# allow/home-channel variables that start.sh forwards by TELEGRAM_* prefix into ~/.hermes/.env, and
# SOTTO_CRON_DELIVER, the one lever that moves the briefs). If CHANNELS.md renames one of these,
# this test is the thing that must be updated with it — that is the point.
DOCUMENTED_VARS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "SOTTO_CRON_DELIVER"]
NAME_EQ_VALUE = re.compile(r"\A[A-Z][A-Z0-9_]*=[^\s][^\n]*\Z")


def test_settings_block_uses_exactly_the_documented_names():
    lines = tg.settings_block(TOKEN, 8675309)
    names = [ln.split("=", 1)[0] for ln in lines]
    for var in DOCUMENTED_VARS:
        assert var in names, f"{var} (CHANNELS.md § Telegram setup) missing from the settings block"
    assert dict(ln.split("=", 1) for ln in lines) == {
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "TELEGRAM_ALLOWED_USERS": "8675309",
        "TELEGRAM_HOME_CHANNEL": "8675309",      # where proactive delivery lands, same chat id
        "SOTTO_CRON_DELIVER": "telegram",
    }
    for ln in lines:
        assert NAME_EQ_VALUE.match(ln), f"not pasteable into Railway's raw editor: {ln!r}"


def _happy_fake():
    return FakeGet({
        "getMe": _ok({"id": 123456789, "is_bot": True, "first_name": "Sotto",
                      "username": "sotto_brief_bot"}),
        "getUpdates": _updates(_group_msg(40),
                               _private_msg(41, 8675309, text="hello sotto")),
    })


def test_cli_happy_path_prints_the_block_and_persists(tmp_path, monkeypatch, capsys):
    _use(monkeypatch, _happy_fake(), tmp_path)
    assert tg.main(["--token", TOKEN, "--timeout", "5"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()

    assert "@sotto_brief_bot" in out                       # WHICH bot you pasted
    assert "Nikunj Kothari" in out and "8675309" in out    # who it linked, no id hunting
    for expected in tg.settings_block(TOKEN, 8675309):
        assert expected in lines                           # each on its own line, unindented
    for var in DOCUMENTED_VARS:
        assert any(ln.startswith(var + "=") and NAME_EQ_VALUE.match(ln) for ln in lines)

    rec = json.loads(open(str(tmp_path / "telegram-link.json"), encoding="utf-8").read())
    assert rec["allowed_user"] == 8675309
    # the CLI writes ONE file and no log: the token must not land anywhere the tree logs to
    assert os.listdir(str(tmp_path)) == ["telegram-link.json"]


def test_cli_bad_token_exits_1_and_writes_nothing(tmp_path, monkeypatch, capsys):
    _use(monkeypatch, FakeGet({"getMe": (401, b'{"ok":false,"description":"Unauthorized"}')}), tmp_path)
    assert tg.main(["--token", TOKEN]) == 1
    out = capsys.readouterr().out
    assert TOKEN not in out                                # nothing to paste, nothing leaked
    assert os.listdir(str(tmp_path)) == []


def test_cli_timeout_exits_1_and_writes_nothing(tmp_path, monkeypatch, capsys):
    _use(monkeypatch, FakeGet({
        "getMe": _ok({"id": 1, "is_bot": True, "first_name": "Sotto", "username": "sotto_brief_bot"}),
        "getUpdates": _updates(),
    }), tmp_path)
    assert tg.main(["--token", TOKEN, "--timeout", "0"]) == 1
    assert "nobody texted the bot in time" in capsys.readouterr().out
    assert os.listdir(str(tmp_path)) == []
