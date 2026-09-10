"""Feature definitions, separated from feature computation.

A :class:`FeatureSpec` is *data describing a feature* -- its name, the history
it needs, whether it may look across session boundaries, and its tunable
parameters. The arithmetic lives elsewhere (``features/price.py`` and friends)
and is attached as ``fn``.

Keeping the description separate from the computation is what lets the rest of
the package answer questions without reading code:

* how much history must be fetched for this set?  (``lookback_bars``)
* when is a value honest rather than computed from a partial window?
  (``warmup_bars``)
* may this feature cross into a previous session?  (``scope``)
* what exactly did "volatility regime" mean in the dataset trained last month?
  (``params``, via the version hash in ``features/registry.py``)

Nothing here reads the database or computes a number; this module is pure
description and is deliberately dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence

# ``scope`` values.
#
# INTRADAY features restart at every session open: at 09:15 they have no
# history at all, and they never reach back into yesterday. This is the
# default, and it is what keeps a single column meaning a single thing -- an
# intraday return column that occasionally holds an 18-hour overnight move is
# two different features wearing one name.
#
# TRAILING features may read completed prior sessions. They exist so that
# "unusual" can mean something: a volatility regime or a volume z-score needs a
# baseline, and a baseline drawn from this morning alone mostly measures the
# normal intraday U-shape rather than anything informative. Trailing features
# still only ever read *completed* history, so the point-in-time guarantee is
# untouched -- see docs/point-in-time-data.md.
#
# ROLLING features read a continuous window of the most recent completed bars,
# regardless of which session they fall in, and *exclude any term that spans a
# session boundary*. They exist because a volatility estimate that restarts
# every morning is unavailable for the first quarter of each session, while the
# quantity it measures does not restart at all.
#
# The exclusion is not a nicety. Measured on Nifty 50 5-minute bars, a
# session-boundary log return has 7.6x the standard deviation of an intraday
# one -- 58x the variance. A single boundary return inside a 20-return window
# inflates the variance 3.9x and overstates realized volatility 2.0x, every
# morning, in a way that is indistinguishable from the genuine opening-session
# volatility it would be confused with.
#
# Only *path-dependent aggregates* may be ROLLING: a sum over per-bar terms
# survives dropping one term. A point-to-point span such as
# ``close[0]/close[-12]`` may not -- the overnight move sits inside a single
# ratio and cannot be separated from it, so those features stay INTRADAY.
#
# SESSION features are computed once per session from its boundary (the
# overnight gap is the only one today). Note they become available when the
# opening bar *closes*, not at the opening instant: with bar data alone, the
# 09:15 bar's open is not knowable until 09:20.
INTRADAY = "intraday"
ROLLING = "rolling"
TRAILING = "trailing"
SESSION = "session"

SCOPES = (INTRADAY, ROLLING, TRAILING, SESSION)

# Families from the Phase 2 brief. Tick/microstructure is deliberately absent:
# tick history is capped by TICK_RETENTION_DAYS, so there is not enough of it
# to train on (see docs/phase-2-summary.md).
FAMILIES = ("price", "volatility", "volume", "cross", "label")

# Bars in one regular NSE session, by timeframe. A trailing window is expressed
# in *sessions* rather than bars, because "5 days of history" is the meaningful
# unit and its bar count differs per timeframe.
BARS_PER_SESSION = {"1m": 375, "5m": 75}

#: Wall-clock minutes one bar covers. Used to check that a span of N bars
#: really covers N*minutes of clock time -- an in-session gap can stretch it,
#: and a column named ``ret_15m`` holding a 20-minute move is mislabelled data.
MINUTES_PER_BAR = {"1m": 1, "5m": 5}

# The trailing baseline, in completed sessions. Five is roughly a week: long
# enough to be a stable notion of "normal", short enough to follow a genuine
# regime shift. It is a default rather than a constant so that alternatives
# (14 was considered) can be built and compared as separately versioned
# datasets instead of being argued about.
DEFAULT_TRAILING_SESSIONS = 5


class SpecError(ValueError):
    """Raised when a feature definition is internally inconsistent."""


@dataclass(frozen=True)
class FeatureSpec:
    """One feature's definition.

    Parameters
    ----------
    name
        Column name in the emitted dataset. Unique within a
        :class:`FeatureSet`.
    family
        One of :data:`FAMILIES`. Grouping only; it carries no behaviour.
    timeframe
        ``"1m"`` or ``"5m"``. A spec is bound to one timeframe because its
        windows are counted in bars, and a 20-bar window means 20 minutes on
        one and 100 on the other.
    lookback_bars
        How many completed bars the computation reads, inclusive of the most
        recent one. ``2`` for a one-bar return (it needs the previous close
        too). The pipeline fetches ``max(required_bars)`` once for the whole
        set rather than querying per feature.
    scope
        :data:`INTRADAY`, :data:`TRAILING` or :data:`SESSION`, as above.
    trailing_sessions
        For :data:`TRAILING` specs only: how many *completed prior sessions*
        form the baseline. Defaults to :data:`DEFAULT_TRAILING_SESSIONS`.
        Meaningless -- and rejected -- on other scopes.
    warmup_bars
        Bars required before the value is honest. Defaults to
        ``lookback_bars``; a value below that would let the feature emit a
        number computed from a partial window, which is a lie dressed as data.
    params
        Tunables (window lengths, thresholds). They are part of the feature's
        identity and therefore part of the version hash, so changing one
        produces a distinguishable dataset rather than silently redefining an
        existing column.
    inputs
        Extra instruments this feature needs, beyond the one being built.
        Cross-index features declare their counterpart here (e.g.
        ``("Nifty Bank",)``); everything else leaves it empty.
    description
        One line, carried into the docs and the dataset manifest.
    fn
        The computation. Left unset in tests that only exercise description.
    """

    name: str
    family: str
    timeframe: str
    lookback_bars: int
    scope: str = INTRADAY
    trailing_sessions: int | None = None
    warmup_bars: int | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    inputs: Sequence[str] = ()
    description: str = ""
    fn: Callable[..., float] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.name:
            raise SpecError("name must not be empty")
        if self.family not in FAMILIES:
            raise SpecError(
                f"{self.name}: unknown family {self.family!r}; "
                f"choose from {', '.join(FAMILIES)}"
            )
        if self.timeframe not in BARS_PER_SESSION:
            raise SpecError(
                f"{self.name}: unknown timeframe {self.timeframe!r}; "
                f"choose from {', '.join(sorted(BARS_PER_SESSION))}"
            )
        if self.scope not in SCOPES:
            raise SpecError(
                f"{self.name}: unknown scope {self.scope!r}; "
                f"choose from {', '.join(SCOPES)}"
            )
        if self.lookback_bars < 1:
            raise SpecError(f"{self.name}: lookback_bars must be >= 1")

        if self.scope == TRAILING:
            sessions = self.trailing_sessions
            if sessions is None:
                object.__setattr__(self, "trailing_sessions", DEFAULT_TRAILING_SESSIONS)
            elif sessions < 1:
                raise SpecError(f"{self.name}: trailing_sessions must be >= 1")
        elif self.trailing_sessions is not None:
            raise SpecError(
                f"{self.name}: trailing_sessions is meaningful only for "
                f"scope={TRAILING!r}, not {self.scope!r}"
            )

        if self.warmup_bars is None:
            object.__setattr__(self, "warmup_bars", self.lookback_bars)
        elif self.warmup_bars < self.lookback_bars:
            # Emitting a value before the window is full would report a number
            # computed from fewer bars than the feature claims to use.
            raise SpecError(
                f"{self.name}: warmup_bars ({self.warmup_bars}) must be >= "
                f"lookback_bars ({self.lookback_bars})"
            )

        # Freeze the mutable arguments so a spec cannot be edited after
        # registration -- its identity is hashed, and a mutated spec would make
        # that hash a lie.
        object.__setattr__(self, "params", dict(self.params))
        object.__setattr__(self, "inputs", tuple(self.inputs))

    @property
    def bars_per_session(self) -> int:
        return BARS_PER_SESSION[self.timeframe]

    @property
    def required_bars(self) -> int:
        """Completed bars this feature needs, as a *fetch hint*.

        For intraday, rolling and session features this is the warmup. For a
        trailing feature it also spans the baseline sessions.

        It is a hint, not a contract: sessions are not uniformly full (5% of
        Nifty 50 sessions are short, one has 17 bars), and a rolling window
        that drops boundary terms consumes more bars than it yields returns.
        Computations therefore verify for themselves that they got enough
        usable observations and return INSUFFICIENT_HISTORY when they did not,
        rather than trusting this number.
        """
        warmup = self.warmup_bars or 0
        if self.scope == TRAILING:
            sessions = self.trailing_sessions or DEFAULT_TRAILING_SESSIONS
            return max(warmup, sessions * self.bars_per_session)
        return warmup

    def identity(self) -> dict[str, Any]:
        """The fields that define what this feature *means*.

        This is what the version hash is computed over. ``description`` and
        ``fn`` are excluded deliberately: rewording a docstring must not
        invalidate a dataset, while changing a window length must.
        """
        return {
            "name": self.name,
            "family": self.family,
            "timeframe": self.timeframe,
            "lookback_bars": self.lookback_bars,
            "scope": self.scope,
            "trailing_sessions": self.trailing_sessions,
            "warmup_bars": self.warmup_bars,
            "params": dict(sorted(self.params.items())),
            "inputs": list(self.inputs),
        }


@dataclass(frozen=True)
class FeatureSet:
    """An ordered collection of specs sharing one timeframe.

    Order is part of the contract. A feature vector is positional, so columns
    silently reordering between the dataset a model was trained on and the one
    it is served is a real and unpleasant production failure. The set preserves
    registration order and never sorts.
    """

    timeframe: str
    specs: tuple[FeatureSpec, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for spec in self.specs:
            if spec.timeframe != self.timeframe:
                raise SpecError(
                    f"{spec.name}: timeframe {spec.timeframe!r} does not match "
                    f"the set's {self.timeframe!r}"
                )
            if spec.name in seen:
                raise SpecError(f"duplicate feature name {spec.name!r}")
            seen.add(spec.name)

    def __iter__(self) -> Iterator[FeatureSpec]:
        return iter(self.specs)

    def __len__(self) -> int:
        return len(self.specs)

    @property
    def names(self) -> tuple[str, ...]:
        """Column names, in vector order."""
        return tuple(spec.name for spec in self.specs)

    @property
    def required_bars(self) -> int:
        """Bars the pipeline must fetch to evaluate every spec in the set."""
        return max((spec.required_bars for spec in self.specs), default=0)

    @property
    def required_instruments(self) -> tuple[str, ...]:
        """Counterpart instruments needed beyond the one being built."""
        extra: list[str] = []
        for spec in self.specs:
            for symbol in spec.inputs:
                if symbol not in extra:
                    extra.append(symbol)
        return tuple(extra)

    def by_family(self, family: str) -> tuple[FeatureSpec, ...]:
        return tuple(spec for spec in self.specs if spec.family == family)

    def select(self, names: Sequence[str]) -> FeatureSet:
        """A subset, preserving this set's order rather than ``names``'."""
        wanted = set(names)
        unknown = wanted - set(self.names)
        if unknown:
            raise SpecError(f"unknown feature(s): {', '.join(sorted(unknown))}")
        return FeatureSet(
            timeframe=self.timeframe,
            specs=tuple(s for s in self.specs if s.name in wanted),
        )

    def identity(self) -> list[dict[str, Any]]:
        """Per-spec identities, in vector order, for the version hash."""
        return [spec.identity() for spec in self.specs]


__all__ = [
    "BARS_PER_SESSION",
    "MINUTES_PER_BAR",
    "DEFAULT_TRAILING_SESSIONS",
    "FAMILIES",
    "INTRADAY",
    "ROLLING",
    "SCOPES",
    "SESSION",
    "TRAILING",
    "FeatureSet",
    "FeatureSpec",
    "SpecError",
]
