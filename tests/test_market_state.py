"""MarketState, FeatureValue and the status model.

The central requirement under test: absence is never silently rendered as a
plausible-looking number, and the *reason* for an absence survives all the way
into the serialised row.

Pure construction -- no database, no network, no market data.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from features.spec import FeatureSet, FeatureSpec
from features.state import (
    ABSENT_STATUSES,
    FeatureStatus,
    FeatureValue,
    MarketState,
    StateError,
    empty_state,
    insufficient_history,
    summarize_quality,
)

T = datetime(2026, 9, 3, 11, 30)


def spec(name: str = "ret_1", **overrides) -> FeatureSpec:
    kwargs = {
        "name": name,
        "family": "price",
        "timeframe": "5m",
        "lookback_bars": 2,
    }
    kwargs.update(overrides)
    return FeatureSpec(**kwargs)


def state(values, **overrides) -> MarketState:
    kwargs = {
        "instrument": "Nifty 50",
        "decision_time": T,
        "timeframe": "5m",
        "values": tuple(values),
    }
    kwargs.update(overrides)
    return MarketState(**kwargs)


class TestFeatureStatus:
    def test_valid_is_the_only_present_status(self):
        assert FeatureStatus.VALID not in ABSENT_STATUSES
        assert len(ABSENT_STATUSES) == len(FeatureStatus) - 1

    def test_statuses_serialise_as_readable_tokens(self):
        assert FeatureStatus.INSUFFICIENT_HISTORY.value == "insufficient_history"
        assert json.dumps(FeatureStatus.MISSING) == '"missing"'

    def test_the_five_briefed_statuses_exist(self):
        for name in (
            "VALID",
            "MISSING",
            "INSUFFICIENT_HISTORY",
            "STALE",
            "INVALID_SOURCE",
        ):
            assert hasattr(FeatureStatus, name)


class TestFeatureValue:
    def test_valid_value_carries_a_number(self):
        v = FeatureValue.valid("ret_1", 0.0012)
        assert v.value == 0.0012
        assert v.is_valid

    def test_valid_status_requires_a_value(self):
        with pytest.raises(StateError, match="VALID but value is None"):
            FeatureValue(name="ret_1", value=None, status=FeatureStatus.VALID)

    def test_absent_status_requires_no_value(self):
        # A number carrying an absent status would be believed by anything
        # reading the value column alone.
        with pytest.raises(StateError, match="must be None"):
            FeatureValue(name="ret_1", value=1.0, status=FeatureStatus.MISSING)

    def test_nan_is_rejected_as_valid(self):
        with pytest.raises(StateError, match="NaN"):
            FeatureValue(name="ret_1", value=float("nan"))

    @pytest.mark.parametrize("value", [float("inf"), float("-inf")])
    def test_infinity_is_rejected_as_valid(self, value):
        with pytest.raises(StateError, match="finite"):
            FeatureValue(name="ret_1", value=value)

    def test_absent_requires_a_reason(self):
        with pytest.raises(StateError, match="needs a reason"):
            FeatureValue.absent("ret_1", FeatureStatus.VALID)

    def test_absent_records_its_detail(self):
        v = FeatureValue.absent("ret_1", FeatureStatus.STALE, "last bar 09:20")
        assert v.status is FeatureStatus.STALE
        assert v.detail == "last bar 09:20"
        assert not v.is_valid

    def test_empty_name_rejected(self):
        with pytest.raises(StateError, match="name"):
            FeatureValue(name="", value=1.0)

    def test_zero_is_a_valid_value(self):
        # Cash indices structurally report zero volume; that is data, not
        # absence, and must survive as a number.
        assert FeatureValue.valid("volume_z", 0.0).is_valid

    def test_as_dict_round_trips_through_json(self):
        v = FeatureValue.absent("ret_1", FeatureStatus.MISSING, "no bar")
        assert json.loads(json.dumps(v.as_dict())) == {
            "name": "ret_1",
            "value": None,
            "status": "missing",
            "detail": "no bar",
        }


class TestMarketState:
    def test_preserves_value_order(self):
        s = state([FeatureValue.valid(n, 1.0) for n in ("c", "a", "b")])
        assert s.names == ("c", "a", "b")

    def test_duplicate_feature_names_rejected(self):
        with pytest.raises(StateError, match="duplicate"):
            state([FeatureValue.valid("a", 1.0), FeatureValue.valid("a", 2.0)])

    def test_empty_instrument_rejected(self):
        with pytest.raises(StateError, match="instrument"):
            state([], instrument="")

    def test_timezone_aware_decision_time_rejected(self):
        from datetime import timezone

        with pytest.raises(StateError, match="naive"):
            state([], decision_time=datetime(2026, 9, 3, 11, 30, tzinfo=timezone.utc))

    def test_lookup_by_name(self):
        s = state([FeatureValue.valid("ret_1", 0.5)])
        assert s.value_of("ret_1") == 0.5
        assert s.status_of("ret_1") is FeatureStatus.VALID

    def test_unknown_name_raises(self):
        with pytest.raises(KeyError):
            state([]).get("nope")

    def test_membership_and_length(self):
        s = state([FeatureValue.valid("a", 1.0), FeatureValue.valid("b", 2.0)])
        assert len(s) == 2
        assert "a" in s and "zzz" not in s

    def test_to_vector_uses_none_for_absent(self):
        s = state(
            [
                FeatureValue.valid("a", 1.5),
                FeatureValue.absent("b", FeatureStatus.INSUFFICIENT_HISTORY),
            ]
        )
        assert s.to_vector() == (1.5, None)

    def test_valid_values_excludes_absent(self):
        s = state(
            [
                FeatureValue.valid("a", 1.5),
                FeatureValue.absent("b", FeatureStatus.MISSING),
            ]
        )
        assert s.valid_values() == {"a": 1.5}

    def test_statuses_reports_every_feature(self):
        s = state(
            [
                FeatureValue.valid("a", 1.5),
                FeatureValue.absent("b", FeatureStatus.STALE),
            ]
        )
        assert s.statuses() == {
            "a": FeatureStatus.VALID,
            "b": FeatureStatus.STALE,
        }


class TestSerialisation:
    def test_as_dict_is_json_serialisable(self):
        s = state([FeatureValue.valid("a", 1.5)], feature_set_version="fs_5m_abc12345")
        loaded = json.loads(json.dumps(s.as_dict()))
        assert loaded["instrument"] == "Nifty 50"
        assert loaded["decision_time"] == "2026-09-03T11:30:00"
        assert loaded["feature_set_version"] == "fs_5m_abc12345"

    def test_equal_states_serialise_identically(self):
        # Determinism: same inputs, byte-identical serialisation.
        values = [FeatureValue.valid("a", 1.5), FeatureValue.valid("b", 2.5)]
        assert json.dumps(state(values).as_dict()) == json.dumps(
            state(values).as_dict()
        )

    def test_to_row_pairs_each_value_with_its_status(self):
        s = state(
            [
                FeatureValue.valid("a", 1.5),
                FeatureValue.absent("b", FeatureStatus.INSUFFICIENT_HISTORY),
            ]
        )
        row = s.to_row()
        assert row["a"] == 1.5 and row["a__status"] == "valid"
        assert row["b"] is None and row["b__status"] == "insufficient_history"

    def test_to_row_carries_identity_columns(self):
        row = state([], feature_set_version="fs_5m_abc12345").to_row()
        assert row["instrument"] == "Nifty 50"
        assert row["decision_time"] == T
        assert row["timeframe"] == "5m"
        assert row["feature_set_version"] == "fs_5m_abc12345"


class TestQuality:
    def test_counts_every_status_including_zeroes(self):
        q = summarize_quality([FeatureValue.valid("a", 1.0)])
        # A zero count says "considered and none found", which is different
        # from a status being absent from the report entirely.
        assert q.status_counts["stale"] == 0
        assert q.status_counts["valid"] == 1

    def test_completeness(self):
        q = summarize_quality(
            [
                FeatureValue.valid("a", 1.0),
                FeatureValue.valid("b", 2.0),
                FeatureValue.absent("c", FeatureStatus.MISSING),
                FeatureValue.absent("d", FeatureStatus.STALE),
            ]
        )
        assert q.total == 4 and q.valid == 2 and q.absent == 2
        assert q.completeness == 0.5

    def test_empty_state_completeness_is_zero_not_one(self):
        assert summarize_quality([]).completeness == 0.0

    def test_state_summarises_its_own_quality_by_default(self):
        s = state(
            [
                FeatureValue.valid("a", 1.0),
                FeatureValue.absent("b", FeatureStatus.MISSING),
            ]
        )
        assert s.quality.completeness == 0.5

    def test_quality_is_json_serialisable(self):
        q = summarize_quality([FeatureValue.valid("a", 1.0)])
        assert json.loads(json.dumps(q.as_dict()))["valid"] == 1

    def test_source_validation_is_carried_through(self):
        q = summarize_quality([], source_validation={"status": "PASS"})
        assert q.as_dict()["source_validation"] == {"status": "PASS"}


class TestHelpers:
    def test_empty_state_marks_every_feature_with_one_reason(self):
        fs = FeatureSet("5m", (spec("a"), spec("b")))
        s = empty_state("Nifty 50", T, fs, FeatureStatus.MARKET_CLOSED, "holiday")
        # A fully-shaped state, so the column set stays consistent and the
        # reason travels with the row.
        assert s.names == ("a", "b")
        assert s.to_vector() == (None, None)
        assert all(v.status is FeatureStatus.MARKET_CLOSED for v in s)
        assert s.quality.completeness == 0.0

    def test_empty_state_rejects_valid_status(self):
        fs = FeatureSet("5m", (spec("a"),))
        with pytest.raises(StateError, match="absent status"):
            empty_state("Nifty 50", T, fs, FeatureStatus.VALID)

    def test_insufficient_history_records_needed_and_available(self):
        v = insufficient_history(spec("rv_20", lookback_bars=20), available=3)
        assert v.status is FeatureStatus.INSUFFICIENT_HISTORY
        assert "20" in v.detail and "3" in v.detail

    def test_insufficient_history_uses_required_bars_for_trailing(self):
        # A trailing feature needs its whole baseline, not just its lookback.
        from features.spec import TRAILING

        s = spec("rv_regime", lookback_bars=20, scope=TRAILING, trailing_sessions=5)
        assert "375" in insufficient_history(s, available=10).detail
