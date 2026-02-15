import os
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands
from openai import OpenAI

# ============================================================
# Config
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # optional but recommended
DB_PATH = os.getenv("DB_PATH", "interviews.db")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # fast + cheap default

# IMPORTANT: update if your paths differ
SKILL_PATH = os.getenv(
    "SKILL_PATH",
    "/Users/rosanna/.openclaw/workspace/admissions-interviewer/SKILL.md"
)
RUBRIC_PATH = os.getenv(
    "RUBRIC_PATH",
    "/Users/rosanna/.openclaw/workspace/admissions-interviewer/references/rubric.md"
)

MAX_TURNS = 10
TARGET_CATEGORIES = [
    "communication_clarity",
    "motivation_purpose",
    "self_awareness_reflection",
    "academic_program_fit",
    "leadership_initiative",
    "integrity_professionalism"
]

OPENING_QUESTION = (
    "Thanks for joining today. To start, tell me about yourself and why you're interested in this program."
)

# ============================================================
# Utilities
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def read_file_safe(path: str, fallback: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return fallback

def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        status TEXT NOT NULL, -- active|ended
        question_index INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL,
        ended_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        role TEXT NOT NULL, -- interviewer|candidate|system
        author_id TEXT,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS session_state (
        session_id INTEGER PRIMARY KEY,
        resume_text TEXT DEFAULT '',
        turn_count INTEGER DEFAULT 0,
        coverage_json TEXT DEFAULT '{}',
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        result_text TEXT NOT NULL,
        result_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )
    """)

    conn.commit()
    conn.close()

def get_active_session(channel_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
      SELECT id, candidate_id, question_index
      FROM sessions
      WHERE channel_id=? AND status='active'
      ORDER BY id DESC
      LIMIT 1
    """, (str(channel_id),))
    row = cur.fetchone()
    conn.close()
    return row  # (id, candidate_id, question_index) or None

def get_last_session(channel_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
      SELECT id, candidate_id
      FROM sessions
      WHERE channel_id=?
      ORDER BY id DESC
      LIMIT 1
    """, (str(channel_id),))
    row = cur.fetchone()
    conn.close()
    return row  # (id, candidate_id) or None

def add_message(session_id: int, role: str, content: str, author_id: Optional[str] = None):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO messages(session_id, role, author_id, content, created_at)
      VALUES (?, ?, ?, ?, ?)
    """, (session_id, role, author_id, content, now_iso()))
    conn.commit()
    conn.close()

def fetch_transcript_rows(session_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
      SELECT role, author_id, content, created_at
      FROM messages
      WHERE session_id=?
      ORDER BY id ASC
    """, (session_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def transcript_text(session_id: int) -> str:
    rows = fetch_transcript_rows(session_id)
    return "\n".join([f"[{r[3]}] {r[0].upper()}: {r[2]}" for r in rows])

def default_coverage():
    return {k: {"covered": False, "evidence_count": 0} for k in TARGET_CATEGORIES}

def get_or_create_state(session_id: int) -> Dict[str, Any]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
      SELECT session_id, resume_text, turn_count, coverage_json
      FROM session_state
      WHERE session_id=?
    """, (session_id,))
    row = cur.fetchone()

    if not row:
        cov = default_coverage()
        cur.execute("""
          INSERT INTO session_state(session_id, resume_text, turn_count, coverage_json)
          VALUES (?, '', 0, ?)
        """, (session_id, json.dumps(cov)))
        conn.commit()
        row = (session_id, "", 0, json.dumps(cov))

    conn.close()
    return {
        "session_id": row[0],
        "resume_text": row[1] or "",
        "turn_count": int(row[2] or 0),
        "coverage": json.loads(row[3] or "{}")
    }

def save_state(session_id: int, resume_text: str, turn_count: int, coverage: Dict[str, Any]):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
      UPDATE session_state
      SET resume_text=?, turn_count=?, coverage_json=?
      WHERE session_id=?
    """, (resume_text, turn_count, json.dumps(coverage), session_id))
    conn.commit()
    conn.close()

def enough_coverage(coverage: Dict[str, Any]) -> bool:
    covered_count = sum(
        1 for k in TARGET_CATEGORIES if coverage.get(k, {}).get("covered") is True
    )
    return covered_count >= 5

# ============================================================
# OpenAI helpers
# ============================================================

if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY env var")

client = OpenAI(api_key=OPENAI_API_KEY)

SKILL_TEXT = read_file_safe(SKILL_PATH, fallback="(SKILL.md not found)")
RUBRIC_TEXT = read_file_safe(RUBRIC_PATH, fallback="(rubric.md not found)")

def safe_json_parse(text: str) -> Dict[str, Any]:
    text = text.strip()
    # try raw JSON
    try:
        return json.loads(text)
    except Exception:
        pass
    # try code block extraction
    if "```" in text:
        parts = text.split("```")
        for p in parts:
            p2 = p.strip()
            if p2.startswith("json"):
                p2 = p2[4:].strip()
            try:
                return json.loads(p2)
            except Exception:
                continue
    raise ValueError(f"Could not parse JSON from model output:\n{text[:500]}")

def generate_next_question(session_id: int, latest_candidate_answer: str) -> str:
    state = get_or_create_state(session_id)
    tr = transcript_text(session_id)

    prompt = f"""
You are an adaptive college admissions interviewer.

Use these policy docs:
--- SKILL.md ---
{SKILL_TEXT[:12000]}
--- rubric.md ---
{RUBRIC_TEXT[:12000]}

Current interview state:
- turn_count: {state["turn_count"]}
- max_turns: {MAX_TURNS}
- coverage_json: {json.dumps(state["coverage"], ensure_ascii=False)}

Transcript:
{tr[-12000:]}

Latest candidate answer:
{latest_candidate_answer}

Task:
1) Update coverage based on transcript evidence.
2) Ask exactly ONE high-value next question.
3) Prioritize uncovered/weak categories.
4) If candidate made vague/inflated claims, ask for concrete verification.
5) Keep question concise and natural.

Return STRICT JSON only:
{{
  "question": "string",
  "coverage_update": {{
    "communication_clarity": {{"covered": true, "evidence_count": 1}},
    "motivation_purpose": {{"covered": false, "evidence_count": 0}},
    "self_awareness_reflection": {{"covered": false, "evidence_count": 0}},
    "academic_program_fit": {{"covered": false, "evidence_count": 0}},
    "leadership_initiative": {{"covered": false, "evidence_count": 0}},
    "integrity_professionalism": {{"covered": false, "evidence_count": 0}}
  }},
  "should_end": false
}}
"""

    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=prompt,
        temperature=0.4
    )
    out = resp.output_text
    data = safe_json_parse(out)

    coverage = data.get("coverage_update", state["coverage"])
    turn_count = state["turn_count"] + 1
    save_state(session_id, state["resume_text"], turn_count, coverage)

    question = data.get("question", "Can you expand on that with a specific example?")
    return question

def run_final_evaluation(session_id: int, candidate_id: str) -> Dict[str, Any]:
    state = get_or_create_state(session_id)
    tr = transcript_text(session_id)
    resume_text = state["resume_text"]

    prompt = f"""
You are a college admissions evaluator.

Use these policy docs:
--- SKILL.md ---
{SKILL_TEXT[:15000]}
--- rubric.md ---
{RUBRIC_TEXT[:15000]}

Candidate ID: {candidate_id}

Resume:
{resume_text[:12000]}

Interview Transcript:
{tr[:18000]}

Return STRICT JSON only with this schema:
{{
  "candidate_id": "{candidate_id}",
  "scores": {{
    "communication_clarity": 0,
    "motivation_purpose": 0,
    "self_awareness_reflection": 0,
    "academic_program_fit": 0,
    "leadership_initiative": 0,
    "integrity_professionalism": 0,
    "resilience_adaptability": null
  }},
  "evidence": [
    {{
      "category": "motivation_purpose",
      "quote": "direct quote",
      "timestamp": "optional",
      "source": "interview_transcript|resume|notes"
    }}
  ],
  "strengths": [],
  "concerns": [],
  "recommendation": "Admit|Borderline|Reject|Insufficient Data",
  "confidence": "High|Medium|Low",
  "bias_safety_note": "Evaluation excludes protected-attribute inference and requires human review."
}}
Rules:
- Evidence-based only.
- At least one evidence item per scored category.
- No protected-attribute inference.
"""

    resp = client.responses.create(
        model=OPENAI_MODEL,
        input=prompt,
        temperature=0.2
    )
    out = resp.output_text
    return safe_json_parse(out)

# ============================================================
# Discord bot setup
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        print(f"Synced slash commands to guild {GUILD_ID}")
    else:
        await tree.sync()
        print("Synced global slash commands")

# --------------------------
# Slash commands
# --------------------------

@tree.command(name="start_interview", description="Start an adaptive interview session")
@app_commands.describe(candidate_id="e.g., ETHANLAM", candidate="Optional: candidate user to invite into thread")
async def start_interview(interaction: discord.Interaction, candidate_id: str, candidate: Optional[discord.Member] = None):
    active = get_active_session(interaction.channel_id)
    if active:
        await interaction.response.send_message(
            "An active interview already exists in this channel. End it first with `/end_interview`.",
            ephemeral=True
        )
        return

    # Try to create a dedicated thread per candidate
    interview_channel_id = interaction.channel_id
    created_thread = None
    try:
        if isinstance(interaction.channel, discord.TextChannel):
            thread_name = f"interview-{candidate_id}"[:95]
            created_thread = await interaction.channel.create_thread(
                name=thread_name,
                auto_archive_duration=1440,
                reason=f"Admissions interview for {candidate_id}"
            )
            # discord.py returns ThreadWithMessage in some versions
            if hasattr(created_thread, "thread"):
                created_thread = created_thread.thread
            interview_channel_id = created_thread.id
    except Exception:
        created_thread = None
        interview_channel_id = interaction.channel_id

    conn = db()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO sessions(candidate_id, channel_id, status, question_index, started_at)
      VALUES (?, ?, 'active', 0, ?)
    """, (candidate_id, str(interview_channel_id), now_iso()))
    session_id = cur.lastrowid
    conn.commit()
    conn.close()

    _ = get_or_create_state(session_id)
    add_message(session_id, "interviewer", OPENING_QUESTION)

    if created_thread:
        invited_msg = ""
        if candidate is not None:
            try:
                await created_thread.add_user(candidate)
                invited_msg = f" Invited {candidate.mention} to the thread."
            except Exception:
                invited_msg = f" Could not auto-invite {candidate.mention}; add them manually from thread members."

        kickoff = f"Interview started for **{candidate_id}**."
        if candidate is not None:
            kickoff += f" {candidate.mention}"
        kickoff += f"\n\n**Q1:** {OPENING_QUESTION}"

        await created_thread.send(kickoff)
        await interaction.response.send_message(
            f"Created thread {created_thread.mention} for **{candidate_id}**.{invited_msg} Continue interview there.",
            ephemeral=False
        )
    else:
        fallback_msg = f"Interview started for **{candidate_id}** (thread creation unavailable)."
        if candidate is not None:
            fallback_msg += f" Please interview with {candidate.mention} in this channel."
        fallback_msg += f"\n\n**Q1:** {OPENING_QUESTION}"
        await interaction.response.send_message(fallback_msg)

@tree.command(name="set_resume", description="Attach candidate resume text to active interview")
@app_commands.describe(resume="Paste full resume text")
async def set_resume(interaction: discord.Interaction, resume: str):
    active = get_active_session(interaction.channel_id)
    if not active:
        await interaction.response.send_message("No active interview in this channel.", ephemeral=True)
        return

    session_id = active[0]
    st = get_or_create_state(session_id)
    save_state(session_id, resume, st["turn_count"], st["coverage"])
    await interaction.response.send_message("Resume saved for this interview session.")

@tree.command(name="end_interview", description="End the active interview session")
async def end_interview(interaction: discord.Interaction):
    active = get_active_session(interaction.channel_id)
    if not active:
        await interaction.response.send_message("No active interview in this channel.", ephemeral=True)
        return

    session_id = active[0]
    conn = db()
    cur = conn.cursor()
    cur.execute("""
      UPDATE sessions
      SET status='ended', ended_at=?
      WHERE id=?
    """, (now_iso(), session_id))
    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"Interview ended (session #{session_id}). Run `/evaluate` for scoring."
    )

@tree.command(name="export_transcript", description="Export transcript from current/last session")
async def export_transcript(interaction: discord.Interaction):
    active = get_active_session(interaction.channel_id)
    if active:
        session_id = active[0]
    else:
        last = get_last_session(interaction.channel_id)
        if not last:
            await interaction.response.send_message("No session found in this channel.", ephemeral=True)
            return
        session_id = last[0]

    tr = transcript_text(session_id)
    tr = tr[:1800] if len(tr) > 1800 else tr
    await interaction.response.send_message(f"Transcript (session #{session_id}):\n```{tr}```")

@tree.command(name="evaluate", description="Run final rubric evaluation on current/last session")
async def evaluate(interaction: discord.Interaction):
    active = get_active_session(interaction.channel_id)
    if active:
        session_id, candidate_id, _ = active
    else:
        last = get_last_session(interaction.channel_id)
        if not last:
            await interaction.response.send_message("No interview session found in this channel.", ephemeral=True)
            return
        session_id, candidate_id = last

    await interaction.response.defer(thinking=True)

    try:
        result = run_final_evaluation(session_id, candidate_id)
        result_text = (
            f"**Evaluation for {candidate_id}**\n"
            f"- Recommendation: **{result.get('recommendation', 'N/A')}**\n"
            f"- Confidence: **{result.get('confidence', 'N/A')}**\n\n"
            f"```json\n{json.dumps(result, indent=2, ensure_ascii=False)}\n```"
        )

        conn = db()
        cur = conn.cursor()
        cur.execute("""
          INSERT INTO evaluations(session_id, result_text, result_json, created_at)
          VALUES (?, ?, ?, ?)
        """, (session_id, result_text, json.dumps(result, ensure_ascii=False), now_iso()))
        conn.commit()
        conn.close()

        # Discord message limit safe split
        if len(result_text) <= 1900:
            await interaction.followup.send(result_text)
        else:
            await interaction.followup.send(result_text[:1900])
            await interaction.followup.send(f"```json\n{json.dumps(result, indent=2, ensure_ascii=False)[:1900]}\n```")

    except Exception as e:
        await interaction.followup.send(f"Evaluation failed: {e}")

# --------------------------
# Message handler: adaptive flow
# --------------------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    active = get_active_session(message.channel.id)
    if active:
        session_id, candidate_id, _ = active

        # Save candidate answer
        add_message(session_id, "candidate", message.content, str(message.author.id))

        # Generate next question adaptively
        st = get_or_create_state(session_id)
        if st["turn_count"] >= MAX_TURNS or enough_coverage(st["coverage"]):
            await message.channel.send(
                "Thanks — we now have enough evidence. Please run `/end_interview`, then `/evaluate`."
            )
        else:
            try:
                question = generate_next_question(session_id, message.content)
            except Exception:
                question = "Thanks. Can you give a concrete example with your exact role, actions, and measurable outcome?"

            add_message(session_id, "interviewer", question)
            await message.channel.send(question)

    await bot.process_commands(message)

# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var")
    init_db()
    bot.run(DISCORD_TOKEN)
