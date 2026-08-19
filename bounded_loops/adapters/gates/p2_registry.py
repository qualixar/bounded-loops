"""The P2 gate classes, imported lazily so a missing adapter is not an import-time failure.

Its own module because `composition` sits over the 800-line cap this repository enforces on itself
under a ratcheted exemption, and the way that ratchet stays honest is that things leave the file
rather than the number going up. This table is self-contained: a name-to-class map and nothing else.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

# P2 gates (axe) — genuinely not yet
# authored. Lazy/guarded import, mirroring the qualixar block below.
# composition.wire() raises ManifestError("gate kind not yet implemented")
# for these until then — honest, not a silent stub. Note:
# the original draft imported these four unconditionally at module level,
# which would have made composition.py unimportable until every P2 gate
# module existed, blocking import entirely.
def _build_p2_registry() -> Mapping[str, type]:
    """Built in a function so the backing dict is UNREACHABLE once the proxy exists.

    ``MappingProxyType`` is a VIEW: freezing a dict that is still a module attribute protects
    nothing, because mutating the backing object changes what the proxy reports. This matters because
    ``composition._instantiate_gate`` reads ``P2_GATE_REGISTRY['osv']`` directly, so a plugin factory setting
    ``P2_GATE_REGISTRY['osv'] = Hijack`` chooses which class gets built.

    This paragraph used to end "and those gates are NOT wrapped, so ... would land an unchecked
    verdict". Untrue since `_instantiate_gate` began wrapping everything: a hijacked class is still
    validated, so the harm is a substituted implementation, not a forged pass. Corrected rather than
    deleted — a comment overstating a hole misdirects the next reader as badly as one understating it.
    """
    built: dict[str, type] = {}
    for _mod, _cls_name, _key in [
        ("axe", "AxeGate", "axe"),
        ("osv", "OsvGate", "osv"),
        ("checkov", "CheckovGate", "checkov"),
    ]:
        try:
            _module = __import__(f"bounded_loops.adapters.gates.{_mod}", fromlist=[_cls_name])
            built[_key] = getattr(_module, _cls_name)
        except ImportError:
            pass  # not yet implemented — wire() raises ManifestError at instantiation
    return MappingProxyType(built)


#: Frozen view. See `_build_p2_registry` for why the backing dict must not outlive it.
P2_GATE_REGISTRY: Mapping[str, type] = _build_p2_registry()
