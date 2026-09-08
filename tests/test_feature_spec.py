"""Feature definitions: validation, derived quantities and set ordering.

Pure description -- no database, no network, no market data.
"""

from __future__ import annotations

import pytest

from features.spec import (
    BARS_PER_SESSION,
    DEFAULT_TRAILING_SESSIONS,
    INTRADAY,
    SESSION,
    TRAILING,
    FeatureSet,
    FeatureSpec,
    SpecError,
)


def spec(**overrides) -> FeatureSpec:
    """A valid spec, overridable field by field."""
    kwargs = {
        "name": "ret_1",
        "family": "price",
        "timeframe": "5m",
        "lookback_bars": 2,
    }
    kwargs.update(overrides)
    return FeatureSpec(**kwargs)


class TestValidation:
    def test_minimal_spec_is_valid(self):
        assert spec().name == "ret_1"

    def test_empty_name_rejected(self):
        with pytest.raises(SpecError, match="name"):
            spec(name="")

    def test_unknown_family_rejected(self):
        with pytest.raises(SpecError, match="family"):
            spec(family="microstructure")

    def test_unknown_timeframe_rejected(self):
        with pytest.raises(SpecError, match="timeframe"):
            spec(timeframe="15m")

    def test_unknown_scope_rejected(self):
        with pytest.raises(SpecError, match="scope"):
            spec(scope="daily")

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_lookback_rejected(self, bad):
        with pytest.raises(SpecError, match="lookback_bars"):
            spec(lookback_bars=bad)

    def test_warmup_below_lookback_rejected(self):
        # Would emit a value computed from fewer bars than the feature claims.
        with pytest.raises(SpecError, match="warmup_bars"):
            spec(lookback_bars=20, warmup_bars=5)

    def test_warmup_may_exceed_lookback(self):
        assert spec(lookback_bars=2, warmup_bars=10).warmup_bars == 10

    def test_warmup_defaults_to_lookback(self):
        assert spec(lookback_bars=7).warmup_bars == 7

    def test_trailing_sessions_rejected_on_intraday_scope(self):
        with pytest.raises(SpecError, match="trailing_sessions"):
            spec(scope=INTRADAY, trailing_sessions=5)

    def test_trailing_sessions_rejected_on_session_scope(self):
        with pytest.raises(SpecError, match="trailing_sessions"):
            spec(scope=SESSION, trailing_sessions=5)

    def test_trailing_sessions_defaults_when_scope_is_trailing(self):
        assert spec(scope=TRAILING).trailing_sessions == DEFAULT_TRAILING_SESSIONS

    def test_trailing_sessions_must_be_positive(self):
        with pytest.raises(SpecError, match="trailing_sessions"):
            spec(scope=TRAILING, trailing_sessions=0)

    def test_explicit_trailing_sessions_is_kept(self):
        assert spec(scope=TRAILING, trailing_sessions=14).trailing_sessions == 14


class TestImmutability:
    def test_params_are_copied_not_aliased(self):
        params = {"window": 20}
        s = spec(params=params)
        params["window"] = 99
        assert s.params["window"] == 20

    def test_inputs_become_a_tuple(self):
        s = spec(inputs=["Nifty Bank"])
        assert s.inputs == ("Nifty Bank",)

    def test_spec_is_frozen(self):
        with pytest.raises(AttributeError):
            spec().name = "other"


class TestRequiredBars:
    def test_intraday_requires_only_its_warmup(self):
        assert spec(lookback_bars=20).required_bars == 20

    def test_session_scope_requires_only_its_warmup(self):
        assert spec(scope=SESSION, lookback_bars=1).required_bars == 1

    def test_trailing_spans_the_baseline_sessions(self):
        s = spec(scope=TRAILING, lookback_bars=20, trailing_sessions=5)
        assert s.required_bars == 5 * BARS_PER_SESSION["5m"]

    def test_trailing_bar_count_differs_per_timeframe(self):
        five = spec(timeframe="5m", scope=TRAILING, trailing_sessions=5)
        one = spec(timeframe="1m", scope=TRAILING, trailing_sessions=5)
        assert five.required_bars == 375
        assert one.required_bars == 1875

    def test_trailing_uses_warmup_when_it_is_the_larger(self):
        s = spec(scope=TRAILING, lookback_bars=2, warmup_bars=9_000, trailing_sessions=1)
        assert s.required_bars == 9_000


class TestFeatureSet:
    def test_preserves_registration_order(self):
        # Column order is positional in the emitted vector; it must never sort.
        names = ("zebra", "alpha", "middle")
        fs = FeatureSet("5m", tuple(spec(name=n) for n in names))
        assert fs.names == names

    def test_duplicate_names_rejected(self):
        with pytest.raises(SpecError, match="duplicate"):
            FeatureSet("5m", (spec(name="ret_1"), spec(name="ret_1")))

    def test_timeframe_mismatch_rejected(self):
        with pytest.raises(SpecError, match="timeframe"):
            FeatureSet("5m", (spec(timeframe="1m"),))

    def test_required_bars_is_the_maximum_over_specs(self):
        fs = FeatureSet(
            "5m",
            (
                spec(name="a", lookback_bars=2),
                spec(name="b", lookback_bars=20),
                spec(name="c", scope=TRAILING, trailing_sessions=5),
            ),
        )
        assert fs.required_bars == 375

    def test_required_bars_of_empty_set_is_zero(self):
        assert FeatureSet("5m", ()).required_bars == 0

    def test_required_instruments_deduplicates_preserving_order(self):
        fs = FeatureSet(
            "5m",
            (
                spec(name="a", inputs=("Nifty Bank",)),
                spec(name="b", inputs=("SENSEX", "Nifty Bank")),
            ),
        )
        assert fs.required_instruments == ("Nifty Bank", "SENSEX")

    def test_by_family_filters(self):
        fs = FeatureSet(
            "5m",
            (spec(name="a"), spec(name="b", family="volatility")),
        )
        assert fs.by_family("volatility") == (fs.specs[1],)

    def test_select_keeps_set_order_not_argument_order(self):
        fs = FeatureSet("5m", (spec(name="a"), spec(name="b"), spec(name="c")))
        assert fs.select(["c", "a"]).names == ("a", "c")

    def test_select_rejects_unknown_names(self):
        fs = FeatureSet("5m", (spec(name="a"),))
        with pytest.raises(SpecError, match="unknown feature"):
            fs.select(["nope"])

    def test_len_and_iteration(self):
        fs = FeatureSet("5m", (spec(name="a"), spec(name="b")))
        assert len(fs) == 2
        assert [s.name for s in fs] == ["a", "b"]


class TestIdentity:
    def test_identity_excludes_description(self):
        # Rewording a docstring must not invalidate an existing dataset.
        a = spec(description="one wording")
        b = spec(description="a completely different wording")
        assert a.identity() == b.identity()

    def test_identity_includes_params(self):
        assert spec(params={"window": 20}).identity()["params"] == {"window": 20}

    def test_identity_params_are_sorted(self):
        ident = spec(params={"b": 2, "a": 1}).identity()
        assert list(ident["params"]) == ["a", "b"]

    def test_identity_reflects_trailing_sessions(self):
        five = spec(scope=TRAILING, trailing_sessions=5).identity()
        fourteen = spec(scope=TRAILING, trailing_sessions=14).identity()
        assert five != fourteen
