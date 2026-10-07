"""Daily Entry: every field carries a plain line saying what to type, and the
shift's minutes are fixed by the shift (owner, 2026-10-07). Add New Orders
pre-fills the SO prefix."""
from pathlib import Path

JS = Path("web/app.js").read_text()
FORM = JS.split("function actualsFormHtml()")[1].split("\nfunction ")[0].split("\n// ")[0]

HINTS = {
    "Date": "The day this work was done.",
    "Shift": "Which shift did the work.",
    "Operator": "Who ran the machine. Leave empty for outside (OS) steps.",
    "SO No": "Which order this work is for. Pick the SO first.",
    "Item Code": "Which item of that SO. Pick it after the SO.",
    "Item Name": "The item's name. Already filled in.",
    "Process": "Which step of the job you are entering.",
    "Machine": "The machine the work was done on.",
    "Cycle Time (min per piece)": "Minutes for one piece. Already filled in.",
    "Minutes Available in Shift": "The working minutes of this shift. Already filled in.",
    "Qty Produced (good pieces)": "Number of good pieces made.",
    "Qty Rejected (bad pieces)": "Number of bad pieces.",
    "Standard Setting Time (min)": ("How long a setup on this machine normally takes. "
                                    "The default is 90 minutes. Change it if this job's "
                                    "setup takes more or less time."),
    "Actual Setting Time (min)": ("How many minutes the setup actually took today. Enter 0 "
                                  "if no setup was done (the job continued from the last shift)."),
    "No Power (min)": "Minutes the machine stopped because there was no power.",
    "No Operator (min)": "Minutes the machine stood because nobody was running it.",
    "Tool Problem (min)": "Minutes lost because of a tool problem.",
    "Machine Breakdown (min)": "Minutes the machine was broken down.",
    "No Load (min)": "Minutes the machine had no job to run.",
    "Other Work (min)": "Minutes the operator spent on other work instead of this job.",
    "Remarks": "Anything else to note.",
}


def test_every_field_has_its_plain_line():
    for label, hint in HINTS.items():
        assert f'"{label}"' in FORM, label
        assert hint in FORM, (label, hint)


def test_no_em_dashes_in_the_form():
    assert "—" not in FORM


def test_shift_minutes_cannot_be_changed():
    tag = FORM.split('id="a-mins"')[1].split(">")[0]
    assert "readonly" in tag


def test_a_new_order_line_starts_with_the_prefix():
    body = JS.split("function addNewOrderLine()")[1].split("\nfunction ")[0]
    assert "so_no: noSoPrefix" in body
    assert "noSoPrefix = body.so_prefix" in JS
