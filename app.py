import streamlit as st
from utils import analyse_tasks, push_airtable, save_csv, notify

st.title("🧠  Luan’s AI VA")

# ─────────── UI INPUTS ───────────
defaults_clients  = ["ClientA", "ClientB"]
defaults_projects = ["ProjectX", "ProjectY"]

text = st.text_area("Paste or dictate your tasks here:", height=200)
clients  = st.text_input("Client list (comma-separated)",  ", ".join(defaults_clients))
projects = st.text_input("Project list (comma-separated)", ", ".join(defaults_projects))

if st.button("Analyse & Save"):
    if not text.strip():
        st.warning("Please enter some tasks first.")
        st.stop()

    tasks = analyse_tasks(
        st.secrets.openai_api_key,
        text,
        [c.strip() for c in clients.split(",") if c.strip()],
        [p.strip() for p in projects.split(",") if p.strip()],
    )

    st.success(f"{len(tasks)} tasks parsed ✅")
    st.json(tasks)

    # Push into Airtable
    push_airtable(
        st.secrets.airtable_api_key,
        st.secrets.airtable_base_id,
        st.secrets.airtable_table_name,
        tasks
    )
    save_csv(tasks)
    notify(
        st.secrets.telegram_bot_token,
        st.secrets.telegram_chat_id,
        f"✅ {len(tasks)} tasks saved to Airtable!"
    )

    st.toast("All done — check your phone! 📲")
