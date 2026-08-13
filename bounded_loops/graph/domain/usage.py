"""What one attempt actually consumed, as the component that ran it measured it.

Every field is optional, and ``None`` means UNMEASURABLE — the component that ran this
attempt cannot report this quantity — never zero. That distinction is the whole point of
this module. Reading an absent measurement as zero makes a budget silently unenforceable:
the cap never trips, no error is raised, and the operator believes they are protected while
spend runs away. A bound that cannot be checked is not a bound, so the runtime refuses
rather than guesses (see the BUDGET_UNMEASURABLE failure cause).

``reported_by`` is required whenever any quantity is present, because a number with no
stated source is not evidence: an operator auditing a spend total has to be able to tell a
provider-reported figure from a locally estimated one.

Money is integer micro-USD throughout — 1_000_000 microunits = 1 USD. No floats anywhere in
the spend path: replay of a run directory has to reproduce the same totals exactly, and
repeated float addition does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from bounded_loops.graph.domain.errors import GraphIntegrityError

#: Micro-USD per USD. The base currency is USD by decision; a deployment that bills in
#: another currency converts in its own price table rather than changing the unit here,
#: so a stored total always means the same thing.
MICROUNITS_PER_USD = 1_000_000

#: The measurable quantities, in the order they are reported in a receipt.
_QUANTITIES = ("input_tokens", "output_tokens", "cost_microunits", "wallclock_ms")


@dataclass(frozen=True)
class WorkerUsage:
    """One attempt's measured consumption. ``None`` on a field means unmeasurable.

    Only ``input_tokens``/``output_tokens`` are carried, not a separate reported total:
    every provider that reports token counts at all reports them split (OpenAI's
    prompt/completion, Anthropic's input/output, Gemini's prompt/candidates counts), so a
    third stored field would be a consistency constraint with no provider behind it. If one
    ever reports only a total, add the field then — with a test — rather than inventing a
    shape now and having adapters cram a total into ``input_tokens``, which would lie.

    ``cost_microunits`` is the provider's OWN reported charge when it gives one. It is kept
    separate from any figure derived from a price table because the two have different
    standing: the provider's number is the truth, a table's number is an estimate.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_microunits: int | None = None
    wallclock_ms: int | None = None
    reported_by: str | None = None

    def __post_init__(self) -> None:
        for field in _QUANTITIES:
            value = getattr(self, field)
            if value is None:
                continue
            # bool is a subclass of int, so True would otherwise read as 1 token.
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GraphIntegrityError(f"usage {field} must be a non-negative integer or None")
        if self.reported_by is not None and (
            not isinstance(self.reported_by, str) or not self.reported_by
        ):
            raise GraphIntegrityError("usage reported_by must be a non-empty string or None")
        if self.measured_anything and self.reported_by is None:
            raise GraphIntegrityError(
                "usage that reports a quantity must name what measured it: an unattributed "
                "number cannot be audited back to a provider or an estimate"
            )

    @property
    def measured_anything(self) -> bool:
        """Whether this carries at least one real measurement."""
        return any(getattr(self, field) is not None for field in _QUANTITIES)

    @property
    def total_tokens(self) -> int | None:
        """Input plus output, or ``None`` when either side is unmeasurable.

        Deliberately not ``(input or 0) + (output or 0)``: a half-known total reported as
        though it were whole is exactly the silent under-count that lets a token cap be
        walked through.
        """
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    def payload(self) -> dict[str, object]:
        """The durable receipt shape. Keys appear only for measured quantities.

        An empty result means "nothing was measured", which is also how a receipt written
        before usage existed reads — both are the same fact, so no extra flag is stored to
        distinguish them.
        """
        body: dict[str, object] = {
            field: getattr(self, field)
            for field in _QUANTITIES
            if getattr(self, field) is not None
        }
        if body:
            # Guaranteed non-None by __post_init__ whenever anything was measured. An
            # attribution with nothing attributed is dropped: it would be noise in the log.
            body["reported_by"] = self.reported_by
        return body


def usage_from_payload(raw: object) -> WorkerUsage:
    """Rebuild usage from a receipt, rejecting a shape this runtime never writes.

    Used when spend is re-derived from a run directory, so a hand-edited log cannot
    smuggle in a negative charge (which would REFUND budget) or an unknown key.
    """
    # Mapping, not dict: the event log hands back frozen payloads as MappingProxyType,
    # which is a Mapping but is NOT a dict subclass. Testing for dict here rejected every
    # usage block the runtime itself had just written.
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise GraphIntegrityError("usage must be an object with string keys")
    unknown = set(raw) - set(_QUANTITIES) - {"reported_by"}
    if unknown:
        raise GraphIntegrityError(f"usage has unknown keys: {sorted(unknown)}")
    reported_by = raw.get("reported_by")
    if reported_by is not None and not isinstance(reported_by, str):
        # Coercing this to None instead would surface as the confusing "must name what
        # measured it" error from __post_init__, hiding the actual defect in the log.
        raise GraphIntegrityError("usage reported_by must be a string")
    return WorkerUsage(
        input_tokens=_quantity(raw, "input_tokens"),
        output_tokens=_quantity(raw, "output_tokens"),
        cost_microunits=_quantity(raw, "cost_microunits"),
        wallclock_ms=_quantity(raw, "wallclock_ms"),
        reported_by=reported_by,
    )


def _quantity(raw: Mapping[str, object], field: str) -> int | None:
    value = raw.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraphIntegrityError(f"usage {field} must be an integer")
    return value
