"""Put a workbook's sales orders into the book for a test.

Since 2026-10-03 ``POST /upload`` carries MASTERS ONLY and never touches the
order book (owner's rule: orders come in through Add New Orders). Many API
tests still need a book of orders to plan, so this does, directly in the store,
exactly what the upload endpoint used to do with the file's SO sheet. It is a
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


def upload_and_seed(client, data: bytes, name: str = "sample.xlsx"):
    """Upload the workbook's masters through the API, then seed its orders."""
    r = client.post("/upload", files={"file": (name, data, XLSX_MIME)})
    if r.status_code == 200:
        seed_orders(data)
    return r
