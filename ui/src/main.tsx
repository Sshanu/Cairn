import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import {
  MutationCache,
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import App from "./App";
import { toast } from "./components/Toaster";
import { applyTheme, load } from "./settings";
import "./index.css";

// Apply the saved accent + theme BEFORE first paint. Nothing else runs applyTheme
// at startup, so without this a chosen accent/theme silently reverted on reload.
applyTheme(load());

// Never fail silently. Any failed request -- a dead backend, a timed-out agent,
// a bad response -- pops a toast so the user can act, instead of a spinner that
// hangs forever. Bursts of the same error are collapsed.
let lastKey = "";
let lastAt = 0;
function report(err: unknown, kind: string) {
  const msg = err instanceof Error ? err.message : "request failed";
  const now = Date.now();
  if (kind + msg === lastKey && now - lastAt < 4000) return;
  lastKey = kind + msg;
  lastAt = now;
  toast(`${kind}: ${msg}`);
}

const queryClient = new QueryClient({
  defaultOptions: {
    // Refetch when the window regains focus: after saving a tab from the browser
    // extension (an out-of-band write) and switching back, the sidebar counts and the
    // item list update on their own instead of needing a manual refresh.
    queries: { refetchOnWindowFocus: true, staleTime: 5_000, retry: 1 },
  },
  queryCache: new QueryCache({
    onError: (err, query) => {
      // The build-watcher polls every 10s; a dead server there is expected and
      // handled by reload, so it should not spam toasts.
      if (query.queryKey[0] === "build") return;
      report(err, "Couldn't load");
    },
  }),
  mutationCache: new MutationCache({
    onError: (err) => report(err, "Action failed"),
  }),
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
