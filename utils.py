import json
import pandas as pd
from typing import List, Dict, Any
from datetime import datetime
from dateutil import parser as date_parser

from openai import OpenAI, OpenAIError
from pyairtable import Table
from telegram import Bot

# ──────────── 1. Timer Category Logic ────────────
CATEGORIES = [
    "🕑 Lifeline",
    "⚡ Quick Task",
    "🔹 Small Task",
    "🔸 Focused Sprint",
    "🔶 1 Hour Challenge",
    "🔥 Deep Work",
]

def _bucket(mins: int) -> str:
    if mins <= 1.5:
        return "🕑 Lifeline"
    if mins <= 5:
        return "⚡ Quick Task"
    if mins <= 10:
        return "🔹 Small Task"
    if mins <= 25:
        return "🔸 Focused Sprint"
    if mins <= 60:
        return "🔶 1 Hour Challenge"
    return "🔥 Deep Work"

# ──────────── 2. GPT → structured rows ────────────
def analyse_tasks(
    api_key: str,
    text: str,
    clients: List[str],
    projects: List[str],
    model: str = "gpt-4o-mini",
) -> List[Dict[str, Any]]:
    """
    Parse free-form text into Airtable-ready rows.
    Enforce Timer Category by Est. Minutes, and parse any DueDate text into ISO.
    """
    prompt = (
        "You are a productivity-game assistant.\n"
        f"Allowed clients: {', '.join(clients)}\n"
        f"Allowed projects: {', '.join(projects)}\n"
        "For each task sentence, return JSON objects with EXACTLY these keys:\n"
        "  Task, Client, Project, Est. Minutes, Timer Category, Early Bonus, Penalty, Actual Minutes, DueDate (optional)\n"
        "Do NOT include any extra keys or prose.\n\n"
        "TEXT:\n"
        f"{text}\n"
    )

    try:
        client = OpenAI(api_key=api_key)
        res = client.chat.completions.create(
            model=model,
            messages=[{"role":"user","content":prompt}],
            response_format={"type":"json_object"},
            temperature=0.1,
        )
    except OpenAIError as e:
        raise RuntimeError(f"OpenAI error → {e}") from e

    data = json.loads(res.choices[0].message.content)

    # unwrap if GPT nested under "tasks"
    if isinstance(data, dict) and "tasks" in data:
        data = data["tasks"]
    if isinstance(data, dict):
        data = [data]

    for row in data:
        # 1) Est. Minutes → integer
        mins = int(row.get("Est. Minutes", 0) or 0)
        row["Est. Minutes"] = mins

        # 2) Timer Category enforced by bucket
        row["Timer Category"] = _bucket(mins)

        # 3) Defaults
        row.setdefault("Client", "General")
        row.setdefault("Project", "General")
        row.setdefault("Early Bonus", 0)
        row.setdefault("Penalty", 0)
        row.setdefault("Actual Minutes", None)

        # 4) Parse DueDate if present
        raw_due = row.pop("DueDate", None) or row.pop("Due Date", None)
        if raw_due:
            try:
                dt = date_parser.parse(raw_due, default=datetime.now())
                row["DueDate"] = dt.date().isoformat()
            except Exception:
                row["DueDate"] = None
        else:
            row["DueDate"] = None

    return data

# ──────────── 3. Airtable / CSV / Telegram ────────────
MUTABLE_FIELDS = {
    "Task", "Client", "Project", "Timer Category", "Est. Minutes",
    "Early Bonus", "Penalty", "Actual Minutes", "DueDate",
}

def push_airtable(
    api_key: str,
    base_id: str,
    table_name: str,
    rows: List[Dict[str, Any]]
) -> None:
    table = Table(api_key, base_id, table_name)
    for r in rows:
        clean = {k: v for k, v in r.items() if k in MUTABLE_FIELDS and v is not None}
        table.create(clean)

def save_csv(rows: List[Dict[str, Any]], fname: str = "tasks.csv") -> None:
    pd.DataFrame(rows).to_csv(fname, index=False)

def notify(bot_token: str, chat_id: str, msg: str) -> None:
    Bot(bot_token).send_message(chat_id=chat_id, text=msg)
