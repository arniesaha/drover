"""What an iteration spent, as a bound for the driver's cap.

Thin adapter over `drover.server.harness.usage`, which owns the per-provider
arithmetic. This module used to carry its own copy and got two things wrong
that mattered: it summed codex, whose usage is a running total, and it read
`input_tokens` alone, which for claude-code is not the input -- one real
session reported 19,109 there against 197,704,800 cache-read tokens. The cap
was therefore set against a number four orders of magnitude too small.

Kept numeric with an `observed` flag rather than Optional: the driver already
turns unobserved into NULL on its way to the ledger, and a cap wants an int.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from drover.server.harness.usage import session_totals


@dataclass(frozen=True)
class Usage:
    #: Every class that is billed as input, not `input_tokens` alone.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: False when the provider gives no way to tell a re-reported record from
    #: a genuine repeat, so this is an upper bound rather than a count.
    exact: bool = True
    #: Whether anything was measured at all. Zero tokens and nothing measured
    #: are different, and a budgeted loop has to be able to tell them apart.
    samples: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def observed(self) -> bool:
        return self.samples > 0


def from_events(
    events: Iterable[Mapping[str, Any]], harness: Optional[str] = None
) -> Usage:
    """Totals for one session, under `harness`'s own arithmetic."""
    materialized = list(events)
    totals = session_totals(harness, materialized)
    if not totals.observed:
        return Usage()
    completion = (totals.output_tokens or 0) + (totals.reasoning_tokens or 0)
    return Usage(
        prompt_tokens=totals.billable_input or 0,
        completion_tokens=completion,
        exact=totals.exact,
        samples=1,
    )
