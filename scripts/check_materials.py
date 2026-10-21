"""Run the offline preflight from a checkout without importing the web app."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/src'))

from rumi_market.preflight import main


if __name__ == '__main__':
    raise SystemExit(main())
