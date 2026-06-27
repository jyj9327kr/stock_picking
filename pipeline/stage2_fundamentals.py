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


def get_latest_report_info(target_date=None):
    """
    Determines the latest available report code and year
    based on the current month or a target date.
    Returns: (year: int, reprt_code: str, description: str)
    """
    if target_date is None:
        now = datetime.now()
    else:
        if isinstance(target_date, str):
            try:
                now = datetime.strptime(target_date, '%Y-%m-%d')
            except ValueError:
                now = datetime.now()
        else:
            now = target_date

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

REVENUE_NAMES = [
    '매출액',
    '영업수익',
    '매출',
    '매출액(영업수익)',
]

OPER_INCOME_NAMES = [
    '영업이익',
    '영업이익(손실)',
    '영업손익',
]

OCF_NAMES = [
    '영업활동현금흐름',
    '영업활동으로인한현금흐름',
    '영업활동으로 인한 현금흐름',
    '영업활동으로인한현금흐름(손실)',
    '영업에서창출된현금흐름',
    '영업에서 창출된 현금흐름',
]

CAPEX_ACQUISITION_NAMES = [
    '유형자산의취득',
    '유형자산의 취득',
    '유형자산취득',
    '무형자산의취득',
    '무형자산의 취득',
    '무형자산취득',
]

AR_NAMES = [
    '매출채권',
    '매출채권및기타채권',
    '매출채권 및 기타채권',
    '매출채권및기타유동채권',
    '매출채권 및 기타유동채권',
]

INV_NAMES = [
    '재고자산',
    '재고자산합계',
    '재고자산 총액',
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
    Returns a dictionary of metrics including Risk_Flags.
    """
    if dart is None:
        return {'Risk_Flags': 'DART API 미지정'}

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
        return {'Risk_Flags': 'DART 데이터 미비'}

    # ─────────────────────────────────────────────
    #  Risk Diagnostics
    # ─────────────────────────────────────────────
    Risk_Flags = []

    # 1. 영업이익 및 OCF
    op_income = _find_account_value(df_fin, OPER_INCOME_NAMES, 'thstrm_amount')
    ocf = _find_account_value(df_fin, OCF_NAMES, 'thstrm_amount')
    if op_income is not None and ocf is not None:
        if op_income > 0 and ocf < 0:
            Risk_Flags.append("영업이익은 흑자이지만 영업활동현금흐름 적자")

    # 2. FCF 적자
    if ocf is not None:
        capex_val = 0.0
        for name in CAPEX_ACQUISITION_NAMES:
            val = _find_account_value(df_fin, [name], 'thstrm_amount')
            if val is not None:
                capex_val += abs(val)
        fcf = ocf - capex_val
        if fcf < 0:
            Risk_Flags.append("FCF 적자")

    # 3. 운전자본 급증
    rev_cur = _find_account_value(df_fin, REVENUE_NAMES, 'thstrm_amount')
    rev_prev = _find_account_value(df_fin, REVENUE_NAMES, 'frmtrm_amount')
    ar_cur = _find_account_value(df_fin, AR_NAMES, 'thstrm_amount')
    ar_prev = _find_account_value(df_fin, AR_NAMES, 'frmtrm_amount')
    inv_cur = _find_account_value(df_fin, INV_NAMES, 'thstrm_amount')
    inv_prev = _find_account_value(df_fin, INV_NAMES, 'frmtrm_amount')

    rev_growth = ((rev_cur - rev_prev) / rev_prev) * 100 if (rev_cur is not None and rev_prev and rev_prev > 0) else None
    ar_growth = ((ar_cur - ar_prev) / ar_prev) * 100 if (ar_cur is not None and ar_prev and ar_prev > 0) else None
    inv_growth = ((inv_cur - inv_prev) / inv_prev) * 100 if (inv_cur is not None and inv_prev and inv_prev > 0) else None

    has_wc_risk = False
    if rev_growth is not None:
        if rev_growth >= 5.0:
            if (ar_growth is not None and ar_growth > rev_growth * 2.0) or \
               (inv_growth is not None and inv_growth > rev_growth * 2.0):
                has_wc_risk = True
        else:
            if (ar_growth is not None and ar_growth >= 15.0) or \
               (inv_growth is not None and inv_growth >= 15.0):
                has_wc_risk = True
    if has_wc_risk:
        Risk_Flags.append("운전자본 급증")

    # 4. CB/BW/유증 빈도
    capital_events = 0
    for y in [used_year - 1, used_year] if used_year else [year - 1, year]:
        try:
            df_cap = dart.report(corp_code, "증자", y)
            if df_cap is not None and not df_cap.empty and 'isu_dcrs_stle' in df_cap.columns:
                for _, r in df_cap.iterrows():
                    stle = str(r['isu_dcrs_stle'])
                    if any(kw in stle for kw in ['유상증자', '전환사채', '신주인수권', 'CB', 'BW', '제3자배정']):
                        capital_events += 1
        except Exception as e:
            logger.debug(f"Error fetching capital change for {corp_code} in {y}: {e}")
    if capital_events >= 2:
        Risk_Flags.append("CB/BW 및 유상증자 이력")

    Risk_Flags_str = ", ".join(Risk_Flags) if Risk_Flags else "정상"

    # Calculate standard metrics (ROE, EPS growth) for reporting purposes
    roe = calculate_roe(df_fin)
    eps_yoy = calculate_eps_yoy(df_fin)
    eps_qoq = calculate_eps_qoq(corp_code, used_year, used_reprt_code)

    return {
        'ROE': round(roe, 2) if roe is not None else None,
        'EPS_YoY': round(eps_yoy, 2) if eps_yoy is not None else None,
        'EPS_QoQ': round(eps_qoq, 2) if eps_qoq is not None else None,
        'Revenue': round(rev_cur / 100000000.0, 1) if rev_cur is not None else None,
        'Operating_Income': round(op_income / 100000000.0, 1) if op_income is not None else None,
        'Report_Year': used_year,
        'Report_Code': used_reprt_code,
        'Risk_Flags': Risk_Flags_str
    }


# ─────────────────────────────────────────────
#  Stage 2 Runner
# ─────────────────────────────────────────────
def run_stage2_screening(df_stage1, target_date=None):
    """
    Runs Stage 2 screening on tickers that passed Stage 1.
    Evaluates actual DART financials for ROE, EPS YoY, and EPS QoQ.
    """
    logger.info("=" * 60)
    logger.info(f"Starting STAGE 2: Financial Safety Net on {len(df_stage1)} tickers for date: {target_date or 'today'}")
    logger.info("=" * 60)

    if dart is None:
        logger.error(
            "DART API Key not found or invalid. "
            "Cannot run Stage 2 without DART API access. "
            "Set DART_API_KEY in .env file."
        )
        return pd.DataFrame()

    # Determine latest report info
    year, reprt_code, desc = get_latest_report_info(target_date=target_date)
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
        result = row.to_dict()

        # Progress logging
        if i % 10 == 0:
            logger.info(f"Processing Stage 2: {i}/{total}")

        if not corp_code:
            logger.debug(f"  {ticker}: No corp_code found")
            result['Risk_Flags'] = 'DART corp_code 없음'
            passed_fundamentals.append(result)
            continue

        try:
            metrics = evaluate_fundamentals(corp_code, year, reprt_code)
            if metrics:
                for key, val in metrics.items():
                    if val is not None or key not in result:
                        result[key] = val
                passed_fundamentals.append(result)
                name = row.get('Name', ticker)
                logger.info(
                    f"  ✓ {name}({ticker}): Risk={metrics.get('Risk_Flags', 'N/A')}"
                )
            else:
                result['Risk_Flags'] = '검증 실패'
                passed_fundamentals.append(result)
        except Exception as e:
            logger.error(f"  Error evaluating {ticker}: {e}")
            result['Risk_Flags'] = f'DART 오류: {e}'
            passed_fundamentals.append(result)

    df_passed = pd.DataFrame(passed_fundamentals)

    if not df_passed.empty:
        df_passed = df_passed.reset_index(drop=True)

    logger.info(f"STAGE 2 Completed. {len(df_passed)} tickers processed (Diagnostic mode).")
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
