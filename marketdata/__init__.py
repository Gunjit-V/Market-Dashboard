"""Trustworthy market-data foundation for HereWeGoAgain.

This package is deliberately dependency-free (standard library only, plus
``scheduler.nse_calendar`` which is itself standard-library only) so that the
data contracts, validation rules and quality reporting can be imported and
unit-tested without a database, without network access and without the
Angel One SDK.

Modules
-------
contracts
    Canonical field definitions for tick / 1-minute / 5-minute records.
sessions
    NSE session model — which bar timestamps are *expected* to exist.
validation
    Non-destructive validators returning structured issues.
quality
    Aggregation of validation issues into a data-quality report.
ingest
    Thin, non-destructive screening helpers used by the ingestion path.
access
    Deterministic, point-in-time-aware historical data access.
"""

from marketdata.contracts import (
    OHLCV_1M,
    OHLCV_5M,
    TICK,
    DatasetContract,
    FieldSpec,
    contract_for,
)
from marketdata.quality import DataQualityReport, build_quality_report
from marketdata.validation import (
    Issue,
    Severity,
    ValidationResult,
    validate_ohlcv,
    validate_ticks,
)

__all__ = [
    "OHLCV_1M",
    "OHLCV_5M",
    "TICK",
    "DatasetContract",
    "FieldSpec",
    "contract_for",
    "DataQualityReport",
    "build_quality_report",
    "Issue",
    "Severity",
    "ValidationResult",
    "validate_ohlcv",
    "validate_ticks",
]
