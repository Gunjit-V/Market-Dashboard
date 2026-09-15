"""Chronological splits, the gap between them, and walk-forward windows.

Brief S4.3 and S4.4, made structural: a leaky split is not something this
harness warns about, it is something that cannot be constructed. Every
:class:`Split` validates itself, so a caller who shuffles dates and hands the
halves over gets a :class:`~research.errors.LeakageError` at construction
rather than a flattering number an hour later.

The three rules a split obeys
-----------------------------
**Order.** Every training session is strictly earlier than every test session.
A random split fails this immediately: with dates interleaved, the latest
training date is later than the earliest test date.

**A gap.** At least one session is dropped between the blocks. Labels in the
Phase 2 catalogue never cross a session boundary -- a forward window that would
run past 15:30 is recorded absent rather than stretched overnight -- so one
dropped session is a complete guarantee that no training label was observed
during the test window, whatever the horizon. The gap is stated in sessions
rather than days because a weekend is not a session, and counting calendar days
would silently shrink the real gap to zero across a Monday.

**Named sessions.** A split holds dates, not row positions. Row counts vary
per session (a deep-OTM strike trades in some minutes and not others), so a
split by row count would land in a different place on every instrument, and the
same split description would not reproduce.

Walk-forward
------------
:func:`walk_forward` yields many splits over one calendar. Brief S4.4 exists
because a single holdout is not evidence; an edge that appears in one window
and not the others is noise, and that is only visible with the windows laid
side by side. :func:`window_count` predicts how many there will be without
generating them, which is what the acceptance test checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

from research.errors import LeakageError, ResearchError

#: Sessions dropped between training and test unless a caller asks for more.
#: One is sufficient for every label in the Phase 2 catalogue; see the module
#: docstring. It is not zero because that sufficiency is a property of today's
#: labels, and a label that did cross a session boundary would silently make
#: zero wrong.
DEFAULT_GAP_SESSIONS = 1


def _ordered(days: Iterable[date], what: str) -> tuple[date, ...]:
    block = tuple(days)
    if len(set(block)) != len(block):
        raise ResearchError(f"{what} contains a repeated session")
    if list(block) != sorted(block):
        raise ResearchError(
            f"{what} is not in ascending order; a split is chronological or "
            f"it is not a split"
        )
    return block


@dataclass(frozen=True)
class Split:
    """One train / (validation) / test division of a session calendar.

    Parameters
    ----------
    train, test
        Session dates, ascending, disjoint. Both non-empty.
    validation
        Optional, and sits between the two: a model selected on data later
        than its test window is selected on the answer.
    gap_sessions
        How many calendar sessions were dropped between the blocks. Stated by
        the caller rather than inferred, because the dates alone cannot say
        whether 2026-09-04 and 2026-09-07 are adjacent sessions or have a
        holiday between them.
    index
        Position in a walk-forward run, for reporting. Zero for a lone split.
    """

    train: tuple[date, ...]
    test: tuple[date, ...]
    validation: tuple[date, ...] = ()
    gap_sessions: int = DEFAULT_GAP_SESSIONS
    index: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "train", _ordered(self.train, "train"))
        object.__setattr__(self, "test", _ordered(self.test, "test"))
        object.__setattr__(
            self, "validation", _ordered(self.validation, "validation")
        )

        if not self.train:
            raise ResearchError("a split needs at least one training session")
        if not self.test:
            raise ResearchError("a split needs at least one test session")
        if self.gap_sessions < DEFAULT_GAP_SESSIONS:
            raise LeakageError(
                f"gap_sessions={self.gap_sessions}: at least "
                f"{DEFAULT_GAP_SESSIONS} session must separate training from "
                f"test, so no training label was observed during the test "
                f"window"
            )

        blocks = [("train", self.train)]
        if self.validation:
            blocks.append(("validation", self.validation))
        blocks.append(("test", self.test))

        seen: dict[date, str] = {}
        for name, block in blocks:
            for day in block:
                if day in seen:
                    raise LeakageError(
                        f"{day} appears in both {seen[day]} and {name}"
                    )
                seen[day] = name

        for (earlier_name, earlier), (later_name, later) in zip(blocks, blocks[1:]):
            if earlier[-1] >= later[0]:
                raise LeakageError(
                    f"{later_name} starts {later[0]} but {earlier_name} runs to "
                    f"{earlier[-1]}; a split is by date and the later block must "
                    f"be strictly later"
                )

    @property
    def train_sessions(self) -> int:
        return len(self.train)

    @property
    def test_sessions(self) -> int:
        return len(self.test)

    @property
    def span(self) -> tuple[date, date]:
        """First training session to last test session."""
        return (self.train[0], self.test[-1])

    def describe(self) -> dict[str, object]:
        out: dict[str, object] = {
            "index": self.index,
            "train": [self.train[0].isoformat(), self.train[-1].isoformat()],
            "train_sessions": len(self.train),
            "test": [self.test[0].isoformat(), self.test[-1].isoformat()],
            "test_sessions": len(self.test),
            "gap_sessions": self.gap_sessions,
        }
        if self.validation:
            out["validation"] = [
                self.validation[0].isoformat(),
                self.validation[-1].isoformat(),
            ]
            out["validation_sessions"] = len(self.validation)
        return out

    def __str__(self) -> str:
        return (
            f"#{self.index} train {self.train[0]}..{self.train[-1]} "
            f"({len(self.train)}s) -> test {self.test[0]}..{self.test[-1]} "
            f"({len(self.test)}s), gap {self.gap_sessions}"
        )


def _calendar(sessions: Sequence[date]) -> tuple[date, ...]:
    calendar = _ordered(sessions, "the session calendar")
    if not calendar:
        raise ResearchError("the session calendar is empty")
    return calendar


def chronological_split(
    sessions: Sequence[date],
    test_sessions: int,
    validation_sessions: int = 0,
    gap_sessions: int = DEFAULT_GAP_SESSIONS,
) -> Split:
    """One split: the last *test_sessions* for test, the earliest for training.

    With a validation block requested, the calendar reads
    ``train | gap | validation | gap | test`` -- two gaps, because the
    validation block is itself a window whose labels must not reach into test.
    """
    calendar = _calendar(sessions)
    if test_sessions < 1:
        raise ResearchError("test_sessions must be at least 1")
    if validation_sessions < 0:
        raise ResearchError("validation_sessions cannot be negative")
    if gap_sessions < DEFAULT_GAP_SESSIONS:
        raise LeakageError(
            f"gap_sessions must be at least {DEFAULT_GAP_SESSIONS}"
        )

    gaps = gap_sessions * (2 if validation_sessions else 1)
    needed = test_sessions + validation_sessions + gaps + 1
    if len(calendar) < needed:
        raise ResearchError(
            f"{len(calendar)} sessions cannot carry a split needing {needed} "
            f"(1 train + {validation_sessions} validation + {test_sessions} "
            f"test + {gaps} gap)"
        )

    test = calendar[len(calendar) - test_sessions :]
    head = calendar[: len(calendar) - test_sessions - gap_sessions]
    if validation_sessions:
        validation = head[len(head) - validation_sessions :]
        train = head[: len(head) - validation_sessions - gap_sessions]
    else:
        validation = ()
        train = head
    return Split(
        train=train,
        test=test,
        validation=validation,
        gap_sessions=gap_sessions,
    )


def window_count(
    n_sessions: int,
    train_sessions: int,
    test_sessions: int,
    gap_sessions: int = DEFAULT_GAP_SESSIONS,
    step_sessions: int | None = None,
) -> int:
    """How many windows :func:`walk_forward` will yield, without building them.

    A closed form rather than ``len(list(...))`` so that a study can state up
    front how many windows it will report -- and so the acceptance test has
    something independent to check the generator against.
    """
    if min(train_sessions, test_sessions) < 1:
        raise ResearchError("train_sessions and test_sessions must be positive")
    if gap_sessions < DEFAULT_GAP_SESSIONS:
        raise LeakageError(f"gap_sessions must be at least {DEFAULT_GAP_SESSIONS}")
    step = test_sessions if step_sessions is None else step_sessions
    if step < 1:
        raise ResearchError("step_sessions must be positive")
    span = train_sessions + gap_sessions + test_sessions
    if n_sessions < span:
        return 0
    return (n_sessions - span) // step + 1


def walk_forward(
    sessions: Sequence[date],
    train_sessions: int,
    test_sessions: int,
    gap_sessions: int = DEFAULT_GAP_SESSIONS,
    step_sessions: int | None = None,
    expanding: bool = False,
) -> tuple[Split, ...]:
    """Rolling out-of-sample windows across *sessions*, earliest first.

    Parameters
    ----------
    train_sessions
        Sessions in each training block. With ``expanding``, the minimum
        size: training starts at the first session and grows, which is what a
        model refit every week actually sees.
    test_sessions
        Sessions in each test block.
    gap_sessions
        Sessions dropped between training and test in every window.
    step_sessions
        How far the origin advances each time. Defaults to *test_sessions*, so
        the test blocks tile the calendar without overlapping -- overlapping
        test blocks would count the same rows twice in the consistency figure
        and make the windows look more agreeing than they are.
    expanding
        Anchor the training block at the first session instead of rolling it.

    Returns a tuple rather than a generator: a study reports how many windows
    it ran, and the count of a generator is not knowable without consuming it.
    """
    calendar = _calendar(sessions)
    step = test_sessions if step_sessions is None else step_sessions
    total = window_count(
        len(calendar), train_sessions, test_sessions, gap_sessions, step
    )
    if total == 0:
        raise ResearchError(
            f"{len(calendar)} sessions cannot carry one window of "
            f"{train_sessions} train + {gap_sessions} gap + {test_sessions} test"
        )

    splits: list[Split] = []
    for index in range(total):
        origin = index * step
        train_end = origin + train_sessions
        test_start = train_end + gap_sessions
        splits.append(
            Split(
                train=calendar[0 if expanding else origin : train_end],
                test=calendar[test_start : test_start + test_sessions],
                gap_sessions=gap_sessions,
                index=index,
            )
        )
    return tuple(splits)


__all__ = [
    "DEFAULT_GAP_SESSIONS",
    "Split",
    "chronological_split",
    "walk_forward",
    "window_count",
]
