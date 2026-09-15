"""The panel's invariants, and the label it hides.

A panel is the harness's only input, so everything downstream assumes its
guarantees: ascending decision times, finite values, one row per decision, and
a masked copy that cannot be talked out of the answers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from research.errors import LeakageError, ResearchError
from research.panel import CLASSIFICATION, REGRESSION, Panel
from tests.featurelib import trading_days
from tests.researchlib import CARRIER, panel_of, volatility_panel

DAYS = trading_days(date(2026, 3, 2), 6)
TIMES = [datetime(2026, 3, 2, 9, 20) + timedelta(minutes=5 * i) for i in range(4)]


def build(**overrides) -> Panel:
    fields = {
        "instrument": "Nifty 50",
        "timeframe": "5m",
        "label": "fwd_rv_60m",
        "kind": REGRESSION,
        "horizon_minutes": 60,
        "feature_names": (CARRIER,),
        "decision_times": tuple(TIMES),
        "rows": tuple((0.1 + i / 100,) for i in range(4)),
        "targets": (0.1, 0.2, 0.3, 0.4),
    }
    fields.update(overrides)
    return Panel(**fields)


class TestInvariants:
    def test_a_well_formed_panel_reports_its_shape(self):
        panel = build()
        assert len(panel) == 4
        assert panel.sessions == (date(2026, 3, 2),)
        assert panel.first_decision == TIMES[0]
        assert panel.last_decision == TIMES[-1]

    def test_decision_times_must_ascend_strictly(self):
        with pytest.raises(ResearchError) as exc:
            build(decision_times=tuple(reversed(TIMES)))
        assert "ascend" in str(exc.value)

    def test_a_repeated_decision_time_is_refused(self):
        with pytest.raises(ResearchError):
            build(decision_times=(TIMES[0], TIMES[0], TIMES[2], TIMES[3]))

    def test_rows_and_decision_times_must_line_up(self):
        with pytest.raises(ResearchError):
            build(rows=((0.1,), (0.2,)))

    def test_targets_and_rows_must_line_up(self):
        with pytest.raises(ResearchError):
            build(targets=(0.1, 0.2))

    def test_every_row_has_the_same_width(self):
        with pytest.raises(ResearchError):
            build(rows=((0.1,), (0.2, 0.3), (0.3,), (0.4,)))

    def test_a_nan_feature_is_refused(self):
        # The Phase 2 status model exists so absence never becomes a
        # plausible-looking number; a panel is the last place it could slip in.
        with pytest.raises(ResearchError) as exc:
            build(rows=((0.1,), (float("nan"),), (0.3,), (0.4,)))
        assert "finite" in str(exc.value)

    def test_an_infinite_target_is_refused(self):
        with pytest.raises(ResearchError):
            build(targets=(0.1, float("inf"), 0.3, 0.4))

    def test_a_duplicate_feature_name_is_refused(self):
        with pytest.raises(ResearchError):
            build(feature_names=(CARRIER, CARRIER), rows=((0.1, 0.2),) * 4)

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ResearchError):
            build(kind="ranking")

    def test_a_horizon_must_be_positive(self):
        with pytest.raises(ResearchError):
            build(horizon_minutes=0)


class TestMasking:
    def test_a_masked_panel_keeps_its_rows(self):
        panel = build()
        masked = panel.masked()
        assert len(masked) == len(panel)
        assert masked.rows == panel.rows
        assert masked.decision_times == panel.decision_times

    def test_a_masked_panel_refuses_to_hand_over_the_answers(self):
        with pytest.raises(LeakageError) as exc:
            build().masked().y
        assert "hidden" in str(exc.value)

    def test_masking_does_not_change_the_original(self):
        panel = build()
        panel.masked()
        assert panel.y == (0.1, 0.2, 0.3, 0.4)
        assert not panel.is_masked


class TestSelection:
    def test_selecting_rows_keeps_order_and_alignment(self):
        panel = build().select([2, 0])
        assert panel.decision_times == (TIMES[0], TIMES[2])
        assert panel.y == (0.1, 0.3)

    def test_selecting_from_a_masked_panel_stays_masked(self):
        assert build().masked().select([0, 1]).is_masked

    def test_rows_are_picked_by_session(self):
        panel = volatility_panel(DAYS, per_session=3)
        subset = panel.on_sessions(DAYS[:2])
        assert subset.sessions == tuple(DAYS[:2])
        assert len(subset) == 6

    def test_a_session_with_no_rows_contributes_none(self):
        panel = volatility_panel(DAYS, per_session=3)
        assert len(panel.on_sessions([date(2020, 1, 1)])) == 0

    def test_a_column_is_read_by_name(self):
        panel = panel_of(TIMES, [1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0])
        assert panel.column(CARRIER) == (1.0, 2.0, 3.0, 4.0)
        assert panel.has_column(CARRIER)
        assert not panel.has_column("rv_baseline")

    def test_an_unknown_column_names_what_is_there(self):
        with pytest.raises(ResearchError) as exc:
            build().column("rv_baseline")
        assert CARRIER in str(exc.value)


class TestDescription:
    def test_describes_itself_for_a_report_header(self):
        described = volatility_panel(DAYS, per_session=4).describe()
        assert described["instrument"] == "Nifty 50"
        assert described["label"] == "fwd_rv_60m"
        assert described["kind"] == REGRESSION
        assert described["rows"] == 24
        assert described["sessions"] == 6
        assert described["horizon_minutes"] == 60

    def test_a_classification_panel_says_so(self):
        panel = panel_of(
            TIMES, [1.0, 2.0, 3.0, 4.0], [1.0, -1.0, -1.0, 1.0],
            kind=CLASSIFICATION, label="fwd_dir_30m", horizon_minutes=30,
        )
        assert panel.describe()["kind"] == CLASSIFICATION
