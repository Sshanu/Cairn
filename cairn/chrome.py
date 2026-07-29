"""JXA bridge. Enumerate windows and tabs, find the front tab, close tabs.

No Chrome extension: osascript reads every window and tab already. The first
call triggers the macOS Automation prompt (System Settings > Privacy & Security
> Automation > your terminal or Raycast > Google Chrome).
"""

from __future__ import annotations

import json
import subprocess


class ChromeError(RuntimeError):
    pass


_ALL_TABS = """
(() => {
  const chrome = Application("Google Chrome");
  if (!chrome.running()) { return JSON.stringify([]); }
  const out = [];
  const windows = chrome.windows;
  for (let w = 0; w < windows.length; w++) {
    const tabs = windows[w].tabs;
    for (let t = 0; t < tabs.length; t++) {
      try {
        out.push({window: w, index: t, url: tabs[t].url(), title: tabs[t].title()});
      } catch (e) {}
    }
  }
  return JSON.stringify(out);
})()
"""

_FRONT_TAB = """
(() => {
  const chrome = Application("Google Chrome");
  if (!chrome.running() || chrome.windows.length === 0) { return JSON.stringify(null); }
  const tab = chrome.windows[0].activeTab();
  return JSON.stringify({window: 0, index: -1, url: tab.url(), title: tab.title()});
})()
"""

_FRONT_WINDOW_TABS = """
(() => {
  const chrome = Application("Google Chrome");
  if (!chrome.running() || chrome.windows.length === 0) { return JSON.stringify([]); }
  const out = [];
  const tabs = chrome.windows[0].tabs;
  for (let t = 0; t < tabs.length; t++) {
    try {
      out.push({window: 0, index: t, url: tabs[t].url(), title: tabs[t].title()});
    } catch (e) {}
  }
  return JSON.stringify(out);
})()
"""

_CLOSE_TABS = """
(() => {
  const targets = new Set(%s);
  const chrome = Application("Google Chrome");
  if (!chrome.running()) { return JSON.stringify(0); }
  let closed = 0;
  const windows = chrome.windows;
  for (let w = windows.length - 1; w >= 0; w--) {
    const tabs = windows[w].tabs;
    for (let t = tabs.length - 1; t >= 0; t--) {
      try {
        if (targets.has(tabs[t].url())) { tabs[t].close(); closed++; }
      } catch (e) {}
    }
  }
  return JSON.stringify(closed);
})()
"""


def _run(script: str, timeout: int = 30):
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript"],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:  # pragma: no cover - macOS only
        raise ChromeError("osascript not found; cairn needs macOS") from exc
    except subprocess.TimeoutExpired as exc:
        raise ChromeError("Chrome did not respond in time") from exc

    if result.returncode != 0:
        message = result.stderr.strip() or "osascript failed"
        if "-1743" in message or "not allowed" in message.lower():
            message += (
                "\nGrant automation access: System Settings > Privacy & Security > "
                "Automation > your terminal (or Raycast) > Google Chrome."
            )
        raise ChromeError(message)

    try:
        return json.loads(result.stdout.strip() or "null")
    except json.JSONDecodeError as exc:
        raise ChromeError(f"unexpected osascript output: {result.stdout!r}") from exc


def all_tabs() -> list[dict]:
    return _run(_ALL_TABS) or []


def front_tab() -> dict | None:
    return _run(_FRONT_TAB)


def front_window_tabs() -> list[dict]:
    return _run(_FRONT_WINDOW_TABS) or []


def close_tabs(urls: list[str]) -> int:
    if not urls:
        return 0
    return _run(_CLOSE_TABS % json.dumps(urls)) or 0
