"""
Parsers for WindData objects returned by WindPy API calls.

CRITICAL: WindData.Data is COLUMN-MAJOR:
  .Data = [[field1_val1, field1_val2, ...], [field2_val1, field2_val2, ...]]
  .Fields = ["field1", "field2", ...]
  .Times = [datetime1, datetime2, ...]  (for WSD/WSI/WST)
  .Codes = ["600030.SH", ...]           (for WSS)

All parsers convert to row-major list[dict] for downstream consumption.
"""

import math
from datetime import datetime
from typing import Any
import logging

logger = logging.getLogger(__name__)


class WindAPIError(Exception):
    """Raised when Wind API returns a non-zero error code."""

    def __init__(self, error_code: int, message: str = ""):
        self.error_code = error_code
        super().__init__(f"Wind API error {error_code}: {message}")


def _check_error(result) -> None:
    """Check WindData.ErrorCode and raise if non-zero."""
    if result.ErrorCode != 0:
        msg = ""
        if hasattr(result, "Data") and result.Data:
            msg = str(result.Data[0]) if result.Data[0] else ""
        raise WindAPIError(result.ErrorCode, msg)


def _safe_value(val: Any) -> Any:
    """Convert Wind's special values to Python-friendly types."""
    if val is None:
        return None
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return None
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    return val


def parse_wss(result) -> list[dict]:
    """
    Parse WSS (cross-sectional snapshot) result.

    Returns: list of dicts, one per security.
    Example: [{"code": "600030.SH", "close": 25.6, "pe_ttm": 18.5}, ...]
    """
    _check_error(result)
    rows = []
    codes = result.Codes
    fields = result.Fields
    for i, code in enumerate(codes):
        row = {"code": code}
        for j, field in enumerate(fields):
            row[field.lower()] = _safe_value(result.Data[j][i])
        rows.append(row)
    return rows


def parse_wsd(result) -> list[dict] | dict:
    """
    Parse WSD (daily time series) result.

    For single-security multi-field: rows keyed by date.
    For multi-security single-field: rows keyed by date with code columns.
    For multi-security multi-field: columnar payload {"date": [...], "codes": [...],
    "fields": [...], "series": {code: {field: [values...]}}} — compact for bulk
    ingestion; use response_format=json.

    Returns: list of dicts, one per date (legacy modes), or columnar dict (multi-multi).
    """
    _check_error(result)
    times = result.Times
    fields = result.Fields
    codes = result.Codes

    if len(codes) == 1:
        # Single security, possibly multiple fields
        rows = []
        for i, t in enumerate(times):
            row = {"date": t.strftime("%Y-%m-%d")}
            for j, field in enumerate(fields):
                row[field.lower()] = _safe_value(result.Data[j][i])
            rows.append(row)
        return rows

    if len(fields) == 1:
        # Multiple securities, single field
        rows = []
        for i, t in enumerate(times):
            row = {"date": t.strftime("%Y-%m-%d")}
            for j, code in enumerate(codes):
                row[code] = _safe_value(result.Data[j][i])
            rows.append(row)
        return rows

    # Multiple securities AND multiple fields. WindPy nests Data either
    # field-major ([field][code][time]) or code-major ([code][field][time]).
    # Only the outer length is trustworthy: when n_f == n_c the shape cannot
    # disambiguate at all, so refuse rather than guess wrong.
    n_f, n_c, n_t = len(fields), len(codes), len(times)
    data = result.Data
    if n_f != n_c and len(data) == n_f:
        field_major = True
    elif n_f != n_c and len(data) == n_c:
        field_major = False
    else:
        raise ValueError(
            f"WSD multi-multi 返回维度无法判定: fields={n_f} codes={n_c} "
            f"times={n_t} data_outer={len(data)}"
        )
    for block in data:
        if len(block) != (n_c if field_major else n_f):
            raise ValueError("WSD multi-multi 返回内层维度与判定不一致")

    dates = [t.strftime("%Y-%m-%d") for t in times]
    series: dict[str, dict[str, list]] = {code: {} for code in codes}
    for fi, field in enumerate(fields):
        for ci, code in enumerate(codes):
            values = data[fi][ci] if field_major else data[ci][fi]
            series[code][field.lower()] = [_safe_value(v) for v in values]
    return {
        "date": dates,
        "codes": list(codes),
        "fields": [f.lower() for f in fields],
        "series": series,
    }


def parse_wsi(result) -> list[dict]:
    """
    Parse WSI (minute bar) result.

    Returns: list of dicts with datetime + OHLCV fields.
    """
    _check_error(result)
    rows = []
    for i, t in enumerate(result.Times):
        row = {"datetime": t.strftime("%Y-%m-%d %H:%M:%S")}
        for j, field in enumerate(result.Fields):
            row[field.lower()] = _safe_value(result.Data[j][i])
        rows.append(row)
    return rows


def parse_wst(result) -> list[dict]:
    """
    Parse WST (intraday tick) result.
    Same structure as WSI.
    """
    return parse_wsi(result)


def parse_wsq(result) -> list[dict]:
    """
    Parse WSQ (realtime quote) result.
    Same structure as WSS (cross-sectional).
    """
    return parse_wss(result)


def parse_wset(result) -> list[dict]:
    """
    Parse WSET (dataset/report) result.

    WSET returns tabular data where:
      .Fields = column names
      .Data = [[col1_values], [col2_values], ...]
    No .Codes or .Times.

    Returns: list of row dicts.
    """
    _check_error(result)
    if not result.Data or not result.Data[0]:
        return []
    n_rows = len(result.Data[0])
    rows = []
    for i in range(n_rows):
        row = {}
        for j, field in enumerate(result.Fields):
            row[field] = _safe_value(result.Data[j][i])
        rows.append(row)
    return rows


def parse_edb(result) -> list[dict]:
    """
    Parse EDB (macro economic data) result.

    EDB returns time series for macro indicators.
    .Codes = indicator codes (e.g., "M5567877")
    .Times = dates
    .Data = [[values per indicator]]

    Returns: list of dicts keyed by date.
    """
    _check_error(result)
    rows = []
    codes = result.Codes
    for i, t in enumerate(result.Times):
        row = {"date": t.strftime("%Y-%m-%d")}
        for j, code in enumerate(codes):
            row[code] = _safe_value(result.Data[j][i])
        rows.append(row)
    return rows


def parse_wses(result) -> list[dict]:
    """Parse WSES (sector series) — same structure as WSD."""
    return parse_wsd(result)


def parse_wsee(result) -> list[dict]:
    """Parse WSEE (sector snapshot) — same structure as WSS."""
    return parse_wss(result)


def _flatten_date_cells(cells):
    """Flatten WindPy tdays-family .Data one level.

    WindPy returns .Data as a list of rows, each row itself a list of cells
    (e.g. [[d1, d2, ...]] for tdays, [[d]] for tdaysoffset/tdayscount), while
    other APIs return a flat list of cells per field. Flattening one level
    makes every element a single value so callers can iterate/index safely.
    """
    out = []
    for item in cells:
        if isinstance(item, (list, tuple)):
            out.extend(item)
        else:
            out.append(item)
    return out


def parse_tdays(result) -> list[str]:
    """
    Parse tdays result.
    Returns: list of date strings.
    """
    _check_error(result)
    return [t.strftime("%Y-%m-%d") for t in _flatten_date_cells(result.Data)]


def parse_tdaysoffset(result) -> str:
    """Parse tdaysoffset result. Returns single date string."""
    _check_error(result)
    d = _flatten_date_cells(result.Data)[0]
    return d.strftime("%Y-%m-%d")


def parse_tdayscount(result) -> int:
    """Parse tdayscount result. Returns integer count."""
    _check_error(result)
    return int(_flatten_date_cells(result.Data)[0])
