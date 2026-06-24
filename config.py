import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DART_API_KEY = os.getenv("DART_API_KEY", "")

# Directory configurations
BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "data" / "cache"

# Ensure cache directory exists
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# --- Stage 1: Technical & Sector Momentum ---
MA_WINDOWS = [50, 150, 200]
DISTANCE_52W_HIGH_MAX = 0.25  # Current Price is within 25% of the 52-week High

# Sector Leadership & Volume Pool
SECTOR_TRAIL_MONTHS = [1, 3]
VOLUME_TOP_N = 100
VOLUME_DAYS = 5
VOLUME_POOL_MIN_STOCKS = 2  # multiple stocks ranking in the top 100

# --- Stage 2: Financial Safety Net ---
ROE_MIN = 15.0         # >= 15%
EPS_YOY_MIN = 15.0     # >= 15%
EPS_QOQ_MIN = 0.0      # >= 0%
