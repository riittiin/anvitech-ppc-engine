# Going live on the PPC system, Friday 7 August 2026

A cutover plan for a floor that is already running. Nothing here stops a machine
or abandons a part-finished job.

---

## Why Thursday is the right day to do this

Thursday is the weekly off. **Production stands still for a full day.** That is the
only moment in the week when the floor's state is not a moving target, so the
system can be loaded with an accurate picture and it stays accurate.

It also lines up with the software on its own: once Wednesday's production is
entered, the plan automatically starts from **Friday**, because it skips Thursday
as a non-working day. Nobody has to set a date by hand.

Three steps: **take a snapshot Wednesday night, load it Thursday, follow it Friday.**

---

## Step 1 — Wednesday night (tonight), shift 2

**Who:** the shift-2 supervisor. **Time:** 15 minutes at end of shift.

Write down, on paper, for **every job now on a machine**:

| SO number | Item code | Which step it is on | Good pieces finished at **each** step so far |
|---|---|---|---|

The last column matters more than it looks. If a job has cleared bandsaw and is
half way through CNC 1st side, record **both**: bandsaw 500 done, CNC 1st side 300
done. Not just the step it is sitting on. The reason is in Step 3.

Nothing goes into the software tonight. Tonight is only about capturing the truth
before the holiday. **Work normally, finish the shift normally.**

---

## Step 2 — Thursday morning: set up the people

**Who:** the owner, on the computer. **Time:** about 30 minutes. **No production is
running, so nothing changes underneath you.**

Do this **before** Step 3. The order is not optional; see the warning at the end.

1. **Upload the current sales-order Excel** on the Orders tab. Check the order
   count looks right.
2. **Settings → Operators & shifts.** For every person:
   - correct name,
   - the machines they actually run, picked from the dropdown,
   - **their shift.**
3. **Read every row of the Shift column carefully.** Shifts no longer swap
   automatically. Whatever you set here is what that person works **every week**
   until you change it. Some rows may currently show whatever the old automatic
   swap left behind, which may not be where you want them.
4. **Settings → Operator absences.** Mark anyone you already know is away next
   week.

---

## Step 3 — Thursday: enter the work already in progress

**Who:** the owner, with the paper from Wednesday night. **Time:** 30 to 60 minutes
depending on how many jobs are open.

On the **Daily Entry** tab, for each job on the paper, enter the finished pieces at
**every step from the first one onward**, dated **Wednesday**.

### The one rule that will otherwise stop you

The system will not let you record work at a step until the step **before** it has
been recorded. This is deliberate: it is what stops the plan from scheduling
pieces that do not physically exist yet.

So for a job at CNC 1st side:

- ✅ Enter `Bandsaw OS = 500`, then `CNC FIRST SIDE = 300`.
- ❌ Entering `CNC FIRST SIDE = 300` on its own is refused, with a message saying
  only 0 pieces have cleared Bandsaw OS.

**Outside (OS) steps must be entered too**, even though they need no operator name.
If you skip them, everything after them stays blocked.

Quantities add up across days, so entering 500 in one go is the same as 200 plus
300. Enter what has genuinely been finished, not what is loaded on the machine.

### Then check your work

Open the **Orders** tab and read the **Remaining** column. It should match what
your floor actually still has to make. **Fix any mistakes now**, on Thursday. Once
Friday's first entry is saved, Wednesday's entries lock and can no longer be
undone from that screen.

---

## Step 4 — Thursday afternoon: build the plan

1. Press **Start deep search** on the Schedule tab. It takes 15 to 30 minutes. You
   can leave the screen open or come back.
2. When it finishes, read the result, then press **Apply**. **Nothing changes until
   you press Apply.**
3. Download and print:
   - **Machine-wise schedule** — one sheet per machine, post it at the machine.
   - **Shift-wise schedule** — hand to each shift supervisor.

Expect the plan to say some orders finish late. That is the system being honest
about a workload the crew cannot absorb, not a fault. Use the delay report if a
director asks why a specific order is late.

---

## Step 5 — Friday: run the shop from it

**Shift 1 start:** hand out the printed sheets. Operators work the machine and job
order on the sheet.

**End of each shift:** the supervisor enters what was produced on the Daily Entry
tab. Save is instant, so entering is quick.

**End of the day, when everything is entered:** press **Done entering, update
plan**. This rebuilds tomorrow's schedule from what actually happened. It takes 15
to 30 minutes, so start it before you leave.

**Anything already part-finished stays on the same machine.** Re-planning never
moves a half-cut part to a different machine.

---

## From Saturday onward, the daily rhythm

| When | Who | What |
|---|---|---|
| Shift start | Supervisor | Work from the printed sheet |
| Shift end | Supervisor | Enter the shift's production |
| Day end | Supervisor | Press **Done entering, update plan**, print tomorrow's sheets |
| Morning, if someone is off | Owner | Mark the absence in Settings, then press **Done entering, update plan** |
| When a delivery date changes | Owner | Change it in the Excel, upload again. Only the date changes. |

---

## Risks, and what to do about them

**If the plan looks wrong on Friday morning, do not fight it.** Keep the paper
snapshot. Run Friday the old way and tell me what looked wrong. Nothing is lost:
no machine has been stopped and no data destroyed. The only cost is a day.

**Do not change an operator's machines while that person has a part-finished
job.** There is a known bug: the system can keep them on a machine you just took
away from them. It is why Step 2 comes before Step 3. If you must change someone
mid-job, tell me and I will check that plan before you print it.

**Do not skip a step when entering in-progress work.** Everything after the skipped
step will stay blocked and the plan will schedule work that is already done.

**No software updates during the cutover.** Pushing a change now deploys straight
to the live site. Nothing goes out between Wednesday night and Friday evening
unless it is fixing something that is blocking you.

**One-time on Friday:** the Optimize panel may say the applied plan is out of date.
That is expected after this week's update. The deep search in Step 4 clears it.

---

## What the director is being asked to agree to

- Thursday is a setup day for one person on a computer. **No production time is
  lost.**
- Friday morning the floor starts following printed sheets instead of the current
  method.
- Someone must enter production at the end of every shift, from Friday onward,
  permanently. **The plan is only as good as that habit.** This is the one real
  commitment being made.
- The first week will need corrections. That is normal for the first week and not
  a sign the system is wrong.
