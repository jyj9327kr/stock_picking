import argparse
import logging
import os
import glob
import json
from datetime import datetime
import pandas as pd
import numpy as np

from pipeline.stage1_momentum import run_stage1_screening
from pipeline.stage2_fundamentals import (
    run_stage2_screening,
    batch_update_financials,
    get_latest_report_info,
)
from utils.data_fetcher import get_kospi_kosdaq_tickers
from config import BASE_DIR

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def save_results(df, filename_prefix):
    """Saves DataFrame to CSV and Markdown, adding top 3 sectors summary."""
    if df is None or df.empty:
        logger.info(f"No results to save for {filename_prefix}")
        return
        
    out_dir = BASE_DIR / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"{filename_prefix}_{timestamp}.csv"
    md_path = out_dir / f"{filename_prefix}_{timestamp}.md"
    
    # 2) Filter and reorder columns according to user request
    target_columns = [
        'Ticker', 'Name', 'Sector', 'Current_Price', 'Marcap', 
        'Revenue', 'Operating_Income', 'ROE', 'PER', 'PBR', 'Excess_Return_3M'
    ]
    
    df_to_save = df.copy()
    for col in target_columns:
        if col not in df_to_save.columns:
            df_to_save[col] = None
            
    df_to_save = df_to_save[target_columns]
    
    # Save CSV
    df_to_save.to_csv(csv_path, index=False, encoding='utf-8-sig')
    
    # If it's weekly watchlist or Stage 1 weekly, also save to weekly_results directory for Git / GitHub Pages
    if filename_prefix in ["Stage1_Weekly", "Watchlist_Weekly"]:
        weekly_dir = BASE_DIR / "weekly_results"
        weekly_dir.mkdir(parents=True, exist_ok=True)
        weekly_csv_path = weekly_dir / f"{filename_prefix}_{timestamp}.csv"
        df_to_save.to_csv(weekly_csv_path, index=False, encoding='utf-8-sig')
        logger.info(f"Saved duplicate weekly result to {weekly_csv_path}")
    
    # Calculate top 3 sectors if Sector column exists
    top_sectors_summary = ""
    sector_counts = None
    if 'Sector' in df.columns:
        valid_sectors = df[df['Sector'].notna() & (df['Sector'] != 'N/A') & (df['Sector'] != '')]
        if not valid_sectors.empty:
            sector_counts = valid_sectors['Sector'].value_counts()
            top_sectors = sector_counts.head(3)
            top_sectors_summary = ", ".join([f"{sec} ({count}개)" for sec, count in top_sectors.items()])
            logger.info(f"[{filename_prefix}] Top 3 Sectors: {top_sectors_summary}")
    
    # Save Markdown
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(f"# {filename_prefix} Results ({timestamp})\n\n")
        if top_sectors_summary and sector_counts is not None:
            f.write(f"### 📈 Top 3 Sectors\n")
            for i, (sec, count) in enumerate(sector_counts.head(3).items()):
                f.write(f"{i+1}. **{sec}** ({count}개)\n")
            f.write("\n---\n\n")
        f.write(df_to_save.to_markdown(index=False))
        
    logger.info(f"Saved results to {csv_path} and {md_path}")


def generate_github_pages_data():
    """
    Analyzes the weekly CSV files in 'weekly_results' directory.
    Identifies the most recent CSV file and counts how many times 
    each stock has appeared in the last 4 weekly CSV files.
    Saves the aggregated data to 'weekly_results/data.json' for GitHub Pages.
    """
    logger.info("Generating GitHub Pages dataset from weekly results...")
    weekly_dir = BASE_DIR / "weekly_results"
    if not weekly_dir.exists():
        logger.warning("No weekly_results directory found.")
        return
        
    # Try to find Watchlist_Weekly CSV files first
    csv_pattern = str(weekly_dir / "Watchlist_Weekly_*.csv")
    csv_files = glob.glob(csv_pattern)
    prefix = "Watchlist_Weekly"
    
    # If none found, fallback to Stage1_Weekly CSV files
    if not csv_files:
        logger.info("No Watchlist_Weekly files found. Falling back to Stage1_Weekly.")
        csv_pattern = str(weekly_dir / "Stage1_Weekly_*.csv")
        csv_files = glob.glob(csv_pattern)
        prefix = "Stage1_Weekly"
        
    if not csv_files:
        logger.warning("No weekly result CSV files found in weekly_results.")
        return
        
    # Sort files by filename descending (recent first)
    csv_files.sort(reverse=True)
    
    # We take up to 4 most recent files
    target_files = csv_files[:4]
    logger.info(f"Analyzing {len(target_files)} recent weekly files: {[os.path.basename(f) for f in target_files]}")
    
    # Read the most recent file as the baseline
    latest_file = target_files[0]
    try:
        df_latest = pd.read_csv(latest_file)
    except Exception as e:
        logger.error(f"Failed to read latest weekly file {latest_file}: {e}")
        return
        
    if df_latest.empty:
        logger.warning("Latest weekly file is empty.")
        data_to_save = {
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stocks": []
        }
        with open(weekly_dir / "data.json", "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
        return

    # Treat Ticker as padded string
    df_latest['Ticker'] = df_latest['Ticker'].astype(str).str.zfill(6)
    
    # Read older files
    historical_tickers = []
    for hist_file in target_files[1:]:
        try:
            df_hist = pd.read_csv(hist_file)
            if not df_hist.empty:
                df_hist['Ticker'] = df_hist['Ticker'].astype(str).str.zfill(6)
                historical_tickers.append(set(df_hist['Ticker'].tolist()))
        except Exception as e:
            logger.warning(f"Failed to read historical file {hist_file}: {e}")
            
    stocks_list = []
    for _, row in df_latest.iterrows():
        ticker = row['Ticker']
        appearance_count = 1
        
        for hist_set in historical_tickers:
            if ticker in hist_set:
                appearance_count += 1
                
        stock_data = row.to_dict()
        
        # Clean NaN/Infinity for valid JSON output
        for k, v in stock_data.items():
            if pd.isna(v):
                stock_data[k] = None
            elif isinstance(v, (int, float)) and np.isinf(v):
                stock_data[k] = None
                
        stock_data['Appearance_Count'] = appearance_count
        stocks_list.append(stock_data)
        
    data_to_save = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stocks": stocks_list
    }
    
    json_path = weekly_dir / "data.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data_to_save, f, ensure_ascii=False, indent=2)
        
    logger.info(f"Successfully generated Pages data: {json_path}")


def run_daily():
    """Daily (Post-Market): Execute Stage 1 to track emerging breakouts."""
    logger.info("=" * 60)
    logger.info("DAILY PIPELINE: Stage 1 Only")
    logger.info("=" * 60)
    
    df_stage1 = run_stage1_screening()
    save_results(df_stage1, "Stage1_Daily")
    
    if not df_stage1.empty:
        logger.info(f"\n--- Daily Screening Summary ---")
        logger.info(f"Total passed: {len(df_stage1)} tickers")
        # Show top 10 closest to 52W high
        top10 = df_stage1.head(10)
        for _, row in top10.iterrows():
            name = row.get('Name', row['Ticker'])
            dist = row.get('Distance_52W_High', 0)
            price = row.get('Current_Price', 0)
            logger.info(f"  {name}({row['Ticker']}): Price={price:,.0f}, Dist52WH={dist:.2%}")

def run_weekly():
    """Weekly (Weekend): Full Stage 1 + Stage 2 pipeline for Watchlist generation."""
    logger.info("=" * 60)
    logger.info("WEEKLY PIPELINE: Stage 1 + Stage 2")
    logger.info("=" * 60)
    
    # Stage 1
    df_stage1 = run_stage1_screening()
    save_results(df_stage1, "Stage1_Weekly")
    
    if df_stage1.empty:
        logger.info("Stage 1 yielded no results, skipping Stage 2.")
        return
    
    # Limit to top 50 before Stage 2 to save DART API calls
    # Sort by Distance_52W_High (closest to high first)
    df_stage1_top = df_stage1.sort_values(by='Distance_52W_High').head(50)
    logger.info(f"Passing top {len(df_stage1_top)} tickers to Stage 2 (out of {len(df_stage1)} from Stage 1)")
    
    # Stage 2
    df_stage2 = run_stage2_screening(df_stage1_top)
    save_results(df_stage2, "Watchlist_Weekly")
    generate_github_pages_data()
    
    if not df_stage2.empty:
        logger.info(f"\n--- Weekly Watchlist Summary ---")
        logger.info(f"Final watchlist: {len(df_stage2)} tickers")
        for _, row in df_stage2.iterrows():
            name = row.get('Name', row['Ticker'])
            roe = row.get('ROE', 0)
            yoy = row.get('EPS_YoY', 0)
            qoq = row.get('EPS_QoQ', 0)
            logger.info(
                f"  {name}({row['Ticker']}): "
                f"ROE={roe:.1f}%, EPS_YoY={yoy:.1f}%, EPS_QoQ={qoq:.1f}%"
            )

def run_quarterly():
    """
    Quarterly (Earnings Season): Mass update DART financials.
    Fetches and caches financial data for all listed companies.
    Uses batch processing to respect DART API daily limit (10,000 calls).
    """
    logger.info("=" * 60)
    logger.info("QUARTERLY PIPELINE: Mass Financial Data Update")
    logger.info("=" * 60)
    
    # Get all tickers
    df_tickers = get_kospi_kosdaq_tickers()
    if df_tickers is None or df_tickers.empty:
        logger.error("Could not fetch ticker list for quarterly update.")
        return
    
    all_tickers = df_tickers['ticker'].tolist()
    total = len(all_tickers)
    logger.info(f"Total listed companies: {total}")
    
    # Determine which reports to fetch
    year, reprt_code, desc = get_latest_report_info()
    logger.info(f"Target report: {year}년 {desc} (reprt_code={reprt_code})")
    
    # DART API has a 10,000 daily call limit.
    # Each ticker needs ~2-3 API calls (finstate_all + possibly prev quarter for QoQ).
    # So we process in batches of ~2000 tickers per run (≈6000 API calls, leaving buffer).
    BATCH_SIZE = 2000
    
    if total > BATCH_SIZE:
        logger.warning(
            f"Total tickers ({total}) exceeds batch size ({BATCH_SIZE}). "
            f"Processing first {BATCH_SIZE} tickers. "
            f"Run again to process remaining tickers (cached data will be skipped)."
        )
    
    batch_update_financials(all_tickers, year, reprt_code, batch_size=BATCH_SIZE)
    
    logger.info("Quarterly update complete. Cached data refreshed.")
    logger.info("Run 'weekly' schedule next to generate an updated watchlist.")

def main():
    parser = argparse.ArgumentParser(description="Korean Market Quant Trading Pipeline")
    parser.add_argument(
        '--schedule', type=str,
        choices=['daily', 'weekly', 'quarterly'],
        default='weekly',
        help=(
            "Execution schedule. "
            "'daily' runs only Stage 1 (Technical & Sector Momentum). "
            "'weekly' runs Stage 1 + Stage 2 (Full pipeline with Watchlist). "
            "'quarterly' runs mass DART financial data update."
        )
    )
    args = parser.parse_args()
    
    logger.info(f"Pipeline execution started: {args.schedule.upper()} ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
    
    if args.schedule == 'daily':
        run_daily()
    elif args.schedule == 'weekly':
        run_weekly()
    elif args.schedule == 'quarterly':
        run_quarterly()
    
    logger.info(f"Pipeline execution finished: {args.schedule.upper()}")

if __name__ == "__main__":
    main()
