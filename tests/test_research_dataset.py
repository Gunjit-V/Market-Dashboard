"""The door between Phase 2's datasets and the harness.

Brief S4.7: features come from the built datasets, never from a fresh query
against ``ohlcv_*``. These tests build a frame in the shape
:mod:`features.store` writes -- value beside ``__status`` -- and check that the
adapter reads it the way Phase 2 meant it to be read: only ``valid`` rows, no
imputation, and a count of everything dropped.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from features.registry import label_set, state_features
from features.state import FeatureStatus
from features.store import paths_for, write_dataset
from features.dataset import BuildStats
from research.dataset import (
    label_horizon_minutes,
    label_kind,
    load_panel,
    panel_from_frame,
)
from research.errors import LeakageError, ResearchError
from research.panel import CLASSIFICATION, REGRESSION

pd = pytest.importorskip("pandas")

FEATURES = ("rv_short", "rv_baseline")
LABEL = "fwd_rv_60m"
VALID = FeatureStatus.VALID.value


def frame(rows):
    """A dataset-shaped frame from ``(offset, feature values, label, statuses)``."""
    base = datetime(2026, 3, 2, 9, 20)
    records = []
    for index, (features, label, statuses) in enumerate(rows):
        record = {
            "instrument": "Nifty 50",
            "decision_time": base + timedelta(minutes=5 * index),
            "timeframe": "5m",
            "feature_set_version": "fs_5m_deadbeef",
            LABEL: label,
            f"{LABEL}__status": statuses.get(LABEL, VALID),
        }
        for name, value in zip(FEATURES, features):
            record[name] = value
            record[f"{name}__status"] = statuses.get(name, VALID)
        records.append(record)
    return pd.DataFrame.from_records(records)


class TestLabelMetadata:
    def test_reads_the_kind_from_the_registry(self):
        assert label_kind("fwd_rv_60m", "5m") == REGRESSION
        assert label_kind("fwd_ret_30m", "5m") == REGRESSION
        assert label_kind("fwd_dir_30m", "5m") == CLASSIFICATION

    @pytest.mark.parametrize(
        "label, timeframe, minutes",
        [
            ("fwd_rv_15m", "5m", 15),
            ("fwd_rv_60m", "5m", 60),
            ("fwd_dir_30m", "5m", 30),
            ("fwd_ret_5m", "1m", 5),
            ("fwd_ret_30m", "1m", 30),
        ],
    )
    def test_reads_the_horizon_from_the_spec_not_the_name(
        self, label, timeframe, minutes
    ):
        assert label_horizon_minutes(label, timeframe) == minutes

    def test_an_unregistered_label_is_refused(self):
        with pytest.raises(ResearchError) as exc:
            label_kind("fwd_rv_90m", "5m")
        assert "fwd_rv_60m" in str(exc.value)

    def test_a_1m_label_is_not_a_5m_label(self):
        # fwd_ret_5m exists on the 1-minute set and not on the 5-minute one.
        assert label_horizon_minutes("fwd_ret_5m", "1m") == 5
        with pytest.raises(ResearchError):
            label_horizon_minutes("fwd_ret_5m", "5m")


class TestPanelFromFrame:
    def test_keeps_only_rows_where_everything_is_valid(self):
        panel, usability = panel_from_frame(
            frame(
                [
                    ((0.10, 0.12), 0.11, {}),
                    ((None, 0.12), 0.11, {"rv_short": "insufficient_history"}),
                    ((0.13, 0.12), None, {LABEL: "missing"}),
                    ((0.14, 0.15), 0.16, {}),
                ]
            ),
            LABEL,
            FEATURES,
        )
        assert len(panel) == 2
        assert panel.y == (0.11, 0.16)
        assert usability.rows_in == 4
        assert usability.rows_out == 2
        assert usability.dropped == 2

    def test_records_why_each_row_went(self):
        _, usability = panel_from_frame(
            frame(
                [
                    ((None, 0.12), 0.11, {"rv_short": "insufficient_history"}),
                    ((None, 0.12), 0.11, {"rv_short": "insufficient_history"}),
                    ((0.13, 0.12), None, {LABEL: "missing"}),
                    ((0.14, 0.15), 0.16, {}),
                ]
            ),
            LABEL,
            FEATURES,
        )
        assert usability.reasons["rv_short"] == {"insufficient_history": 2}
        assert usability.reasons[LABEL] == {"missing": 1}
        assert "rv_short" in usability.render()
        assert usability.describe()["rows_out"] == 1

    def test_nothing_is_imputed(self):
        # A dropped row is dropped. The Phase 1 rule -- nothing is repaired --
        # does not stop at the dataset boundary.
        panel, _ = panel_from_frame(
            frame(
                [
                    ((0.10, 0.12), 0.11, {}),
                    ((None, 0.12), 0.11, {"rv_short": "missing"}),
                    ((0.14, 0.15), 0.16, {}),
                ]
            ),
            LABEL,
            FEATURES,
        )
        assert panel.column("rv_short") == (0.10, 0.14)
        assert len(panel) == 2

    def test_rows_come_back_in_decision_time_order(self):
        built = frame([((0.10 + i / 100, 0.12), 0.11, {}) for i in range(5)])
        shuffled = built.iloc[[3, 0, 4, 1, 2]].reset_index(drop=True)
        panel, _ = panel_from_frame(shuffled, LABEL, FEATURES)
        assert list(panel.decision_times) == sorted(panel.decision_times)

    def test_carries_the_label_kind_and_horizon(self):
        panel, _ = panel_from_frame(frame([((0.10, 0.12), 0.11, {})] * 2), LABEL, FEATURES)
        assert panel.kind == REGRESSION
        assert panel.horizon_minutes == 60
        assert panel.instrument == "Nifty 50"
        assert panel.timeframe == "5m"

    def test_defaults_to_every_registered_feature(self):
        names = state_features("5m").names
        built = frame([((0.10, 0.12), 0.11, {})] * 2)
        for name in names:
            if name not in built.columns:
                built[name] = 0.5
                built[f"{name}__status"] = VALID
        panel, _ = panel_from_frame(built, LABEL)
        assert panel.feature_names == names

    def test_decision_times_come_back_as_datetimes(self):
        panel, _ = panel_from_frame(frame([((0.10, 0.12), 0.11, {})] * 2), LABEL, FEATURES)
        assert all(isinstance(t, datetime) for t in panel.decision_times)


class TestRefusals:
    def test_a_label_cannot_be_used_as_a_feature(self):
        # fwd_dir_30m is the sign of fwd_ret_30m; the pair would look like a
        # spectacular model.
        with pytest.raises(LeakageError) as exc:
            panel_from_frame(
                frame([((0.10, 0.12), 0.11, {})] * 2),
                "fwd_ret_30m",
                ("fwd_dir_30m",),
            )
        assert "is a label" in str(exc.value)

    def test_the_label_cannot_also_be_a_feature(self):
        with pytest.raises(LeakageError):
            panel_from_frame(frame([((0.10, 0.12), 0.11, {})] * 2), LABEL, (LABEL,))

    def test_an_unregistered_feature_is_refused(self):
        with pytest.raises(ResearchError) as exc:
            panel_from_frame(
                frame([((0.10, 0.12), 0.11, {})] * 2), LABEL, ("my_alpha_signal",)
            )
        assert "registered" in str(exc.value)

    def test_a_missing_column_names_what_is_absent(self):
        built = frame([((0.10, 0.12), 0.11, {})] * 2).drop(columns=["rv_baseline"])
        with pytest.raises(ResearchError) as exc:
            panel_from_frame(built, LABEL, FEATURES)
        assert "rv_baseline" in str(exc.value)

    def test_two_instruments_in_one_frame_are_refused(self):
        built = frame([((0.10, 0.12), 0.11, {})] * 2)
        built.loc[1, "instrument"] = "Nifty Bank"
        with pytest.raises(ResearchError) as exc:
            panel_from_frame(built, LABEL, FEATURES)
        assert "found 2" in str(exc.value)

    def test_two_timeframes_in_one_frame_are_refused(self):
        built = frame([((0.10, 0.12), 0.11, {})] * 2)
        built.loc[1, "timeframe"] = "1m"
        with pytest.raises(ResearchError):
            panel_from_frame(built, LABEL, FEATURES)


class TestLoadPanel:
    def test_reads_a_written_dataset_back_into_a_panel(self, tmp_path):
        # End to end through the Phase 2 store: write with features.store,
        # read with research.dataset. No database is involved either way.
        fs = state_features("5m")
        labels = label_set("5m")
        base = datetime(2026, 3, 2, 9, 20)
        rows = []
        for index in range(8):
            row = {
                "instrument": "Nifty 50",
                "decision_time": base + timedelta(minutes=5 * index),
                "timeframe": "5m",
                "feature_set_version": "fs_5m_deadbeef",
            }
            for spec in fs:
                row[spec.name] = 0.1 + index / 100
                row[f"{spec.name}__status"] = VALID
            for spec in labels:
                usable = index % 4 != 3
                row[spec.name] = 0.2 + index / 100 if usable else None
                row[f"{spec.name}__status"] = VALID if usable else "missing"
            rows.append(row)

        write_dataset(
            rows,
            "Nifty 50",
            fs,
            labels,
            BuildStats(),
            date(2026, 3, 2),
            date(2026, 3, 2),
            root=tmp_path,
        )
        assert paths_for("Nifty 50", fs, tmp_path).exists

        panel, usability = load_panel(
            "Nifty 50", "5m", LABEL, features=FEATURES, root=tmp_path
        )
        assert usability.rows_in == 8
        assert len(panel) == 6
        assert panel.label == LABEL
        assert panel.feature_names == FEATURES
        assert usability.reasons[LABEL] == {"missing": 2}

    def test_a_missing_dataset_says_how_to_build_one(self, tmp_path):
        with pytest.raises(FileNotFoundError) as exc:
            load_panel("Nifty 50", "5m", LABEL, root=tmp_path)
        assert "features.build" in str(exc.value)
