#!/usr/bin/env python3
"""
Telegram Planner v2.01 — the whole program, in one file.

A Telegram bot that keeps a personal schedule in SQLite. You message it in plain
English; it figures out what you mean, shows you what it's about to save, and
writes only after you say "yes".

  python app.py              run the bot (long polling)
  python app.py --selftest   prove storage works, no API key needed
  python app.py --ask "..."  print what the LLM returns, no Telegram needed
  python app.py --init       create planner.db and exit

--------------------------------------------------------------------------------
CHANGELOG (newest first)
--------------------------------------------------------------------------------
v2.01.1  2026-09-28  Fixes from real `--ask` output on the user's machine.
                    - "today" and "tomorrow" returned two different intents
                      ("today" vs "list"), so the same job had two code paths.
                      Collapsed: there is now ONE retrieval intent, "list", where
                      a single day is start_date == end_date. The router handles it
                      uniformly and falls back to today if the model gives no dates.
                    - Added "list_all" for "all events" / "everything" / "show me
                      all", which previously listed only today.
                    - Added local greeting handler (hi / hello / good morning) so
                      greetings cost no API call and read naturally.
                    - Bare yes/no with nothing pending no longer reaches the model.
                    - Prompt: tightened title casing guidance.

v2.01.0  2026-09-28  Initial single-file build.
                    - 5 sections: config / storage / llm / handlers / main
                    - guardrail: the LLM has NO write capability; writes happen in
                      Python only after an explicit confirmation.
                    - gpt-5-nano: no temperature/top_p/penalties (they 400 on this
                      model); reasoning_effort=minimal; JSON output mode.
                    - Python re-validates every date; never writes an unvalidated one.
                    - crash-proof: every handler wrapped, one bad message can't kill it.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

# =============================================================================
# 1. CONFIG
# =============================================================================

import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install -r requirements.txt")

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("Missing dependency. Run:  pip install -r requirements.txt")

try:
    from openai import OpenAI
except ImportError:
    sys.exit("Missing dependency. Run:  pip install -r requirements.txt")


# --- settings ----------------------------------------------------------------

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MODEL = os.getenv("MODEL", "gpt-5-nano").strip()

try:
    ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0").strip())
except ValueError:
    sys.exit("ALLOWED_USER_ID in .env must be a plain number, e.g. 123456789")

# Optional: only set this if you want to keep the API key out of .env
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip() or None

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "planner.db")

TG_API = f"https://api.telegram.org/bot{TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TOKEN}"

# How many times you may revise a draft before it's dropped.
MAX_CONFIRM_TURNS = 5

# "yes" / "no" word sets for the confirmation gate.
YES_WORDS = {
    "yes", "y", "yeah", "yep", "yup", "ok", "okay", "sure",
    "save", "confirm", "confirmed", "correct", "do it", "go ahead", "👍",
}
NO_WORDS = {
    "no", "n", "nope", "nah", "cancel", "abort", "stop", "forget it", "discard", "❌",
}

# One pending draft per user, in memory. A bot restart loses a half-finished
# draft — harmless for a single user, and it saves a whole table.
PENDING: dict[int, dict] = {}

# Cached Telegram identity map, mostly so duplicate "Add this?" context can show a
# chat id if Telegram ever hands us one we don't expect. Enabled lazily.
ME: dict = {}


# =============================================================================
# 2. STORAGE
# =============================================================================

def connect() -> sqlite3.Connection:
    """Open the database. WAL so a read never blocks a write.

    Note: `with sqlite3.connect(...)` commits but does NOT close the connection.
    Every caller here uses try/finally with conn.close() instead, so no file
    handles are left behind holding the WAL.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    """Create the table if it isn't there. Safe to call every startup."""
    conn = connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                event_date  TEXT NOT NULL,
                event_time  TEXT,
                title       TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'open',
                raw_input   TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON entries(event_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON entries(status)")
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _remove_file(path: str) -> None:
    """Delete a file if present, ignoring any failure.

    Only ever called on our own throwaway selftest database.
    """
    try:
        if os.path.exists(path):
            os.chmod(path, 0o600)  # clear any read-only bit first
            os.remove(path)
    except OSError as exc:
        print(f"[selftest] could not remove {path}: {exc}", flush=True)


def insert_entry(event_date: str, event_time, title: str, raw_input: str = "") -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO entries (created_at, event_date, event_time, title, status, raw_input)"
            " VALUES (?, ?, ?, ?, 'open', ?)",
            (_now(), event_date, event_time, title, raw_input),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_entry(entry_id: int, **fields) -> bool:
    """Update only the columns given. Returns False if the row doesn't exist."""
    allowed = {"event_date", "event_time", "title", "status"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    clause = ", ".join(f"{k} = ?" for k in sets)
    conn = connect()
    try:
        cur = conn.execute(
            f"UPDATE entries SET {clause} WHERE id = ?", (*sets.values(), entry_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_entry(entry_id: int):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_entry(entry_id: int) -> bool:
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def entries_for_date(d: str):
    """Timed items first in clock order, all-day items last."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM entries WHERE event_date = ? AND status = 'open'"
            " ORDER BY (event_time IS NULL), event_time, id",
            (d,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def entries_in_range(start: str, end: str):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM entries WHERE event_date >= ? AND event_date <= ?"
            " AND status = 'open' ORDER BY event_date, (event_time IS NULL), event_time, id",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def overdue_entries():
    """Open items whose day has passed. This is what makes it better than a diary."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM entries WHERE event_date < ? AND status = 'open'"
            " ORDER BY event_date, (event_time IS NULL), event_time, id",
            (today_iso(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def all_open_entries(limit: int = 200):
    """Every open entry, no date limit."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM entries WHERE status = 'open'"
            " ORDER BY event_date, (event_time IS NULL), event_time, id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_entries(term: str, limit: int = 15):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM entries WHERE status = 'open' AND title LIKE ?"
            " ORDER BY event_date, (event_time IS NULL), event_time LIMIT ?",
            (f"%{term}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_entries() -> int:
    conn = connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
    finally:
        conn.close()


# =============================================================================
# 3. LLM
# =============================================================================

SYSTEM_PROMPT = """You are the intent parser for a personal planner bot.

Today is {today} ({weekday}). The user's timezone is Asia/Singapore (UTC+8).

Read the user's message and reply with ONE JSON object and nothing else.

Possible replies:

  Add something new:
    {{"intent": "add", "title": "...", "event_date": "YYYY-MM-DD", "event_time": "HH:MM"}}

  What is on today / tomorrow / a named day:
    {{"intent": "list", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}}

  A range of days:
    {{"intent": "list", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}}

  Everything still open, with no date limit:
    {{"intent": "list_all"}}

  Change an existing entry (only include fields that change):
    {{"intent": "edit", "id": 12, "title": "...", "event_date": "YYYY-MM-DD", "event_time": "HH:MM"}}

  Remove an entry:
    {{"intent": "delete", "id": 12}}

  Mark an entry as finished:
    {{"intent": "done", "id": 12}}

  Find something by keyword:
    {{"intent": "search", "query": "dentist"}}

  Open items from before today:
    {{"intent": "overdue"}}

  The user asked for help or to see the commands:
    {{"intent": "help"}}

  Anything else — questions, chat, coding, arithmetic, news:
    {{"intent": "other"}}

Rules:
- Resolve every relative date against today: {today}.
- Return dates as YYYY-MM-DD and times as 24-hour HH:MM.
- For any request to READ the schedule, use "list" with start_date and end_date.
  A single day means start_date equals end_date. So "today" is
  {today} to {today}, and "tomorrow" is one day later. "This week", "next
  7 days" and "this month" are ranges ending on the appropriate later day.
- "all events", "everything", "show me all", "my whole schedule" and "what have
  I got" mean the entire open list with no date limit — use "list_all". Only use
  "list" when a specific day or bounded range is asked for.
- If no clock time is stated, set "event_time" to null. A bare "morning",
  "afternoon" or "evening" is NOT a clock time — use null. Only a real time
  ("1pm", "13:00", "7.30pm", "1900") counts.
- Write "title" as a short, tidy phrase. Keep the user's own wording and
  capitalisation for names. Do not add a full stop.
- Title case: "call mum" -> "Call mum", "submit gst" -> "Submit GST".
- "today", "tomorrow", "next tuesday", "friday", "on 30 oct" are normal to see.
- For edit, delete and done, the user will give you an id number like "#12" or
  "entry 12". Put it in "id".
- You cannot read or write the database. You only parse language. Never claim an
  entry was saved.
- If the message is not about the user's schedule, reply {{"intent": "other"}}.
"""

_client = None


def llm_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    return _client


def call_llm(user_text: str) -> dict:
    """Ask the model what the user means. Returns a dict, never raises.

    On any failure returns {"intent": "error", "detail": "..."} so the caller can
    apologise instead of crashing.
    """
    now = datetime.now()
    prompt = SYSTEM_PROMPT.format(
        today=now.strftime("%Y-%m-%d"), weekday=now.strftime("%A")
    )

    kwargs = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_text},
        ],
        "response_format": {"type": "json_object"},
    }

    # gpt-5-nano is a reasoning model: it rejects temperature, top_p and the
    # penalty parameters outright (HTTP 400). So we pass none of them.
    # "minimal" keeps a pure classification task fast and cheap.
    if MODEL.startswith("gpt-5"):
        kwargs["reasoning_effort"] = "minimal"

    try:
        resp = llm_client().chat.completions.create(**kwargs)
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return {"intent": "error", "detail": "empty response"}
        data = json.loads(content)
        if not isinstance(data, dict) or "intent" not in data:
            return {"intent": "error", "detail": "unexpected shape"}
        return data
    except json.JSONDecodeError:
        return {"intent": "error", "detail": "model did not return JSON"}
    except Exception as exc:  # noqa: BLE001 - deliberately broad, we never crash
        name = type(exc).__name__
        if "temperature" in str(exc):
            return {
                "intent": "error",
                "detail": f"temperature rejected by {MODEL} — it must not be sent.",
            }
        return {"intent": "error", "detail": f"{name}: {exc}"}


# =============================================================================
# 4. HANDLERS
# =============================================================================

# --- time helpers ------------------------------------------------------------

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def today_iso() -> str:
    return date.today().isoformat()


def validate_date(value) -> str | None:
    """Only accept a real YYYY-MM-DD date. Return None if it isn't one."""
    if not isinstance(value, str):
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        datetime.strptime(value.strip(), "%Y-%m-%d")
        return value.strip()
    except ValueError:
        return None


def validate_time(value) -> str | None:
    """Accept 'HH:MM' (and tidy up 'H:MM'). Return None to mean all-day."""
    if value in (None, "", "null"):
        return None
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def pretty_date(iso: str) -> str:
    """'2026-10-06' -> 'Tuesday, 06 Oct 2026'."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return iso
    return f"{WEEKDAYS[d.weekday()]}, {d.strftime('%d %b %Y')}"


def pretty_time(t) -> str:
    if not t:
        return "all day"
    h, m = t.split(":")
    h = int(h)
    suffix = "AM" if h < 12 else "PM"
    display = h % 12 or 12
    return f"{display}:{m} {suffix}"


def relative_day(iso: str) -> str:
    """'today' / 'tomorrow' / 'yesterday', else ''."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return ""
    delta = (d - date.today()).days
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get(delta, "")


# --- rendering ---------------------------------------------------------------

def render_line(e: dict) -> str:
    t = pretty_time(e.get("event_time"))
    tag = f"{t:>7}" if e.get("event_time") else "  all day"
    return f"  {tag}  {e['title']}   (#{e['id']})"


def render_agenda(d: str, items: list, heading: str | None = None) -> str:
    head = heading or f"📅 {pretty_date(d)}"
    if not items:
        return f"{head}\n─────────────\nNothing scheduled. 🎉"
    lines = [render_line(e) for e in items]
    return f"{head}\n─────────────\n" + "\n".join(lines)


def render_full_entry(e: dict) -> str:
    bits = [
        f"📅 {pretty_date(e['event_date'])}"
        + (f" — {pretty_time(e['event_time'])}" if e.get("event_time") else " — all day"),
        f"📝 {e['title']}",
        f"🆔 #{e['id']}  ({e['status']})",
    ]
    return "\n".join(bits)


def help_text() -> str:
    return (
        "📔 Planner — what I do\n"
        "\n"
        "Add      \"lunch with sarah tomorrow 1pm\"\n"
        "         \"submit gst 30 oct\"\n"
        "         \"call mum friday 7pm\"\n"
        "Today    \"today\"\n"
        "Other    \"tomorrow\" · \"friday\" · \"this week\" · \"all events\"\n"
        "         \"overdue\"\n"
        "Search   \"search dentist\"\n"
        "Change   \"edit 12 to 2pm\"\n"
        "         \"delete 12\"\n"
        "         \"done 12\"\n"
        "Help     \"help\"\n"
        "\n"
        "I'll always show you what I'm about to save, and wait for \"yes\".\n"
        "If you name a real clock time, I'll keep it. \"Morning\", \"afternoon\"\n"
        "and \"evening\" are treated as all-day — say \"9am\" if you mean 9am."
    )


# --- Telegram transport ------------------------------------------------------

def tg(method: str, **params):
    """Call the Telegram API. Returns the `result` on success, None on failure."""
    try:
        r = requests.post(f"{TG_API}/{method}", json=params, timeout=40)
        data = r.json()
        if not data.get("ok"):
            print(f"[tg] {method} failed: {data.get('description')}", flush=True)
            return None
        return data.get("result")
    except Exception as exc:  # noqa: BLE001
        print(f"[tg] {method} error: {type(exc).__name__}: {exc}", flush=True)
        return None


def send(chat_id: int, text: str) -> None:
    # Plain text on purpose: no Markdown means no escaping bugs and no parse errors.
    if not tg("sendMessage", chat_id=chat_id, text=text):
        # One retry, in case of a transient network blip.
        time.sleep(1)
        tg("sendMessage", chat_id=chat_id, text=text)


def notify_owner(text: str) -> None:
    if ALLOWED_USER_ID:
        send(ALLOWED_USER_ID, text)


# --- the confirmation gate ---------------------------------------------------

def build_preview(payload: dict) -> str:
    d, t, title = payload["event_date"], payload["event_time"], payload["title"]
    lines = [
        "Add this?",
        "─────────────",
        f"📅 {pretty_date(d)}"
        + (f" — {pretty_time(t)}" if t else "  (all day)"),
        f"📝 {title}",
    ]
    # Cheap conflict check: same day, both timed, same clock time.
    if t:
        clashes = [
            e for e in entries_for_date(d)
            if e.get("event_time") == t and e["title"].lower() != title.lower()
        ]
        if clashes:
            lines.append(f"⚠️ Clashes with: {clashes[0]['title']} (#{clashes[0]['id']})")
    lines.append("─────────────")
    lines.append('Reply "yes" to save, "no" to cancel, or tell me what to change.')
    return "\n".join(lines)


def ask_add(chat_id: int, user_id: int, user_text: str, data: dict) -> None:
    """Draft a new entry and ask for confirmation. Nothing is written yet."""
    d = validate_date(data.get("event_date"))
    if not d:
        send(chat_id, "I couldn't work out the date. Try \"tomorrow 1pm\" or \"30 oct\".")
        return

    t = validate_time(data.get("event_time"))

    if d < today_iso():
        send(
            chat_id,
            f"⚠️ That date is in the past ({pretty_date(d)}).\n"
            "If that's deliberate, say it again with the year — or give me a "
            "future date.",
        )
        return

    title = (data.get("title") or "").strip()
    if not title:
        send(chat_id, "I need a short title for that. What should I call it?")
        return

    payload = {"event_date": d, "event_time": t, "title": title, "raw_input": user_text}
    PENDING[user_id] = {"action": "add", "data": payload, "turns": 0, "orig": user_text}
    send(chat_id, build_preview(payload))


def confirm(chat_id: int, user_id: int) -> None:
    """The user said yes. THIS is the only place new rows are created."""
    pending = PENDING.pop(user_id, None)
    if not pending:
        send(chat_id, "Nothing to confirm. What would you like to add?")
        return
    data = pending["data"]
    try:
        new_id = insert_entry(
            data["event_date"], data["event_time"], data["title"], data["raw_input"]
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[db] insert failed: {exc}", flush=True)
        send(chat_id, f"Sorry — I couldn't save that ({type(exc).__name__}). Try again.")
        return
    when = pretty_date(data["event_date"])
    if data["event_time"]:
        when += f", {pretty_time(data['event_time'])}"
    send(chat_id, f"✅ Saved #{new_id} — {when}\n{data['title']}")


def revise(chat_id: int, user_id: int, pending: dict, change_text: str) -> None:
    """Re-draft from a change request, keeping the original phrasing as context."""
    pending["turns"] += 1
    if pending["turns"] > MAX_CONFIRM_TURNS:
        PENDING.pop(user_id, None)
        send(chat_id, "Let's start over — what would you like to add?")
        return

    data = pending["data"]
    combined = (
        f"Original: {pending['orig']}\n"
        f"Currently drafted: {data['title']} on {data['event_date']} "
        f"at {data['event_time'] or 'no time'}\n"
        f"Change requested: {change_text}\n"
        "Apply the change and return the full updated 'add' object."
    )
    result = call_llm(combined)

    if result.get("intent") != "add":
        # We couldn't parse the change as an edit — just say so and keep waiting.
        send(chat_id, f"Sorry, I didn't catch that. {build_preview(data)}")
        return

    new_date = validate_date(result.get("event_date")) or data["event_date"]
    new_time = validate_time(result.get("event_time"))
    new_title = (result.get("title") or data["title"]).strip()

    data.update({"event_date": new_date, "event_time": new_time, "title": new_title})
    send(chat_id, build_preview(data))


def handle_pending(chat_id: int, user_id: int, text: str) -> None:
    """A draft is open. Route the reply: confirm, cancel, or revise."""
    pending = PENDING.get(user_id)
    if not pending:
        return
    low = text.strip().lower().rstrip("!.")

    if low in YES_WORDS:
        confirm(chat_id, user_id)
    elif low in NO_WORDS:
        PENDING.pop(user_id, None)
        send(chat_id, "Cancelled — nothing was saved.")
    else:
        revise(chat_id, user_id, pending, text)


# --- read handlers -----------------------------------------------------------

def show_today(chat_id: int) -> None:
    d = today_iso()
    items = entries_for_date(d)
    out = render_agenda(d, items)
    past = overdue_entries()
    if past:
        out += f"\n\n⏰ {len(past)} open item(s) from before today — type \"overdue\"."
    send(chat_id, out)


def show_date(chat_id: int, d: str) -> None:
    send(chat_id, render_agenda(d, entries_for_date(d)))


def show_range(chat_id: int, start: str, end: str) -> None:
    items = entries_in_range(start, end)
    if not items:
        send(chat_id, f"Nothing scheduled between {pretty_date(start)} and {pretty_date(end)}.")
        return
    blocks, current = [], None
    for e in items:
        if e["event_date"] != current:
            current = e["event_date"]
            blocks.append(f"\n── {pretty_date(current)}")
        blocks.append(render_line(e))
    send(chat_id, "📅 Coming up" + "\n".join(blocks))


def show_all(chat_id: int) -> None:
    """Every open entry, grouped by day. No date limit."""
    items = all_open_entries()
    if not items:
        send(chat_id, "Nothing open at all. 🎉")
        return
    blocks, current = [], None
    for e in items:
        if e["event_date"] != current:
            current = e["event_date"]
            rel = relative_day(current)
            label = f"{pretty_date(current)}  ({rel})" if rel else pretty_date(current)
            blocks.append(f"\n── {label}")
        blocks.append(render_line(e))
    send(chat_id, f"📋 All open — {len(items)} item(s)" + "\n".join(blocks))


def show_overdue(chat_id: int) -> None:
    items = overdue_entries()
    if not items:
        send(chat_id, "Nothing overdue. 🎉")
        return
    blocks, current = [], None
    for e in items:
        if e["event_date"] != current:
            current = e["event_date"]
            blocks.append(f"\n── {pretty_date(current)}")
        blocks.append(render_line(e))
    send(chat_id, f"⏰ {len(items)} open item(s) from before today\n" + "\n".join(blocks))


def show_entry(chat_id: int, entry_id: int) -> None:
    e = get_entry(entry_id)
    if not e:
        send(chat_id, f"No entry #{entry_id}.")
        return
    send(chat_id, render_full_entry(e))


def do_search(chat_id: int, query: str) -> None:
    items = search_entries(query)
    if not items:
        send(chat_id, f"Nothing open matching \"{query}\".")
        return
    send(chat_id, f"🔍 {len(items)} match(es) for \"{query}\"\n" + "\n".join(render_line(e) for e in items))


# --- write handlers (all go through a confirmation) --------------------------

def ask_delete(chat_id: int, user_id: int, entry_id: int) -> None:
    e = get_entry(entry_id)
    if not e:
        send(chat_id, f"No entry #{entry_id}.")
        return
    PENDING[user_id] = {"action": "delete", "id": entry_id, "turns": 0, "orig": "delete"}
    send(
        chat_id,
        f"⚠️ Delete #{entry_id}?\n"
        "─────────────\n"
        f"📅 {pretty_date(e['event_date'])}"
        + (f" — {pretty_time(e['event_time'])}" if e.get("event_time") else " — all day")
        + f"\n📝 {e['title']}\n"
        "─────────────\n"
        'Reply "yes" to delete, or "no" to keep it.',
    )


def ask_done(chat_id: int, user_id: int, entry_id: int) -> None:
    e = get_entry(entry_id)
    if not e:
        send(chat_id, f"No entry #{entry_id}.")
        return
    PENDING[user_id] = {"action": "done", "id": entry_id, "turns": 0, "orig": "done"}
    send(
        chat_id,
        f"✅ Mark #{entry_id} as done?\n"
        "─────────────\n"
        f"📅 {pretty_date(e['event_date'])}\n📝 {e['title']}\n"
        "─────────────\n"
        'Reply "yes" to confirm, or "no" to leave it open.',
    )


def ask_edit(chat_id: int, user_id: int, data: dict) -> None:
    entry_id = data.get("id")
    if not isinstance(entry_id, int):
        send(chat_id, "Which entry? Give me the number, e.g. \"edit 12 to 2pm\".")
        return
    e = get_entry(entry_id)
    if not e:
        send(chat_id, f"No entry #{entry_id}.")
        return

    changes = {}
    d = validate_date(data.get("event_date"))
    if d:
        changes["event_date"] = d
    if "event_time" in data:
        changes["event_time"] = validate_time(data.get("event_time"))
    if data.get("title"):
        changes["title"] = str(data["title"]).strip()

    if not changes:
        send(chat_id, "What should I change about it? e.g. \"move 12 to 2pm\".")
        return

    PENDING[user_id] = {
        "action": "edit", "id": entry_id, "changes": changes, "turns": 0, "orig": "edit"
    }

    after = {**e, **changes}
    lines = [
        f"✏️ Change #{entry_id}?",
        "─────────────",
        f"was:  {pretty_date(e['event_date'])}"
        + (f" {pretty_time(e['event_time'])}" if e.get("event_time") else " (all day)")
        + f"  —  {e['title']}",
        f"now:  {pretty_date(after['event_date'])}"
        + (f" {pretty_time(after.get('event_time'))}" if after.get("event_time") else " (all day)")
        + f"  —  {after['title']}",
        "─────────────",
        'Reply "yes" to apply, or "no" to leave it.',
    ]
    send(chat_id, "\n".join(lines))


def confirm_change(chat_id: int, user_id: int, pending: dict) -> None:
    """Apply a delete/done/edit. The only places rows are changed."""
    action = pending["action"]
    entry_id = pending["id"]
    try:
        if action == "delete":
            if delete_entry(entry_id):
                send(chat_id, f"🗑️ Deleted #{entry_id}.")
            else:
                send(chat_id, f"No entry #{entry_id} — nothing to delete.")
        elif action == "done":
            if update_entry(entry_id, status="done"):
                send(chat_id, f"✅ #{entry_id} marked done.")
            else:
                send(chat_id, f"No entry #{entry_id}.")
        elif action == "edit":
            if update_entry(entry_id, **pending["changes"]):
                e = get_entry(entry_id)
                send(chat_id, "✏️ Updated.\n" + render_full_entry(e))
            else:
                send(chat_id, f"No entry #{entry_id}.")
    except Exception as exc:  # noqa: BLE001
        print(f"[db] {action} failed: {exc}", flush=True)
        send(chat_id, f"Sorry — that didn't work ({type(exc).__name__}). Try again.")


# --- the dispatcher ----------------------------------------------------------

def route(chat_id: int, user_id: int, text: str) -> None:
    """Turn one message into one action."""
    if user_id in PENDING:
        pending = PENDING[user_id]
        low = text.strip().lower().rstrip("!.")

        if pending.get("action") == "add":
            handle_pending(chat_id, user_id, text)
            return
        # delete / done / edit are simple yes-no gates
        if low in YES_WORDS:
            PENDING.pop(user_id, None)
            confirm_change(chat_id, user_id, pending)
        elif low in NO_WORDS:
            PENDING.pop(user_id, None)
            send(chat_id, "Cancelled — nothing changed.")
        else:
            send(chat_id, 'Reply "yes" or "no", or send "cancel".')
        return

    # Handy shortcuts that skip the model entirely.
    low = text.strip().lower()
    if low in {"/start", "/help", "help", "?"}:
        send(chat_id, help_text())
        return

    # A bare "yes"/"no" with nothing pending is almost always a stale reply to an
    # old prompt. Say so plainly instead of running it through the model.
    if text.strip().lower().rstrip("!.") in YES_WORDS | NO_WORDS:
        send(chat_id, "Nothing to confirm. What would you like to add?")
        return

    # Greetings: answer locally so they cost nothing and read naturally.
    if low.strip("!.,") in {"hi", "hello", "hey", "yo", "good morning",
                            "good afternoon", "good evening", "morning"}:
        send(
            chat_id,
            "👋 Hello. Ask me for \"today\", or tell me something to add —\n"
            "e.g. \"lunch with sarah tomorrow 1pm\". Type \"help\" for the rest.",
        )
        return

    if low in {"today", "/today", "agenda", "schedule", "today's schedule", "todays schedule"}:
        show_today(chat_id)
        return
    if low in {"tomorrow", "/tomorrow"}:
        show_date(chat_id, (date.today() + timedelta(days=1)).isoformat())
        return
    if low in {"overdue", "/overdue"}:
        show_overdue(chat_id)
        return
    if low in {"list", "this week", "week", "/list"}:
        show_range(chat_id, today_iso(), (date.today() + timedelta(days=7)).isoformat())
        return
    if low in {"all", "all events", "everything", "list all", "show all", "/all"}:
        show_all(chat_id)
        return

    data = call_llm(text)
    intent = data.get("intent")

    if intent == "add":
        ask_add(chat_id, user_id, text, data)
    elif intent == "list":
        # One retrieval path for any date or range: single day == start == end.
        start = validate_date(data.get("start_date")) or validate_date(data.get("event_date"))
        end = validate_date(data.get("end_date")) or start
        if start:
            if start == end:
                show_date(chat_id, start)
            else:
                show_range(chat_id, start, end)
        else:
            show_today(chat_id)
    elif intent == "list_all":
        show_all(chat_id)
    elif intent == "overdue":
        show_overdue(chat_id)
    elif intent == "delete":
        if isinstance(data.get("id"), int):
            ask_delete(chat_id, user_id, data["id"])
        else:
            send(chat_id, "Which entry? Give me the number, e.g. \"delete 12\".")
    elif intent == "done":
        if isinstance(data.get("id"), int):
            ask_done(chat_id, user_id, data["id"])
        else:
            send(chat_id, "Which entry? e.g. \"done 12\".")
    elif intent == "edit":
        ask_edit(chat_id, user_id, data)
    elif intent == "search":
        q = (data.get("query") or "").strip()
        do_search(chat_id, q) if q else send(chat_id, "Search for what?")
    elif intent == "help":
        send(chat_id, help_text())
    elif intent == "other":
        send(
            chat_id,
            "I only handle your schedule — adding, finding, changing and "
            'finishing things. Try "help", or "lunch with sarah tomorrow 1pm".',
        )
    elif intent == "error":
        send(chat_id, f"Sorry, my brain glitched. ({data.get('detail', 'unknown')})")
    else:
        send(chat_id, 'Not sure what you meant. Try "help".')


def handle_message(msg: dict) -> None:
    """One incoming Telegram message. Never raises."""
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    chat_id = chat.get("id")
    user_id = sender.get("id")

    if chat_id is None or user_id is None:
        return

    # ---- the allowlist: everyone else gets silence ----
    if user_id != ALLOWED_USER_ID:
        print(f"[blocked] message from user_id={user_id} ignored", flush=True)
        return

    text = (msg.get("text") or "").strip()
    if not text:
        send(
            chat_id,
            "I can only read text at the moment. Try \"lunch with sarah tomorrow 1pm\".",
        )
        return

    print(f"[msg] {text}", flush=True)
    route(chat_id, user_id, text)


# =============================================================================
# 5. MAIN
# =============================================================================

def selftest() -> None:
    """Prove storage works. No API key, no Telegram, nothing real touched.

    Uses a throwaway database so your real planner.db is never opened.
    """
    global DB_PATH
    original = DB_PATH
    folder = os.path.dirname(original)
    DB_PATH = os.path.join(folder, "selftest.db")
    try:
        # Start from a clean slate: the file we are about to use is our own
        # throwaway, so removing it here is safe and keeps runs independent.
        _remove_file(DB_PATH)
        init_db()
        print(f"database: {DB_PATH}")

        # All test dates are derived from today, so this passes on any day.
        today = date.today()
        future = (today + timedelta(days=8)).isoformat()
        far_future = (today + timedelta(days=32)).isoformat()
        past = (today - timedelta(days=3)).isoformat()

        got = insert_entry(future, "13:00", "Lunch with Sarah (selftest)", "selftest")
        insert_entry(far_future, None, "Submit GST filing (selftest)", "selftest")
        insert_entry(past, None, "Overdue thing (selftest)", "selftest")
        print(f"inserted id={got}  (dated {future})")

        print(f"\nentries for {future}:")
        for e in entries_for_date(future):
            print("  ", render_line(e))

        print("\nall-day handling:")
        for e in entries_for_date(far_future):
            print("  ", render_line(e))

        assert update_entry(got, event_time="12:30", title="Lunch with Sarah Tan")
        row = get_entry(got)
        assert row["event_time"] == "12:30", row
        assert row["title"] == "Lunch with Sarah Tan", row
        print("\nupdated id:", got, "->", row["event_time"], row["title"])

        assert update_entry(got, status="done")
        assert entries_for_date(future) == [], "done item must not show as open"
        print("marked done -> disappears from open lists: ok")

        assert search_entries("GST"), "search should find the GST entry"
        print("search: ok")

        assert len(overdue_entries()) == 1, "only the back-dated entry should be overdue"
        print("overdue detection: ok")

        assert delete_entry(got)
        assert get_entry(got) is None, "row should be gone"
        print("deleted -> gone: ok")

        assert validate_date("2026-13-45") is None
        assert validate_date("tomorrow") is None
        assert validate_date(future) == future
        assert validate_time("7pm") is None
        assert validate_time("07:00") == "07:00"
        assert validate_time("7:5") is None
        assert validate_time(None) is None
        print("date/time validation: ok")

        assert pretty_time("13:00") == "1:00 PM"
        assert pretty_time(None) == "all day"
        assert pretty_time("00:30") == "12:30 AM"
        print("time formatting: ok")

        print("\n✅ all storage checks passed")
    finally:
        DB_PATH = original
        for suffix in ("", "-wal", "-shm"):
            _remove_file(os.path.join(folder, f"selftest.db{suffix}"))


def ask_once(text: str) -> None:
    """Show what the model returns for one phrase. No Telegram needed."""
    print(f"model: {MODEL}")
    print(f"today: {datetime.now().strftime('%Y-%m-%d (%A)')}")
    print(f"input: {text}\n")
    result = call_llm(text)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if result.get("intent") == "add":
        d = validate_date(result.get("event_date"))
        t = validate_time(result.get("event_time"))
        print("\n--- validated ---")
        if d:
            print(f"date : {d}  ({pretty_date(d)})")
            print(f"time : {t or 'all day'}")
        else:
            print("date : REJECTED — would ask the user again")
        print(f"title: {result.get('title')}")


def run_bot() -> None:
    """Long-poll Telegram forever."""
    init_db()

    me = tg("getMe")
    if me:
        ME.update(me)
        print(f"bot: @{me.get('username')} ({me.get('first_name')})", flush=True)
    else:
        sys.exit("Could not reach Telegram. Check TELEGRAM_BOT_TOKEN in .env")

    # Drop any backlog so we act only on new messages.
    pending = tg("getUpdates", offset=-1, timeout=0)
    offset = (pending[-1]["update_id"] + 1) if pending else 0

    print(f"allowed user: {ALLOWED_USER_ID}", flush=True)
    print(f"model: {MODEL}   db: {DB_PATH}", flush=True)
    print("polling... (Ctrl+C to stop)", flush=True)
    notify_owner("📔 Planner is online.")

    while True:
        try:
            updates = tg("getUpdates", offset=offset, timeout=30) or []
            for u in updates:
                offset = u["update_id"] + 1  # ack first: a crash retries nothing
                msg = u.get("message") or u.get("edited_message")
                if msg:
                    try:
                        handle_message(msg)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[handler] {type(exc).__name__}: {exc}", flush=True)
        except KeyboardInterrupt:
            print("\nstopped", flush=True)
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[loop] {type(exc).__name__}: {exc} — retrying in 5s", flush=True)
            time.sleep(5)


def main() -> None:
    args = sys.argv[1:]

    if not args:
        if not TOKEN:
            sys.exit("TELEGRAM_BOT_TOKEN missing from .env")
        if not OPENAI_API_KEY:
            sys.exit("OPENAI_API_KEY missing from .env")
        if not ALLOWED_USER_ID:
            sys.exit("ALLOWED_USER_ID missing from .env")
        run_bot()
        return

    if args[0] == "--init":
        init_db()
        print(f"ready: {DB_PATH}")
    elif args[0] == "--selftest":
        selftest()
    elif args[0] == "--ask":
        if len(args) < 2:
            sys.exit('Usage: python app.py --ask "lunch with sarah tomorrow 1pm"')
        if not OPENAI_API_KEY:
            sys.exit("OPENAI_API_KEY missing from .env")
        ask_once(" ".join(args[1:]))
    elif args[0] == "--count":
        init_db()
        print(f"{count_entries()} entries in {DB_PATH}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
