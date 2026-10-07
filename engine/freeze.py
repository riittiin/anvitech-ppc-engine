"""Pure freeze logic (reporting/derivation only — never mutates a plan).

Two pure functions:
  - ``schedule_projection(schedule)`` — the applied plan's per-op assignment, the durable
    record of "the plan the floor is following" (machine + operator + time per op).
  - ``compute_frozen_set(applied_rows, so_lines, good_by_step, masters)`` — from that
    record + the punches, the in-progress ops to FREEZE (machine/operator from the plan,
    remaining qty from the punches). See the 2026-07-29 spec.
"""
from __future__ import annotations
from engine.loaders import normalize_process_name as _norm
from engine.new_engine import OFF_LANES as _OS_LANES


def schedule_projection(schedule) -> list[dict]:
    """One row per real (machine) operation in the applied plan. OS/off-lane entries are
    skipped (no in-house machine to pin)."""
    rows = []
    for e in schedule:
        if e.machine in _OS_LANES:
            continue
        rows.append({
            "batch_id": e.batch_id,
            "item_code": e.item_code,
            "process_seq": e.process_seq,
            "process_name": e.process_name,
            "machine": e.machine,
            "operator": e.operator or "",
            "start": e.start.isoformat(timespec="seconds"),
            "end": e.end.isoformat(timespec="seconds"),
            # Whose pieces are on this bar: a batch whose lines are at different
            # stages is one bar per part, and each line's in-progress work must be
            # pinned to ITS part (the resumed pieces, or the rest), never the other.
            "so_refs": list(getattr(e, "piece_refs", None) or e.so_refs or []),
            # The order the engine placed it in: a repair places jobs in this order
            # so each keeps the people the published plan gave it (fixed plan).
            "placed": int(getattr(e, "placed", 0) or 0),
            # Who ran it, stretch by stretch: a repair keeps the same person on the
            # same stretch where they are still allowed (fixed plan, D7).
            "staff": [[s.isoformat(timespec="seconds"), en.isoformat(timespec="seconds"), n or ""]
                      for s, en, n in (getattr(e, "op_segments", None) or [])],
        })
    return rows


def compute_frozen_set(applied_rows, so_lines, good_by_step, masters) -> list[dict]:
    """Frozen (in-progress) ops: partially-punched steps (good>0 and remaining>0),
    with machine + operator looked up from the applied plan BY STEP NAME (never by
    sequence number, which an edit to the routing can shift). Steps not present in the
    applied plan, or whose applied machine is OS/off-lane, are not frozen.

    ``remaining_qty`` here is ONE SO line's remaining at that step — it decides
    WHETHER the step is in progress, and nothing else. It is NOT how much the
    scheduler runs: Rule 1 may club several SO lines into the batch that owns the
    operation, so the quantity comes from the batch at plan time
    (``new_engine._ppc_frozen`` reads ``Order.process_remaining``). Using this number
    as the op's qty is the 2026-08-11 live bug — a part-finished SO clubbed with an
    untouched one scheduled 88 pieces where the batch owed 369, and the missing 281
    appeared in no plan at all."""
    # Index applied rows by (item_code, normalised process NAME), never by seq: the
    # routing is editable (Item Process Master, 2026-10-05), so removing a step ahead
    # of a running one shifts its seq, and a seq lookup then reads ANOTHER step's row
    # (its machine and operator) for the step on the floor. The name is what the
    # punches key on too. A row with no name (none is written without one) is skipped.
    by_item_name: dict[tuple[str, str], list[dict]] = {}
    for r in applied_rows or []:
        if r.get("process_name"):
            by_item_name.setdefault((r["item_code"], _norm(r["process_name"])), []).append(r)

    out = []
    for line in so_lines:
        routing = masters.routings.get(line.item_code)
        if routing is None:
            continue
        pq = line.process_qty or {}
        for op in routing.processes:
            nkey = _norm(op.name)
            remaining = int(round(float(pq.get(nkey, 0))))
            good = int(round(float(good_by_step.get((line.so_no, line.item_code, nkey), 0))))
            if good <= 0 or remaining <= 0:
                continue  # not started, or fully done → not frozen
            # Machine/operator from the applied plan row covering this SO for this op.
            # Rows at this step's own seq first, so an unedited routing reads exactly
            # the row it always did.
            cand = sorted(by_item_name.get((line.item_code, nkey), []),
                          key=lambda r: r.get("process_seq") != op.seq)
            row = next((r for r in cand if line.so_no in (r.get("so_refs") or [])), None)
            if row is None or row["machine"] in _OS_LANES:
                continue  # not in last plan / outsourced → not frozen
            out.append({
                "so_no": line.so_no, "item_code": line.item_code,
                "process": op.name, "op_seq": op.seq,
                "machine": row["machine"], "operator": row.get("operator", "") or "",
                "remaining_qty": remaining, "prev_start": row["start"],
                # When this step was due to FINISH in the applied plan. Read by
                # new_engine._ppc_frozen to decide whether a maintenance break on the
                # pinned machine overlaps this step's window (2026-08-31 spec).
                "prev_end": row["end"],
            })
            # Moved to this machine since it was last worked (`mark_moved`) and not
            # punched since: it resumes with a setup (2026-08-31 rule).
            mv = (row.get("moved") or {}).get(line.so_no)
            if mv and mv.get("from") not in (None, row["machine"]) and mv.get("good") == good:
                out[-1]["setup_from"] = mv["from"]
    return out


def mark_moved(rows, old_frozen, good_by_step) -> list[dict]:
    """Copies of a plan's projection ``rows``, about to be published, where a step
    that is half finished (in ``old_frozen``, the frozen set on file, built from the
    plan published before) now sits on a DIFFERENT machine from the one it was last
    worked on: the row records, per SO line, that machine and the good count at this
    moment (``"moved": {so: {"from": machine, "good": n}}``). ``compute_frozen_set``
    turns that into ``setup_from`` while the count is unchanged, so the job pays its
    setup on the new machine until it is punched again (it has been set up then).

    "Last worked on" is the old frozen row's ``setup_from`` when it still owes a setup
    (published again before anyone worked it), else its machine. The machine named on
    a punch would be the direct signal, but it is optional and mostly blank."""
    last = {}
    for f in old_frozen or []:
        last[(f.get("so_no"), f.get("item_code"), _norm(f.get("process") or ""))] = (
            f.get("setup_from") or f.get("machine"))
    out = []
    for r in rows or []:
        moved = {}
        for so in r.get("so_refs") or []:
            k = (so, r.get("item_code"), _norm(r.get("process_name") or ""))
            was = last.get(k)
            if was and was != r.get("machine"):
                moved[so] = {"from": was, "good": int(round(float(good_by_step.get(k, 0))))}
        out.append({**r, "moved": moved} if moved else dict(r))
    return out
