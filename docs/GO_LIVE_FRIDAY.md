# Going live on the PPC system, Friday 7 August 2026

A cutover plan for a floor that is already running. Nothing here stops a machine
or abandons a part-finished job.

---

## The one fact that shapes everything

**Production is entered between 9:30 and 10:00 in the morning. Shift 1 starts at
08:00.**

So the plan can never be refreshed before shift 1 begins. Not on Friday, not on any
day after. A plan built at 10:30 cannot govern a shift that started two and a half
hours earlier.

This is not a problem to solve, it is a rhythm to accept:

> **Each morning's entry produces the sheets for tonight's shift 2 and tomorrow's
> shift 1. The floor always works from the sheets printed the day before.**

The plan covers weeks of work, so being one cycle behind costs nothing. Trying to
make shift 1 wait for a mid-morning refresh would cost real hours every day.

---

## The three days

| Day | What happens | Who |
|---|---|---|
| **Thursday** (holiday) | System setup. No production data needed. | Owner, about 45 min |
| **Friday 9:30 to 10:00** | Enter Wednesday's production, build the plan, print | Owner, about 1 hour total |
| **Friday 19:00, shift 2** | **The floor starts following the printed sheets** | Shift 2 supervisor |

Friday's shift 1 runs the way it runs today. That is deliberate: the plan does not
exist until about 10:45, and switching a shift over halfway through is how mistakes
get made.

---

## Thursday: setup, nothing to do with production

Nobody is working, so nothing changes underneath you. Take your time.

1. **Upload the current sales-order Excel** on the Orders tab. Check the order
   count looks right.
2. **Settings → Operators & shifts.** For every person: correct name, the machines
   they actually run picked from the dropdown, and **their shift**.
3. **Read every row of the Shift column.** Shifts no longer swap automatically.
   What you set is what that person works **every week** until you change it. Some
   rows may still show whatever the old automatic swap left behind.
4. **Settings → Operator absences.** Mark anyone you already know is away.

That is the whole of Thursday. Doing it now is what keeps Friday morning down to
one hour.

---

## Friday 9:30 to 10:00: enter Wednesday's production

Wednesday's work is the only production outstanding. Nobody worked Thursday.

On the **Daily Entry** tab, enter it **dated Wednesday 05-08-2026**, not Friday.
The date matters: entering it as Wednesday makes the plan start from Friday on its
own, because the system skips Thursday as a non-working day. I verified this
behaviour directly.

### The one rule that will otherwise stop you

You cannot record work at a step until the step **before** it has been recorded.
This is deliberate: it stops the plan from scheduling pieces that do not physically
exist yet.

For a job sitting at CNC 1st side:

- ✅ Enter `Bandsaw OS = 500`, then `CNC FIRST SIDE = 300`.
- ❌ Entering `CNC FIRST SIDE = 300` alone is refused, saying only 0 pieces have
  cleared Bandsaw OS.

**Outside (OS) steps must be entered too**, even though they need no operator name.
Skip one and everything after it stays blocked.

Enter what has genuinely been **finished** at each step, not what is loaded on the
machine. Quantities add up across days.

---

## Friday 10:00 to about 10:45: build and print

1. **Check the Orders tab first.** The **Remaining** column should match what your
   floor actually still has to make. Fix mistakes now. Once you save a Friday-dated
   entry later, Wednesday's entries lock.
2. Press **Start deep search** on the Schedule tab. 15 to 30 minutes.
3. When it finishes, read the result and press **Apply**. **Nothing changes until
   you press Apply.**
4. Print:
   - **Machine-wise schedule**, one sheet per machine, posted at the machine.
   - **Shift-wise schedule**, handed to the shift supervisor.

**Ignore anything on the sheet dated before 19:00 Friday.** The plan starts from
late Friday morning because that is when you built it, but the floor is not
following it until shift 2. That gap corrects itself at the next refresh.

Expect some orders to show as late. That is the system being honest about a
workload the crew cannot absorb, not a fault.

---

## Friday 19:00: the floor goes live

Shift 2 starts working from the printed sheets. From here on, the sheets are the
instruction.

**One decision for you:** shift 2 is a night shift and you may not be there. If you
would rather be standing on the floor for the first shift that follows the system,
go live **Saturday shift 1** instead and treat Friday shift 2 as a normal shift.
Everything above is unchanged, you simply print on Friday and start on Saturday.
Losing half a day to be present for the first one is a fair trade.

---

## The daily rhythm from Saturday onward

| Time | Who | What |
|---|---|---|
| Shift start | Supervisor | Work from the sheets printed yesterday |
| 9:30 to 10:00 | Owner | Enter yesterday's production, both shifts |
| About 10:00 | Owner | Press **Done entering, update plan**, wait 15 to 30 min |
| About 10:45 | Owner | Print the new sheets. **They govern tonight's shift 2 and tomorrow's shift 1** |
| Morning, if someone is off | Owner | Mark the absence first, then press **Done entering, update plan** |
| When a delivery date changes | Owner | Change it in the Excel and upload again. Only the date changes. |

**Anything already part-finished stays on the same machine** when the plan is
rebuilt. A half-cut part is never moved.

---

## Risks, and what to do about them

**If Friday's plan looks wrong, do not fight it.** Run Friday shift 2 the way you
run it today and tell me what looked wrong. Nothing is lost: no machine stopped, no
data destroyed. The cost is a day.

**Do not change an operator's machines while that person has a part-finished job.**
There is a known bug where the system can keep them on a machine you just took
away. It is exactly why the operator setup is on Thursday, before any production is
entered. If you must change someone mid-job, tell me and I will check the plan
before you print it.

**Do not skip a step when entering production.** Everything after it stays blocked
and the plan will schedule work that is already done.

**No software changes from me between now and Friday evening** unless something is
actively blocking you. Pushing a change deploys straight to the live site.

**One-time on Friday:** the Optimize panel may say the applied plan is out of date.
Expected after this week's update. The deep search clears it.

---

## What the director is being asked to agree to

- Thursday costs **no production time**. One person, 45 minutes.
- Friday morning costs **one hour of the owner's time**, not the floor's.
- From Friday evening, the floor works from printed sheets instead of the current
  method.
- **Someone enters production every day, permanently.** The plan is only ever as
  good as that habit. This is the one real commitment.
- **Shift 1 always works from sheets printed the day before**, because entry
  happens at 9:30. That is by design, not a delay.
- The first week will need corrections. Normal, and not a sign the system is wrong.
