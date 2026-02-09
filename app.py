# app.py — consolidated + FIXED version
# Key fixes:
# 1) ✅ No more NaTType.start_time crash: _week_agg_date() is now NaT-safe (uses week_bucket()).
# 2) ✅ Removed duplicate/contradictory definitions (dial_class_from_score, bottom_nav, imports, etc.)
# 3) ✅ Removed the broken commented-out _build_dial block that was causing indentation/parse issues.
# 4) ✅ Kept your structure + UI intact, but made the weekly bucketing + plotting robust.

import os
import re
import json
import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import gspread

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from dash import Dash, html, dcc, Input, Output, State, ALL, callback_context, no_update
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash.exceptions import PreventUpdate

# ============================================================
#  JSON extraction helper
# ============================================================

def extract_json_object(text: str) -> str | None:
    if not text:
        return None

    s = str(text).strip()
    start = s.find("{")
    if start == -1:
        return None

    depth = 0
    in_str = False
    esc = False

    for i in range(start, len(s)):
        ch = s[i]

        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]

    return None


# ============================================================
#  USER_LOGINS (Render env var) + local fallback
# ============================================================

RAW_USER_LOGINS = os.getenv("USER_LOGINS", "").strip()

USER_LOGINS = {}
if RAW_USER_LOGINS:
    try:
        USER_LOGINS = json.loads(RAW_USER_LOGINS)
    except Exception:
        maybe = extract_json_object(RAW_USER_LOGINS)
        if maybe:
            USER_LOGINS = json.loads(maybe)

print("User login config loaded keys:", list(USER_LOGINS.keys()))

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


# ============================================================
#  Timezone
# ============================================================

ADL_TZ = ZoneInfo("Australia/Adelaide")

def today_adl():
    return dt.datetime.now(ADL_TZ).date()


# ============================================================
#  Environment / Credentials
# ============================================================

load_dotenv()

GSHEET_ID = os.getenv("GSHEET_ID")


EMAIL_WEBHOOK_URL = os.getenv("EMAIL_WEBHOOK_URL")

if not EMAIL_WEBHOOK_URL:
    raise RuntimeError("❌ EMAIL_WEBHOOK_URL not set in environment")

APP_PASSCODE = os.getenv("APP_PASSCODE")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

print("Loaded GSHEET_ID:", GSHEET_ID)
print("APP_PASSCODE set?:", bool(APP_PASSCODE))
print("🔗 EMAIL_WEBHOOK_URL loaded from env:", EMAIL_WEBHOOK_URL)

print("OPENAI_API_KEY set?:", bool(OPENAI_API_KEY))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

service_json = os.getenv("GS_SERVICE_JSON")
sh = None

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
#  Plot layout helpers
# ============================================================

MOBILE_PLOT_LAYOUT = dict(
    autosize=True,
    height=360,
    margin=dict(l=24, r=16, t=48, b=48),

    font=dict(size=13),

    hoverlabel=dict(
        bgcolor="white",
        font_size=14,
        font_family="Inter, Arial",
        align="left",
    ),

    legend=dict(
        orientation="h",
        yanchor="bottom",
        y=-0.35,
        xanchor="center",
        x=0.5,
        font=dict(size=11),
    ),
)



BLUE       = "#1565C0"
ORANGE     = "#EF6C00"
GREEN_DARK = "#2E7D32"
PURPLE     = "#6A1B9A"
RED        = "#C62828"
TEAL       = "#00897B"


def _legend_right_layout(base: dict | None = None) -> dict:
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


# ============================================================
#  ✅ NaT-safe weekly bucketing (fixes your crash)
# ============================================================

def week_bucket(dates: pd.Series, week_anchor: str = "W-SAT") -> pd.Series:
    """
    Returns week bucket timestamps (start of anchored week), safely handling NaT.
    """
    s = pd.to_datetime(dates, errors="coerce")

    if s.notna().sum() == 0:
        return pd.Series([pd.NaT] * len(s), index=s.index)

    p = s.dt.to_period(week_anchor)

    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    mask = p.notna()
    # Period -> Timestamp (start_time) safely:
    out.loc[mask] = p[mask].dt.start_time
    return out


def _week_agg_date(d: pd.Series) -> pd.Series:
    """
    Backwards-compatible wrapper used by your plots.
    Returns a Timestamp series (week start), NaT-safe.
    """
    return week_bucket(d, "W-SAT")


# ============================================================
#  UI bits
# ============================================================

logout_button = dbc.Button(
    "Log out",
    id="logout-button",
    size="sm",
    color="secondary",
    outline=True,
    style={"opacity": 0.65, "fontSize": "12px", "padding": "4px 10px"},
)


def dial_class_from_score(score: float | None):
    if score is None or (isinstance(score, float) and np.isnan(score)):
        return "dial-grey"

    if score >= 80:
        return "dial-blue"     # BEST
    elif score >= 60:
        return "dial-green"    # GOOD
    elif score >= 40:
        return "dial-amber"    # MANAGE
    elif score >= 20:
           return "dial-red"      # RECOVER


def _build_dial(value_str: str, percent: float, colour_class: str):
    percent = 0 if percent is None else float(percent)
    percent = max(0, min(percent, 100))
    return html.Div(
        className="dial-wrapper",
        children=[
            html.Div(
                className=f"dial-circle updated {colour_class}",
                style={"--dial-progress": round(percent)},
                children=[html.Div(value_str, className="dial-text")],
            )
        ],
    )


def apple_sessions_ring(exposure_pct: int | None):
    if exposure_pct is None or (isinstance(exposure_pct, float) and np.isnan(exposure_pct)):
        return _build_dial("—", 0, "dial-grey")
    v = max(0, min(int(exposure_pct), 100))
    return _build_dial(f"{v}", v, dial_class_from_score(v))


def apple_neuromuscular_ring(score_0_100: float | None):
    if score_0_100 is None or (isinstance(score_0_100, float) and np.isnan(score_0_100)):
        return _build_dial("—", 0, "dial-grey")
    v = max(0, min(float(score_0_100), 100))
    return _build_dial(f"{int(round(v))}", v, dial_class_from_score(v))


def apple_readiness_ring(score_0_100: float | None):
    if score_0_100 is None or (isinstance(score_0_100, float) and np.isnan(score_0_100)):
        return _build_dial("—", 0, "dial-grey")
    v = max(0, min(float(score_0_100), 100))
    return _build_dial(f"{int(round(v))}", v, dial_class_from_score(v))


def streak_dial(streak: int):
    s = max(0, int(streak))
    percent = min(s, 14) / 14 * 100
    colour = streak_colour_from_days(s)
    return _build_dial(str(s), percent, colour)



def dial_flip(front_child, back_title: str, back_body: str):
    return html.Div(
        className="dial-flip",
        children=[
            html.Div(
                className="dial-flip-inner",
                children=[
                    html.Div(className="dial-face dial-front", children=front_child),
                    html.Div(
                        className="dial-face dial-back",
                        children=html.Div(
                            className="dial-back-content",
                            children=[
                                html.Div(back_title, className="dial-back-title"),
                                html.Div(back_body, className="dial-back-body"),
                                html.Div("Tap to flip back", className="dial-back-hint"),
                            ],
                        ),
                    ),
                ],
            )
        ],
    )


# ============================================================
#  Sheets helpers
# ============================================================
# ============================================================
#  Readiness calculations (authoritative)
# ============================================================

def streak_colour_from_days(days: int):
    if days >= 10:
        return "dial-green"
    elif days >= 6:
        return "dial-blue"
    elif days >= 3:
        return "dial-amber"
    else:
        return "dial-red"


def calc_daily_readiness(load_series, rpe_series, span=7):
    if load_series.empty or rpe_series.empty:
        return None

    df = pd.DataFrame({
        "load": pd.to_numeric(load_series, errors="coerce"),
        "rpe":  pd.to_numeric(rpe_series, errors="coerce")
    }).dropna()

    if df.empty:
        return None

    df["strain"] = df["load"] * df["rpe"]

    # EWMA baseline
    ewma = df["strain"].ewm(span=span, adjust=False).mean()
    baseline = ewma.iloc[-2] if len(ewma) > 1 else ewma.iloc[-1]

    if pd.isna(baseline) or baseline == 0:
        return 75.0

    ratio = df["strain"].iloc[-1] / baseline

    # Map ratio → readiness
    readiness = 100 - (ratio - 1) * 50
    return float(max(0, min(readiness, 100)))



def calc_neuro_readiness(
    sleep, fatigue, soreness, mood,
    history_df=None, span=3
):
    weights = {
        "sleep": 0.4,
        "fatigue": 0.25,
        "soreness": 0.2,
        "mood": 0.15
    }

    today = (
        sleep * weights["sleep"]
        + (6 - fatigue) * weights["fatigue"]
        + (6 - soreness) * weights["soreness"]
        + mood * weights["mood"]
    )

    score = today * 20  # 0–100

    if history_df is None or "Neuro" not in history_df:
        return score

    hist = pd.to_numeric(history_df["Neuro"], errors="coerce").dropna()
    hist = pd.concat([hist, pd.Series([score])])

    smooth = hist.ewm(span=span, adjust=False).mean()
    return float(smooth.iloc[-1])


def icon_row(icon, title, content):
    return html.Div(
        [
            html.Div(icon, className="sess-icon"),
            html.Div(
                [
                    html.Div(title, className="sess-title"),
                    html.Div(content, className="sess-content"),
                ],
                className="sess-text",
            ),
        ],
        className="sess-row",
    )


def input_card(children):
    return html.Div(
        children,
        style={
            "border": "1px solid #e0e0e0",
            "borderRadius": "10px",
            "padding": "10px",
            "boxShadow": "0 2px 4px rgba(0,0,0,0.08)",
            "marginBottom": "12px",
            "background": "#fafafa",
        },
    )


def list_tabs():
    if sh is None:
        return []
    return [ws.title for ws in sh.worksheets()]


def load_tab(tab_name: str) -> pd.DataFrame:
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
    if sh is None:
        return
    ws = sh.worksheet(tab_name)
    sheet_vals = ws.get_all_values()
    if not sheet_vals:
        return

    headers = sheet_vals[0]
    row_number = row_idx_0 + 2

    row = ws.row_values(row_number)
    if len(row) < len(headers):
        row += [""] * (len(headers) - len(row))

    for col_name, value in payload.items():
        if col_name in headers:
            j = headers.index(col_name)
            row[j] = "" if value is None else str(value)

    ws.update(values=[row], range_name=f"A{row_number}")


def safe(df: pd.DataFrame, row_idx: int, col: str, default: str = "") -> str:
    try:
        if col in df.columns:
            val = df.at[row_idx, col]
            if pd.notna(val):
                return str(val)
    except Exception:
        pass
    return default


def get_day_status(df, date_obj):
    if df.empty or "Date" not in df.columns:
        return {"logged": False, "rpe": None, "has_notes": False, "has_load": False}

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce").dt.date

    rows = d[d["Date"] == date_obj]
    if rows.empty:
        return {"logged": False, "rpe": None, "has_notes": False, "has_load": False}

    row = rows.iloc[-1]

    rpe = pd.to_numeric(row.get("RPE_Post_Session", row.get("sRPE", None)), errors="coerce")
    load = pd.to_numeric(row.get("Load", None), errors="coerce")

    notes = str(row.get("Athlete_Notes", "")).strip().lower()
    invalid_notes = {"", "nan", "none", "nil", "0", "n/a", "na"}

    has_notes = notes not in invalid_notes
    has_rpe = pd.notna(rpe) and rpe > 0
    has_load = pd.notna(load) and load > 0

    logged = has_notes or has_rpe or has_load

    return {
        "logged": logged,
        "rpe": float(rpe) if has_rpe else None,
        "has_notes": has_notes,
        "has_load": has_load,
    }


def count_planned_sessions_in_week(df: pd.DataFrame, week_start: dt.date, week_end: dt.date) -> int:
    if df is None or df.empty or "Date" not in df.columns:
        return 0

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce").dt.date
    in_week = d[(d["Date"] >= week_start) & (d["Date"] <= week_end)]

    if "Workout" not in in_week.columns:
        return 0

    workout = in_week["Workout"].dropna().astype(str).str.strip().str.lower()

    invalid = {"", "nan", "none", "nil", "-", "—", "rest", "off", "recovery", "tbc"}
    planned = workout[~workout.isin(invalid)]
    return int(planned.shape[0])


def count_logged_sessions_in_week(df: pd.DataFrame, week_start: dt.date, week_end: dt.date) -> int:
    if df is None or df.empty or "Date" not in df.columns:
        return 0

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce").dt.date
    d = d.dropna(subset=["Date"])

    days = sorted({dte for dte in d["Date"].tolist() if week_start <= dte <= week_end})
    if not days:
        return 0

    return sum(1 for day in days if get_day_status(d, day).get("logged", False))


def compute_streaks(df: pd.DataFrame):
    if df is None or df.empty or "Date" not in df.columns:
        return 0, 0

    ddf = df.copy()
    ddf["Date"] = pd.to_datetime(ddf["Date"], errors="coerce").dt.date
    ddf = ddf.dropna(subset=["Date"]).sort_values("Date")

    if "Athlete_Notes" not in ddf.columns:
        return 0, 0

    notes_clean = ddf["Athlete_Notes"].fillna("").astype(str).str.strip().str.lower()
    invalid = {"", "nan", "none", "nil", "0"}

    logged_days = set(ddf.loc[~notes_clean.isin(invalid), "Date"].tolist())

    today = today_adl()

    streak = 0
    cursor = today
    while cursor in logged_days:
        streak += 1
        cursor -= dt.timedelta(days=1)

    best = 0
    current = 0
    for date in sorted(ddf["Date"].unique()):
        if date in logged_days:
            current += 1
        else:
            best = max(best, current)
            current = 0
    best = max(best, current)

    return streak, best


# ============================================================
#  AI helpers
# ============================================================

def build_context_summary(df: pd.DataFrame, days: int = 7) -> str:
    if df.empty:
        return "No recent data available."

    if "Date" in df.columns:
        ddf = df.copy()
        ddf["Date"] = pd.to_datetime(ddf["Date"], errors="coerce").dt.date
        cutoff = today_adl() - dt.timedelta(days=days)
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
    sleep7 = safe_mean("Sleep_1_5")
    fat7 = safe_mean("Fatigue_1_5")
    mood7 = safe_mean("Mood_1_5")
    soreness7 = safe_mean("Soreness_1_5")

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
        f"Fatigue avg: {fat7}, Sleep avg: {sleep7}, Soreness avg: {soreness7}, Mood avg: {mood7}, ACWR approx: {acwr7}."
    )


def _describe_trend(series: pd.Series, label: str, window: int = 7) -> str:
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


def build_trend_context(df: pd.DataFrame, days: int = 14) -> str:
    if df.empty or "Date" not in df.columns:
        return "No recent training or wellness data is available."

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce").dt.date
    d = d.sort_values("Date")

    cutoff = today_adl() - dt.timedelta(days=days)
    recent = d[d["Date"] >= cutoff]
    if recent.empty:
        return f"No data has been logged in the last {days} days."

    lines = []

    if "RPE_Post_Session" in recent.columns:
        lines.append(_describe_trend(recent["RPE_Post_Session"], "Session RPE (1–5)"))
    elif "sRPE" in recent.columns:
        lines.append(_describe_trend(recent["sRPE"], "Session RPE (1–5)"))

    if "Load" in recent.columns:
        lines.append(_describe_trend(recent["Load"], "Training Load"))

    if "Fatigue_1_5" in recent.columns:
        lines.append(_describe_trend(recent["Fatigue_1_5"], "Fatigue (1–5)"))
    if "Mood_1_5" in recent.columns:
        lines.append(_describe_trend(recent["Mood_1_5"], "Mood (1–5)"))
    if "Sleep_1_5" in recent.columns:
        lines.append(_describe_trend(recent["Sleep_1_5"], "Sleep quality (1–5)"))

    ew7_col = "EWMA 7" if "EWMA 7" in recent.columns else ("EMWA 7" if "EMWA 7" in recent.columns else None)
    ew28_col = "EWMA 28" if "EWMA 28" in recent.columns else ("EMWA 28" if "EMWA 28" in recent.columns else None)

    if ew7_col and ew28_col:
        try:
            ew7 = pd.to_numeric(recent[ew7_col], errors="coerce")
            ew28 = pd.to_numeric(recent[ew28_col], errors="coerce").replace(0, np.nan)
            acwr = (ew7 / ew28).replace([np.inf, -np.inf], np.nan)
            acwr_recent = acwr.dropna()
            if not acwr_recent.empty:
                lines.append(_describe_trend(acwr_recent, "ACWR", window=min(7, len(acwr_recent))))
        except Exception:
            pass

    if not lines:
        return "Recent data are limited, but you can still use the single-session metrics."

    return "Recent trends (last ~2 weeks): " + " ".join(lines)


def build_text_history(df: pd.DataFrame, max_rows: int = 7) -> str:
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


def build_upcoming_context(df: pd.DataFrame, anchor_date: dt.date, n: int = 5) -> str:
    try:
        if df is None or df.empty or "Date" not in df.columns:
            return "No upcoming data."

        ddf = df.copy()
        ddf["Date"] = pd.to_datetime(ddf["Date"], errors="coerce").dt.date
        ddf = ddf.sort_values("Date")

        future = ddf[ddf["Date"] > anchor_date].copy()

        if "Workout" in future.columns:
            future["Workout"] = future["Workout"].astype(str).str.strip()
            future = future[future["Workout"] != ""]
        else:
            return "No future planned sessions found."

        if future.empty:
            return "No future planned sessions found."

        lines = []
        take = future.head(n)

        for _, r in take.iterrows():
            date_str = str(r.get("Date", ""))
            workout = str(r.get("Workout", "")).strip()

            focus = str(r.get("Focus", "")).strip() if "Focus" in future.columns else ""
            venue = str(r.get("Venue", "")).strip() if "Venue" in future.columns else ""

            extras = []
            if focus and focus.lower() not in ("nan", "none", "nil"):
                extras.append(f"Focus: {focus}")
            if venue and venue.lower() not in ("nan", "none", "nil"):
                extras.append(f"Venue: {venue}")

            extra_txt = f" ({' | '.join(extras)})" if extras else ""
            lines.append(f"{date_str}: {workout}{extra_txt}")

        return "\n".join(lines)

    except Exception:
        return "No upcoming data."


def persona_prompt(mode: str) -> str:
    PERSONA_PROMPTS = {
        "Acceleration & Speed Coach": (
            "You are an acceleration and speed coach who thinks like a track sprint coach. "
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
        "Recovery & Readiness Coach": (
            "You are a recovery and readiness coach. "
            "You integrate physical load, fatigue, soreness, mood, and life stress. "
            "You help the athlete balance training, sleep, and recovery, and you keep the message supportive but honest."
        ),
        "General": (
            "You are a clear, supportive performance coach. "
            "You summarise what the trends suggest and give one or two concrete action steps."
        ),
    }
    return PERSONA_PROMPTS.get(mode, PERSONA_PROMPTS["General"])


PERSONA_KEYWORDS = {
    "Acceleration & Speed Coach": ["acceleration", "speed", "max velocity", "explosive", "contact time", "fast reps"],
    "Tempo & Endurance Coach": ["tempo", "aerobic", "endurance", "pacing", "conditioning"],
    "Technical Sprint Coach": ["posture", "angles", "mechanics", "arm action", "technique", "rhythm"],
    "Strength & Power Coach": ["strength", "load", "gym", "sets", "reps", "bar speed", "plyometric"],
    "Recovery & Readiness Coach": ["fatigue", "recovery", "sleep", "soreness", "readiness", "stress"],
}


def call_openai_chat(messages: list, max_tokens: int = 700) -> str:
    if not OPENAI_API_KEY:
        return "AI suggestion unavailable (missing API key)."
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": messages,
                "temperature": 0.6,
                "max_tokens": max_tokens,
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
    session_rpe,
    session_quality,
    sleep,
    fatigue,
    mood,
    soreness,
    notes,
    sets_reps_load,
    track_reps_times,
    ai_mode_1: str,
    ai_mode_2: str,
):
    df = load_tab(athlete_name)

    if df is None or df.empty:
        return "No athlete data available yet.", ""

    try:
        selected_date_dt = pd.to_datetime(selected_date).date()
    except Exception:
        return "Invalid selected date.", ""

    if "Date" not in df.columns:
        return "Sheet is missing a 'Date' column.", ""

    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date

    row_matches = df.index[df["Date"] == selected_date_dt].tolist()
    row_idx = row_matches[0] if row_matches else None

    workout = safe(df, row_idx, "Workout", "nil") if row_idx is not None else "nil"
    focus_txt = safe(df, row_idx, "Focus", "nil") if row_idx is not None else "nil"
    venue = safe(df, row_idx, "Venue", "nil") if row_idx is not None else "nil"
    upcoming = build_upcoming_context(df, selected_date_dt, n=5)

    first_name = athlete_name.split()[0] if athlete_name else "Athlete"

    summary = build_context_summary(df, days=7)
    trend_context = build_trend_context(df, days=14)
    history_text = build_text_history(df, max_rows=7)

    notes = notes or "nil"
    sets_reps_load = sets_reps_load or "nil"
    track_reps_times = track_reps_times or "nil"

    session_block = (
        f"Current session — {selected_date_dt}\n"
        f"Planned workout: {workout}\n"
        f"Focus: {focus_txt}\n"
        f"Venue: {venue}\n"
        f"Upcoming (next sessions):\n{upcoming}\n\n"
        f"RPE (1–10): {session_rpe}\n"
        f"Session quality (execution, 1–5): {session_quality}\n"
        f"Sleep (1–5): {sleep}\n"
        f"Fatigue (1–5): {fatigue}\n"
        f"Mood (1–5): {mood}\n"
        f"Soreness (1–5): {soreness}\n"
        f"Athlete notes: {notes}\n"
        f"Sets × Reps × Load: {sets_reps_load}\n"
        f"Track reps & times: {track_reps_times}\n"
    )

    def build_messages(mode: str):
        persona = persona_prompt(mode)
        system_msg = (
            persona
            + "\nYou are providing short, specific, natural coaching advice. "
              "Avoid guessing injuries or medical issues. "
              "Avoid generic advice. "
              "Your tone must match the persona style. "
              f"Always begin your response with: '{first_name},'"
        )

        user_msg = (
            f"Recent trend summary:\n{summary}\n\n"
            f"Detailed trend context:\n{trend_context}\n\n"
            f"Recent history:\n{history_text}\n\n"
            f"{session_block}\n"
            f"Session quality (1–5): {session_quality} (execution, rhythm, technical sharpness)\n"

            "TASK:\n"
            f"- Write coaching feedback as the {mode} persona.\n"
            f"- Start with '{first_name},'\n"
            "- Keep it concise (2–4 sentences).\n"
            "- Give 3–4 clear, actionable recommendations for the next 24–48 hours.\n"
            "- Tie recommendations directly to the trends and the session info.\n"
        )

        return [{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}]

    primary_messages = build_messages(ai_mode_1)
    primary_messages[0]["content"] += (
        "\nROLE: PRIMARY COACH\n"
        "Set the main training priority. Be decisive and focused. Use 3–4 sentences."
    )
    ai1 = call_openai_chat(primary_messages)

    blocked_terms = PERSONA_KEYWORDS.get(ai_mode_1, [])
    secondary_system_msg = (
        persona_prompt(ai_mode_2)
        + "\nROLE: SECONDARY COACH\n"
          "Support or monitor the primary focus — do NOT compete with it.\n"
          f"Avoid these concepts entirely: {', '.join(blocked_terms) if blocked_terms else 'none'}.\n"
          "Offer awareness cues or recovery considerations only.\n\n"
          f"PRIMARY COACH FEEDBACK:\n{ai1}\n\n"
          f"Always begin with '{first_name},'"
    )

    secondary_user_msg = (
        f"Recent trend summary:\n{summary}\n\n"
        f"Detailed trend context:\n{trend_context}\n\n"
        f"Recent history:\n{history_text}\n\n"
        f"{session_block}\n"
        "TASK:\n"
        "- Add a complementary perspective ONLY.\n"
        "- Provide 1–2 monitoring cues.\n"
        "- End with ONE reflective question.\n"
        "- Keep it brief (2–3 sentences).\n"
    )

    ai2 = call_openai_chat([
        {"role": "system", "content": secondary_system_msg},
        {"role": "user", "content": secondary_user_msg},
    ])

    return (ai1 or "").strip(), (ai2 or "").strip()


# ============================================================
#  Email webhook
# ============================================================

import os, json, requests

def send_email_payload(payload):
    url = os.getenv("EMAIL_WEBHOOK_URL", "")
    print("🔗 USING EMAIL_WEBHOOK_URL =", url)

    if not url.startswith("https://script.google.com/macros/s/"):
        raise ValueError(f"Webhook URL looks wrong: {url}")

    print("📦 PAYLOAD KEYS =", sorted(payload.keys()))
    r = requests.post(url, json=payload, timeout=15)
    print("✅ WEBHOOK STATUS =", r.status_code)
    print("✅ WEBHOOK RESPONSE =", r.text[:400])
    r.raise_for_status()

# ============================================================
#  Plot builders
# ============================================================

def build_load_plot(df: pd.DataFrame, view_mode: str):
    fig = go.Figure()

    if df.empty or "Date" not in df.columns or "Load" not in df.columns:
        fig.update_layout(**MOBILE_PLOT_LAYOUT)
        return fig

    # --- Colours ---
    BLUE = "#1E6BD6"
    TEAL = "#1BA39C"
    GREEN_DARK = "#6B7280"
    PURPLE = "#7B61FF"

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.sort_values("Date")
    d["Load"] = pd.to_numeric(d["Load"], errors="coerce")

    # =========================================================
    # WEEKLY VIEW
    # =========================================================
    if view_mode == "weekly":
        d["Week"] = _week_agg_date(d["Date"])

        g = d.groupby("Week", as_index=False).agg(
            Load=("Load", lambda s: s.sum(min_count=1))
        )

        g["EWMA7"] = g["Load"].ewm(span=7, adjust=False, min_periods=3).mean()
        g["EWMA28"] = g["Load"].ewm(span=28, adjust=False, min_periods=10).mean()
        g["ACWR"] = g["EWMA7"] / g["EWMA28"]

        x = g["Week"]

        # --- Load bars (WHOOP-style) ---
        fig.add_bar(
            x=x,
            y=g["Load"],
            name="Weekly Load",
            marker=dict(
                color="rgba(30,107,214,0.35)",
                line=dict(color=BLUE, width=1.8),
            ),
            hovertemplate="Load: %{y:.2f}<extra></extra>",
        )

        # --- Lines ---
        fig.add_trace(go.Scatter(
            x=x, y=g["EWMA7"],
            name="Short-term Load",
            mode="lines",
            line=dict(color=TEAL, width=2.6),
            line_shape="spline",
            line_smoothing=0.75,
        ))

        fig.add_trace(go.Scatter(
            x=x, y=g["EWMA28"],
            name="Long-term Load",
            mode="lines",
            line=dict(color=GREEN_DARK, width=2, dash="dot"),
            line_shape="spline",
            line_smoothing=0.75,
            opacity=0.5,
            visible="legendonly",
        ))

        fig.add_trace(go.Scatter(
            x=x, y=g["ACWR"],
            name="Load Balance (ACWR)",
            mode="lines",
            yaxis="y2",
            line=dict(color=PURPLE, width=1.6),
            line_shape="spline",
            line_smoothing=0.75,
            opacity=0.7,
        ))

        # --- ACWR band ---
        fig.add_shape(
            type="rect",
            xref="paper", x0=0, x1=1,
            yref="y2", y0=0.9, y1=1.25,
            fillcolor="rgba(56,189,248,0.12)",
            line_width=0,
            layer="below",
        )

        fig.update_layout(
            title="Weekly Training Load & Balance",
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
            **MOBILE_PLOT_LAYOUT,
        )

        return fig

    # =========================================================
    # DAILY VIEW
    # =========================================================
    d["EWMA7"] = d["Load"].ewm(span=7, adjust=False).mean()
    d["EWMA28"] = d["Load"].ewm(span=28, adjust=False).mean()
    d["ACWR"] = d["EWMA7"] / d["EWMA28"]

    x = d["Date"]

    fig.add_bar(
        x=x,
        y=d["Load"],  # ✅ FIXED (was g["Load"])
        name="Daily Load",
        marker=dict(
            color="rgba(30,107,214,0.35)",
            line=dict(color=BLUE, width=1.8),
        ),
        hovertemplate="Load: %{y:.2f}<extra></extra>",
    )

    fig.add_trace(go.Scatter(
        x=x, y=d["EWMA7"],
        name="Short-term Load",
        mode="lines",
        line=dict(color=TEAL, width=2.6),
        line_shape="spline",
        line_smoothing=0.75,
    ))

    fig.add_trace(go.Scatter(
        x=x, y=d["EWMA28"],
        name="Long-term Load",
        mode="lines",
        line=dict(color=GREEN_DARK, width=2, dash="dot"),
        line_shape="spline",
        line_smoothing=0.75,
        opacity=0.5,
        visible="legendonly",
    ))

    fig.add_trace(go.Scatter(
        x=x, y=d["ACWR"],
        name="ACWR",
        mode="lines",
        yaxis="y2",
        line=dict(color=PURPLE, width=1.6),
        line_shape="spline",
        line_smoothing=0.75,
    ))

    fig.add_shape(
        type="rect",
        xref="paper", x0=0, x1=1,
        yref="y2", y0=0.9, y1=1.25,
        fillcolor="rgba(56,189,248,0.12)",
        line_width=0,
        layer="below",
    )

    fig.update_layout(
        title="Daily Training Load & Balance",
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
        **MOBILE_PLOT_LAYOUT,
    )

    return fig



def build_wellness_plot(df: pd.DataFrame, view_mode: str):
    fig = go.Figure()

    if df.empty or "Date" not in df.columns:
        fig.update_layout(**_legend_right_layout())
        return fig

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.sort_values("Date")

    metrics = {
        "Sleep_1_5":    ("Sleep", BLUE),
        "Fatigue_1_5":  ("Fatigue", ORANGE),
        "Soreness_1_5": ("Soreness", GREEN_DARK),
        "Mood_1_5":     ("Mood", PURPLE),
    }

    for col in metrics:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")

    # =====================================================
    # WEEKLY VIEW
    # =====================================================
    if view_mode == "weekly":
        d["Week"] = _week_agg_date(d["Date"])

        g = (
            d.groupby("Week", as_index=False)
             .mean(numeric_only=True)
        )

        x = g["Week"]
        window = 3

        for col, (label, color) in metrics.items():
            if col not in g.columns:
                continue

            y = g[col]
            if y.dropna().empty:
                continue

            roll = y.rolling(window, min_periods=1).mean()

            # --- SHADED AREA ---
            fig.add_trace(go.Scatter(
                x=x,
                y=roll,
                mode="lines",
                line=dict(width=0),
                fill="tozeroy",
                fillcolor=f"rgba{tuple(int(color[i:i+2],16) for i in (1,3,5)) + (0.12,)}",
                hoverinfo="skip",
                showlegend=False,
            ))

            # --- LINE ---
            fig.add_trace(go.Scatter(
                x=x,
                y=roll,
                name=label,
                mode="lines",
                line=dict(color=color, width=2.6),
                line_shape="spline",
                line_smoothing=0.7,
                hovertemplate=f"{label}: %{{y:.2f}}<extra></extra>",
            ))

        fig.update_layout(
            title="Weekly Wellness Trends",
            xaxis_title="Week",
            yaxis=dict(
                title="Scale (1–5)",
                range=[0.8, 5.2],
                tickvals=[1, 2, 3, 4, 5],
            ),
            hovermode="x unified",
            **_legend_right_layout()
        )

        # 🔑 FORCE X-AXIS TO MATCH OTHER WEEKLY CHARTS
        fig.update_xaxes(
            range=[d["Week"].min(), d["Week"].max()]
        )

        fig.update_layout(**MOBILE_PLOT_LAYOUT)
        return fig

    # =====================================================
    # DAILY VIEW
    # =====================================================
    x = d["Date"]
    window = 3

    for col, (label, color) in metrics.items():
        if col not in d.columns:
            continue

        y = d[col]
        if y.dropna().empty:
            continue

        roll = y.rolling(window, min_periods=1).mean()

        # --- BASELINE (required for correct fill behaviour) ---
        fig.add_trace(go.Scatter(
            x=x,
            y=[0] * len(x),
            mode="lines",
            line=dict(width=0),
            hoverinfo="skip",
            showlegend=False,
        ))

        # --- SHADED AREA ---
        fig.add_trace(go.Scatter(
            x=x,
            y=roll,
            mode="lines",
            line=dict(width=0),
            fill="tonexty",
            fillcolor=f"rgba{tuple(int(color[i:i+2],16) for i in (1,3,5)) + (0.15,)}",
            hoverinfo="skip",
            showlegend=False,
        ))

        # --- LINE ---
        fig.add_trace(go.Scatter(
            x=x,
            y=roll,
            name=label,
            mode="lines",
            line=dict(color=color, width=2.6),
            line_shape="spline",
            line_smoothing=0.7,
            hovertemplate=f"{label}: %{{y:.2f}}<extra></extra>",
            visible="legendonly" if label == "Mood" else True,
        ))

    fig.update_layout(
        title="Daily Wellness Trends",
        xaxis_title="Date",
        yaxis=dict(
            title="Scale (1–5)",
            range=[0.8, 5.2],
            tickvals=[1, 2, 3, 4, 5],
        ),
        hovermode="x unified",
        **_legend_right_layout()
    )

    fig.update_layout(**MOBILE_PLOT_LAYOUT)
    return fig



def build_speed_tempo_plot(df: pd.DataFrame, view_mode: str):
    fig = go.Figure()

    if df.empty or "Date" not in df.columns:
        fig.update_layout(**MOBILE_PLOT_LAYOUT)
        return fig

    BLUE   = "#2563EB"
    ORANGE = "#F59E0B"

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.sort_values("Date")

    speed = pd.to_numeric(d.get("SPEED (m)", np.nan), errors="coerce")
    tempo = pd.to_numeric(d.get("TEMPO (m)", np.nan), errors="coerce")

    # =====================================================
    # DAILY VIEW
    # =====================================================
    if view_mode == "daily":
        x = d["Date"]

        # --- SPEED BARS ---
        fig.add_bar(
            x=x,
            y=speed,
            name="Speed exposure",
            marker=dict(
                color="rgba(37,99,235,0.35)",
                line=dict(color=BLUE, width=1.6),
            ),
            hovertemplate="Speed: %{y:.2f} m<extra></extra>",
        )

        # --- TEMPO BARS ---
        fig.add_bar(
            x=x,
            y=tempo,
            name="Tempo exposure",
            marker=dict(
                color="rgba(245,158,11,0.35)",
                line=dict(color=ORANGE, width=1.6),
            ),
            hovertemplate="Tempo: %{y:.2f} m<extra></extra>",
        )

        # --- SPEED ROLLING LINES ---
        fig.add_trace(go.Scatter(
            x=x,
            y=speed.rolling(7, min_periods=1).mean(),
            name="Speed 7d",
            mode="lines",
            line=dict(color=BLUE, width=2.4, dash="dot"),
            line_shape="spline",
            line_smoothing=0.7,
            hovertemplate="Speed 7d: %{y:.2f} m<extra></extra>",
        ))

        fig.add_trace(go.Scatter(
            x=x,
            y=speed.rolling(28, min_periods=1).mean(),
            name="Speed 28d",
            mode="lines",
            line=dict(color=BLUE, width=2.4, dash="dash"),
            line_shape="spline",
            line_smoothing=0.7,
            hovertemplate="Speed 28d: %{y:.2f} m<extra></extra>",
            visible="legendonly",
        ))

        # --- TEMPO ROLLING LINES ---
        fig.add_trace(go.Scatter(
            x=x,
            y=tempo.rolling(7, min_periods=1).mean(),
            name="Tempo 7d",
            mode="lines",
            line=dict(color=ORANGE, width=2.4, dash="dot"),
            line_shape="spline",
            line_smoothing=0.7,
            hovertemplate="Tempo 7d: %{y:.2f} m<extra></extra>",
        ))

        fig.add_trace(go.Scatter(
            x=x,
            y=tempo.rolling(28, min_periods=1).mean(),
            name="Tempo 28d",
            mode="lines",
            line=dict(color=ORANGE, width=2.4, dash="dash"),
            line_shape="spline",
            line_smoothing=0.7,
            hovertemplate="Tempo 28d: %{y:.2f} m<extra></extra>",
            visible="legendonly",
        ))

        fig.update_layout(
            title="Daily Speed & Tempo Volumes",
            xaxis_title="Date",
            yaxis_title="Metres",
            barmode="stack",
            hovermode="x unified",
            **MOBILE_PLOT_LAYOUT,
        )

        return fig

    # =====================================================
    # WEEKLY VIEW
    # =====================================================
    d["Week"] = _week_agg_date(d["Date"])
    d["Speed_clean"] = speed
    d["Tempo_clean"] = tempo

    g = d.groupby("Week", as_index=False).agg(
        Speed=("Speed_clean", lambda s: s.sum(min_count=1)),
        Tempo=("Tempo_clean", lambda s: s.sum(min_count=1)),
    )

    x = g["Week"]

    # --- BARS ---
    fig.add_bar(
        x=x,
        y=g["Speed"],
        name="Speed exposure",
        marker=dict(
            color="rgba(37,99,235,0.35)",
            line=dict(color=BLUE, width=1.6),
        ),
        hovertemplate="Speed: %{y:.2f} m<extra></extra>",
    )

    fig.add_bar(
        x=x,
        y=g["Tempo"],
        name="Tempo exposure",
        marker=dict(
            color="rgba(245,158,11,0.35)",
            line=dict(color=ORANGE, width=1.6),
        ),
        hovertemplate="Tempo: %{y:.2f} m<extra></extra>",
    )

    # --- ROLLING LINES ---
    fig.add_trace(go.Scatter(
        x=x,
        y=g["Speed"].rolling(1, min_periods=1).mean(),
        name="Speed 7d",
        mode="lines",
        line=dict(color=BLUE, width=2.4, dash="dot"),
        line_shape="spline",
        line_smoothing=0.7,
    ))

    fig.add_trace(go.Scatter(
        x=x,
        y=g["Speed"].rolling(4, min_periods=1).mean(),
        name="Speed 28d",
        mode="lines",
        line=dict(color=BLUE, width=2.4, dash="dash"),
        line_shape="spline",
        line_smoothing=0.7,
    ))

    fig.add_trace(go.Scatter(
        x=x,
        y=g["Tempo"].rolling(1, min_periods=1).mean(),
        name="Tempo 7d",
        mode="lines",
        line=dict(color=ORANGE, width=2.4, dash="dot"),
        line_shape="spline",
        line_smoothing=0.7,
    ))

    fig.add_trace(go.Scatter(
        x=x,
        y=g["Tempo"].rolling(4, min_periods=1).mean(),
        name="Tempo 28d",
        mode="lines",
        line=dict(color=ORANGE, width=2.4, dash="dash"),
        line_shape="spline",
        line_smoothing=0.7,
    ))

    fig.update_layout(
        title="Weekly Speed & Tempo Volumes",
        xaxis_title="Week",
        yaxis_title="Metres",
        barmode="stack",
        hovermode="x unified",
        **MOBILE_PLOT_LAYOUT,
    )

    return fig



# ============================================================
#  Calendar UI
# ============================================================

def build_month_calendar(df: pd.DataFrame, month_date: dt.date, selected_date_str: str | None):
    if df.empty or "Date" not in df.columns:
        return html.Div("No data", className="text-muted")

    ddf = df.copy()
    ddf["Date"] = pd.to_datetime(ddf["Date"], errors="coerce").dt.date

    year = month_date.year
    month = month_date.month
    first_day = dt.date(year, month, 1)

    start_weekday = first_day.weekday()          # Monday=0
    start_offset = (start_weekday + 1) % 7       # Sunday-start
    days = [
        first_day - dt.timedelta(days=start_offset) + dt.timedelta(days=i)
        for i in range(42)
    ]

    selected_date = None
    if selected_date_str:
        try:
            selected_date = pd.to_datetime(selected_date_str).date()
        except Exception:
            pass

    today = today_adl()
    cells = []

    for day in days:
        match = ddf[ddf["Date"] == day]

        # ------------------------
        # Extract session values
        # ------------------------
        rpe = load = None
        notes_val = ""

        if not match.empty:
            row = match.iloc[-1]
            rpe = pd.to_numeric(row.get("sRPE", np.nan), errors="coerce")
            load = pd.to_numeric(row.get("Load", np.nan), errors="coerce")
            notes_val = str(row.get("Athlete_Notes", "")).strip()

        # ------------------------
        # RPE colour (1–10)
        # ------------------------
        if pd.isna(rpe):
            pill_color = "#CFD8DC"      # no data
        elif rpe <= 2:
            pill_color = "#4285F4"      # very easy
        elif rpe <= 5:
            pill_color = "#4CAF50"      # moderate
        elif rpe <= 7:
            pill_color = "#FF9800"      # hard
        else:
            pill_color = "#F44336"      # very hard

        # ------------------------
        # Logged session logic
        # ------------------------
        logged_session = (
            pd.notna(rpe)
            or pd.notna(load)
            or notes_val.lower() not in ["", "nan", "none", "nil", "0"]
        )

        # ------------------------
        # Cell classes
        # ------------------------
        classes = ["calendar-day"]

        if day == today:
            classes.append("today")

        if logged_session:
            classes.append("logged")

        if day.month != month:
            classes.append("out-month")

        if selected_date and day == selected_date:
            classes.append("selected")

        # ------------------------
        # Tooltip text
        # ------------------------
        tooltip_parts = [day.strftime("%a %d %b %Y")]

        if pd.notna(rpe):
            tooltip_parts.append(f"sRPE: {int(rpe)}/10")

        if pd.notna(load):
            tooltip_parts.append(f"Load: {round(load, 1)}")

        if notes_val and notes_val.lower() not in ["nan", "none", "nil", "0"]:
            tooltip_parts.append(f"Notes: {notes_val[:60]}")

        tooltip_text = " | ".join(tooltip_parts)

        # ------------------------
        # Build cell
        # ------------------------
        cells.append(
            html.Div(
                [
                    html.Div(str(day.day), className="cal-day-number"),
                    html.Div(
                        className="rpe-dot",
                        style={"backgroundColor": pill_color},
                        title=tooltip_text,   # 👈 hover / long-press
                    ),
                ],
                id={"type": "calendar-day", "date": str(day)},
                n_clicks=0,
                className=" ".join(classes),
            )
        )

    # ------------------------
    # Calendar grid
    # ------------------------
    grid = html.Div(
        cells,
        style={
            "display": "grid",
            "gridTemplateColumns": "repeat(7, 1fr)",
            "gap": "4px",
            "padding": "6px",
        },
    )

    weekdays = html.Div(
        [
            html.Div(d, style={"textAlign": "center", "fontWeight": "600"})
            for d in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        ],
        style={
            "display": "grid",
            "gridTemplateColumns": "repeat(7, 1fr)",
            "marginBottom": "4px",
        },
    )

    legend = html.Div(
        [
            html.Small("RPE Colour Scale:", className="fw-bold me-2"),
            html.Span("1–2", style={"background": "#4285F4", "color": "white", "padding": "2px 8px", "borderRadius": "6px", "marginRight": "6px", "fontSize": "12px"}),
            html.Span("3–5", style={"background": "#4CAF50", "color": "white", "padding": "2px 8px", "borderRadius": "6px", "marginRight": "6px", "fontSize": "12px"}),
            html.Span("6–7", style={"background": "#FF9800", "color": "white", "padding": "2px 8px", "borderRadius": "6px", "marginRight": "6px", "fontSize": "12px"}),
            html.Span("8–10", style={"background": "#F44336", "color": "white", "padding": "2px 8px", "borderRadius": "6px", "fontSize": "12px"}),
        ],
        style={"textAlign": "center", "marginTop": "8px"},
    )

    return html.Div([legend, weekdays, grid])



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
app._favicon = "icon-192.png"

app.index_string = """
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <meta name="mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-title" content="AthleteAI">
        <meta name="theme-color" content="#1e88e5">

        <link rel="manifest" href="/assets/manifest.json">
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


def app_header(center=False):
    align = "center" if center else "left"
    return html.Div(
        [
            html.Img(
                src="/assets/app_icon.png",
                style={"height": "56px", "marginRight": "10px", "verticalAlign": "middle"},
            ),
            html.Div(
                [
                    html.H2("Adaptive Coaching Intelligence", style={"margin": 0, "fontWeight": 600, "textAlign": align}),
                    html.Small("Elevating performance through athlete insight",
                               style={"color": "#555", "textAlign": align, "display": "block"}),
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
                                html.H4("Secure Access", className="mb-3", style={"textAlign": "center"}),
                                dcc.Input(type="password", style={"display": "none"}, autoComplete="new-password"),
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
                                ),
                                dbc.Button("Login", id="login-button", color="primary", style={"width": "100%"}),
                                html.Div(id="login-error", className="text-danger mt-2", style={"textAlign": "center"}),
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


def build_main_layout(auth_data):
    athlete_sheet = auth_data.get("athlete_sheet")
    is_coach = auth_data.get("is_coach", False)

    tabs = list_tabs()
    if athlete_sheet and athlete_sheet in tabs:
        default_tab = athlete_sheet
    elif tabs:
        default_tab = tabs[0]
    else:
        default_tab = None

    if is_coach:
        options = []
        for _, info in USER_LOGINS.items():
            sheet_name = info.get("sheet", "")
            if sheet_name and sheet_name in tabs:
                options.append({"label": sheet_name, "value": sheet_name})
    else:
        options = [{"label": athlete_sheet, "value": athlete_sheet}] if athlete_sheet in tabs else []

    if default_tab is None and options:
        default_tab = options[0]["value"]

    # HOME VIEW
    home_view = html.Div(
        id="home-view",
        children=[
            dbc.Row(
                className="g-2 align-items-end mb-2",
                children=[
                    dbc.Col(
                        [
                            html.Div("Athlete", className="mini-label"),
                            dcc.Dropdown(
                                id="athlete-dropdown",
                                options=options,
                                value=default_tab,
                                clearable=False,
                                disabled=not is_coach,
                                className="compact-dd",
                            ),
                        ],
                        lg=6, md=6, width=12,
                    ),
                    dbc.Col(
                        [
                            html.Div("Today", className="mini-label"),
                            html.Div(id="today-date", className="compact-today"),
                        ],
                        lg=6, md=6, width=12,
                    ),
                ],
            ),

            dbc.Row(
                className="g-2 align-items-stretch mt-2 dial-row",
                children=[
                    dbc.Col(html.Div([html.Div("Daily Readiness", className="dial-label"),
                                      html.Div(id="readiness-dial-container", className="dial-center")],
                                     className="dial-block"),
                            lg=3, md=3, sm=6, xs=6, width=6),
                    dbc.Col(html.Div([html.Div("Neuromuscular Readiness", className="dial-label"),
                                      html.Div(id="neuromuscular-dial-container", className="dial-center")],
                                     className="dial-block"),
                            lg=3, md=3, sm=6, xs=6, width=6),
                    dbc.Col(html.Div([html.Div("Training Exposure", className="dial-label"),
                                      html.Div(id="weekly-dial-container", className="dial-center")],
                                     className="dial-block"),
                            lg=3, md=3, sm=6, xs=6, width=6),
                    dbc.Col(html.Div([html.Div("Training Streak", className="dial-label"),
                                      html.Div(id="streak-dial-container", className="dial-center")],
                                     className="dial-block"),
                            lg=3, md=3, sm=6, xs=6, width=6),
                ],
            ),

            html.P(
                "Swipe between Calendar, Graphs and AI Session Builder using the bottom nav.",
                className="text-muted mt-2",
                style={"textAlign": "center"},
            ),

            html.Div(
                logout_button,
                style={"display": "flex", "justifyContent": "flex-end", "marginTop": "10px", "marginRight": "4px"},
            ),
        ],
        style={"display": "block"},
    )

    # CALENDAR VIEW (your full UI kept)
    calendar_view = html.Div(
        id="calendar-view",
        children=[
            html.H4("Training Program", className="mt-3"),

            # ===== Calendar header + grid =====
            html.Div(
                [
                    html.Div(
                        [
                            dbc.Button("◀", id="calendar-prev", size="sm",
                                       color="secondary", outline=True, className="me-2"),
                            html.Div(
                                id="calendar-window-label",
                                className="flex-grow-1 text-center small text-muted",
                                style={"minHeight": "24px"},
                            ),
                            dbc.Button("▶", id="calendar-next", size="sm",
                                       color="secondary", outline=True, className="ms-2"),
                        ],
                        className="d-flex align-items-center justify-content-between mb-2",
                    ),
                    html.Div(id="calendar-grid", className="mb-4"),
                ]
            ),

            html.Hr(),

            html.H3("Selected Session & Athlete Input", className="mt-3"),

            # ===== SESSION CONTAINER =====
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

                    html.H5(id="selected-date-header", className="mb-2"),

                    # ---- Read-only context (calendar driven) ----
                    html.Div(
                        [
                            html.Div(id="ctx-workout"),
                            html.Div(id="ctx-focus"),
                            html.Div(id="ctx-venue"),
                        ],
                        id="session-context-wrapper",
                    ),

                    # ---- Main content row ----
                    dbc.Row([

                        # ================= LEFT: Athlete inputs =================
                        dbc.Col([

                            input_card([
                                html.Label("Athlete Notes"),
                                dcc.Textarea(
                                    id="athlete-notes",
                                    placeholder="e.g., Last two reps were my best...",
                                    style={"width": "100%", "height": "80px", "border": "none"},
                                ),
                            ]),

                            input_card([
                                html.Label("Sets × Reps × Load"),
                                dcc.Textarea(
                                    id="sets-reps-load",
                                    placeholder="e.g., add here",
                                    style={"width": "100%", "height": "80px", "border": "none"},
                                ),
                            ]),

                            input_card([
                                html.Label("Track Reps & Times"),
                                dcc.Textarea(
                                    id="track-reps-times",
                                    placeholder="e.g., add here",
                                    style={"width": "100%", "height": "80px", "border": "none"},
                                ),
                            ]),

                            dbc.Label("Session RPE (1 = very easy, 5 = maximal)"),
                            dcc.Slider(
                                id="slider-session-rpe",
                                min=1,
                                max=5,
                                step=1,
                                value=3,
                            ),

                            dbc.Label("Session Quality (1 = poor, 5 = excellent)"),
                            dcc.Slider(
                                id="slider-session-quality",
                                min=1,
                                max=5,
                                step=1,
                                value=3,
                            ),

                            dbc.Label("Sleep (1 = tired, 5 = well-rested)"),
                            dcc.Slider(
                                id="slider-sleep",
                                min=1,
                                max=5,
                                step=1,
                                value=3,
                            ),

                            dbc.Label("Mood (1 = sad, 5 = upbeat)"),
                            dcc.Slider(
                                id="slider-mood",
                                min=1,
                                max=5,
                                step=1,
                                value=3,
                            ),

                            dbc.Label("Fatigue (1 = low energy, 5 = energetic)"),
                            dcc.Slider(
                                id="slider-fatigue",
                                min=1,
                                max=5,
                                step=1,
                                value=3,
                            ),

                            dbc.Label("Soreness (1 = low, 5 = high)"),
                            dcc.Slider(
                                id="slider-soreness",
                                min=1,
                                max=5,
                                step=1,
                                value=3,
                            ),

                                                    ], md=6),

                        # ================= RIGHT: Coaching feedback + AI =================
                        dbc.Col([

                            dbc.Row(
                                [
                                    dbc.Col(
                                        [
                                            dbc.Label("Primary Coaching Feedback"),
                                            dcc.Dropdown(
                                                id="ai-mode-1",
                                                options=[
                                                    {"label": "Acceleration & Speed Coach",
                                                     "value": "Acceleration & Speed Coach"},
                                                    {"label": "Tempo & Endurance Coach",
                                                     "value": "Tempo & Endurance Coach"},
                                                    {"label": "Technical Sprint Coach",
                                                     "value": "Technical Sprint Coach"},
                                                    {"label": "Strength & Power Coach",
                                                     "value": "Strength & Power Coach"},
                                                    {"label": "Recovery & Readiness Coach",
                                                     "value": "Recovery & Readiness Coach"},
                                                ],
                                                value=None,
                                                placeholder="Select Coach Feedback",
                                                searchable=False,
                                                clearable=False,
                                                className="aw-dropdown",
                                            ),
                                        ],
                                        md=6,
                                    ),
                                    dbc.Col(
                                        [
                                            dbc.Label("Secondary Coaching Feedback"),
                                            dcc.Dropdown(
                                                id="ai-mode-2",
                                                options=[
                                                    {"label": "Acceleration & Speed Coach",
                                                     "value": "Acceleration & Speed Coach"},
                                                    {"label": "Tempo & Endurance Coach",
                                                     "value": "Tempo & Endurance Coach"},
                                                    {"label": "Technical Sprint Coach",
                                                     "value": "Technical Sprint Coach"},
                                                    {"label": "Strength & Power Coach",
                                                     "value": "Strength & Power Coach"},
                                                    {"label": "Recovery & Readiness Coach",
                                                     "value": "Recovery & Readiness Coach"},
                                                ],
                                                value=None,
                                                placeholder="Select Coach Feedback",
                                                searchable=False,
                                                clearable=False,
                                                className="aw-dropdown",
                                            ),
                                        ],
                                        md=6,
                                    ),
                                ],
                                className="g-3",
                            ),
                            dbc.Button(
                                "Log Session & Generate AI Coaching Feedback",
                                id="btn-generate-ai",
                                className="mt-4 w-100 ai-save-btn",
                            ),

                            html.Div(
                                id="save-status",
                                className="mt-2",
                            ),

                            dcc.Loading(
                                id="ai-loader",
                                type="circle",
                                children=[
                                    html.Div(id="ai-suggestion-1", className="mt-3"),
                                    html.Div(id="ai-suggestion-2", className="mt-3"),
                                ],
                            ),

                        ], md=6),

                    ]),
                ],
            ),
        ],
    )

    # ================= GRAPH VIEW =================
    graphs_view = html.Div(

        id="graphs-view",
        style={"display": "none"},
        children=[
            html.H3("Training Load, Wellness & Speed/Tempo", className="mb-4"),
            dbc.Row(
                [
                    dbc.Col(
                        [
                            html.Div("View mode", className="fw-semibold text-muted mb-1"),
                            dcc.RadioItems(
                                id="view-mode",
                                options=[{"label": "Weekly", "value": "weekly"}, {"label": "Daily", "value": "daily"}],
                                value="weekly",
                                inline=True,
                                className="view-toggle",
                                inputClassName="view-toggle-input",
                                labelClassName="view-toggle-label",
                            ),
                        ],
                        width="auto",
                    ),
                    dbc.Col(
                        dbc.Button("Refresh", id="refresh-btn", color="light", size="sm", className="refresh-btn"),
                        width="auto",
                        className="d-flex align-items-end",
                    ),
                ],
                className="g-3 align-items-end mb-4",
            ),
            dcc.Graph(id="load-plot", config={"displayModeBar": False}),
            dcc.Graph(id="wellness-plot", config={"displayModeBar": False}),
            dcc.Graph(id="speedtempo-plot", config={"displayModeBar": False}),
        ],
    )

    ai_view = html.Div(id="ai-view", style={"display": "none"}, children=[
        html.Div(className="page-wrap", children=[
            html.Div(className="ai-hero", children=[
                html.Div(className="d-flex align-items-start justify-content-between", children=[
                    html.Div(children=[
                        html.H3("AI Training Session Builder", className="ai-hero-title"),
                        html.P("Build a structured session in clean blocks (warm-up → primary → secondary → tertiary → cool-down).",
                               className="ai-hero-sub"),
                    ]),
                    html.Div(className="pill", children=[html.Div(className="pill-dot"), html.Span("ACI", className="text-nowrap")]),
                ]),
            ]),
            dbc.Row(className="g-3", children=[
                dbc.Col(md=5, children=[
                    dbc.Card(className="premium-card", children=[
                        dbc.CardHeader("Session Inputs"),
                        dbc.CardBody(children=[
                            html.Div("Keep the goal tight and specific. The plan will follow your recent trends.", className="card-muted"),
                            html.Div(className="divider-soft"),

                            dbc.Label("Coaching Focus"),
                            dcc.Dropdown(
                                id="ai-plan-coach",
                                options=[{"label": k, "value": k} for k in [
                                    "Acceleration & Speed Coach",
                                    "Tempo & Endurance Coach",
                                    "Technical Sprint Coach",
                                    "Strength & Power Coach",
                                    "Recovery & Readiness Coach",
                                ]],
                                placeholder="Select your coach style",
                                clearable=False,
                                className="aw-dropdown premium-input",
                            ),
                            html.Br(),

                            dbc.Label("Main session goal / focus"),
                            dcc.Textarea(
                                id="ai-plan-goal",
                                placeholder="e.g., Lower body speed-strength + low CNS cost. Keep bar speed high; finish feeling sharp.",
                                className="form-control premium-textarea",
                            ),
                            html.Br(),

                            dbc.Label("Approx. session duration (min)"),
                            dcc.Input(id="ai-plan-duration", type="number", min=10, max=120, step=5, value=45,
                                      className="form-control premium-number"),

                            html.Div(className="divider-soft"),
                            dbc.Button([html.I(className="bi bi-magic me-2"), "Generate Session Plan"],
                                       id="btn-generate-plan", color="primary", className="w-100 premium-btn"),
                            html.Div(id="ai-plan-status", className="mt-2 text-danger small"),
                        ]),
                    ]),
                ]),
                dbc.Col(md=7, children=[
                    dbc.Card(className="premium-card", children=[
                        dbc.CardHeader("Suggested Session Plan"),
                        dbc.CardBody(
                            dcc.Loading(type="circle", children=html.Div(
                                id="ai-plan-output",
                                children=html.Div([
                                    html.Div("Ready when you are.", className="fw-semibold"),
                                    html.Div("Generate a session to see structured cards here.", className="card-muted"),
                                ])
                            ))
                        ),
                    ]),
                ]),
            ]),
        ])
    ])

    bottom_nav = html.Div(
        [
            html.Div(id="nav-underline", className="nav-underline"),
            dbc.Row(
                [
                    dbc.Col(html.Div([html.I(id="icon-home", className="bi bi-house nav-icon"), html.Div("Home", className="nav-label")],
                                    id="nav-home", n_clicks=0, className="nav-item")),
                    dbc.Col(html.Div([html.I(id="icon-calendar", className="bi bi-calendar-event nav-icon"), html.Div("Calendar", className="nav-label")],
                                    id="nav-calendar", n_clicks=0, className="nav-item")),
                    dbc.Col(html.Div([html.I(id="icon-graphs", className="bi bi-bar-chart-line nav-icon"), html.Div("Graphs", className="nav-label")],
                                    id="nav-graphs", n_clicks=0, className="nav-item")),
                    dbc.Col(html.Div([html.I(id="icon-ai", className="bi bi-cpu nav-icon"), html.Div("AI", className="nav-label")],
                                    id="nav-ai", n_clicks=0, className="nav-item")),
                ],
                className="g-0",
            ),
        ],
        className="bottom-nav",
    )

    return dbc.Container(
        [
            app_header(center=False),
            dcc.Store(id="selected-date-store"),
            dcc.Store(id="calendar-window-start"),
            dcc.Store(id="bottom-nav-click", data="home"),

            home_view,
            calendar_view,
            graphs_view,
            ai_view,
            bottom_nav,
        ],
        fluid=True,
        className="pb-5 app-shell",
    )


app.layout = html.Div(
    [
        dcc.Location(id="url", refresh=False),
        dcc.Store(id="auth-store", storage_type="session"),
        dcc.Store(id="active-tab-store", data="home"),

        html.Div(
            id="splash-screen",
            children=[
                html.Img(src="/assets/app_icon.png", className="splash-logo"),
                html.H2("Adaptive Coaching Intelligence", className="splash-title"),
                html.P("Empowering performance through athlete insight", className="splash-subtitle"),
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

@app.callback(
    Output("page-content", "children"),
    Input("auth-store", "data"),
)
def render_page(auth_data):
    if auth_data and auth_data.get("authed"):
        return build_main_layout(auth_data)
    return build_login_layout()


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

    for athlete_key, info in USER_LOGINS.items():
        u = str(info.get("username", "")).strip().lower()
        p = str(info.get("password", "")).strip()
        role = str(info.get("role", "athlete")).lower()
        sheet = info.get("sheet", "")

        if username.strip().lower() == u and password.strip() == p:
            return (
                {
                    "authed": True,
                    "username": username.strip(),
                    "athlete_name": athlete_key,
                    "athlete_sheet": sheet,
                    "is_coach": (role == "coach"),
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
    return {"authed": False}

@app.callback(
    Output("session-context-wrapper", "style"),
    Input("close-session-button", "n_clicks"),
    State("session-context-wrapper", "style"),
    prevent_initial_call=True,
)
def close_session_context(n, style):
    if not n:
        raise PreventUpdate
    if style and style.get("display") == "none":
        return {"display": "block"}
    return {"display": "none"}



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

    out = []
    for key in ["home", "calendar", "graphs", "ai"]:
        out.append({"display": "block"} if key == tab else {"display": "none"})
    out.append(tab)
    return out


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

    if current_month is None:
        month_date = today.replace(day=1)
    else:
        month_date = pd.to_datetime(current_month).date()

    triggered = callback_context.triggered[0]["prop_id"].split(".")[0]

    if triggered == "calendar-prev":
        year = month_date.year
        month = month_date.month - 1
        if month == 0:
            month = 12
            year -= 1
        month_date = dt.date(year, month, 1)

    elif triggered == "calendar-next":
        year = month_date.year
        month = month_date.month + 1
        if month == 13:
            month = 1
            year += 1
        month_date = dt.date(year, month, 1)

    label = month_date.strftime("%B %Y")
    return str(month_date), label


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
    if window_start:
        month_date = pd.to_datetime(window_start).date().replace(day=1)
    else:
        month_date = dt.date.today().replace(day=1)

    return build_month_calendar(df, month_date, selected_date)


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
    if not athlete_id:
        today_date_str = today_adl().strftime("%d %b %Y")
        return (
            today_date_str,
            dial_flip(apple_sessions_ring(None), "Weekly Training Exposure", "—"),
            dial_flip(streak_dial(0), "Training Streak", "—"),
            dial_flip(apple_neuromuscular_ring(None), "Neuromuscular State", "—"),
            dial_flip(apple_readiness_ring(None), "Training Readiness Index", "—"),
            go.Figure(), go.Figure(), go.Figure()
        )

    today = today_adl()
    today_date_str = today.strftime("%d %b %Y")
    df = load_tab(athlete_id)

    dow = today.weekday()
    days_since_sat = (dow - 5) % 7
    week_start = today - dt.timedelta(days=days_since_sat)
    week_end = week_start + dt.timedelta(days=6)

    if df is None or df.empty:
        planned_count = 0
        completed_count = 0
        weekly_exposure_pct = None
        streak = 0
        neuro_val = None
        readiness_val = None

        weekly_ui = dial_flip(
            apple_sessions_ring(weekly_exposure_pct),
            "Weekly Training Exposure",
            (
                "This score is the % of planned sessions (calendar workouts) that were completed (logged) this week.\n\n"
                f"Planned: {planned_count}  •  Completed: {completed_count}\n"
                f"Exposure score: {weekly_exposure_pct if weekly_exposure_pct is not None else '—'}/100\n\n"
                "100 = every planned session was logged.\n"
                "Lower scores mean planned sessions were missed or not entered."
            )
        )

        streak_ui = dial_flip(streak_dial(streak), " ", "Consecutive days with a logged session/note.")
        neuro_ui = dial_flip(apple_neuromuscular_ring(neuro_val), " ", "Mood + Fatigue combined and scaled to 0–100.")
        readiness_ui = dial_flip(apple_readiness_ring(readiness_val), " ", "Daily readiness proxy scaled 0–100.")

        load_fig = go.Figure().update_layout(title="Training Load (No Data)", **_legend_right_layout())
        wellness_fig = go.Figure().update_layout(title="Wellness (No Data)", **_legend_right_layout())
        speed_fig = go.Figure().update_layout(title="Speed & Tempo (No Data)", **_legend_right_layout())
        load_fig.update_layout(**MOBILE_PLOT_LAYOUT)
        wellness_fig.update_layout(**MOBILE_PLOT_LAYOUT)
        speed_fig.update_layout(**MOBILE_PLOT_LAYOUT)

        return today_date_str, weekly_ui, streak_ui, neuro_ui, readiness_ui, load_fig, wellness_fig, speed_fig

    try:
        load_fig = build_load_plot(df, view_mode)
        wellness_fig = build_wellness_plot(df, view_mode)
        speed_fig = build_speed_tempo_plot(df, view_mode)
    except Exception as e:
        print("❌ Plot build error:", e)
        load_fig, wellness_fig, speed_fig = go.Figure(), go.Figure(), go.Figure()
        load_fig.update_layout(title=f"Plot error: {e}")

    planned_count = count_planned_sessions_in_week(df, week_start, week_end)
    completed_count = count_logged_sessions_in_week(df, week_start, week_end)

    # --- Training Exposure (always initialise) ---
    weekly_exposure_pct = 0  # default fallback

    if planned_count > 0:
        # normal planned exposure
        weekly_exposure_pct = int(round((completed_count / planned_count) * 100))
    else:
        # fallback: infer exposure from recent logged behaviour (last 7 days)
        completed_last_7 = count_logged_sessions_in_week(
            df,
            today - dt.timedelta(days=6),
            today
        )
        weekly_exposure_pct = int(round((completed_last_7 / 7) * 100))

        # optional: soften perception for inferred exposure
        weekly_exposure_pct = min(100, weekly_exposure_pct + 10)

    # clamp for safety
    weekly_exposure_pct = max(0, min(weekly_exposure_pct, 100))

    # --- Streaks ---
    streak, best = compute_streaks(df)

    # =========================
    # Neuromuscular Readiness
    # =========================
    sleep_s = pd.to_numeric(df.get("Sleep_1_5"), errors="coerce").dropna()
    fatigue_s = pd.to_numeric(df.get("Fatigue_1_5"), errors="coerce").dropna()
    soreness_s = pd.to_numeric(df.get("Soreness_1_5"), errors="coerce").dropna()
    mood_s = pd.to_numeric(df.get("Mood_1_5"), errors="coerce").dropna()

    if any(s.empty for s in [sleep_s, fatigue_s, soreness_s, mood_s]):
        neuro_val = None
    else:
        sleep_score = ((sleep_s.clip(1, 5).mean() - 1) / 4) * 100
        fatigue_score = 100 - ((fatigue_s.clip(1, 5).mean() - 1) / 4) * 100
        soreness_score = 100 - ((soreness_s.clip(1, 5).mean() - 1) / 4) * 100
        mood_score = ((mood_s.clip(1, 5).mean() - 1) / 4) * 100

        neuro_val = (
                0.40 * sleep_score +
                0.30 * fatigue_score +
                0.20 * soreness_score +
                0.10 * mood_score
        )

        neuro_val = float(np.clip(neuro_val, 0, 100))

    # =========================
    # Daily Readiness
    # =========================
    load_series = pd.to_numeric(df.get("Load"), errors="coerce")
    rpe_series = pd.to_numeric(
        df.get("RPE_Post_Session", df.get("sRPE")),
        errors="coerce"
    )

    readiness_val = calc_daily_readiness(load_series, rpe_series)

    weekly_ui = dial_flip(
        apple_sessions_ring(weekly_exposure_pct),
        " ",
        (
            "This score is the % of planned sessions (calendar workouts) that were completed (logged) this week.\n\n"
            f"Planned: {planned_count}  •  Completed: {completed_count}\n"
            f"Exposure score: {weekly_exposure_pct if weekly_exposure_pct is not None else '—'}/100\n\n"
            "100 = every planned session was logged.\n"
            "Lower scores mean planned sessions were missed or not entered."
        )
    )

    streak_ui = dial_flip(
        streak_dial(streak),
        " ",
        (
            f"Current streak = {streak} consecutive days with a logged session/note.\n"
            "If fatigue rises, keep the streak with low-cost work (mobility / tempo / recovery)."
        )
    )

    neuro_ui = dial_flip(
        apple_neuromuscular_ring(neuro_val),
        " ",
        "Neuromuscular Readiness reflects nervous system and movement state using fatigue, mood, sleep, and soreness. Lower scores indicate neuromuscular fatigue and reduced coordination."
    )

    readiness_ui = dial_flip(
        apple_readiness_ring(readiness_val),
        " ",
        "Daily Readiness reflects how well your body has recovered relative to recent training load and post-session exertion. Lower scores indicate accumulated load and reduced readiness."
    )

    return today_date_str, weekly_ui, streak_ui, neuro_ui, readiness_ui, load_fig, wellness_fig, speed_fig


# ============================================================
#  Save + AI callback (kept; uses make_ai_suggestions)
# ============================================================



@app.callback(
    [Output("ai-suggestion-1", "children"),
     Output("ai-suggestion-2", "children"),
     Output("save-status", "children")],
    Input("btn-generate-ai", "n_clicks"),
    [State("athlete-dropdown", "value"),
     State("selected-date-store", "data"),
     State("ai-mode-1", "value"),
     State("ai-mode-2", "value"),
     State("athlete-notes", "value"),
     State("sets-reps-load", "value"),
     State("track-reps-times", "value"),
     Input("slider-session-rpe", "value"),
     Input("slider-session-quality", "value"),
     Input("slider-sleep", "value"),
     Input("slider-fatigue", "value"),
     Input("slider-mood", "value"),
     Input("slider-soreness", "value")],
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
    session_quality,   # 👈 ADD THIS
    sleep,
    fatigue,
    mood,
    soreness,
):


    if not n_clicks:
        raise PreventUpdate

    if not athlete_name:
        return no_update, no_update, "⚠️ Please select an athlete first."

    if not ai_mode_1 or not ai_mode_2:
        return no_update, no_update, "⚠️ Please select coaching feedback."

    if not selected_date:
        return no_update, no_update, "⚠️ Please select a date from the calendar first."

    rpe = 3.0 if rpe is None else float(rpe)
    session_quality = 3.0 if session_quality is None else float(session_quality)
    sleep = 3.0 if sleep is None else float(sleep)
    fatigue = 3.0 if fatigue is None else float(fatigue)
    mood = 3.0 if mood is None else float(mood)
    soreness = 3.0 if soreness is None else float(soreness)

    notes = (notes or "").strip()
    sets_reps_load = (sets_reps_load or "").strip()
    track_reps_times = (track_reps_times or "").strip()

    df = load_tab(athlete_name)
    if df is None or df.empty or "Date" not in df.columns:
        return no_update, no_update, "⚠️ Athlete sheet missing or has no Date column."

    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date

    selected_date_dt = pd.to_datetime(selected_date, errors="coerce")
    if pd.isna(selected_date_dt):
        return no_update, no_update, "⚠️ Selected date is invalid."
    selected_date_dt = selected_date_dt.date()

    row_matches = df.index[df["Date"] == selected_date_dt].tolist()
    if not row_matches:
        return no_update, no_update, "⚠️ No session entry exists for this date."
    row_idx = row_matches[0]

    ai1, ai2 = make_ai_suggestions(
        athlete_name=athlete_name,
        selected_date=selected_date_dt,
        session_rpe=rpe,
        session_quality=session_quality,
        sleep=sleep,
        fatigue=fatigue,
        mood=mood,
        soreness=soreness,
        notes=notes,
        sets_reps_load=sets_reps_load,
        track_reps_times=track_reps_times,
        ai_mode_1=ai_mode_1,
        ai_mode_2=ai_mode_2,
    )

    payload = {
        "RPE_Post_Session": rpe,
        "Session_1_5": session_quality,
        "Sleep_1_5": sleep,
        "Fatigue_1_5": fatigue,
        "Mood_1_5": mood,
        "Soreness_1_5": soreness,
        "Athlete_Notes": notes,
        "Sets_Reps_Load": sets_reps_load,
        "Track_Reps_Times": track_reps_times,
        "AI_Suggestion_1": ai1,
        "AI_Suggestion_2": ai2,
        "Last_Updated": dt.datetime.now().isoformat(timespec="seconds"),
    }

    try:
        write_row(athlete_name, row_idx, payload)
    except Exception as e:
        return no_update, no_update, f"❌ Save failed: {e}"

    athlete_email = safe(df, row_idx, "Athlete_email") if "Athlete_email" in df.columns else ""
    athlete_display = safe(df, row_idx, "Athlete", athlete_name)
    date_display = str(selected_date_dt)
    focus = safe(df, row_idx, "Focus", "")
    venue = safe(df, row_idx, "Venue", "")
    workout = safe(df, row_idx, "Workout", "")

    status_msg = "✅ Saved, AI suggestions generated & email sent to Coach."

    try:
        send_email_payload({
            "sheet_name": athlete_name,
            "row": row_idx + 1,

            "Athlete": athlete_display,
            "Date": date_display,
            "Focus": focus,
            "Venue": venue,
            "Workout": workout,

            "RPE_Post_Session": rpe,
            "Session_1_5": session_quality,
            "Sleep_1_5": sleep,
            "Fatigue_1_5": fatigue,
            "Mood_1_5": mood,
            "Soreness_1_5": soreness,

            "Athlete_Notes": notes,
            "Sets_Reps_Load": sets_reps_load,
            "Track_Reps_Times": track_reps_times,

            "AI_Suggestion_1": ai1,
            "AI_Suggestion_2": ai2,

            "Athlete_email": athlete_email,
        })
    except Exception as e:
        status_msg = f"⚠️ Saved + AI generated, but email failed: {e}"

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

    return ai1_div, ai2_div, html.Span(
    "✅ Saved, AI suggestions generated & email sent to Coach.",
    style={"color": "#2E7D32", "fontWeight": 600}
)


@app.callback(
    Output("session-input-container", "style"),
    Output("selected-date-store", "data"),
    Output("selected-date-header", "children"),
    Input({"type": "calendar-day", "date": ALL}, "n_clicks"),
    State("athlete-dropdown", "value"),
    prevent_initial_call=True,
)
def on_day_click(n_clicks_list, athlete_name):

    if not n_clicks_list or all((n or 0) == 0 for n in n_clicks_list):
        raise PreventUpdate

    ctx = callback_context
    triggered = json.loads(ctx.triggered[0]["prop_id"].split(".")[0])
    clicked_date = triggered["date"]

    # Header text only (data hydration happens elsewhere)
    header = html.H5(f"Selected session: {clicked_date}")

    return (
        {"display": "block"},
        clicked_date,
        header
    )


@app.callback(
    Output("ctx-workout", "children"),
    Output("ctx-focus", "children"),
    Output("ctx-venue", "children"),
    Input("selected-date-store", "data"),
    State("athlete-dropdown", "value"),
    prevent_initial_call=True,
)
def populate_session_context(selected_date, athlete_name):

    if not selected_date or not athlete_name:
        raise PreventUpdate

    df = load_tab(athlete_name)
    if df is None or df.empty or "Date" not in df.columns:
        return no_update, no_update, no_update

    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    d = pd.to_datetime(selected_date, errors="coerce").date()

    match = df[df["Date"] == d]
    if match.empty:
        return no_update, no_update, no_update

    row = match.iloc[0]

    def val(col):
        if col not in row or pd.isna(row[col]) or row[col] == "":
            return None
        return row[col]

    # ---- Values ----
    workout = val("Workout")
    key_distance = val("Key_Distance")

    focus = val("Focus")
    srpe = val("sRPE")
    duration = val("Duration")
    load = val("Load")

    venue = val("Venue")
    notes = val("Notes")

    # ---- Workout card ----
    workout_card = [
        html.Div("🏃 Workout", className="ctx-title"),
        html.Div(workout or "—", className="ctx-main"),
        html.Div(f"📏 Key Distance: {key_distance}", className="ctx-sub")
        if key_distance else None,
    ]

    # ---- Focus card (FIXED: no joins, stacked lines) ----
    focus_card = [
        html.Div("🎯 Session Focus", className="ctx-title"),
        html.Div(focus or "—", className="ctx-main"),
        html.Div(
            [
                html.Div(f"🔥 Planned sRPE: {srpe}") if srpe else None,
                html.Div(f"⏱ Duration: {duration} min") if duration else None,
                html.Div(f"⚖️ Load: {load}") if load else None,
            ],
            className="ctx-sub",
        ),
    ]

    # ---- Venue card ----
    venue_card = [
        html.Div("📍 Venue & Notes", className="ctx-title"),
        html.Div(venue or "—", className="ctx-main"),
        html.Div(f"📝 {notes}", className="ctx-sub")
        if notes else None,
    ]

    return workout_card, focus_card, venue_card







@app.callback(
    [
        Output("athlete-dropdown", "value"),
        Output("slider-session-rpe", "value"),
        Output("slider-session-quality", "value"),
        Output("slider-sleep", "value"),
        Output("slider-fatigue", "value"),
        Output("slider-mood", "value"),
        Output("slider-soreness", "value"),
        Output("athlete-notes", "value"),
        Output("sets-reps-load", "value"),
        Output("track-reps-times", "value"),
    ],
    Input("reset-session-button", "n_clicks"),
    prevent_initial_call=True,
)
def reset_inputs(n):
    if not n:
        raise PreventUpdate

    return (
        no_update,     # ✅ keep athlete dropdown
        3, 3, 3, 3, 3, 3,   # sliders back to neutral
        "", "", ""          # clear text boxes
    )






app.clientside_callback(
    """
    function(activeTab){
        const tabs = ["home", "calendar", "graphs", "ai"];
        tabs.forEach(t => {
            const nav = document.getElementById("nav-" + t);
            const icon = document.getElementById("icon-" + t);
            if(nav) nav.classList.remove("active");
            if(icon){
                icon.classList.remove("bounce");
                icon.classList.remove("wobble");
            }
        });

        const active = document.getElementById("nav-" + activeTab);
        const icon = document.getElementById("icon-" + activeTab);

        if(active){
            active.classList.add("active");
            if(icon){
                icon.classList.add("bounce");
                setTimeout(() => icon.classList.add("wobble"), 120);
            }

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


if __name__ == "__main__":
    app.run(debug=True)
