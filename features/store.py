"""Writing and reading versioned feature datasets.

Parquet, on disk, under ``data/features/`` -- not a database table. Features
are regenerable derivatives of market data, and writing them back into
PostgreSQL would blur the boundary Phase 1 was careful to keep: raw market data
is stored, everything derived is computed on read. ``data/`` is already
gitignored, pandas reads Parquet natively, and the format is columnar, so a
query touching three of sixty columns reads three.

Every dataset is a **pair**:

``nifty50_5m_fs_5m_381e35d4.parquet``   the rows
``nifty50_5m_fs_5m_381e35d4.json``      the manifest

The feature-set version is in the filename because that is what makes the
versioning useful. Rebuild with a 14-session baseline instead of 5 and a second
file appears beside the first, rather than silently replacing it.

The manifest records what the columns mean, when the data was pulled, how many
rows carry each status, and the source-validation verdict -- everything needed
to judge whether a dataset should be trained on, without rerunning the build.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from features.dataset import BuildStats
from features.quality import FAIL, PASS, WARNING, summarize_sources, worst_verdict
from features.registry import describe, feature_set_digest, feature_set_version
from features.spec import FeatureSet

#: Default output directory. Already gitignored via ``data/``.
DEFAULT_ROOT = Path("data/features")

#: IST offset, for recording the extraction time in the timezone every stored
#: timestamp is read in as well as in UTC.
_IST_OFFSET_HOURS = 5.5


def slug(instrument: str) -> str:
    """A filename-safe form of an instrument symbol."""
    return "".join(
        c.lower() if c.isalnum() else "_" for c in instrument.strip()
    ).strip("_").replace("__", "_")


def dataset_name(instrument: str, fs: FeatureSet) -> str:
    """Stem shared by a dataset's Parquet file and its manifest."""
    return f"{slug(instrument)}_{fs.timeframe}_{feature_set_version(fs)}"


@dataclass(frozen=True)
class DatasetPaths:
    parquet: Path
    manifest: Path

    @property
    def exists(self) -> bool:
        return self.parquet.exists() and self.manifest.exists()


def paths_for(
    instrument: str,
    fs: FeatureSet,
    root: Path | str = DEFAULT_ROOT,
) -> DatasetPaths:
    root = Path(root)
    stem = dataset_name(instrument, fs)
    return DatasetPaths(root / f"{stem}.parquet", root / f"{stem}.json")


def build_manifest(
    instrument: str,
    fs: FeatureSet,
    labels: FeatureSet,
    stats: BuildStats,
    start: date,
    end: date,
    extracted_at: datetime | None = None,
) -> dict[str, Any]:
    """Everything a reader needs to judge a dataset without rebuilding it.

    ``extracted_at`` is recorded because the market-data tables carry no
    as-of versioning (see "No as-of versioning of the data itself" in
    ``docs/02-market-data.md``): if the vendor revises a bar, the original is
    not retained, so the pull time is the only honest record of what this
    dataset saw.
    """
    now = extracted_at or datetime.now(timezone.utc)
    quality = summarize_sources(stats.source_quality)
    # The per-window detail is large and mostly repetitive; keep the summary
    # and only the windows that actually found something.
    quality["windows"] = [
        w for w in quality["windows"] if w["error_count"] or w["warning_count"]
    ]

    return {
        "instrument": instrument,
        "timeframe": fs.timeframe,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "extracted_at_utc": now.isoformat(),
        "extracted_at_ist": (
            now.astimezone(timezone.utc).replace(tzinfo=None)
            + _ist_delta()
        ).isoformat(),
        "rows": stats.rows,
        "sessions": stats.sessions,
        "first_decision_time": (
            stats.first_decision.isoformat() if stats.first_decision else None
        ),
        "last_decision_time": (
            stats.last_decision.isoformat() if stats.last_decision else None
        ),
        "features": describe(fs),
        "labels": {
            "label_set_version": feature_set_version(labels),
            "label_set_digest": feature_set_digest(labels),
            "count": len(labels),
            "labels": [
                {**spec.identity(), "description": spec.description} for spec in labels
            ],
        },
        "feature_status_counts": dict(sorted(stats.status_counts.items())),
        "label_status_counts": dict(sorted(stats.label_status_counts.items())),
        "source_quality": quality,
        "source_verdict": worst_verdict(stats.source_quality),
    }


def _ist_delta():
    from datetime import timedelta

    return timedelta(hours=int(_IST_OFFSET_HOURS), minutes=30)


def column_order(fs: FeatureSet, labels: FeatureSet) -> list[str]:
    """Deterministic column order: identity, then features, then labels.

    Each value is followed immediately by its ``__status`` partner, so a reader
    scanning the schema sees the pairing rather than two distant blocks.
    """
    columns = ["instrument", "decision_time", "timeframe", "feature_set_version"]
    for spec in fs:
        columns += [spec.name, f"{spec.name}__status"]
    for spec in labels:
        columns += [spec.name, f"{spec.name}__status"]
    return columns


def write_dataset(
    rows: Iterable[dict[str, Any]],
    instrument: str,
    fs: FeatureSet,
    labels: FeatureSet,
    stats: BuildStats,
    start: date,
    end: date,
    root: Path | str = DEFAULT_ROOT,
    extracted_at: datetime | None = None,
) -> DatasetPaths:
    """Write rows to Parquet and the manifest beside it."""
    import pandas as pd

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = paths_for(instrument, fs, root)

    frame = pd.DataFrame(list(rows), columns=column_order(fs, labels))
    frame.to_parquet(target.parquet, index=False, compression="snappy")

    manifest = build_manifest(instrument, fs, labels, stats, start, end, extracted_at)
    target.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return target


def dataset_sessions(
    instrument: str,
    fs: FeatureSet,
    root: Path | str = DEFAULT_ROOT,
) -> list[date]:
    """Session dates already present in a dataset, ascending.

    Empty when no dataset exists yet, which is what lets an incremental build
    fall back to a full one without a special case.
    """
    import pandas as pd

    target = paths_for(instrument, fs, root)
    if not target.parquet.exists():
        return []
    frame = pd.read_parquet(target.parquet, columns=["decision_time"])
    if frame.empty:
        return []
    return sorted({t.date() for t in frame["decision_time"]})


def resume_from(
    instrument: str,
    fs: FeatureSet,
    root: Path | str = DEFAULT_ROOT,
) -> date | None:
    """The session an incremental build should restart at, or ``None``.

    Deliberately the **last session already stored**, not the day after it. A
    dataset written while a session was still running holds only part of that
    session, and appending strictly after it would leave the remainder missing
    for good. Rebuilding one session is cheap; silently losing half of one is
    not.
    """
    sessions = dataset_sessions(instrument, fs, root)
    return sessions[-1] if sessions else None


def stats_from_frame(frame, fs: FeatureSet, labels: FeatureSet) -> BuildStats:
    """Recompute dataset-wide counts from the rows themselves.

    An incremental build could instead add the new counts to the old ones, but
    that has to stay in step with which sessions were replaced, and a drift
    there would be invisible. Counting the final rows cannot drift.
    """
    stats = BuildStats()
    stats.rows = len(frame)
    if stats.rows == 0:
        return stats
    stats.sessions = len({t.date() for t in frame["decision_time"]})
    stats.first_decision = min(frame["decision_time"]).to_pydatetime()
    stats.last_decision = max(frame["decision_time"]).to_pydatetime()
    for spec in fs:
        for status, n in frame[f"{spec.name}__status"].value_counts().items():
            stats.status_counts[f"{spec.name}:{status}"] = int(n)
    for spec in labels:
        for status, n in frame[f"{spec.name}__status"].value_counts().items():
            stats.label_status_counts[f"{spec.name}:{status}"] = int(n)
    return stats


def _merge_source_quality(
    old: dict[str, Any] | None,
    new_stats: BuildStats,
    rebuilt_from: date,
) -> dict[str, Any]:
    """Combine the previous run's validation summary with this run's.

    Windows from sessions that were rebuilt are dropped from the old summary,
    so a session validated twice is counted once. Only windows that found
    something are stored in a manifest, so the aggregate counts are adjusted
    from the old totals rather than recomputed from that filtered list.
    """
    fresh = summarize_sources(new_stats.source_quality)
    if not old:
        return fresh

    kept = [
        w for w in old.get("windows", [])
        if date.fromisoformat(w["start"][:10]) < rebuilt_from
    ]
    dropped = [
        w for w in old.get("windows", [])
        if date.fromisoformat(w["start"][:10]) >= rebuilt_from
    ]
    old_sessions_replaced = len({w["start"][:10] for w in dropped})

    merged = kept + fresh["windows"]
    if any(w.get("error_count", 0) for w in merged):
        verdict = FAIL
    elif any(w.get("warning_count", 0) for w in merged):
        verdict = WARNING
    else:
        verdict = PASS

    return {
        "verdict": verdict,
        "window_count": max(0, old.get("window_count", 0) - old_sessions_replaced)
        + fresh["window_count"],
        "error_count": max(
            0, old.get("error_count", 0) - sum(w.get("error_count", 0) for w in dropped)
        )
        + fresh["error_count"],
        "warning_count": max(
            0,
            old.get("warning_count", 0)
            - sum(w.get("warning_count", 0) for w in dropped),
        )
        + fresh["warning_count"],
        "windows": merged,
    }


def append_dataset(
    rows: Iterable[dict[str, Any]],
    instrument: str,
    fs: FeatureSet,
    labels: FeatureSet,
    stats: BuildStats,
    rebuilt_from: date,
    end: date,
    root: Path | str = DEFAULT_ROOT,
    extracted_at: datetime | None = None,
) -> DatasetPaths:
    """Merge freshly built sessions into an existing dataset.

    Rows for ``rebuilt_from`` onward are discarded from the stored file and
    replaced by the new ones, so re-running is idempotent and a partially
    written session is completed rather than duplicated.

    The result must be indistinguishable from a full rebuild;
    ``tests/test_feature_dataset.py`` asserts exactly that.
    """
    import pandas as pd

    target = paths_for(instrument, fs, root)
    columns = column_order(fs, labels)
    fresh = pd.DataFrame(list(rows), columns=columns)

    if target.parquet.exists():
        stored = pd.read_parquet(target.parquet)
        kept = stored[stored["decision_time"].dt.date < rebuilt_from]
        combined = pd.concat([kept, fresh], ignore_index=True)
    else:
        combined = fresh

    combined = combined.sort_values(
        "decision_time", kind="stable"
    ).reset_index(drop=True)
    combined = combined[columns]

    old_manifest = None
    if target.manifest.exists():
        try:
            old_manifest = json.loads(target.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old_manifest = None

    total = stats_from_frame(combined, fs, labels)
    total.source_quality = stats.source_quality

    Path(root).mkdir(parents=True, exist_ok=True)
    combined.to_parquet(target.parquet, index=False, compression="snappy")

    start = (
        date.fromisoformat(old_manifest["range"]["start"])
        if old_manifest and "range" in old_manifest
        else min(t.date() for t in combined["decision_time"])
    )
    manifest = build_manifest(instrument, fs, labels, total, start, end, extracted_at)
    manifest["source_quality"] = _merge_source_quality(
        (old_manifest or {}).get("source_quality"), stats, rebuilt_from
    )
    manifest["source_verdict"] = manifest["source_quality"]["verdict"]
    # Derived from the rows actually written, not from the caller's counters:
    # a manifest should describe what is in the file, whatever route produced it.
    manifest["incremental"] = {
        "rebuilt_from": rebuilt_from.isoformat(),
        "sessions_built": len({t.date() for t in fresh["decision_time"]}),
        "rows_built": len(fresh),
    }
    target.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return target


def read_dataset(
    instrument: str,
    fs: FeatureSet,
    root: Path | str = DEFAULT_ROOT,
    columns: Sequence[str] | None = None,
    filters: Any = None,
):
    """Read a dataset back as a pandas DataFrame.

    ``columns`` and ``filters`` are passed to Parquet, so a query that wants
    three columns or one session reads only the relevant blocks rather than the
    whole file.
    """
    import pandas as pd

    target = paths_for(instrument, fs, root)
    if not target.parquet.exists():
        raise FileNotFoundError(
            f"no dataset at {target.parquet}; build it with "
            f"python -m features.build --instrument {instrument!r}"
        )
    return pd.read_parquet(target.parquet, columns=columns, filters=filters)


def read_manifest(
    instrument: str,
    fs: FeatureSet,
    root: Path | str = DEFAULT_ROOT,
) -> dict[str, Any]:
    target = paths_for(instrument, fs, root)
    return json.loads(target.manifest.read_text(encoding="utf-8"))


def list_datasets(root: Path | str = DEFAULT_ROOT) -> list[dict[str, Any]]:
    """Every dataset under *root*, newest extraction first."""
    root = Path(root)
    if not root.exists():
        return []
    found: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("*.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found.append(
            {
                "stem": manifest_path.stem,
                "instrument": manifest.get("instrument"),
                "timeframe": manifest.get("timeframe"),
                "rows": manifest.get("rows"),
                "sessions": manifest.get("sessions"),
                "feature_set_version": manifest.get("features", {}).get(
                    "feature_set_version"
                ),
                "extracted_at_utc": manifest.get("extracted_at_utc"),
                "source_verdict": manifest.get("source_verdict"),
            }
        )
    return sorted(found, key=lambda d: d["extracted_at_utc"] or "", reverse=True)


__all__ = [
    "DEFAULT_ROOT",
    "DatasetPaths",
    "append_dataset",
    "build_manifest",
    "column_order",
    "dataset_name",
    "dataset_sessions",
    "list_datasets",
    "paths_for",
    "read_dataset",
    "read_manifest",
    "resume_from",
    "slug",
    "stats_from_frame",
    "write_dataset",
]
