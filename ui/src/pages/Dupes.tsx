import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { Button, Empty, ListSkeleton } from "../ui";

export function Dupes() {
  const query = useQuery({ queryKey: ["dupes"], queryFn: api.dupes });
  const merge = useMutation({
    mutationFn: api.mergeDupes,
    onSuccess: () => query.refetch(),
  });
  const groups = query.data?.groups ?? [];

  return (
    <div className="px-8 py-6">
      <h1 className="font-display text-[26px] leading-none">Duplicates</h1>
      <p className="mt-2 mb-5 max-w-xl text-sm text-muted">
        The same work stored under different URLs — an arXiv preprint and its
        published version, for instance. Found by title similarity with no model
        involved. Merging keeps the richest metadata and the earliest first-seen
        date, and records the other URLs as aliases so a re-ingest cannot bring
        them back.
      </p>

      {query.isLoading ? (
        <ListSkeleton rows={5} />
      ) : groups.length === 0 ? (
        <Empty title="No duplicates." hint="Every item is distinct." />
      ) : (
        <>
          <div className="mb-4 flex items-center gap-3">
            <Button onClick={() => merge.mutate()} disabled={merge.isPending}>
              Merge all {groups.length} groups
            </Button>
            {merge.isSuccess && (
              <span className="text-sm text-muted">
                Removed {merge.data.removed} duplicate rows.
              </span>
            )}
          </div>
          {groups.map((group, i) => (
            <div key={i} className="mb-5 border-l-[3px] border-accent py-1 pl-4">
              <p className="text-[15px]">{group[0].title}</p>
              {group.map((row) => (
                <p key={row.id} className="mt-1 font-mono text-[11.5px] text-muted">
                  [{row.id}] {row.source || "web"} — {row.canonical_url}
                </p>
              ))}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
