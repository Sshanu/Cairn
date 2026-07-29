import clsx from "clsx";
import type { ReactNode } from "react";

/** The age spine: weight and colour encode days since first_seen.
 *  This is the one piece of data no other tool has, so nothing else on a row
 *  competes with it -- no badges, no progress rings, no status pills. */
export function spine(age: number | null | undefined) {
  const d = age ?? 0;
  if (d >= 90) return { width: 4, color: "var(--color-accent)" };
  if (d >= 30) return { width: 3, color: "color-mix(in oklab, var(--color-accent) 55%, transparent)" };
  if (d >= 7) return { width: 2, color: "var(--color-muted)" };
  return { width: 1, color: "var(--color-line)" };
}

export function Button({
  children,
  variant = "primary",
  className,
  ...rest
}: {
  children: ReactNode;
  variant?: "primary" | "ghost" | "quiet";
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...rest}
      className={clsx(
        "inline-flex items-center gap-2 rounded-lg text-sm font-medium",
        "transition-all duration-150 active:scale-[0.98]",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
        "disabled:opacity-40 disabled:pointer-events-none",
        variant === "primary" && "bg-accent text-white px-3.5 py-2 hover:brightness-105",
        variant === "ghost" &&
          "border border-line text-ink px-3.5 py-2 hover:bg-surface",
        variant === "quiet" && "text-muted px-2 py-1 hover:text-ink",
        className,
      )}
    >
      {children}
    </button>
  );
}

export function Tag({ name, onClick, onRemove }: {
  name: string;
  onClick?: () => void;
  onRemove?: () => void;
}) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded-md border border-line",
        "px-1.5 py-0.5 font-mono text-[11.5px] text-muted transition-colors",
        onClick && "cursor-pointer hover:border-accent hover:text-accent",
      )}
      onClick={(e) => {
        e.stopPropagation();
        onClick?.();
      }}
    >
      {name}
      {onRemove && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          className="ml-0.5 text-muted hover:text-accent"
          aria-label={`remove ${name}`}
        >
          ×
        </button>
      )}
    </span>
  );
}

export function Empty({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="py-20 text-center">
      <p className="font-display text-2xl text-ink">{title}</p>
      {hint && <p className="mt-2 text-sm text-muted">{hint}</p>}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-muted">
      <span className="thinking-dot">●</span>
      {label}
    </span>
  );
}

/** Shimmering placeholder rows that echo a list's shape while it loads --
 *  quieter than a spinner and it holds the layout so nothing jumps when the
 *  real rows arrive. Widths vary deterministically so it reads as text. */
export function ListSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="flex items-center gap-3 border-b border-line py-3 pl-4 pr-4"
          style={{ opacity: Math.max(0.15, 1 - i * 0.11) }}
        >
          <div className="skeleton h-3.5 w-3.5 shrink-0 rounded-[4px]" />
          <div className="min-w-0 flex-1">
            <div className="skeleton h-3.5" style={{ width: `${42 + ((i * 17) % 46)}%` }} />
            <div className="skeleton mt-2 h-2.5 w-28" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <h3 className="mb-2 text-[10.5px] font-semibold uppercase tracking-[0.09em] text-muted">
      {children}
    </h3>
  );
}

/** The Cairn mark: a stack of balanced stones, slightly offset so it reads as
 *  hand-placed rather than machined. Uses currentColor so it takes the accent
 *  wherever it sits. */
export function CairnMark({ size = 22, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
      <g fill="currentColor">
        <ellipse cx="12" cy="20.2" rx="7.2" ry="2.6" />
        <ellipse cx="11.3" cy="15.6" rx="5.6" ry="2.3" opacity="0.82" />
        <ellipse cx="12.7" cy="11.5" rx="4.4" ry="2.05" />
        <ellipse cx="11.6" cy="7.8" rx="3.1" ry="1.75" opacity="0.82" />
        <ellipse cx="12.2" cy="4.6" rx="1.95" ry="1.45" />
      </g>
    </svg>
  );
}
