"""Run with:  python -m shops.fernfig.app   (or start everything with run_all.py)"""
import os

from shops.fernfig.catalog import CONFIG
from shops.shopkit import create_shop

app = create_shop(CONFIG, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    app.run(port=CONFIG["port"], debug=False, use_reloader=False)
