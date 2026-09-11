"""
Wind MCP Server — entry point.

Exposes 23 tools + 1 resource via FastMCP for AI assistant access to Wind Financial Terminal.
"""

import json
import logging
import time
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from .models.inputs import (
    SnapshotInput, HistoricalInput, MinuteBarsInput, TicksInput,
    RealtimeInput, DatasetInput, MacroInput, SectorSeriesInput,
    SectorSnapshotInput, ScreenInput, DynamicScreenInput,
    EstimatesInput, HoldersInput, StockConnectInput,
    EventCalendarInput, TradingDaysInput, DateOffsetInput, DaysCountInput,
    CompanyProfileInput,
    PortfolioReportInput, PortfolioSnapshotInput, PortfolioSeriesInput,
)
from .formatters import format_response

from .handlers.snapshot import handle_snapshot
from .handlers.historical import handle_historical
from .handlers.minute_bars import handle_minute_bars
from .handlers.ticks import handle_ticks
from .handlers.realtime import handle_realtime
from .handlers.dataset import handle_dataset
from .handlers.macro import handle_macro
from .handlers.sector import handle_sector_series, handle_sector_snapshot
from .handlers.screening import handle_screen, handle_dynamic_screen
from .handlers.estimates import handle_estimates
from .handlers.holders import handle_holders
from .handlers.stock_connect import handle_stock_connect
from .handlers.calendar import handle_calendar
from .handlers.dates import handle_trading_days, handle_date_offset, handle_days_count
from .handlers.composite import handle_company_profile
from .handlers.portfolio import (
    handle_portfolio_report, handle_portfolio_snapshot, handle_portfolio_series,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def wind_lifespan(server):
    """Manage Wind session lifecycle: startup warmup + shutdown cleanup."""
    logger.info("Wind MCP starting up...")
    try:
        from .core.session import WindSession
        WindSession.get()  # Warm up connection
        logger.info("Wind session warmed up.")
    except Exception as e:
        logger.error(f"Wind session warmup failed: {e}")
    yield
    # Shutdown
    logger.info("Wind MCP shutting down...")
    try:
        from .core.cache import get_cache
        cache = get_cache()
        logger.info(f"Cache stats at shutdown: {cache.stats()}")
    except Exception:
        pass
    try:
        from .core.executor import shutdown_executor
        shutdown_executor()
    except Exception:
        pass
    try:
        from .core.session import WindSession
        WindSession._shutdown()
    except Exception:
        pass
    logger.info("Wind MCP shutdown complete.")


mcp = FastMCP(
    "wind-mcp",
    instructions="Wind Financial Terminal MCP Server",
    lifespan=wind_lifespan,
    host="0.0.0.0",
)


def _timed_call(tool_name: str, func, *args, **kwargs):
    """Execute a handler with metrics instrumentation."""
    from .core.metrics import get_metrics
    metrics = get_metrics()
    t0 = time.perf_counter()
    error = False
    try:
        result = func(*args, **kwargs)
        return result
    except Exception:
        error = True
        raise
    finally:
        latency_ms = (time.perf_counter() - t0) * 1000
        metrics.record_tool_call(tool_name, latency_ms, error=error)


# === Core Data Tools ===

@mcp.tool()
def wind_get_snapshot(params: SnapshotInput) -> str:
    """Cross-sectional data for securities (WSS).

USE WHEN: You need current/point-in-time data for one or more securities — fundamentals, valuation multiples, price, or any snapshot field.
SUPPORTS: Multiple securities × multiple fields. Bloomberg ticker auto-conversion. FieldSet shortcuts (PRICE, VALUATION, MOMENTUM, etc.).
EXAMPLES:
  codes='600030.SH', fields='close,pe_ttm,pb_lf,roe_ttm2'
  codes=['AAPL US Equity','MSFT US Equity'], fields='VALUATION'
  codes='00700.HK', fields='mkt_cap_ard,profit_ttm', options='tradeDate=20250101;unit=1'"""
    data = _timed_call("snapshot", handle_snapshot, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_get_historical(params: HistoricalInput) -> str:
    """Daily time series data (WSD) — price, volume, fundamentals over a date range.

USE WHEN: You need historical daily data over time — price trends, rolling fundamentals, performance comparison.
CONSTRAINT: Multi-code queries only support a single field; single-code queries support multiple fields.
DATE MACROS: '-1M', '-3M', '-1Y', '-5TD', 'LYR' (last year-end), 'MRQ' (most recent quarter), 'ED-10d' (10 days before today).
EXAMPLES:
  codes='600030.SH', fields='close,volume,pct_chg', begin_date='-3M'
  codes=['600030.SH','000001.SZ'], fields='close', begin_date='2024-01-01', end_date='2024-12-31'
  options='Period=W;PriceAdj=F;Fill=Previous' for weekly, forward-adjusted, gap-filled"""
    data = _timed_call("historical", handle_historical, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_get_minute_bars(params: MinuteBarsInput) -> str:
    """Minute-level K-line / OHLCV bars (WSI).

USE WHEN: Intraday bar analysis — 1/3/5/10/15/30/60 minute intervals.
EXAMPLES:
  codes='600030.SH', begin_time='2025-03-28 09:30:00', end_time='2025-03-28 15:00:00'
  options='BarSize=5' for 5-minute bars (default=1)"""
    data = _timed_call("minute_bars", handle_minute_bars, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_get_ticks(params: TicksInput) -> str:
    """Intraday tick data (WST) — trade-by-trade and Level-2 bid/ask.

USE WHEN: You need tick-level granularity — microstructure analysis, VWAP, order flow.
CONSTRAINT: Single security only."""
    data = _timed_call("ticks", handle_ticks, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_get_realtime(params: RealtimeInput) -> str:
    """Real-time quote snapshot (WSQ) — latest price, change, volume.

USE WHEN: You need live/current market data. Fields prefixed with 'rt_'.
NOTE: This is a one-shot snapshot, not a streaming subscription.
EXAMPLES:
  codes='600030.SH', fields='rt_last,rt_chg,rt_pct_chg,rt_vol,rt_amt'"""
    data = _timed_call("realtime", handle_realtime, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_get_dataset(params: DatasetInput) -> str:
    """Report/dataset tables (WSET) — structured reference data.

USE WHEN: You need index/sector constituents, IPO events, fund holdings, margin data, block trades, restricted shares.
KEY TABLES: 'IndexConstituent', 'SectorConstituent', 'StockConnect', 'MarginTrade', 'BlockTrade', 'RestrictedStock', 'IPOEvent', 'FundPortfolio'.
EXAMPLES:
  table_name='IndexConstituent', options='date=2025-03-31;windcode=000300.SH'
  table_name='SectorConstituent', options='date=2025-03-31;sectorId=a001010100'"""
    data = _timed_call("dataset", handle_dataset, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_get_macro(params: MacroInput) -> str:
    """Global macro economic data (EDB) — GDP, CPI, PMI, rates, FX, commodities.

USE WHEN: You need macro indicator time series. Codes are EDB IDs (not security codes).
EXAMPLES:
  codes='M5567877' (China CPI YoY), begin_date='-1Y'
  codes='M0000272,M0000273' (China PMI Manufacturing + Non-manufacturing)
NOTE: Find EDB codes via Wind terminal's EDB browser or code generator."""
    data = _timed_call("macro", handle_macro, params)
    return format_response(data, params.response_format.value)


# === Sector Tools ===

@mcp.tool()
def wind_get_sector_series(params: SectorSeriesInput) -> str:
    """Sector-level time series (WSES) — sector averages over time.

USE WHEN: You need how a sector aggregate metric evolved (average PE, price index, etc.).
CONSTRAINT: Only ONE field per call (Wind API limitation). Make separate calls for multiple fields.
EXAMPLES:
  codes='a001010100', fields='sec_close_avg', begin_date='-3M'"""
    data = _timed_call("sector_series", handle_sector_series, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_get_sector_snapshot(params: SectorSnapshotInput) -> str:
    """Sector-level snapshot (WSEE) — cross-sectional sector averages.

USE WHEN: You need a point-in-time sector aggregate (e.g., current average PE for all A-shares).
CONSTRAINT: Only ONE field per call. Make separate calls for multiple fields.
EXAMPLES:
  codes='a001010100', fields='sec_close_avg'"""
    data = _timed_call("sector_snapshot", handle_sector_snapshot, params)
    return format_response(data, params.response_format.value)


# === Screening Tools ===

@mcp.tool()
def wind_run_screen(params: ScreenInput) -> str:
    """Run a saved screening scheme from Wind terminal (WEQS).

USE WHEN: User has a pre-built screen in Wind terminal and wants to execute it by name."""
    data = _timed_call("screen", handle_screen, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_dynamic_screen(params: DynamicScreenInput) -> str:
    """Custom dynamic stock screening — the most powerful screening tool.

USE WHEN: You want to screen stocks programmatically with custom universe, fields, filters, ranking.
WORKFLOW: 1) Pick universe (index/sector/codes) → 2) Choose fields (FieldSet shortcuts supported) → 3) Apply filters (gt/lt/between/in/etc.) → 4) Rank + slice top N.
EXAMPLES:
  universe='index:000300.SH', fields=['VALUATION','roe_ttm2'], filters=[{'field':'pe_ttm','op':'between','value':[5,30]},{'field':'roe_ttm2','op':'gt','value':15}], rank_by='roe_ttm2', top_n=20"""
    data = _timed_call("dynamic_screen", handle_dynamic_screen, params)
    return format_response(data, params.response_format.value)


# === Analysis Tools ===

@mcp.tool()
def wind_get_estimates(params: EstimatesInput) -> str:
    """Consensus analyst estimates — EPS, revenue, ROE forecasts, target price, ratings.

USE WHEN: You need sell-side consensus data. Default metrics include FY1/FY2 EPS, ROE, rating, target price, and revision momentum (1w/1m/3m).
EXAMPLES:
  codes='600030.SH' (uses default metrics)
  codes=['600030.SH','000001.SZ'], metrics=['est_eps_fy1','wrating_avg']"""
    data = _timed_call("estimates", handle_estimates, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_get_holders(params: HoldersInput) -> str:
    """Shareholder data — top 10, institutional, and fund holders.

USE WHEN: You need ownership structure, institutional positioning, or fund concentration.
TYPES: 'top10_holder' (前10大股东), 'top10_tradable_holder' (前10大流通股东), 'fund_holder' (基金持仓), 'institutional' (机构持仓).
EXAMPLES:
  codes='600030.SH', holder_type='top10_holder'
  codes='600030.SH', holder_type='fund_holder'"""
    data = _timed_call("holders", handle_holders, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_get_stock_connect(params: StockConnectInput) -> str:
    """Stock Connect (沪深港通) northbound holding data — key A-share marginal pricing signal.

USE WHEN: You need northbound flow data — either full summary or per-security holdings.
MODES:
  codes=None → full northbound summary table (WSET)
  codes='600030.SH' → per-security northbound holding details (WSS)"""
    data = _timed_call("stock_connect", handle_stock_connect, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_get_calendar(params: EventCalendarInput) -> str:
    """Event calendar — dividends, IPOs, restricted share unlocks, earnings.

USE WHEN: You need upcoming or historical corporate events.
TYPES: 'dividend' (分红派息), 'ipo' (新股), 'restricted' (限售解禁), 'earnings' (业绩预告/快报).
EXAMPLES:
  event_type='dividend', begin_date='2025-01-01', end_date='2025-06-30'"""
    data = _timed_call("calendar", handle_calendar, params)
    return format_response(data, params.response_format.value)


# === Date Utility Tools ===

@mcp.tool()
def wind_get_trading_days(params: TradingDaysInput) -> str:
    """Trading day sequence between two dates.

USE WHEN: You need a list of trading days for scheduling, alignment, or counting.
SUPPORTS: Multiple exchanges (SSE, SZSE, HKEX, NYSE, etc.), day types (Trading, Weekdays, Alldays), periods (D/W/M/Q/Y).
EXAMPLES:
  begin_date='2025-01-01', end_date='2025-03-31'
  begin_date='-1M', calendar='HKEX'"""
    data = _timed_call("trading_days", handle_trading_days, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_get_date_offset(params: DateOffsetInput) -> str:
    """Get the date N trading/calendar days before or after a reference date.

EXAMPLES: offset=-5 → 5 trading days ago. offset=10, begin_date='2025-01-01' → 10 trading days after Jan 1."""
    data = _timed_call("date_offset", handle_date_offset, params)
    return str(data)


@mcp.tool()
def wind_get_days_count(params: DaysCountInput) -> str:
    """Count trading/calendar days between two dates.

EXAMPLES: begin_date='2025-01-01', end_date='2025-03-31' → number of SSE trading days."""
    data = _timed_call("days_count", handle_days_count, params)
    return str(data)


# === Composite Tools ===

@mcp.tool()
def wind_company_profile(params: CompanyProfileInput) -> str:
    """One-stop company overview — combines 3 Wind API calls into a single response.

RETURNS: {basic: {name, price, mkt_cap, PE/PB/PS, ROE, dividend_yield}, estimates: {EPS FY1/FY2, target_price, rating}, price_history: [last 20 trading days OHLCV]}
USE WHEN: You want a quick comprehensive look at a company before deeper analysis.
EXAMPLES:
  codes='600030.SH'
  codes='AAPL US Equity'"""
    data = _timed_call("company_profile", handle_company_profile, params)
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


# === Portfolio Management Tools ===

@mcp.tool()
def wind_portfolio_report(params: PortfolioReportInput) -> str:
    """Portfolio report from Wind PMS/AMS (WPF) — performance, attribution, risk analysis.

USE WHEN: You need portfolio-level reports (not security-level data).
REQUIRES: Wind PMS/AMS license and portfolio setup.
EXAMPLES:
  product_name='MyFund', table_name='Performance', options='view=AMS;Owner=frank;startDate=2024-01-01;endDate=2025-01-01'"""
    data = _timed_call("portfolio_report", handle_portfolio_report, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_portfolio_snapshot(params: PortfolioSnapshotInput) -> str:
    """Portfolio snapshot from Wind PMS/AMS (WPS) — NAV, holdings, weights, PnL.

USE WHEN: You need current portfolio state: what's in it, how much, at what cost.
REQUIRES: Wind PMS/AMS license.
EXAMPLES:
  portfolio_name='MyFund', fields='nav,return_1d,return_ytd,total_asset'
  portfolio_name='MyFund', fields='wind_code,sec_name,weight,mkt_value,pnl', options='view=AMS;Owner=frank'"""
    data = _timed_call("portfolio_snapshot", handle_portfolio_snapshot, params)
    return format_response(data, params.response_format.value)


@mcp.tool()
def wind_portfolio_series(params: PortfolioSeriesInput) -> str:
    """Portfolio time series from Wind PMS/AMS (WPD) — NAV, return, drawdown over time.

USE WHEN: You need portfolio performance history for charting or analysis.
REQUIRES: Wind PMS/AMS license.
EXAMPLES:
  portfolio_name='MyFund', fields='nav,return_acc,maxdrawdown', begin_date='2024-01-01'
  options='view=AMS;Owner=frank;Period=W;Currency=CNY'"""
    data = _timed_call("portfolio_series", handle_portfolio_series, params)
    return format_response(data, params.response_format.value)


# === Observability Tools ===

@mcp.tool()
def wind_metrics() -> str:
    """Get Wind MCP server metrics: tool call counts, latency, Wind API stats, cache hit rate, uptime."""
    from .core.metrics import get_metrics
    from .core.cache import get_cache
    snapshot = get_metrics().snapshot()
    snapshot["cache"] = get_cache().stats()
    return json.dumps(snapshot, indent=2, ensure_ascii=False)


# === Resources ===

@mcp.resource("wind://health")
def wind_health() -> str:
    """Wind connection and cache health status."""
    from .core.session import WindSession
    from .core.cache import get_cache
    status = {
        "session": WindSession.health_check(),
        "cache": get_cache().stats(),
    }
    return json.dumps(status, indent=2)


# === Server Runner ===

def main():
    import sys
    if "--http" in sys.argv or "--sse" in sys.argv:
        port = 8080
        for arg in sys.argv:
            if arg.startswith("--port="):
                port = int(arg.split("=")[1])
        transport = "streamable-http" if "--http" in sys.argv else "sse"
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port
        mcp.run(transport=transport)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
