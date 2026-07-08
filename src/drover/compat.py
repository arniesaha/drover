"""Transition shims: legacy nexus-* CLI entry points (Drover, formerly Nexus).

Each shim prints a one-line deprecation notice to stderr, then runs the real
Drover entry point. These exist so live launchd/systemd units and muscle
memory keep working through the device cutover. Remove in the post-cutover
cleanup (docs/porting-and-cutover.md §7.6).
"""

from __future__ import annotations

import sys


def _deprecated(old: str, new: str) -> None:
    print(
        f"[drover] `{old}` is deprecated; use `{new}`. "
        "This shim will be removed after the cutover.",
        file=sys.stderr,
    )


def nexus_server() -> None:
    _deprecated("nexus-server", "drover-server")
    from drover.server.__main__ import main

    main()


def nexus_harnessd() -> None:
    _deprecated("nexus-harnessd", "drover-harnessd")
    from drover.server.harness.cli import main

    main()


def nexus_collect() -> None:
    _deprecated("nexus-collect", "drover-collect")
    from drover.collect.__main__ import main

    main()


def nexus_hook() -> None:
    _deprecated("nexus-hook", "drover-hook")
    from drover.hook.__main__ import main

    main()
