import type { ComponentChildren } from "preact";

export const statusLabels = ["Needs input", "Needs approval", "Running", "Finished", "Failed", "Terminated", "Unknown"] as const;
export type StatusLabel = typeof statusLabels[number];

export function ScreenHeader({ title, eyebrow, subtitle, meta, actions, filters }: { title: string; eyebrow?: string; subtitle?: string; meta?: ComponentChildren; actions?: ComponentChildren; filters?: ComponentChildren }) {
  return <header class="screen-header"><div class="screen-header-top"><div>{eyebrow && <p class="eyebrow">{eyebrow}</p>}<h1>{title}</h1>{subtitle && <p class="screen-subtitle">{subtitle}</p>}</div><div class="screen-actions">{meta && <span class="header-meta">{meta}</span>}{actions}</div></div>{filters && <div class="header-filters">{filters}</div>}</header>;
}
export function Card({ children, className = "" }: { children: ComponentChildren; className?: string }) { return <article class={`card ${className}`}>{children}</article>; }
export function StatusChip({ status }: { status: StatusLabel }) { return <span class={`status-chip ${status.toLowerCase().replaceAll(" ", "-")}`}><i aria-hidden="true" />{status}</span>; }
export function ListRow({ title, meta, status, onClick, children, variant = "standard" }: { title: string; meta?: ComponentChildren; status?: StatusLabel; onClick?: () => void; children?: ComponentChildren; variant?: "standard" | "compact" | "data" }) {
  const content = <><div class="list-row-copy"><h2>{title}</h2>{meta && <p>{meta}</p>}</div>{children}{status && <StatusChip status={status} />}</>;
  return onClick ? <button class={`list-row ${variant}`} onClick={onClick}>{content}</button> : <div class={`list-row ${variant}`}>{content}</div>;
}
export function StateView({ kind, title, body, meta, action }: { kind: "empty" | "loading" | "error" | "unavailable"; title: string; body: string; meta?: string; action?: ComponentChildren }) { return <section class={`state-view ${kind}`} role={kind === "error" ? "alert" : undefined}><span class="state-icon" aria-hidden="true">{kind === "loading" ? "◌" : kind === "error" ? "!" : kind === "unavailable" ? "⊘" : "○"}</span><h2>{title}</h2><p>{body}</p>{meta && <small>{meta}</small>}{action && <div>{action}</div>}</section>; }
