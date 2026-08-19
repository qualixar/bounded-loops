"""WHICH gate decided a lap, recorded beside the verdict in the ledger.

Its own module rather than a section of `plugins` for two reasons. It answers a different question —
`plugins` decides whether a verdict may be BELIEVED, this records who produced it — and `plugins`
reached the 800-line cap this repository enforces on itself, which is a cap worth keeping rather than
exempting the file that grew.
"""
from __future__ import annotations

from typing import Mapping

#: Cap on any single provenance value. A class name is chosen by the gate's own package, so it is
#: attacker-controlled length that ends up inside the ledger's hash chain. 200 is far past any real
#: class name and far short of a value that makes a receipt unreadable.
_PROVENANCE_VALUE_MAX = 200


def _short_value(value: object) -> str | None:
    """A provenance value, or None if it is not a usable short string."""
    if not isinstance(value, str) or not value:
        return None
    cleaned = "".join(character for character in value if character.isprintable())
    if not cleaned:
        return None
    return cleaned[:_PROVENANCE_VALUE_MAX]


def gate_provenance(
    gate: object,
    *,
    kind: object,
    plugin_kinds: frozenset[str],
    distributions: Mapping[str, str],
) -> dict[str, str]:
    """WHICH gate produced a verdict, derived by the HARNESS and never supplied by the gate.

    Written into ``LedgerEntry.gate`` — a sibling of ``verdict``, deliberately not a member of it,
    because everything inside ``verdict`` is authored by the gate and provenance a gate can write is
    not provenance.

    ``kind`` IS THE ARGUMENT THAT MAKES THE FIRST SENTENCE TRUE. It is the registry key the harness
    itself resolved in ``composition.wire`` — ``manifest.gate_kind``, or ``"command-override"`` for
    ``--gate-override``. This function used to read ``getattr(gate, "gate_kind", None)`` instead, so
    the value it called harness-derived came off the object under audit: ``GuardedGate`` freezes
    ``__setattr__`` but not ``object.__setattr__``, and
    ``object.__setattr__(g, "_kind", "pytest")`` on a plugin gate produced
    ``{'kind': 'pytest', 'source': 'shipped'}`` — a third-party gate recorded in the hash chain as a
    shipped one. Not a forged pass; a forged answer to "who decided this", which is the question this
    record exists to answer. The docstring asserting harness derivation was itself the strongest
    evidence it needed to be true.

    Keys, and exactly how much each is worth:
      ``kind``            the harness's own registry key, passed in above. ``_validated_kind``
                          refuses the names the harness reserves for itself, so it cannot be squatted.
      ``source``          ``shipped`` or ``plugin``, decided by the entry-point scan, not by the gate.
      ``distribution``    for a plugin only, from installed package METADATA. Absent for shipped
                          gates rather than hardcoded: ``source: shipped`` already says the gate came
                          with bounded-loops, and inventing a literal here would be one more string
                          to drift.
      ``implementation``  the concrete class that ran. **Self-reported** — a name the gate's own
                          package chose. Records WHAT ran, not that it is trustworthy.

    There is no ``measured`` key and must not be one until a vacuity probe exists. Whether a gate
    would actually catch a regression needs a per-gate mutation corpus, which cannot be built for
    arbitrary third-party code.

    NEVER RAISES. This runs while building a ledger row, and a provenance lookup that killed a run
    would make the audit trail the thing that breaks the audited work. A hostile or broken gate
    yields ``{}`` — an absent claim, which reads correctly, rather than a false one.
    """
    try:
        resolved = _short_value(kind)
        if resolved is None:
            return {}
        record = {
            "kind": resolved,
            "source": "plugin" if resolved in plugin_kinds else "shipped",
        }
        implementation = _short_value(type(getattr(gate, "wraps", gate)).__name__)
        if implementation is not None:
            record["implementation"] = implementation
        if record["source"] == "plugin":
            distribution = _short_value(distributions.get(resolved))
            if distribution is not None:
                record["distribution"] = distribution
        return record
    except KeyboardInterrupt:
        raise
    except BaseException:  # noqa: BLE001 — see NEVER RAISES above
        return {}
