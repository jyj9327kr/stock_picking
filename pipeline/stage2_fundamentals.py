import pandas as pd
import numpy as np
import logging
from datetime import datetime
import time

from config import ROE_MIN, EPS_YOY_MIN, EPS_QOQ_MIN
from utils.data_fetcher import get_financials, dart

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Report code determination
# ─────────────────────────────────────────────
# reprt_code: '11011' (사업보고서/Annual, ~3월 공시)
#             '11012' (반기보고서/Semi, ~8월 공시)
#             '11013' (1분기보고서/Q1, ~5월 공시)
#             '11014' (3분기보고서/Q3, ~11월 공시)

REPORT_SCHEDULE = [
    # (start_month, end_month, year_offset, reprt_code, description)
    (1, 3,   -1, '11014', '3분기보고서'),   # Jan-Mar: most recent is Q3 of prior year
    (4, 5,   -1, '11011', '사업보고서'),     # Apr-May: Annual of prior year
    (6, 8,    0, '11013', '1분기보고서'),     # Jun-Aug: Q1 of current year
    (9, 11,   0, '11012', '반기보고서'),      # Sep-Nov: Semi of current year
    (12, 12,  0, '11014', '3분기보고서'),     # Dec: Q3 of current year
]

# Previous quarter mapping for QoQ comparison
PREV_QUARTER_MAP = {
    '11011': ('11014', 0),   # Annual → previous Q3 (same year)
    '11013': ('11011', -1),  # Q1 → previous Annual (prior year)
    '11012': ('11013', 0),   # Semi → previous Q1 (same year)
    '11014': ('11012', 0),   # Q3 → previous Semi (same year)
}


def get_latest_report_info():
    """
    Determines the latest available report code and year
    based on the current month.
    Returns: (year: int, reprt_code: str, description: str)
    """
    now = datetime.now()
    current_month = now.month
    current_year = now.year

    for start_m, end_m, year_offset, code, desc in REPORT_SCHEDULE:
        if start_m <= current_month <= end_m:
            return current_year + year_offset, code, desc

    # Fallback
    return current_year - 1, '11011', '사업보고서'


# ─────────────────────────────────────────────
#  Financial parsing helpers
# ─────────────────────────────────────────────
# Account name variants used in DART filings
NET_INCOME_NAMES = [
    '당기순이익',
    '당기순이익(손실)',
    '분기순이익',
    '분기순이익(손실)',
    '당기순손익',
    '연결당기순이익',
    '연결당기순이익(손실)',
    '연결분기순이익',
    '연결분기순이익(손실)',
]

EQUITY_NAMES = [
    '자본총계',
    '자본 총계',
    '자본합계',
]


def _clean_amount(value):
    """Converts DART amount string to numeric value."""
    if value is None or value == '' or pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        # Remove commas and whitespace
        cleaned = str(value).replace(',', '').replace(' ', '').strip()
        if cleaned == '' or cleaned == '-':
            return None
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _find_account_value(df, account_names, amount_col='thstrm_amount', fs_div='CFS'):
    """
    Searches for an account value in the financial statement DataFrame.
    Tries CFS (consolidated) first, falls back to OFS (individual).

    Args:
        df: finstate_all DataFrame
        account_names: list of possible account name strings
        amount_col: column to extract value from
        fs_div: 'CFS' for consolidated, 'OFS' for individual

    Returns:
        float or None
    """
    if df is None or df.empty:
        return None

    # Ensure we have the necessary columns
    required_cols = {'account_nm', amount_col}
    if not required_cols.issubset(set(df.columns)):
        return None

    # Try CFS first, then OFS
    for div in [fs_div, 'OFS'] if fs_div == 'CFS' else [fs_div]:
        if 'fs_div' in df.columns:
            df_filtered = df[df['fs_div'] == div]
        else:
            df_filtered = df

        if df_filtered.empty:
            continue

        # Remove whitespace from account names for matching
        df_filtered = df_filtered.copy()
        df_filtered['account_nm_clean'] = df_filtered['account_nm'].astype(str).str.replace(' ', '', regex=False)

        for name in account_names:
            name_clean = name.replace(' ', '')
            matches = df_filtered[df_filtered['account_nm_clean'] == name_clean]
            if not matches.empty:
                val = _clean_amount(matches.iloc[0][amount_col])
                if val is not None:
                    return val

    return None


# ─────────────────────────────────────────────
#  Core metric calculations
# ─────────────────────────────────────────────
def calculate_roe(df_fin):
    """
    Calculates ROE = (당기순이익 / 자본총계) × 100
    Using the current period (thstrm_amount) values.
    """
    net_income = _find_account_value(df_fin, NET_INCOME_NAMES, 'thstrm_amount')
    equity = _find_account_value(df_fin, EQUITY_NAMES, 'thstrm_amount')

    if net_income is None or equity is None or equity == 0:
        return None

    return (net_income / equity) * 100


def calculate_eps_yoy(df_fin):
    """
    Calculates EPS YoY growth = ((당기순이익 - 전기순이익) / |전기순이익|) × 100
    Using thstrm_amount (current) vs frmtrm_amount (prior year).
    """
    net_income_current = _find_account_value(df_fin, NET_INCOME_NAMES, 'thstrm_amount')
    net_income_prior = _find_account_value(df_fin, NET_INCOME_NAMES, 'frmtrm_amount')

    if net_income_current is None or net_income_prior is None:
        return None

    if net_income_prior == 0:
        # If prior was zero and current is positive, that's infinite growth
        return 100.0 if net_income_current > 0 else None

    return ((net_income_current - net_income_prior) / abs(net_income_prior)) * 100


def calculate_eps_qoq(corp_code, current_year, current_reprt_code):
    """
    Calculates EPS QoQ growth by comparing current quarter net income
    with the previous quarter's net income.

    Returns: float (percentage) or None
    """
    # Get current quarter net income
    df_current = get_financials(corp_code, str(current_year), current_reprt_code)
    if df_current is None or df_current.empty:
        return None

    current_ni = _find_account_value(df_current, NET_INCOME_NAMES, 'thstrm_amount')
    if current_ni is None:
        return None

    # Get previous quarter info
    prev_info = PREV_QUARTER_MAP.get(current_reprt_code)
    if prev_info is None:
        return None

    prev_reprt_code, year_adjust = prev_info
    prev_year = current_year + year_adjust

    df_prev = get_financials(corp_code, str(prev_year), prev_reprt_code)
    if df_prev is None or df_prev.empty:
        return None

    prev_ni = _find_account_value(df_prev, NET_INCOME_NAMES, 'thstrm_amount')
    if prev_ni is None:
        return None

    if prev_ni == 0:
        return 100.0 if current_ni > 0 else None

    return ((current_ni - prev_ni) / abs(prev_ni)) * 100


# ─────────────────────────────────────────────
#  Corp code mapping
# ─────────────────────────────────────────────
def get_corp_code_map():
    """Returns a dictionary mapping stock ticker to corp_code."""
    if dart is None:
        return {}
    try:
        df = dart.corp_codes
        # Filter only listed companies
        df = df[df['stock_code'].notna() & (df['stock_code'] != '')]
        return dict(zip(df['stock_code'], df['corp_code']))
    except Exception as e:
        logger.error(f"Error loading corp codes: {e}")
        return {}


# ─────────────────────────────────────────────
#  Main evaluation function
# ─────────────────────────────────────────────

# Fallback order: try these reports in sequence if primary has no data
REPORT_FALLBACK_CHAIN = [
    # (year_offset_from_current, reprt_code, description)
    (0, '11013', '1분기보고서'),       # Current year Q1
    (-1, '11011', '사업보고서'),        # Prior year Annual
    (-1, '11014', '3분기보고서'),       # Prior year Q3
    (-1, '11012', '반기보고서'),        # Prior year Semi
    (-1, '11013', '1분기보고서'),       # Prior year Q1
]


def evaluate_fundamentals(corp_code, year, reprt_code):
    """
    Evaluates fundamentals using actual DART financial statements.
    If the requested report is not available, tries fallback reports.
    Returns a dictionary of metrics if passed, else None.
    """
    if dart is None:
        return None

    # Build list of reports to try: primary first, then fallbacks
    reports_to_try = [(year, reprt_code)]
    for y_offset, rc, _ in REPORT_FALLBACK_CHAIN:
        candidate = (year + y_offset, rc)
        if candidate not in reports_to_try:
            reports_to_try.append(candidate)

    df_fin = None
    used_year = None
    used_reprt_code = None

    for try_year, try_code in reports_to_try:
        df_fin = get_financials(corp_code, str(try_year), try_code)
        if df_fin is not None and not df_fin.empty:
            used_year = try_year
            used_reprt_code = try_code
            if try_year != year or try_code != reprt_code:
                logger.debug(f"  {corp_code}: Fallback to {try_year} {try_code}")
            break

    if df_fin is None or df_fin.empty:
        return None

    # Calculate ROE
    roe = calculate_roe(df_fin)
    if roe is None:
        logger.debug(f"  {corp_code}: ROE calculation failed (missing data)")
        return None

    # Calculate EPS YoY
    eps_yoy = calculate_eps_yoy(df_fin)
    if eps_yoy is None:
        logger.debug(f"  {corp_code}: EPS YoY calculation failed (missing data)")
        return None

    # Calculate EPS QoQ
    eps_qoq = calculate_eps_qoq(corp_code, used_year, used_reprt_code)
    if eps_qoq is None:
        logger.debug(f"  {corp_code}: EPS QoQ calculation failed (missing data)")
        return None

    # Apply filters
    if roe >= ROE_MIN and eps_yoy >= EPS_YOY_MIN and eps_qoq >= EPS_QOQ_MIN:
        return {
            'ROE': round(roe, 2),
            'EPS_YoY': round(eps_yoy, 2),
            'EPS_QoQ': round(eps_qoq, 2),
            'Report_Year': used_year,
            'Report_Code': used_reprt_code,
        }

    logger.debug(
        f"  {corp_code}: Filtered out "
        f"(ROE={roe:.1f}, YoY={eps_yoy:.1f}, QoQ={eps_qoq:.1f})"
    )
    return None


# ─────────────────────────────────────────────
#  Stage 2 Runner
# ─────────────────────────────────────────────
def run_stage2_screening(df_stage1):
    """
    Runs Stage 2 screening on tickers that passed Stage 1.
    Evaluates actual DART financials for ROE, EPS YoY, and EPS QoQ.
    """
    logger.info("=" * 60)
    logger.info(f"Starting STAGE 2: Financial Safety Net on {len(df_stage1)} tickers")
    logger.info("=" * 60)

    if dart is None:
        logger.error(
            "DART API Key not found or invalid. "
            "Cannot run Stage 2 without DART API access. "
            "Set DART_API_KEY in .env file."
        )
        return pd.DataFrame()

    # Determine latest report info
    year, reprt_code, desc = get_latest_report_info()
    logger.info(f"Using report: {year}년 {desc} (reprt_code={reprt_code})")

    # Build ticker → corp_code mapping
    ticker_to_corp = get_corp_code_map()
    if not ticker_to_corp:
        logger.error("Could not load corp code mapping from DART.")
        return pd.DataFrame()

    logger.info(f"Corp code map loaded: {len(ticker_to_corp)} listed companies")

    passed_fundamentals = []
    total = len(df_stage1)

    for i, (_, row) in enumerate(df_stage1.iterrows()):
        ticker = row['Ticker']
        corp_code = ticker_to_corp.get(ticker)

        # Progress logging
        if i % 10 == 0:
            logger.info(f"Processing Stage 2: {i}/{total}")

        if not corp_code:
            logger.debug(f"  {ticker}: No corp_code found, skipping")
            continue

        try:
            metrics = evaluate_fundamentals(corp_code, year, reprt_code)
            if metrics:
                result = row.to_dict()
                result.update(metrics)
                passed_fundamentals.append(result)
                name = row.get('Name', ticker)
                logger.info(
                    f"  ✓ {name}({ticker}): "
                    f"ROE={metrics['ROE']:.1f}%, "
                    f"YoY={metrics['EPS_YoY']:.1f}%, "
                    f"QoQ={metrics['EPS_QoQ']:.1f}%"
                )
        except Exception as e:
            logger.error(f"  Error evaluating {ticker}: {e}")
            continue

    df_passed = pd.DataFrame(passed_fundamentals)

    if not df_passed.empty:
        # Sort by ROE descending
        df_passed = df_passed.sort_values('ROE', ascending=False).reset_index(drop=True)

    logger.info(f"STAGE 2 Completed. {len(df_passed)} tickers passed.")
    return df_passed


def batch_update_financials(tickers_list, year, reprt_code, batch_size=100):
    """
    Batch fetches and caches financial data for a list of tickers.
    Used by the quarterly update schedule.

    Args:
        tickers_list: list of ticker strings
        year: fiscal year
        reprt_code: report code
        batch_size: max tickers per batch to stay within DART daily limit
    """
    if dart is None:
        logger.error("DART API not available for batch update.")
        return

    ticker_to_corp = get_corp_code_map()
    total = len(tickers_list)
    fetched = 0
    errors = 0

    logger.info(f"Batch financial update: {total} tickers, year={year}, reprt_code={reprt_code}")

    for i, ticker in enumerate(tickers_list):
        if fetched >= batch_size:
            logger.info(f"Batch limit ({batch_size}) reached. Stopping to preserve DART API quota.")
            break

        corp_code = ticker_to_corp.get(ticker)
        if not corp_code:
            continue

        try:
            df_fin = get_financials(corp_code, str(year), reprt_code)
            if df_fin is not None and not df_fin.empty:
                fetched += 1
            else:
                errors += 1
        except Exception as e:
            logger.error(f"Batch update error for {ticker}: {e}")
            errors += 1

        # Progress logging
        if i % 50 == 0:
            logger.info(f"Batch progress: {i}/{total} (fetched={fetched}, errors={errors})")

    logger.info(f"Batch update complete: {fetched} fetched, {errors} errors out of {total} attempted")
