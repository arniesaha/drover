"""Static architectural dependency guard (stdlib only; issue #1024 Slice B).

Builds the drover-internal import graph from source with ``ast`` and fails
when a protected root can reach a forbidden module. The graph includes
absolute, relative and ``from pkg import submodule`` imports, implicit parent
package ``__init__`` execution, function-scoped (lazy) imports and literal
``importlib.import_module("...")`` calls. ``if TYPE_CHECKING:`` blocks are
excluded because they never run. Contract and limitations:
docs/operations/structural-quality.md. Exit 0 pass, 1 violation, 4 harness error.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Boundary:
    name: str
    doc: str
    roots: tuple[str, ...]
    forbidden: tuple[str, ...]  # module prefixes, internal or third-party
    # Exact (importer module, imported module) edges characterized as existing
    # violations. Only --ratchet removes them; it fails if one disappears.
    known: tuple[tuple[str, str], ...] = ()


BOUNDARIES = (
    Boundary(
        name="work-coordinator-control",
        doc="docs/architecture.md#process-boundaries",
        roots=("drover.server.work_queue",),
        # Process Boundaries: the work coordinator is control-API-only; the
        # analytics role owns the lake, analytical routes, MCP and optional
        # OTLP; summaries/embeddings/advisory are derived-memory enrichment.
        forbidden=(
            "drover.server.lake",
            "drover.server.analytics_boundary",
            "drover.server.analytics_maintenance",
            "drover.server.mcp.tools",
            "drover.server.mcp.server",
            "drover.server.summarizer",
            "drover.server.embeddings",
            "drover.server.advisory",
            "drover.server.briefs",
            "drover.server.otlp",
            "drover.server.cockpit",
            "drover.server.web.app",
            "anthropic",
            "opentelemetry",
        ),
    ),
)


@dataclass(frozen=True)
class Edge:
    target: str
    lineno: int
    lazy: bool


def module_name(path: Path, src: Path) -> str:
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


class _Collector(ast.NodeVisitor):
    def __init__(self, module: str, is_package: bool):
        self.package = module if is_package else module.rpartition(".")[0]
        self.raw: list[tuple[str, tuple[str, ...], int, bool]] = []
        self.depth = 0

    def _scoped(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _scoped

    def visit_If(self, node: ast.If):
        if _is_type_checking(node.test):
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.raw.append((alias.name, (), node.lineno, self.depth > 0))

    def visit_ImportFrom(self, node: ast.ImportFrom):
        base = node.module or ""
        if node.level:
            anchor = self.package.split(".") if self.package else []
            if node.level > 1:
                anchor = anchor[: len(anchor) - (node.level - 1)]
            base = ".".join([*anchor, base] if base else anchor)
        names = tuple(a.name for a in node.names if a.name != "*")
        self.raw.append((base, names, node.lineno, self.depth > 0))

    def visit_Call(self, node: ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if (
            name == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and not node.args[0].value.startswith(".")
        ):
            self.raw.append((node.args[0].value, (), node.lineno, self.depth > 0))
        self.generic_visit(node)


def build_graph(src: Path) -> dict[str, list[Edge]]:
    files = {module_name(p, src): p for p in sorted(src.rglob("*.py"))}
    graph: dict[str, list[Edge]] = {}
    for module, path in files.items():
        collector = _Collector(module, path.name == "__init__.py")
        collector.visit(ast.parse(path.read_text(), filename=str(path)))
        edges: list[Edge] = []
        for base, names, lineno, lazy in collector.raw:
            # `from pkg import sub` loads pkg.sub when it is a module; either
            # way pkg itself runs, which the parent-chain loop below adds.
            targets = [f"{base}.{n}" for n in names if f"{base}.{n}" in files] or [base]
            for target in targets:
                parts = target.split(".")
                # importing a.b.c executes a, a.b and a.b.c
                for i in range(1, len(parts) + 1):
                    edges.append(Edge(".".join(parts[:i]), lineno, lazy))
        graph[module] = edges
    return graph


def policy_graph(graph, boundary: Boundary, *, eager_only: bool = False):
    """Import-time edges everywhere, plus the protected roots' own lazy imports.

    A function-scoped import inside a shared module (say control_outbox) only
    runs if a root calls that function; proving that needs a call graph, so
    those edges are out of scope (documented limitation), not silently passed.
    """
    return {
        m: [
            e
            for e in es
            if not e.lazy or (not eager_only and _matches(m, boundary.roots))
        ]
        for m, es in graph.items()
    }


def _matches(module: str, prefixes) -> str | None:
    for prefix in prefixes:
        if module == prefix or module.startswith(prefix + "."):
            return prefix
    return None


def find_violations(graph: dict[str, list[Edge]], boundary: Boundary) -> list[dict]:
    """Shortest path (BFS) from any root module to each forbidden prefix hit."""
    starts = [m for m in graph if _matches(m, boundary.roots)]
    if not starts:
        raise LookupError(f"{boundary.name}: no source modules match {boundary.roots}")
    parent: dict[str, tuple[str, Edge] | None] = {m: None for m in starts}
    queue = deque(starts)
    hits: dict[str, str] = {}
    while queue:
        current = queue.popleft()
        prefix = _matches(current, boundary.forbidden)
        if prefix and prefix not in hits:
            hits[prefix] = current
            continue  # do not explore through a forbidden module
        for edge in graph.get(current, ()):
            if edge.target not in parent:
                parent[edge.target] = (current, edge)
                queue.append(edge.target)
    violations = []
    for prefix, module in sorted(hits.items()):
        path, node = [], module
        while parent[node] is not None:
            src, edge = parent[node]
            path.append(
                f"{src}:{edge.lineno}{' (lazy)' if edge.lazy else ''} -> {node}"
            )
            node = src
        violations.append({"forbidden": prefix, "reached": module, "path": path[::-1]})
    return violations


def reachable(graph: dict[str, list[Edge]], roots) -> set[str]:
    seen = {m for m in graph if _matches(m, roots)}
    queue = deque(seen)
    while queue:
        for edge in graph.get(queue.popleft(), ()):
            if edge.target not in seen:
                seen.add(edge.target)
                queue.append(edge.target)
    return seen


def main(argv=None, *, src: Path | None = None, boundaries=BOUNDARIES) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--closure", action="store_true", help="print reachable modules")
    mode.add_argument(
        "--eager-only",
        action="store_true",
        help="import-time graph only (ignores the roots' own lazy imports too)",
    )
    mode.add_argument(
        "--ratchet",
        action="store_true",
        help="default policy minus exactly the characterized known edges",
    )
    args = parser.parse_args(argv)
    try:
        full = build_graph(src or ROOT / "src")
        report = {}
        for b in boundaries:
            graph = policy_graph(full, b, eager_only=args.eager_only)
            if args.closure:
                print(json.dumps({b.name: sorted(reachable(graph, b.roots))}, indent=1))
                continue
            stale = []
            if args.ratchet:
                stale, graph = _without_known(graph, b)
            report[b.name] = stale + find_violations(graph, b)
        if args.closure:
            return 0
    except (OSError, SyntaxError, LookupError) as exc:
        print(json.dumps({"classification": "harness_failure", "error": str(exc)}))
        return 4
    failed = any(report.values())
    print(
        json.dumps(
            {"classification": "violation" if failed else "pass", "boundaries": report},
            indent=1,
            sort_keys=True,
        )
    )
    return 1 if failed else 0


def _without_known(graph, boundary: Boundary):
    """Drop exactly the known edges; a known edge that no longer exists is stale."""
    stale = [
        {"stale_known_edge": f"{src} -> {dst}", "action": "remove it from known"}
        for src, dst in boundary.known
        if not any(e.target == dst for e in graph.get(src, ()))
    ]
    known = set(boundary.known)
    pruned = {
        m: [e for e in es if (m, e.target) not in known] for m, es in graph.items()
    }
    return stale, pruned


if __name__ == "__main__":
    sys.exit(main())
