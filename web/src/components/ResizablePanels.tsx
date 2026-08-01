import React, { useCallback, useEffect, useId, useState } from "react";
import type { CSSProperties, ReactNode } from "react";

const STORAGE_PREFIX = "crew:resizable:";

export interface PanelProps {
  id: string;
  children: ReactNode;
  defaultWidth?: number;
  minWidth?: number;
  maxWidth?: number;
  className?: string;
}

interface NormalizedPanel {
  id: string;
  children: ReactNode;
  defaultWidth: number;
  minWidth: number;
  maxWidth: number | null;
  className?: string;
  resizable: boolean;
}

interface ResizablePanelsProps {
  storageKey?: string;
  className?: string;
  children: ReactNode;
}

interface ActiveSash {
  index: number;
  startX: number;
  startWidth: number;
}

function clamp(value: number, min: number, max: number | null): number {
  if (value < min) return min;
  if (max !== null && value > max) return max;
  return value;
}

function readStoredWidths(storageKey: string | undefined): Record<string, number> | null {
  if (!storageKey || typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(`${STORAGE_PREFIX}${storageKey}`);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      return Object.fromEntries(
        Object.entries(parsed).filter(([, v]) => typeof v === "number"),
      ) as Record<string, number>;
    }
  } catch {
    // ignore corrupt storage
  }
  return null;
}

function writeStoredWidths(storageKey: string | undefined, widths: Record<string, number>): void {
  if (!storageKey || typeof window === "undefined") return;
  try {
    window.localStorage.setItem(`${STORAGE_PREFIX}${storageKey}`, JSON.stringify(widths));
  } catch {
    // ignore storage errors (e.g. private mode)
  }
}

export function ResizablePanels({ storageKey, className, children }: ResizablePanelsProps): JSX.Element {
  const uniqueId = useId();
  const [active, setActive] = useState<ActiveSash | null>(null);
  const [isMobile, setIsMobile] = useState(false);
  const [isPointerCoarse, setIsPointerCoarse] = useState(false);

  const panels = normalizeChildren(children);
  const flexibleIndex = panels.findIndex((p) => !p.resizable);

  const [widths, setWidths] = useState<Record<string, number>>(() => {
    const stored = readStoredWidths(storageKey);
    const initial: Record<string, number> = {};
    for (const panel of panels) {
      if (panel.resizable) {
        initial[panel.id] = clamp(
          stored?.[panel.id] ?? panel.defaultWidth,
          panel.minWidth,
          panel.maxWidth,
        );
      }
    }
    return initial;
  });

  useEffect(() => {
    const update = () => {
      setIsMobile(window.innerWidth <= 720);
      setIsPointerCoarse(window.matchMedia("(pointer: coarse)").matches);
    };
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  useEffect(() => {
    if (active) return;
    writeStoredWidths(storageKey, widths);
  }, [widths, storageKey, active]);

  const handleMouseDown = useCallback(
    (index: number, e: React.MouseEvent) => {
      if (isMobile || isPointerCoarse) return;
      e.preventDefault();
      const panel = panels[index];
      if (!panel?.resizable) return;
      setActive({
        index,
        startX: e.clientX,
        startWidth: widths[panel.id] ?? panel.defaultWidth,
      });
    },
    [isMobile, isPointerCoarse, panels, widths],
  );

  useEffect(() => {
    if (!active) return;

    const handleMouseMove = (e: MouseEvent) => {
      const panel = panels[active.index];
      if (!panel) return;
      const delta = e.clientX - active.startX;
      // Left sash: drag right expands the left panel (delta > 0).
      // Right sash: drag left expands the right panel (delta < 0 relative to right edge).
      const sign = active.index < flexibleIndex ? 1 : -1;
      const nextWidth = clamp(active.startWidth + delta * sign, panel.minWidth, panel.maxWidth);
      setWidths((prev) => ({ ...prev, [panel.id]: nextWidth }));
    };

    const handleMouseUp = () => {
      setActive(null);
    };

    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
    return () => {
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
    };
  }, [active, panels, flexibleIndex]);

  const resetPanel = useCallback(
    (index: number) => {
      const panel = panels[index];
      if (!panel?.resizable) return;
      setWidths((prev) => ({ ...prev, [panel.id]: panel.defaultWidth }));
    },
    [panels],
  );

  const disabled = isMobile || isPointerCoarse;

  return (
    <div
      className={["resizable-panels", disabled ? "resizable-panels--disabled" : "", className || ""]
        .filter(Boolean)
        .join(" ")}
    >
      {panels.map((panel, index) => {
        const style: CSSProperties = panel.resizable
          ? {
              flex: "0 0 auto",
              width: widths[panel.id] ?? panel.defaultWidth,
              minWidth: panel.minWidth,
              maxWidth: panel.maxWidth ?? undefined,
            }
          : { flex: "1 1 0", minWidth: 0 };

        const isLast = index === panels.length - 1;
        const nextPanel = !isLast ? panels[index + 1] : null;
        const isLeftSash = panel.resizable && !nextPanel?.resizable;
        const isRightSash = !panel.resizable && nextPanel?.resizable;
        const hasSash = !disabled && !isLast && (isLeftSash || isRightSash);
        const resizeIndex = isRightSash ? index + 1 : index;
        const resizePanel = panels[resizeIndex];

        return (
          <React.Fragment key={`${uniqueId}-${panel.id}`}>
            <div className={panel.className} style={disabled ? undefined : style}>
              {panel.children}
            </div>
            {hasSash && resizePanel && (
              <div
                className={[
                  "resizable-panels__sash",
                  active?.index === resizeIndex ? "resizable-panels__sash--active" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                onMouseDown={(e) => handleMouseDown(resizeIndex, e)}
                onDoubleClick={() => resetPanel(resizeIndex)}
                role="separator"
                aria-label="调整面板宽度"
                aria-orientation="vertical"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
                    e.preventDefault();
                    const isLeft = resizeIndex < flexibleIndex;
                    const increase =
                      (isLeft && e.key === "ArrowRight") || (!isLeft && e.key === "ArrowLeft");
                    const current = widths[resizePanel.id] ?? resizePanel.defaultWidth;
                    const nextWidth = clamp(
                      current + (increase ? 20 : -20),
                      resizePanel.minWidth,
                      resizePanel.maxWidth,
                    );
                    setWidths((prev) => ({ ...prev, [resizePanel.id]: nextWidth }));
                  }
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    resetPanel(resizeIndex);
                  }
                }}
              />
            )}
          </React.Fragment>
        );
      })}

      {active && <div className="resizable-panels__overlay" />}
    </div>
  );
}

function normalizeChildren(children: ReactNode): NormalizedPanel[] {
  const result: NormalizedPanel[] = [];
  const arr = Array.isArray(children) ? children : [children];
  for (const child of arr) {
    if (!child || typeof child !== "object" || !("props" in child)) continue;
    const props = (child as { props: PanelProps }).props;
    const resizable = typeof props.defaultWidth === "number";
    result.push({
      id: props.id,
      children: props.children,
      defaultWidth: props.defaultWidth ?? 0,
      minWidth: props.minWidth ?? 160,
      maxWidth: props.maxWidth ?? null,
      className: props.className,
      resizable,
    });
  }
  return result;
}

export function ResizablePanel(_props: PanelProps): JSX.Element | null {
  // Marker component; ResizablePanels reads props from its children directly.
  return null;
}

ResizablePanels.Panel = ResizablePanel;
