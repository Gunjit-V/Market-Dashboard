"""Dataset generation: decision times, the fast path, and the Parquet store.

The load-bearing test here is
:meth:`TestFastPathEquivalence.test_matches_the_one_at_a_time_path`. The
builder reads once per session and slides the decision time through bars held
in memory, which is ~75x fewer database round trips -- and is only safe if it
produces exactly what the one-at-a-time path produces. If it ever drifts, a
model trained on a dataset would be served by different arithmetic than it
learned from.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

import pytest

from features.dataset import (
    BuildStats,
    build_session_rows,
    session_decision_times,
)
from features.engine import build_state_from_bars, compute_labels, state_row
from features.registry import feature_set_version, label_set, state_features
from features.state import FeatureStatus, FeatureValue
from features.store import (
    append_dataset,
    build_manifest,
    column_order,
    dataset_name,
    dataset_sessions,
    list_datasets,
    paths_for,
    read_dataset,
    resume_from,
    slug,
    stats_from_frame,
    write_dataset,
)
from tests.featurelib import sessions, trading_days

DAYS = trading_days(date(2026, 3, 2), 8)


@pytest.fixture(scope="module")
def history():
    return sessions(DAYS, timeframe="5m")


@pytest.fixture(scope="module")
def fs():
    return state_features("5m")


@pytest.fixture(scope="module")
def labels():
    return label_set("5m")


class TestDecisionTimes:
    def test_one_per_bar_slot_on_five_minutes(self):
        assert len(session_decision_times(DAYS[0], "5m")) == 75

    def test_one_per_bar_slot_on_one_minute(self):
        assert len(session_decision_times(DAYS[0], "1m")) == 375

    def test_starts_at_the_session_open(self):
        # 09:15 is a real decision point: nothing has closed today, but the
        # rolling volatility features already carry yesterday's history.
        assert session_decision_times(DAYS[0], "5m")[0].time() == time(9, 15)

    def test_last_is_before_the_close(self):
        assert session_decision_times(DAYS[0], "5m")[-1].time() == time(15, 25)

    def test_never_reaches_the_close(self):
        assert all(t.time() < time(15, 30) for t in session_decision_times(DAYS[0], "5m"))

    def test_all_on_the_grid(self):
        anchor = datetime.combine(DAYS[0], time(9, 15))
        for moment in session_decision_times(DAYS[0], "5m"):
            assert (moment - anchor).total_seconds() % 300 == 0


class TestFastPathEquivalence:
    """The optimisation must change nothing."""

    def visible_at(self, bars, moment, width=5):
        return [b for b in bars if b.timestamp + timedelta(minutes=width) <= moment]

    def test_matches_the_one_at_a_time_path(self, history, fs, labels):
        day = DAYS[6]
        rows = list(build_session_rows(day, history, "Nifty 50", fs, labels))
        assert len(rows) == 75

        today = [b for b in history if b.timestamp.date() == day]
        for row in rows:
            moment = row["decision_time"]
            expected_state = build_state_from_bars(
                "Nifty 50", moment, self.visible_at(history, moment), fs
            )
            knowable = self.visible_at(today, moment)
            if knowable:
                decision_bar = knowable[-1]
                forward = [b for b in today if b.timestamp > decision_bar.timestamp]
                label_values = compute_labels(decision_bar, forward, labels)
            else:
                # Nothing has closed today, so there is no bar to measure
                # forward from -- every label is absent, but present as a column.
                label_values = tuple(
                    FeatureValue.absent(
                        spec.name,
                        FeatureStatus.INSUFFICIENT_HISTORY,
                        "no bar has closed yet in this session",
                    )
                    for spec in labels
                )
            assert row == state_row(expected_state, label_values)

    def test_equivalence_holds_at_the_session_open(self, history, fs, labels):
        day = DAYS[6]
        row = list(build_session_rows(day, history, "Nifty 50", fs, labels))[0]
        expected = build_state_from_bars(
            "Nifty 50", row["decision_time"], self.visible_at(history, row["decision_time"]), fs
        )
        assert row["rv_short"] == expected.value_of("rv_short")
        assert row["ret_5m"] is None  # nothing has closed today yet

    def test_rows_are_not_vacuous(self, history, fs, labels):
        """A dataset of all-absent values would satisfy equivalence trivially."""
        rows = list(build_session_rows(DAYS[6], history, "Nifty 50", fs, labels))
        late = rows[-20]
        valid = sum(1 for spec in fs if late[spec.name] is not None)
        assert valid == len(fs)


class TestSessionRows:
    def test_one_row_per_decision_time(self, history, fs, labels):
        rows = list(build_session_rows(DAYS[5], history, "Nifty 50", fs, labels))
        assert len(rows) == len(session_decision_times(DAYS[5], "5m"))

    def test_rows_are_ordered_by_decision_time(self, history, fs, labels):
        rows = list(build_session_rows(DAYS[5], history, "Nifty 50", fs, labels))
        times = [r["decision_time"] for r in rows]
        assert times == sorted(times)

    def test_every_feature_has_a_status_column(self, history, fs, labels):
        row = list(build_session_rows(DAYS[5], history, "Nifty 50", fs, labels))[-1]
        for spec in fs:
            assert f"{spec.name}__status" in row
        for spec in labels:
            assert f"{spec.name}__status" in row

    def test_labels_absent_at_the_session_open(self, history, fs, labels):
        """No bar has closed, so there is no decision bar to label.

        The row must still carry every label column: a ragged row would give
        consumers a KeyError and leave the absence unexplained.
        """
        row = list(build_session_rows(DAYS[5], history, "Nifty 50", fs, labels))[0]
        for spec in labels:
            assert row[spec.name] is None
            assert row[f"{spec.name}__status"] == "insufficient_history"

    def test_every_row_has_the_same_keys(self, history, fs, labels):
        rows = list(build_session_rows(DAYS[5], history, "Nifty 50", fs, labels))
        expected = set(column_order(fs, labels))
        assert all(set(r) == expected for r in rows)

    def test_labels_absent_at_the_end_of_the_session(self, history, fs, labels):
        rows = list(build_session_rows(DAYS[5], history, "Nifty 50", fs, labels))
        assert rows[-1]["fwd_ret_60m"] is None
        assert rows[-1]["fwd_ret_60m__status"] == "missing"

    def test_stats_are_accumulated(self, history, fs, labels):
        stats = BuildStats()
        list(build_session_rows(DAYS[5], history, "Nifty 50", fs, labels, stats))
        assert stats.rows == 75
        assert stats.status_counts["rv_short:valid"] > 0
        assert stats.first_decision.time() == time(9, 15)
        assert stats.last_decision.time() == time(15, 25)

    def test_out_of_session_bars_never_become_rows(self, history, fs, labels):
        from tests.featurelib import make_bar

        intruder = make_bar(datetime.combine(DAYS[5], time(9, 10)), 30_000.0)
        rows = list(
            build_session_rows(DAYS[5], list(history) + [intruder], "Nifty 50", fs, labels)
        )
        assert all(r["decision_time"].time() >= time(9, 15) for r in rows)


class TestColumnOrder:
    def test_identity_columns_come_first(self, fs, labels):
        assert column_order(fs, labels)[:4] == [
            "instrument",
            "decision_time",
            "timeframe",
            "feature_set_version",
        ]

    def test_each_value_is_followed_by_its_status(self, fs, labels):
        columns = column_order(fs, labels)
        for spec in fs:
            index = columns.index(spec.name)
            assert columns[index + 1] == f"{spec.name}__status"

    def test_labels_come_after_features(self, fs, labels):
        columns = column_order(fs, labels)
        assert columns.index("fwd_ret_15m") > columns.index("rv_regime")

    def test_column_count(self, fs, labels):
        assert len(column_order(fs, labels)) == 4 + 2 * len(fs) + 2 * len(labels)

    def test_order_is_deterministic(self, fs, labels):
        assert column_order(fs, labels) == column_order(fs, labels)


class TestNaming:
    def test_slug_is_filename_safe(self):
        assert slug("Nifty 50") == "nifty_50"
        assert slug("Nifty Bank") == "nifty_bank"

    def test_dataset_name_carries_timeframe_and_version(self, fs):
        name = dataset_name("Nifty 50", fs)
        assert name.startswith("nifty_50_5m_fs_5m_")
        assert feature_set_version(fs) in name

    def test_paths_share_a_stem(self, fs, tmp_path):
        target = paths_for("Nifty 50", fs, tmp_path)
        assert target.parquet.stem == target.manifest.stem
        assert target.parquet.suffix == ".parquet"
        assert target.manifest.suffix == ".json"

    def test_version_in_the_filename_separates_definitions(self, fs, tmp_path):
        """Rebuilding with different definitions must not overwrite."""
        from features.spec import FeatureSet

        altered = FeatureSet(
            "5m",
            tuple(
                s if s.name != "rv_baseline" else type(s)(
                    **{**s.identity(), "trailing_sessions": 14, "fn": s.fn,
                       "description": s.description}
                )
                for s in fs
            ),
        )
        assert paths_for("Nifty 50", fs, tmp_path).parquet != (
            paths_for("Nifty 50", altered, tmp_path).parquet
        )


class TestManifest:
    def stats(self, history, fs, labels):
        stats = BuildStats()
        list(build_session_rows(DAYS[5], history, "Nifty 50", fs, labels, stats))
        return stats

    def test_records_identity_and_version(self, history, fs, labels):
        manifest = build_manifest(
            "Nifty 50", fs, labels, self.stats(history, fs, labels), DAYS[0], DAYS[7]
        )
        assert manifest["instrument"] == "Nifty 50"
        assert manifest["timeframe"] == "5m"
        assert manifest["features"]["feature_set_version"] == feature_set_version(fs)

    def test_records_the_extraction_time(self, history, fs, labels):
        """No as-of versioning exists upstream, so the pull time is the only
        record of what a dataset saw."""
        manifest = build_manifest(
            "Nifty 50", fs, labels, self.stats(history, fs, labels), DAYS[0], DAYS[7]
        )
        assert manifest["extracted_at_utc"]
        assert manifest["extracted_at_ist"]

    def test_records_row_and_status_counts(self, history, fs, labels):
        manifest = build_manifest(
            "Nifty 50", fs, labels, self.stats(history, fs, labels), DAYS[0], DAYS[7]
        )
        assert manifest["rows"] == 75
        assert any(k.endswith(":valid") for k in manifest["feature_status_counts"])

    def test_describes_every_feature_and_label(self, history, fs, labels):
        manifest = build_manifest(
            "Nifty 50", fs, labels, self.stats(history, fs, labels), DAYS[0], DAYS[7]
        )
        assert len(manifest["features"]["features"]) == len(fs)
        assert len(manifest["labels"]["labels"]) == len(labels)

    def test_is_json_serialisable(self, history, fs, labels):
        manifest = build_manifest(
            "Nifty 50", fs, labels, self.stats(history, fs, labels), DAYS[0], DAYS[7]
        )
        assert json.loads(json.dumps(manifest))["rows"] == 75

    def test_clean_sources_report_pass(self, history, fs, labels):
        manifest = build_manifest(
            "Nifty 50", fs, labels, self.stats(history, fs, labels), DAYS[0], DAYS[7]
        )
        assert manifest["source_verdict"] == "PASS"


class TestStoreRoundTrip:
    def test_write_then_read(self, history, fs, labels, tmp_path):
        pytest.importorskip("pandas")
        from features.store import read_dataset, read_manifest, write_dataset

        stats = BuildStats()
        rows = list(build_session_rows(DAYS[5], history, "Nifty 50", fs, labels, stats))
        write_dataset(rows, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[7], tmp_path)

        frame = read_dataset("Nifty 50", fs, tmp_path)
        assert len(frame) == 75
        assert list(frame.columns) == column_order(fs, labels)
        assert read_manifest("Nifty 50", fs, tmp_path)["rows"] == 75

    def test_absent_dataset_raises_with_a_useful_message(self, fs, tmp_path):
        pytest.importorskip("pandas")
        from features.store import read_dataset

        with pytest.raises(FileNotFoundError, match="features.build"):
            read_dataset("Nifty 50", fs, tmp_path)

    def test_listing_is_empty_for_a_missing_directory(self, tmp_path):
        assert list_datasets(tmp_path / "nope") == []

    def test_listing_reports_written_datasets(self, history, fs, labels, tmp_path):
        pytest.importorskip("pandas")
        from features.store import write_dataset

        stats = BuildStats()
        rows = list(build_session_rows(DAYS[5], history, "Nifty 50", fs, labels, stats))
        write_dataset(rows, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[7], tmp_path)
        listed = list_datasets(tmp_path)
        assert len(listed) == 1
        assert listed[0]["instrument"] == "Nifty 50"
        assert listed[0]["rows"] == 75


class TestCli:
    def test_parser_defaults(self):
        from features.build import build_parser

        args = build_parser().parse_args([])
        assert args.instrument == "Nifty 50"
        assert args.timeframe == "5m"

    def test_rejects_a_bad_date(self):
        from features.build import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["--from", "03-09-2026"])

    def test_rejects_an_unknown_timeframe(self):
        from features.build import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["--timeframe", "15m"])

    def test_list_runs_without_a_database(self, tmp_path, capsys):
        from features.build import main

        assert main(["--list", "--out", str(tmp_path)]) == 0
        assert "No datasets" in capsys.readouterr().out


class TestIncrementalBuild:
    """An incremental build must be indistinguishable from a full one.

    This is the same guarantee the fast path carries, for the same reason: if
    appending ever diverged from rebuilding, a dataset grown day by day would
    quietly stop matching one built in a single pass, and nothing would say so.
    """

    def build(self, days, history, fs, labels):
        """Rows and stats for a list of sessions, as the CLI would produce."""
        stats = BuildStats()
        rows = []
        for day in days:
            rows.extend(build_session_rows(day, history, "Nifty 50", fs, labels, stats))
        return rows, stats

    def test_append_equals_full_rebuild(self, history, fs, labels, tmp_path):
        pytest.importorskip("pandas")
        import pandas as pd

        full_dir, inc_dir = tmp_path / "full", tmp_path / "inc"

        # One pass over every session.
        rows, stats = self.build(DAYS, history, fs, labels)
        write_dataset(rows, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[-1], full_dir)

        # Built in two goes: the first six sessions, then the rest appended.
        rows_a, stats_a = self.build(DAYS[:6], history, fs, labels)
        write_dataset(rows_a, "Nifty 50", fs, labels, stats_a, DAYS[0], DAYS[5], inc_dir)

        resume = resume_from("Nifty 50", fs, inc_dir)
        assert resume == DAYS[5]
        rows_b, stats_b = self.build(DAYS[5:], history, fs, labels)
        append_dataset(rows_b, "Nifty 50", fs, labels, stats_b, resume, DAYS[-1], inc_dir)

        a = read_dataset("Nifty 50", fs, full_dir)
        b = read_dataset("Nifty 50", fs, inc_dir)
        pd.testing.assert_frame_equal(a, b)

    def test_append_is_idempotent(self, history, fs, labels, tmp_path):
        pytest.importorskip("pandas")

        rows, stats = self.build(DAYS[:6], history, fs, labels)
        write_dataset(rows, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[5], tmp_path)

        for _ in range(2):
            resume = resume_from("Nifty 50", fs, tmp_path)
            more, more_stats = self.build(
                [d for d in DAYS if d >= resume], history, fs, labels
            )
            append_dataset(
                more, "Nifty 50", fs, labels, more_stats, resume, DAYS[-1], tmp_path
            )
            frame = read_dataset("Nifty 50", fs, tmp_path)
            assert len(frame) == len(DAYS) * 75
            assert not frame.decision_time.duplicated().any()

    def test_resume_restarts_at_the_last_session_not_after_it(
        self, history, fs, labels, tmp_path
    ):
        """A dataset written mid-session must be completed, not skipped past."""
        pytest.importorskip("pandas")

        # Store a deliberately partial final session: only its first 20 rows.
        rows, stats = self.build(DAYS[:5], history, fs, labels)
        partial = [r for r in rows if r["decision_time"].date() < DAYS[4]]
        partial += [
            r for r in rows if r["decision_time"].date() == DAYS[4]
        ][:20]
        write_dataset(
            partial, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[4], tmp_path
        )
        assert len(read_dataset("Nifty 50", fs, tmp_path)) == 4 * 75 + 20

        resume = resume_from("Nifty 50", fs, tmp_path)
        assert resume == DAYS[4], "must restart AT the partial session"

        more, more_stats = self.build([DAYS[4]], history, fs, labels)
        append_dataset(
            more, "Nifty 50", fs, labels, more_stats, resume, DAYS[4], tmp_path
        )
        frame = read_dataset("Nifty 50", fs, tmp_path)
        assert len(frame) == 5 * 75, "the partial session should now be complete"
        assert not frame.decision_time.duplicated().any()

    def test_append_without_an_existing_dataset_is_a_full_build(
        self, history, fs, labels, tmp_path
    ):
        pytest.importorskip("pandas")

        assert resume_from("Nifty 50", fs, tmp_path) is None
        rows, stats = self.build(DAYS, history, fs, labels)
        append_dataset(
            rows, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[-1], tmp_path
        )
        assert len(read_dataset("Nifty 50", fs, tmp_path)) == len(DAYS) * 75

    def test_rows_stay_ordered_after_appending(self, history, fs, labels, tmp_path):
        pytest.importorskip("pandas")

        rows, stats = self.build(DAYS[:6], history, fs, labels)
        write_dataset(rows, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[5], tmp_path)
        resume = resume_from("Nifty 50", fs, tmp_path)
        more, more_stats = self.build(DAYS[5:], history, fs, labels)
        append_dataset(
            more, "Nifty 50", fs, labels, more_stats, resume, DAYS[-1], tmp_path
        )
        times = read_dataset("Nifty 50", fs, tmp_path).decision_time.tolist()
        assert times == sorted(times)

    def test_column_order_survives_an_append(self, history, fs, labels, tmp_path):
        pytest.importorskip("pandas")

        rows, stats = self.build(DAYS[:6], history, fs, labels)
        write_dataset(rows, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[5], tmp_path)
        resume = resume_from("Nifty 50", fs, tmp_path)
        more, more_stats = self.build(DAYS[5:], history, fs, labels)
        append_dataset(
            more, "Nifty 50", fs, labels, more_stats, resume, DAYS[-1], tmp_path
        )
        assert list(read_dataset("Nifty 50", fs, tmp_path).columns) == column_order(
            fs, labels
        )


class TestIncrementalManifest:
    def build(self, days, history, fs, labels):
        stats = BuildStats()
        rows = []
        for day in days:
            rows.extend(build_session_rows(day, history, "Nifty 50", fs, labels, stats))
        return rows, stats

    def test_counts_cover_the_whole_dataset_not_just_the_new_part(
        self, history, fs, labels, tmp_path
    ):
        pytest.importorskip("pandas")
        from features.store import read_manifest

        rows, stats = self.build(DAYS[:6], history, fs, labels)
        write_dataset(rows, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[5], tmp_path)
        resume = resume_from("Nifty 50", fs, tmp_path)
        more, more_stats = self.build(DAYS[5:], history, fs, labels)
        append_dataset(
            more, "Nifty 50", fs, labels, more_stats, resume, DAYS[-1], tmp_path
        )

        manifest = read_manifest("Nifty 50", fs, tmp_path)
        assert manifest["rows"] == len(DAYS) * 75
        assert manifest["sessions"] == len(DAYS)
        total = sum(
            n for k, n in manifest["feature_status_counts"].items()
            if k.startswith("rv_short:")
        )
        assert total == len(DAYS) * 75

    def test_manifest_records_what_was_rebuilt(self, history, fs, labels, tmp_path):
        pytest.importorskip("pandas")
        from features.store import read_manifest

        rows, stats = self.build(DAYS[:6], history, fs, labels)
        write_dataset(rows, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[5], tmp_path)
        resume = resume_from("Nifty 50", fs, tmp_path)
        more, more_stats = self.build(DAYS[5:], history, fs, labels)
        append_dataset(
            more, "Nifty 50", fs, labels, more_stats, resume, DAYS[-1], tmp_path
        )

        inc = read_manifest("Nifty 50", fs, tmp_path)["incremental"]
        assert inc["rebuilt_from"] == DAYS[5].isoformat()
        assert inc["sessions_built"] == 3
        assert inc["rows_built"] == 3 * 75

    def test_range_start_is_preserved_across_an_append(
        self, history, fs, labels, tmp_path
    ):
        """The dataset still begins where it began, not where the append did."""
        pytest.importorskip("pandas")
        from features.store import read_manifest

        rows, stats = self.build(DAYS[:6], history, fs, labels)
        write_dataset(rows, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[5], tmp_path)
        resume = resume_from("Nifty 50", fs, tmp_path)
        more, more_stats = self.build(DAYS[5:], history, fs, labels)
        append_dataset(
            more, "Nifty 50", fs, labels, more_stats, resume, DAYS[-1], tmp_path
        )
        assert read_manifest("Nifty 50", fs, tmp_path)["range"]["start"] == (
            DAYS[0].isoformat()
        )


class TestStoreHelpers:
    def test_dataset_sessions_lists_what_is_stored(self, history, fs, labels, tmp_path):
        pytest.importorskip("pandas")

        stats = BuildStats()
        rows = []
        for day in DAYS[:4]:
            rows.extend(build_session_rows(day, history, "Nifty 50", fs, labels, stats))
        write_dataset(rows, "Nifty 50", fs, labels, stats, DAYS[0], DAYS[3], tmp_path)
        assert dataset_sessions("Nifty 50", fs, tmp_path) == list(DAYS[:4])

    def test_dataset_sessions_empty_without_a_dataset(self, fs, tmp_path):
        assert dataset_sessions("Nifty 50", fs, tmp_path) == []

    def test_stats_from_frame_matches_a_live_build(self, history, fs, labels, tmp_path):
        pytest.importorskip("pandas")
        import pandas as pd

        stats = BuildStats()
        rows = list(build_session_rows(DAYS[5], history, "Nifty 50", fs, labels, stats))
        frame = pd.DataFrame(rows, columns=column_order(fs, labels))
        recomputed = stats_from_frame(frame, fs, labels)

        assert recomputed.rows == stats.rows
        assert recomputed.sessions == 1
        assert recomputed.status_counts == stats.status_counts
        assert recomputed.label_status_counts == stats.label_status_counts
