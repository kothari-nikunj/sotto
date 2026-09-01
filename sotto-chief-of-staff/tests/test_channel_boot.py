"""The boot channel decision, run as shell — not asserted as prose.

start.sh decides ONE thing before anything else starts: which channel Sotto delivers to. The rule is
one sentence — *Telegram, unless this volume already holds a paired WhatsApp session and no
TELEGRAM_BOT_TOKEN is set* — and its failure mode is silent channel loss, so these tests EXECUTE the
shipped lines (extracted between their own markers) instead of grepping for them. The Telegram
capture is exercised the same way, with `python3` stubbed on PATH: the shell wiring is what is under
test here; the handshake itself is tested in runtime/trigger-receiver/test_telegram_link.py, which
owns it. So is the gateway-start decision, whose failure mode is subtler — a gateway started for an
unlinked Telegram eats the pairing message that the next boot's capture needs, which made the
"then restart" recovery instruction impossible to follow.
"""
import os
import stat
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
HERMES = os.path.dirname(os.path.dirname(HERE))                 # sotto-hermes/
START_SH = os.path.join(HERMES, "adapters", "hermes", "start.sh")

with open(START_SH, encoding="utf-8") as f:
    START = f.read()

CHANNEL_START = 'WA_CREDS="$HOME/.hermes/platforms/whatsapp/session/creds.json"'
CHANNEL_END = 'echo "[sotto] delivery channel:'
CODE_START = "setup_code() {"
CODE_END = "# Hermes v0.20 dropped some config keys"
TELEGRAM_START = 'if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -z "${TELEGRAM_ALLOWED_USERS:-}" ]; then'
TELEGRAM_END = "# 5b) Pair WhatsApp."
GATEWAY_START = "START_GATEWAY=1"
GATEWAY_END = "# No gateway to supervise:"


def _block(first: str, last: str) -> str:
    """The shipped lines between two markers — so a rewrite of start.sh changes what these run."""
    head = START.index(first)
    tail = START.index(last, head)
    if last.startswith("#"):                       # a following-section marker: stop before it
        return START[head:tail]
    return START[head:START.index("\n", tail) + 1]


def _run(script: str, home: str, env: dict) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    for key in ("SOTTO_CRON_DELIVER", "WHATSAPP_ENABLED", "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_ALLOWED_USERS", "SOTTO_SETUP_CODE", "SOTTO_DATA"):
        full.pop(key, None)
    full.update({"HOME": home, **env})
    return subprocess.run(["bash", "-euo", "pipefail", "-c", script],
                          capture_output=True, text=True, env=full)


def _channel(tmp_path, paired: bool, **env) -> tuple:
    """(channel, whatsapp_enabled, boot log) after running start.sh's own resolution."""
    home = str(tmp_path / "home")
    creds = os.path.join(home, ".hermes", "platforms", "whatsapp", "session", "creds.json")
    os.makedirs(os.path.dirname(creds), exist_ok=True)
    if paired:
        with open(creds, "w", encoding="utf-8") as f:
            f.write("{}")
    script = _block(CHANNEL_START, CHANNEL_END) + \
        '\nprintf "%s %s\\n" "$SOTTO_CRON_DELIVER" "$WHATSAPP_ENABLED"\n'
    out = _run(script, home, env)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    channel, enabled = lines[-1].split()
    return channel, enabled, "\n".join(lines[:-1])


def test_a_fresh_deploy_delivers_to_telegram(tmp_path):
    channel, enabled, _ = _channel(tmp_path, paired=False)
    assert channel == "telegram"
    assert enabled == "false"          # no WhatsApp gateway, so no boot-time QR wait


def test_a_volume_already_paired_with_whatsapp_keeps_whatsapp(tmp_path):
    """MIGRATION: an existing WhatsApp deploy must never lose its channel on a redeploy — it set no
    variable when it was set up, so nothing but the session on its volume can speak for it."""
    channel, enabled, _ = _channel(tmp_path, paired=True)
    assert (channel, enabled) == ("whatsapp", "true")


def test_a_bot_token_beats_an_old_whatsapp_session(tmp_path):
    """Explicit configuration wins: pasting a bot token IS the choice to move to Telegram."""
    channel, enabled, _ = _channel(tmp_path, paired=True, TELEGRAM_BOT_TOKEN="<bot-token>")
    assert (channel, enabled) == ("telegram", "false")


def test_the_variable_always_wins(tmp_path):
    for paired in (True, False):
        assert _channel(tmp_path / f"p{paired}", paired=paired,
                        SOTTO_CRON_DELIVER="telegram")[0] == "telegram"
        assert _channel(tmp_path / f"q{paired}", paired=paired,
                        SOTTO_CRON_DELIVER="whatsapp")[:2] == ("whatsapp", "true")


def test_whatsapp_stays_an_opt_in_beside_another_channel(tmp_path):
    """WhatsApp is no longer the default, and it was not deleted: WHATSAPP_ENABLED=true still pairs
    (and serves) it while the briefs go somewhere else."""
    channel, enabled, _ = _channel(tmp_path, paired=False, SOTTO_CRON_DELIVER="telegram",
                                   WHATSAPP_ENABLED="true")
    assert (channel, enabled) == ("telegram", "true")


def test_the_boot_log_names_the_channel_and_the_reason(tmp_path):
    """Silent channel loss is the failure mode, so the decision is loud — one line, both facts."""
    _, _, log = _channel(tmp_path / "a", paired=True)
    assert "delivery channel: whatsapp (this volume is already paired with WhatsApp)" in log
    _, _, log = _channel(tmp_path / "b", paired=False)
    assert "delivery channel: telegram (the default)" in log
    _, _, log = _channel(tmp_path / "c", paired=False, SOTTO_CRON_DELIVER="local")
    assert "delivery channel: local (SOTTO_CRON_DELIVER is set)" in log


# ── 5a: the capture, wired into ~/.hermes/.env ───────────────────────────────────────────────────

def _capture(tmp_path, fake_id: str = "", setup_code: str = "Xk2p-9fQzR7t", envf: str = "",
             **env) -> tuple:
    """(the .env lines it wrote, the linker's argv, the boot log) after running start.sh's step 5a
    with `python3` stubbed — the stub prints `fake_id` (empty = the linker failed/timed out). The
    shipped setup_code()/setup_qs() resolver runs for real against a $SOTTO_DATA of our own."""
    home = str(tmp_path / "home")
    binp = str(tmp_path / "bin")
    data = str(tmp_path / "data")
    os.makedirs(home, exist_ok=True)
    os.makedirs(binp, exist_ok=True)
    os.makedirs(data, exist_ok=True)
    if setup_code:
        with open(os.path.join(data, "setup_code"), "w", encoding="utf-8") as f:
            f.write(setup_code + "\n")
    calls = str(tmp_path / "calls.txt")
    stub = os.path.join(binp, "python3")
    with open(stub, "w", encoding="utf-8") as f:
        f.write('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SOTTO_TEST_CALLS"\n'
                '[ -n "$SOTTO_TEST_ID" ] || exit 1\n'
                'printf "%s\\n" "$SOTTO_TEST_ID"\n')
    os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
    envf = envf or str(tmp_path / "hermes.env")   # a caller may seed it (a previous bot's ids)
    harness = (f'ENVF="{envf}"\ntouch "$ENVF"\n'
               'upsert_env() {\n'
               '  grep -v "^$1=" "$ENVF" > "$ENVF.tmp" 2>/dev/null || true\n'
               '  mv "$ENVF.tmp" "$ENVF"\n'
               "  printf '%s=%s\\n' \"$1\" \"$2\" >> \"$ENVF\"\n"
               '}\n'
               'drop_env() {\n'
               '  grep -v "^$1=" "$ENVF" > "$ENVF.tmp" 2>/dev/null || true\n'
               '  mv "$ENVF.tmp" "$ENVF"\n'
               '}\n') + _block(CODE_START, CODE_END)
    out = _run(harness + _block(TELEGRAM_START, TELEGRAM_END), home,
               {"PATH": binp + os.pathsep + os.environ["PATH"], "SOTTO_DATA": data,
                "SOTTO_TEST_CALLS": calls, "SOTTO_TEST_ID": fake_id, **env})
    assert out.returncode == 0, out.stderr          # a failed capture never fails the boot
    with open(envf, encoding="utf-8") as f:
        written = [ln.strip() for ln in f if ln.strip()]
    argv = open(calls, encoding="utf-8").read() if os.path.exists(calls) else ""
    return written, argv, out.stdout


def test_a_bot_token_and_no_chat_id_captures_one_and_forwards_it(tmp_path):
    """The whole promise: paste a token, tap the link, delivery works — no third and fourth
    variable, no redeploy to fill them in."""
    written, argv, log = _capture(tmp_path, fake_id="8675309",
                                  TELEGRAM_BOT_TOKEN="<bot-token>")
    assert written == ["TELEGRAM_ALLOWED_USERS=8675309", "TELEGRAM_HOME_CHANNEL=8675309"]
    assert "telegram_link.py --token <bot-token> --phrase Xk2p-9fQzR7t --boot" in argv
    assert "telegram linked" in log


def test_the_pairing_phrase_is_this_deploys_setup_code_and_nothing_new(tmp_path):
    """The capture must be authenticated or a stranger who guesses the bot's @username owns the
    deploy — and the secret for that is the setup code the boot already prints, resolved by the ONE
    resolver (env override first, else the code the receiver persisted to the volume)."""
    _, argv, _ = _capture(tmp_path / "file", fake_id="1", setup_code="on-volume",
                          TELEGRAM_BOT_TOKEN="<bot-token>")
    assert "--phrase on-volume" in argv
    _, argv, _ = _capture(tmp_path / "env", fake_id="1", setup_code="on-volume",
                          SOTTO_SETUP_CODE="from-env", TELEGRAM_BOT_TOKEN="<bot-token>")
    assert "--phrase from-env" in argv


def test_no_setup_code_fails_closed_rather_than_linking_a_stranger(tmp_path):
    """No phrase, no capture: the linker is never even run, because an unauthenticated capture
    would hand the briefs to whoever messaged the bot first."""
    written, argv, log = _capture(tmp_path, fake_id="8675309", setup_code="",
                                  TELEGRAM_BOT_TOKEN="<bot-token>")
    assert written == [] and argv == ""
    assert "no setup code resolved yet" in log


def test_a_configured_chat_id_never_runs_the_capture(tmp_path):
    """Explicit configuration wins here too — and boot must not sit long-polling for a message the
    deployer already told us not to wait for."""
    written, argv, _ = _capture(tmp_path, fake_id="8675309",
                                TELEGRAM_BOT_TOKEN="<bot-token>",
                                TELEGRAM_ALLOWED_USERS="4242")
    assert written == [] and argv == ""


def test_no_bot_token_is_not_a_capture(tmp_path):
    written, argv, _ = _capture(tmp_path, fake_id="8675309")
    assert written == [] and argv == ""


def test_a_capture_that_times_out_lets_the_boot_continue(tmp_path):
    """Fail toward silence: nothing is forwarded, the log says what to do, and the brief still
    composes — the next boot tries again."""
    written, argv, log = _capture(tmp_path, TELEGRAM_BOT_TOKEN="<bot-token>")
    assert written == []
    assert "--boot" in argv
    assert "telegram NOT linked yet" in log
    # the recovery instruction is the link + a RESTART; "message your bot" alone was unreachable
    # advice while a running gateway ate the message (see the gateway tests below)
    assert "tap the pairing link above, then restart this deploy" in log


# ── 6: an unlinked Telegram gets no gateway ──────────────────────────────────────────────────────

def _gateway(tmp_path, channel: str, env_lines: str = "") -> tuple:
    """(START_GATEWAY, boot log) after running start.sh's own gateway decision against a
    ~/.hermes/.env holding `env_lines`."""
    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    envf = str(tmp_path / "hermes.env")
    with open(envf, "w", encoding="utf-8") as f:
        f.write(env_lines)
    script = (f'ENVF="{envf}"\n' + _block(GATEWAY_START, GATEWAY_END) +
              '\nprintf "%s\\n" "$START_GATEWAY"\n')
    out = _run(script, home, {"SOTTO_CRON_DELIVER": channel})
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    return lines[-1], "\n".join(lines[:-1])


def test_an_unlinked_telegram_never_starts_the_gateway(tmp_path):
    """The recovery instruction only works if the pairing message SURVIVES: `hermes gateway`
    long-polls getUpdates with the same bot token, so a running gateway would consume the message
    the next boot's capture is waiting for. An unlinked channel cannot deliver anyway."""
    start, log = _gateway(tmp_path, "telegram")
    assert start == "0"
    assert "telegram gateway NOT started" in log
    assert "restart this deploy" in log


def test_a_linked_telegram_starts_the_gateway_normally(tmp_path):
    """Linked is linked, however the id got there — the allowlist you set yourself, the id step 5a
    just captured, or one a previous boot wrote — as long as the bot that would carry it is there
    too: both halves land in the same ~/.hermes/.env, and either alone delivers nothing."""
    for line in ("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_ALLOWED_USERS=8675309\n",
                 "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_ALLOWED_USERS=8675309\nTELEGRAM_HOME_CHANNEL=8675309\n"):
        start, log = _gateway(tmp_path / str(len(line)), "telegram", line)
        assert start == "1" and "NOT started" not in log


def test_an_empty_allowlist_line_is_not_a_link(tmp_path):
    """`TELEGRAM_ALLOWED_USERS=` with nothing after it denies every user in Hermes too."""
    assert _gateway(tmp_path, "telegram", "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_ALLOWED_USERS=\n")[0] == "0"


def test_every_other_channel_starts_the_gateway_as_before(tmp_path):
    """The rule is Telegram-shaped on purpose: WhatsApp has its own pairing step and gateway, and a
    channel Sotto cannot probe must never lose its gateway over a link state nobody can see."""
    for channel in ("whatsapp", "local", "discord", "slack", "signal", "bluebubbles"):
        start, log = _gateway(tmp_path / channel, channel)
        assert start == "1" and log == ""


# ── the channel is resolved ONCE: no literal channel name may stand in for the answer ────────────

# Every place a delivery channel used to be guessed. Each now takes the resolved value from the ONE
# decider (start.sh step 0.4, mirrored by receiver._deliver_target) — a `whatsapp` fallback here was
# how a Telegram deploy could still re-register its crons onto WhatsApp.
NO_FALLBACK = [
    os.path.join(HERMES, "adapters", "hermes", "reconcile_crons.py"),
    os.path.join(HERMES, "runtime", "trigger-receiver", "receiver.py"),
    os.path.join(HERMES, "sotto-chief-of-staff", "setup", "SKILL.md"),
    os.path.join(HERMES, "sotto-chief-of-staff", "routines", "SKILL.md"),
    os.path.join(HERMES, "sotto-chief-of-staff", "relationship-pulse", "SKILL.md"),
]


def test_no_delivery_default_is_spelled_as_a_channel_name():
    for path in NO_FALLBACK:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for bad in (':-whatsapp', ':-telegram',
                    'SOTTO_CRON_DELIVER", "whatsapp"', "SOTTO_CRON_DELIVER', 'whatsapp'",
                    'deliver: str = "whatsapp"', 'deliver="whatsapp"'):
            assert bad not in text, f"{os.path.relpath(path, HERMES)} still falls back to {bad!r}"


def test_a_stale_id_from_a_previous_bot_never_survives_a_failed_relink(tmp_path):
    """Rotate the token, and the capture times out: nothing the OLD bot captured may stay in .env.
    It would read as linked, start the gateway, and let it eat the next pairing message — which is
    exactly the loop the gateway gate exists to break (external review, Sep 1)."""
    envf = str(tmp_path / "hermes.env")
    with open(envf, "w", encoding="utf-8") as f:
        f.write("TELEGRAM_ALLOWED_USERS=111\nTELEGRAM_HOME_CHANNEL=111\nGOOGLE_AI_API_KEY=keep-me\n")
    written, _argv, log = _capture(tmp_path, fake_id="", envf=envf,
                                   TELEGRAM_BOT_TOKEN="<new-token>")
    assert not any(ln.startswith("TELEGRAM_ALLOWED_USERS=") for ln in written), written
    assert not any(ln.startswith("TELEGRAM_HOME_CHANNEL=") for ln in written), written
    assert "GOOGLE_AI_API_KEY=keep-me" in written        # only the channel ids are dropped
    assert "NOT linked yet" in log


def test_a_successful_relink_writes_the_new_id(tmp_path):
    """The other half: the drop is not a regression for the normal path."""
    envf = str(tmp_path / "hermes.env")
    with open(envf, "w", encoding="utf-8") as f:
        f.write("TELEGRAM_ALLOWED_USERS=111\nTELEGRAM_HOME_CHANNEL=111\n")
    written, _argv, _log = _capture(tmp_path, fake_id="8675309", envf=envf,
                                    TELEGRAM_BOT_TOKEN="<new-token>")
    assert "TELEGRAM_ALLOWED_USERS=8675309" in written
    assert "TELEGRAM_HOME_CHANNEL=8675309" in written


# ── the laptop lane: the installer's channel has to outlive the installer ────────────────────────

def test_the_local_installer_persists_its_channel_for_later_skill_runs(tmp_path):
    """The installer's shell ends; the skills that register a routine later read
    $SOTTO_CRON_DELIVER with no fallback of their own. Without this the next process runs
    `--deliver ""` and the brief lands in the local sink nobody reads (external review, Sep 1)."""
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    with open(os.path.join(root, "adapters", "hermes", "install.sh"), encoding="utf-8") as f:
        installer = f.read()
    head = installer.index('SOTTO_CRON_DELIVER="${SOTTO_CRON_DELIVER:-whatsapp}"')
    block = installer[head:installer.index("cron_rows()", head)]
    envf = str(tmp_path / "hermes.env")
    script = (f'HERMES_HOME="{tmp_path}"\nENVF="{envf}"\nDRY_RUN=0\ntouch "$ENVF"\n'
              'upsert_env() {\n'
              '  grep -v "^$1=" "$ENVF" > "$ENVF.tmp" 2>/dev/null || true\n'
              '  mv "$ENVF.tmp" "$ENVF"\n'
              "  printf '%s=%s\\n' \"$1\" \"$2\" >> \"$ENVF\"\n"
              '}\n') + block
    out = _run(script, str(tmp_path), {})
    assert out.returncode == 0, out.stderr
    with open(envf, encoding="utf-8") as f:
        assert "SOTTO_CRON_DELIVER=whatsapp" in f.read()


def test_a_boot_that_cannot_pair_still_drops_the_previous_bots_ids(tmp_path):
    """The fail-closed branch is exactly when stale ids are most dangerous: no setup code means no
    capture, so nothing re-adds them — and an old allowlist left behind would read as linked and
    start a gateway that eats the next pairing message (external review, Sep 1)."""
    envf = str(tmp_path / "hermes.env")
    with open(envf, "w", encoding="utf-8") as f:
        f.write("TELEGRAM_ALLOWED_USERS=111\nTELEGRAM_HOME_CHANNEL=111\n")
    written, argv, log = _capture(tmp_path, fake_id="8675309", setup_code="", envf=envf,
                                  TELEGRAM_BOT_TOKEN="<new-token>")
    assert argv == "", "no setup code, no capture — pairing a stranger is worse than not pairing"
    assert not any(ln.startswith("TELEGRAM_") for ln in written), written
    assert "no setup code" in log


def test_the_gateway_needs_a_bot_as_well_as_a_recipient(tmp_path):
    """Half a configuration delivers nothing: an allowlist with no bot token must not start the
    Telegram gateway any more than a bot with nobody to talk to."""
    envf = str(tmp_path / "hermes.env")

    def _gate(lines: str) -> str:
        with open(envf, "w", encoding="utf-8") as f:
            f.write(lines)
        script = (f'ENVF="{envf}"\n' + _block(GATEWAY_START, GATEWAY_END)
                  + 'echo "START_GATEWAY=$START_GATEWAY"\n')
        out = _run(script, str(tmp_path), {"SOTTO_CRON_DELIVER": "telegram"})
        assert out.returncode == 0, out.stderr
        return out.stdout

    assert "START_GATEWAY=0" in _gate("TELEGRAM_ALLOWED_USERS=8675309\n")
    assert "START_GATEWAY=0" in _gate("TELEGRAM_BOT_TOKEN=<bot>\n")
    assert "START_GATEWAY=1" in _gate("TELEGRAM_BOT_TOKEN=<bot>\nTELEGRAM_ALLOWED_USERS=8675309\n")


def test_rerunning_the_installer_keeps_the_channel_you_chose(tmp_path):
    """A plain re-run must not undo a choice: environment first, then what a previous run persisted,
    and only then the laptop default (external review, Sep 1)."""
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    with open(os.path.join(root, "adapters", "hermes", "install.sh"), encoding="utf-8") as f:
        installer = f.read()
    head = installer.index("# Resolution order, and re-running this installer")
    block = installer[head:installer.index("cron_rows()", head)]
    envf = str(tmp_path / "hermes.env")

    def _resolve(seed: str, **env) -> str:
        with open(envf, "w", encoding="utf-8") as f:
            f.write(seed)
        script = (f'HERMES_HOME="{tmp_path}"\nENVF="{envf}"\nDRY_RUN=0\n'
                  'env_file_get() { [ -f "$ENVF" ] && sed -n "s/^$1=//p" "$ENVF" | tail -1 || true; }\n'
                  'upsert_env() {\n'
                  '  grep -v "^$1=" "$ENVF" > "$ENVF.tmp" 2>/dev/null || true\n'
                  '  mv "$ENVF.tmp" "$ENVF"\n'
                  "  printf '%s=%s\\n' \"$1\" \"$2\" >> \"$ENVF\"\n"
                  '}\n') + block + 'echo "RESOLVED=$SOTTO_CRON_DELIVER"\n'
        out = _run(script, str(tmp_path), env)
        assert out.returncode == 0, out.stderr
        return out.stdout

    assert "RESOLVED=telegram" in _resolve("SOTTO_CRON_DELIVER=telegram\n")     # the persisted choice
    assert "RESOLVED=whatsapp" in _resolve("")                                   # nothing chosen yet
    assert "RESOLVED=discord" in _resolve("SOTTO_CRON_DELIVER=telegram\n",
                                           SOTTO_CRON_DELIVER="discord")         # env still wins
