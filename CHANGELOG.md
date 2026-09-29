# Changelog

Version IDs are `vMAJOR.MINOR.PATCH`:

| Part | Changes when |
|------|--------------|
| **MAJOR** (v**2**.02.0) | the storage format or the architecture changes (v1 = CLI, v2 = Telegram) |
| **MINOR** (v2.**02**.0) | user-facing capability is added (this release) |
| **PATCH** (v2.02.**0**) | bug fixes and prompt tweaks only |

Every release is tagged on GitHub, and the same number appears in three places so
you can always tell which build is running: the `VERSION` constant in `app.py`,
this file, and the git tag. `python app.py --version` prints it.

---

## v2.02.0 — 2026-09-29

Recurring meetings, history, a clock, and a help card that fits a phone.

### 1. Recurring meetings (`freq` + a compulsory end date)

A repeating entry now lives in one message:

```
standup 9am every weekday until 30 nov
pay rent 1 oct monthly 12 times
```

Four frequencies: **daily**, **weekdays** (Mon–Fri, weekends skipped), **weekly**,
**monthly**.

**An end date is mandatory.** You may state it either way:

| You say | Stored as |
|---------|-----------|
| `until 30 nov` | an end date |
| `12 times` | a count |

If you give neither, the bot asks for one and **creates nothing** until you answer.
That is enforced in three places, so no code path can write a series without it:

1. `plan_dates()` refuses to return dates without an end date or a count.
2. A bare `yes` while the end date is still missing re-asks instead of confirming.
3. `confirm()` itself re-checks before touching the database.

The confirmation card always states both figures before you commit:

```
🔁 Save this series?
─────────────
📝 Standup
📅 Tuesday, 29 Sep 2026 — 9:00 AM
🔁 Repeats every weekday (Mon–Fri)
🔢 10 occurrences
🏁 Ends Friday, 09 Oct 2026
─────────────
Reply "yes" to save all 10, "no" to cancel,
or tell me what to change.
```

Your answer to "how long?" is read **locally** — `30 nov`, `nov 30`, `30/11/2026`,
`8 times` are parsed with plain date arithmetic, so that step costs no API call and
cannot fail on a bad network. `31 feb` and other unreal dates are rejected, and a
year is inferred forward when you leave it out.

Two guards worth knowing: a series is capped at **120 occurrences** (a typo like
"until 2099" is refused, not created), and an end date earlier than the first
occurrence is refused.

**Each occurrence is its own row.** That keeps the single-table design and means any
single date can be edited or deleted on its own (`delete 37` removes just that one).
The trade-off: there is no "delete the whole series" command yet — see Later.

### 2. History

```
history
history this month
what did I do last week
```

Shows what is already **finished** (✔) or whose **day has passed** (○), newest first.
Default window is the last 7 days; at most 25 lines are shown, with a count of how
many more exist.

```
🕘 History — 4 item(s)
23 Sep → 29 Sep  ·  1 done

── Tuesday, 29 Sep 2026  (today)
  ✔  8:00 AM  Dentist   (#24)
── Sunday, 27 Sep 2026
  ○  all day  Old overdue thing   (#25)
```

Looking up an entry by id now also shows the words you originally typed
(`💬 You said: ...`), so the record keeps your phrasing as well as the tidy title.

### 3. The clock

```
now        date        time        what time is it
```

```
🕐 Right now
─────────────
📅 Tuesday, 29 Sep 2026
🕒 10:20 AM
🌏 Asia/Singapore  (UTC+08:00)
🔢 2026-09-29
```

The offset is read from the **machine**, not from config. If the server is on UTC —
the classic way this bot silently shifts every date by 8 hours — the reply says so
explicitly, and the bot warns you in Telegram at startup. `python app.py --version`
prints the same check from the command line.

### 4. Help that fits a phone

The old card used aligned columns, which wrapped into a mess on a phone screen. The
new one is a single narrow column, grouped by what you want to do, every line under
36 characters — enforced by a selftest assertion, so it can't drift back.

### Also

- `--version` flag; `VERSION` constant; `TZ_NAME` configurable via `.env`.
- Startup banner now reports version and clock offset.
- `render_full_entry` shows `open`/`done` as a word, not the raw status code.

### Tests

`python app.py --selftest` grew to cover series maths (weekly, weekdays skipping
weekends, month-end clamping, end-date mode, the 120 cap), the compulsory-end-date
rule, the confirmation card's count/end-date lines, every `parse_end_answer` shape,
history querying and ordering, the clock, and the help-text width budget.

An external harness (37 checks) additionally drives the real router with a stubbed
model and no Telegram, covering: preview writes nothing, `yes` saves exactly the
right number of rows, `yes` is refused while awaiting an end date, weekdays land only
on weekdays, deleting one occurrence leaves the rest intact, strangers are ignored,
greetings cost zero model calls. Result: **37 passed, 0 failed**.

### Upgrading from v2.01

Drop-in. Only additive columns are used (`freq`/`until`/`count` live in the in-memory
draft, not the schema), so `planner.db` needs no migration and no existing entry is
touched. Nothing on the Vultr server changes except `app.py` itself.

### Later (not in this version)

- Delete or edit a whole series in one go (`delete series 12`).
- A daily digest / reminders — still deferred to v2.1 by choice.
- Voice notes and photo capture — v2.0 plan §1.3, unchanged.
