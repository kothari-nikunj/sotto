"""Telegram link engine: token validation, first-sender capture, the 0600 handoff file, and the
settings block the CLI prints.

NO NETWORK, ever: `telegram_link._get` is the single wire seam (same shape as connectors._http —
(status, body)) and every test replaces it with `FakeGet`, which routes by Bot API method name and
records the URLs it was called with. The payloads below are fixture-shaped Telegram `getUpdates`
JSON: real `update_id` / `chat.type` / `from.is_bot` / `text` fields, because those four are exactly
what the filter reads — the fourth being the PAIRING PHRASE, without which a capture would link
whoever found the bot's discoverable @username first.
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
# Shaped like what start.sh passes: this deploy's setup code, which only the operator can read.
PHRASE = "Xk2p-9fQzR7t"


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


def _private_msg(update_id, user_id, first="Nikunj", last="Kothari", text=None, is_bot=False):
    """A private message that CARRIES the phrase unless a test hands it other text — the default is
    exactly what tapping the deep link sends."""
    text = f"/start {PHRASE}" if text is None else text
    return {"update_id": update_id,
            "message": {"message_id": update_id, "text": text,
                        "chat": {"id": user_id, "type": "private"},
                        "from": {"id": user_id, "is_bot": is_bot,
                                 "first_name": first, "last_name": last, "username": "nk"}}}


def _group_msg(update_id, user_id=999):
    m = _private_msg(update_id, user_id, text=f"group chatter {PHRASE}")
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
        _private_msg(13, 8675309, text=f"hello sotto {PHRASE}"),   # <- the human
        _private_msg(14, 111111, first="Later"),            # arrived after; must not win
    )}))
    assert tg.capture_first_sender(TOKEN, PHRASE, timeout_secs=5) == {
        "ok": True, "user_id": 8675309, "name": "Nikunj Kothari",
        "text": f"hello sotto {PHRASE}"}
    # found on the first poll — then ONE zero-wait call at update 13's offset, so Telegram
    # confirms the pairing message and the gateway that starts later never receives it
    assert fake.offsets() == [None, 14]


def test_capture_advances_the_offset_past_ignored_updates(monkeypatch):
    # Poll 1 returns only noise. Poll 2 must ask for offset = last update_id + 1, or Telegram
    # re-serves the same backlog forever and the noise is re-examined on every poll.
    fake = _use(monkeypatch, FakeGet({"getUpdates": [
        _updates(_group_msg(70), _channel_post(71)),
        _updates(_private_msg(72, 555, first="Ada", last="Lovelace", text=f"ping {PHRASE}")),
    ]}))
    got = tg.capture_first_sender(TOKEN, PHRASE, timeout_secs=5)
    assert got["user_id"] == 555 and got["name"] == "Ada Lovelace"
    assert fake.offsets() == [None, 72, 73]                 # unoffset, then 71 + 1, then the ack


def test_capture_falls_back_to_username_then_id_for_the_display_name(monkeypatch):
    u = _private_msg(1, 77)
    u["message"]["from"] = {"id": 77, "is_bot": False, "username": "handle_only"}
    _use(monkeypatch, FakeGet({"getUpdates": _updates(u)}))
    assert tg.capture_first_sender(TOKEN, PHRASE, timeout_secs=5)["name"] == "handle_only"


def test_capture_times_out_with_the_honest_line(monkeypatch):
    # timeout_secs=0 → the deadline is already past once the first (empty) poll is processed.
    _use(monkeypatch, FakeGet({"getUpdates": _updates()}))
    assert tg.capture_first_sender(TOKEN, PHRASE, timeout_secs=0) == {
        "ok": False, "error": "nobody sent the pairing link in time"}


def test_capture_surfaces_an_api_error_rather_than_calling_it_a_timeout(monkeypatch):
    _use(monkeypatch, FakeGet({"getUpdates": (409, b'{"ok":false,"description":'
                                                   b'"Conflict: terminated by other getUpdates"}')}))
    out = tg.capture_first_sender(TOKEN, PHRASE, timeout_secs=5)
    assert out["ok"] is False
    assert "Conflict" in out["error"] and "nobody sent" not in out["error"]


# ── the ownership check: only a message carrying the pairing phrase may link ─────────────────────

def test_a_stranger_who_never_sends_the_pairing_phrase_is_never_linked(monkeypatch):
    """THE security rule. A bot's @username is discoverable, so before the phrase existed the first
    stranger to message the bot became the owner and received the user's briefs. Their messages are
    now consumed (the offset still advances) and dropped, and the wait ends as a timeout."""
    fake = _use(monkeypatch, FakeGet({"getUpdates": _updates(
        _private_msg(20, 66666, first="Mallory", text="hi"),
        _private_msg(21, 66666, first="Mallory", text="/start"),
        _private_msg(22, 66666, first="Mallory", text="let me in"),
    )}))
    assert tg.capture_first_sender(TOKEN, PHRASE, timeout_secs=0) == {
        "ok": False, "error": "nobody sent the pairing link in time"}
    assert fake.offsets() == [None]              # seen once, never re-examined on a later poll


def test_the_phrase_carrying_message_links_even_behind_a_stranger(monkeypatch):
    """The owner does not lose the deploy by arriving second."""
    _use(monkeypatch, FakeGet({"getUpdates": _updates(
        _private_msg(30, 66666, first="Mallory", text="hello?"),
        _private_msg(31, 8675309),                                   # the deep link's own message
    )}))
    assert tg.capture_first_sender(TOKEN, PHRASE, timeout_secs=5)["user_id"] == 8675309


def test_the_deep_links_start_form_and_a_typed_phrase_are_the_same_message(monkeypatch):
    """Telegram delivers `?start=<phrase>` as the text `/start <phrase>`, and a phone keyboard may
    capitalize — a containment match, case-insensitive, covers every form the owner can send."""
    for text in (f"/start {PHRASE}", PHRASE, f"  {PHRASE.upper()}  ", f"code: {PHRASE.lower()}!"):
        _use(monkeypatch, FakeGet({"getUpdates": _updates(_private_msg(50, 8675309, text=text))}))
        assert tg.capture_first_sender(TOKEN, PHRASE, timeout_secs=5)["user_id"] == 8675309


def test_no_phrase_fails_closed_instead_of_linking_whoever_messages_first(tmp_path, monkeypatch):
    """Every entry point refuses, and none of them polls: an empty phrase would match every message,
    which is exactly the vulnerability the phrase exists to close."""
    fake = _use(monkeypatch, _happy_fake(), tmp_path)
    assert tg.capture_first_sender(TOKEN, "", timeout_secs=5) == {"ok": False, "error": tg.NO_PHRASE}
    assert tg.capture_first_sender(TOKEN, "   ", timeout_secs=5)["error"] == tg.NO_PHRASE
    assert tg.ensure_link(TOKEN, "", timeout_secs=5) == {"ok": False, "error": tg.NO_PHRASE}
    assert tg.main(["--token", TOKEN, "--boot"]) == 1
    assert tg.main(["--token", TOKEN]) == 1
    assert fake.urls == [] and os.listdir(str(tmp_path)) == []


def test_the_deep_link_is_the_one_tap_form_of_the_phrase():
    assert tg.deep_link("sotto_brief_bot", PHRASE) == f"https://t.me/sotto_brief_bot?start={PHRASE}"
    assert "%20" in tg.deep_link("b", "two words")     # a phrase is query-escaped, never raw


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
        "getUpdates": _updates(_group_msg(40), _private_msg(41, 8675309)),
    })


def test_cli_happy_path_prints_the_block_and_persists(tmp_path, monkeypatch, capsys):
    _use(monkeypatch, _happy_fake(), tmp_path)
    assert tg.main(["--token", TOKEN, "--phrase", PHRASE, "--timeout", "5"]) == 0
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
    assert tg.main(["--token", TOKEN, "--phrase", PHRASE]) == 1
    out = capsys.readouterr().out
    assert TOKEN not in out                                # nothing to paste, nothing leaked
    assert os.listdir(str(tmp_path)) == []


def test_cli_timeout_exits_1_and_writes_nothing(tmp_path, monkeypatch, capsys):
    _use(monkeypatch, FakeGet({
        "getMe": _ok({"id": 1, "is_bot": True, "first_name": "Sotto", "username": "sotto_brief_bot"}),
        "getUpdates": _updates(),
    }), tmp_path)
    assert tg.main(["--token", TOKEN, "--phrase", PHRASE, "--timeout", "0"]) == 1
    assert "nobody sent the pairing link in time" in capsys.readouterr().out
    assert os.listdir(str(tmp_path)) == []


# ── the boot handshake (what start.sh runs) ──────────────────────────────────────────────────────

def test_ensure_link_captures_and_persists_when_nothing_is_linked_yet(tmp_path, monkeypatch):
    fake = _use(monkeypatch, _happy_fake(), tmp_path)
    out = tg.ensure_link(TOKEN, PHRASE, timeout_secs=5)
    assert out["ok"] is True and out["user_id"] == 8675309 and out["reused"] is False
    assert out["username"] == "sotto_brief_bot"
    rec = json.loads(open(str(tmp_path / "telegram-link.json"), encoding="utf-8").read())
    assert rec["allowed_user"] == 8675309 and rec["bot_username"] == "sotto_brief_bot"
    assert any("/getUpdates" in u for u in fake.urls)


def test_ensure_link_reuses_the_captured_id_without_calling_telegram(tmp_path, monkeypatch):
    """The second boot must not wait on a message that already arrived — and must not poll at all."""
    tg.persist(TOKEN, 8675309, str(tmp_path / "telegram-link.json"), "sotto_brief_bot")
    fake = _use(monkeypatch, FakeGet({}), tmp_path)
    assert tg.ensure_link(TOKEN, PHRASE, timeout_secs=5) == {"ok": True, "user_id": 8675309, "reused": True}
    assert fake.urls == []


def test_ensure_link_recaptures_when_the_bot_token_changed(tmp_path, monkeypatch):
    """A swapped bot must re-link: delivering to the chat the OLD bot captured is a silent misroute."""
    tg.persist("999:OTHER-not-a-real-token", 111, str(tmp_path / "telegram-link.json"))
    _use(monkeypatch, _happy_fake(), tmp_path)
    assert tg.ensure_link(TOKEN, PHRASE, timeout_secs=5)["user_id"] == 8675309


def test_ensure_link_reports_a_timeout_and_writes_nothing(tmp_path, monkeypatch):
    """Boot must be able to carry on: a timeout is one short line, no file, no exception."""
    _use(monkeypatch, FakeGet({
        "getMe": _ok({"id": 1, "is_bot": True, "first_name": "Sotto", "username": "sotto_brief_bot"}),
        "getUpdates": _updates(),
    }), tmp_path)
    out = tg.ensure_link(TOKEN, PHRASE, timeout_secs=0)
    assert out["ok"] is False and out["error"] == "nobody sent the pairing link in time"
    assert os.listdir(str(tmp_path)) == []


def test_boot_mode_prints_only_the_id_on_stdout(tmp_path, monkeypatch, capsys):
    """start.sh reads stdout in a command substitution and the deploy log reads stderr, so the id
    must be the ONLY thing on stdout — and the token must never appear on either."""
    _use(monkeypatch, _happy_fake(), tmp_path)
    assert tg.main(["--token", TOKEN, "--phrase", PHRASE, "--timeout", "5", "--boot"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "8675309"
    assert "sotto_brief_bot" in captured.err
    assert TOKEN not in captured.out and TOKEN not in captured.err


def test_boot_mode_exits_1_with_an_empty_stdout_when_nobody_texts(tmp_path, monkeypatch, capsys):
    """The failure start.sh treats as "not linked yet": no id to forward, boot continues."""
    _use(monkeypatch, FakeGet({
        "getMe": _ok({"id": 1, "is_bot": True, "first_name": "Sotto", "username": "sotto_brief_bot"}),
        "getUpdates": _updates(),
    }), tmp_path)
    assert tg.main(["--token", TOKEN, "--phrase", PHRASE, "--timeout", "0", "--boot"]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "nobody sent the pairing link in time" in captured.err
    # the recovery instruction is the link itself — "message your bot" would be untappable and,
    # since the gateway stays down, the message it asked for would sit unread anyway
    assert f"https://t.me/sotto_brief_bot?start={PHRASE}" in captured.err
    assert "restart this deploy" in captured.err


def test_boot_mode_prints_the_deep_link_before_it_starts_waiting(tmp_path, monkeypatch, capsys):
    """A link printed after the five-minute wait is a link nobody could tap: the deploy log must
    carry it while there is still time."""
    _use(monkeypatch, _happy_fake(), tmp_path)
    assert tg.main(["--token", TOKEN, "--phrase", PHRASE, "--timeout", "5", "--boot"]) == 0
    err = capsys.readouterr().err
    assert f"TAP THIS TO LINK YOUR CHAT:  https://t.me/sotto_brief_bot?start={PHRASE}" in err
    assert err.index("TAP THIS") < err.index("linked @sotto_brief_bot")


def test_capture_confirms_the_pairing_message_so_the_gateway_never_sees_it(monkeypatch):
    """Telegram confirms an update only when a LATER getUpdates carries its offset. Returning on the
    hit left "/start <setup code>" unconfirmed, so the gateway that started minutes later received
    the per-deploy secret as the user's first message and replied to it (Day-0 simulation, Sep
    2026). The ack is one call, zero wait, at exactly the hit's offset — the update that arrived
    after it is left for the gateway."""
    fake = _use(monkeypatch, FakeGet({"getUpdates": _updates(
        _private_msg(40, 8675309, text=f"/start {PHRASE}"),
        _private_msg(41, 8675309, text="hi sotto"),
    )}))
    assert tg.capture_first_sender(TOKEN, PHRASE, timeout_secs=5)["user_id"] == 8675309
    assert fake.offsets() == [None, 41]
