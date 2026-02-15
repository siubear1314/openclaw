import os
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from urllib.parse import quote_plus

import requests

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
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")  # optional web search for candidate questions

# Profile paths (switchable at runtime)
ADMISSIONS_SKILL_PATH = "/Users/rosanna/.openclaw/workspace/admissions-interviewer/SKILL.md"
ADMISSIONS_RUBRIC_PATH = "/Users/rosanna/.openclaw/workspace/admissions-interviewer/references/rubric.md"
AI_TECH_ZH_SKILL_PATH = "/Users/rosanna/.openclaw/workspace/ai-technical-interviewer-zh/SKILL.md"
AI_TECH_ZH_RUBRIC_PATH = "/Users/rosanna/.openclaw/workspace/ai-technical-interviewer-zh/references/rubric.md"

PROFILE_MAP = {
    "admissions": {
        "skill": ADMISSIONS_SKILL_PATH,
        "rubric": ADMISSIONS_RUBRIC_PATH,
    },
    "ai-tech-zh": {
        "skill": AI_TECH_ZH_SKILL_PATH,
        "rubric": AI_TECH_ZH_RUBRIC_PATH,
    },
}

DEFAULT_PROFILE = os.getenv("INTERVIEW_PROFILE", "admissions")

MAX_TURNS = 20
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
    return covered_count >= 6

def get_recent_interviewer_questions(session_id: int, limit: int = 8):
    rows = fetch_transcript_rows(session_id)
    qs = [r[2].strip() for r in rows if r[0] == "interviewer" and r[2].strip()]
    return qs[-limit:]

def _norm_text(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum() or ch.isspace()).strip()

def is_similar_question(a: str, b: str) -> bool:
    a_n, b_n = _norm_text(a), _norm_text(b)
    if not a_n or not b_n:
        return False
    if a_n in b_n or b_n in a_n:
        return True
    a_set, b_set = set(a_n.split()), set(b_n.split())
    if not a_set or not b_set:
        return False
    overlap = len(a_set & b_set) / max(1, len(a_set | b_set))
    return overlap >= 0.65

def fallback_question_for_coverage(coverage: Dict[str, Any]) -> str:
    if not coverage.get("motivation_purpose", {}).get("covered"):
        return "What specifically about this program matches your goals, and why?"
    if not coverage.get("academic_program_fit", {}).get("covered"):
        return "Which course or faculty fit you best, and what preparation proves you can succeed?"
    if not coverage.get("leadership_initiative", {}).get("covered"):
        return "Describe one leadership example with your exact actions and measurable impact."
    if not coverage.get("self_awareness_reflection", {}).get("covered"):
        return "Tell me about a failure, what you changed, and the concrete result after that change."
    if not coverage.get("integrity_professionalism", {}).get("covered"):
        return "Describe a time you faced an ethical choice and how you made the decision."
    return "Give one concrete example that best shows why we should admit you." 

# ============================================================
# OpenAI helpers
# ============================================================

if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY env var")

client = OpenAI(api_key=OPENAI_API_KEY)

ACTIVE_PROFILE = DEFAULT_PROFILE if DEFAULT_PROFILE in PROFILE_MAP else "admissions"
ACTIVE_SKILL_PATH = PROFILE_MAP[ACTIVE_PROFILE]["skill"]
ACTIVE_RUBRIC_PATH = PROFILE_MAP[ACTIVE_PROFILE]["rubric"]
SKILL_TEXT = read_file_safe(ACTIVE_SKILL_PATH, fallback="(SKILL.md not found)")
RUBRIC_TEXT = read_file_safe(ACTIVE_RUBRIC_PATH, fallback="(rubric.md not found)")

def set_active_profile(profile: str) -> bool:
    global ACTIVE_PROFILE, ACTIVE_SKILL_PATH, ACTIVE_RUBRIC_PATH, SKILL_TEXT, RUBRIC_TEXT
    if profile not in PROFILE_MAP:
        return False
    ACTIVE_PROFILE = profile
    ACTIVE_SKILL_PATH = PROFILE_MAP[profile]["skill"]
    ACTIVE_RUBRIC_PATH = PROFILE_MAP[profile]["rubric"]
    SKILL_TEXT = read_file_safe(ACTIVE_SKILL_PATH, fallback="(SKILL.md not found)")
    RUBRIC_TEXT = read_file_safe(ACTIVE_RUBRIC_PATH, fallback="(rubric.md not found)")
    return True

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

def candidate_asked_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False

    # Strong explicit markers
    if t.startswith("question:") or t.startswith("q:"):
        return True
    if "i have a question" in t or "can i ask" in t or "quick question" in t:
        return True

    # Ends with question mark and not too long (avoids classifying long answers with one '?')
    if t.endswith("?") and len(t.split()) <= 28:
        return True

    # Interrogative start with short sentence only
    interrogatives = (
        "can ", "could ", "would ", "will ", "what ", "how ", "why ",
        "when ", "where ", "which ", "is ", "are ", "do ", "does ", "did "
    )
    if t.startswith(interrogatives) and len(t.split()) <= 24:
        return True

    return False

def brave_search(query: str, count: int = 3) -> str:
    if not BRAVE_API_KEY:
        return ""
    try:
        url = f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(query)}&count={count}"
        headers = {"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}
        r = requests.get(url, headers=headers, timeout=8)
        r.raise_for_status()
        data = r.json()
        results = (data.get("web", {}) or {}).get("results", [])[:count]
        lines = []
        for it in results:
            title = it.get("title", "")
            desc = it.get("description", "")
            url = it.get("url", "")
            lines.append(f"- {title}: {desc} ({url})")
        return "\n".join(lines)
    except Exception:
        return ""

def answer_candidate_question(question: str, session_id: int) -> str:
    tr = transcript_text(session_id)
    snippets = brave_search(question)
    prompt = f"""
You are the interviewer. Candidate asked a question during interview.
Reply in <= 80 words, clear and practical. If unsure, say so.

Candidate question:
{question}

Interview context:
{tr[-4000:]}

Optional web search snippets:
{snippets if snippets else '(none)'}
"""
    try:
        resp = client.responses.create(model=OPENAI_MODEL, input=prompt, temperature=0.2)
        return (resp.output_text or "Good question. I’ll note it and we can revisit at the end.").strip()
    except Exception:
        return "Good question. I can’t verify that right now, but I’ll note it and we can revisit at the end."

def generate_next_question(session_id: int, latest_candidate_answer: str) -> str:
    state = get_or_create_state(session_id)
    tr = transcript_text(session_id)
    recent_questions = get_recent_interviewer_questions(session_id)

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
- recent_questions: {json.dumps(recent_questions, ensure_ascii=False)}

Transcript:
{tr[-12000:]}

Latest candidate answer:
{latest_candidate_answer}

Task:
1) Update coverage based on transcript evidence.
2) Ask exactly ONE high-value next question.
3) Prioritize uncovered/weak categories.
4) If candidate made vague/inflated claims, ask for concrete verification.
5) Keep the question short: <= 18 words, no preamble, no two-part question.
6) Do NOT repeat or paraphrase any question in recent_questions.
7) Make interview fast: move forward when a category already has enough evidence.

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
        temperature=0.2
    )
    out = resp.output_text
    data = safe_json_parse(out)

    coverage = data.get("coverage_update", state["coverage"])
    turn_count = state["turn_count"] + 1
    save_state(session_id, state["resume_text"], turn_count, coverage)

    question = data.get("question", "Give one concrete example with your actions and measurable impact.")
    if any(is_similar_question(question, q) for q in recent_questions):
        question = fallback_question_for_coverage(coverage)
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
    print(f"Active profile: {ACTIVE_PROFILE} | skill={ACTIVE_SKILL_PATH} | rubric={ACTIVE_RUBRIC_PATH}")
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

@tree.command(name="set_profile", description="Switch interviewer profile")
@app_commands.describe(profile="admissions or ai-tech-zh")
@app_commands.choices(profile=[
    app_commands.Choice(name="admissions", value="admissions"),
    app_commands.Choice(name="ai-tech-zh", value="ai-tech-zh"),
])
async def set_profile(interaction: discord.Interaction, profile: app_commands.Choice[str]):
    ok = set_active_profile(profile.value)
    if not ok:
        await interaction.response.send_message("Invalid profile.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"Profile switched to **{ACTIVE_PROFILE}**.\nSkill: `{ACTIVE_SKILL_PATH}`\nRubric: `{ACTIVE_RUBRIC_PATH}`"
    )

@tree.command(name="show_profile", description="Show current interviewer profile")
async def show_profile(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Current profile: **{ACTIVE_PROFILE}**\nSkill: `{ACTIVE_SKILL_PATH}`\nRubric: `{ACTIVE_RUBRIC_PATH}`",
        ephemeral=True
    )

@tree.command(name="start_interview", description="Start an adaptive interview session")
@app_commands.describe(candidate_id="e.g., ETHANLAM", candidate="Optional: candidate user to invite into thread", private_thread="Create private thread and invite candidate")
async def start_interview(
    interaction: discord.Interaction,
    candidate_id: str,
    candidate: Optional[discord.Member] = None,
    private_thread: bool = False,
):
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
                type=discord.ChannelType.private_thread if private_thread else discord.ChannelType.public_thread,
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
        if private_thread and candidate is None:
            invited_msg = " Private thread created without candidate; add members manually."

        if candidate is not None:
            try:
                await created_thread.add_user(candidate)
                invited_msg = f" Invited {candidate.mention} to the thread."
            except Exception:
                # keep working even if invite fails
                invited_msg = f" Could not auto-invite {candidate.mention}; check channel/thread permissions."

        kickoff = f"Interview started for **{candidate_id}**."
        if candidate is not None:
            kickoff += f" {candidate.mention}"
        kickoff += f"\n\n**Q1:** {OPENING_QUESTION}"

        await created_thread.send(kickoff)

        guild_id = interaction.guild_id or 0
        thread_url = f"https://discord.com/channels/{guild_id}/{created_thread.id}"
        await interaction.response.send_message(
            f"Created {'private' if private_thread else 'public'} thread {created_thread.mention} for **{candidate_id}**.{invited_msg} Continue interview there.\nThread link: {thread_url}",
            ephemeral=False
        )

        # Extra reliable notification in parent channel with direct URL
        if candidate is not None:
            try:
                await interaction.followup.send(
                    f"{candidate.mention} your interview is ready: {thread_url}",
                    ephemeral=False
                )
            except Exception:
                pass
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

        # Save candidate message
        add_message(session_id, "candidate", message.content, str(message.author.id))

        # If candidate asks a question, answer briefly (optionally with web search), then continue interview.
        if candidate_asked_question(message.content):
            answer = answer_candidate_question(message.content, session_id)
            add_message(session_id, "interviewer", answer)
            await message.channel.send(answer)

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
                question = "Give one concrete example with your exact actions and measurable impact."

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
