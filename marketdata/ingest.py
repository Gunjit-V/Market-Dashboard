"""Non-destructive validation hook for the ingestion path.

The contract this module keeps with the rest of the system:

    detect → report → optionally reject/quarantine

It never edits a candle. In the default ``report`` mode the set of rows handed
back for insertion is *byte-for-byte the rows that came in*, so switching
validation on cannot change what lands in the database — only what gets
logged. ``reject`` mode is opt-in and merely withholds rows that violate the
contract, keeping them intact in ``rejected`` for quarantine.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from marketdata.validation import ValidationResult, validate_ohlcv

LOGGER = logging.getLogger("marketdata.ingest")

REPORT = "report"
REJECT = "reject"
MODES = (REPORT, REJECT)

#: Environment variable selecting the ingestion mode. Default keeps the
#: pre-Phase-1 behaviour exactly: validate, log, insert everything.
MODE_ENV = "MARKETDATA_VALIDATION_MODE"
QUARANTINE_ENV = "MARKETDATA_QUARANTINE_FILE"


def configured_mode(env: Mapping[str, str] | None = None) -> str:
    """Resolve the ingestion validation mode, defaulting to ``report``."""
    source = os.environ if env is None else env
    mode = (source.get(MODE_ENV) or REPORT).strip().lower()
    if mode not in MODES:
        LOGGER.warning(
            "Unknown %s=%r; falling back to %r", MODE_ENV, mode, REPORT
        )
        return REPORT
    return mode


@dataclass(frozen=True)
class ScreenResult:
    """Outcome of screening a batch of raw candles before insertion."""

    #: Rows to insert. In ``report`` mode this is the input, unchanged.
    accepted: tuple[Sequence[Any], ...]
    #: Rows withheld in ``reject`` mode; always the original objects.
    rejected: tuple[Sequence[Any], ...]
    result: ValidationResult
    mode: str

    @property
    def summary(self) -> str:
        counts = self.result.by_code()
        detail = ", ".join(f"{code}={n}" for code, n in sorted(counts.items()))
        return (
            f"{self.result.dataset}: {self.result.record_count} rows, "
            f"{len(self.result.errors)} errors, {len(self.result.warnings)} warnings"
            + (f" [{detail}]" if detail else "")
        )


def candle_to_record(candle: Sequence[Any], instrument_id: int) -> dict[str, Any]:
    """Map one raw Angel One candle to the ``ohlcv_*`` column names.

    The feed returns ``[timestamp, open, high, low, close, volume]``. The
    timestamp is passed through untouched — normalisation stays the
    downloader's job, and validation reports on what the feed actually sent.
    """
    return {
        "instrument_id": instrument_id,
        "timestamp": candle[0],
        "open": candle[1],
        "high": candle[2],
        "low": candle[3],
        "close": candle[4],
        "volume": candle[5],
    }


def screen_candles(
    candles: Sequence[Sequence[Any]],
    instrument_id: int,
    timeframe: str,
    mode: str | None = None,
    *,
    normalize_timestamp=None,
) -> ScreenResult:
    """Validate raw candles ahead of insertion, without altering them.

    Parameters
    ----------
    candles
        Raw ``[ts, o, h, l, c, v]`` rows as returned by the Angel One API.
    instrument_id
        The instrument these candles belong to (used for the contract check).
    timeframe
        ``"1m"`` or ``"5m"``.
    mode
        ``"report"`` (default) or ``"reject"``. ``None`` reads the environment.
    normalize_timestamp
        Optional callable applied to a *copy* of each timestamp purely so the
        validator sees the same naive-IST value the database will store. The
        original candle rows are never touched.

    Gap detection is disabled here: an ingestion batch is a single API chunk
    and is expected to be partial, so gaps carry no meaning at this point.
    Gaps are reported by the dataset-level quality report instead.
    """
    mode = configured_mode() if mode is None else mode
    if mode not in MODES:
        raise ValueError(f"Unknown mode {mode!r}. Choose from: {', '.join(MODES)}")

    records = []
    for candle in candles:
        record = candle_to_record(candle, instrument_id)
        if normalize_timestamp is not None:
            try:
                record["timestamp"] = normalize_timestamp(record["timestamp"])
            except Exception:
                # Leave the raw value in place; the validator will report it
                # as a malformed timestamp rather than the ingest silently
                # dropping the row.
                pass
        records.append(record)

    result = validate_ohlcv(records, timeframe, check_gaps=False)

    if mode == REJECT and result.invalid_indices:
        accepted = tuple(c for i, c in enumerate(candles) if i not in result.invalid_indices)
        rejected = tuple(c for i, c in enumerate(candles) if i in result.invalid_indices)
    else:
        accepted = tuple(candles)
        rejected = ()

    return ScreenResult(accepted=accepted, rejected=rejected, result=result, mode=mode)


def log_screen_result(screened: ScreenResult, context: str = "") -> None:
    """Emit a single log line per batch; details at DEBUG."""
    if not screened.result.issues:
        return
    prefix = f"{context}: " if context else ""
    level = logging.WARNING if screened.result.errors else logging.INFO
    LOGGER.log(level, "%s%s", prefix, screened.summary)
    for issue in screened.result.issues:
        LOGGER.debug("%s%s", prefix, issue.message)


def quarantine_path(env: Mapping[str, str] | None = None) -> Path | None:
    """Where to append rejected rows, if the operator configured a file."""
    source = os.environ if env is None else env
    raw = (source.get(QUARANTINE_ENV) or "").strip()
    return Path(raw) if raw else None


def write_quarantine(
    screened: ScreenResult,
    instrument_id: int,
    timeframe: str,
    path: Path | None = None,
) -> int:
    """Append rejected rows to a JSONL quarantine file. Returns rows written.

    Quarantining is *storage*, never repair: the rejected row is written
    verbatim alongside the reasons it was withheld, so a human can decide what
    to do with it.
    """
    if not screened.rejected:
        return 0
    path = path or quarantine_path()
    if path is None:
        return 0

    reasons: dict[int, list[str]] = {}
    for issue in screened.result.errors:
        if issue.index is not None:
            reasons.setdefault(issue.index, []).append(issue.code)

    rejected_indices = sorted(screened.result.invalid_indices)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("a", encoding="utf-8") as handle:
        for position, index in enumerate(rejected_indices):
            handle.write(json.dumps({
                "quarantined_at": datetime.now().isoformat(),
                "instrument_id": instrument_id,
                "timeframe": timeframe,
                "row": list(screened.rejected[position]),
                "reasons": reasons.get(index, []),
            }, default=str) + "\n")
            written += 1
    return written


__all__ = [
    "MODE_ENV",
    "QUARANTINE_ENV",
    "REJECT",
    "REPORT",
    "ScreenResult",
    "candle_to_record",
    "configured_mode",
    "log_screen_result",
    "quarantine_path",
    "screen_candles",
    "write_quarantine",
]
