import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ArrowUp, ExternalLink, MessageSquarePlus, MessagesSquare, Trash2 } from "lucide-react";
import { Link } from "react-router-dom";
import clsx from "clsx";

import { api } from "../api";
import { Empty } from "../ui";
import { useConversations, type Turn } from "../useConversations";

const EXAMPLES = [
  "what have I read that connects visual token pruning to attention sinks?",
  "summarise everything I have on instruction tuning for VLMs",
  "which papers here are about long-context inference?",
];

const newId = () =>
  `c${Date.now().toString(36)}${Math.floor(performance.now()).toString(36)}`;

const ACTIVE_KEY = "tt-active-conv";

function restoreActive(): { id: string; turns: Turn[] } {
  const id = localStorage.getItem(ACTIVE_KEY);
  if (id) {
    try {
      const convs = JSON.parse(localStorage.getItem("tt-conversations-v1") ?? "[]");
      const found = Array.isArray(convs) ? convs.find((c) => c?.id === id) : null;
      if (found) return { id, turns: found.turns ?? [] };
    } catch {
      /* fall through to a fresh chat */
    }
  }
  return { id: newId(), turns: [] };
}

export function Ask() {
  const restored = useRef(restoreActive());
  const [turns, setTurns] = useState<Turn[]>(restored.current.turns);
  const [convId, setConvId] = useState<string>(restored.current.id);
  const [draft, setDraft] = useState("");
  const [showList, setShowList] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const { list, save, remove } = useConversations();

  // Persist the conversation as it grows, so it survives navigation and reload.
  useEffect(() => {
    if (turns.length) save(convId, turns);
  }, [turns, convId, save]);

  // Remember which conversation is open, so navigating away and back restores it
  // rather than silently starting a blank one.
  useEffect(() => {
    localStorage.setItem(ACTIVE_KEY, convId);
  }, [convId]);

  const ask = useMutation({
    mutationFn: api.ask,
    onSuccess: (answer) => {
      setTurns((t) => [...t, { role: "cairn", answer }]);
      setTimeout(() => bottom.current?.scrollIntoView({ behavior: "smooth" }), 40);
    },
    onError: (err: Error) =>
      setTurns((t) => [...t, { role: "error", text: err.message }]),
  });

  function send(text: string) {
    if (!text.trim() || ask.isPending) return;
    setTurns((t) => [...t, { role: "you", text }]);
    setDraft("");
    ask.mutate(text);
    setTimeout(() => bottom.current?.scrollIntoView({ behavior: "smooth" }), 40);
  }

  function newChat() {
    setTurns([]);
    setConvId(newId());
    setShowList(false);
  }

  function openChat(id: string) {
    const c = list.find((x) => x.id === id);
    if (!c) return;
    setConvId(c.id);
    setTurns(c.turns);
    setShowList(false);
  }

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col px-8 py-6">
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-[26px] leading-none">Ask your library</h1>
          <p className="mt-2 max-w-xl text-sm text-muted">
            Retrieval is the FTS index; the model only reads what the index already
            found and cites the items it used. It cannot mention a paper you have
            not saved.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {list.length > 0 && (
            <div className="relative">
              <button
                onClick={() => setShowList((v) => !v)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-[12.5px] text-muted transition-colors hover:border-accent hover:text-accent"
              >
                <MessagesSquare size={13} /> Conversations
              </button>
              {showList && (
                <>
                  <div className="fixed inset-0 z-10" onClick={() => setShowList(false)} />
                  <div className="rise absolute right-0 z-20 mt-1 max-h-80 w-72 overflow-auto rounded-lg border border-line bg-surface p-1 shadow-xl">
                    {list.map((c) => (
                      <div key={c.id} className="group flex items-center gap-1">
                        <button
                          onClick={() => openChat(c.id)}
                          className={clsx(
                            "min-w-0 flex-1 truncate rounded-md px-2 py-1.5 text-left text-[12.5px] transition-colors hover:bg-accent-soft",
                            c.id === convId ? "text-accent" : "text-muted hover:text-ink",
                          )}
                          title={c.title}
                        >
                          {c.title}
                        </button>
                        <button
                          onClick={() => remove(c.id)}
                          title="Delete"
                          className="shrink-0 px-1 text-muted opacity-0 transition-opacity hover:text-red-600 group-hover:opacity-100"
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}
          {turns.length > 0 && (
            <button
              onClick={newChat}
              className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-[12.5px] text-muted transition-colors hover:border-accent hover:text-accent"
            >
              <MessageSquarePlus size={13} /> New chat
            </button>
          )}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto pr-1">
        {turns.length === 0 && (
          <div className="pt-6">
            <Empty title="Nothing asked yet." />
            <div className="mx-auto mt-2 max-w-lg space-y-2">
              {EXAMPLES.map((e) => (
                <button
                  key={e}
                  onClick={() => send(e)}
                  className="block w-full rounded-lg border border-line px-4 py-2.5 text-left text-[13.5px] text-muted transition-colors hover:border-accent hover:text-ink"
                >
                  {e}
                </button>
              ))}
            </div>

            {list.length > 0 && (
              <div className="mx-auto mt-8 max-w-lg">
                <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.09em] text-muted">
                  Recent conversations
                </p>
                <ul className="space-y-1">
                  {list.map((c) => (
                    <li key={c.id} className="group flex items-center gap-2">
                      <button
                        onClick={() => openChat(c.id)}
                        className="min-w-0 flex-1 truncate rounded-md px-2 py-1.5 text-left text-[13px] text-muted transition-colors hover:bg-surface hover:text-ink"
                        title={c.title}
                      >
                        {c.title}
                      </button>
                      <button
                        onClick={() => remove(c.id)}
                        title="Delete conversation"
                        className="shrink-0 text-muted opacity-0 transition-opacity hover:text-red-600 group-hover:opacity-100"
                      >
                        <Trash2 size={13} />
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        <div className="space-y-5">
          {turns.map((turn, i) =>
            turn.role === "you" ? (
              <div key={i} className="flex justify-end">
                <p className="max-w-[80%] rounded-2xl rounded-br-sm bg-accent px-4 py-2.5 text-[14.5px] text-white">
                  {turn.text}
                </p>
              </div>
            ) : turn.role === "error" ? (
              <div key={i} className="rise rounded-xl border border-red-300 bg-red-50 px-4 py-3 text-[13.5px] text-red-700 dark:bg-red-950/30">
                <p className="font-medium">Could not answer</p>
                <p className="mt-1 opacity-80">{turn.text}</p>
                <p className="mt-2 text-[11.5px] opacity-70">
                  Ask needs an agent. Pick a provider in{" "}
                  <Link to="/settings" className="underline hover:text-red-900">
                    Settings → Agent
                  </Link>{" "}
                  — Claude, OpenAI, Ollama or Codex — and add a key if it needs one.
                </p>
              </div>
            ) : (
              <div key={i} className="rise">
                <p className="whitespace-pre-wrap text-[14.5px] leading-relaxed">
                  {turn.answer.answer}
                </p>
                {turn.answer.items.length > 0 && (
                  <div className="mt-3 space-y-0.5 border-t border-line pt-3">
                    {turn.answer.items.map((it) => (
                      <div key={it.id} className="group flex items-baseline gap-2">
                        {/* Open the actual item's detail, not a search for its title. */}
                        <Link
                          to={`/?item=${it.id}`}
                          className="min-w-0 flex-1 truncate font-mono text-[12px] text-muted transition-colors hover:text-accent"
                          title={it.title ?? it.canonical_url}
                        >
                          [{it.id}] {it.title ?? it.canonical_url}
                        </Link>
                        <a
                          href={it.canonical_url}
                          target="_blank"
                          rel="noreferrer"
                          title="Open in browser"
                          className="shrink-0 text-muted opacity-0 transition-opacity hover:text-accent group-hover:opacity-100"
                        >
                          <ExternalLink size={11} />
                        </a>
                      </div>
                    ))}
                  </div>
                )}
                <p className="mt-2 font-mono text-[11px] text-muted opacity-70">
                  retrieved {turn.answer.retrieved} items from the index
                </p>
              </div>
            ),
          )}

          {ask.isPending && (
            <p className="font-display text-lg text-muted">
              Thinking<span className="thinking-dot">…</span>
            </p>
          )}
        </div>
        <div ref={bottom} />
      </div>

      <div className="mt-4 flex items-end gap-2 rounded-xl border border-line bg-surface p-2 focus-within:border-accent">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(draft);
            }
          }}
          rows={1}
          placeholder="Ask anything about what you have read…"
          className="max-h-40 flex-1 resize-none bg-transparent px-2 py-1.5 text-[14.5px] outline-none placeholder:text-muted"
        />
        <button
          onClick={() => send(draft)}
          disabled={!draft.trim() || ask.isPending}
          className="rounded-lg bg-accent p-2 text-white transition-opacity disabled:opacity-30"
        >
          <ArrowUp size={16} />
        </button>
      </div>
    </div>
  );
}
