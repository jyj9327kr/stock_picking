import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta
import time

from config import (
    MA_WINDOWS,
    DISTANCE_52W_HIGH_MAX,
    SECTOR_TRAIL_MONTHS,
    VOLUME_TOP_N,
    VOLUME_DAYS,
    VOLUME_POOL_MIN_STOCKS
)
from utils.data_fetcher import (
    get_kospi_kosdaq_tickers,
    get_ohlcv,
    get_ticker_name,
    get_stock_sector_map,
    get_sector_stocks_grouped,
    get_top_volume_tickers,
    get_market_benchmark_return,
    calculate_sector_performance,
    get_naver_financial_info,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Minervini Trend Filters
# ─────────────────────────────────────────────
def apply_minervini_trend_filters(df_ohlcv, market, benchmark_return_3m):
    """
    Applies Minervini trend filters to a single stock's OHLCV dataframe.
    Returns (passed: bool, metrics: dict or None).
    """
    # Requires at least 250 trading days (approx 1 year) to safely calculate 200-day SMA, 
    # 52-week High/Low, and 1-month-ago 200-day SMA.
    if df_ohlcv is None or len(df_ohlcv) < 250:
        return False, None

    try:
        current_price = df_ohlcv['Close'].iloc[-1]

        # Calculate SMAs
        close_series = df_ohlcv['Close']
        sma_50_series = close_series.rolling(window=50).mean()
        sma_150_series = close_series.rolling(window=150).mean()
        sma_200_series = close_series.rolling(window=200).mean()

        sma_50 = sma_50_series.iloc[-1]
        sma_150 = sma_150_series.iloc[-1]
        sma_200 = sma_200_series.iloc[-1]

        # 200-day SMA from 1 month ago (approx 20 trading days ago)
        sma_200_1m_ago = sma_200_series.iloc[-21]

        # 52-week high and low (approx 252 trading days)
        available_days = min(252, len(df_ohlcv))
        high_52w = close_series.iloc[-available_days:].max()
        low_52w = close_series.iloc[-available_days:].min()

        # 3-month return (approx 60 trading days ago)
        price_3m_ago = close_series.iloc[-61]
        if price_3m_ago <= 0:
            return False, None
        return_3m = (current_price - price_3m_ago) / price_3m_ago

        # 1. Current Price Location: Price > 150 SMA AND Price > 200 SMA
        cond_1 = (current_price > sma_150) and (current_price > sma_200)

        # 2. Moving Average Alignment 1: 150 SMA > 200 SMA
        cond_2 = sma_150 > sma_200

        # 3. 200-day SMA Trend: SMA 200 > 1 month ago SMA 200
        cond_3 = sma_200 > sma_200_1m_ago

        # 4. Moving Average Alignment 2: 50 SMA > 150 SMA AND 50 SMA > 200 SMA
        cond_4 = (sma_50 > sma_150) and (sma_50 > sma_200)

        # 5. Short-term Trend Support: Price > 50 SMA
        cond_5 = current_price > sma_50

        # 6. Bottom Escape Confirmation: Price >= 52-week Low * 1.3
        cond_6 = current_price >= (low_52w * 1.3)

        # 7. Proximity to High: Price >= 52-week High * 0.75
        cond_7 = current_price >= (high_52w * 0.75)

        # 8. Relative Strength: 3-month Return > 3-month Benchmark Return
        cond_8 = return_3m > benchmark_return_3m

        if not (cond_1 and cond_2 and cond_3 and cond_4 and cond_5 and cond_6 and cond_7 and cond_8):
            return False, None

        dist_52w_high = (high_52w - current_price) / high_52w
        dist_52w_low = (current_price - low_52w) / low_52w
        excess_return_3m = return_3m - benchmark_return_3m

        return True, {
            'Current_Price': current_price,
            'SMA_50': round(sma_50, 2),
            'SMA_150': round(sma_150, 2),
            'SMA_200': round(sma_200, 2),
            'SMA_200_1M_Ago': round(sma_200_1m_ago, 2),
            'High_52W': high_52w,
            'Low_52W': low_52w,
            'Distance_52W_High': round(dist_52w_high, 4),
            'Distance_52W_Low': round(dist_52w_low, 4),
            'Return_3M': round(return_3m, 4),
            'Excess_Return_3M': round(excess_return_3m, 4)
        }
    except Exception as e:
        logger.error(f"Error applying Minervini filters: {e}")
        return False, None


# ─────────────────────────────────────────────
#  Sector Leadership Filter
# ─────────────────────────────────────────────
def identify_outperforming_sectors(sector_grouped, market="KOSPI"):
    """
    Identifies sectors that outperform the broader market (KOSPI/KOSDAQ)
    over SECTOR_TRAIL_MONTHS (e.g., [1, 3] months).

    A sector must outperform in at least one trailing period.

    Args:
        sector_grouped: dict[sector_name] -> list[ticker]
        market: 'KOSPI' or 'KOSDAQ'

    Returns:
        set of sector names that are outperforming
    """
    outperforming = set()

    for trail_months in SECTOR_TRAIL_MONTHS:
        benchmark_return = get_market_benchmark_return(market, trail_months)
        logger.info(f"{market} benchmark {trail_months}M return: {benchmark_return:.4f}")

        for sector_name, sector_tickers in sector_grouped.items():
            if sector_name in outperforming:
                continue  # Already qualified

            sector_return = calculate_sector_performance(sector_tickers, trail_months)
            if sector_return is not None and sector_return > benchmark_return:
                outperforming.add(sector_name)
                logger.debug(
                    f"  ✓ {sector_name} {trail_months}M: "
                    f"{sector_return:.4f} > {benchmark_return:.4f}"
                )

    logger.info(f"{market}: {len(outperforming)} outperforming sectors identified")
    return outperforming


# ─────────────────────────────────────────────
#  Volume Pool Filter
# ─────────────────────────────────────────────
def filter_by_volume_pool(outperforming_sectors, sector_grouped):
    """
    Filters outperforming sectors to only those with at least
    VOLUME_POOL_MIN_STOCKS stocks in the top VOLUME_TOP_N by trading value.

    Returns:
        set of stock tickers belonging to qualified sectors
        dict mapping ticker -> sector_name
    """
    top_vol_tickers = get_top_volume_tickers(top_n=VOLUME_TOP_N)
    if not top_vol_tickers:
        logger.warning("Could not get top volume tickers. Skipping volume pool filter.")
        # Return all tickers from outperforming sectors
        all_tickers = set()
        ticker_to_sector = {}
        for sector in outperforming_sectors:
            for t in sector_grouped.get(sector, []):
                all_tickers.add(t)
                ticker_to_sector[t] = sector
        return all_tickers, ticker_to_sector

    qualified_tickers = set()
    ticker_to_sector = {}

    for sector_name in outperforming_sectors:
        sector_tickers = set(sector_grouped.get(sector_name, []))
        overlap = sector_tickers & top_vol_tickers

        if len(overlap) >= VOLUME_POOL_MIN_STOCKS:
            # All constituents of this qualified sector pass
            for t in sector_tickers:
                qualified_tickers.add(t)
                ticker_to_sector[t] = sector_name
            logger.info(
                f"  Sector '{sector_name}': {len(overlap)} stocks in top {VOLUME_TOP_N} volume → QUALIFIED"
            )
        else:
            logger.debug(
                f"  Sector '{sector_name}': {len(overlap)} stocks in top {VOLUME_TOP_N} volume → skipped"
            )

    return qualified_tickers, ticker_to_sector


# ─────────────────────────────────────────────
#  Main Stage 1 Runner
# ─────────────────────────────────────────────
def run_stage1_screening():
    """
    Runs Stage 1 screening:
    Applies Minervini trend filters directly to all KOSPI and KOSDAQ tickers.
    Returns a DataFrame of tickers that passed all filters.
    """
    logger.info("=" * 60)
    logger.info("Starting STAGE 1: Technical Screening (Minervini Trend Template)")
    logger.info("=" * 60)

    # Load all tickers
    df_tickers = get_kospi_kosdaq_tickers()
    if df_tickers is None or df_tickers.empty:
        logger.error("Could not fetch ticker list.")
        return pd.DataFrame()

    # Filter out preferred stocks (우선주 제외)
    initial_count = len(df_tickers)
    # Exclude names ending with 우, 우A, 우B, 우C, 우선주, 우(전환), 우(선), 우1, 우2, 우3 etc.
    df_tickers = df_tickers[~df_tickers['name'].str.contains(r'우$|우[A-C]$|우선주$|우\(.*?\)$|우\d[A-C]?$', regex=True)]
    logger.info(f"Filtered out {initial_count - len(df_tickers)} preferred stocks (우선주). Remaining: {len(df_tickers)}")

    # Filter out stocks with market cap <= 150B KRW (150,000,000,000 KRW)
    if 'Marcap' in df_tickers.columns:
        initial_cap_count = len(df_tickers)
        df_tickers = df_tickers[df_tickers['Marcap'] > 150000000000]
        logger.info(f"Filtered out {initial_cap_count - len(df_tickers)} stocks with market cap <= 150B KRW. Remaining: {len(df_tickers)}")
    else:
        logger.warning("Marcap column not found in ticker data. Cannot apply 150B cap filter.")

    # Build maps
    name_map = dict(zip(df_tickers['ticker'], df_tickers['name']))
    market_map = dict(zip(df_tickers['ticker'], df_tickers['market']))
    marcap_map = dict(zip(df_tickers['ticker'], df_tickers['Marcap'])) if 'Marcap' in df_tickers.columns else {}
    
    # Load sector map for final reporting/metadata
    sector_map = get_stock_sector_map()

    qualified_tickers = set(df_tickers['ticker'].tolist())
    logger.info(f"Total {len(qualified_tickers)} tickers to screen with Minervini filters.")

    # ── Minervini Trend Filters ──
    logger.info(f"Applying Minervini trend filters...")
    
    # Pre-calculate 3-month benchmark returns for KOSPI and KOSDAQ to optimize performance
    benchmark_3m_kospi = get_market_benchmark_return('KOSPI', months=3)
    benchmark_3m_kosdaq = get_market_benchmark_return('KOSDAQ', months=3)
    logger.info(f"3-Month Benchmark Returns - KOSPI: {benchmark_3m_kospi:.2%}, KOSDAQ: {benchmark_3m_kosdaq:.2%}")
    
    passed_tickers = []
    total = len(qualified_tickers)

    for i, ticker in enumerate(sorted(qualified_tickers)):
        if i % 200 == 0:
            logger.info(f"Processing Stage 1: {i}/{total}")

        df_ohlcv = get_ohlcv(ticker)
        if df_ohlcv is None or df_ohlcv.empty:
            continue

        market = market_map.get(ticker, "N/A")
        # Use the maximum of KOSPI and KOSDAQ 3-month returns as a universal benchmark to select true market leaders
        benchmark_return_3m = max(benchmark_3m_kospi, benchmark_3m_kosdaq)

        passed, metrics = apply_minervini_trend_filters(df_ohlcv, market, benchmark_return_3m)
        if passed and metrics:
            name = name_map.get(ticker, '') or get_ticker_name(ticker)
            sector = sector_map.get(ticker, "N/A") if sector_map else "N/A"

            result = {
                'Ticker': ticker,
                'Name': name,
                'Market': market,
                'Sector': sector,
                'Marcap': marcap_map.get(ticker, 0),
            }
            result.update(metrics)
            passed_tickers.append(result)

    df_passed = pd.DataFrame(passed_tickers)

    if not df_passed.empty:
        logger.info(f"Fetching Naver financial info for {len(df_passed)} passed tickers...")
        df_passed['Revenue'] = None
        df_passed['Operating_Income'] = None
        df_passed['ROE'] = None
        df_passed['PER'] = None
        df_passed['PBR'] = None
        
        for idx, row in df_passed.iterrows():
            ticker = row['Ticker']
            try:
                fin_info = get_naver_financial_info(ticker)
                df_passed.at[idx, 'Revenue'] = fin_info.get('Revenue')
                df_passed.at[idx, 'Operating_Income'] = fin_info.get('Operating_Income')
                df_passed.at[idx, 'ROE'] = fin_info.get('ROE')
                df_passed.at[idx, 'PER'] = fin_info.get('PER')
                df_passed.at[idx, 'PBR'] = fin_info.get('PBR')
            except Exception as e:
                logger.error(f"Error updating financials for {ticker}: {e}")
                
        df_passed = df_passed.sort_values('Distance_52W_High', ascending=True).reset_index(drop=True)

    logger.info(f"STAGE 1 Completed. {len(df_passed)} tickers passed.")
    return df_passed
