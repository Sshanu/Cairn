import { useEffect, useState } from "react";
import { Check } from "lucide-react";

/** Fire a transient confirmation toast from anywhere. */
export function toast(message: string) {
  window.dispatchEvent(new CustomEvent("tt-toast", { detail: message }));
}

type Note = { id: number; message: string };

/** Renders the toasts. One instance, mounted once near the app root. */
export function Toaster() {
  const [notes, setNotes] = useState<Note[]>([]);

  useEffect(() => {
    let seq = 0;
    const onToast = (e: Event) => {
      const id = ++seq;
      setNotes((n) => [...n, { id, message: (e as CustomEvent<string>).detail }]);
      // The CSS animation runs ~2.2s; drop the node just after.
      setTimeout(() => setNotes((n) => n.filter((x) => x.id !== id)), 2400);
    };
    window.addEventListener("tt-toast", onToast);
    return () => window.removeEventListener("tt-toast", onToast);
  }, []);

  if (notes.length === 0) return null;

  return (
    <div className="pointer-events-none fixed bottom-5 left-1/2 z-50 -translate-x-1/2 space-y-2">
      {notes.map((n) => (
        <div
          key={n.id}
          className="toast flex items-center gap-2 rounded-lg border border-line bg-surface px-3.5 py-2 text-[13px] text-ink shadow-xl"
        >
          <Check size={14} className="text-emerald-600" />
          {n.message}
        </div>
      ))}
    </div>
  );
}
