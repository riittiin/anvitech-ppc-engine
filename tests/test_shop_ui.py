"""Settings > Machines and Weekly off and holidays cards (shop masters, stage 2)."""
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def test_cards_exist_and_are_visible_to_both_roles():
    html = (WEB / "index.html").read_text()
    for cid in ("machines-card", "holidays-card"):
        line = next(l for l in html.splitlines() if f'id="{cid}"' in l)
        assert "admin-only" not in line
    assert html.index('id="machines-card"') < html.index('id="operators-header"')
    assert html.index('id="holidays-card"') < html.index('id="operators-header"')


def test_writes_are_role_checked_and_copy_is_clean():
    js = (WEB / "app.js").read_text()
    section = js.split("// ===== Machines and holidays =====")[1].split("// ===== end Machines and holidays =====")[0]
    assert 'currentRole === "admin"' in section
    assert "—" not in section
    for path in ('"/machines"', '"/machines/delete"', '"/holidays"', '"/holidays/delete"'):
        assert path in section


def test_boot_loads_both_cards_and_copy_is_clean():
    js = (WEB / "app.js").read_text()
    boot = js.split("(async function boot()")[1]
    for call in ("wireShopMasters()", "loadMachines()", "loadHolidays()"):
        assert call in boot
    html = (WEB / "index.html").read_text()
    for cid in ("machines-card", "holidays-card"):
        start = html.index(f'id="{cid}"')
        card = html[start:html.index('<div class="card', start + 1)]
        assert "—" not in card
