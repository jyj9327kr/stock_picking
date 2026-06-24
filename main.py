import argparse
import logging
import os
from datetime import datetime

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
    
    # Save CSV
    df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    
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
        f.write(df.to_markdown(index=False))
        
    logger.info(f"Saved results to {csv_path} and {md_path}")

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
