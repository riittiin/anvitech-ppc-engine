"""Put a workbook's sales orders into the book for a test.

The Excel upload endpoint is gone (stage 2). Many API tests still need a book
of orders to plan, so this does, directly in the store, what the upload used to
do with the file's SO sheet. It is a
test fixture, never an app code path."""
import io

from engine import book_store, orderbook
from engine.loaders import load_all

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def seed_orders(data: bytes) -> int:
    """Merge the workbook's SO lines into the book; returns how many were added."""
    import api.main as m
    so_lines, _ = load_all(io.BytesIO(data))
    new, updated, _ = orderbook.merge_upload(
        so_lines, book_store.load_active_orders(), book_store.load_completed_orders(),
        first_seen=m._ist_today().isoformat())
    book_store.add_orders(new + updated)
    return len(new)


class _Stored:
    """Stand-in for the old upload response (the endpoint is gone since stage 2)."""
    status_code = 200

    def json(self):
        return {"masters_updated": True, "orders_changed": 0}


def upload_and_seed(client, data: bytes, name: str = "sample.xlsx"):
    """Store the workbook's masters directly (as the removed upload did), then seed
    its orders. ``client`` is kept for call compatibility."""
    import api.main as m
    book_store.save_masters_bytes(data)
    m._MASTERS_CACHE["masters"] = None
    seed_orders(data)
    return _Stored()
