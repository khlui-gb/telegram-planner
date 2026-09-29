# Telegram Planner v2.02

A Telegram bot that keeps your schedule in one SQLite file. Message it in plain
English; it works out what you mean, shows you what it will save, and writes only
after you say `yes`.

**One Python file. Three dependencies. One database.**

> **Current version: `v2.02.0`** — repeating meetings with a compulsory end date,
> history, a clock, and a phone-sized help card.
> See [CHANGELOG.md](CHANGELOG.md) for the version-ID scheme and what changed.
> `python app.py --version` prints the build you are actually running.

---

## Setup

```powershell
pip install -r requirements.txt
copy .env.example .env
notepad .env          # fill in the three real values
```

`.env` needs:

| Key | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather in Telegram → `/newbot` |
| `ALLOWED_USER_ID` | Message your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.from.id` |
| `OPENAI_API_KEY` | platform.openai.com → API keys |

> `ALLOWED_USER_ID` is your security boundary. Anyone not on it is ignored
> **silently** — no reply, no error, no hint the bot exists.

---

## Commands

```powershell
python app.py              # run the bot (long polling, Ctrl+C to stop)
python app.py --selftest   # test storage + all the date maths — no API key needed
python app.py --ask "..."  # see what the LLM returns — no Telegram needed
python app.py --version    # version, model, and the timezone the machine reports
python app.py --init       # create planner.db and exit
python app.py --count      # how many entries are stored
```

**Build order:** run `--selftest` first (storage), then `--ask` (the brain), then
`python app.py` (the whole thing). If a layer is broken you want to know before
the next one goes on top of it.

---

## Using it

```
today                    what's on today
tomorrow
friday
list                     next 7 days
all events               everything still open, grouped by day
overdue                  open items from before today
history                  what's finished or already past (last 7 days)
now                      the date, time and timezone right now
help                     the command card

lunch with sarah tomorrow 1pm
submit gst 30 oct
call mum friday 7pm
                         → bot shows the draft
                         → "yes" saves it, "no" cancels
                         → or "change to 12:30" to revise, then "yes"

standup 9am every weekday until 30 nov
pay rent 1 oct monthly 12 times
                         → a repeating series, one message

edit 12 to 2pm
delete 12
done 12                  mark finished (drops out of your lists)
search dentist
```

### Repeating entries

Say it once and the series is created:

| Frequency | Triggered by |
|---|---|
| `daily` | "every day", "daily" |
| `weekdays` | "every weekday", "every working day", "Monday to Friday" (weekends skipped) |
| `weekly` | "every monday", "weekly" |
| `monthly` | "monthly", "each month" |

**An end date is mandatory.** Give either `until 30 nov` (an end date) or
`12 times` (a count). If you give neither, the bot asks for one and saves nothing
until you answer — a bare `yes` won't get past it. Your reply is understood locally
(`30 nov`, `nov 30`, `30/11/2026`, `8 times`), so that step is instant, free, and
works even if the API is down.

The confirmation card states **both** figures before you commit:

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

Each occurrence is stored as its own row, so any single date can be edited or
deleted on its own. A series is capped at 120 occurrences, and an end date before
the first occurrence is refused.

### Two rules worth knowing

- **All-day vs a real time.** A stated clock time (`1pm`, `13:00`, `7.30pm`, `1900`)
  is kept. A vague word like "morning", "afternoon" or "evening" is treated as
  **all-day** — say "9am" if you mean 9am.
- **Nothing is written without a `yes`.** The model has no write capability at all;
  it only parses language. Saving happens in Python, only after you confirm.

### Two retrieval intents

`list` covers any day or range — a single day is just `start_date == end_date`, so
"today", "tomorrow", "friday" and "next week" all take one code path. `list_all`
means "no date limit". `history` is the past: finished items (✔) and days that have
gone by (○), newest first.

---

## How it works

```
Telegram ──► handle_message() ──► allowlist check
                                      │
                                      ▼
                              call_llm()  (gpt-5-nano, JSON only)
                                      │
                                      ▼
                         intent ──► handler ──► draft shown
                                                     │
                          "yes" ────────────────────┤
                                                     ▼
                                     plan_dates()  ← expanding a series, and
                                                     ← refusing one with no end
                                                     ▼
                                          insert/update/delete  ← the only writes
                                                     │
                                                     ▼
                                              planner.db
```

### Sections in `app.py`

| # | Section | What's in it |
|---|---|---|
| 1 | `config` | `.env` loading, `VERSION`, constants, word sets |
| 2 | `storage` | `init_db()` + 11 query helpers |
| 3 | `llm` | `call_llm()` — one function, JSON mode |
| 4 | `handlers` | date maths, series expansion, rendering, the confirmation gate, routing |
| 5 | `main` | CLI flags + the polling loop |

### The one table

```sql
entries(
  id, created_at, event_date, event_time, title, status, raw_input
)
```

Dates are stored as **local Singapore strings** (`YYYY-MM-DD`), not UTC. One user,
one timezone, no DST — so "today's schedule" is a literal string comparison and
there is no timezone conversion code to get wrong.

A repeating entry is **expanded into one row per occurrence**, so the table needs no
new columns: `freq`, `until` and `count` live only in the in-memory draft. That is
why v2.01 → v2.02 needs no migration.

---

## Design notes

**Why one file.** v2.0's multi-module split (`core/db.py`, `core/timez.py`, …)
existed to serve voice, photos, reminders and bilingual parsing. Remove those and
the reason to split evaporates at ~1100 lines. Split when it hurts, not before —
see `PLAN-v2.01.md` §8 for the concrete trigger points.

**Why the model can't write.** The LLM's only output is an intent plus a few
fields. There is no `write` tool to call. This is v1.0's core guardrail, preserved.

**Why the date is re-validated in Python.** The model returns `YYYY-MM-DD`; Python
parses it with `strptime` and rejects anything malformed or in the past. A guessed
appointment time is worse than asking again.

**Why no `temperature`.** `gpt-5-nano` is a reasoning model and rejects
`temperature`, `top_p`, `presence_penalty` and `frequency_penalty` with HTTP 400.
It's sent with `reasoning_effort="minimal"` instead, which suits a pure
classification task. JSON output mode is supported and is what we rely on.

**Why pending state is only in memory.** A dict, not a table. A bot restart loses a
half-finished draft — harmless for one user, and it avoids a table plus a TTL sweep.
`PLAN.md` (full v2.0) upgrades this to a `pending_actions` table when it matters.

**Why the end date is compulsory and enforced in three places.** A series with no end
is a data-entry accident waiting to happen — "every monday" turns into 500 rows. So
`plan_dates()` refuses to produce dates without one, `handle_pending()` re-asks instead
of honouring a bare `yes`, and `confirm()` re-checks before inserting. Any one of the
three alone would be enough; all three mean no future refactor can quietly undo it.

**Why the end-date answer is parsed locally.** "How long should this repeat?" is the
one question the bot *must* get answered, so it doesn't depend on the model or the
network: `30 nov`, `nov 30`, `30/11/2026` and `8 times` are plain date arithmetic.
The model handles the fuzzy cases ("for eight weeks"); the parser handles the crisp
ones for free.

**Why the clock reads the OS, not the config.** `offset_str()` asks the machine. A
server on UTC would otherwise shift every date by 8 hours while `TZ_NAME` cheerfully
claimed Singapore. Now the mismatch is visible in the `now` reply and announced in
Telegram at startup.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot never replies | Check the terminal for errors; confirm `ALLOWED_USER_ID` matches your real ID |
| `Conflict: terminated by other getUpdates` | The bot is running twice (PC + server). Stop one. |
| Times look 8 hours off | `sudo timedatectl set-timezone Asia/Singapore` on the server, restart the service. `now` tells you if it's wrong. |
| A series created more rows than expected | `all events`, then `delete <id>` the ones you don't want. Cap is 120 per series. |
| `401 invalid_api_key` | Wrong key in `.env` |
| `insufficient_quota` | Top up OpenAI billing |
| JSON errors often | `MODEL=gpt-4o-mini` in `.env`, restart |
| `Unsupported value: 'temperature'` | A bug — `app.py` must not send it. Tell me. |

---

## Upgrading

Replace `app.py` and restart. No database migration: v2.02 only adds columns to the
in-memory draft, never to the schema, so `planner.db` is untouched and every existing
entry keeps working. `CHANGELOG.md` records what changed in each version.

---

## Deploying 24/7

See `../DEPLOY-v2.01.md` — Phases D–F cover the Vultr server, systemd, firewall,
SSH hardening and nightly backups.
