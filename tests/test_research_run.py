"""The CLI, end to end on a dataset written to a temporary directory.

Brief S5 acceptance 1 and 5 in one place: given a labelled dataset, the command
produces baseline scores with confidence intervals, needing no database and no
network to do it.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from features.dataset import BuildStats
from features.registry import label_set, state_features
from features.state import FeatureStatus
from features.store import write_dataset
from research.run import main
from tests.featurelib import trading_days

pytest.importorskip("pandas")

VALID = FeatureStatus.VALID.value
DAYS = trading_days(date(2026, 3, 2), 8)


@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory):
    """A small, entirely valid 5-minute dataset for "Nifty 50"."""
    root = tmp_path_factory.mktemp("features")
    fs = state_features("5m")
    labels = label_set("5m")
    rows = []
    for day_index, day in enumerate(DAYS):
        base = datetime.combine(day, time(9, 20))
        for step in range(10):
            level = 0.10 + (day_index + step) % 7 / 100
            row = {
                "instrument": "Nifty 50",
                "decision_time": base + timedelta(minutes=5 * step),
                "timeframe": "5m",
                "feature_set_version": "fs_5m_deadbeef",
            }
            for spec in fs:
                row[spec.name] = level
                row[f"{spec.name}__status"] = VALID
            for spec in labels:
                if spec.params["kind"] == "dir":
                    row[spec.name] = 1.0 if step % 3 else -1.0
                else:
                    row[spec.name] = 0.9 * level
                row[f"{spec.name}__status"] = VALID
            rows.append(row)
    write_dataset(
        rows, "Nifty 50", fs, labels, BuildStats(), DAYS[0], DAYS[-1], root=root
    )
    return root


class TestRun:
    def test_reports_the_baselines_on_a_volatility_label(self, dataset_root, capsys):
        code = main(
            [
                "--label", "fwd_rv_60m",
                "--train", "3",
                "--test", "2",
                "--out", str(dataset_root),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "fwd_rv_60m" in out
        assert "baseline" in out
        assert "persistence" in out
        assert "train-mean" in out
        assert "consistency" in out
        assert "hypotheses: 1 tested" in out
        assert out.index("baseline") < out.index("model")

    def test_reports_a_direction_label_against_the_best_naive_answer(
        self, dataset_root, capsys
    ):
        code = main(
            [
                "--label", "fwd_dir_30m",
                "--train", "3",
                "--test", "2",
                "--out", str(dataset_root),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "best-naive" in out
        assert "always-up" in out
        # 2 of every 3 rows are up, so the convenient guess and the best naive
        # answer agree here -- what matters is that both are reported.
        assert "verdict" in out

    def test_an_unknown_label_is_a_usage_error(self, dataset_root, capsys):
        code = main(["--label", "fwd_rv_90m", "--out", str(dataset_root)])
        assert code == 3
        assert "not a registered" in capsys.readouterr().err

    def test_a_missing_dataset_is_a_usage_error(self, tmp_path, capsys):
        code = main(["--out", str(tmp_path)])
        assert code == 3
        assert "features.build" in capsys.readouterr().err

    def test_a_calendar_too_short_for_a_window_is_a_usage_error(
        self, dataset_root, capsys
    ):
        code = main(
            ["--train", "50", "--test", "5", "--out", str(dataset_root)]
        )
        assert code == 3
        assert "cannot carry" in capsys.readouterr().err

    def test_an_unknown_carrier_is_a_usage_error(self, dataset_root, capsys):
        code = main(
            [
                "--carrier", "rv_short_typo",
                "--train", "3",
                "--test", "2",
                "--out", str(dataset_root),
            ]
        )
        assert code == 3
        assert "rv_short_typo" in capsys.readouterr().err

    def test_declaring_several_hypotheses_adjusts_the_threshold(
        self, dataset_root, capsys
    ):
        main(
            [
                "--label", "fwd_rv_60m",
                "--train", "3",
                "--test", "2",
                "--hypotheses", "6",
                "--out", str(dataset_root),
            ]
        )
        out = capsys.readouterr().out
        assert "hypotheses: 6 tested" in out
        assert "Bonferroni threshold 0.0083" in out
