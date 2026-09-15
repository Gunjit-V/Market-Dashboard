"""Deterministic synthetic panels for the Phase 3 harness tests.

No database, no network, no wall clock, and no unseeded randomness: the panels
here are built from a seeded uniform draw, so every assertion below holds on
every machine and every run. That matters more here than usual -- brief gate 8
asks that the same data and seed produce the same numbers, and a fixture that
wandered would make the check meaningless.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from typing import Sequence

from research.panel import CLASSIFICATION, REGRESSION, Panel

SESSION_OPEN = time(9, 15)

#: The carrier a persistence baseline reads in these fixtures, named after the
#: real feature it stands in for.
CARRIER = "rv_short"


def decision_times(days: Sequence[date], per_session: int, step: int = 5):
    """Decision times on the 5-minute grid, ascending, session by session."""
    out: list[datetime] = []
    for day in days:
        base = datetime.combine(day, SESSION_OPEN)
        out.extend(base + timedelta(minutes=step * (i + 1)) for i in range(per_session))
    return out


def volatility_panel(
    days: Sequence[date],
    per_session: int = 12,
    seed: int = 7,
    noise: float = 0.004,
    slope: float = 0.9,
    label: str = "fwd_rv_60m",
    horizon_minutes: int = 60,
    instrument: str = "Nifty 50",
) -> Panel:
    """A regression panel where the carrier genuinely predicts the target.

    ``target = slope * rv_short + uniform noise``. With *noise* at zero the
    relationship is exact, which is what lets a test assert a specific
    correlation or a specific bias-correction factor rather than a range.
    """
    rng = random.Random(seed)
    times = decision_times(days, per_session)
    rows: list[tuple[float, ...]] = []
    targets: list[float] = []
    for _ in times:
        carrier = 0.10 + 0.05 * rng.random()
        unrelated = rng.random()
        rows.append((carrier, unrelated))
        targets.append(slope * carrier + rng.uniform(-noise, noise))
    return Panel(
        instrument=instrument,
        timeframe="5m",
        label=label,
        kind=REGRESSION,
        horizon_minutes=horizon_minutes,
        feature_names=(CARRIER, "unrelated"),
        decision_times=tuple(times),
        rows=tuple(rows),
        targets=tuple(targets),
    )


def direction_panel(
    days: Sequence[date],
    per_session: int = 12,
    down_fraction: float = 0.65,
    seed: int = 11,
    label: str = "fwd_dir_30m",
    horizon_minutes: int = 30,
    instrument: str = "Nifty 50",
) -> Panel:
    """A classification panel with a known class balance.

    The outcomes are laid out deterministically rather than sampled, so a test
    knows exactly what "always predict down" scores -- which is the number the
    best-naive baseline exists to find.
    """
    rng = random.Random(seed)
    times = decision_times(days, per_session)
    n = len(times)
    downs = round(down_fraction * n)
    outcomes = [-1.0] * downs + [1.0] * (n - downs)
    rng.shuffle(outcomes)
    rows = tuple((rng.random(), rng.random()) for _ in times)
    return Panel(
        instrument=instrument,
        timeframe="5m",
        label=label,
        kind=CLASSIFICATION,
        horizon_minutes=horizon_minutes,
        feature_names=(CARRIER, "unrelated"),
        decision_times=tuple(times),
        rows=rows,
        targets=tuple(outcomes),
    )


def panel_of(
    times: Sequence[datetime],
    carrier: Sequence[float],
    targets: Sequence[float],
    kind: str = REGRESSION,
    label: str = "fwd_rv_60m",
    horizon_minutes: int = 60,
) -> Panel:
    """A panel from explicit values, for tests that hand-compute a metric."""
    return Panel(
        instrument="Nifty 50",
        timeframe="5m",
        label=label,
        kind=kind,
        horizon_minutes=horizon_minutes,
        feature_names=(CARRIER,),
        decision_times=tuple(times),
        rows=tuple((value,) for value in carrier),
        targets=tuple(targets),
    )
