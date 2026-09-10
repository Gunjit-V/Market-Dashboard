"""The feature catalogue and its version hash.

The version hash is the "versioned" half of the Phase 2 exit criterion, so
these tests pin down exactly what does and does not change it.
"""

from __future__ import annotations

import pytest

from features import registry
from features.spec import SESSION, TRAILING, FeatureSet, FeatureSpec, SpecError


def spec(**overrides) -> FeatureSpec:
    kwargs = {
        "name": "ret_1",
        "family": "price",
        "timeframe": "5m",
        "lookback_bars": 2,
    }
    kwargs.update(overrides)
    return FeatureSpec(**kwargs)


@pytest.fixture
def empty_registry():
    """An empty catalogue, restored afterwards.

    Registration happens at import time, so a test that wants the registry in
    isolation has to clear it and put it back. The family modules are imported
    *before* clearing: ``feature_set`` loads them lazily, so clearing first
    would simply trigger the import and repopulate what was just emptied.
    """
    registry._load_families()
    saved = {tf: list(bucket) for tf, bucket in registry._REGISTRY.items()}
    registry._reset_registry_for_tests()
    yield registry
    registry._REGISTRY.clear()
    registry._REGISTRY.update({tf: list(b) for tf, b in saved.items()})


class TestRegistration:
    def test_register_returns_the_spec(self, empty_registry):
        s = spec()
        assert empty_registry.register(s) is s

    def test_registered_features_are_retrievable(self, empty_registry):
        empty_registry.register(spec(name="a"))
        assert empty_registry.feature_set("5m").names == ("a",)

    def test_registration_order_is_the_column_order(self, empty_registry):
        empty_registry.register_all(
            [spec(name="zebra"), spec(name="alpha"), spec(name="middle")]
        )
        assert empty_registry.feature_set("5m").names == ("zebra", "alpha", "middle")

    def test_duplicate_name_rejected_rather_than_replaced(self, empty_registry):
        empty_registry.register(spec(name="a"))
        with pytest.raises(SpecError, match="already registered"):
            empty_registry.register(spec(name="a"))

    def test_same_name_allowed_across_timeframes(self, empty_registry):
        empty_registry.register(spec(name="ret_1", timeframe="5m"))
        empty_registry.register(spec(name="ret_1", timeframe="1m"))
        assert empty_registry.feature_set("5m").names == ("ret_1",)
        assert empty_registry.feature_set("1m").names == ("ret_1",)

    def test_timeframes_are_isolated(self, empty_registry):
        empty_registry.register(spec(name="only_5m", timeframe="5m"))
        assert empty_registry.feature_set("1m").names == ()

    def test_unknown_timeframe_rejected(self, empty_registry):
        with pytest.raises(SpecError, match="timeframe"):
            empty_registry.feature_set("15m")

    def test_subset_selection(self, empty_registry):
        empty_registry.register_all([spec(name="a"), spec(name="b"), spec(name="c")])
        assert empty_registry.feature_set("5m", ["c", "a"]).names == ("a", "c")


class TestVersionHash:
    def test_version_is_deterministic(self):
        fs = FeatureSet("5m", (spec(),))
        assert registry.feature_set_version(fs) == registry.feature_set_version(fs)

    def test_equal_definitions_hash_equally(self):
        a = FeatureSet("5m", (spec(name="x", params={"window": 20}),))
        b = FeatureSet("5m", (spec(name="x", params={"window": 20}),))
        assert registry.feature_set_version(a) == registry.feature_set_version(b)

    def test_version_carries_the_timeframe(self):
        assert registry.feature_set_version(FeatureSet("5m", ())).startswith("fs_5m_")
        assert registry.feature_set_version(FeatureSet("1m", ())).startswith("fs_1m_")

    def test_param_change_changes_the_version(self):
        a = FeatureSet("5m", (spec(params={"window": 20}),))
        b = FeatureSet("5m", (spec(params={"window": 30}),))
        assert registry.feature_set_version(a) != registry.feature_set_version(b)

    def test_trailing_sessions_change_changes_the_version(self):
        # The 5-vs-14 baseline comparison must produce two labelled datasets.
        a = FeatureSet("5m", (spec(scope=TRAILING, trailing_sessions=5),))
        b = FeatureSet("5m", (spec(scope=TRAILING, trailing_sessions=14),))
        assert registry.feature_set_version(a) != registry.feature_set_version(b)

    def test_lookback_change_changes_the_version(self):
        a = FeatureSet("5m", (spec(lookback_bars=2),))
        b = FeatureSet("5m", (spec(lookback_bars=3),))
        assert registry.feature_set_version(a) != registry.feature_set_version(b)

    def test_scope_change_changes_the_version(self):
        a = FeatureSet("5m", (spec(name="x", lookback_bars=1),))
        b = FeatureSet("5m", (spec(name="x", lookback_bars=1, scope=SESSION),))
        assert registry.feature_set_version(a) != registry.feature_set_version(b)

    def test_adding_a_feature_changes_the_version(self):
        a = FeatureSet("5m", (spec(name="a"),))
        b = FeatureSet("5m", (spec(name="a"), spec(name="b")))
        assert registry.feature_set_version(a) != registry.feature_set_version(b)

    def test_reordering_features_changes_the_version(self):
        # Order is positional in the vector, so it is part of the identity.
        a = FeatureSet("5m", (spec(name="a"), spec(name="b")))
        b = FeatureSet("5m", (spec(name="b"), spec(name="a")))
        assert registry.feature_set_version(a) != registry.feature_set_version(b)

    def test_description_does_not_change_the_version(self):
        a = FeatureSet("5m", (spec(description="one wording"),))
        b = FeatureSet("5m", (spec(description="another wording entirely"),))
        assert registry.feature_set_version(a) == registry.feature_set_version(b)

    def test_params_key_order_does_not_change_the_version(self):
        a = FeatureSet("5m", (spec(params={"a": 1, "b": 2}),))
        b = FeatureSet("5m", (spec(params={"b": 2, "a": 1}),))
        assert registry.feature_set_version(a) == registry.feature_set_version(b)

    def test_digest_is_full_length_hex(self):
        digest = registry.feature_set_digest(FeatureSet("5m", (spec(),)))
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_version_is_a_prefix_of_the_digest(self):
        fs = FeatureSet("5m", (spec(),))
        version = registry.feature_set_version(fs)
        assert version.endswith(registry.feature_set_digest(fs)[:8])


class TestDescribe:
    def test_describe_reports_the_version_and_count(self):
        fs = FeatureSet("5m", (spec(name="a"), spec(name="b")))
        described = registry.describe(fs)
        assert described["feature_count"] == 2
        assert described["feature_set_version"] == registry.feature_set_version(fs)
        assert described["timeframe"] == "5m"

    def test_describe_lists_features_in_vector_order(self):
        fs = FeatureSet("5m", (spec(name="zebra"), spec(name="alpha")))
        names = [f["name"] for f in registry.describe(fs)["features"]]
        assert names == ["zebra", "alpha"]

    def test_describe_includes_descriptions(self):
        fs = FeatureSet("5m", (spec(description="a one-line explanation"),))
        assert registry.describe(fs)["features"][0]["description"] == (
            "a one-line explanation"
        )

    def test_describe_reports_required_bars_and_instruments(self):
        fs = FeatureSet(
            "5m",
            (
                spec(name="a", scope=TRAILING, trailing_sessions=5),
                spec(name="b", inputs=("Nifty Bank",)),
            ),
        )
        described = registry.describe(fs)
        assert described["required_bars"] == 375
        assert described["required_instruments"] == ["Nifty Bank"]

    def test_describe_is_json_serialisable(self):
        import json

        fs = FeatureSet("5m", (spec(params={"window": 20}, inputs=("SENSEX",)),))
        assert json.loads(json.dumps(registry.describe(fs)))["feature_count"] == 1


class TestCanonicalOrder:
    """Column order must not depend on which module Python imported first.

    Registration happens at import time, so raw insertion order varies with
    import sequence -- and since order is part of the version hash, that would
    make the hash vary between runs. Family order fixes it.
    """

    def test_family_order_is_applied(self):
        from features.registry import state_features

        families = [s.family for s in state_features("5m")]
        assert families == sorted(families, key=registry.FAMILY_ORDER.index)

    def test_price_features_come_first(self):
        from features.registry import state_features

        assert state_features("5m").names[0].startswith("ret_")

    def test_version_is_stable_across_import_orders(self):
        """Both orders were verified to produce fs_5m_381e35d4."""
        import subprocess
        import sys

        script = (
            "import features.{first}, features.{second};"
            "from features.registry import state_features, feature_set_version;"
            "print(feature_set_version(state_features('5m')))"
        )
        versions = set()
        for first, second in (("price", "volatility"), ("volatility", "price")):
            out = subprocess.run(
                [sys.executable, "-c", script.format(first=first, second=second)],
                capture_output=True,
                text=True,
                check=True,
            )
            versions.add(out.stdout.strip())
        assert len(versions) == 1, f"import order changed the version: {versions}"

    def test_labels_sort_after_state_features(self):
        all_specs = registry.feature_set("5m")
        families = [s.family for s in all_specs]
        assert families.index("label") > families.index("price")
