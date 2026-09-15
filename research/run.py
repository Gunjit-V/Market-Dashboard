"""CLI: score the baselines on a stored dataset, walk-forward.

    python -m research.run --instrument "Nifty 50" --label fwd_rv_60m
    python -m research.run --label fwd_dir_30m --train 20 --test 5
    python -m research.run --label fwd_rv_60m --carrier rv_short --expanding

Phase 3A ships no models, so what this runs is the trivial answers against each
other: bias-corrected persistence measured against plain persistence and the
training mean, or the "always up" guess measured against the best naive answer.
That is the point rather than a placeholder. It is the acceptance test for the
yardstick itself -- if the harness cannot report a constant predictor honestly,
including saying that its ranking is undefined rather than zero, it cannot be
trusted to report a model.

Exit codes match ``features.build``: 0 the study ran, 3 a usage problem.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from features.store import DEFAULT_ROOT
from research.baselines import (
    AlwaysPredict,
    BestNaiveClassifier,
    BiasCorrectedPersistence,
    Persistence,
    TrainMeanRegressor,
)
from research.dataset import load_panel
from research.errors import ResearchError
from research.evaluate import walk_forward_study
from research.panel import CLASSIFICATION
from research.report import HypothesisLedger
from research.splits import walk_forward

EXIT_OK = 0
EXIT_USAGE = 3

#: Default carrier for a persistence forecast of realized volatility. On the
#: 5-minute set ``rv_short`` covers 100 minutes of returns, so it is the
#: closest trailing analogue of a 60-minute forward window.
DEFAULT_CARRIER = "rv_short"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m research.run",
        description=(
            "Evaluate the Phase 3A baselines on a stored feature dataset, "
            "walk-forward, with every result beside its comparison."
        ),
    )
    parser.add_argument("--instrument", default="Nifty 50")
    parser.add_argument("--timeframe", default="5m", choices=("1m", "5m"))
    parser.add_argument(
        "--label",
        default="fwd_rv_60m",
        help="the label to forecast, e.g. fwd_rv_60m or fwd_dir_30m",
    )
    parser.add_argument(
        "--carrier",
        default=DEFAULT_CARRIER,
        help="the feature persistence carries forward (regression labels only)",
    )
    parser.add_argument("--train", type=int, default=20, help="training sessions")
    parser.add_argument("--test", type=int, default=5, help="test sessions")
    parser.add_argument(
        "--gap",
        type=int,
        default=1,
        help="sessions dropped between training and test (minimum 1)",
    )
    parser.add_argument(
        "--step",
        type=int,
        default=None,
        help="sessions the origin advances per window (default: --test)",
    )
    parser.add_argument(
        "--expanding",
        action="store_true",
        help="anchor training at the first session instead of rolling it",
    )
    parser.add_argument(
        "--hypotheses",
        type=int,
        default=1,
        help=(
            "how many hypotheses this study belongs to, for the multiplicity "
            "adjustment (brief S4.6)"
        ),
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        panel, usability = load_panel(
            args.instrument, args.timeframe, args.label, root=args.out
        )
    except (FileNotFoundError, ResearchError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    print(f"instrument   {panel.instrument}  ({panel.timeframe})")
    print(f"label        {panel.label}  [{panel.kind}, {panel.horizon_minutes} min]")
    print(usability.render())

    if panel.kind == CLASSIFICATION:
        # The candidate is deliberately the *convenient* naive answer, so the
        # report shows it losing to the best one -- the exact comparison brief
        # S4.1 was written about.
        candidate = AlwaysPredict(1.0, name="always-up")
        baselines = [BestNaiveClassifier()]
    else:
        if not panel.has_column(args.carrier):
            print(
                f"--carrier {args.carrier!r} is not a column of this panel",
                file=sys.stderr,
            )
            return EXIT_USAGE
        candidate = BiasCorrectedPersistence(args.carrier)
        baselines = [TrainMeanRegressor(), Persistence(args.carrier)]

    try:
        splits = walk_forward(
            panel.sessions,
            train_sessions=args.train,
            test_sessions=args.test,
            gap_sessions=args.gap,
            step_sessions=args.step,
            expanding=args.expanding,
        )
    except ResearchError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    ledger = HypothesisLedger(
        tested=(args.label,)
        if args.hypotheses == 1
        else tuple(f"hypothesis-{i + 1}" for i in range(args.hypotheses)),
        primary=args.label if args.hypotheses == 1 else None,
    )

    try:
        report = walk_forward_study(candidate, baselines, panel, splits, ledger)
    except ResearchError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    print()
    print(report.render())
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
