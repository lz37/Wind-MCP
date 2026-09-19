"""Tests for WindData parsers — runs without Wind Terminal."""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class MockWindData:
    def __init__(self, error_code=0, data=None, fields=None, codes=None, times=None):
        self.ErrorCode = error_code
        self.Data = data or []
        self.Fields = fields or []
        self.Codes = codes or []
        self.Times = times or []


from wind_mcp.core.parser import (
    parse_wss, parse_wsd, parse_wsi, parse_wst, parse_wsq,
    parse_wset, parse_edb, WindAPIError,
    parse_tdays, parse_tdaysoffset, parse_tdayscount,
)


def test_parse_wss_basic():
    result = MockWindData(
        error_code=0,
        data=[[25.6, 30.1], [18.5, 22.3]],
        fields=["close", "pe_ttm"],
        codes=["600030.SH", "000001.SZ"],
    )
    parsed = parse_wss(result)
    assert len(parsed) == 2
    assert parsed[0]["code"] == "600030.SH"
    assert parsed[0]["close"] == 25.6
    assert parsed[0]["pe_ttm"] == 18.5
    assert parsed[1]["code"] == "000001.SZ"
    assert parsed[1]["close"] == 30.1


def test_parse_wss_nan_handling():
    result = MockWindData(
        error_code=0,
        data=[[float("nan")], [10.0]],
        fields=["close", "pe_ttm"],
        codes=["600030.SH"],
    )
    parsed = parse_wss(result)
    assert parsed[0]["close"] is None
    assert parsed[0]["pe_ttm"] == 10.0


def test_parse_wss_error():
    result = MockWindData(error_code=-40521000, data=[["Timeout"]])
    try:
        parse_wss(result)
        assert False, "Should have raised WindAPIError"
    except WindAPIError as e:
        assert e.error_code == -40521000


def test_parse_wsd_single_security():
    result = MockWindData(
        error_code=0,
        data=[[100.0, 101.0, 102.0], [1000, 1100, 1200]],
        fields=["close", "volume"],
        codes=["600030.SH"],
        times=[datetime(2025, 1, 1), datetime(2025, 1, 2), datetime(2025, 1, 3)],
    )
    parsed = parse_wsd(result)
    assert len(parsed) == 3
    assert parsed[0]["date"] == "2025-01-01"
    assert parsed[0]["close"] == 100.0
    assert parsed[2]["volume"] == 1200


def test_parse_wsd_multi_security():
    result = MockWindData(
        error_code=0,
        data=[[100.0, 101.0], [200.0, 201.0]],
        fields=["close"],
        codes=["600030.SH", "000001.SZ"],
        times=[datetime(2025, 1, 1), datetime(2025, 1, 2)],
    )
    parsed = parse_wsd(result)
    assert len(parsed) == 2
    assert parsed[0]["600030.SH"] == 100.0
    assert parsed[0]["000001.SZ"] == 200.0


def test_parse_wsi():
    result = MockWindData(
        error_code=0,
        data=[[100.0, 101.0], [105.0, 106.0], [99.0, 100.0], [103.0, 104.0]],
        fields=["open", "high", "low", "close"],
        times=[datetime(2025, 1, 1, 9, 31), datetime(2025, 1, 1, 9, 32)],
    )
    parsed = parse_wsi(result)
    assert len(parsed) == 2
    assert parsed[0]["datetime"] == "2025-01-01 09:31:00"
    assert parsed[0]["open"] == 100.0
    assert parsed[1]["close"] == 104.0


def test_parse_wset():
    result = MockWindData(
        error_code=0,
        data=[["600030.SH", "000001.SZ"], ["中信证券", "平安银行"], [0.05, 0.03]],
        fields=["wind_code", "sec_name", "i_weight"],
    )
    parsed = parse_wset(result)
    assert len(parsed) == 2
    assert parsed[0]["wind_code"] == "600030.SH"
    assert parsed[0]["sec_name"] == "中信证券"
    assert parsed[1]["i_weight"] == 0.03


def test_parse_wset_empty():
    result = MockWindData(error_code=0, data=[[]], fields=["wind_code"])
    parsed = parse_wset(result)
    assert parsed == []


def test_parse_edb():
    result = MockWindData(
        error_code=0,
        data=[[2.1, 2.3, 2.5]],
        codes=["M5567877"],
        times=[datetime(2025, 1, 1), datetime(2025, 2, 1), datetime(2025, 3, 1)],
    )
    parsed = parse_edb(result)
    assert len(parsed) == 3
    assert parsed[0]["date"] == "2025-01-01"
    assert parsed[0]["M5567877"] == 2.1

# --- tdays-family parsers (WindPy returns nested .Data rows) ---

def test_parse_tdays_nested_data():
    """WindPy tdays returns .Data as [[d1, d2, ...]] — must be flattened."""
    result = MockWindData(
        error_code=0,
        data=[[datetime(2025, 1, 1), datetime(2025, 1, 2), datetime(2025, 1, 3)]],
    )
    parsed = parse_tdays(result)
    assert parsed == ["2025-01-01", "2025-01-02", "2025-01-03"]


def test_parse_tdays_flat_data_still_works():
    """Flat .Data (hypothetical shape) keeps working after flattening."""
    result = MockWindData(
        error_code=0,
        data=[datetime(2025, 1, 1), datetime(2025, 1, 2)],
    )
    parsed = parse_tdays(result)
    assert parsed == ["2025-01-01", "2025-01-02"]


def test_parse_tdaysoffset_nested_data():
    """WindPy tdaysoffset returns .Data as [[d]] — must be un-nested."""
    result = MockWindData(error_code=0, data=[[datetime(2025, 1, 15)]])
    assert parse_tdaysoffset(result) == "2025-01-15"


def test_parse_tdayscount_nested_data():
    """WindPy tdayscount returns .Data as [[n]] — must be un-nested."""
    result = MockWindData(error_code=0, data=[[5]])
    assert parse_tdayscount(result) == 5
