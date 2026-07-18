# UI/UX redesign — clean, simple, idiot-proof

**Date:** 2026-07-19 · **Status:** AWAITING OWNER APPROVAL ·
**Hard constraint:** the backend does not change — same endpoints, same request/
response shapes, same role gating, same features. Frontend files only
(`web/index.html`, `web/app.js`, `web/style.css`).

## The problem (owner's words + confirmed in code)

The page is one long vertical dump: toolbar → optimize panel → settings →
report → absences list → the full operator master → and only THEN the actual
work tabs. First-time users land on master data instead of their work. No
visual hierarchy, minimal styling (394 lines of CSS), banners scattered.

## The redesign in one sentence

A top navigation with six clear destinations, a one-line status strip that
always says what the system is doing, and everything administrative tucked
into Settings — so a worker sees exactly one thing: their work.

## Layout (wireframe)

```
┌────────────────────────────────────────────────────────────────┐
│  ANVITECH PPC          Orders · Schedule · Gantt · Daily Entry │
│                        · Analytics · Settings          [Logout]│
├────────────────────────────────────────────────────────────────┤
│  ● 54 orders · 42 late · Plan follows today (19-07-2026)       │
│  Next optimization: Mon 11:00 · Next shift rotation: Fri 24-07 │
│  ▸ last note: "Checked 10:56 — current plan still best."       │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│                    [ the ONE selected view ]                   │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

## The six destinations

1. **Orders** (landing view, both roles) — the order book table as today, plus
   the Upload control at the top (admin; it belongs where orders arrive).
   Lane badges, promised/expected columns unchanged. Empty state: "Upload
   your Excel to begin."
2. **Schedule** (both) — the Rule 6 allocation table + its CSV downloads
   (full + shift-wise), exactly today's data.
3. **Gantt** (both) — unchanged Gantt, full width.
4. **Daily Entry** (both) — Capture Actuals: the entry form (operator +
   shift dropdowns, qty, downtime) with the saved-entries list and rollback.
   Big, obvious, finger-friendly — this is the floor's page.
5. **Analytics** (both) — the utilization/bottleneck tab as today.
6. **Settings** (admin only in nav; hidden for users) — sub-sections on one
   scrollable page with clear cards: Plan settings (incl. "Start from
   today") · Optimize (the panel + banners) · Operators & shifts ·
   Absences · Efficiency report (month picker + download). Users who need
   read-only operator/absence visibility get a compact read-only "Team"
   card at the bottom of Daily Entry instead (list only, no controls) — so
   nothing users could previously see is lost.

## The status strip (always visible, both roles)

One calm line replacing today's scattered banners: order count · late count ·
plan basis ("follows today", resolved date) · next scheduled optimization ·
next rotation · the latest auto-note (collapsible). Warning states (staleness
banner, unstaffed hours) appear here as a colored chip that expands on click.
All fed by existing response fields — zero new endpoints.

## Visual system (self-contained; no frameworks, no external assets — CSP-safe)

- System font stack; 3 sizes (14/16/20) + one display size for headings.
- CSS variables: ink #1a1d21, paper #f6f7f9, card #ffffff, accent #1f6feb,
  ok #1a7f37, warn #b35900, danger #b42318, line #e4e7eb.
- Cards with 8px radius + subtle border (no shadows-everywhere), 8px spacing
  grid, tables with sticky headers + zebra rows + right-aligned numbers.
- Buttons: one primary (accent, verbs: "Upload & merge", "Start
  optimization", "Save entry"), ghost secondary, red only for destructive.
- Badges for lanes/status (Pending/Running/Complete, Open/Committed/Urgent).
- Every section: a title + ONE plain-language explainer sentence.
- Responsive down to tablet width (the Gantt/tables scroll inside their
  cards, never the page sideways).

## Idiot-proofing rules

- One view at a time; the nav highlights where you are.
- Every empty state names the next action.
- Dates DD-MM-YYYY everywhere (existing convention).
- Danger (delete orders, clear book) stays behind the existing password
  modal, grouped at the bottom of Settings, visually separated.
- The user role never sees empty admin shells — admin-only cards are absent
  from their DOM (existing gating pattern, applied consistently).

## Implementation approach (risk containment)

- **Keep `app.js` logic and every element ID intact.** The redesign
  restructures `index.html` (nav + view wrappers around the existing
  sections), adds a ~40-line hash router (show/hide views, restore last
  view), and rewrites `style.css` as the design system. The existing tab
  machinery for trace tabs is absorbed into the new nav (same render
  functions, new mount points).
- No backend edits of any kind; no new endpoints; no changed payloads.
- Golden/pytest untouched by construction; the API test suite must stay
  green (it will — no api changes).
- REQUIRED verification: full browser walkthrough of both roles on the
  sample book (every view, every action incl. upload, punch, absence,
  operator edit, optimize preview, efficiency download), plus a visual
  check on a narrow window.

## Out of scope (explicitly)

Dark mode; mobile-phone layouts; charts beyond existing Analytics; any
change to wording of stored data; framework adoption.
