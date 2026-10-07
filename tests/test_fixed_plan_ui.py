"""Static checks on the fixed-plan screens (web/index.html, web/app.js)."""
from pathlib import Path

HTML = Path("web/index.html").read_text()
JS = Path("web/app.js").read_text()


def test_button_is_called_optimize():
    assert '<button id="optimize-start" class="primary admin-only">Optimize</button>' in HTML


def test_optimize_warns_before_starting():
    body = JS.split("async function startOptimize()")[1].split("\nasync function ")[0]
    assert "window.confirm(" in body and "different machines" in body


def test_alerts_live_in_the_data_gaps_card_for_both_roles():
    card = HTML.split('id="data-gaps-card"')[1].split("</div>\n    </div>")[0]
    assert 'id="fixed-plan-alerts"' in card
    assert "admin-only" not in card.split('id="fixed-plan-alerts"')[0][-80:]
    assert '"fixed-plan-alerts"' in JS.split("function syncDataGapsCard()")[1][:400]


def test_done_no_longer_promises_a_search():
    body = JS.split("async function doneOptimize()")[1].split("\nasync function ")[0]
    assert "15 to" not in body and "pollDoneOptimize" not in body
    assert "Nothing new" not in body
    assert "pollDoneOptimize" not in JS


def test_result_renders_the_date_list():
    body = JS.split("function renderOptimizeResult(st)")[1].split("\nfunction ")[0]
    assert "date_changes" in body


def test_result_distinguishes_unknown_from_empty_date_list():
    body = JS.split("function renderOptimizeResult(st)")[1].split("\nfunction ")[0]
    assert "date_changes === null" in body
    assert "Could not work out which delivery dates change." in body
    assert "No delivery date changes." in body


def test_alerts_rendered_after_report_in_runplan():
    assert "renderFixedPlanAlerts(data.fixed_plan_alerts)" in JS


def test_no_em_dashes_in_new_copy():
    for fn in ("async function doneOptimize()", "async function startOptimize()",
               "function renderFixedPlanAlerts("):
        body = JS.split(fn)[1].split("\nasync function ")[0].split("\nfunction ")[0]
        assert "—" not in body, fn


def test_no_stale_deep_search_label():
    assert "Start deep search" not in HTML + JS


def test_done_explainer_does_not_promise_a_long_wait():
    seg = JS.split("Click <b>Done entering")[1][:400]
    assert "15 to 30" not in seg and "few seconds" in seg


def test_dates_changed_warning_reads_plainly():
    body = JS.split("function datesChangedWarningText(meta)")[1].split("\nfunction ")[0]
    assert "does not know about it" not in body and "reflects the change" not in body
    assert "—" not in body


def test_an_unknown_date_list_is_said_whatever_the_result():
    """Final review minor (d): null after a plan result is said even when the result
    is not an improvement (it used to be said only when improved)."""
    body = JS.split("function renderOptimizeResult(st)")[1].split("\nfunction ")[0]
    seg = body.split("st.date_changes === null")[1][:200]
    assert "if (st.improved)" not in seg
    assert "Could not work out which delivery dates change." in seg


def test_going_back_to_the_standard_plan_warns_that_jobs_may_move():
    """Final review I6: it republishes the standard plan, so jobs may change machine."""
    seg = JS.split('id="optimize-clear-btn"')[1].split("\nfunction ")[0]
    confirm = seg.split("window.confirm(")[1].split(")) return")[0]
    assert "other machines" in confirm and "—" not in confirm


# --------------------------------------------------------------------------- #
# Task 9 fix round: browser findings B1-B5.
# --------------------------------------------------------------------------- #
DONE_PHRASE = "Jobs keep their machines; only times changed."


def test_done_copy_says_one_thing_and_no_longer_promises_a_turn():
    """B3: after D6 was amended a job may give way when the one ahead is not ready,
    so no Done text may promise that jobs keep their turn or their order."""
    done = JS.split("async function doneOptimize()")[1].split("\nasync function ")[0]
    assert DONE_PHRASE in done
    assert "Jobs keep their machines; only times change." in done      # the confirm
    explainer = JS.split('id="optimize-done-status"')[1][:900]
    assert "Jobs keep their machines; only times change." in explainer
    for text in (done, explainer, Path("api/main.py").read_text()):
        assert "their turn" not in text and "their order;" not in text
        assert "and their order" not in text


def test_done_success_text_survives_the_re_render():
    """B2: runPlan re-renders Daily Entry and wiped the status line; the text is kept
    in state and rendered with the row, and set again on the new element."""
    done = JS.split("async function doneOptimize()")[1].split("\nasync function ")[0]
    after = done.split("await runPlan(false);")[1]
    assert "lastDoneStatus = " in after and '$("optimize-done-status")' in after
    assert 'id="optimize-done-status" class="status">${escapeHtml(lastDoneStatus)}' in JS


def test_optimize_result_columns_are_now_and_after_apply():
    """B4: the left column is the plan in force, not a standard plan."""
    body = JS.split("function renderOptimizeResult(st)")[1].split("\nfunction ")[0]
    assert "<th>Now</th><th>After Apply</th>" in body
    assert "Standard plan" not in body


def test_user_role_is_told_who_can_press_optimize():
    """B5: the notice says Press Optimize; the user role has no such button."""
    body = JS.split("function renderFixedPlanAlerts(alerts)")[1].split("\nfunction ")[0]
    assert 'currentRole === "user"' in body          # A0 minor 7: not while unknown
    assert "Your admin can press Optimize to fix this." in body


def test_an_old_done_status_does_not_linger():
    """Fix round 2 (B2 minor): the kept status is cleared when Done starts again and
    when the user leaves Daily Entry, so an old "Plan updated" line never lingers."""
    done = JS.split("async function doneOptimize()")[1].split("\nasync function ")[0]
    before = done.split("await runPlan(false);")[0]
    assert 'lastDoneStatus = "";' in before
    show = JS.split("function showView(v, push) {")[1].split("\nfunction ")[0]
    assert 'activeView === "entry" && v !== "entry"' in show and 'lastDoneStatus = ""' in show
