"""Build a throwaway LOCAL store from one of the owner's workbooks (no live store).

Writes: the masters workbook bytes, every SO line of the workbook's SO sheet as an
active order (the Excel upload is masters-only since 2026-10-03, so orders are put
in the store directly, exactly as the earlier verification harnesses did), and a
production-like plan config (new engine, operator logic on, overlap 88,
consolidation window 1 day). Nothing else; the app seeds operators / item master /
machines from the workbook on first use.

Usage: .venv/bin/python -B build_store.py <workbook.xlsx> <out_store_dir>
"""
import io, json, os, sys
os.environ["STORE_DIR"] = sys.argv[2]
for k in ("MONGODB_URI", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
    os.environ.pop(k, None)
sys.path.insert(0, os.getcwd())
from engine import loaders, book_store
from engine.config import Config
from engine.models import Order

raw = open(sys.argv[1], "rb").read()
book_store.save_masters_bytes(raw)
so_lines, masters = loaders.load_all(io.BytesIO(raw))
orders = {}
for l in so_lines:
    o = Order(so_no=l.so_no, item_code=l.item_code, item_name=l.item_name,
              ordered_qty=float(l.qty), delivery_date=l.delivery_date)
    orders[o.key] = o
book_store.add_orders(orders.values())
cfg = Config(scheduler="new", apply_operator_logic=True, overlap_percent=88,
             consolidation_window_days=1)
book_store.save_plan_config(json.dumps(cfg.to_dict()))
print(f"store {sys.argv[2]}: {len(orders)} orders, {len(masters.machines)} machines")
