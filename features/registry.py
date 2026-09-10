"""The feature catalogue and its version hash.

Two jobs.

**Catalogue.** One place where every feature is registered, so callers ask for
``feature_set("5m")`` instead of assembling a list of specs by hand at each
call site and drifting apart over time.

**Version hash.** ``feature_set_version(fs)`` is a deterministic content hash
over what every spec in the set *means*: name, family, timeframe, windows,
scope and params. It is written into each dataset's manifest, which is the
"versioned" half of the Phase 2 exit criterion:

* "which features was this model trained on?" has an exact answer;
* two datasets built from different definitions cannot be silently mixed;
* changing the trailing baseline from 5 sessions to 14 yields a second,
  cleanly-labelled dataset rather than a redefinition of the first.

The hash deliberately covers *definitions*, not values. Rebuilding the same
features over more trading days must not change the version; changing what a
feature means must.
"""

from __future__ import annotations

import hashlib
import json
from typing import Callable, Iterable

from features.spec import BARS_PER_SESSION, FeatureSet, FeatureSpec, SpecError

# Registration order is preserved per timeframe, and that order becomes the
# column order of the emitted vector.
_REGISTRY: dict[str, list[FeatureSpec]] = {tf: [] for tf in BARS_PER_SESSION}

# Bumped only when the *hashing scheme itself* changes -- i.e. when two
# identical spec sets would otherwise hash differently before and after a
# release. It keeps old version strings interpretable.
HASH_SCHEME_VERSION = 1

_VERSION_PREFIX = "fs"
_HASH_CHARS = 8


def register(spec: FeatureSpec) -> FeatureSpec:
    """Add *spec* to the catalogue and return it unchanged.

    Returning the spec lets the family modules register and keep a reference in
    one statement. Re-registering the same name for a timeframe is an error
    rather than a silent replacement: two definitions of one column name is
    exactly the ambiguity the version hash exists to prevent.
    """
    bucket = _REGISTRY.setdefault(spec.timeframe, [])
    if any(existing.name == spec.name for existing in bucket):
        raise SpecError(
            f"feature {spec.name!r} is already registered for "
            f"timeframe {spec.timeframe!r}"
        )
    bucket.append(spec)
    return bucket[-1]


def register_all(specs: Iterable[FeatureSpec]) -> tuple[FeatureSpec, ...]:
    """Register several specs, in order."""
    return tuple(register(spec) for spec in specs)


def _load_families() -> None:
    """Import the family modules so their specs register themselves.

    Imported lazily and defensively: the catalogue is useful (and testable)
    before every family module exists, so a family that is not yet written
    simply contributes nothing rather than breaking every caller.
    """
    for module in ("price", "volatility", "volume", "cross", "labels"):
        try:
            __import__(f"features.{module}")
        except ModuleNotFoundError as exc:  # pragma: no cover - build order only
            # Only tolerate the family module itself being absent; a genuine
            # missing dependency inside one must still surface.
            if exc.name != f"features.{module}":
                raise


#: Canonical family order for the emitted vector. Registration happens at
#: module import, so raw registration order depends on which module Python
#: imported first -- and a test that imports ``features.volatility`` directly
#: would otherwise produce a different column order, and therefore a different
#: version hash, than a normal run. Sorting by family (stably, so order
#: *within* a family is still registration order) makes the vector independent
#: of import sequence.
FAMILY_ORDER = ("price", "volatility", "volume", "cross", "label")


def _family_rank(spec: FeatureSpec) -> int:
    try:
        return FAMILY_ORDER.index(spec.family)
    except ValueError:  # a family not yet listed sorts last, deterministically
        return len(FAMILY_ORDER)


def _canonical_order(specs: Iterable[FeatureSpec]) -> list[FeatureSpec]:
    """Specs in canonical family order, stable within each family."""
    return sorted(specs, key=_family_rank)


def feature_set(
    timeframe: str,
    names: Iterable[str] | None = None,
    families: Iterable[str] | None = None,
) -> FeatureSet:
    """The registered features for *timeframe*, in registration order.

    Parameters
    ----------
    timeframe
        ``"1m"`` or ``"5m"``.
    names
        Optional subset. The set's own order is preserved regardless of the
        order given here, so a caller cannot accidentally permute the vector.
    families
        Optional family filter. Everything is returned by default; use
        :func:`state_features` and :func:`label_set` for the two splits that
        matter, rather than passing ``families`` by hand.
    """
    if timeframe not in BARS_PER_SESSION:
        raise SpecError(
            f"unknown timeframe {timeframe!r}; "
            f"choose from {', '.join(sorted(BARS_PER_SESSION))}"
        )
    _load_families()
    specs = tuple(_canonical_order(_REGISTRY.get(timeframe, ())))
    if families is not None:
        wanted = set(families)
        specs = tuple(s for s in specs if s.family in wanted)
    built = FeatureSet(timeframe=timeframe, specs=specs)
    if names is None:
        return built
    return built.select(list(names))


#: Families that describe the market at a decision time. Everything except
#: labels, which are what *happened next* and were not knowable at T.
STATE_FAMILIES = ("price", "volatility", "volume", "cross")


def state_features(timeframe: str) -> FeatureSet:
    """The features that make up a :class:`~features.state.MarketState`.

    Labels are excluded by construction. A label is the answer, not the
    question, and letting one into the state is how a dataset trains on itself.
    """
    return feature_set(timeframe, families=STATE_FAMILIES)


def label_set(timeframe: str) -> FeatureSet:
    """The forward-looking labels registered for *timeframe*."""
    return feature_set(timeframe, families=("label",))


def registered_names(timeframe: str) -> tuple[str, ...]:
    """Names registered for *timeframe*, in vector order."""
    return feature_set(timeframe).names


def _canonical(payload: object) -> str:
    """A stable JSON encoding: sorted keys, no incidental whitespace.

    Determinism is the whole point -- the same set of definitions must hash
    identically across processes, machines and Python versions.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def feature_set_digest(fs: FeatureSet) -> str:
    """The full SHA-256 hex digest of *fs*'s definitions."""
    payload = {
        "scheme": HASH_SCHEME_VERSION,
        "timeframe": fs.timeframe,
        "specs": fs.identity(),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def feature_set_version(fs: FeatureSet) -> str:
    """A short, human-quotable version string, e.g. ``fs_5m_a3f8c21e``.

    Truncated to :data:`_HASH_CHARS` because this string is read aloud, pasted
    into filenames and compared by eye. The full digest is kept alongside it in
    the manifest for anyone who wants the whole thing.
    """
    return f"{_VERSION_PREFIX}_{fs.timeframe}_{feature_set_digest(fs)[:_HASH_CHARS]}"


def describe(fs: FeatureSet) -> dict[str, object]:
    """A manifest-ready description of *fs*.

    Everything a reader needs to know what a dataset's columns mean, without
    the code that produced them.
    """
    return {
        "timeframe": fs.timeframe,
        "feature_set_version": feature_set_version(fs),
        "feature_set_digest": feature_set_digest(fs),
        "hash_scheme_version": HASH_SCHEME_VERSION,
        "feature_count": len(fs),
        "required_bars": fs.required_bars,
        "required_instruments": list(fs.required_instruments),
        "features": [
            {**spec.identity(), "description": spec.description} for spec in fs
        ],
    }


def _reset_registry_for_tests(factory: Callable[[], None] | None = None) -> None:
    """Clear the catalogue. Test-only.

    Registration happens at import time, so a test that wants to exercise the
    registry in isolation needs a way back to an empty one.
    """
    for bucket in _REGISTRY.values():
        bucket.clear()
    if factory is not None:
        factory()


__all__ = [
    "FAMILY_ORDER",
    "HASH_SCHEME_VERSION",
    "STATE_FAMILIES",
    "describe",
    "feature_set",
    "label_set",
    "feature_set_digest",
    "feature_set_version",
    "register",
    "register_all",
    "registered_names",
    "state_features",
]
