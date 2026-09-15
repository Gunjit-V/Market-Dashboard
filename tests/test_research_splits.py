"""Splits: chronology, the gap, and how many windows a calendar carries.

Brief S5 acceptance 2 and 3. The load-bearing test here is
``TestLeakyeSplitsAreRejected.test_a_random_split_is_refused``: a random split
is the single cheapest way to produce a result that is not one, and the harness
must refuse to construct it rather than report on it.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from research.errors import LeakageError, ResearchError
from research.splits import (
    DEFAULT_GAP_SESSIONS,
    Split,
    chronological_split,
    walk_forward,
    window_count,
)
from tests.featurelib import trading_days

CALENDAR = trading_days(date(2026, 3, 2), 40)


class TestSplitConstruction:
    def test_holds_its_blocks_and_their_sizes(self):
        split = Split(train=tuple(CALENDAR[:10]), test=tuple(CALENDAR[11:15]))
        assert split.train_sessions == 10
        assert split.test_sessions == 4
        assert split.span == (CALENDAR[0], CALENDAR[14])
        assert split.gap_sessions == DEFAULT_GAP_SESSIONS

    def test_describes_itself_for_a_report(self):
        split = Split(train=tuple(CALENDAR[:10]), test=tuple(CALENDAR[11:15]), index=3)
        described = split.describe()
        assert described["index"] == 3
        assert described["train_sessions"] == 10
        assert described["test"] == [
            CALENDAR[11].isoformat(),
            CALENDAR[14].isoformat(),
        ]

    def test_needs_both_blocks(self):
        with pytest.raises(ResearchError):
            Split(train=(), test=tuple(CALENDAR[:3]))
        with pytest.raises(ResearchError):
            Split(train=tuple(CALENDAR[:3]), test=())


class TestLeakySplitsAreRejected:
    def test_a_random_split_is_refused(self):
        # Brief S4.3 and S5 acceptance 2. Shuffle the calendar, take halves,
        # sort each so the blocks look tidy -- the interleaving is still there
        # and the split cannot be built.
        shuffled = list(CALENDAR)
        random.Random(0).shuffle(shuffled)
        with pytest.raises(LeakageError) as exc:
            Split(
                train=tuple(sorted(shuffled[:20])),
                test=tuple(sorted(shuffled[20:])),
            )
        assert "strictly later" in str(exc.value)

    def test_training_may_not_start_after_testing(self):
        with pytest.raises(LeakageError):
            Split(train=tuple(CALENDAR[20:30]), test=tuple(CALENDAR[:10]))

    def test_a_session_in_both_blocks_is_refused(self):
        with pytest.raises(LeakageError):
            Split(train=tuple(CALENDAR[:10]), test=tuple(CALENDAR[9:15]))

    def test_adjacent_blocks_without_a_gap_are_refused(self):
        with pytest.raises(LeakageError) as exc:
            Split(train=tuple(CALENDAR[:10]), test=tuple(CALENDAR[10:15]),
                  gap_sessions=0)
        assert "gap_sessions" in str(exc.value)

    def test_blocks_out_of_order_within_themselves_are_refused(self):
        with pytest.raises(ResearchError):
            Split(train=tuple(reversed(CALENDAR[:10])), test=tuple(CALENDAR[11:15]))

    def test_a_repeated_session_is_refused(self):
        with pytest.raises(ResearchError):
            Split(train=(CALENDAR[0], CALENDAR[0]), test=tuple(CALENDAR[11:15]))

    def test_validation_must_sit_between_training_and_test(self):
        with pytest.raises(LeakageError):
            Split(
                train=tuple(CALENDAR[:10]),
                validation=tuple(CALENDAR[20:25]),
                test=tuple(CALENDAR[11:15]),
            )


class TestChronologicalSplit:
    def test_takes_the_last_sessions_for_test(self):
        split = chronological_split(CALENDAR, test_sessions=5)
        assert split.test == tuple(CALENDAR[-5:])
        assert split.train == tuple(CALENDAR[:-6])
        assert split.train[-1] < split.test[0]

    def test_drops_exactly_the_gap_sessions(self):
        split = chronological_split(CALENDAR, test_sessions=5, gap_sessions=3)
        dropped = set(CALENDAR) - set(split.train) - set(split.test)
        assert len(dropped) == 3
        assert dropped == set(CALENDAR[-8:-5])

    def test_a_validation_block_sits_between_with_two_gaps(self):
        split = chronological_split(
            CALENDAR, test_sessions=5, validation_sessions=4, gap_sessions=1
        )
        assert split.validation == tuple(CALENDAR[-10:-6])
        assert split.test == tuple(CALENDAR[-5:])
        assert split.train[-1] < split.validation[0] < split.validation[-1]
        assert split.validation[-1] < split.test[0]
        assert len(split.train) == len(CALENDAR) - 5 - 4 - 2

    def test_refuses_a_calendar_too_short_to_carry_the_split(self):
        with pytest.raises(ResearchError) as exc:
            chronological_split(CALENDAR[:5], test_sessions=5)
        assert "cannot carry" in str(exc.value)

    def test_refuses_a_zero_gap(self):
        with pytest.raises(LeakageError):
            chronological_split(CALENDAR, test_sessions=5, gap_sessions=0)

    def test_refuses_an_empty_calendar(self):
        with pytest.raises(ResearchError):
            chronological_split([], test_sessions=1)

    def test_refuses_an_unsorted_calendar(self):
        with pytest.raises(ResearchError):
            chronological_split(list(reversed(CALENDAR)), test_sessions=5)


class TestWalkForward:
    def test_yields_the_expected_number_of_windows(self):
        # Brief S5 acceptance 3, over a known range: 40 sessions, blocks of
        # 10 train + 1 gap + 5 test = 16, stepping 5 at a time
        # -> (40 - 16)//5 + 1 = 5 windows.
        splits = walk_forward(CALENDAR, train_sessions=10, test_sessions=5)
        assert len(splits) == 5
        assert len(splits) == window_count(len(CALENDAR), 10, 5)

    @pytest.mark.parametrize(
        "n, train, test, gap, step, expected",
        [
            (40, 10, 5, 1, None, 5),
            (40, 10, 5, 1, 1, 25),
            (40, 20, 5, 1, 5, 3),
            (16, 10, 5, 1, None, 1),
            (15, 10, 5, 1, None, 0),
            (100, 60, 20, 1, 20, 1),
            (261, 20, 5, 1, 5, 48),
        ],
    )
    def test_the_count_matches_the_generator(self, n, train, test, gap, step, expected):
        calendar = trading_days(date(2026, 1, 1), n)
        assert window_count(n, train, test, gap, step) == expected
        if expected:
            assert len(walk_forward(calendar, train, test, gap, step)) == expected

    def test_windows_are_numbered_in_order(self):
        splits = walk_forward(CALENDAR, train_sessions=10, test_sessions=5)
        assert [s.index for s in splits] == [0, 1, 2, 3, 4]
        assert all(
            earlier.test[0] < later.test[0]
            for earlier, later in zip(splits, splits[1:])
        )

    def test_test_blocks_tile_without_overlapping_by_default(self):
        splits = walk_forward(CALENDAR, train_sessions=10, test_sessions=5)
        seen = [day for split in splits for day in split.test]
        assert len(seen) == len(set(seen))

    def test_a_rolling_origin_keeps_the_training_size_fixed(self):
        splits = walk_forward(CALENDAR, train_sessions=10, test_sessions=5)
        assert {len(s.train) for s in splits} == {10}
        assert splits[0].train[0] != splits[-1].train[0]

    def test_an_expanding_origin_grows_from_the_first_session(self):
        splits = walk_forward(
            CALENDAR, train_sessions=10, test_sessions=5, expanding=True
        )
        assert {s.train[0] for s in splits} == {CALENDAR[0]}
        assert [len(s.train) for s in splits] == [10, 15, 20, 25, 30]

    def test_every_window_keeps_its_gap(self):
        splits = walk_forward(CALENDAR, train_sessions=10, test_sessions=5, gap_sessions=2)
        for split in splits:
            assert split.gap_sessions == 2
            dropped = [
                day for day in CALENDAR if split.train[-1] < day < split.test[0]
            ]
            assert len(dropped) == 2

    def test_a_calendar_too_short_for_one_window_is_an_error_not_an_empty_run(self):
        # Silently returning no windows would let a study report "no windows"
        # as though it were a null result.
        with pytest.raises(ResearchError) as exc:
            walk_forward(CALENDAR[:12], train_sessions=10, test_sessions=5)
        assert "cannot carry" in str(exc.value)

    def test_refuses_a_zero_gap(self):
        with pytest.raises(LeakageError):
            walk_forward(CALENDAR, train_sessions=10, test_sessions=5, gap_sessions=0)

    @pytest.mark.parametrize("train, test", [(0, 5), (10, 0), (-1, 5)])
    def test_refuses_an_empty_block(self, train, test):
        with pytest.raises(ResearchError):
            window_count(40, train, test)

    def test_refuses_a_non_advancing_step(self):
        with pytest.raises(ResearchError):
            window_count(40, 10, 5, 1, 0)

    def test_a_holiday_does_not_shrink_the_gap(self):
        # The gap counts sessions, not days. Dropping "one calendar day"
        # between a Friday and a Monday would drop nothing at all.
        friday = date(2026, 3, 6)
        calendar = [friday - timedelta(days=4 - i) for i in range(5)]
        calendar += [friday + timedelta(days=3 + i) for i in range(5)]
        splits = walk_forward(calendar, train_sessions=4, test_sessions=3)
        assert splits[0].train[-1] == calendar[3]
        assert splits[0].test[0] == calendar[5]
        assert (splits[0].test[0] - splits[0].train[-1]).days == 4
