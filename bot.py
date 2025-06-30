"""
Telegram companion bot for AI-VA
Commands:
  /add  <task sentence>
  /tasks               – last 10 open tasks
  /due  [YYYY-MM-DD]   – tasks due today (default) or a specific date
  /remind <mins> <msg> – one-off reminder
"""
import os, json, asyncio, datetime as dt
import openai
from pyairtable import Table
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

# ─── ENV / secrets ──────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID          = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
AIRTABLE_KEY     = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE    = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE   = os.getenv("AIRTABLE_TABLE_NAME")

from utils import analyse_tasks                 # re-use your own function
table = Table(AIRTABLE_KEY, AIRTABLE_BASE, AIRTABLE_TABLE)

# ─── helpers ────────────────────────────────────────────────────────
def _clean(fields: dict):
    """Return only columns that really exist in Airtable."""
    keep = {
        "Task","Client","Project","Priority","Time","Points",
        "DueDate","Tags","Timer Category","Est. Minutes",
        "Early Bonus","Penalty","Actual Minutes",
    }
    return {k:v for k,v in fields.items() if k in keep and v is not None}

# ─── command handlers ──────────────────────────────────────────────
async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.message.reply_text(
        "Hi!  /add  /tasks  /due  /remind  — that’s all you need.\n"
        "Example:\n"
        "  /add Finish Q3 deck, ClientA, ProjectX, 45m Friday"
    )

async def cmd_add(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sentence = " ".join(ctx.args) if ctx.args else upd.message.text
    if not sentence:
        return await upd.message.reply_text("Usage: /add <task sentence>")
    rows = analyse_tasks(OPENAI_API_KEY, sentence, ["General"], ["General"])
    # personalise: if user sent it, stamp their first name as Client
    rows[0]["Client"] = upd.effective_user.first_name or "TG"
    table.create(_clean(rows[0]))
    t = rows[0]
    await upd.message.reply_text(
        f"✅ Saved *{t['Task']}* → {t['Timer Category']} "
        f"({t['Est. Minutes']} min)", parse_mode="Markdown"
    )

async def cmd_tasks(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    recs = table.all(max_records=10,
                     formula="NOT({Status} = 'Done')",
                     fields=["Task","Timer Category","DueDate"])
    if not recs:
        return await upd.message.reply_text("🎉 No open tasks!")
    msg = "\n".join(
        f"*{i+1}.* {r['fields']['Timer Category']} {r['fields']['Task']} "
        f"– {r['fields'].get('DueDate','')[:10]}"
        for i,r in enumerate(recs)
    )
    await upd.message.reply_text(msg, parse_mode="Markdown")

async def cmd_due(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    date = ctx.args[0] if ctx.args else dt.date.today().isoformat()
    formula = f"DATETIME_FORMAT({{DueDate}}, 'YYYY-MM-DD')='{date}'"
    recs = table.all(formula=formula,
                     fields=["Task","Timer Category"])
    if not recs:
        return await upd.message.reply_text(f"No tasks due on {date}.")
    msg = [f"🗓 *Due {date}*"]
    msg += [f"• {r['fields']['Timer Category']} {r['fields']['Task']}"
            for r in recs]
    await upd.message.reply_text("\n".join(msg), parse_mode="Markdown")

async def cmd_remind(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2 or not ctx.args[0].rstrip("m").isdigit():
        return await upd.message.reply_text(
            "Usage: /remind <minutes> <message>")
    mins = int(ctx.args[0].rstrip("m"))
    msg  = " ".join(ctx.args[1:])
    when = dt.datetime.now() + dt.timedelta(minutes=mins)
    ctx.job_queue.run_once(
        lambda *_: upd.effective_chat.send_message(f"⏰ Reminder: {msg}"),
        when=when,
    )
    await upd.message.reply_text(f"👍 I’ll remind you in {mins} min.")

# ─── daily digest job ───────────────────────────────────────────────
async def morning_digest(app):
    today = dt.date.today().isoformat()
    formula = f"DATETIME_FORMAT({{DueDate}}, 'YYYY-MM-DD')='{today}'"
    recs = table.all(formula=formula, fields=["Task","Timer Category"])
    if not recs: return
    lines = ["🌅 *Today*"]
    lines += [f"• {r['fields']['Timer Category']} {r['fields']['Task']}"
              for r in recs]
    await app.bot.send_message(CHAT_ID, "\n".join(lines),
                               parse_mode="Markdown")

# ─── main loop ──────────────────────────────────────────────────────
async def main():
    openai.api_key = OPENAI_API_KEY
    app = (ApplicationBuilder().token(BOT_TOKEN).build())
    jq = app.job_queue
    jq.run_daily(morning_digest, dt.time(hour=9, minute=0))
    # commands
    app.add_handler(CommandHandler(["start","help"], cmd_start))
    app.add_handler(CommandHandler("add",   cmd_add))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("due",   cmd_due))
    app.add_handler(CommandHandler("remind",cmd_remind))
    # bare text → /add
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_add))
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
