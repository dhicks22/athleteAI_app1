import os
import json
RAW_USER_LOGINS = os.getenv("USER_LOGINS", "{}")

try:
    USER_LOGINS = json.loads(RAW_USER_LOGINS)
    print("User login config loaded:", USER_LOGINS)
except Exception as e:
    print("ERROR parsing USER_LOGINS:", e)
    USER_LOGINS = {}

# ------------------------------
# LOCAL DEVELOPMENT FALLBACK
# If USER_LOGINS is empty (environment variable not set),
# use this default so local login always works.
# ------------------------------
if not USER_LOGINS:
    USER_LOGINS = {
        "dylan": {
            "username": "dylan",
            "password": "1234",
            "sheet": "Dylan Hicks",
            "role": "coach",
        }
    }
    print("⚠️ USER_LOGINS not found — using local fallback login.")


import datetime as dt
from zoneinfo import ZoneInfo

ADL_TZ = ZoneInfo("Australia/Adelaide")


def today_adl():
    return dt.datetime.now(ADL_TZ).date()



import gspread
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from dash import (
    Dash, html, dcc, Input, Output, State, ALL,
    callback_context, no_update
)
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash.exceptions import PreventUpdate

# ============================================================
#  Environment / Credentials
# ============================================================

load_dotenv()   # <-- REQUIRED so .env and Render env vars are loaded

# Read main environment variables
GSHEET_ID = os.getenv("GSHEET_ID")
EMAIL_WEBHOOK_URL = os.getenv("EMAIL_WEBHOOK_URL")
APP_PASSCODE = os.getenv("APP_PASSCODE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

print("Loaded GSHEET_ID:", GSHEET_ID)
print("APP_PASSCODE set?:", bool(APP_PASSCODE))
print("EMAIL_WEBHOOK_URL set?:", bool(EMAIL_WEBHOOK_URL))
print("OPENAI_API_KEY set?:", bool(OPENAI_API_KEY))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

service_json = os.getenv("GS_SERVICE_JSON")
sh = None  # default to None so app can still run without Sheets

if not service_json:
    print("⚠️ WARNING: GS_SERVICE_JSON is missing. App will run but Sheets features will be disabled.")
else:
    try:
        service_account_info = json.loads(service_json)
        creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
        gc = gspread.authorize(creds)
        if not GSHEET_ID:
            print("⚠️ WARNING: GSHEET_ID is missing. Cannot open spreadsheet.")
        else:
            sh = gc.open_by_key(GSHEET_ID)
            print("✅ Google Sheet opened successfully.")
    except Exception as e:
        print(f"❌ ERROR initialising Google Sheets: {e}")
        sh = None


# ============================================================
#  Helpers: Sheets
# ============================================================
MOBILE_PLOT_LAYOUT = dict(
    autosize=True,
    height=360,                        # much shorter for phones
    margin=dict(l=20, r=10, t=40, b=40),
    legend=dict(
        orientation="h",               # horizontal legend
        yanchor="bottom",
        y=-0.35,                       # push legend BELOW the chart
        xanchor="center",
        x=0.5,
        font=dict(size=10),
    ),
    xaxis=dict(
        automargin=True,
        title_font=dict(size=12),
        tickfont=dict(size=10),
    ),
    yaxis=dict(
        automargin=True,
        title_font=dict(size=12),
        tickfont=dict(size=10),
    ),
)

def compute_streaks(df):
    """
    Compute current streak AND best streak from Athlete_Notes.
    Streak = consecutive days with non-empty notes.
    """
    if df.empty or "Date" not in df.columns:
        return 0, 0

    ddf = df.copy()
    ddf["Date"] = pd.to_datetime(ddf["Date"], errors="coerce").dt.date
    ddf = ddf.sort_values("Date")

    # Normalize notes
    ddf["notes_clean"] = (
        ddf["Athlete_Notes"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
    )

    valid_markers = {"", "nan", "none", "nil", "0"}

    # Record all days with valid notes
    logged_days = {
        row["Date"]
        for _, row in ddf.iterrows()
        if row["notes_clean"] not in valid_markers
    }

    today = today_adl()

    # -------------------------------
    # CURRENT STREAK
    # -------------------------------
    streak = 0
    cursor = today
    while cursor in logged_days:
        streak += 1
        cursor -= dt.timedelta(days=1)

    # -------------------------------
    # BEST STREAK
    # -------------------------------
    best = 0
    current_segment = 0

    for date in sorted(ddf["Date"].unique()):
        if date in logged_days:
            current_segment += 1
        else:
            best = max(best, current_segment)
            current_segment = 0

    best = max(best, current_segment)

    return streak, best

def streak_colors(streak):
    """
    Return dial colors for streak length:
    0–2  → green
    3–6  → blue
    7+   → gold
    """
    if streak <= 2:
        return "#4CAF50", "#66BB6A"     # green
    elif streak <= 6:
        return "#1E88E5", "#42A5F5"     # blue
    else:
        return "#FFC107", "#FFB300"     # gold


def list_tabs():
    """Return worksheet titles (athlete sheets)."""
    if sh is None:
        return []
    return [ws.title for ws in sh.worksheets()]

def load_users_table():
    """Load the 'Users' sheet containing username/password/athlete_sheet."""
    if sh is None:
        return pd.DataFrame()
    try:
        ws = sh.worksheet("Users")
        df = pd.DataFrame(ws.get_all_records())
        return df
    except:
        return pd.DataFrame()


def get_day_status(df, date_obj):
    """
    Return structured info about whether a given date has:
      - a logged session (notes / rpe / load)
      - rpe value
      - whether athlete notes exist
      - whether load exists
    """
    if df.empty or "Date" not in df.columns:
        return {
            "logged": False,
            "rpe": None,
            "has_notes": False,
            "has_load": False,
        }

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date

    rows = df[df["Date"] == date_obj]
    if rows.empty:
        return {
            "logged": False,
            "rpe": None,
            "has_notes": False,
            "has_load": False,
        }

    row = rows.iloc[-1]

    rpe = pd.to_numeric(row.get("sRPE", None), errors="coerce")
    notes = str(row.get("Athlete_Notes", "")).strip()
    load = pd.to_numeric(row.get("Load", None), errors="coerce")

    logged = False
    if (pd.notna(rpe) and rpe > 0) or notes not in ["", "nan", ""] or pd.notna(load):
        logged = True

    return {
        "logged": logged,
        "rpe": rpe if pd.notna(rpe) else None,
        "has_notes": notes not in ["", "nan", ""],
        "has_load": pd.notna(load),
    }

def load_tab(tab_name: str) -> pd.DataFrame:
    """Load worksheet to DataFrame with parsed Date."""
    if sh is None or not tab_name:
        return pd.DataFrame()

    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.get_worksheet(0)
    records = ws.get_all_records()
    df = pd.DataFrame(records)

    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    return df


def write_row(tab_name: str, row_idx_0: int, payload: dict):
    """
    Update one row (0-based from df) with payload values.
    Only updates columns that already exist.
    """
    if sh is None:
        return

    ws = sh.worksheet(tab_name)
    sheet_vals = ws.get_all_values()
    if not sheet_vals:
        return

    headers = sheet_vals[0]
    row_number = row_idx_0 + 2  # 1 for header + 1-based index

    row = ws.row_values(row_number)
    if len(row) < len(headers):
        row += [""] * (len(headers) - len(row))

    for col_name, value in payload.items():
        if col_name in headers:
            j = headers.index(col_name)
            row[j] = "" if value is None else str(value)

    ws.update(values=[row], range_name=f"A{row_number}")


def safe(df: pd.DataFrame, row_idx: int, col: str, default: str = "") -> str:
    """Safely fetch a value from df[row_idx, col] as string."""
    try:
        if col in df.columns:
            val = df.at[row_idx, col]
            if pd.notna(val):
                return str(val)
    except Exception:
        pass
    return default

def extract_thematic_tags(note: str):
    """
    Extract key themes from athlete notes using simple keyword matching.
    Returns a list of semantic tags (fatigue, confidence, acceleration, etc.)
    """

    if not isinstance(note, str):
        return []

    text = note.lower()

    themes = []

    # FATIGUE / ENERGY
    if any(w in text for w in ["tired", "fatigue", "heavy", "exhausted", "flat"]):
        themes.append("fatigue")

    if any(w in text for w in ["fresh", "sharp", "good energy", "ready"]):
        themes.append("energy")

    # RUNNING TECHNICAL
    if any(w in text for w in ["projection","position", "shin", "angles", "ground contact", "contact time"]):
        themes.append("technical_running")

    if any(w in text for w in ["acceleration","force application", "first step", "drive", "stride"]):
        themes.append("acceleration")

       # STRENGTH TRAINING THEMES
    if any(w in text for w in ["lift", "bench", "squat", "clean", "pull", "press"]):
        themes.append("strength_training")

    # PSYCHOLOGY / FEEL
    if any(w in text for w in ["confidence", "focus", "motivation", "stress", "anxious"]):
        themes.append("psych_state")

    return themes

def analyse_trends(df: pd.DataFrame):
    """
    Analyse athlete trends using:
      - Fatigue changes
      - Mood changes
      - Logging consistency
      - Extracted thematic patterns from notes
    Returns a dictionary of trends.
    """

    ddf = df.copy()

    if ddf.empty or "Date" not in ddf.columns:
        return {}

    ddf["Date"] = pd.to_datetime(ddf["Date"], errors="coerce")
    ddf = ddf.sort_values("Date")

    notes_list = ddf["Athlete_Notes"].fillna("").astype(str).tolist()

    trends = {}

    # -------------------------
    # BASIC NUMERIC TRENDS
    # -------------------------
    def series_slope(series):
        """Difference between recent and older values."""
        if len(series) < 3:
            return None
        clean = pd.to_numeric(series, errors="coerce").dropna()
        if len(clean) < 3:
            return None
        return clean.iloc[-1] - clean.iloc[0]

    # Fatigue
    if "Fatigue_1_5" in ddf.columns:
        fatigue_trend = series_slope(ddf["Fatigue_1_5"])
        if fatigue_trend is not None:
            if fatigue_trend > 0.3:
                trends["fatigue"] = "improving"
            elif fatigue_trend < -0.3:
                trends["fatigue"] = "worsening"

    # Mood
    if "Mood_1_5" in ddf.columns:
        mood_trend = series_slope(ddf["Mood_1_5"])
        if mood_trend is not None:
            if mood_trend > 0.3:
                trends["mood"] = "improving"
            elif mood_trend < -0.3:
                trends["mood"] = "dropping"

    # -------------------------
    # CONSISTENCY (last 7 days)
    # -------------------------
    if len(ddf) >= 7:
        last7 = ddf.tail(7)
        notes_logged = sum(len(str(n).strip()) > 0 for n in last7["Athlete_Notes"])
        if notes_logged >= 6:
            trends["consistency"] = "excellent"
        elif notes_logged >= 4:
            trends["consistency"] = "good"
        else:
            trends["consistency"] = "low"

    # -------------------------
    # THEMATIC TRENDS
    # -------------------------
    tags = []
    for n in notes_list:
        tags.extend(extract_thematic_tags(n))

    # Count themes
    tag_counts = pd.Series(tags).value_counts() if tags else pd.Series([])

    # Highlight themes that occurred at least twice
    trends["themes"] = tag_counts[tag_counts >= 2].index.tolist()

    return trends


# ============================================================
#  AI Personas & Helpers (Unified Engine, no TTS)
# ============================================================

def build_context_summary(df: pd.DataFrame, days: int = 7) -> str:
    """Summarise last `days` worth of load / wellness & ACWR."""
    if df.empty:
        return "No recent data available."

    if "Date" in df.columns:
        ddf = df.copy()
        ddf["Date"] = pd.to_datetime(ddf["Date"], errors="coerce").dt.date
        cutoff = dt.date.today() - dt.timedelta(days=days)
        recent = ddf[ddf["Date"] >= cutoff]
    else:
        recent = df.tail(days)

    def safe_mean(col):
        if col not in recent.columns:
            return "n/a"
        vals = pd.to_numeric(recent[col], errors="coerce")
        if vals.dropna().empty:
            return "n/a"
        return round(float(vals.mean(skipna=True)), 1)

    sRPE7 = safe_mean("sRPE")
    load7 = safe_mean("Load")
    sesh7 = safe_mean("Session_1_5")
    fat7 = safe_mean("Fatigue_1_5")
    mood7 = safe_mean("Mood_1_5")

    # Approx ACWR from EWMA/EMWA cols if present
    ew7_col = "EWMA 7" if "EWMA 7" in df.columns else ("EMWA 7" if "EMWA 7" in df.columns else None)
    ew28_col = "EWMA 28" if "EWMA 28" in df.columns else ("EMWA 28" if "EMWA 28" in df.columns else None)

    if ew7_col and ew28_col:
        try:
            ew7 = pd.to_numeric(df[ew7_col], errors="coerce")
            ew28 = pd.to_numeric(df[ew28_col], errors="coerce").replace(0, np.nan)
            acwr = (ew7 / ew28).replace([np.inf, -np.inf], np.nan)
            if acwr.dropna().empty:
                acwr7 = "n/a"
            else:
                acwr7 = round(float(acwr.tail(days).mean(skipna=True)), 2)
        except Exception:
            acwr7 = "n/a"
    else:
        acwr7 = "n/a"

    return (
        f"Last {days} days — sRPE avg: {sRPE7}, Load avg: {load7}, "
        f"Fatigue avg: {fat7}, Session avg: {sesh7}, Mood avg: {mood7}, ACWR approx: {acwr7}."
    )


def build_history_text(df: pd.DataFrame, max_rows: int = 7) -> str:
    """Use previous notes + AI suggestions as context (last `max_rows` entries)."""
    if df.empty:
        return ""

    cols = [
        c
        for c in ["Date", "Athlete_Notes", "AI_Suggestion_1", "AI_Suggestion_2"]
        if c in df.columns
    ]
    if not cols:
        return ""

    tail = df[cols].tail(max_rows)

    def _clean_str(val):
        s = str(val).strip()
        if s.lower() in ("nan", "none"):
            return ""
        return s

    lines = []
    for _, r in tail.iterrows():
        d = r.get("Date", "")
        note = _clean_str(r.get("Athlete_Notes", ""))
        ai1 = _clean_str(r.get("AI_Suggestion_1", ""))
        ai2 = _clean_str(r.get("AI_Suggestion_2", ""))
        if note or ai1 or ai2:
            lines.append(f"{d}: Note='{note}' AI1='{ai1}' AI2='{ai2}'")

    if not lines:
        return ""
    return "Recent interaction history:\n" + "\n".join(lines)


# ============================================================
#  AI Personas & Trend-Aware Suggestion Engine (Unified)
# ============================================================

def persona_prompt(mode: str) -> str:
    """
    Coaching personas tuned to be evidence-informed, SHORT, and clearly distinct.
    """
    PERSONAS = {
        "Speed & Power Coach": (
            "You are a speed and power coach who thinks like a track sprint coach. "
            "You focus on acceleration, max velocity, high-quality explosive reps, and fresh, snappy contacts. "
            "You give very direct, practical cues about intensity, contact time, and how many fast reps to keep."
        ),
        "Tempo & Endurance Coach": (
            "You are a tempo and endurance coach. "
            "You care about rhythm, aerobic conditioning, pacing, and avoiding sloppy fatigue. "
            "You give advice on tempo runs, controlled volume, and smooth, repeatable efforts."
        ),
        "Technical Sprint Coach": (
            "You are a technical sprint coach. "
            "You focus on posture, projection angles, rhythm, arm action, and how the athlete moves, not just how hard they work. "
            "You talk about a small number of key technical cues for the next session."
        ),
        "Strength & Power Coach": (
            "You are a strength and power coach. "
            "You think in sets × reps × load, jump quality, bar speed, and gym/plyometric progressions. "
            "You emphasise smart adjustments to load, jumps, and exercise selection to keep power high without unnecessary fatigue."
        ),
        "Holistic Readiness Coach": (
            "You are a recovery and readiness coach. "
            "You integrate physical load, fatigue, soreness, mood, and life stress. "
            "You help the athlete balance training, sleep, and recovery, and you keep the message supportive but honest."
        ),
        "General": (
            "You are a clear, supportive performance coach. "
            "You summarise what the trends suggest and give one or two concrete action steps."
        ),
    }
    return PERSONAS.get(mode, PERSONAS["General"])

# ---------------------------------------------------------
# AI SESSION DESIGN PERSONA PROFILES (for session generator)
# ---------------------------------------------------------
SESSION_COACH_PERSONAS = {
    "Speed & Power Coach":
        "You design sprint sessions emphasising projection, acceleration, and max-velocity quality. "
        "You protect freshness and avoid junk reps.",

    "Tempo & Endurance Coach":
        "You design smooth aerobic tempo work that supports conditioning and recovery without creating undue fatigue.",

    "Technical Sprint Coach":
        "You design sessions focused on posture, stiffness, projection, rhythm and technical cues.",

    "Strength & Power Coach":
        "You design gym-based strength and power progressions that complement sprint qualities.",

    "Recovery & Readiness Coach":
        "You design balanced sessions considering fatigue, readiness, mood, and life stressors.",
}



# ------------------------------------------------------------
# Helper: generic trend description for a numeric series
# ------------------------------------------------------------
def build_upcoming_context(df, today):
    try:
        ddf = df.copy()
        ddf["Date"] = pd.to_datetime(ddf["Date"], errors="coerce").dt.date
        future = ddf[ddf["Date"] > today]
        future = future[future["Workout"].astype(str).str.strip() != ""]

        if future.empty:
            return "No future planned sessions found."

        lines = []
        for _, r in future.head(5).iterrows():
            lines.append(f"{r['Date']}: {r.get('Workout', '')}")
        return "\n".join(lines)
    except:
        return "No upcoming data."


def _describe_trend(series: pd.Series, label: str, window: int = 7) -> str:
    """
    Take last `window` values of a numeric series and describe trend.
    Returns a short human-readable summary string.
    """
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return f"{label}: no recent data."

    recent = s.tail(window)
    if len(recent) < 2:
        avg = round(float(recent.mean()), 1)
        return f"{label}: limited data, recent average ≈ {avg}."

    start_val = recent.iloc[0]
    end_val = recent.iloc[-1]
    avg = round(float(recent.mean()), 1)
    delta = end_val - start_val

    # Heuristic thresholds
    if delta > 0.5:
        direction = "rising"
    elif delta < -0.5:
        direction = "falling"
    else:
        direction = "fairly stable"

    return (
        f"{label}: {direction} over the last {len(recent)} entries "
        f"(from {start_val:.1f} to {end_val:.1f}, mean ≈ {avg})."
    )


# ------------------------------------------------------------
# Helper: build recent load / wellness / ACWR context text
# ------------------------------------------------------------
def build_trend_context(df: pd.DataFrame, days: int = 14) -> str:
    """
    Summarise the last `days` of load, wellness and ACWR into a compact
    coaching-friendly paragraph.
    """
    if df.empty or "Date" not in df.columns:
        return "No recent training or wellness data is available."

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce").dt.date
    d = d.sort_values("Date")

    cutoff = dt.date.today() - dt.timedelta(days=days)
    recent = d[d["Date"] >= cutoff]
    if recent.empty:
        return f"No data has been logged in the last {days} days."

    lines = []

    # sRPE / RPE_Post_Session / Load
    if "RPE_Post_Session" in recent.columns:
        lines.append(_describe_trend(recent["RPE_Post_Session"], "Session RPE (1–10)"))
    elif "sRPE" in recent.columns:
        lines.append(_describe_trend(recent["sRPE"], "Session RPE (1–10)"))

    if "Load" in recent.columns:
        lines.append(_describe_trend(recent["Load"], "Training Load"))

    # Fatigue / Mood / Session (1–5)
    if "Fatigue_1_5" in recent.columns:
        lines.append(_describe_trend(recent["Fatigue_1_5"], "Fatigue (1–5)"))
    if "Mood_1_5" in recent.columns:
        lines.append(_describe_trend(recent["Mood_1_5"], "Mood (1–5)"))
    if "Session_1_5" in recent.columns:
        lines.append(_describe_trend(recent["Session_1_5"], "Session quality (1–5)"))

    # ACWR using EWMA cols if available
    ew7_col = "EWMA 7" if "EWMA 7" in recent.columns else ("EMWA 7" if "EMWA 7" in recent.columns else None)
    ew28_col = "EWMA 28" if "EWMA 28" in recent.columns else ("EMWA 28" if "EMWA 28" in recent.columns else None)

    if ew7_col and ew28_col:
        try:
            ew7 = pd.to_numeric(recent[ew7_col], errors="coerce")
            ew28 = pd.to_numeric(recent[ew28_col], errors="coerce").replace(0, np.nan)
            acwr = (ew7 / ew28).replace([np.inf, -np.inf], np.nan)
            acwr_recent = acwr.dropna()
            if not acwr_recent.empty:
                desc = _describe_trend(acwr_recent, "ACWR", window=min(7, len(acwr_recent)))
                lines.append(desc)
        except Exception:
            pass

    if not lines:
        return "Recent data are limited, but you can still use the single-session metrics."

    return "Recent trends (last ~2 weeks): " + " ".join(lines)


# ------------------------------------------------------------
# Helper: collect recent notes / track / sets × reps for context
# ------------------------------------------------------------
def build_text_history(df: pd.DataFrame, max_rows: int = 7) -> str:
    """
    Use recent athlete-facing text inputs as context for the coach AI.
    Includes notes + AI suggestions if present.
    """
    if df.empty:
        return "No previous written notes are available."

    cols = [c for c in [
        "Date",
        "Athlete_Notes",
        "Sets_Reps_Load",
        "Track_Reps_Times",
        "AI_Suggestion_1",
        "AI_Suggestion_2",
    ] if c in df.columns]

    if not cols:
        return "No previous written notes are available."

    d = df.copy()
    if "Date" in d.columns:
        d["Date"] = pd.to_datetime(d["Date"], errors="coerce").dt.date
        d = d.sort_values("Date")

    tail = d[cols].tail(max_rows)

    lines = []
    for _, r in tail.iterrows():
        date_str = str(r.get("Date", ""))
        note = str(r.get("Athlete_Notes", "")).strip()
        sets_reps = str(r.get("Sets_Reps_Load", "")).strip()
        track = str(r.get("Track_Reps_Times", "")).strip()

        # Only bring AI suggestions if they’re non-empty
        ai1 = str(r.get("AI_Suggestion_1", "")).strip()
        ai2 = str(r.get("AI_Suggestion_2", "")).strip()

        parts = []
        if note and note.lower() not in ("nan", "none"):
            parts.append(f"Note: {note}")
        if sets_reps and sets_reps.lower() not in ("nan", "none"):
            parts.append(f"Gym: {sets_reps}")
        if track and track.lower() not in ("nan", "none"):
            parts.append(f"Track: {track}")
        if ai1 and ai1.lower() not in ("nan", "none"):
            parts.append(f"Prev AI1: {ai1}")
        if ai2 and ai2.lower() not in ("nan", "none"):
            parts.append(f"Prev AI2: {ai2}")

        if parts:
            lines.append(f"{date_str} → " + " | ".join(parts))

    if not lines:
        return "No previous written notes are available."

    return "Recent text history:\n" + "\n".join(lines)


# ------------------------------------------------------------
# Helper: simple thematic tags from current session text
# ------------------------------------------------------------
def extract_thematic_tags(notes: str, sets_reps: str, track_reps: str) -> str:
    """
    Very lightweight keyword tagging to give the coach AI a feel
    for what kind of session this was.
    """
    text = " ".join([
        str(notes or "").lower(),
        str(sets_reps or "").lower(),
        str(track_reps or "").lower(),
    ])

    tags = []

    # Sprint / projection / maxV
    if any(k in text for k in ["block start", "accel", "acceleration", "drive phase", "projection"]):
        tags.append("acceleration / projection")
    if any(k in text for k in ["max v", "top speed", "flying", "fly", "upright"]):
        tags.append("max velocity / upright mechanics")

    # Strength / power
    if any(k in text for k in ["squat", "deadlift", "rdl", "split squat", "lunge"]):
        tags.append("lower-body strength")
    if any(k in text for k in ["clean", "snatch", "jump squat", "olympic"]):
        tags.append("power & RFD")

    # Plyometric / stiffness
    if any(k in text for k in ["plyo", "bounds", "pogos", "hops", "jump series"]):
        tags.append("plyometrics / stiffness")

    # Tempo / conditioning
    if any(k in text for k in ["tempo", "cruise", "aerobic", "ext tempo"]):
        tags.append("tempo / aerobic conditioning")

    # Fatigue / soreness / niggle
    if any(k in text for k in ["sore", "tight", "niggle", "ache", "fatigued", "heavy legs"]):
        tags.append("fatigue / soreness flags")

    if not tags:
        return "general sprint & strength themes"

    # De-duplicate and join
    uniq = []
    for t in tags:
        if t not in uniq:
            uniq.append(t)
    return ", ".join(uniq)


# ------------------------------------------------------------
# Core OpenAI wrapper (unchanged interface)
# ------------------------------------------------------------
def call_openai_chat(messages: list) -> str:
    """
    Wrapper for OpenAI chat completions.
    Keeps responses short (2–3 sentences) and robust to failures.
    """
    if not OPENAI_API_KEY:
        return "AI suggestion unavailable (missing API key)."
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 260,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return f"AI suggestion unavailable (HTTP {resp.status_code})."
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"AI suggestion unavailable ({e})."


# ------------------------------------------------------------
# Builder: messages for a single persona suggestion
# ------------------------------------------------------------
def build_ai_messages_for_persona(
    persona_mode: str,
    df: pd.DataFrame,
    athlete_name: str,
    selected_date,
    session_rpe,
    session_quality,
    fatigue,
    mood,
    notes,
    sets_reps_load,
    track_reps_times,
) -> list:
    """
    Compose the system + user messages for one persona suggestion.
    """
    persona = persona_prompt(persona_mode)

    # Numeric context → trend summary
    trend_context = build_trend_context(df, days=14)

    # Textual history (notes, sets/reps, track, previous AI)
    history_text = build_text_history(df, max_rows=7)

    # Thematic tags for current session
    themes = extract_thematic_tags(notes, sets_reps_load, track_reps_times)

    # Current session snapshot
    session_block = (
        f"Athlete: {athlete_name}\n"
        f"Current session date: {selected_date}\n"
        f"Session RPE (1–10): {session_rpe}\n"
        f"Session quality (1–5): {session_quality}\n"
        f"Fatigue (1–5): {fatigue}\n"
        f"Mood (1–5): {mood}\n"
        f"Session themes: {themes}\n\n"
        f"Athlete notes: {notes or 'nil'}\n"
        f"Sets × Reps × Load: {sets_reps_load or 'nil'}\n"
        f"Track reps & times: {track_reps_times or 'nil'}\n"
    )

    system_content = (
        persona +
        " Always remain evidence-informed and conservative with return-to-sport decisions. "
        "Never guess specific injuries, illnesses or diagnoses. "
        "Base all recommendations strictly on the provided metrics, themes and notes. "
        "Use the recent trends as your context, then link your coaching advice to those trends. "
        "Your reply should be detailed, 3–4 sentences, roughly 120–150 words. "
        "Provide richer reasoning tied directly to recent trends, and include one or two specific training actions."
        "for the next 24–48 hours (e.g., adjust volume, intensity, emphasis, or recovery focus)."
    )

    user_content = (
        f"{trend_context}\n\n"
        f"{history_text}\n\n"
        f"{session_block}\n"
        "Using the above information, interpret the athlete's current readiness and risk, "
        "then give 2-3 precise coaching recommendation for the next 24–48 hours that fits "
        f"your persona style ({persona_mode})."
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


# ------------------------------------------------------------
# Public entry point used by the Dash callback
# ------------------------------------------------------------
def make_ai_suggestions(
    athlete_name: str,
    selected_date,
    session_rpe: int | float,
    session: int | float,
    fatigue: int | float,
    mood: int | float,
    notes: str,
    sets_reps_load: str,
    track_reps_times: str,
    ai_mode_1: str,
    ai_mode_2: str,
):
    """
    Unified AI engine (v3):
      - Uses trend analysis (14-day)
      - Uses recent text history (notes + sets/reps + track + AI)
      - Uses current session metrics
      - Persona-based coaching suggestions
      - Each output starts with "<FirstName>,"
      - Output: 2–3 natural coaching sentences (not robotic)
    """

    df = load_tab(athlete_name)

    # Extract first name ONLY
    first_name = athlete_name.split()[0]

    # Trend summary
    summary = build_context_summary(df)
    trend_context = build_trend_context(df)
    history_text = build_text_history(df)

    # Build session snapshot
    session_block = (
        f"Current session — {selected_date}\n"
        f"RPE (1–10): {session_rpe}\n"
        f"Session quality (1–5): {session}\n"
        f"Fatigue (1–5): {fatigue}\n"
        f"Mood (1–5): {mood}\n"
        f"Athlete notes: {notes}\n"
        f"Sets × Reps × Load: {sets_reps_load}\n"
        f"Track reps & times: {track_reps_times}\n"
    )

    # Helper to construct messages for each persona
    def build_messages(mode: str):
        persona = persona_prompt(mode)

        system_msg = (
            persona
            + "\nYou are providing short, specific, natural coaching advice."
              "Avoid guessing injuries or medical issues. Avoid generic advice."
              "Your tone should match the persona style."
              "Output must be 3-4 sentences (~100-150 words)."
              f"\nAlways begin your response with: '{first_name},'"
        )

        user_msg = (
            f"ATHLETE FIRST NAME: {first_name}\n\n"
            f"Recent trend summary:\n{summary}\n\n"
            f"Detailed trend context:\n{trend_context}\n\n"
            f"Recent note + training history:\n{history_text}\n\n"
            f"Current session data:\n{session_block}\n\n"
            "TASK:\n"
            f"- Write coaching feedback as the {mode} persona.\n"
            "- Start with: '<FirstName>,'\n"
            "- Make the tone natural, like a real coach speaking.\n"
            "- Keep it concise but meaningful (2–3 sentences).\n"
            "- Give 3-4 clear, actionable recommendations for the next 24–48 hours.\n"
        )

        return [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

    # Generate both AI suggestions
    ai1 = call_openai_chat(build_messages(ai_mode_1))
    ai2 = call_openai_chat(build_messages(ai_mode_2))

    return ai1, ai2





# ============================================================
#  Email webhook
# ============================================================

def send_email_payload(payload: dict):
    """
    POST to EMAIL_WEBHOOK_URL if configured.
    Handle routing to athlete & coach via Apps Script.
    """
    if not EMAIL_WEBHOOK_URL:
        return

    try:
        print("\n=== EMAIL PAYLOAD ===")
        print(payload)
        print("====================\n")

        requests.post(
            EMAIL_WEBHOOK_URL,
            json=payload,
            timeout=15,
        )
    except Exception as e:
        print("Email webhook error:", e)
        pass


# ============================================================
#  Plot builders & Layout helpers
# ============================================================

# ============================================================
#   DIAL SYSTEM — Unified Apple/Fitness-Style Dial Rendering
# ============================================================

def _legend_right_layout(base: dict | None = None) -> dict:
    """Shared legend layout: vertical, right side."""
    base = base or {}
    base.update(
        legend=dict(
            orientation="v",
            yanchor="top",
            y=1,
            xanchor="left",
            x=1.02,
        )
    )
    return base


def _week_agg_date(d):
    """Convert dates to week buckets starting Saturday → Friday."""
    return d.dt.to_period("W-SAT").apply(lambda r: r.start_time)


# ============================================================
#   GENERIC DIAL COMPONENT
# ============================================================

def _build_dial(value_str, percent, color):
    """
    Generic Fitness-style animated dial.
    - value_str: text displayed in the center
    - percent: 0–100 fill
    - color: CSS color for the ring
    """

    return html.Div(
        className="dial-wrapper",
        children=[
            html.Div(
                className="dial-circle updated",   # "updated" triggers CSS animation
                style={
                    "--dial-progress": percent,
                    "--dial-color": color,
                },
                children=[
                    html.Div(value_str, className="dial-text")
                ]
            )
        ],
    )


# ============================================================
#   WEEKLY TRAINING EXPOSURE (0–7)
# ============================================================

def apple_sessions_ring(progress: int):
    """
    Weekly exposure dial:
    - 0–7 sessions
    - Color ramps red → orange → green → blue
    """

    p = max(0, min(int(progress), 7))
    percent = (p / 7) * 100

    if p <= 2:
        color = "#E53935"    # red
    elif p <= 4:
        color = "#FB8C00"    # orange
    elif p <= 6:
        color = "#4CAF50"    # green
    else:
        color = "#1E88E5"    # blue

    return _build_dial(f"{p}/7", percent, color)


# ============================================================
#   STREAK DIAL — Simplified (Cleaner Apple Fitness Look)
# ============================================================

def streak_dial(streak):
    """
    Training streak dial (Apple Fitness style)
    - No “Best streak” text (cleaner UI)
    - Colors via streak intensity
    """

    # 0–2 low → green
    # 3–6 medium → blue
    # 7+ high → gold
    if streak <= 2:
        color = "#4CAF50"
    elif streak <= 6:
        color = "#1E88E5"
    else:
        color = "#FFC107"

    # Cap visual ring at 7
    percent = min(streak, 7) * (100 / 7)

    return _build_dial(str(streak), percent, color)


# ============================================================
#   NEUROMUSCULAR STATE (1–5)
# ============================================================

def apple_neuromuscular_ring(avg_score: float | None):
    if avg_score is None or np.isnan(avg_score):
        return _build_dial("—", 0, "#CFD8DC")

    v = max(1.0, min(float(avg_score), 5.0))
    percent = (v / 5.0) * 100

    # Color map
    if v < 2:
        color = "#E53935"
    elif v < 3:
        color = "#FB8C00"
    elif v < 4:
        color = "#4CAF50"
    else:
        color = "#1E88E5"

    return _build_dial(f"{v:.1f}", percent, color)


# ============================================================
#   TRAINING READINESS (1–5)
# ============================================================

def apple_readiness_ring(readiness_score: float | None):
    if readiness_score is None or np.isnan(readiness_score):
        return _build_dial("—", 0, "#CFD8DC")

    v = max(1.0, min(float(readiness_score), 5.0))
    percent = (v / 5.0) * 100

    if v < 2:
        color = "#E53935"
    elif v < 3:
        color = "#FB8C00"
    elif v < 4:
        color = "#4CAF50"
    else:
        color = "#1E88E5"

    return _build_dial(f"{v:.1f}", percent, color)


# ============================================================
#   NEW MONTH CALENDAR WITH RPE PILLS
# ============================================================

def build_month_calendar(df: pd.DataFrame, month_date: dt.date, selected_date_str: str | None):
    """
    Renders a full month view calendar with coloured RPE pills.
    Each day = click → opens session input.
    """

    if df.empty or "Date" not in df.columns:
        return html.Div("No data", className="text-muted")

    # Convert dataframe date column
    ddf = df.copy()
    ddf["Date"] = pd.to_datetime(ddf["Date"], errors="coerce").dt.date

    # Determine month start
    year = month_date.year
    month = month_date.month
    first_day = dt.date(year, month, 1)

    # Always 6×7 = 42 days view
    start_weekday = first_day.weekday()   # Monday=0
    start_offset = (start_weekday + 1) % 7  # Convert to Sunday-start

    days = [first_day - dt.timedelta(days=start_offset) + dt.timedelta(days=i) for i in range(42)]

    # Selected date conversion
    selected_date = None
    if selected_date_str:
        try:
            selected_date = pd.to_datetime(selected_date_str).date()
        except:
            selected_date = None

    today = today_adl()
    cells = []

    for day in days:
        # Check entry for that day
        match = ddf[ddf["Date"] == day]
        rpe = pd.to_numeric(match.iloc[-1].get("sRPE", np.nan), errors="coerce") if not match.empty else np.nan

        # Pill color logic
        if pd.isna(rpe):
            pill_color = "#CFD8DC"
        elif rpe <= 2:
            pill_color = "#4285F4"
        elif 3 <= rpe <= 5:
            pill_color = "#4CAF50"
        elif 6 <= rpe <= 7:
            pill_color = "#FF9800"
        else:
            pill_color = "#F44336"

        # CSS classes for the cell
        classes = ["calendar-day"]

        if day == today:
            classes.append("today")

        # Logged session?
        logged_session = False
        if not match.empty:
            notes_val = str(match.iloc[-1].get("Athlete_Notes", "")).strip().lower()
            if notes_val not in ["", "nan", "none", "nil", "0"]:
                logged_session = True

        if logged_session:
            classes.append("logged")

        # Out-of-month fade
        if day.month != month:
            classes.append("out-month")

        # Selected date highlight → handled via CSS? Otherwise add class
        if selected_date and day == selected_date:
            classes.append("selected")

        # FINAL — append cell
        cells.append(
            html.Div(
                [
                    html.Div(str(day.day), className="cal-day-number"),
                    html.Div(className="rpe-dot", style={"backgroundColor": pill_color}),
                ],
                id={"type": "calendar-day", "date": str(day)},
                n_clicks=0,
                className=" ".join(classes),
            )
        )

    # Build grid
    grid = html.Div(
        cells,
        style={
            "display": "grid",
            "gridTemplateColumns": "repeat(7, 1fr)",
            "gap": "4px",
            "padding": "6px"
        }
    )

    # Weekday labels
    weekdays = html.Div(
        [html.Div(d, style={"textAlign": "center", "fontWeight": "600"})
         for d in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]],
        style={"display": "grid", "gridTemplateColumns": "repeat(7, 1fr)", "marginBottom": "4px"}
    )

    # RPE legend
    legend = html.Div(
        [
            html.Small("RPE Colour Scale:", className="fw-bold me-2"),

            html.Span("1–2", style={
                "background": "#4285F4", "color": "white",
                "padding": "2px 8px", "borderRadius": "6px",
                "marginRight": "6px", "fontSize": "12px"
            }),

            html.Span("3–5", style={
                "background": "#4CAF50", "color": "white",
                "padding": "2px 8px", "borderRadius": "6px",
                "marginRight": "6px", "fontSize": "12px"
            }),

            html.Span("6–7", style={
                "background": "#FF9800", "color": "white",
                "padding": "2px 8px", "borderRadius": "6px",
                "marginRight": "6px", "fontSize": "12px"
            }),

            html.Span("8–10", style={
                "background": "#F44336", "color": "white",
                "padding": "2px 8px", "borderRadius": "6px",
                "fontSize": "12px"
            }),
        ],
        style={"textAlign": "center", "marginTop": "8px"}
    )

    return html.Div([legend, weekdays, grid])



# ============================================================
#   TRAINING LOAD
# ============================================================

def build_load_plot(df: pd.DataFrame, view_mode: str):
    """
    Training Load plot (daily / weekly)
    - Bars: Load
    - Lines: 7d rolling load, EWMA7, EWMA28
    - ACWR on secondary axis with safe band
    """

    # Colour palette (complementary + consistent)
    BLUE       = "#1E88E5"   # Load bars
    ORANGE     = "#FB8C00"   # 7d avg
    TEAL       = "#26A69A"   # EWMA 7
    GREEN_DARK = "#2E7D32"   # EWMA 28
    PURPLE     = "#8E24AA"   # ACWR

    fig = go.Figure()

    # Basic guards
    if df.empty or "Date" not in df.columns or "Load" not in df.columns:
        fig.update_layout(**_legend_right_layout())
        return fig

    # ---------- Core prep ----------
    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.sort_values("Date")

    d["Load"] = pd.to_numeric(d["Load"], errors="coerce")
    d["Load_7d"] = d["Load"].rolling(7, min_periods=1).mean()

    # EWMA columns (if present)
    d["EWMA7"] = pd.to_numeric(
        d.get("EMWA 7", d.get("EWMA 7", np.nan)),
        errors="coerce"
    )
    d["EWMA28"] = pd.to_numeric(
        d.get("EWMA 28", d.get("EMWA 28", np.nan)),
        errors="coerce"
    )
    d["ACWR_col"] = pd.to_numeric(
        d.get("EMWA ACWR", d.get("EWMA ACWR", np.nan)),
        errors="coerce"
    )

    # =========================================
    # WEEKLY VIEW
    # =========================================
    if view_mode == "weekly":
        # Week bucket: Sat–Fri via helper
        d["Week"] = _week_agg_date(d["Date"])

        g = d.groupby("Week", as_index=False).agg(
            Load=("Load", "sum"),
            Load_7d=("Load_7d", "mean"),
            EWMA7=("EWMA7", "mean"),
            EWMA28=("EWMA28", "mean"),
            ACWR_col=("ACWR_col", "mean"),
        )

        x = g["Week"]

        # Bars – Weekly Load
        fig.add_bar(
            x=x,
            y=g["Load"],
            name="Weekly Load",
            marker_color=BLUE,
        )

        # 7d Avg Load
        fig.add_trace(go.Scatter(
            x=x, y=g["Load_7d"],
            name="7-day Avg Load",
            mode="lines",
            line=dict(color=ORANGE, width=3),
        ))

        # EWMA 7
        fig.add_trace(go.Scatter(
            x=x, y=g["EWMA7"],
            name="EWMA 7",
            mode="lines",
            line=dict(color=TEAL, width=2, dash="dash"),
        ))

        # EWMA 28
        fig.add_trace(go.Scatter(
            x=x, y=g["EWMA28"],
            name="EWMA 28",
            mode="lines",
            line=dict(color=GREEN_DARK, width=2, dash="dot"),
        ))

        # ACWR on secondary axis
        fig.add_trace(go.Scatter(
            x=x, y=g["ACWR_col"],
            name="ACWR",
            mode="lines",
            yaxis="y2",
            line=dict(color=PURPLE, width=2),
        ))

        # Safe ACWR band 0.8–1.3
        fig.add_shape(
            type="rect",
            xref="paper", x0=0, x1=1,
            yref="y2", y0=0.8, y1=1.3,
            fillcolor="rgba(38,166,154,0.15)",  # soft teal
            line_width=0,
            layer="below",
        )

        fig.update_layout(
            title="Training Load (Weekly)",
            xaxis_title="Week (Sat–Fri)",
            yaxis=dict(title="Load"),
            yaxis2=dict(
                title="ACWR",
                overlaying="y",
                side="right",
                range=[0, 2],
                showgrid=False,
            ),
            hovermode="x unified",
            **_legend_right_layout()
        )

        # Apply mobile-friendly layout overrides
        fig.update_layout(**MOBILE_PLOT_LAYOUT)

        return fig

    # =========================================
    # DAILY VIEW
    # =========================================
    x = d["Date"]

    # Bars – Daily Load
    fig.add_bar(x=x, y=d["Load"].round(2), name="Daily Load", marker_color=BLUE)

    # 7d Avg Load
    fig.add_trace(go.Scatter(
        x=x, y=d["Load_7d"],
        name="7-day Avg Load",
        mode="lines",
        line=dict(color=ORANGE, width=3),
    ))

    # EWMA 7
    fig.add_trace(go.Scatter(
        x=x, y=d["EWMA7"],
        name="EWMA 7",
        mode="lines",
        line=dict(color=TEAL, width=2, dash="dash"),
    ))

    # EWMA 28
    fig.add_trace(go.Scatter(
        x=x, y=d["EWMA28"],
        name="EWMA 28",
        mode="lines",
        line=dict(color=GREEN_DARK, width=2, dash="dot"),
    ))

    # ACWR on y2
    fig.add_trace(go.Scatter(
        x=x, y=d["ACWR_col"],
        name="ACWR",
        mode="lines",
        yaxis="y2",
        line=dict(color=PURPLE, width=2),
    ))

    # Safe band
    fig.add_shape(
        type="rect",
        xref="paper", x0=0, x1=1,
        yref="y2", y0=0.8, y1=1.3,
        fillcolor="rgba(38,166,154,0.15)",
        line_width=0,
        layer="below",
    )

    fig.update_layout(
        title="Training Load (Daily)",
        xaxis_title="Date",
        yaxis=dict(title="Load"),
        yaxis2=dict(
            title="ACWR",
            overlaying="y",
            side="right",
            range=[0, 2],
            showgrid=False,
        ),
        hovermode="x unified",
        **_legend_right_layout()
    )

    # Apply mobile-friendly layout overrides
    fig.update_layout(**MOBILE_PLOT_LAYOUT)

    return fig


# ============================================================
#   WELLNESS
# ============================================================

def build_wellness_plot(df: pd.DataFrame, view_mode: str):
    fig = go.Figure()

    if df.empty or "Date" not in df.columns:
        fig.update_layout(**_legend_right_layout())
        return fig

    # -------------------------
    # CLEAN + PREP DATAFRAME
    # -------------------------
    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.sort_values("Date")

    # Columns that must be numeric
    wellness_cols = ["Session_1_5", "Fatigue_1_5", "Mood_1_5", "RPE_Post_Session"]

    for c in wellness_cols:
        if c in d.columns:
            d[c] = (
                d[c].astype(str)
                .str.strip()
                .replace("", np.nan)
            )
            d[c] = pd.to_numeric(d[c], errors="coerce").round(2)

    # Scale RPE (1–10 → 1–5)
    if "RPE_Post_Session" in d.columns:
        d["RPE_SCALED"] = (d["RPE_Post_Session"] / 2).round(2)
    else:
        d["RPE_SCALED"] = np.nan

    # -------------------------
    # WEEKLY MODE
    # -------------------------
    if view_mode == "weekly":
        d["Week"] = _week_agg_date(d["Date"])
        d = d.groupby("Week", as_index=False).mean(numeric_only=True)
        x = d["Week"]
    else:
        x = d["Date"]

    # -------------------------
    # Helper to add lines
    # -------------------------
    def add_line(col, name, color):
        if col in d.columns:
            vals = pd.to_numeric(d[col], errors="coerce").round(2)
            if vals.dropna().empty:
                return
            fig.add_trace(go.Scatter(
                x=x,
                y=vals,
                mode="lines",
                name=name,
                line=dict(width=3, color=color),
            ))

    # -------------------------
    # ADD ALL WELLNESS SERIES
    # -------------------------
    add_line("Session_1_5",      "Session",   "#1E88E5")
    add_line("RPE_SCALED",       "RPE Post",  "#E53935")
    add_line("Fatigue_1_5",      "Fatigue",   "#FB8C00")
    add_line("Mood_1_5",         "Mood",      "#8E24AA")

    # -------------------------
    # Layout
    # -------------------------
    fig.update_layout(
        title=f"Wellness ({'Weekly Avg' if view_mode=='weekly' else 'Daily'})",
        xaxis_title="Date",
        yaxis_title="Scale (1–5)",
        hovermode="x unified",
        **_legend_right_layout()
    )

    # Apply mobile-friendly layout overrides
    fig.update_layout(**MOBILE_PLOT_LAYOUT)

    return fig


# ============================================================
#   SPEED & TEMPO
# ============================================================

def build_speed_tempo_plot(df: pd.DataFrame, view_mode: str):
    BLUE = "#1E88E5"
    ORANGE = "#FB8C00"
    TEAL = "#26A69A"
    PURPLE = "#8E24AA"

    fig = go.Figure()

    if df.empty or "Date" not in df.columns:
        fig.update_layout(**_legend_right_layout())
        return fig

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.sort_values("Date")

    speed = pd.to_numeric(d.get("SPEED (m)", np.nan), errors="coerce").fillna(0).round(2)
    tempo = pd.to_numeric(d.get("TEMPO (m)", np.nan), errors="coerce").fillna(0).round(2)

    if view_mode == "daily":
        x = d["Date"]

        fig.add_bar(x=x, y=speed, name="Speed (m)", marker_color=BLUE)
        fig.add_bar(x=x, y=tempo, name="Tempo (m)", marker_color=ORANGE)

        fig.add_trace(go.Scatter(
            x=x, y=speed.rolling(7, min_periods=1).mean(),
            name="Speed 7d", mode="lines",
            line=dict(color=TEAL, width=2, dash="dashdot")
        ))

        fig.add_trace(go.Scatter(
            x=x, y=speed.rolling(28, min_periods=1).mean(),
            name="Speed 28d", mode="lines",
            line=dict(color=PURPLE, width=2, dash="solid")
        ))

        fig.add_trace(go.Scatter(
            x=x, y=tempo.rolling(7, min_periods=1).mean(),
            name="Tempo 7d", mode="lines",
            line=dict(color=TEAL, width=2, dash="dash")
        ))

        fig.add_trace(go.Scatter(
            x=x, y=tempo.rolling(28, min_periods=1).mean(),
            name="Tempo 28d", mode="lines",
            line=dict(color=PURPLE, width=2, dash="dot")
        ))

        fig.update_layout(
            title="Speed & Tempo Volumes (Daily)",
            xaxis_title="Date",
            yaxis_title="Metres",
            barmode="stack",
            hovermode="x unified",
            **_legend_right_layout()
        )
        return fig

    # Weekly view -----------------------------------------
    d["Week"] = _week_agg_date(d["Date"])
    d["Speed_clean"] = speed
    d["Tempo_clean"] = tempo

    g = d.groupby("Week", as_index=False).agg(
        Speed=("Speed_clean", "sum"),
        Tempo=("Tempo_clean", "sum"),
    )

    x = g["Week"]

    fig.add_bar(x=x, y=g["Speed"], name="Speed (m)", marker_color=BLUE)
    fig.add_bar(x=x, y=g["Tempo"], name="Tempo (m)", marker_color=ORANGE)

    fig.add_trace(go.Scatter(
        x=x, y=g["Speed"].rolling(1).mean(),
        name="Speed 7d", mode="lines",
        line=dict(color=TEAL, width=2, dash="dashdot")
    ))

    fig.add_trace(go.Scatter(
        x=x, y=g["Speed"].rolling(4).mean(),
        name="Speed 28d", mode="lines",
        line=dict(color=PURPLE, width=2, dash="solid")
    ))

    fig.add_trace(go.Scatter(
        x=x, y=g["Tempo"].rolling(1).mean(),
        name="Tempo 7d", mode="lines",
        line=dict(color=TEAL, width=2, dash="dash")
    ))

    fig.add_trace(go.Scatter(
        x=x, y=g["Tempo"].rolling(4).mean(),
        name="Tempo 28d", mode="lines",
        line=dict(color=PURPLE, width=2, dash="dot")
    ))

    fig.update_layout(
        title="Speed & Tempo Volumes (Weekly)",
        xaxis_title="Week",
        yaxis_title="Metres",
        barmode="stack",
        hovermode="x unified",
        **_legend_right_layout()
    )

    # Apply mobile-friendly layout overrides
    fig.update_layout(**MOBILE_PLOT_LAYOUT)

    return fig


# ============================================================
#  Dash app
# ============================================================

app = Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title="Adaptive Coaching Intelligence",
    update_title=None,
)

server = app.server

# Serve favicon from assets/
app._favicon = "icon-192.png"

# Override the index to include manifest + apple icon
app.index_string = """
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <meta name="mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-title" content="AthleteAI">
        <meta name="theme-color" content="#1e88e5">

        <!-- Android/Chrome PWA -->
        <link rel="manifest" href="/assets/manifest.json">

        <!-- App Icons -->
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css">
        <link rel="icon" type="image/png" sizes="192x192" href="/assets/icon-192.png">
        <link rel="apple-touch-icon" href="/assets/icon-192.png">

        <title>Adaptive Coaching Intelligence</title>
        {%css%}
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
"""


# --------------- UI Components ---------------

def app_header(center=False):
    align = "center" if center else "left"
    return html.Div(
        [
            html.Img(
                src="/assets/app_icon.png",
                style={
                    "height": "56px",
                    "marginRight": "10px",
                    "verticalAlign": "middle",
                },
            ),
            html.Div(
                [
                    html.H2(
                        "Adaptive Coaching Intelligence",
                        style={
                            "margin": 0,
                            "fontWeight": 600,
                            "textAlign": align,
                        },
                    ),
                    html.Small(
                        "AI-aligned athlete & coaching feedback",
                        style={"color": "#555", "textAlign": align, "display": "block"},
                    ),
                ],
                style={"display": "inline-block", "verticalAlign": "middle"},
            ),
        ],
        style={"textAlign": align, "marginBottom": "20px"},
    )
logout_button = html.Div(
    html.Button(
        "Logout",
        id="logout-button",
        n_clicks=0,
        style={
            "background": "#3589e5",
            "color": "white",
            "border": "none",
            "padding": "6px 12px",
            "fontSize": "14px",
            "borderRadius": "6px",
            "float": "right",
            "margin": "5px 10px",
            "cursor": "pointer",
        },
    )
)

def build_login_layout():
    return dbc.Container(
        [
            app_header(center=True),
            dbc.Row(
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.H4(
                                    "Secure Access",
                                    className="mb-3",
                                    style={"textAlign": "center"},
                                ),
                                dcc.Input(
                                    type="password",
                                    style={"display": "none"},
                                    autoComplete="new-password"
                                ),

                                dcc.Input(
                                    id="user_input",
                                    type="text",
                                    placeholder="Username",
                                    className="form-control mb-3",
                                    autoComplete="off",
                                    name="fake-username"
                                ),

                                dcc.Input(
                                    id="pass_input",
                                    type="password",
                                    placeholder="Password",
                                    className="form-control mb-3",
                                    autoComplete="new-password",
                                    name="fake-password"
                                )

                                ,

                                dbc.Button(
                                    "Login",
                                    id="login-button",
                                    color="primary",
                                    style={"width": "100%"},
                                ),
                                html.Div(
                                    id="login-error",
                                    className="text-danger mt-2",
                                    style={"textAlign": "center"},
                                ),
                            ]
                        ),
                        className="login-card shadow-sm",
                    ),
                    width=12,
                    lg=4,
                ),
                justify="center",
                className="mt-4",
            ),
        ],
        fluid=True,
        className="pt-5",
    )

def build_athlete_selector_row(is_coach, options, default_tab):
    return dbc.Row(
        [
            dbc.Col(
                [
                    dbc.Label("Select athlete" if is_coach else "Athlete"),
                    dcc.Dropdown(
                        id="athlete-dropdown",
                        options=options,
                        value=default_tab,
                        clearable=False,
                        disabled=not is_coach,  # Athletes locked to their own sheet
                    ),
                ],
                lg=6, width=12,
            ),
        ],
        className="mb-3 g-3",
    )


def build_main_layout(auth_data):
    athlete_sheet = auth_data.get("athlete_sheet")
    is_coach = auth_data.get("is_coach", False)
    username = auth_data.get("username", "")

    tabs = list_tabs()

    if athlete_sheet and athlete_sheet in tabs:
        default_tab = athlete_sheet
    elif tabs:
        default_tab = tabs[0]
    else:
        default_tab = None

    # Build dropdown options
    if is_coach:
        options = []
        for key, info in USER_LOGINS.items():
            sheet_name = info.get("sheet", "")
            if sheet_name and sheet_name in tabs:
                options.append({"label": sheet_name, "value": sheet_name})
        athlete_dropdown_disabled = False
    else:
        if athlete_sheet and athlete_sheet in tabs:
            options = [{"label": athlete_sheet, "value": athlete_sheet}]
        else:
            options = []
        athlete_dropdown_disabled = True

    if default_tab is None and options:
        default_tab = options[0]["value"]

    # ✅ INSERT THIS FIXED ROW HERE
    athlete_selector_row = dbc.Row(
        [
            dbc.Col(
                [
                    dbc.Label("Select athlete" if is_coach else "Athlete"),
                    dcc.Dropdown(
                        id="athlete-dropdown",
                        options=options,
                        value=default_tab,
                        clearable=False,
                        disabled=not is_coach,
                    ),
                ],
                lg=6, width=12,
            )
        ],
        className="mb-3 g-3",
    )

    # --- Build sections (views) ---

    # 1) HOME VIEW → today + dials
    home_view = html.Div(
        id="home-view",
        children=[
            dbc.Row(
                [

                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.Div("Today", className="text-muted small"),
                                    html.H4(id="today-date", className="mb-0"),
                                ]
                            ),
                            className="mb-3 shadow-sm",
                        ),
                        lg=3, md=6, width=12,
                    ),
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.Div("Weekly Training Exposure", className="text-muted small"),
                                    html.Div(id="weekly-dial-container"),
                                ]
                            ),
                            className="mb-3 shadow-sm",
                        ),
                        lg=3, md=6, width=12,
                    ),
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.Div("Training Streak", className="text-muted small"),
                                    html.Div(id="streak-dial-container"),
                                ]
                            ),
                            className="mb-3 shadow-sm",
                        ),
                        lg=3, md=6, width=12,
                    ),
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.Div("Neuromuscular State", className="text-muted small"),
                                    html.Div(id="neuromuscular-dial-container"),
                                ]
                            ),
                            className="mb-3 shadow-sm",
                        ),
                        lg=3, md=6, width=12,
                    ),
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.Div("Training Readiness Index", className="text-muted small"),
                                    html.Div(id="readiness-dial-container"),
                                ]
                            ),
                            className="mb-3 shadow-sm",
                        ),
                        lg=3, md=6, width=12,
                    ),
                ],
                className="g-3",
            ),
            html.P(
                "Swipe between Calendar, Graphs and AI Session Builder using the bottom nav.",
                className="text-muted mt-2",
            ),
        ],
        style={"display": "block"},
    )

    # 2) CALENDAR VIEW → Program, calendar, pills, session details + AI feedback
    calendar_view = html.Div(
        id="calendar-view",
        children=[
            html.H4("Training Program", className="mt-3"),

            html.Div(
                [
                    html.Div(
                        [
                            dbc.Button(
                                "◀",
                                id="calendar-prev",
                                size="sm",
                                color="secondary",
                                outline=True,
                                className="me-2",
                            ),
                            html.Div(
                                id="calendar-window-label",
                                className="flex-grow-1 text-center small text-muted",
                                style={"minHeight": "24px"},
                            ),
                            dbc.Button(
                                "▶",
                                id="calendar-next",
                                size="sm",
                                color="secondary",
                                outline=True,
                                className="ms-2",
                            ),
                        ],
                        className="d-flex align-items-center justify-content-between mb-2",
                    ),
                    html.Div(id="calendar-grid", className="mb-4"),
                ]
            ),

            html.Hr(),

            html.H3("Selected Session & Athlete Input", className="mt-3"),
            html.Div(
                id="session-input-container",
                style={"display": "none"},
                children=[
                    dbc.Button(
                        "Close",
                        id="close-session-button",
                        color="secondary",
                        outline=True,
                        size="sm",
                        className="mb-3",
                    ),
                    dbc.Button(
                        "Reset",
                        id="reset-session-button",
                        color="warning",
                        outline=True,
                        size="sm",
                        className="mb-3 ms-2",
                    ),

                    html.H5(id="selected-date-header", className="mb-3"),

                    dbc.Row([
                        # LEFT: athlete inputs
                        dbc.Col([
                            # Athlete Notes
                            html.Div([
                                html.Label("Athlete Notes"),
                                dcc.Textarea(
                                    id="athlete-notes",
                                    placeholder="e.g., Last two reps were my best, powerful first step, strong projection, better stiffness on ground contact",
                                    style={
                                        "width": "100%", "height": "80px",
                                        "border": "none", "outline": "none",
                                        "padding": "10px", "background": "transparent"
                                    },
                                ),
                            ], style={
                                "border": "1px solid #e0e0e0",
                                "borderRadius": "10px",
                                "padding": "10px",
                                "boxShadow": "0 2px 4px rgba(0,0,0,0.08)",
                                "marginBottom": "12px",
                                "background": "#fafafa",
                            }),

                            # Sets × Reps × Load
                            html.Div([
                                html.Label("Sets × Reps × Load"),
                                dcc.Textarea(
                                    id="sets-reps-load",
                                    placeholder="e.g., Cleans - 4 × 5 @ 85kg",
                                    style={
                                        "width": "100%", "height": "80px",
                                        "border": "none", "outline": "none",
                                        "padding": "10px", "background": "transparent"
                                    },
                                ),
                            ], style={
                                "border": "1px solid #e0e0e0",
                                "borderRadius": "10px",
                                "padding": "10px",
                                "boxShadow": "0 2px 4px rgba(0,0,0,0.08)",
                                "marginBottom": "12px",
                                "background": "#fafafa",
                            }),

                            # Track reps & times
                            html.Div([
                                html.Label("Track Reps & Times"),
                                dcc.Textarea(
                                    id="track-reps-times",
                                    placeholder="e.g., 4 × 60m @ 6.85s",
                                    style={
                                        "width": "100%", "height": "80px",
                                        "border": "none", "outline": "none",
                                        "padding": "10px", "background": "transparent"
                                    },
                                ),
                            ], style={
                                "border": "1px solid #e0e0e0",
                                "borderRadius": "10px",
                                "padding": "10px",
                                "boxShadow": "0 2px 4px rgba(0,0,0,0.08)",
                                "marginBottom": "12px",
                                "background": "#fafafa",
                            }),

                            html.Br(),
                            dbc.Label("Session RPE (1=easy, 10=maximal)"),
                            dcc.Slider(
                                id="slider-session-rpe",
                                min=1, max=10, step=1, value=5,
                                marks={i: str(i) for i in range(1, 11)},
                                tooltip={"placement": "bottom"},
                            ),

                            html.Br(),
                            dbc.Label("Session Performance (1=poor, 5=excellent)"),
                            dcc.Slider(
                                id="slider-session",
                                min=1, max=5, step=1, value=3,
                                marks={i: str(i) for i in range(1, 6)},
                                tooltip={"placement": "bottom"},
                            ),

                            html.Br(),
                            dbc.Label("Fatigue (1=very tired, 5=fresh)"),
                            dcc.Slider(
                                id="slider-fatigue",
                                min=1, max=5, step=1, value=3,
                                marks={i: str(i) for i in range(1, 6)},
                                tooltip={"placement": "bottom"},
                            ),

                            html.Br(),
                            dbc.Label("Mood (1=sad, 5=upbeat)"),
                            dcc.Slider(
                                id="slider-mood",
                                min=1, max=5, step=1, value=3,
                                marks={i: str(i) for i in range(1, 6)},
                                tooltip={"placement": "bottom"},
                            ),

                            dbc.Button(
                                "Log Session & Generate AI Coaching Feedback",
                                id="btn-generate-ai",
                                color="secondary",
                                className="mt-3 w-100 ai-save-btn",
                            ),

                            html.Div(id="save-status", className="mt-2 text-success"),
                        ], md=6),

                        # RIGHT: AI feedback
                        dbc.Col([
                            dbc.Row([
                                dbc.Col([
                                    dbc.Label("Primary Coaching Feedback"),
                                    dcc.Dropdown(
                                        id="ai-mode-1",
                                        options=[
                                            {"label": "Speed & Power Coach", "value": "Speed & Power Coach"},
                                            {"label": "Tempo & Endurance Coach", "value": "Tempo & Endurance Coach"},
                                            {"label": "Technical Sprint Coach", "value": "Technical Sprint Coach"},
                                            {"label": "Strength & Power Coach", "value": "Strength & Power Coach"},
                                            {"label": "Recovery & Readiness Coach", "value": "Holistic Readiness Coach"},
                                        ],
                                        value=None,
                                        placeholder="Select Coach Feedback",
                                        searchable=False,  # ✅ removes the caret mark
                                        clearable=False,
                                        className="aw-dropdown"
                                    ),
                                ], md=6),
                                dbc.Col([
                                    dbc.Label("Secondary Coaching Feedback"),
                                    dcc.Dropdown(
                                        id="ai-mode-2",
                                        options=[
                                            {"label": "Speed & Power Coach", "value": "Speed & Power Coach"},
                                            {"label": "Tempo & Endurance Coach", "value": "Tempo & Endurance Coach"},
                                            {"label": "Technical Sprint Coach", "value": "Technical Sprint Coach"},
                                            {"label": "Strength & Power Coach", "value": "Strength & Power Coach"},
                                            {"label": "Recovery & Readiness Coach", "value": "Holistic Readiness Coach"},
                                        ],
                                        value=None,
                                        placeholder="Select Coach Feedback",
                                        searchable=False,  # ✅ removes the caret mark
                                        clearable=False,
                                        className="aw-dropdown"
                                    ),
                                ], md=6),
                            ], className="g-3"),

                            dcc.Loading(
                                id="ai-loader",
                                type="circle",
                                children=[
                                    html.Div(id="ai-suggestion-1", className="mt-3"),
                                    html.Div(id="ai-suggestion-2", className="mt-3"),
                                ]
                            ),
                        ], md=6),
                    ]),
                ]
            ),
        ],
        style={"display": "none"},
    )

    # 3) GRAPHS VIEW → load, wellness, speed/tempo
    graphs_view = html.Div(
        id="graphs-view",
        style={"display": "none"},  # shown only when nav selects it
        children=[

            html.Div(
                [
                    html.H3("Training Load, Wellness & Speed/Tempo", className="mb-3"),

                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label("View mode"),
                                    dcc.RadioItems(
                                        id="view-mode",
                                        options=[
                                            {"label": "Weekly", "value": "weekly"},
                                            {"label": "Daily", "value": "daily"},
                                        ],
                                        value="weekly",
                                        inline=True,
                                    ),
                                ],
                                width="auto",
                            ),
                            dbc.Col(
                                dbc.Button(
                                    "Refresh",
                                    id="refresh-btn",
                                    color="secondary",
                                    size="sm",
                                    className="mt-4",
                                ),
                                width="auto",
                            ),
                        ],
                        className="align-items-end mb-4",
                    ),

                    dcc.Graph(id="load-plot"),
                    dcc.Graph(id="wellness-plot"),
                    dcc.Graph(id="speedtempo-plot"),
                ]
            )
        ]
    )

    # 4) AI SESSION VIEW → session design generator
    ai_view = html.Div(
        id="ai-view",
        children=[
            html.H3("AI Training Session Builder", className="mt-3"),
            html.P(
                "Use this to generate a training session based on your current focus, "
                "recent trends, and upcoming load.",
                className="text-muted",
            ),
            dbc.Row([
                dbc.Col([
                    dbc.Label("Coaching Focus"),
                    dcc.Dropdown(
                        id="ai-plan-coach",
                        options=[
                            {"label": "Speed & Power Coach", "value": "Speed & Power Coach"},
                            {"label": "Tempo & Endurance Coach", "value": "Tempo & Endurance Coach"},
                            {"label": "Technical Sprint Coach", "value": "Technical Sprint Coach"},
                            {"label": "Strength & Power Coach", "value": "Strength & Power Coach"},
                            {"label": "Recovery & Readiness Coach", "value": "Recovery & Readiness Coach"},
                        ],
                        placeholder="Select your Coach",
                        clearable=False,
                    ),
                    html.Br(),
                    dbc.Label("Main session goal / focus"),
                    dcc.Textarea(
                        id="ai-plan-goal",
                        placeholder="e.g., Quality 60–80m accel work with low fatigue; keep hamstrings happy before Saturday comp.",
                        style={"width": "100%", "height": "80px"},
                    ),
                    html.Br(),
                    dbc.Label("Approx. session duration (min)"),
                    dcc.Input(
                        id="ai-plan-duration",
                        type="number",
                        min=10,
                        max=120,
                        step=5,
                        value=45,
                        className="form-control",
                    ),
                    html.Br(),
                    dbc.Button(
                        "Generate Session Plan",
                        id="btn-generate-plan",
                        color="primary",
                        className="w-100 mt-2",
                    ),
                    html.Div(id="ai-plan-status", className="mt-2 text-danger"),
                ], md=5),
                dbc.Col([
                    html.H5("Suggested Session Plan", className="mt-2"),
                    dcc.Loading(
                        children=html.Div(id="ai-plan-output", className="mt-2"),
                        type="circle",
                    ),
                ], md=7),
            ], className="g-3"),
        ],
        style={"display": "none"},
    )


    # Bottom nav (fixed)
    bottom_nav = html.Div(
        [
            # sliding underline (must come first so it sits behind icons)
            html.Div(id="nav-underline", className="nav-underline"),

            dbc.Row(
                [
                    dbc.Col(
                        html.Div(
                            [
                                html.I(id="icon-home", className="bi bi-house nav-icon"),
                                html.Div("Home", className="nav-label"),
                            ],
                            id="nav-home",
                            n_clicks=0,
                            className="nav-item",
                        )
                    ),
                    dbc.Col(
                        html.Div(
                            [
                                html.I(id="icon-calendar", className="bi bi-calendar-event nav-icon"),
                                html.Div("Calendar", className="nav-label"),
                            ],
                            id="nav-calendar",
                            n_clicks=0,
                            className="nav-item",
                        )
                    ),
                    dbc.Col(
                        html.Div(
                            [
                                html.I(id="icon-graphs", className="bi bi-bar-chart-line nav-icon"),
                                html.Div("Graphs", className="nav-label"),
                            ],
                            id="nav-graphs",
                            n_clicks=0,
                            className="nav-item",
                        )
                    ),
                    dbc.Col(
                        html.Div(
                            [
                                html.I(id="icon-ai", className="bi bi-cpu nav-icon"),
                                html.Div("AI", className="nav-label"),
                            ],
                            id="nav-ai",
                            n_clicks=0,
                            className="nav-item",
                        )
                    ),
                ],
                className="g-0",
            ),
        ],
        className="bottom-nav",
    )

    return dbc.Container(
        [
            app_header(center=False),
            logout_button,

            dcc.Store(id="selected-date-store"),
            dcc.Store(id="calendar-window-start"),

            # Athlete selector (only for coach)
            athlete_selector_row,

            # ---- YOUR FOUR SECTIONS MUST BE LIST ITEMS ↓↓↓ ----
            home_view,
            calendar_view,
            graphs_view,
            ai_view,

            bottom_nav,
        ],
        fluid=True,
        className="pb-5",
    )


# Root layout with splash (CSS will auto-hide it)
app.layout = html.Div(
    [
        dcc.Location(id="url", refresh=False),
        dcc.Store(id="auth-store", storage_type="session"),
        dcc.Store(id="active-nav", data="home"),
        dcc.Store(id="active-tab-store", data="home"),
        dcc.Store(id="bottom-nav-click", data="home"),
        # 👈 add this

        html.Div(
            id="splash-screen",
            children=[
                html.Img(src="/assets/app_icon.png", className="splash-logo"),
                html.H2("Adaptive Coaching Intelligence", className="splash-title"),
                html.P("AI-aligned athlete & coaching feedback", className="splash-subtitle"),
                html.Div(className="spinner")
            ]
        ),

        html.Div(
            id="page-content",
            style={"display": "block"},
            children=build_login_layout(),
        ),
    ]
)



# ============================================================
#  Callbacks
# ============================================================

# ---------------------------------------------------------
# BOTTOM NAVIGATION CALLBACK
# ---------------------------------------------------------

bottom_nav = html.Div(
    [
        html.Div(id="nav-underline", className="nav-underline"),

        dbc.Row(
            [
                dbc.Col(
                    html.Div([
                        html.I(id="icon-home", className="bi bi-house nav-icon"),
                        html.Div("Home", className="nav-label")
                    ], id="nav-home", n_clicks=0, className="nav-item"),
                ),
                dbc.Col(
                    html.Div([
                        html.I(id="icon-calendar", className="bi bi-calendar-check nav-icon"),
                        html.Div("Calendar", className="nav-label")
                    ], id="nav-calendar", n_clicks=0, className="nav-item"),
                ),
                dbc.Col(
                    html.Div([
                        html.I(id="icon-graphs", className="bi bi-graph-up-arrow nav-icon"),
                        html.Div("Graphs", className="nav-label")
                    ], id="nav-graphs", n_clicks=0, className="nav-item"),
                ),
                dbc.Col(
                    html.Div([
                        html.I(id="icon-ai", className="bi bi-cpu nav-icon"),
                        html.Div("AI", className="nav-label")
                    ], id="nav-ai", n_clicks=0, className="nav-item"),
                ),
            ],
            className="g-0",
        ),
    ],
    className="bottom-nav"
)



@app.callback(
    [
        Output("home-view", "style"),
        Output("calendar-view", "style"),
        Output("graphs-view", "style"),
        Output("ai-view", "style"),
        Output("bottom-nav-click", "data"),
    ],
    [
        Input("nav-home", "n_clicks"),
        Input("nav-calendar", "n_clicks"),
        Input("nav-graphs", "n_clicks"),
        Input("nav-ai", "n_clicks"),
    ],
)
def show_section(h, c, g, a):
    ctx = callback_context
    if not ctx.triggered:
        tab = "home"
    else:
        tab = ctx.triggered[0]["prop_id"].split(".")[0].replace("nav-", "")

    styles = {
        "home": {"display": "block"},
        "calendar": {"display": "block"},
        "graphs": {"display": "block"},
        "ai": {"display": "block"},
    }

    # hide all except target
    out = []
    for key in ["home", "calendar", "graphs", "ai"]:
        out.append(styles[key] if key == tab else {"display": "none"})

    out.append(tab)  # last output for bottom-nav-click
    return out



@app.callback(
    [
        Output("ai-plan-output", "children", allow_duplicate=True),
        Output("ai-plan-status", "children", allow_duplicate=True),
    ],
    Input("btn-generate-plan", "n_clicks"),
    [
        State("athlete-dropdown", "value"),
        State("ai-plan-coach", "value"),
        State("ai-plan-goal", "value"),
        State("ai-plan-duration", "value"),
    ],
    prevent_initial_call=True
)
def generate_ai_session_plan(n_clicks, athlete_name, coach_mode, goal, duration):

    if not n_clicks:
        raise PreventUpdate

    if not athlete_name:
        return no_update, "Select an athlete first."

    if not coach_mode:
        return no_update, "Choose a coach style."

    goal = (goal or "").strip()
    if not goal:
        return no_update, "Add a main goal/focus for the session."

    try:
        dur = int(duration) if duration is not None else 45
    except Exception:
        dur = 45

    df = load_tab(athlete_name)
    trend_context = build_trend_context(df, days=14)
    history_text = build_text_history(df, max_rows=7)
    persona = persona_prompt(coach_mode)

    system_msg = (
        persona
        + " You are now designing a single practical training session. "
          "It must be realistic for a track & field / field-sport athlete, "
          "appropriate to recent load and wellness trends, and not exceed the suggested duration. "
          "Do not diagnose injuries. Focus on structure: warm-up, main sets, and cool-down / recovery."
    )

    user_msg = (
        f"Athlete sheet: {athlete_name}\n"
        f"Session goal: {goal}\n"
        f"Target duration: ~{dur} minutes.\n\n"
        f"Recent trend summary:\n{trend_context}\n\n"
        f"Recent notes and training history:\n{history_text}\n\n"
        "TASK:\n"
        "- Design one session that fits the goal and duration.\n"
        "- Break it into clear sections (e.g., Warm-up, Main, Optional Top-up, Cool down).\n"
        "- Use simple bullet points and volumes (sets × reps, distances, rest).\n"
        "- Keep the language athlete-friendly and concise."
    )

    text = call_openai_chat(
        [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]
    )

    plan_div = html.Div(
        [
            html.Div(
                text,
                style={
                    "whiteSpace": "pre-wrap",
                    "fontSize": "0.95rem",
                },
            )
        ],
        className="ai-session-plan-card",
    )

    return plan_div, ""

@app.callback(
    Output("ai-plan-output", "children"),
    Input("btn-generate-ai-plan", "n_clicks"),
    State("athlete-dropdown", "value"),
    State("ai-plan-persona", "value"),
    State("ai-plan-focus", "value"),
    State("ai-plan-target", "value"),
    prevent_initial_call=True
)
def generate_ai_training_session(n, athlete_name, persona, focus, target_day):

    if not n:
        raise PreventUpdate

    # --- Load athlete data ---
    df = load_tab(athlete_name)
    today = today_adl()

    # --- Build contextual info ---
    trend_text = build_trend_context(df, 14)
    history_text = build_text_history(df, 7)
    upcoming_text = build_upcoming_context(df, today)

    # --- Determine the target date ---
    if target_day == "today":
        target_date = today
    else:
        d = df.copy()
        d["Date"] = pd.to_datetime(d["Date"], errors="coerce").dt.date

        fut = d[d["Date"] >= today]
        fut = fut[fut["Workout"].astype(str).str.strip() != ""]  # upcoming programmed sessions

        target_date = fut["Date"].min() if not fut.empty else today

    # --- Persona selection ---
    persona_prompt = SESSION_COACH_PERSONAS.get(persona, "")
    if not persona_prompt:
        persona_prompt = (
            "You are a supportive, evidence-informed performance coach who creates "
            "safe, high-quality sprint / conditioning / gym training sessions."
        )

    # --- System message (rules for the AI) ---
    system_msg = (
        persona_prompt +
        " You design complete training sessions including warmup, main block, "
        "and optional gym. Keep recommendations realistic, safe, clear, "
        "and never diagnose injuries or medical issues."
    )

    # --- User message (input info for the AI) ---
    user_msg = (
        f"Athlete: {athlete_name}\n"
        f"Target Date: {target_date}\n"
        f"Focus Area: {focus}\n\n"
        f"Recent Trends:\n{trend_text}\n\n"
        f"Recent Notes:\n{history_text}\n\n"
        f"Upcoming Sessions:\n{upcoming_text}\n\n"
        "TASK:\n"
        "- Design a complete training session.\n"
        "- Include WARMUP, MAIN BLOCK, and optional GYM WORK.\n"
        "- Specify distances, sets × reps, rest intervals, intensities.\n"
        "- Respect fatigue, mood, readiness, and upcoming loads.\n"
        "- Provide 2–3 coaching cues that fit the session.\n"
        "- Keep the structure clean, readable, and athlete friendly.\n"
    )

    # --- Call AI ---
    response = call_openai_chat([
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ])

    # --- HTML Return (Arial font + formatted card) ---
    return html.Div(
        [
            html.Div("🤖 AI Session Plan", className="ai-title"),
            html.Div(
                response,
                className="ai-plan-text",
                style={
                    "fontFamily": "Arial, sans-serif",
                    "whiteSpace": "pre-wrap",
                    "lineHeight": "1.45",
                    "fontSize": "15px"
                },
            ),
        ],
        className="ai-card ai-card-blue",
        style={"fontFamily": "Arial, sans-serif"}
    )


# --- Render page (login vs main) ---
@app.callback(
    Output("page-content", "children"),
    Input("auth-store", "data"),
)
def render_page(auth_data):
    if auth_data and auth_data.get("authed"):
        return build_main_layout(auth_data)
    return build_login_layout()



# --- Login ---
@app.callback(
    Output("auth-store", "data"),
    Output("login-error", "children"),
    Input("login-button", "n_clicks"),
    State("user_input", "value"),
    State("pass_input", "value"),
    prevent_initial_call=True
)
def do_login(n_clicks, username, password):
    if not n_clicks:
        raise PreventUpdate

    if not username or not password:
        return {"authed": False}, "Enter both username and password."

    # Check against USER_LOGINS loaded from env
    for athlete_key, info in USER_LOGINS.items():
        u = str(info.get("username", "")).strip().lower()
        p = str(info.get("password", "")).strip()
        role = str(info.get("role", "athlete")).lower()
        sheet = info.get("sheet", "")

        if username.strip().lower() == u and password.strip() == p:
            is_coach = (role == "coach")

            return (
                {
                    "authed": True,
                    "username": username.strip(),
                    "athlete_name": athlete_key,
                    "athlete_sheet": sheet,
                    "is_coach": is_coach,
                },
                ""
            )

    return {"authed": False}, "Incorrect username or password."


@app.callback(
    Output("auth-store", "data", allow_duplicate=True),
    Input("logout-button", "n_clicks"),
    prevent_initial_call=True,
)
def logout(n):
    if not n:
        raise PreventUpdate
    # Clear auth → sends user back to login screen
    return {"authed": False}




# --- Calendar window start (prev / next week + reset on athlete change) ---
@app.callback(
    Output("calendar-window-start", "data"),
    Output("calendar-window-label", "children"),
    Input("athlete-dropdown", "value"),
    Input("calendar-prev", "n_clicks"),
    Input("calendar-next", "n_clicks"),
    State("calendar-window-start", "data"),
)
def update_calendar_window(athlete_tab, prev_clicks, next_clicks, current_month):
    today = today_adl()


    # Default = first day of current month
    if current_month is None:
        month_date = today.replace(day=1)
    else:
        month_date = pd.to_datetime(current_month).date()

    triggered = callback_context.triggered[0]["prop_id"].split(".")[0]

    if triggered == "calendar-prev":
        # subtract one month
        year = month_date.year
        month = month_date.month - 1
        if month == 0:
            month = 12
            year -= 1
        month_date = dt.date(year, month, 1)

    elif triggered == "calendar-next":
        # add one month
        year = month_date.year
        month = month_date.month + 1
        if month == 13:
            month = 1
            year += 1
        month_date = dt.date(year, month, 1)

    label = month_date.strftime("%B %Y")
    return str(month_date), label



# --- Calendar grid (horizontal strip) ---
@app.callback(
    Output("calendar-grid", "children"),
    Input("athlete-dropdown", "value"),
    Input("calendar-window-start", "data"),
    Input("selected-date-store", "data"),
)
def update_calendar(athlete_tab, window_start, selected_date):
    if not athlete_tab:
        return "Select athlete."

    df = load_tab(athlete_tab)
    # Convert window_start to month anchor
    if window_start:
        month_date = pd.to_datetime(window_start).date().replace(day=1)
    else:
        month_date = dt.date.today().replace(day=1)

    return build_month_calendar(df, month_date, selected_date)


# --- Dashboard metrics & plots ---
@app.callback(
    Output("today-date", "children"),
    Output("weekly-dial-container", "children"),
    Output("streak-dial-container", "children"),
    Output("neuromuscular-dial-container", "children"),
    Output("readiness-dial-container", "children"),
    Output("load-plot", "figure"),
    Output("wellness-plot", "figure"),
    Output("speedtempo-plot", "figure"),
    Input("athlete-dropdown", "value"),
    Input("view-mode", "value"),
    Input("refresh-btn", "n_clicks"),
)
def update_dashboard(athlete_id, view_mode, n_clicks):
    """
    Dashboard updater:
    - today’s real date
    - weekly sessions via Athlete_Notes (0–7 dial)
    - neuromuscular state dial
    - readiness dial
    - load, wellness, speed/tempo plots
    """

    df = load_tab(athlete_id)



    # Today string (always real date)
    today = today_adl()
    today_date_str = today.strftime("%d %b %Y")

    # ------------------------
    # EMPTY DATAFRAME HANDLING
    # ------------------------
    if df is None or df.empty:
        empty = go.Figure()
        empty.update_layout(title="No Data")
        return (
            today_date_str,
            apple_sessions_ring(0),
            streak_dial(0),
            apple_neuromuscular_ring(None),
            apple_readiness_ring(None),
            empty,
            empty,
            empty,
        )

    # ------------------------
    # BUILD PLOTS
    # ------------------------
    load_fig = build_load_plot(df, view_mode)
    wellness_fig = build_wellness_plot(df, view_mode)
    speed_fig = build_speed_tempo_plot(df, view_mode)

    # ------------------------
    # WEEKLY SESSIONS (for dial)
    # Based on Athlete_Notes column only
    # ------------------------
    if "Athlete_Notes" in df.columns:
        df["Athlete_Notes"] = df["Athlete_Notes"].astype(str).str.strip()
        df_sessions = df[df["Athlete_Notes"] != ""].copy()
    else:
        df_sessions = df.copy()

    if df_sessions.empty:
        weekly_count = 0
    else:
        df_sessions.loc[:, "Date"] = pd.to_datetime(df_sessions["Date"], errors="coerce").dt.date

        # ISO weekday: Monday=0 ... Sunday=6
        # Using Saturday (5) as week anchor
        dow = today.weekday()
        days_since_sat = (dow - 5) % 7
        week_start = today - dt.timedelta(days=days_since_sat)
        week_end = week_start + dt.timedelta(days=6)

        weekly_count = df_sessions[
            (df_sessions["Date"] >= week_start) &
            (df_sessions["Date"] <= week_end)
        ].shape[0]

    # Compute streak
    streak, best = compute_streaks(df)
    streak_dial_ui = streak_dial(streak)

    # ------------------------
    # AVG FATIGUE / MOOD  (for neuromuscular dial)
    # ------------------------
    fatigue = pd.to_numeric(df.get("Fatigue_1_5"), errors="coerce")
    mood = pd.to_numeric(df.get("Mood_1_5"), errors="coerce")

    if fatigue.dropna().empty or mood.dropna().empty:
        avg_fm_val = None
    else:
        avg_fm_val = float((fatigue.mean() + mood.mean()) / 2)

    # ------------------------
    # READINESS INDEX (simple) - numeric for dial
    # ------------------------
    readiness_val = None
    try:
        load_vals = pd.to_numeric(df.get("Load"), errors="coerce").dropna()
        if not load_vals.empty:
            z = (load_vals - load_vals.mean()) / (load_vals.std() or 1)
            readiness_series = 5 - z.clip(-2, 2)
            readiness_val = float(readiness_series.iloc[-1])
    except Exception:
        readiness_val = None

    # ------------------------
    # RETURN EVERYTHING
    # ------------------------
    return (
        today_date_str,
        apple_sessions_ring(weekly_count),
        streak_dial_ui,
        apple_neuromuscular_ring(avg_fm_val),
        apple_readiness_ring(readiness_val),
        load_fig,
        wellness_fig,
        speed_fig,
    )


# --- Save & AI & Email ---
@app.callback(
    [
        Output("ai-suggestion-1", "children"),
        Output("ai-suggestion-2", "children"),
        Output("save-status", "children"),
    ],
    Input("btn-generate-ai", "n_clicks"),
    [
        State("athlete-dropdown", "value"),       # athlete_name
        State("selected-date-store", "data"),     # selected_date
        State("ai-mode-1", "value"),              # ai_mode_1
        State("ai-mode-2", "value"),              # ai_mode_2
        State("athlete-notes", "value"),          # notes
        State("sets-reps-load", "value"),         # sets_reps_load
        State("track-reps-times", "value"),       # track_reps_times
        State("slider-session-rpe", "value"),     # rpe
        State("slider-session", "value"),         # session
        State("slider-fatigue", "value"),         # fatigue
        State("slider-mood", "value"),            # mood
    ],
    prevent_initial_call=True,
)
def save_and_ai(
    n_clicks,
    athlete_name,
    selected_date,
    ai_mode_1,
    ai_mode_2,
    notes,
    sets_reps_load,
    track_reps_times,
    rpe,
    session,
    fatigue,
    mood,
):

    # 1. Button not clicked → do nothing
    if not n_clicks:
        raise PreventUpdate

    # 2. Personas not selected
    if not ai_mode_1 or not ai_mode_2:
        return no_update, no_update, "⚠️ Please select both coach personas."

    # 3. No date selected
    if not selected_date:
        return no_update, no_update, "⚠️ Please select a date from the calendar first."

    # NIL defaults
    notes = notes or "nil"
    sets_reps_load = sets_reps_load or "nil"
    track_reps_times = track_reps_times or "nil"


    # Load sheet
    df = load_tab(athlete_name)
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    selected_date_dt = pd.to_datetime(selected_date).date()

    row_matches = df.index[df["Date"] == selected_date_dt].tolist()
    if not row_matches:
        return None, None, "⚠️ No session entry exists for this date."

    row_idx = row_matches[0]

    # Generate AI suggestions (unified engine)
    ai1, ai2 = make_ai_suggestions(
        athlete_name,
        selected_date_dt,
        rpe,
        session,
        fatigue,
        mood,
        notes,
        sets_reps_load,
        track_reps_times,
        ai_mode_1,
        ai_mode_2,
    )

    # Write to sheet
    payload = {
        "RPE_Post_Session": rpe,
        "Session_1_5": session,
        "Fatigue_1_5": fatigue,
        "Mood_1_5": mood,
        "Athlete_Notes": notes,
        "Sets_Reps_Load": sets_reps_load,
        "Track_Reps_Times": track_reps_times,
        "AI_Suggestion_1": ai1,
        "AI_Suggestion_2": ai2,
        "Last_Updated": dt.datetime.now().isoformat(timespec="seconds"),
    }

    write_row(athlete_name, row_idx, payload)

    # Send email
    focus = df.loc[row_idx, "Focus"] if "Focus" in df.columns else "unspecified"
    send_email_payload({
        "sheet_name": athlete_name,
        "row": row_idx + 1,
        "athlete": athlete_name,
        "focus": focus,
        "date": str(selected_date_dt),
        "session_rpe": rpe,
        "session": session,
        "fatigue": fatigue,
        "mood": mood,
        "notes": notes,
        "sets_reps_load": sets_reps_load,
        "track_reps_times": track_reps_times,
        "ai_suggestion1": ai1,
        "ai_suggestion2": ai2,
        "Athlete_email": safe(df, row_idx, "Athlete_email"),
    })

    # Display – styled AI cards
    ai1_div = html.Div(
        html.Div([
            html.Div("💡 AI Suggestion 1", className="ai-title"),
            html.P(ai1),
        ], className="ai-card ai-card-green")
    )

    ai2_div = html.Div(
        html.Div([
            html.Div("💡 AI Suggestion 2", className="ai-title"),
            html.P(ai2),
        ], className="ai-card ai-card-blue")
    )

    return ai1_div, ai2_div, "✅ Saved, AI suggestions generated & email sent to Coach."


# ============================================================
#  Select calendar day → open session input panel
# ============================================================

@app.callback(
    Output("session-input-container", "style"),
    Output("selected-date-store", "data"),
    Output("selected-date-header", "children"),
    Input({"type": "calendar-day", "date": ALL}, "n_clicks"),
    State("athlete-dropdown", "value"),
    prevent_initial_call=True,
)
def on_day_click(n_clicks_list, athlete_name):

    # No clicks → stop
    if not n_clicks_list or all(n is None or n == 0 for n in n_clicks_list):
        raise PreventUpdate

    ctx = callback_context
    if not ctx.triggered:
        raise PreventUpdate

    # Extract clicked date
    triggered_id = json.loads(ctx.triggered[0]["prop_id"].split(".")[0])
    clicked_date = triggered_id["date"]
    clicked_date_dt = pd.to_datetime(clicked_date).date()


    # Lookup workout, RPE, Venue from selected athlete sheet
    df = load_tab(athlete_name)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date

    workout_txt = ""
    rpe_txt = ""
    venue_txt = ""

    matches = df.index[df["Date"] == clicked_date_dt].tolist()
    if matches:
        row = df.loc[matches[0]]

        workout = str(row.get("Workout", "")).strip()
        rpe = row.get("sRPE", "")
        venue = str(row.get("Venue", "")).strip()

        if workout:
            workout_txt = f" / Workout: {workout}"
        if pd.notna(rpe) and str(rpe).strip():
            rpe_txt = f" / RPE: {rpe}"
        if venue:
            venue_txt = f" / Venue: {venue}"

    # Final header
    # build collapsible session header card
    header = html.Div([
        # Toggle Button
        html.Button(
            "Session Details",
            id="session-header-toggle",
            n_clicks=0,
            className="session-header-toggle"
        )
,

        # Collapsible Content
        dbc.Collapse(
            dbc.Card(
                dbc.CardBody([

                    html.Div([
                        html.Span("📅 ", className="session-icon"),
                        html.Span("Selected Date: ", className="session-header-label"),
                        html.Span(str(clicked_date))
                    ], className="session-header-line"),

                    workout and html.Div([
                        html.Span("📝 ", className="session-icon"),
                        html.Span("Workout: ", className="session-header-label"),
                        html.Span(workout)
                    ], className="session-header-line"),

                    rpe and html.Div([
                        html.Span("🔥 ", className="session-icon"),
                        html.Span("RPE: ", className="session-header-label"),
                        html.Span(str(rpe))
                    ], className="session-header-line"),

                    venue and html.Div([
                        html.Span("📍 ", className="session-icon"),
                        html.Span("Venue: ", className="session-header-label"),
                        html.Span(venue)
                    ], className="session-header-line"),

                ]),
                className="session-header-card"
            ),
            id="session-header-collapse",
            is_open=False
        )
    ])

    return (
        {"display": "block"},   # show panel
        clicked_date,           # store date
        header,                 # header text
    )

@app.callback(
    Output("session-header-collapse", "is_open"),
    Input("session-header-toggle", "n_clicks"),
    State("session-header-collapse", "is_open"),
)
def toggle_session_header(n, is_open):
    if n:
        return not is_open
    return is_open



@app.callback(
    Output("session-input-container", "style", allow_duplicate=True),
    Output("selected-date-store", "data", allow_duplicate=True),
    Output("selected-date-header", "children", allow_duplicate=True),
    Input("close-session-button", "n_clicks"),
    prevent_initial_call=True,
)
def close_session_panel(n_clicks):
    return {"display": "none"}, None, ""

@app.callback(
    [
        Output("athlete-notes", "value"),
        Output("sets-reps-load", "value"),
        Output("track-reps-times", "value"),
        Output("slider-session-rpe", "value"),
        Output("slider-session", "value"),
        Output("slider-fatigue", "value"),
        Output("slider-mood", "value"),
        Output("ai-mode-1", "value"),
        Output("ai-mode-2", "value"),

        Output("ai-suggestion-1", "children", allow_duplicate=True),
        Output("ai-suggestion-2", "children", allow_duplicate=True),
        Output("save-status", "children", allow_duplicate=True),
    ],
    Input("reset-session-button", "n_clicks"),
    prevent_initial_call=True,
)
def reset_session_inputs(n):
    if not n:
        raise PreventUpdate

    return (
        "", "", "",      # text areas
        5, 3, 3, 3,       # slider resets
        None, None,       # dropdown resets
        "", "", "",       # AI 1, AI 2, save-status resets
    )

app.clientside_callback(
    """
    function(activeTab){
        // remove active class from all
        const tabs = ["home", "calendar", "graphs", "ai"];
        tabs.forEach(t => {
            document.getElementById("nav-" + t).classList.remove("active");
            document.getElementById("icon-" + t).classList.remove("bounce");
            document.getElementById("icon-" + t).classList.remove("wobble");
        });

        // add active class
        const active = document.getElementById("nav-" + activeTab);
        const icon = document.getElementById("icon-" + activeTab);

        if(active){
            active.classList.add("active");

            // bounce + wobble animation
            icon.classList.add("bounce");
            setTimeout(() => icon.classList.add("wobble"), 120);

            // underline movement
            const underline = document.getElementById("nav-underline");
            const index = tabs.indexOf(activeTab);
            if(underline){
                underline.style.transform = `translateX(${index * 100}%)`;
            }
        }

        return window.dash_clientside.no_update;
    }
    """,
    Output("active-tab-store", "data"),
    Input("bottom-nav-click", "data"),
)

# ============================================================
#  Run
# ============================================================

if __name__ == "__main__":
    app.run(debug=True)
