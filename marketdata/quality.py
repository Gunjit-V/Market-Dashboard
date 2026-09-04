"""Data-quality reporting built from real validation results.

Nothing in this module invents a number: every metric is a count taken from
:class:`marketdata.validation.ValidationResult` objects produced by validating
actual records.  A section that was not validated is reported as absent rather
than as zero, so "no ticks were checked" can never be mistaken for "no
problems with the ticks".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from marketdata import validation as V
from marketdata.validation import Severity, ValidationResult

PASS = "PASS"
WARNING = "WARNING"
FAIL = "FAIL"


@dataclass(frozen=True)
class DatasetQuality:
    """Quality metrics for one dataset of one instrument."""

    dataset: str
    records: int
    errors: int
    warnings: int
    duplicates: int
    out_of_order: int
    invalid_ohlc: int
    schema_invalid: int
    unexpected_gaps: int
    out_of_session: int
    volume_issues: int
    expected_records: int | None
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    issue_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.errors:
            return FAIL
        if self.warnings:
            return WARNING
        return PASS

    @property
    def completeness_pct(self) -> float | None:
        """Share of expected in-session bars actually present, 0-100."""
        if not self.expected_records:
            return None
        present = self.expected_records - self.unexpected_gaps
        return round(100.0 * present / self.expected_records, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "status": self.status,
            "records": self.records,
            "expected_records": self.expected_records,
            "completeness_pct": self.completeness_pct,
            "errors": self.errors,
            "warnings": self.warnings,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "invalid_ohlc": self.invalid_ohlc,
            "schema_invalid": self.schema_invalid,
            "unexpected_gaps": self.unexpected_gaps,
            "out_of_session": self.out_of_session,
            "volume_issues": self.volume_issues,
            "first_timestamp": self.first_timestamp.isoformat() if self.first_timestamp else None,
            "last_timestamp": self.last_timestamp.isoformat() if self.last_timestamp else None,
            "issue_counts": dict(self.issue_counts),
        }


@dataclass(frozen=True)
class DataQualityReport:
    """Per-instrument, per-period quality summary across all datasets."""

    instrument: str
    start: datetime
    end: datetime
    sections: tuple[DatasetQuality, ...]
    generated_at: datetime | None = None
    notes: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        statuses = {section.status for section in self.sections}
        if FAIL in statuses:
            return FAIL
        if WARNING in statuses:
            return WARNING
        return PASS

    def section(self, dataset: str) -> DatasetQuality | None:
        for candidate in self.sections:
            if candidate.dataset == dataset:
                return candidate
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "period": {"start": self.start.isoformat(), "end": self.end.isoformat()},
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "status": self.status,
            "sections": [s.as_dict() for s in self.sections],
            "notes": list(self.notes),
        }

    def render(self) -> str:
        """Human-readable report, e.g. for a CLI or a log line."""
        lines = [
            f"Instrument: {self.instrument}",
            f"Period: {self.start.isoformat(sep=' ')} → {self.end.isoformat(sep=' ')}",
        ]
        if not self.sections:
            lines.append("")
            lines.append("No datasets were validated.")
        for section in self.sections:
            lines.append("")
            lines.append(f"{_LABELS.get(section.dataset, section.dataset)}  [{section.status}]")
            lines.append(f"  Records         : {section.records}")
            if section.expected_records is not None:
                lines.append(
                    f"  Expected bars   : {section.expected_records} "
                    f"(completeness {section.completeness_pct}%)"
                )
            lines.append(f"  Duplicates      : {section.duplicates}")
            lines.append(f"  Out-of-order    : {section.out_of_order}")
            lines.append(f"  Schema invalid  : {section.schema_invalid}")
            if section.dataset != "tick":
                lines.append(f"  Invalid OHLC    : {section.invalid_ohlc}")
                lines.append(f"  Unexpected gaps : {section.unexpected_gaps}")
            else:
                lines.append(f"  Invalid snapshot: {section.invalid_ohlc}")
            lines.append(f"  Out-of-session  : {section.out_of_session}")
            lines.append(f"  Volume issues   : {section.volume_issues}")
        for note in self.notes:
            lines.append("")
            lines.append(f"Note: {note}")
        lines.append("")
        lines.append(f"Overall status: {self.status}")
        return "\n".join(lines)


_LABELS = {
    "tick": "Ticks",
    "ohlcv_1m": "1m OHLCV",
    "ohlcv_5m": "5m OHLCV",
}

_SCHEMA_CODES = (
    V.MISSING_FIELD,
    V.UNEXPECTED_TYPE,
    V.MALFORMED_TIMESTAMP,
    V.TIMEZONE_INCONSISTENT,
    V.TIMESTAMP_NOT_ALIGNED,
)
_VOLUME_CODES = (V.NEGATIVE_VALUE, V.VOLUME_REGRESSION)
_OHLC_CODES = (V.OHLC_INVARIANT, V.NON_POSITIVE_PRICE, V.LTP_OUTSIDE_DAY_RANGE)


def summarize(
    result: ValidationResult,
    timestamps: Sequence[datetime] = (),
) -> DatasetQuality:
    """Turn one :class:`ValidationResult` into a :class:`DatasetQuality` row."""
    counts = result.by_code()

    def total(codes: tuple[str, ...]) -> int:
        return sum(counts.get(code, 0) for code in codes)

    ordered = sorted(timestamps)
    return DatasetQuality(
        dataset=result.dataset,
        records=result.record_count,
        errors=len(result.errors),
        warnings=len(result.warnings),
        duplicates=counts.get(V.DUPLICATE_TIMESTAMP, 0),
        out_of_order=counts.get(V.OUT_OF_ORDER, 0),
        invalid_ohlc=total(_OHLC_CODES),
        schema_invalid=total(_SCHEMA_CODES),
        unexpected_gaps=counts.get(V.UNEXPECTED_GAP, 0),
        out_of_session=counts.get(V.OUT_OF_SESSION, 0),
        volume_issues=total(_VOLUME_CODES),
        expected_records=result.expected_count,
        first_timestamp=ordered[0] if ordered else None,
        last_timestamp=ordered[-1] if ordered else None,
        issue_counts=counts,
    )


def build_quality_report(
    instrument: str,
    start: datetime,
    end: datetime,
    results: Mapping[str, ValidationResult],
    timestamps: Mapping[str, Sequence[datetime]] | None = None,
    generated_at: datetime | None = None,
    notes: Sequence[str] = (),
) -> DataQualityReport:
    """Assemble a report from validation results keyed by dataset name.

    ``results`` keys are dataset names (``"tick"``, ``"ohlcv_1m"``,
    ``"ohlcv_5m"``); only the datasets present are reported on.
    """
    timestamps = timestamps or {}
    order = ["tick", "ohlcv_1m", "ohlcv_5m"]
    ordered_keys = [k for k in order if k in results]
    ordered_keys += [k for k in results if k not in order]

    sections = tuple(
        summarize(results[key], timestamps.get(key, ()))
        for key in ordered_keys
    )
    return DataQualityReport(
        instrument=instrument,
        start=start,
        end=end,
        sections=sections,
        generated_at=generated_at,
        notes=tuple(notes),
    )


__all__ = [
    "PASS",
    "WARNING",
    "FAIL",
    "DatasetQuality",
    "DataQualityReport",
    "build_quality_report",
    "summarize",
    "Severity",
]
