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
from features.quality import summarize_sources, worst_verdict
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
    as-of versioning (``docs/point-in-time-data.md`` §5.4): if the vendor
    revises a bar, the original is not retained, so the pull time is the only
    honest record of what this dataset saw.
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
    "build_manifest",
    "column_order",
    "dataset_name",
    "list_datasets",
    "paths_for",
    "read_dataset",
    "read_manifest",
    "slug",
    "write_dataset",
]
