import os
import pandas as pd
import numpy as np
import FinanceDataReader as fdr
from pykrx import stock
import OpenDartReader
from datetime import datetime, timedelta
import time
import logging
import requests
from io import BytesIO
from bs4 import BeautifulSoup

from config import CACHE_DIR, DART_API_KEY

logger = logging.getLogger(__name__)

# Initialize DART reader if API key is provided and valid
dart = None
if DART_API_KEY and DART_API_KEY != "your_dart_api_key_here":
    try:
        dart = OpenDartReader(DART_API_KEY)
    except Exception as e:
        logger.error(f"Failed to initialize OpenDartReader: {e}")

# ─────────────────────────────────────────────
#  Cache helpers
# ─────────────────────────────────────────────
def get_cache_path(filename):
    return CACHE_DIR / f"{filename}.pkl"

def load_from_cache(filename, max_age_days=1):
    path = get_cache_path(filename)
    if path.exists():
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if datetime.now() - mtime < timedelta(days=max_age_days):
            try:
                return pd.read_pickle(path)
            except Exception as e:
                logger.warning(f"Failed to load cache {filename}: {e}")
    return None

def save_to_cache(data, filename):
    path = get_cache_path(filename)
    try:
        if isinstance(data, pd.DataFrame):
            data.to_pickle(path)
        else:
            pd.to_pickle(data, path)
    except Exception as e:
        logger.warning(f"Failed to save cache {filename}: {e}")

# ─────────────────────────────────────────────
#  Ticker & Name functions
# ─────────────────────────────────────────────
def get_kospi_kosdaq_tickers():
    """
    Fetches all current tickers for KOSPI and KOSDAQ using FDR.
    Returns a DataFrame with columns: [market, ticker, name, amount].
    """
    today = datetime.today().strftime('%Y%m%d')
    cache_name = f"tickers_{today}"
    cached = load_from_cache(cache_name, max_age_days=1)
    if cached is not None:
        return cached

    try:
        rows = []
        for market in ['KOSPI', 'KOSDAQ']:
            df = fdr.StockListing(market)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = str(row.get('Code', '')).zfill(6)
                    name = row.get('Name', '')
                    amount = row.get('Amount', 0)
                    volume = row.get('Volume', 0)
                    marcap = row.get('Marcap', 0)
                    rows.append({
                        'market': market,
                        'ticker': code,
                        'name': name,
                        'amount': amount,    # 거래대금
                        'volume': volume,    # 거래량
                        'Marcap': marcap,    # 시가총액
                    })

        df_tickers = pd.DataFrame(rows)
        if not df_tickers.empty:
            save_to_cache(df_tickers, cache_name)
        logger.info(f"Loaded {len(df_tickers)} tickers (FDR)")
        return df_tickers
    except Exception as e:
        logger.error(f"Error fetching ticker list: {e}")
        return pd.DataFrame()


def get_ticker_name(ticker):
    """Returns the company name for a given ticker using pykrx."""
    try:
        name = stock.get_market_ticker_name(ticker)
        return name if name else ""
    except Exception:
        return ""


# ─────────────────────────────────────────────
#  Sector (업종) mapping via KRX KIND
# ─────────────────────────────────────────────
def get_stock_sector_map():
    """
    Returns a dict mapping ticker -> sector (업종).
    Fetches from KRX KIND (kind.krx.co.kr) which provides sector data.
    Falls back to an empty dict on failure.
    """
    cache_name = f"sector_map_{datetime.today().strftime('%Y%m%d')}"
    cached = load_from_cache(cache_name, max_age_days=7)
    if cached is not None:
        return cached

    sector_map = {}

    # Strategy 1: Try FDR first (some versions have Sector column)
    try:
        df_krx = fdr.StockListing('KRX')
        if df_krx is not None and not df_krx.empty and 'Sector' in df_krx.columns:
            code_col = None
            for col_candidate in ['Code', 'Symbol', 'code', 'symbol']:
                if col_candidate in df_krx.columns:
                    code_col = col_candidate
                    break
            if code_col:
                df_valid = df_krx[df_krx['Sector'].notna()]
                sector_map = dict(zip(
                    df_valid[code_col].astype(str).str.zfill(6),
                    df_valid['Sector']
                ))
    except Exception as e:
        logger.debug(f"FDR StockListing sector fetch: {e}")

    # Strategy 2: KRX KIND - 상장회사 목록 (includes 업종)
    if not sector_map:
        logger.info("Fetching sector data from KRX KIND...")
        try:
            url = 'http://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13'
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                dfs = pd.read_html(BytesIO(resp.content), header=0, encoding='euc-kr')
                if dfs:
                    df_kind = dfs[0]
                    # Columns: 회사명, 종목코드, 업종, 주요제품, 상장일, ...
                    if '종목코드' in df_kind.columns and '업종' in df_kind.columns:
                        df_kind['종목코드'] = df_kind['종목코드'].apply(lambda x: str(x).zfill(6))
                        valid = df_kind[df_kind['업종'].notna()]
                        sector_map = dict(zip(valid['종목코드'], valid['업종']))
                        logger.info(f"KRX KIND sector map: {len(sector_map)} companies")
        except Exception as e:
            logger.warning(f"KRX KIND fetch failed: {e}")

    if sector_map:
        save_to_cache(sector_map, cache_name)
    else:
        logger.warning("Could not load sector data from any source.")
    return sector_map


def get_sector_stocks_grouped(sector_map):
    """
    Groups tickers by sector.
    Returns: dict[sector_name] -> list[ticker]
    """
    grouped = {}
    for ticker, sector in sector_map.items():
        if sector not in grouped:
            grouped[sector] = []
        grouped[sector].append(ticker)
    return grouped


# ─────────────────────────────────────────────
#  OHLCV data
# ─────────────────────────────────────────────
def get_ohlcv(ticker, start_date=None, end_date=None):
    """
    Fetches OHLCV data using FinanceDataReader.
    Default period: 600 calendar days to ensure enough data for
    200-day SMA + 252-day 52-week high calculation.
    """
    if end_date is None:
        end_date = datetime.today().strftime('%Y-%m-%d')
    if start_date is None:
        start_date = (datetime.today() - timedelta(days=600)).strftime('%Y-%m-%d')

    cache_name = f"ohlcv_{ticker}_{start_date}_{end_date}"
    cached = load_from_cache(cache_name, max_age_days=1)
    if cached is not None:
        return cached

    try:
        df = fdr.DataReader(ticker, start_date, end_date)
        if df is not None and not df.empty:
            save_to_cache(df, cache_name)
        return df
    except Exception as e:
        logger.error(f"Error fetching OHLCV for {ticker}: {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────
#  Sector performance calculation
# ─────────────────────────────────────────────
def calculate_sector_performance(sector_stocks, months=1):
    """
    Calculates the average return of stocks in a sector over N months.
    Uses a sampling approach: picks up to 10 representative stocks
    (by market cap / first in list) to avoid excessive API calls.

    Returns: float (average return) or None
    """
    if not sector_stocks:
        return None

    # Sample up to 10 stocks per sector
    sample = sector_stocks[:10]
    returns = []

    end_date = datetime.today()
    start_date = end_date - timedelta(days=months * 30)
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')

    for ticker in sample:
        try:
            df = get_ohlcv(ticker, start_str, end_str)
            if df is not None and len(df) >= 5:
                first_close = df['Close'].iloc[0]
                last_close = df['Close'].iloc[-1]
                if first_close > 0:
                    ret = (last_close - first_close) / first_close
                    returns.append(ret)
        except Exception:
            continue

    if returns:
        return np.mean(returns)
    return None


def get_market_benchmark_return(market, months=1, end_date=None):
    """
    Calculates benchmark return using a market ETF as proxy.
    KOSPI: KODEX 200 (069500), KOSDAQ: KODEX 코스닥150 (229200)
    """
    etf_map = {
        'KOSPI': '069500',   # KODEX 200
        'KOSDAQ': '229200',  # KODEX 코스닥150
    }
    etf_ticker = etf_map.get(market, '069500')

    if end_date is None:
        end_date_dt = datetime.today()
    elif isinstance(end_date, str):
        end_date_dt = datetime.strptime(end_date, '%Y-%m-%d')
    else:
        end_date_dt = end_date

    start_date_dt = end_date_dt - timedelta(days=months * 30)

    try:
        df = get_ohlcv(etf_ticker, start_date_dt.strftime('%Y-%m-%d'), end_date_dt.strftime('%Y-%m-%d'))
        if df is not None and len(df) >= 5:
            first_close = df['Close'].iloc[0]
            last_close = df['Close'].iloc[-1]
            if first_close > 0:
                return (last_close - first_close) / first_close
    except Exception:
        pass
    return 0.0


# ─────────────────────────────────────────────
#  Volume ranking (using FDR StockListing data)
# ─────────────────────────────────────────────
def get_top_volume_tickers(top_n=100):
    """
    Returns the top N tickers by trading value (거래대금).
    Uses FDR StockListing which includes today's Amount column.
    Returns a set of ticker strings.
    """
    df_tickers = get_kospi_kosdaq_tickers()
    if df_tickers is None or df_tickers.empty:
        return set()

    if 'amount' not in df_tickers.columns:
        logger.warning("No 'amount' column in ticker data for volume ranking.")
        return set()

    # Sort by trading value descending
    df_sorted = df_tickers.sort_values('amount', ascending=False)
    top_tickers = set(df_sorted.head(top_n)['ticker'].tolist())

    logger.info(f"Top {top_n} volume tickers identified ({len(top_tickers)} unique)")
    return top_tickers


# ─────────────────────────────────────────────
#  DART financial data
# ─────────────────────────────────────────────
def get_financials(corp_code, year, reprt_code='11011'):
    """
    Fetches financial statements from DART.
    reprt_code: '11011' (사업보고서), '11012' (반기보고서),
                '11013' (1분기보고서), '11014' (3분기보고서)
    """
    if dart is None:
        logger.error("DART API Key is not set.")
        return None

    cache_name = f"dart_fin_{corp_code}_{year}_{reprt_code}"
    cached = load_from_cache(cache_name, max_age_days=30)
    if cached is not None:
        return cached

    try:
        # finstate_all expects year as int
        year_int = int(year) if isinstance(year, str) else year
        df = dart.finstate_all(corp_code, year_int, reprt_code=reprt_code)
        if df is not None and not df.empty:
            save_to_cache(df, cache_name)
        time.sleep(0.1)  # DART limit mitigation
        return df
    except Exception as e:
        logger.error(f"Error fetching DART financials for {corp_code} {year} {reprt_code}: {e}")
        return None


def get_naver_financial_info(ticker):
    """
    Fetches Revenue, Operating Income, ROE, PER, PBR from Naver Finance.
    Saves to and loads from local cache (max age 1 day) to avoid hitting Naver repeatedly.
    """
    cache_name = f"naver_fin_{ticker}_{datetime.today().strftime('%Y%m%d')}"
    cached = load_from_cache(cache_name, max_age_days=1)
    if cached is not None:
        return cached

    result = {
        'Revenue': None,
        'Operating_Income': None,
        'ROE': None,
        'PER': None,
        'PBR': None
    }
    
    url = f"https://finance.naver.com/item/main.naver?code={ticker}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return result
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 1. PER, PBR from the right panel
        per_elem = soup.find(id='_per')
        if per_elem:
            try:
                result['PER'] = float(per_elem.text.replace(',', '').strip())
            except ValueError:
                pass
                
        pbr_elem = soup.find(id='_pbr')
        if pbr_elem:
            try:
                result['PBR'] = float(pbr_elem.text.replace(',', '').strip())
            except ValueError:
                pass
        
        # 2. Revenue, Operating Income, ROE from Corporate Analysis Table
        table = soup.select_one('.section.cop_analysis table')
        if table:
            rows = table.select('tr')
            if len(rows) > 1:
                headers_row = [th.get_text(strip=True) for th in rows[1].select('th, td')]
                
                # Find the most recent quarter (index >= 4) that does not end with '(E)'
                target_col_idx = -1
                for idx in range(len(headers_row) - 1, 3, -1):
                    col_name = headers_row[idx]
                    if '(E)' not in col_name and col_name:
                        target_col_idx = idx
                        break
                
                # Fallback to the most recent annual (index 0 to 3) without (E)
                if target_col_idx == -1:
                    for idx in range(3, -1, -1):
                        col_name = headers_row[idx]
                        if '(E)' not in col_name and col_name:
                            target_col_idx = idx
                            break
                            
                if target_col_idx != -1:
                    data_col_idx = target_col_idx + 1
                    
                    for r in rows[2:]:
                        tds = [td.get_text(strip=True) for td in r.select('th, td')]
                        if not tds:
                            continue
                        row_title = tds[0]
                        
                        if '매출액' in row_title and len(tds) > data_col_idx:
                            val_str = tds[data_col_idx].replace(',', '').strip()
                            if val_str and val_str != '-':
                                try:
                                    # Revenue in 100M KRW (억 원)
                                    result['Revenue'] = float(val_str)
                                except ValueError:
                                    pass
                        elif '영업이익' in row_title and '영업이익률' not in row_title and len(tds) > data_col_idx:
                            val_str = tds[data_col_idx].replace(',', '').strip()
                            if val_str and val_str != '-':
                                try:
                                    # Operating Income in 100M KRW (억 원)
                                    result['Operating_Income'] = float(val_str)
                                except ValueError:
                                    pass
                        elif 'ROE' in row_title and len(tds) > data_col_idx:
                            val_str = tds[data_col_idx].replace(',', '').strip()
                            if val_str and val_str != '-':
                                try:
                                    result['ROE'] = float(val_str)
                                except ValueError:
                                    pass
                                    
                        # Fallback for PER and PBR if not found in side panel
                        if result['PER'] is None and 'PER' in row_title and len(tds) > data_col_idx:
                            val_str = tds[data_col_idx].replace(',', '').strip()
                            if val_str and val_str != '-':
                                try:
                                    result['PER'] = float(val_str)
                                except ValueError:
                                    pass
                        if result['PBR'] is None and 'PBR' in row_title and len(tds) > data_col_idx:
                            val_str = tds[data_col_idx].replace(',', '').strip()
                            if val_str and val_str != '-':
                                try:
                                    result['PBR'] = float(val_str)
                                except ValueError:
                                    pass
    except Exception as e:
        logger.error(f"Error scraping Naver Finance for {ticker}: {e}")
        
    save_to_cache(result, cache_name)
    return result

