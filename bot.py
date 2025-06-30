# ─── bot.py ─────────────────────────────────────────────────────────────────────
"""
Telegram front-end for the AI Task VA – now with weekly /list,
morning digests, 4-hour nudges and due-date reminders.
"""

from __future__ import annotations
import logging, os, re, textwrap, tomllib, pathlib, time
import datetime as dt
from typing import Any, Dict, List

from telegram import Update, constants
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

# ── pyairtable import that survives any version ────────────────────────────────
try:                                     # classic 1.x
    from pyairtable import Table, ApiError          # type: ignore
except ImportError:                      # 0.x  or  ≥ 2.0
    from pyairtable import Table
    try:
        from pyairtable.formulas import ApiError    # type: ignore
    except ImportError:
        class ApiError(Exception): ...              # dummy fallback

from utils import analyse_tasks                     # GPT splitter

# ── CONFIG ──────────────────────────────────────────────────────────────────────
_cfg = tomllib.loads(pathlib.Path(".streamlit/secrets.toml").read_text())

def _env(key: str, fallback: str) -> str:
    return os.getenv(key, _cfg[fallback])

BOT_TOKEN      = _env("TELEGRAM_BOT_TOKEN",  "telegram_bot_token")
CHAT_ID        = _env("TELEGRAM_CHAT_ID",    "telegram_chat_id")
OPENAI_KEY     = _env("OPENAI_API_KEY",      "openai_api_key")
AIRTABLE_KEY   = _env("AIRTABLE_API_KEY",    "airtable_api_key")
AIRTABLE_BASE  = _env("AIRTABLE_BASE_ID",    "airtable_base_id")
AIRTABLE_TABLE = _env("AIRTABLE_TABLE_NAME", "airtable_table_name")

LOCAL_TZ       = dt.timezone.utc            # change to your TZ if desired

table = Table(AIRTABLE_KEY, AIRTABLE_BASE, AIRTABLE_TABLE)

# ── DECORATORS / HELPERS ───────────────────────────────────────────────────────
def typing_action(fn):
    """Show Telegram ‘typing…’ while we work."""
    async def wrapper(u: Update, c: ContextTypes.DEFAULT_TYPE):
        await c.bot.send_chat_action(u.effective_chat.id,
                                     constants.ChatAction.TYPING)
        return await fn(u, c)
    return wrapper

def _format(rec: Dict[str, Any], *, show_id=False) -> str:
    f = rec["fields"]
    prefix = "✅" if f.get("Done") else "•"
    uid    = f"#{rec['id'][:4]} " if show_id else ""
    due    = f.get("DueDate", "—")
    return f"{prefix} {uid}{f['Task']}  _({due})_"

def _parse_flags(txt: str) -> tuple[str, Dict[str, Any]]:
    extra: Dict[str, Any] = {}
    m = re.search(r"--p(?:riority)?\s+(high|medium|low)", txt, re.I)
    if m:
        extra["Priority"] = m.group(1).capitalize()
        txt = txt.replace(m.group(0), "")
    m = re.search(r"--due\s+(\d{4}-\d{2}-\d{2})", txt)
    if m:
        extra["DueDate"] = m.group(1)
        txt = txt.replace(m.group(0), "")
    return txt.strip(), extra

# ── SCHEDULER CALL-BACKS ───────────────────────────────────────────────────────
async def _after_4h(ctx: ContextTypes.DEFAULT_TYPE):
    task = ctx.job.data["task"]
    await ctx.bot.send_message(CHAT_ID,
                               f"⏰ 4-hour nudge: don’t forget *{task}*!",
                               parse_mode="Markdown")

async def _due_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    task  = ctx.job.data["task"]
    label = ctx.job.data["label"]
    await ctx.bot.send_message(CHAT_ID,
                               f"🗓 {label}: *{task}*",
                               parse_mode="Markdown")

async def _morning(ctx: ContextTypes.DEFAULT_TYPE):
    today = dt.date.today().isoformat()
    recs  = table.all(formula=f"AND({{DueDate}} = '{today}', {{Done}} != 1)")
    if not recs:
        msg = "Good morning! Nothing due today – seize the day! 🏆"
    else:
        lines = "\n".join(_format(r) for r in recs)
        msg = f"Hey, good morning. Veni Vidi Vici!  Here’s today’s tasks:\n{lines}"
    await ctx.bot.send_message(CHAT_ID, msg, parse_mode="Markdown")

# ── COMMANDS ────────────────────────────────────────────────────────────────────
HELP = textwrap.dedent("""\
*Commands*
/add <task> [--p high|medium|low] [--due YYYY-MM-DD]
/list               – tasks due *this week*
/today              – tasks due today
/done  <id>         – mark done
/delete <id>        – remove
/ping               – latency
/help               – this screen
""")

async def start(u: Update, _: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("👋 Hi! I’m your Task bot.\n" + HELP,
                               parse_mode="Markdown")

async def ping(u: Update, _: ContextTypes.DEFAULT_TYPE):
    t0 = time.time()
    await u.message.reply_text("🏓 pong")
    await u.message.reply_text(f"⏱ {(time.time()-t0)*1000:,.0f} ms")

# -------------------------------------------------------------------- /add -----
@typing_action
async def add(u: Update, c: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(c.args)
    if not raw:
        await u.message.reply_text("⚠️  Use `/add buy milk`", parse_mode="Markdown")
        return

    task_txt, extras = _parse_flags(raw)
    try:
        rows = analyse_tasks(
            OPENAI_KEY, task_txt,
            clients=["General"], projects=["General"]
        )
    except Exception as e:
        logging.exception(e)
        await u.message.reply_text("❌ GPT couldn’t parse that.")
        return

    for row in rows:
        row.update(extras); row.setdefault("Done", False)
        try:
            table.create(row)
        except ApiError as e:
            logging.exception(e)
            await u.message.reply_text("❌ Airtable refused that row.")
            return

        # schedule nudges & reminders
        jobq = c.job_queue
        added = dt.datetime.now(tz=LOCAL_TZ)
        jobq.run_once(_after_4h, 4*60*60,  data={"task": row["Task"]})

        if row.get("DueDate"):
            due_d = dt.datetime.fromisoformat(row["DueDate"]).date()
            day_before = dt.datetime.combine(
                due_d - dt.timedelta(days=1), dt.time(9, tzinfo=LOCAL_TZ))
            jobq.run_once(_due_reminder, day_before.timestamp(),
                          data={"task": row["Task"], "label": "⏳ due *tomorrow*"})
            jobq.run_once(_due_reminder,
                          dt.datetime.combine(due_d, dt.time(9, tzinfo=LOCAL_TZ)).timestamp(),
                          data={"task": row["Task"], "label": "🚨 due *today*"})
            jobq.run_once(_due_reminder,
                          dt.datetime.combine(due_d - dt.timedelta(days=2),
                                              dt.time(9, tzinfo=LOCAL_TZ)).timestamp(),
                          data={"task": row["Task"], "label": "⚠️ due in 2 days"})

    await u.message.reply_text(f"✅ *{len(rows)}* task(s) added!",
                               parse_mode="Markdown")

# -------------------------------------------------------------------- /list ----
@typing_action
async def list_tasks(u: Update, _: ContextTypes.DEFAULT_TYPE):
    """Show all open tasks due *this week*, grouped by Client."""
    today      = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())   # Monday
    week_end   = week_start + dt.timedelta(days=6)            # Sunday

    try:
        recs = table.all(formula="{Done} != 1")
    except ApiError as e:
        logging.exception(e)
        await u.message.reply_text("❌ Airtable unreachable."); return

    # keep only those inside the current week
    def _in_week(r):
        due = r["fields"].get("DueDate")
        if not due:  # tasks without date aren’t listed
            return False
        try:
            d = dt.date.fromisoformat(due)
        except ValueError:
            return False
        return week_start <= d <= week_end

    week_recs = [r for r in recs if _in_week(r)]
    if not week_recs:
        await u.message.reply_text("📭 No tasks due this week – you’re clear!")
        return

    # group → {Client: [records]}
    grouped: Dict[str, List[Any]] = {}
    for r in week_recs:
        client = r["fields"].get("Client", "General")
        grouped.setdefault(client, []).append(r)

    weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    blocks: List[str] = []

    for client in sorted(grouped.keys()):
        lines: List[str] = [f"*{client}*"]
        for r in sorted(grouped[client], key=lambda x: x["fields"]["DueDate"]):
            f  = r["fields"]
            d  = dt.date.fromisoformat(f["DueDate"])
            rel = ("today"      if d == today else
                   "tomorrow"   if d == today + dt.timedelta(days=1) else
                   f"this {weekday[d.weekday()]}" if 0 < (d - today).days <= 6
                   else weekday[d.weekday()])
            nice = d.strftime("%d %b %Y")
            lines.append(
                f"• {f['Task']} – {f.get('Priority','—')} – {nice} ({rel})"
            )
        blocks.append("\n".join(lines))

    await u.message.reply_text("\n\n".join(blocks), parse_mode="Markdown")

# ---------------------------------------------------------------- other cmds ---
@typing_action
async def today(u: Update, _: ContextTypes.DEFAULT_TYPE):
    recs = table.all(formula=f"AND({{DueDate}} = '{dt.date.today().isoformat()}', {{Done}} != 1)")
    if not recs:
        await u.message.reply_text("😎 Nothing due today."); return
    await u.message.reply_text("\n".join(_format(r, show_id=True) for r in recs),
                               parse_mode="Markdown")

async def _by_id(uid: str):
    for r in table.all():
        if r["id"].startswith(uid): return r

async def done(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not c.args: await u.message.reply_text("`/done <id>`", parse_mode="Markdown"); return
    rec = await _by_id(c.args[0].lstrip("#"))
    if not rec: await u.message.reply_text("❌ ID not found."); return
    table.update(rec["id"], {"Done": True}); await u.message.reply_text("✅ Done!")

async def delete(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not c.args: await u.message.reply_text("`/delete <id>`", parse_mode="Markdown"); return
    rec = await _by_id(c.args[0].lstrip("#"))
    if not rec: await u.message.reply_text("❌ ID not found."); return
    table.delete(rec["id"]); await u.message.reply_text("🗑️ Deleted!")

async def unknown(u: Update, _: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🤖 Unknown command. `/help`")

# ── MAIN ────────────────────────────────────────────────────────────────────────
def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start","help"], start))
    app.add_handler(CommandHandler("ping",    ping))
    app.add_handler(CommandHandler("add",     add))
    app.add_handler(CommandHandler("list",    list_tasks))
    app.add_handler(CommandHandler("today",   today))
    app.add_handler(CommandHandler("done",    done))
    app.add_handler(CommandHandler("delete",  delete))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    # daily 08:00 digest
    app.job_queue.run_daily(_morning, time=dt.time(8, 0, tzinfo=LOCAL_TZ))

    print("🤖 Bot is up. Ctrl-C to stop.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
