# Save to Cairn — Chrome extension

A one-shortcut way to save the current tab into your local Cairn library, with a
Zotero-style popup that lets you file it into a collection/branch.

## Install (once)

1. Open `chrome://extensions` and turn on **Developer mode** (top-right).
2. Click **Load unpacked** and choose this `extension/` folder.
3. Open `chrome://extensions/shortcuts` and confirm/change the shortcut — it suggests
   **⌘⇧E** (Cmd+Shift+E). Set it to whatever you like.

## Use

- Press **⌘⇧E** (or click the ⛏ toolbar icon) on any page.
- A popup shows the tab; type/pick a **collection or branch** (autocompleted from your
  library) and optional extra tags, then **Save**.
- It talks to the local server at `http://localhost:8765`, so Cairn must be running.

No macOS Automation permission is involved — the extension reads the tab's own URL and
title and sends them to the local server. Never-store rules (video, meetings, chat, your
blocklist) still apply, so a blocked page reports "Skipped".
