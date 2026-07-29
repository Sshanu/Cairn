import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useQueryClient } from "@tanstack/react-query";
import { api, type Job } from "../api";

/** Ingest and organize take minutes, so the UI starts a job and polls it
 *  rather than holding a request open. */
const ACTIVE_JOB_KEY = "tt-active-job";

export function JobBar() {
  // Persist the active job id so a page reload -- including the build-watcher's
  // auto-reload after a redeploy -- reconnects to a job that is still running,
  // instead of silently losing track of it (which read as "it never finishes").
  const [jobId, setJobId] = useState<string | null>(() => localStorage.getItem(ACTIVE_JOB_KEY));
  const qc = useQueryClient();

  useEffect(() => {
    const onStart = (e: Event) => {
      const id = (e as CustomEvent<string>).detail;
      localStorage.setItem(ACTIVE_JOB_KEY, id);
      setJobId(id);
    };
    window.addEventListener("tt-job", onStart);
    return () => window.removeEventListener("tt-job", onStart);
  }, []);

  const { data: job, isError } = useQuery<Job>({
    queryKey: ["job", jobId],
    queryFn: () => api.job(jobId!),
    enabled: !!jobId,
    retry: false,
    refetchInterval: (q) =>
      q.state.data && q.state.data.state === "running" ? 900 : false,
  });

  // A persisted id can be stale after a server restart (jobs live in memory, so a
  // restart both kills the job and forgets it -> 404). Drop it rather than showing
  // a dead bar forever.
  useEffect(() => {
    if (isError) {
      localStorage.removeItem(ACTIVE_JOB_KEY);
      setJobId(null);
    }
  }, [isError]);

  useEffect(() => {
    if (job && job.state !== "running") {
      qc.invalidateQueries();
      localStorage.removeItem(ACTIVE_JOB_KEY);
      const t = setTimeout(() => setJobId(null), 6000);
      return () => clearTimeout(t);
    }
  }, [job?.state]);

  if (!job) return null;
  const last = job.log[job.log.length - 1];

  return (
    <div className="flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-1.5 text-[12.5px]">
      <span
        className={
          job.state === "running"
            ? "thinking-dot text-accent"
            : job.state === "failed"
              ? "text-red-600"
              : "text-emerald-600"
        }
      >
        ●
      </span>
      <span className="font-mono text-muted">{job.kind}</span>
      <span className="max-w-64 truncate text-muted">
        {job.state === "failed" ? job.error : last ?? job.state}
      </span>
    </div>
  );
}

/** Called from the command palette so the bar picks the job up. */
export function startJob(kind: string, body: Record<string, unknown> = {}) {
  return api.startJob(kind, body).then(({ job }) => {
    window.dispatchEvent(new CustomEvent("tt-job", { detail: job }));
    return job;
  });
}
