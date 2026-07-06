# Uninstalling Sotto Bridge

Complete removal — the app, its login item, the Keychain token, all local state, and the Full Disk
Access grant. Nothing here touches your actual data (Messages, WhatsApp, etc.) — the Bridge only ever
read it.

## 1. Quit the app
Click the menu bar icon → **Quit** (this also stops the supervised `sotto-bridged` engine).

## 2. Disable the login item
Either flip **Start at login** off in the app before quitting, or afterwards:
**System Settings → General → Login Items & Extensions** → remove/toggle off **Sotto Bridge**.

## 3. Delete the app
```bash
rm -rf "/Applications/Sotto Bridge.app"
```

## 4. Remove the Keychain token
The pairing token lives in your login keychain under the service `ai.sotto.bridge`:
```bash
security delete-generic-password -s ai.sotto.bridge
```
(Or Keychain Access → search `ai.sotto.bridge` → delete.)

## 5. Delete local state
Status/config files and any legacy Bridge state:
```bash
rm -rf ~/Library/Application\ Support/Sotto
rm -rf ~/.sotto
```
(`~/Library/Application Support/Sotto/` holds the host URL, source toggles, and status files;
`~/.sotto/` is legacy state from pre-app CLI installs — fine if it doesn't exist.)

## 6. Remove the Full Disk Access entry
**System Settings → Privacy & Security → Full Disk Access** → select **Sotto Bridge** → **–** (minus).

## 7. Cloud side (optional)
If you're done with Sotto entirely, delete the Railway service (and its `/data` volume) from the
Railway dashboard — that removes the WhatsApp session, Google token, and knowledge graph. If you're
only removing the Mac app, leave Railway alone: cron briefs keep working from Google data, just
without local iMessage/WhatsApp signals.

---

Reinstalling later? Download the signed app from
<https://github.com/kothari-nikunj/sotto/releases/latest> and re-pair via the setup link.
