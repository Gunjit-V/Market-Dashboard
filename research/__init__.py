"""Phase 3 -- forecasting and evaluation.

The yardstick, built before anything to measure with it.

Phase 2 answered *what was knowable at time T*. Phase 3 asks what is
predictable, with what uncertainty, and whether it holds out of sample -- and
the honest answer to that depends far more on how a result is compared than on
what is fitted. Phase 2's exploratory work produced a 22-point direction
"edge" and a 67% error "improvement", and both disappeared under a correct
baseline or a second metric.

So this package ships the comparison, not the model. Phase 3A deliberately
contains no fitting at all:

``research.panel``      the rows, and the label they hide from models
``research.splits``     chronological splits and walk-forward windows
``research.baselines``  the trivial answers, as first-class predictors
``research.metrics``    accuracy, RMSE, correlation, power, multiplicity
``research.evaluate``   fitting and scoring, with the leakage checks
``research.report``     results that refuse to appear without a baseline
``research.dataset``    the one door to Phase 2's point-in-time datasets
``research.run``        a CLI that scores the baselines on a stored dataset

The rules the package enforces are numbered in
``docs/07-phase-3-brief.md`` S4, and every enforcement point names the one it
serves. See ``docs/08-evaluation.md`` for how to use it.
"""
