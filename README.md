# Telegram Planner v2.01

A Telegram bot that keeps your schedule in one SQLite file. Message it in plain
English; it works out what you mean, shows you what it will save, and writes only
after you say `yes`.

**One Python file. Three dependencies. One database.**

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
python app.py --selftest   # test storage only — no API key, no Telegram needed
python app.py --ask "..."  # see what the LLM returns — no Telegram needed
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
help                     the command card

lunch with sarah tomorrow 1pm
submit gst 30 oct
call mum friday 7pm
                         → bot shows the draft
                         → "yes" saves it, "no" cancels
                         → or "change to 12:30" to revise, then "yes"

edit 12 to 2pm
delete 12
done 12                  mark finished (drops out of your lists)
search dentist
```

### Two rules worth knowing

- **All-day vs a real time.** A stated clock time (`1pm`, `13:00`, `7.30pm`, `1900`)
  is kept. A vague word like "morning", "afternoon" or "evening" is treated as
  **all-day** — say "9am" if you mean 9am.
- **Nothing is written without a `yes`.** The model has no write capability at all;
  it only parses language. Saving happens in Python, only after you confirm.

### One retrieval intent

There is a single read intent, `list`, with `start_date` / `end_date`. A single day
is just `start_date == end_date`, so "today", "tomorrow", "friday" and "next week"
all take the same code path. `list_all` is separate and means "no date limit".

If the model returns `list` without usable dates, the bot shows today rather than
erroring.

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
                                            "yes" ───┤
                                                     ▼
                                          insert/update/delete  ← the only writes
                                                     │
                                                     ▼
                                              planner.db
```

### Sections in `app.py`

| # | Section | What's in it |
|---|---|---|
| 1 | `config` | `.env` loading, constants, word sets |
| 2 | `storage` | `init_db()` + 8 SQL functions |
| 3 | `llm` | `call_llm()` — one function, JSON mode |
| 4 | `handlers` | rendering, Telegram transport, the confirmation gate, routing |
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

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot never replies | Check the terminal for errors; confirm `ALLOWED_USER_ID` matches your real ID |
| `Conflict: terminated by other getUpdates` | The bot is running twice (PC + server). Stop one. |
| Times look 8 hours off | `sudo timedatectl set-timezone Asia/Singapore` on the server |
| `401 invalid_api_key` | Wrong key in `.env` |
| `insufficient_quota` | Top up OpenAI billing |
| JSON errors often | `MODEL=gpt-4o-mini` in `.env`, restart |
| `Unsupported value: 'temperature'` | A bug — `app.py` must not send it. Tell me. |

---

## Deploying 24/7

See `../DEPLOY-v2.01.md` — Phases D–F cover the Vultr server, systemd, firewall,
SSH hardening and nightly backups.
