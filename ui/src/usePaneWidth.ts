import { useEffect, useState } from "react";

/** A draggable, remembered width for a side pane.
 *
 *  `edge` says which side the handle lives on, which is the only thing that
 *  differs between the two panes: dragging the left sidebar's right edge grows
 *  it as x increases, while dragging the detail panel's left edge grows it as x
 *  decreases. Sharing one hook keeps the clamping and the persistence identical.
 */
export function usePaneWidth(
  key: string,
  fallback: number,
  { min = 200, max = 720, edge = "right" as "left" | "right" } = {},
) {
  const clamp = (n: number) => Math.min(max, Math.max(min, n));
  const [width, setWidth] = useState(() =>
    clamp(Number(localStorage.getItem(key)) || fallback),
  );

  useEffect(() => {
    try {
      localStorage.setItem(key, String(width));
    } catch {
      /* storage blocked: the pane still resizes, it just forgets */
    }
  }, [key, width]);

  const onPointerDown = (e: React.PointerEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = width;
    const sign = edge === "right" ? 1 : -1;
    const move = (ev: PointerEvent) =>
      setWidth(clamp(startWidth + sign * (ev.clientX - startX)));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  return { width, onPointerDown, reset: () => setWidth(fallback) };
}
