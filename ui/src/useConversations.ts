import { useCallback, useEffect, useState } from "react";

import type { Answer } from "./api";

export type Turn =
  | { role: "you"; text: string }
  | { role: "cairn"; answer: Answer }
  | { role: "error"; text: string };

export type Conversation = {
  id: string;
  title: string;
  turns: Turn[];
  updated: number;
};

const KEY = "tt-conversations-v1";
const KEEP = 10; // only the ten most recent are worth keeping around

function load(): Conversation[] {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

/** The last ten Ask conversations, in localStorage, newest first.
 *
 *  Kept on the client on purpose: a chat with your own library is personal, and
 *  a single-user local tool has no reason to round-trip it through the server. */
export function useConversations() {
  const [list, setList] = useState<Conversation[]>(load);

  useEffect(() => {
    // Reflect edits made in another tab.
    const sync = () => setList(load());
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);

  const persist = useCallback((next: Conversation[]) => {
    const trimmed = next.sort((a, b) => b.updated - a.updated).slice(0, KEEP);
    setList(trimmed);
    try {
      localStorage.setItem(KEY, JSON.stringify(trimmed));
    } catch {
      /* storage full: the chat still works, it just will not be remembered */
    }
  }, []);

  const save = useCallback(
    (id: string, turns: Turn[]) => {
      if (turns.length === 0) return;
      const firstYou = turns.find((t) => t.role === "you");
      const title =
        firstYou && firstYou.role === "you" ? firstYou.text.slice(0, 80) : "Untitled";
      const rest = load().filter((c) => c.id !== id);
      persist([...rest, { id, title, turns, updated: Date.now() }]);
    },
    [persist],
  );

  const remove = useCallback(
    (id: string) => persist(load().filter((c) => c.id !== id)),
    [persist],
  );

  return { list, save, remove };
}
