"""Non-destructive validation for tick and OHLCV records.

Design rules (see docs/02-market-data.md):

* Validators are **pure functions**. They never mutate, coerce, re-order,
  interpolate or otherwise "repair" the records handed to them. Financial data
  that silently changes shape is worse than data that is known to be wrong.
* Every problem is reported as an :class:`Issue` carrying a stable ``code``,
  a :class:`Severity`, the offending record index and (where meaningful) the
  timestamp — so callers can reject, quarantine or merely log, as they choose.
* Absence of data is not automatically a defect. Gap detection is done against
  an explicit session model (``marketdata.sessions``); weekends, holidays and
  overnight closes are never reported.

Severity contract
-----------------
``ERROR``   the record violates the data contract or an arithmetic invariant.
            It cannot be trusted for research or trading.
``WARNING`` the record is internally consistent but the dataset shows
            something a human should look at (a gap, an out-of-session bar,
            a cumulative volume that went backwards).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from marketdata.contracts import DatasetContract, contract_for
from marketdata.sessions import (
    MarketSession,
    REGULAR_SESSION,
    TradingDayFn,
    expected_bar_starts,
    is_aligned,
    is_in_session,
)


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


# ── Issue codes ──────────────────────────────────────────────────────────────
# Stable identifiers so downstream consumers can filter without string
# matching on human-readable messages.

MISSING_FIELD = "missing_field"
UNEXPECTED_TYPE = "unexpected_type"
MALFORMED_TIMESTAMP = "malformed_timestamp"
UNKNOWN_FIELD = "unknown_field"
TIMEZONE_INCONSISTENT = "timezone_inconsistent"
TIMESTAMP_NOT_ALIGNED = "timestamp_not_aligned"
DUPLICATE_TIMESTAMP = "duplicate_timestamp"
OUT_OF_ORDER = "out_of_order"
OHLC_INVARIANT = "ohlc_invariant"
NON_POSITIVE_PRICE = "non_positive_price"
NEGATIVE_VALUE = "negative_value"
UNEXPECTED_GAP = "unexpected_gap"
OUT_OF_SESSION = "out_of_session"
VOLUME_REGRESSION = "volume_regression"
LTP_OUTSIDE_DAY_RANGE = "ltp_outside_day_range"


@dataclass(frozen=True)
class Issue:
    """One validation finding. Immutable; carries enough context to act on."""

    code: str
    severity: Severity
    message: str
    index: int | None = None
    timestamp: datetime | None = None
    field_name: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "index": self.index,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "field": self.field_name,
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating one batch of records for one instrument."""

    dataset: str
    record_count: int
    issues: tuple[Issue, ...] = ()
    #: Indices of records carrying at least one ERROR-level issue.
    invalid_indices: frozenset[int] = frozenset()
    #: In-session bar timestamps that the session model expected but that are
    #: absent from the data. Empty for irregular datasets such as ticks.
    missing_timestamps: tuple[datetime, ...] = ()
    #: Number of bar slots the session model expected across the observed span.
    expected_count: int | None = None

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.WARNING)

    @property
    def ok(self) -> bool:
        """True when nothing at ERROR severity was found."""
        return not self.errors

    def count(self, code: str) -> int:
        return sum(1 for i in self.issues if i.code == code)

    def by_code(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.code] = counts.get(issue.code, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "record_count": self.record_count,
            "expected_count": self.expected_count,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issue_counts": self.by_code(),
            "issues": [i.as_dict() for i in self.issues],
            "missing_timestamps": [t.isoformat() for t in self.missing_timestamps],
            "invalid_record_count": len(self.invalid_indices),
        }


# ── Shared helpers ───────────────────────────────────────────────────────────

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_float(value: Any) -> float | None:
    """Best-effort numeric read. Returns ``None`` when not numeric.

    ``Decimal`` values (what psycopg2 returns for NUMERIC columns) are numeric
    but not ``int``/``float``; they are accepted here because comparing them is
    exact and lossless.
    """
    if _is_number(value):
        return float(value)
    try:
        return float(value)  # Decimal, numpy scalars, numeric strings
    except (TypeError, ValueError):
        return None


def _type_ok(value: Any, spec_types: tuple[type, ...]) -> bool:
    if isinstance(value, bool):
        # bool is a subclass of int; never a valid market-data value.
        return False
    if isinstance(value, spec_types):
        return True
    # Accept anything that behaves numerically where a number is expected
    # (Decimal from psycopg2, numpy scalars from pandas).
    if any(t in (int, float) for t in spec_types):
        return _as_float(value) is not None
    return False


def _check_schema(
    record: Mapping[str, Any],
    index: int,
    contract: DatasetContract,
    issues: list[Issue],
    invalid: set[int],
) -> None:
    """Required-field presence, type sanity and unknown-column detection."""
    for spec in contract.fields:
        if spec.name == contract.timestamp_field:
            # Timestamp typing is handled entirely by _read_timestamp, which
            # also accepts the ISO-8601 strings the Angel One feed sends
            # before downloader.ohlcv.normalize_timestamp() runs.
            if record.get(spec.name) is None and spec.required:
                issues.append(Issue(
                    MISSING_FIELD, Severity.ERROR,
                    f"Required field {spec.name!r} is missing or null",
                    index=index, field_name=spec.name,
                ))
                invalid.add(index)
            continue

        present = spec.name in record and record[spec.name] is not None
        if not present:
            if spec.required:
                issues.append(Issue(
                    MISSING_FIELD, Severity.ERROR,
                    f"Required field {spec.name!r} is missing or null",
                    index=index, field_name=spec.name,
                ))
                invalid.add(index)
            continue

        value = record[spec.name]
        if not _type_ok(value, spec.types):
            issues.append(Issue(
                UNEXPECTED_TYPE, Severity.ERROR,
                f"Field {spec.name!r} has type {type(value).__name__}, "
                f"expected {'/'.join(t.__name__ for t in spec.types)}",
                index=index, field_name=spec.name,
            ))
            invalid.add(index)
            continue

        number = _as_float(value)
        if number is None:
            continue
        if spec.positive and number <= 0:
            issues.append(Issue(
                NON_POSITIVE_PRICE, Severity.ERROR,
                f"Field {spec.name!r} must be strictly positive, got {number}",
                index=index, field_name=spec.name, context={"value": number},
            ))
            invalid.add(index)
        elif spec.non_negative and number < 0:
            issues.append(Issue(
                NEGATIVE_VALUE, Severity.ERROR,
                f"Field {spec.name!r} must not be negative, got {number}",
                index=index, field_name=spec.name, context={"value": number},
            ))
            invalid.add(index)

    for key in record:
        if key not in contract.known_fields:
            issues.append(Issue(
                UNKNOWN_FIELD, Severity.WARNING,
                f"Field {key!r} is not part of the {contract.name} contract",
                index=index, field_name=key,
            ))


def _read_timestamp(
    record: Mapping[str, Any],
    index: int,
    contract: DatasetContract,
    issues: list[Issue],
    invalid: set[int],
) -> datetime | None:
    """Return the record's timestamp, reporting (never fixing) tz problems.

    The contract stores naive IST. A timezone-aware value is *reported* rather
    than converted, because converting here would hide an upstream bug and make
    two different ingestion paths silently disagree.
    """
    raw = record.get(contract.timestamp_field)
    if raw is None:
        return None  # already reported by _check_schema when required

    if isinstance(raw, datetime):
        moment = raw
    elif isinstance(raw, str):
        try:
            text = raw.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            moment = datetime.fromisoformat(text)
        except ValueError:
            issues.append(Issue(
                MALFORMED_TIMESTAMP, Severity.ERROR,
                f"Timestamp {raw!r} is not a parseable ISO-8601 datetime",
                index=index, field_name=contract.timestamp_field,
            ))
            invalid.add(index)
            return None
    else:
        issues.append(Issue(
            UNEXPECTED_TYPE, Severity.ERROR,
            f"Field {contract.timestamp_field!r} has type {type(raw).__name__}, "
            "expected datetime or an ISO-8601 string",
            index=index, field_name=contract.timestamp_field,
        ))
        invalid.add(index)
        return None

    if moment.tzinfo is not None:
        # Reported, never converted: normalise upstream (the ingest path calls
        # downloader.ohlcv.normalize_timestamp before validating) so that one
        # timezone decision is made in one place instead of two.
        issues.append(Issue(
            TIMEZONE_INCONSISTENT, Severity.ERROR,
            "Timestamp is timezone-aware; the contract stores naive IST "
            f"(got offset {moment.utcoffset()})",
            index=index, timestamp=moment, field_name=contract.timestamp_field,
            context={"utcoffset": str(moment.utcoffset())},
        ))
        invalid.add(index)
        return None

    return moment


def _check_sequence(
    timestamps: Sequence[tuple[int, datetime]],
    contract: DatasetContract,
    issues: list[Issue],
    invalid: set[int],
) -> None:
    """Duplicate and ordering checks over the timestamps actually present."""
    seen: dict[datetime, int] = {}
    previous: datetime | None = None

    for index, moment in timestamps:
        first = seen.get(moment)
        if first is not None:
            issues.append(Issue(
                DUPLICATE_TIMESTAMP,
                Severity.ERROR if contract.strictly_increasing else Severity.WARNING,
                f"Duplicate timestamp {moment.isoformat()} "
                f"(first seen at record {first})",
                index=index, timestamp=moment,
                context={"first_index": first},
            ))
            if contract.strictly_increasing:
                invalid.add(index)
        else:
            seen[moment] = index

        if previous is not None:
            regressed = moment < previous if not contract.strictly_increasing else moment <= previous
            if regressed and first is None:
                issues.append(Issue(
                    OUT_OF_ORDER,
                    Severity.ERROR if contract.strictly_increasing else Severity.WARNING,
                    f"Timestamp {moment.isoformat()} is not after the previous "
                    f"record's {previous.isoformat()}",
                    index=index, timestamp=moment,
                    context={"previous": previous.isoformat()},
                ))
                if contract.strictly_increasing:
                    invalid.add(index)
        previous = moment


# ── OHLCV validation ─────────────────────────────────────────────────────────

def _check_ohlc_invariants(
    record: Mapping[str, Any],
    index: int,
    moment: datetime | None,
    issues: list[Issue],
    invalid: set[int],
) -> None:
    """high >= max(open, close), low <= min(open, close), high >= low."""
    values = {name: _as_float(record.get(name)) for name in ("open", "high", "low", "close")}
    if any(v is None for v in values.values()):
        return  # missing/typed-wrong values already reported

    open_, high, low, close = (
        values["open"], values["high"], values["low"], values["close"]
    )

    if high < low:
        issues.append(Issue(
            OHLC_INVARIANT, Severity.ERROR,
            f"high ({high}) is below low ({low})",
            index=index, timestamp=moment, context=dict(values),
        ))
        invalid.add(index)
    if high < max(open_, close):
        issues.append(Issue(
            OHLC_INVARIANT, Severity.ERROR,
            f"high ({high}) is below max(open, close) ({max(open_, close)})",
            index=index, timestamp=moment, context=dict(values),
        ))
        invalid.add(index)
    if low > min(open_, close):
        issues.append(Issue(
            OHLC_INVARIANT, Severity.ERROR,
            f"low ({low}) is above min(open, close) ({min(open_, close)})",
            index=index, timestamp=moment, context=dict(values),
        ))
        invalid.add(index)


def validate_ohlcv(
    records: Iterable[Mapping[str, Any]],
    timeframe: str,
    *,
    session: MarketSession = REGULAR_SESSION,
    is_trading_day: TradingDayFn | None = None,
    check_gaps: bool = True,
    check_alignment: bool = True,
) -> ValidationResult:
    """Validate a chronological batch of OHLCV bars for ONE instrument.

    Parameters
    ----------
    records
        Mappings using the column names of ``ohlcv_1min`` / ``ohlcv_5min``.
        They are read, never modified.
    timeframe
        ``"1m"`` or ``"5m"`` (anything ``contracts.contract_for`` accepts).
    session, is_trading_day
        Session model used for gap and out-of-session detection. Inject these
        in tests, or to model an exchange with different hours.
    check_gaps
        Set to ``False`` when validating a deliberately partial batch (for
        example a single ingestion chunk) where gaps carry no meaning.
    check_alignment
        Set to ``False`` for datasets not expected to sit on the session grid.
    """
    contract = contract_for(timeframe)
    if contract.bar_minutes is None:
        raise ValueError(f"{timeframe!r} is not a bar dataset; use validate_ticks()")

    rows = list(records)
    issues: list[Issue] = []
    invalid: set[int] = set()
    timestamps: list[tuple[int, datetime]] = []

    for index, record in enumerate(rows):
        _check_schema(record, index, contract, issues, invalid)
        moment = _read_timestamp(record, index, contract, issues, invalid)
        if moment is not None:
            timestamps.append((index, moment))

            if check_alignment and not is_aligned(moment, contract.bar_minutes, session):
                issues.append(Issue(
                    TIMESTAMP_NOT_ALIGNED, Severity.ERROR,
                    f"Bar timestamp {moment.isoformat()} is not on the "
                    f"{contract.bar_minutes}-minute grid anchored at "
                    f"{session.open.isoformat()}",
                    index=index, timestamp=moment,
                ))
                invalid.add(index)

            if not is_in_session(moment, session, is_trading_day):
                issues.append(Issue(
                    OUT_OF_SESSION, Severity.WARNING,
                    f"Bar timestamp {moment.isoformat()} falls outside a known "
                    "trading session (holiday calendar may be incomplete, or "
                    "the timestamp is in the wrong timezone)",
                    index=index, timestamp=moment,
                ))

        _check_ohlc_invariants(record, index, moment, issues, invalid)

    _check_sequence(timestamps, contract, issues, invalid)

    missing: tuple[datetime, ...] = ()
    expected_count: int | None = None
    if check_gaps and timestamps:
        present = {moment for _, moment in timestamps}
        first = min(present)
        last = max(present)
        expected = expected_bar_starts(
            first, last, contract.bar_minutes, session, is_trading_day
        )
        expected_count = len(expected)
        missing = tuple(m for m in expected if m not in present)
        for moment in missing:
            issues.append(Issue(
                UNEXPECTED_GAP, Severity.WARNING,
                f"No {timeframe} bar at {moment.isoformat()}, which falls "
                "inside a trading session",
                timestamp=moment,
            ))

    return ValidationResult(
        dataset=contract.name,
        record_count=len(rows),
        issues=tuple(issues),
        invalid_indices=frozenset(invalid),
        missing_timestamps=missing,
        expected_count=expected_count,
    )


# ── Tick validation ──────────────────────────────────────────────────────────

def validate_ticks(
    records: Iterable[Mapping[str, Any]],
    *,
    session: MarketSession = REGULAR_SESSION,
    is_trading_day: TradingDayFn | None = None,
    check_volume_monotonic: bool = True,
) -> ValidationResult:
    """Validate a chronological batch of ticks for ONE instrument.

    Ticks are irregular by nature, so there is no gap detection: the absence
    of a tick only means nobody traded. What *is* checked is the contract
    (types, positivity), timestamp sanity, ordering, duplicate timestamps
    (which the ``UNIQUE(instrument_id, timestamp)`` constraint silently drops
    on insert), and the day-cumulative ``volume`` never running backwards
    within a session.
    """
    contract = contract_for("tick")
    rows = list(records)
    issues: list[Issue] = []
    invalid: set[int] = set()
    timestamps: list[tuple[int, datetime]] = []

    previous_volume: float | None = None
    previous_day = None

    for index, record in enumerate(rows):
        _check_schema(record, index, contract, issues, invalid)
        moment = _read_timestamp(record, index, contract, issues, invalid)

        if moment is not None:
            timestamps.append((index, moment))
            if not is_in_session(moment, session, is_trading_day):
                issues.append(Issue(
                    OUT_OF_SESSION, Severity.WARNING,
                    f"Tick timestamp {moment.isoformat()} falls outside a known "
                    "trading session — check the collector's timezone (see "
                    "docs/02-market-data.md) before assuming bad data",
                    index=index, timestamp=moment,
                ))

        high = _as_float(record.get("high"))
        low = _as_float(record.get("low"))
        ltp = _as_float(record.get("ltp"))

        if high is not None and low is not None and high < low:
            issues.append(Issue(
                OHLC_INVARIANT, Severity.ERROR,
                f"Snapshot day high ({high}) is below day low ({low})",
                index=index, timestamp=moment,
                context={"high": high, "low": low},
            ))
            invalid.add(index)
        elif ltp is not None and high is not None and low is not None and not (low <= ltp <= high):
            # A snapshot's day range can lag the LTP by a message, so this is
            # a warning rather than a rejection.
            issues.append(Issue(
                LTP_OUTSIDE_DAY_RANGE, Severity.WARNING,
                f"ltp ({ltp}) is outside the snapshot day range [{low}, {high}]",
                index=index, timestamp=moment,
                context={"ltp": ltp, "low": low, "high": high},
            ))

        if check_volume_monotonic:
            volume = _as_float(record.get("volume"))
            day = moment.date() if moment else None
            if day != previous_day:
                previous_volume = None
                previous_day = day
            if volume is not None:
                if previous_volume is not None and volume < previous_volume:
                    issues.append(Issue(
                        VOLUME_REGRESSION, Severity.WARNING,
                        f"Cumulative day volume fell from {previous_volume} to "
                        f"{volume} within the same session",
                        index=index, timestamp=moment,
                        context={"previous": previous_volume, "value": volume},
                    ))
                previous_volume = volume

    _check_sequence(timestamps, contract, issues, invalid)

    return ValidationResult(
        dataset=contract.name,
        record_count=len(rows),
        issues=tuple(issues),
        invalid_indices=frozenset(invalid),
    )


# ── Non-destructive partitioning ─────────────────────────────────────────────

@dataclass(frozen=True)
class Partition:
    """Records split by validity. Neither list is modified in any way."""

    accepted: tuple[Mapping[str, Any], ...]
    rejected: tuple[Mapping[str, Any], ...]
    result: ValidationResult


def partition(
    records: Sequence[Mapping[str, Any]], result: ValidationResult
) -> Partition:
    """Split *records* into accepted/rejected using *result*'s ERROR findings.

    This is the only "action" this module offers, and it still does not alter
    a single value: rejected rows are handed back verbatim so the caller can
    quarantine them for inspection.
    """
    accepted = tuple(r for i, r in enumerate(records) if i not in result.invalid_indices)
    rejected = tuple(r for i, r in enumerate(records) if i in result.invalid_indices)
    return Partition(accepted=accepted, rejected=rejected, result=result)
