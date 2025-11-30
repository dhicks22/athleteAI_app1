import os
import json
import datetime as dt

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

def list_tabs():
    """Return worksheet titles (athlete sheets)."""
    if sh is None:
        return []
    return [ws.title for ws in sh.worksheets()]

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


def persona_prompt(mode: str) -> str:
    """
    Coaching personas tuned to be evidence-informed, short, and specific.
    """
    PERSONAS = {
        "100m Sprint":
            "You are an elite sprint coach synthesising the philosophies of "
            "Stu McMillan (ALTIS systems thinking & individualisation), "
            "Lance Brauman (max velocity development & race modelling), "
            "Ken Clark (acceleration biomechanics), and "
            "JB Morin & Matt Jordan (force diagnostics & eccentric strength). "
            "Your role is to interpret readiness markers and provide a precise next-step coaching action.",

        "400m Sprint":
            "You are a world-class 400m coach influenced by Clyde Hart and Stephen Francis. "
            "You emphasise rhythm, distribution, speed endurance, careful dose management, "
            "and avoiding unnecessary fatigue.",

        "S&C":
            "You are an S&C coach combining Mike Boyle (robust unilateral progressions), "
            "Matt Jordan (force diagnostics & tendon integrity), and "
            "JB Morin (force-velocity profiling). "
            "You interpret neuromuscular readiness and prescribe appropriate micro-adjustments.",

        "General":
            "You are a holistic sports performance coach integrating physical, psychological, "
            "and life-load readiness. You are specific, supportive, and realistic."
    }
    return PERSONAS.get(mode, PERSONAS["General"])


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
    Unified AI engine:
      - Uses 7-day context (load, wellness, ACWR)
      - Uses recent history (notes + AI suggestions)
      - Uses current session metrics (RPE, session rating, fatigue, mood)
      - Uses training inputs (notes, sets × reps × load, track reps & times)
      - Generates 2 persona-based suggestions (modes 1 & 2)
    """
    df = load_tab(athlete_name)

    # Context & history
    summary = build_context_summary(df)
    history = build_history_text(df)

    # Current session snapshot
    session_block = (
        f"Current session — date: {selected_date}\n"
        f"Session RPE (1–10): {session_rpe}\n"
        f"Session performance (1–5): {session}\n"
        f"Fatigue (1–5): {fatigue}\n"
        f"Mood (1–5): {mood}\n\n"
        f"Athlete notes: {notes}\n"
        f"Sets × Reps × Load: {sets_reps_load}\n"
        f"Track Reps & Times: {track_reps_times}\n"
    )

    def build_messages(mode: str):
        persona = persona_prompt(mode)
        history_text = history if history else "No previous note/AI history available."

        return [
            {
                "role": "system",
                "content": (
                    persona +
                    " Always remain evidence-informed. "
                    "Never guess injury, illness, or personal issues. "
                    "Base all recommendations strictly on the provided metrics and notes. "
                    "Respond in 2–3 sentences maximum."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"{summary}\n\n"
                    f"{history_text}\n\n"
                    f"{session_block}\n"
                    "Using these data points (load, sRPE, session performance, fatigue, mood, "
                    "and the training details provided), give a 2–3 sentence response that:\n"
                    "1. Interprets readiness and risk.\n"
                    "2. Gives one actionable recommendation for the next 24–48 hours.\n"
                    "3. Uses the coaching philosophy of your persona.\n"
                    "4. Is specific to this athlete and this session, not generic.\n"
                ),
            },
        ]

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
    """Convert dates to week-buckets starting Sat -> Fri."""
    return d.dt.to_period("W-SAT").apply(lambda r: r.start_time)


# ---------- Apple-style radial dials ----------
# CSS expects:
# .dial-wrapper, .dial-circle, .dial-text
# with CSS variables:
#   --dial-progress: 0–100
#   --dial-color: ring colour


def apple_sessions_ring(progress: int):
    """
    Radial dial for Weekly Training Exposure:
    - progress: # of sessions logged in current Sat–Fri week (0–7).
    - Colour map: red (low) → orange → green → blue (full week).
    """
    p = max(0, min(int(progress), 7))
    percent = (p / 7) * 100

    if p <= 2:
        color = "#E53935"   # red - underdone
    elif p <= 4:
        color = "#FB8C00"   # orange - moderate
    elif p <= 6:
        color = "#4CAF50"   # green - strong
    else:
        color = "#1E88E5"   # blue - full week, elite

    return html.Div(
        className="dial-wrapper",
        children=[
            html.Div(
                className="dial-circle",
                style={
                    "--dial-progress": f"{percent}",
                    "--dial-color": color,
                },
                children=[
                    html.Div(f"{p}/7", className="dial-text"),
                ],
            )
        ],
    )


def apple_neuromuscular_ring(avg_score: float | None):
    """
    Radial dial for Neuromuscular State:
    - avg_score: composite 1–5 from Fatigue & Mood.
    - Colour map:
        1–2   → red   (compromised)
        2–3   → orange (caution)
        3–4   → green (good)
        4–5   → blue  (prime)
    """
    if avg_score is None or np.isnan(avg_score):
        percent = 0
        color = "#CFD8DC"
        text = "—"
    else:
        v = max(1.0, min(float(avg_score), 5.0))
        percent = (v / 5.0) * 100
        if v < 2.0:
            color = "#E53935"
        elif v < 3.0:
            color = "#FB8C00"
        elif v < 4.0:
            color = "#4CAF50"
        else:
            color = "#1E88E5"
        text = f"{v:.1f}"

    return html.Div(
        className="dial-wrapper",
        children=[
            html.Div(
                className="dial-circle",
                style={
                    "--dial-progress": f"{percent}",
                    "--dial-color": color,
                },
                children=[
                    html.Div(text, className="dial-text"),
                ],
            )
        ],
    )


def apple_readiness_ring(readiness_score: float | None):
    """
    Radial dial for Training Readiness Index:
    - readiness_score: 1–5 style value (higher = more ready).
    - Colour map:
        <2   → red
        2–3  → orange
        3–4  → green
        >4   → blue
    """
    if readiness_score is None or np.isnan(readiness_score):
        percent = 0
        color = "#CFD8DC"
        text = "—"
    else:
        v = max(1.0, min(float(readiness_score), 5.0))
        percent = (v / 5.0) * 100
        if v < 2.0:
            color = "#E53935"
        elif v < 3.0:
            color = "#FB8C00"
        elif v < 4.0:
            color = "#4CAF50"
        else:
            color = "#1E88E5"
        text = f"{v:.1f}"

    return html.Div(
        className="dial-wrapper",
        children=[
            html.Div(
                className="dial-circle",
                style={
                    "--dial-progress": f"{percent}",
                    "--dial-color": color,
                },
                children=[
                    html.Div(text, className="dial-text"),
                ],
            )
        ],
    )




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

    # Determine month start/end
    year = month_date.year
    month = month_date.month
    first_day = dt.date(year, month, 1)
    last_day = (first_day.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)

    # Build grid: Always 6 rows × 7 columns (42 cells)
    start_weekday = first_day.weekday()  # Monday=0…Sunday=6
    # Convert to Sunday-start calendar
    start_offset = (start_weekday + 1) % 7

    days = []
    for i in range(42):
        day = first_day - dt.timedelta(days=start_offset) + dt.timedelta(days=i)
        days.append(day)

    selected_date = None
    if selected_date_str:
        try:
            selected_date = pd.to_datetime(selected_date_str).date()
        except:
            selected_date = None

    # Build calendar cells
    cells = []
    today = dt.date.today()

    for day in days:
        # Check entry in df
        match = ddf[ddf["Date"] == day]
        if not match.empty:
            row = match.iloc[-1]
            rpe = pd.to_numeric(row.get("sRPE", np.nan), errors="coerce")
        else:
            rpe = np.nan

        # Color logic
        if pd.isna(rpe):
            pill_color = "#CFD8DC"  # light grey
        elif rpe <= 2:
            pill_color = "#4285F4"  # blue
        elif 3 <= rpe <= 5:
            pill_color = "#4CAF50"  # green
        elif 6 <= rpe <= 7:
            pill_color = "#FF9800"  # orange
        else:
            pill_color = "#F44336"  # red

        # style for pill
        pill_style = {
            "width": "10px",
            "height": "10px",
            "borderRadius": "50%",
            "margin": "4px auto 0 auto",
            "backgroundColor": pill_color,
        }

        # cell highlight
        cell_style = {
            "width": "100%",
            "padding": "6px 0",
            "textAlign": "center",
            "borderRadius": "8px",
            "cursor": "pointer"
        }

        # Today glow
        if day == today:
            cell_style["boxShadow"] = "0 0 6px rgba(33,150,243,0.8)"

        # Selected date border
        if selected_date and day == selected_date:
            cell_style["border"] = "2px solid black"

        # Text fade for out-of-month days
        day_num_style = {
            "fontSize": "14px",
            "color": "#000" if (day.month == month) else "#B0BEC5"
        }

        cells.append(
            html.Div(
                [
                    html.Div(str(day.day), style=day_num_style),
                    html.Div(style=pill_style),
                ],
                id={"type": "calendar-day", "date": str(day)},
                n_clicks=0,
                style=cell_style,
            )
        )

    # 7-column grid container
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
        [
            html.Div(d, style={"textAlign": "center", "fontWeight": "600"})
            for d in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        ],
        style={
            "display": "grid",
            "gridTemplateColumns": "repeat(7, 1fr)",
            "marginBottom": "4px"
        }
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
app._favicon = "favicon.png"


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
                                    id="login-passcode",
                                    type="password",
                                    placeholder="Enter access code",
                                    className="form-control mb-3",
                                ),
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


def build_main_layout():
    tabs = list_tabs()
    default_tab = tabs[0] if tabs else None

    return dbc.Container(
        [
            app_header(center=False),

            dcc.Store(id="selected-date-store"),
            dcc.Store(id="calendar-window-start"),

            dbc.Row(
                [
                    dbc.Col(
                        [
                            dbc.Label("Select athlete sheet"),
                            dcc.Dropdown(
                                id="athlete-dropdown",
                                options=[{"label": t, "value": t} for t in tabs],
                                value=default_tab,
                                clearable=False,
                                placeholder="No sheets available" if not tabs else None,
                            ),
                        ],
                        lg=6, width=12,
                    ),
                    dbc.Col(
                        [
                            dbc.Label("View mode"),
                            dcc.RadioItems(
                                id="view-mode",
                                options=[
                                    {"label": "Weekly", "value": "weekly"},
                                    {"label": "Daily", "value": "daily"},
                                ],
                                value="weekly",  # <-- default is now WEEKLY
                                inline=True,
                            ),
                            dbc.Button(
                                "Refresh",
                                id="refresh-btn",
                                color="secondary",
                                size="sm",
                                className="mt-2",
                            ),
                        ],
                        lg=3, width=12,
                        className="mt-3 mt-lg-0",
                    ),
                ],
                className="mb-3 g-3",
            ),

            # Summary cards
            dbc.Row(
                [
                    # Current Date
                    dbc.Col(
                        dbc.Card(
                            dbc.CardBody(
                                [
                                    html.Div("Current Date", className="text-muted small"),
                                    html.H4(id="today-date", className="mb-0"),
                                ]
                            ),
                            className="mb-3 shadow-sm",
                        ),
                        lg=3, md=6, width=12,
                    ),
                    # Weekly Training Exposure Dial
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
                    # Neuromuscular State Dial
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
                    # Training Readiness Index Dial
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

            html.H4("Training Program", className="mt-4"),

            # Navigation controls + calendar strip
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
                    html.H5(id="selected-date-header", className="mb-3"),

                    dbc.Row([
                        # LEFT SIDE: Athlete Notes + Session Inputs
                        dbc.Col([

                            # Athlete Notes
                            html.Div([
                                html.Label("Athlete Notes"),
                                dcc.Textarea(
                                    id="athlete-notes",
                                    placeholder="e.g., Last two reps were my best, powerful first step, strong projection, and better stiffness on ground contact",
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

                            # Track Reps & Times
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
                                "Save & Generate AI Suggestions",
                                id="btn-generate-ai",
                                color="success",
                                className="mt-3 w-100",
                            ),

                            html.Div(id="save-status", className="mt-2 text-success"),
                        ], md=6),

                        # RIGHT SIDE — AI Focus & AI Output
                        dbc.Col([
                            dbc.Row([
                                dbc.Col([
                                    dbc.Label("AI Suggestion 1 Focus"),
                                    dcc.Dropdown(
                                        id="ai-mode-1",
                                        options=["100m Sprint", "400m Sprint", "S&C", "General"],
                                        value="400m Sprint",
                                        clearable=False,
                                    ),
                                ], md=6),

                                dbc.Col([
                                    dbc.Label("AI Suggestion 2 Focus"),
                                    dcc.Dropdown(
                                        id="ai-mode-2",
                                        options=["100m Sprint", "400m Sprint", "S&C", "General"],
                                        value="S&C",
                                        clearable=False,
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
                            )
                        ], md=6),
                    ]),
                ]
            ),

            html.Hr(),

            html.H3("Training Load / Readiness", className="mt-3"),
            dcc.Graph(id="load-plot", figure=go.Figure()),

            html.H3("Wellness (Session / RPE / Fatigue / Mood)", className="mt-4"),
            dcc.Graph(id="wellness-plot", figure=go.Figure()),

            html.H3("Speed & Tempo Volumes", className="mt-4"),
            dcc.Graph(id="speedtempo-plot", figure=go.Figure()),
        ],
        fluid=True,
        className="pb-5",
    )


# Root layout with splash (CSS will auto-hide it)
app.layout = html.Div(
    [
        dcc.Location(id="url", refresh=False),
        dcc.Store(id="auth-store", storage_type="session"),

        html.Div(
            id="splash-screen",
            children=[
                html.Img(src="/assets/app_icon.png", className="splash-logo"),
                html.H2("Adaptive Coaching Intelligence", className="splash-title"),
                html.P("AI-aligned athlete & coaching feedback", className="splash-subtitle"),
                html.Div(className="spinner")  # spiral loader
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

# --- Render page (login vs main) ---
@app.callback(
    Output("page-content", "children"),
    Input("auth-store", "data"),
)
def render_page(auth_data):
    if auth_data and auth_data.get("authed"):
        return build_main_layout()
    return build_login_layout()


# --- Login ---
@app.callback(
    Output("auth-store", "data"),
    Output("login-error", "children"),
    Input("login-button", "n_clicks"),
    State("login-passcode", "value"),
    prevent_initial_call=True,
)
def do_login(n_clicks, code):
    if not n_clicks:
        raise PreventUpdate
    if not code:
        return {"authed": False}, "Please enter access code."
    if code == APP_PASSCODE:
        return {"authed": True}, ""
    return {"authed": False}, "Incorrect access code."


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
    today = dt.date.today()

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
    today = dt.date.today()
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

    if not n_clicks:
        raise PreventUpdate

    if not selected_date:
        return None, None, "⚠️ Please select a date from the calendar first."

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
    header = f"Selected Date: {clicked_date}{workout_txt}{rpe_txt}{venue_txt}"

    return (
        {"display": "block"},   # show panel
        clicked_date,           # store date
        header,                 # header text
    )


@app.callback(
    Output("session-input-container", "style", allow_duplicate=True),
    Output("selected-date-store", "data", allow_duplicate=True),
    Output("selected-date-header", "children", allow_duplicate=True),
    Input("close-session-button", "n_clicks"),
    prevent_initial_call=True,
)
def close_session_panel(n_clicks):
    return {"display": "none"}, None, ""


# ============================================================
#  Run
# ============================================================

if __name__ == "__main__":
    app.run(debug=True)
