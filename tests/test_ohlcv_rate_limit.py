"""Rate-limit classification in the OHLCV candle fetch.

Angel One signals throttling two ways: a structured JSON body with errorcode
AB1004, and a plain-text body the SDK cannot parse, which surfaces as a
DataException. The second form previously fell into the generic handler and
was logged as a bare "DataException" — indistinguishable from the data simply
not existing.
"""

from __future__ import annotations

import pytest

pytest.importorskip("SmartApi", reason="Angel One SDK not installed")

from downloader.ohlcv import RATE_LIMIT_ERRORCODE, _is_rate_limit_error


def test_the_unparseable_throttle_response_is_recognised():
    exc = Exception(
        "Couldn't parse the JSON response received from the server: "
        "b'Access denied because of exceeding access rate'"
    )
    assert _is_rate_limit_error(exc)


def test_detection_is_case_insensitive():
    assert _is_rate_limit_error(Exception("ACCESS DENIED BECAUSE OF EXCEEDING ACCESS RATE"))


def test_a_generic_parse_failure_is_not_a_rate_limit():
    # A 500 page is a real failure and must not be mislabelled as throttling,
    # which would imply it resolves on its own.
    exc = Exception("Couldn't parse the JSON response: b'<html>500</html>'")
    assert not _is_rate_limit_error(exc)


def test_a_network_error_is_not_a_rate_limit():
    assert not _is_rate_limit_error(ConnectionError("connection reset by peer"))


def test_the_structured_errorcode_is_still_recognised():
    assert RATE_LIMIT_ERRORCODE == "AB1004"


def test_the_handler_logs_the_message_not_just_the_class():
    import inspect

    from downloader.ohlcv import fetch_candle_data

    src = inspect.getsource(fetch_candle_data)
    # The message must reach the log; type(e).__name__ alone hid the cause.
    assert "{type(e).__name__}: {e}" in src
    assert "_is_rate_limit_error" in src
