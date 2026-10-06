"""The Item Process Master tab is admin only (2026-10-06); writes are admin-gated."""
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def test_tab_is_admin_only():
    """Owner, 2026-10-06: the user login neither sees nor changes the Item
    Process Master. Hidden in the nav, bounced off the URL hash, 403 on GET."""
    html = (WEB / "index.html").read_text()
    line = next(l for l in html.splitlines() if 'data-view="itemmaster"' in l)
    assert "admin-only" in line
    assert 'id="view-itemmaster"' in html
    js = (WEB / "app.js").read_text()
    assert 'v === "itemmaster" && currentRole !== "admin"' in js


def test_view_is_routed_and_writes_are_role_checked():
    js = (WEB / "app.js").read_text()
    assert '"itemmaster"' in js.split("const VIEWS")[1].split("\n")[0]
    assert 'v === "itemmaster"' in js
    body = js.split("function renderItemMaster")[1]
    assert 'currentRole === "admin"' in body


def test_no_em_dashes_in_tab_copy():
    js = (WEB / "app.js").read_text()
    section = js.split("// ===== Item Process Master =====")[1].split("// ===== end Item Process Master =====")[0]
    html = (WEB / "index.html").read_text()
    tab = html.split('id="view-itemmaster"')[1].split("</section>")[0]
    for text in (section, tab):
        assert "—" not in text
