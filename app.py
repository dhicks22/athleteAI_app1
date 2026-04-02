# app.py — consolidated + FIXED version
# Key fixes:
# 1) ✅ No more NaTType.start_time crash: _week_agg_date() is now NaT-safe (uses week_bucket()).
# 2) ✅ Removed duplicate/contradictory definitions (dial_class_from_score, bottom_nav, imports, etc.)
# 3) ✅ Removed the broken commented-out _build_dial block that was causing indentation/parse issues.
# 4) ✅ Kept your structure + UI intact, but made the weekly bucketing + plotting robust.
# 5) ✅ Session log popup: clicking a logged day shows a read-only summary modal.
#       Clicking an unlogged day opens the session input form as before.

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
from flask import redirect, request as flask_request, jsonify

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
    margin=dict(l=24, r=16, t=48, b=64),

    font=dict(size=13),

    hoverlabel=dict(
        bgcolor="white",
        font_size=14,
        font_family="Inter, Arial",
        align="left",
    ),

    legend=dict(
        orientation="h",
        yanchor="top",
        y=-0.18,
        xanchor="center",
        x=0.5,
        font=dict(size=10),
        tracegroupgap=2,
        itemsizing="constant",
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
        return "dial-blue"
    elif score >= 60:
        return "dial-green"
    elif score >= 40:
        return "dial-amber"
    elif score >= 20:
        return "dial-red"
    else:
        return "dial-red"


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
    if score_0_100 is None:
        return _build_dial("—", 0, "dial-grey")
    try:
        v = float(score_0_100)
    except (TypeError, ValueError):
        return _build_dial("—", 0, "dial-grey")
    if np.isnan(v):
        return _build_dial("—", 0, "dial-grey")
    v = max(0, min(v, 100))
    return _build_dial(f"{int(round(v))}", v, dial_class_from_score(v))


def streak_dial(streak: int):
    s = max(0, int(streak))
    percent = min(s, 31) / 31 * 100
    colour = streak_colour_from_days(s)

    display = "0" if s == 0 else str(s)

    return _build_dial(display, percent, colour)



def dial_flip(front_child, back_title: str, back_body):
    return html.Div(
        className="dial-flip",
        style={"width": "var(--dial-size)", "height": "var(--dial-size)"},
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
    # Streak dial is always hot pink — intensity reflects length via fill %
    return "dial-pink"


def calc_daily_readiness(load_series, rpe_series, quality_series, span=7):
    df = pd.DataFrame({
        "load": pd.to_numeric(load_series, errors="coerce"),
        "rpe":  pd.to_numeric(rpe_series,  errors="coerce"),
        "qual": pd.to_numeric(quality_series, errors="coerce"),
    })

    rpe_valid = df["rpe"].dropna()
    if rpe_valid.empty:
        return None

    load_ref = df["load"].quantile(0.90)
    if pd.isna(load_ref) or load_ref <= 0:
        load_ref = df["load"].max()
    if pd.isna(load_ref) or load_ref <= 0:
        return None

    load_n = df["load"].fillna(0) / load_ref
    rpe_n  = 0.6 + 0.4 * ((df["rpe"].fillna(3).clip(1, 5) - 1) / 4)
    qual_n = 1.10 - 0.20 * ((df["qual"].fillna(3).clip(1, 5) - 1) / 4)

    df["stress"] = (load_n * rpe_n * qual_n).fillna(0)

    acute   = df["stress"].ewm(span=7,  adjust=False).mean()
    chronic = df["stress"].ewm(span=28, adjust=False).mean()

    a_val = float(acute.iloc[-1])
    c_val = float(chronic.iloc[-1])

    if pd.isna(a_val) or pd.isna(c_val) or c_val == 0:
        base_readiness = 65.0
    else:
        acwr = a_val / c_val

        if acwr < 0.8:
            base_readiness = 55.0 + (acwr / 0.8) * 20.0
        elif acwr <= 1.3:
            if acwr <= 1.0:
                base_readiness = 60.0 + ((acwr - 0.8) / 0.2) * 25.0
            else:
                base_readiness = 85.0 - ((acwr - 1.0) / 0.3) * 10.0
        elif acwr <= 1.5:
            base_readiness = 75.0 - ((acwr - 1.3) / 0.2) * 25.0
        else:
            base_readiness = max(20.0, 50.0 - (acwr - 1.5) * 30.0)

    base_readiness = float(np.clip(base_readiness, 0, 100))

    rpe_dated = pd.to_numeric(rpe_series, errors="coerce")
    last_pos  = rpe_dated.last_valid_index()

    if last_pos is not None and hasattr(last_pos, "date"):
        days_silent = (dt.datetime.now(ADL_TZ).date() - last_pos.date()).days
    elif last_pos is not None:
        days_silent = len(rpe_dated) - 1 - rpe_dated.index.get_loc(last_pos)
    else:
        days_silent = len(df)

    days_silent = max(0, int(days_silent))

    DECAY_RATE = 5.0
    MAX_PENALTY = 50.0

    if days_silent > 0:
        penalty    = min(DECAY_RATE * days_silent, MAX_PENALTY)
        readiness  = float(np.clip(base_readiness - penalty, 0, 100))
    else:
        readiness  = base_readiness

    load_dated    = pd.to_numeric(load_series, errors="coerce")
    last_load_pos = load_dated.last_valid_index()

    if last_load_pos is not None and hasattr(last_load_pos, "date"):
        days_since_load = (dt.datetime.now(ADL_TZ).date() - last_load_pos.date()).days
        if days_since_load > 21:
            readiness = min(readiness, 35)
        elif days_since_load > 14:
            readiness = min(readiness, 50)
        elif days_since_load > 7:
            readiness = min(readiness, 68)

    return float(np.clip(readiness, 0, 100))


def calc_neuro_readiness(sleep, fatigue, soreness, mood, history_df=None, span=3):
    try:
        sleep = float(np.clip(sleep, 1, 5))
        fatigue = float(np.clip(fatigue, 1, 5))
        soreness = float(np.clip(soreness, 1, 5))
        mood = float(np.clip(mood, 1, 5))
    except Exception:
        return None

    raw = (
            sleep * 0.35 +
            fatigue * 0.30 +
            (6 - soreness) * 0.20 +
            mood * 0.15
    )

    score = (raw - 1.8) / (4.5 - 1.8) * 100
    score = float(np.clip(score, 0, 100))

    if history_df is None or history_df.empty:
        return score

    def _row_score(r):
        try:
            s = float(np.clip(pd.to_numeric(r.get("Sleep_1_5"), errors="coerce"), 1, 5))
            f = float(np.clip(pd.to_numeric(r.get("Fatigue_1_5"), errors="coerce"), 1, 5))
            so = float(np.clip(pd.to_numeric(r.get("Soreness_1_5"), errors="coerce"), 1, 5))
            m = float(np.clip(pd.to_numeric(r.get("Mood_1_5"), errors="coerce"), 1, 5))
            raw = s * 0.35 + f * 0.30 + (6 - so) * 0.20 + m * 0.15
            return float(np.clip((raw - 1.8) / 2.7 * 100, 0, 100))
        except Exception:
            return np.nan

    hist_scores = history_df.apply(_row_score, axis=1).dropna()

    if hist_scores.empty:
        return score

    hist_scores = pd.concat([hist_scores, pd.Series([score])], ignore_index=True)
    smooth = float(hist_scores.ewm(span=span, adjust=False).mean().iloc[-1])

    if "Date" in history_df.columns:
        last_entry = pd.to_datetime(history_df["Date"], errors="coerce").max()
        if pd.notna(last_entry):
            days_stale = (dt.datetime.now(ADL_TZ).date() - last_entry.date()).days
            if days_stale > 14:
                smooth *= 0.55
            elif days_stale > 7:
                smooth *= 0.75

    return float(np.clip(smooth, 0, 100))




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

    try:
        records = ws.get_all_records()
        df = pd.DataFrame(records)
    except Exception:
        all_values = ws.get_all_values()
        if not all_values:
            return pd.DataFrame()

        headers = all_values[0]
        seen = {}
        clean_headers = []
        for h in headers:
            h = h.strip() if h else ""
            if not h:
                h = "_unnamed"
            if h in seen:
                seen[h] += 1
                h = f"{h}_{seen[h]}"
            else:
                seen[h] = 0
            clean_headers.append(h)

        df = pd.DataFrame(all_values[1:], columns=clean_headers)
        df = df.loc[:, ~df.columns.str.startswith("_unnamed")]

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
        return {"logged": False, "rpe": None}

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce").dt.date
    rows = d[d["Date"] == date_obj]

    if rows.empty:
        return {"logged": False, "rpe": None}

    row = rows.iloc[-1]

    invalid_text = {"", "nan", "none", "nil", "0", "n/a", "na", "-", "—"}

    def _has_value(col):
        raw = str(row.get(col, "")).strip()
        return raw.lower() not in invalid_text

    has_notes = _has_value("Athlete_Notes")
    has_sets  = _has_value("Sets_Reps_Load")
    has_track = _has_value("Track_Reps_Times")

    rpe_post = pd.to_numeric(row.get("RPE_Post_Session", np.nan), errors="coerce")
    has_rpe  = pd.notna(rpe_post) and rpe_post > 0

    sleep_val    = pd.to_numeric(row.get("Sleep_1_5",    np.nan), errors="coerce")
    fatigue_val  = pd.to_numeric(row.get("Fatigue_1_5",  np.nan), errors="coerce")
    mood_val     = pd.to_numeric(row.get("Mood_1_5",     np.nan), errors="coerce")
    soreness_val = pd.to_numeric(row.get("Soreness_1_5", np.nan), errors="coerce")
    has_wellness = any(
        pd.notna(v) and v > 0
        for v in [sleep_val, fatigue_val, mood_val, soreness_val]
    )

    logged = has_notes or has_sets or has_track or has_rpe or has_wellness

    return {
        "logged": logged,
        "rpe": float(rpe_post) if has_rpe else None,
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

    athlete_cols = ["Athlete_Notes", "RPE_Post_Session", "Sleep_1_5", "Fatigue_1_5"]
    if not any(c in ddf.columns for c in athlete_cols):
        return 0, 0

    logged_days = set()

    for d in ddf["Date"].unique():
        status = get_day_status(ddf, d)
        if status.get("logged", False):
            logged_days.add(d)

    today = today_adl()
    yesterday = today - dt.timedelta(days=1)

    streak = 0
    # Grace period: if today isn't logged yet, start from yesterday.
    # The streak only resets if BOTH today and yesterday are unlogged.
    # This prevents the streak dropping to zero first thing in the morning.
    if today in logged_days:
        cursor = today
    else:
        cursor = yesterday

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
        return "No previous session data available."

    d = df.copy()
    if "Date" in d.columns:
        d["Date"] = pd.to_datetime(d["Date"], errors="coerce").dt.date
        d = d.sort_values("Date")

    athlete_cols = ["Athlete_Notes", "Sets_Reps_Load", "Track_Reps_Times",
                    "RPE_Post_Session", "Sleep_1_5", "Fatigue_1_5",
                    "Mood_1_5", "Soreness_1_5"]

    present = [c for c in athlete_cols if c in d.columns]
    if not present:
        return "No previous session data available."

    tail = d.tail(max_rows)
    lines = []

    for _, r in tail.iterrows():
        date_str = str(r.get("Date", "unknown"))

        note     = str(r.get("Athlete_Notes",   "")).strip()
        sets     = str(r.get("Sets_Reps_Load",  "")).strip()
        track    = str(r.get("Track_Reps_Times","")).strip()
        ai1_prev = str(r.get("AI_Suggestion_1", "")).strip()

        rpe      = pd.to_numeric(r.get("RPE_Post_Session"), errors="coerce")
        sleep    = pd.to_numeric(r.get("Sleep_1_5"),        errors="coerce")
        fatigue  = pd.to_numeric(r.get("Fatigue_1_5"),      errors="coerce")
        mood     = pd.to_numeric(r.get("Mood_1_5"),         errors="coerce")
        soreness = pd.to_numeric(r.get("Soreness_1_5"),     errors="coerce")

        has_data = (
            any(s.lower() not in ("", "nan", "none", "nil")
                for s in [note, sets, track])
            or any(pd.notna(v) and v > 0
                   for v in [rpe, sleep, fatigue, mood, soreness])
        )
        if not has_data:
            continue

        parts = [f"[{date_str}]"]

        flags = []
        if pd.notna(soreness):
            if soreness >= 4:
                flags.append(f"HIGH soreness ({int(soreness)}/5)")
            elif soreness >= 3:
                flags.append(f"moderate soreness ({int(soreness)}/5)")

        if pd.notna(fatigue):
            if fatigue <= 2:
                flags.append(f"LOW energy/fatigue ({int(fatigue)}/5)")
            elif fatigue <= 3:
                flags.append(f"moderate fatigue ({int(fatigue)}/5)")

        if pd.notna(sleep):
            if sleep <= 2:
                flags.append(f"POOR sleep ({int(sleep)}/5)")
            elif sleep <= 3:
                flags.append(f"average sleep ({int(sleep)}/5)")

        if pd.notna(mood):
            if mood <= 2:
                flags.append(f"LOW mood ({int(mood)}/5)")

        if pd.notna(rpe):
            parts.append(f"RPE {int(rpe)}/5")

        if flags:
            parts.append("Wellness: " + ", ".join(flags))
        else:
            if any(pd.notna(v) for v in [sleep, fatigue, mood, soreness]):
                parts.append("Wellness: all markers within normal range")

        if note and note.lower() not in ("nan", "none", "nil"):
            parts.append(f"Note: {note}")
        if sets and sets.lower() not in ("nan", "none", "nil"):
            parts.append(f"Gym: {sets}")
        if track and track.lower() not in ("nan", "none", "nil"):
            parts.append(f"Track: {track}")

        if ai1_prev and ai1_prev.lower() not in ("nan", "none", "nil"):
            trimmed = ai1_prev[:120].strip()
            if len(ai1_prev) > 120:
                trimmed += "…"
            parts.append(f"Prev AI said: {trimmed}")

        lines.append(" | ".join(parts))

    if not lines:
        return "No previous logged sessions found in this window."

    return "Recent logged sessions (oldest → newest):\n" + "\n".join(lines)

def build_wellness_flags(df: pd.DataFrame, days: int = 7) -> str:
    if df.empty or "Date" not in df.columns:
        return ""

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce").dt.date
    d = d.sort_values("Date")

    cutoff = today_adl() - dt.timedelta(days=days)
    recent = d[d["Date"] >= cutoff]

    if recent.empty:
        return ""

    def _series(col):
        if col not in recent.columns:
            return pd.Series(dtype=float)
        s = pd.to_numeric(recent[col], errors="coerce").dropna()
        return s[s > 0]

    soreness = _series("Soreness_1_5")
    fatigue  = _series("Fatigue_1_5")
    sleep    = _series("Sleep_1_5")
    mood     = _series("Mood_1_5")
    rpe      = _series("RPE_Post_Session")

    flags = []

    if not soreness.empty:
        avg_sor = soreness.mean()
        days_high_sor = int((soreness >= 4).sum())
        if days_high_sor >= 3:
            flags.append(
                f"⚠️ Soreness has been HIGH (≥4/5) on {days_high_sor} of the last "
                f"{len(soreness)} logged days (avg {avg_sor:.1f}/5) — "
                "possible accumulated muscle stress, consider load reduction or recovery work."
            )
        elif avg_sor >= 3.5:
            flags.append(
                f"Soreness trending elevated (avg {avg_sor:.1f}/5 over {len(soreness)} sessions)."
            )

    if not fatigue.empty:
        avg_fat = fatigue.mean()
        days_low_fat = int((fatigue <= 2).sum())
        if days_low_fat >= 3:
            flags.append(
                f"⚠️ Energy/fatigue LOW (≤2/5) on {days_low_fat} of last "
                f"{len(fatigue)} logged days (avg {avg_fat:.1f}/5) — "
                "athlete reporting consistently low energy."
            )
        elif avg_fat <= 2.5:
            flags.append(
                f"Fatigue/energy trending low (avg {avg_fat:.1f}/5 over {len(fatigue)} sessions)."
            )

    if not sleep.empty:
        avg_slp = sleep.mean()
        days_poor_slp = int((sleep <= 2).sum())
        if days_poor_slp >= 2:
            flags.append(
                f"⚠️ Sleep has been POOR (≤2/5) on {days_poor_slp} of last "
                f"{len(sleep)} logged days (avg {avg_slp:.1f}/5)."
            )

    if not mood.empty:
        avg_mood = mood.mean()
        if avg_mood <= 2.5 and len(mood) >= 3:
            flags.append(
                f"Mood trending low over {len(mood)} sessions (avg {avg_mood:.1f}/5) — "
                "worth a brief check-in."
            )

    if len(rpe) >= 4:
        first_half = rpe.iloc[:len(rpe)//2].mean()
        second_half = rpe.iloc[len(rpe)//2:].mean()
        if second_half - first_half >= 0.8:
            flags.append(
                f"RPE trending UP over 7 days ({first_half:.1f} → {second_half:.1f}/5) — "
                "sessions feeling harder for likely same workload."
            )

    if not flags:
        return "No persistent wellness concerns in the last 7 days."

    return "7-day wellness scan:\n" + "\n".join(f"- {f}" for f in flags)

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

    workout   = safe(df, row_idx, "Workout",  "not specified") if row_idx is not None else "not specified"
    focus_txt = safe(df, row_idx, "Focus",    "not specified") if row_idx is not None else "not specified"
    venue     = safe(df, row_idx, "Venue",    "not specified") if row_idx is not None else "not specified"
    upcoming  = build_upcoming_context(df, selected_date_dt, n=4)

    first_name = athlete_name.strip().split()[0] if athlete_name.strip() else "Athlete"

    summary      = build_context_summary(df, days=7)
    trend_context = build_trend_context(df, days=14)
    wellness_scan = build_wellness_flags(df, days=7)
    history_text  = build_text_history(df, max_rows=5)

    notes          = (notes or "").strip() or "none provided"
    sets_reps_load = (sets_reps_load or "").strip() or "none provided"
    track_reps_times = (track_reps_times or "").strip() or "none provided"

    session_block = (
        f"SESSION — {selected_date_dt}\n"
        f"Workout: {workout}\n"
        f"Focus: {focus_txt}\n"
        f"Venue: {venue}\n"
        f"Post-session RPE (1–5): {session_rpe}\n"
        f"Session quality / execution (1–5): {session_quality}\n"
        f"Sleep last night (1–5): {sleep}\n"
        f"Fatigue (1–5, higher = more energetic): {fatigue}\n"
        f"Mood (1–5): {mood}\n"
        f"Soreness (1–5): {soreness}\n"
        f"Athlete notes: {notes}\n"
        f"Sets × Reps × Load: {sets_reps_load}\n"
        f"Track reps & times: {track_reps_times}\n"
        f"\nUpcoming sessions:\n{upcoming}"
    )

    if ai_mode_1 == ai_mode_2:
        ai_mode_2 = "Recovery & Readiness Coach"

    persona_1 = persona_prompt(ai_mode_1)

    system_1 = (
        f"{persona_1}\n\n"
        f"You are giving post-session feedback to {first_name}. "
        "Be specific, direct, and grounded in the numbers provided. "
        "Do NOT give generic advice. Do NOT repeat what was said in previous sessions. "
        "Do NOT mention injury or medical issues. "
        f"Always open with '{first_name},' — this is mandatory."
    )

    user_1 = (
        f"7-day summary: {summary}\n\n"
        f"Trend context (14 days):\n{trend_context}\n\n"
        f"Recent session notes:\n{history_text}\n\n"
        f"{session_block}\n\n"
        f"Wellness pattern scan (last 7 days):\n{wellness_scan}\n\n"
        "TASK — PRIMARY COACH:\n"
        f"Write feedback as the {ai_mode_1}.\n"
        f"Open with '{first_name},'\n"
        "Give 3 concrete, specific recommendations tied DIRECTLY to the logged session data above.\n"
        "You MUST reference the actual exercises, loads, or times the athlete entered.\n"
        "Do NOT give generic recovery, hydration, or nutrition advice unless the wellness data specifically justifies it.\n"
        "Focus on what to DO in the next 24–48 hours. Be a coach, not a chatbot.\n"
        "Keep to 3–5 sentences. No waffle, no generics."
        )

    ai1 = call_openai_chat(
        [{"role": "system", "content": system_1},
         {"role": "user",   "content": user_1}],
        max_tokens=500,
    )

    persona_2 = persona_prompt(ai_mode_2)

    ai1_summary = (
        f"The primary coach ({ai_mode_1}) has already addressed the main training focus. "
        f"Here is what they said:\n\"{ai1}\"\n\n"
        "Your job is to add a DIFFERENT perspective — not to repeat or agree with the above."
    )

    system_2 = (
        f"{persona_2}\n\n"
        f"You are a secondary coach giving {first_name} a complementary perspective. "
        f"{ai1_summary}"
        "Focus on what the primary coach did NOT cover. "
        "Be specific and practical. Do NOT repeat training load or speed advice if that was covered above. "
        f"Always open with '{first_name},' — this is mandatory."
    )

    user_2 = (
        f"7-day summary: {summary}\n\n"
        f"{session_block}\n\n"
        f"Wellness pattern scan (last 7 days):\n{wellness_scan}\n\n"
        "TASK — SECONDARY COACH:\n"
        f"Write as the {ai_mode_2}.\n"
        f"Open with '{first_name},'\n"
        "Add 2 specific observations tied to the actual session data — reference the exercises or loads logged.\n"
        "Do NOT mention hydration, nutrition, or mindfulness unless wellness scores specifically warrant it.\n"
        "End with ONE pointed reflective question about their training, not their lifestyle.\n"
        "3–4 sentences maximum. Be direct."
        )

    ai2 = call_openai_chat(
        [{"role": "system", "content": system_2},
         {"role": "user",   "content": user_2}],
        max_tokens=400,
    )

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
#  Garmin Health API integration
# ============================================================

GARMIN_CONSUMER_KEY    = os.getenv("GARMIN_CONSUMER_KEY", "")
GARMIN_CONSUMER_SECRET = os.getenv("GARMIN_CONSUMER_SECRET", "")
GARMIN_ENABLED         = bool(GARMIN_CONSUMER_KEY and GARMIN_CONSUMER_SECRET)

GARMIN_REQUEST_TOKEN_URL = "https://connectapi.garmin.com/oauth-service/oauth/request_token"
GARMIN_AUTHORIZE_URL     = "https://connect.garmin.com/oauthConfirm"
GARMIN_ACCESS_TOKEN_URL  = "https://connectapi.garmin.com/oauth-service/oauth/access_token"
GARMIN_API_BASE          = "https://apis.garmin.com/wellness-api/rest"


def _garmin_oauth1(user_token=None, user_secret=None, verifier=None):
    """Build an OAuth1 auth object. Requires requests-oauthlib."""
    try:
        from requests_oauthlib import OAuth1
    except ImportError:
        raise RuntimeError("Install requests-oauthlib: pip install requests-oauthlib")
    return OAuth1(
        GARMIN_CONSUMER_KEY,
        GARMIN_CONSUMER_SECRET,
        resource_owner_key=user_token,
        resource_owner_secret=user_secret,
        verifier=verifier,
    )


def garmin_get_request_token():
    auth = _garmin_oauth1()
    r = requests.post(GARMIN_REQUEST_TOKEN_URL, auth=auth, timeout=10)
    r.raise_for_status()
    params = dict(p.split("=") for p in r.text.split("&"))
    return params["oauth_token"], params["oauth_token_secret"]


def garmin_get_access_token(oauth_token, oauth_token_secret, oauth_verifier):
    auth = _garmin_oauth1(oauth_token, oauth_token_secret, oauth_verifier)
    r = requests.post(GARMIN_ACCESS_TOKEN_URL, auth=auth, timeout=10)
    r.raise_for_status()
    params = dict(p.split("=") for p in r.text.split("&"))
    return params["oauth_token"], params["oauth_token_secret"]


def garmin_get_athlete_tokens(df: pd.DataFrame):
    """
    Read Garmin_Token and Garmin_Secret from the first row of the athlete sheet.
    Returns (token, secret) or (None, None) if not linked.
    """
    if df is None or df.empty:
        return None, None
    for col_t, col_s in [("Garmin_Token", "Garmin_Secret"),
                          ("garmin_token", "garmin_secret")]:
        if col_t in df.columns and col_s in df.columns:
            tok = df[col_t].dropna().astype(str)
            sec = df[col_s].dropna().astype(str)
            tok = tok[tok.str.strip().str.len() > 5]
            sec = sec[sec.str.strip().str.len() > 5]
            if not tok.empty and not sec.empty:
                return tok.iloc[0].strip(), sec.iloc[0].strip()
    return None, None


def garmin_fetch_today(user_token: str, user_secret: str, date: dt.date) -> dict:
    """
    Pull today's data from Garmin for a linked athlete.
    Returns raw dict of all available fields.
    """
    auth = _garmin_oauth1(user_token, user_secret)

    # Garmin uses unix timestamps for range queries
    start_ts = int(dt.datetime.combine(date, dt.time.min).timestamp())
    end_ts   = int(dt.datetime.combine(date, dt.time.max).timestamp())

    endpoints = {
        "dailies":    f"{GARMIN_API_BASE}/dailies?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
        "sleeps":     f"{GARMIN_API_BASE}/sleeps?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
        "activities": f"{GARMIN_API_BASE}/activities?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
        "hrv":        f"{GARMIN_API_BASE}/hrv?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
        "bodyBattery": f"{GARMIN_API_BASE}/bodyBattery?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
        "stressDetails": f"{GARMIN_API_BASE}/stressDetails?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
    }

    results = {}
    for key, url in endpoints.items():
        try:
            r = requests.get(url, auth=auth, timeout=10)
            if r.status_code == 200:
                results[key] = r.json()
            else:
                results[key] = {}
        except Exception:
            results[key] = {}

    return results


def garmin_parse_to_scales(raw: dict) -> dict:
    """
    Convert Garmin API response → your app's column names.
    Returns dict ready to be written to the Google Sheet row.

    Mapping logic:
      Sleep_1_5    ← Garmin sleep score (0-100) → 1-5
      Fatigue_1_5  ← Body Battery high point (0-100) → 1-5 (high BB = energetic)
      Soreness_1_5 ← Average stress level (0-100) → 1-5 (inverted: high stress = high soreness)
      Mood_1_5     ← HRV status: POOR=1 LOW=2 UNBALANCED=2 BALANCED=4 HIGH=5
      Load         ← activityTrainingLoad from activity
      SPEED (m)    ← total distance for speed-type activities
    """
    out = {}

    # ── Dailies ──────────────────────────────────────────────
    dailies = raw.get("dailies", [])
    if dailies:
        d = dailies[0]
        out["Garmin_Steps"]           = d.get("steps")
        out["Garmin_Resting_HR"]      = d.get("restingHeartRateInBeatsPerMinute")
        out["Garmin_Avg_HR"]          = d.get("averageHeartRateInBeatsPerMinute")
        out["Garmin_Stress_Avg"]      = d.get("averageStressLevel")
        out["Garmin_BB_Low"]          = d.get("bodyBatteryLowestValue")
        out["Garmin_BB_High"]         = d.get("bodyBatteryHighestValue")
        out["Garmin_Intensity_Mins"]  = (
            (d.get("moderateIntensityMinutes") or 0) +
            (d.get("vigorousIntensityMinutes") or 0)
        )

        # Fatigue_1_5 from body battery high (high BB = energetic = high score)
        bb_high = d.get("bodyBatteryHighestValue")
        if bb_high is not None:
            out["Fatigue_1_5"] = max(1, min(5, round(bb_high / 20)))

        # Soreness proxy from stress (inverted)
        stress = d.get("averageStressLevel")
        if stress is not None:
            out["Soreness_1_5"] = max(1, min(5, 6 - round(stress / 20)))

    # ── Sleep ────────────────────────────────────────────────
    sleeps = raw.get("sleeps", [])
    if sleeps:
        s = sleeps[0]
        out["Garmin_Sleep_Dur_s"]   = s.get("durationInSeconds")
        out["Garmin_Sleep_Deep_s"]  = s.get("deepSleepDurationInSeconds")
        out["Garmin_Sleep_REM_s"]   = s.get("remSleepInSeconds")
        out["Garmin_Sleep_Score"]   = (s.get("overallSleepScore") or {}).get("value")
        out["Garmin_SpO2_Avg"]      = s.get("averageSpO2Value")

        sleep_score = out.get("Garmin_Sleep_Score")
        if sleep_score is not None:
            out["Sleep_1_5"] = max(1, min(5, round(sleep_score / 20)))

    # ── HRV ──────────────────────────────────────────────────
    hrv_list = raw.get("hrv", [])
    if hrv_list:
        h = hrv_list[0].get("hrvSummary", {})
        out["Garmin_HRV_Weekly_Avg"] = h.get("weeklyAvg")
        out["Garmin_HRV_LastNight"]  = h.get("lastNight")
        out["Garmin_HRV_Status"]     = h.get("status")

        hrv_map = {"POOR": 1, "LOW": 2, "UNBALANCED": 2, "BALANCED": 4, "HIGH": 5}
        hrv_status = (h.get("status") or "").upper()
        if hrv_status in hrv_map:
            out["Mood_1_5"] = hrv_map[hrv_status]

    # ── Activities ───────────────────────────────────────────
    activities = raw.get("activities", [])
    if activities:
        a = activities[0]
        out["Garmin_Activity_Name"]  = a.get("activityName")
        out["Garmin_Activity_Type"]  = a.get("activityType")
        out["Garmin_Duration_s"]     = a.get("durationInSeconds")
        out["Garmin_Distance_m"]     = a.get("distanceInMeters")
        out["Garmin_Activity_HR"]    = a.get("averageHeartRateInBeatsPerMinute")
        out["Garmin_Training_Effect"] = a.get("aerobicTrainingEffect")

        garmin_load = a.get("activityTrainingLoad")
        if garmin_load is not None:
            out["Load"] = round(float(garmin_load), 1)

        dist_m = a.get("distanceInMeters")
        if dist_m is not None:
            out["SPEED (m)"] = round(float(dist_m), 0)

    out["Garmin_Synced"] = "yes"
    return out


def garmin_enrich_df_row(df: pd.DataFrame, date: dt.date) -> dict:
    """
    For a given athlete df and date, return the best available
    values for dial computation — preferring Garmin data where
    present, falling back to manual slider entries.
    """
    result = {
        "sleep":    None, "fatigue":  None,
        "mood":     None, "soreness": None,
        "load":     None, "rpe":      None,
        "quality":  None,
        "source":   "manual",
    }

    if df is None or df.empty or "Date" not in df.columns:
        return result

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce").dt.date
    rows = d[d["Date"] == date]
    if rows.empty:
        return result

    row = rows.iloc[-1]

    def _n(col):
        v = pd.to_numeric(row.get(col, np.nan), errors="coerce")
        return float(v) if pd.notna(v) and v > 0 else None

    # Check if Garmin data is present for this row
    garmin_synced = str(row.get("Garmin_Synced", "")).strip().lower() == "yes"

    # Always prefer manual if athlete entered it (RPE_Post_Session written by athlete)
    manual_rpe = _n("RPE_Post_Session")

    if garmin_synced:
        result["source"]  = "garmin"
        result["sleep"]   = _n("Sleep_1_5")    # already mapped from sleep score
        result["fatigue"] = _n("Fatigue_1_5")  # from body battery
        result["soreness"]= _n("Soreness_1_5") # from stress
        result["mood"]    = _n("Mood_1_5")     # from HRV status
        result["load"]    = _n("Load")          # from training load
        result["rpe"]     = manual_rpe          # RPE stays manual always
        result["quality"] = _n("Session_1_5")
    else:
        result["source"]  = "manual"
        result["sleep"]   = _n("Sleep_1_5")
        result["fatigue"] = _n("Fatigue_1_5")
        result["soreness"]= _n("Soreness_1_5")
        result["mood"]    = _n("Mood_1_5")
        result["load"]    = _n("Load")
        result["rpe"]     = manual_rpe
        result["quality"] = _n("Session_1_5")

    return result


# ============================================================
#  Plot builders
# ============================================================

def build_load_plot(df: pd.DataFrame, view_mode: str):
    fig = go.Figure()

    if df.empty or "Date" not in df.columns or "Load" not in df.columns:
        fig.update_layout(**MOBILE_PLOT_LAYOUT)
        return fig

    BLUE       = "#1E6BD6"
    TEAL       = "#1BA39C"
    GREEN_DARK = "#6B7280"
    PURPLE     = "#7B61FF"

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.sort_values("Date")
    d["Load"] = pd.to_numeric(d["Load"], errors="coerce")

    if view_mode == "weekly":
        d["Week"] = _week_agg_date(d["Date"])

        g = d.groupby("Week", as_index=False).agg(
            Load=("Load", lambda s: s.sum(min_count=1))
        )

        g["EWMA7"]  = g["Load"].ewm(span=7,  adjust=False, min_periods=3).mean()
        g["EWMA28"] = g["Load"].ewm(span=28, adjust=False, min_periods=10).mean()
        g["ACWR"]   = g["EWMA7"] / g["EWMA28"]

        x = g["Week"]

        fig.add_bar(
            x=x,
            y=g["Load"],
            name="Load",
            marker=dict(
                color="rgba(30,107,214,0.35)",
                line=dict(color=BLUE, width=1.8),
            ),
            hovertemplate="Load: %{y:,.0f}<extra></extra>",
        )

        fig.add_trace(go.Scatter(
            x=x, y=g["EWMA7"],
            name="Short-term Load",
            mode="lines",
            line=dict(color=TEAL, width=2.6),
            line_shape="spline",
            line_smoothing=0.75,
            showlegend=False,
            hovertemplate="Short-term: %{y:,.0f}<extra></extra>",
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
            showlegend=False,
            hovertemplate="Long-term: %{y:,.0f}<extra></extra>",
        ))

        fig.add_trace(go.Scatter(
            x=x, y=g["ACWR"],
            name="ACWR",
            mode="lines",
            yaxis="y2",
            line=dict(color=PURPLE, width=1.6),
            line_shape="spline",
            line_smoothing=0.75,
            opacity=0.7,
            hovertemplate="ACWR: %{y:.2f}<extra></extra>",
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
            title="Weekly Training Load & Balance",
            xaxis_title="",
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

    d["EWMA7"]  = d["Load"].ewm(span=7,  adjust=False).mean()
    d["EWMA28"] = d["Load"].ewm(span=28, adjust=False).mean()
    d["ACWR"]   = d["EWMA7"] / d["EWMA28"]

    x = d["Date"]

    fig.add_bar(
        x=x,
        y=d["Load"],
        name="Daily Load",
        marker=dict(
            color="rgba(30,107,214,0.35)",
            line=dict(color=BLUE, width=1.8),
        ),
        hovertemplate="Load: %{y:,.0f}<extra></extra>",
    )

    fig.add_trace(go.Scatter(
        x=x, y=d["EWMA7"],
        name="Short-term Load",
        mode="lines",
        line=dict(color=TEAL, width=2.6),
        line_shape="spline",
        line_smoothing=0.75,
        hovertemplate="Short-term: %{y:,.0f}<extra></extra>",
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
        showlegend=False,
        hovertemplate="Long-term: %{y:,.0f}<extra></extra>",
    ))

    fig.add_trace(go.Scatter(
        x=x, y=d["ACWR"],
        name="ACWR",
        mode="lines",
        yaxis="y2",
        line=dict(color=PURPLE, width=1.6),
        line_shape="spline",
        line_smoothing=0.75,
        hovertemplate="ACWR: %{y:.2f}<extra></extra>",
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
        xaxis_title="",
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
        "Sleep_1_5":    ("Sleep",    BLUE),
        "Fatigue_1_5":  ("Fatigue",  ORANGE),
        "Soreness_1_5": ("Soreness", GREEN_DARK),
        "Mood_1_5":     ("Mood",     PURPLE),
    }

    for col in metrics:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")

    if view_mode == "weekly":
        d["Week"] = _week_agg_date(d["Date"])
        g = d.groupby("Week", as_index=False).mean(numeric_only=True)
        x = g["Week"]
        window = 3

        for col, (label, color) in metrics.items():
            if col not in g.columns:
                continue
            y = g[col]
            if y.dropna().empty:
                continue
            roll = y.rolling(window, min_periods=1).mean()

            fig.add_trace(go.Scatter(
                x=x, y=roll,
                mode="lines",
                line=dict(width=0),
                fill="tozeroy",
                fillcolor=f"rgba{tuple(int(color[i:i+2], 16) for i in (1,3,5)) + (0.12,)}",
                hoverinfo="skip",
                showlegend=False,
            ))

            fig.add_trace(go.Scatter(
                x=x, y=roll,
                name=label,
                mode="lines",
                line=dict(color=color, width=2.6),
                line_shape="spline",
                line_smoothing=0.7,
                hovertemplate=f"{label}: %{{y:.1f}}<extra></extra>",
            ))

        fig.update_layout(
            title="Weekly Wellness Trends",
            xaxis_title="",
            yaxis=dict(
                title="Scale (1–5)",
                range=[0.8, 5.2],
                tickvals=[1, 2, 3, 4, 5],
            ),
            hovermode="x unified",
        )
        fig.update_layout(**MOBILE_PLOT_LAYOUT)
        return fig

    x = d["Date"]
    window = 3

    for col, (label, color) in metrics.items():
        if col not in d.columns:
            continue
        y = d[col]
        if y.dropna().empty:
            continue
        roll = y.rolling(window, min_periods=1).mean()

        fig.add_trace(go.Scatter(
            x=x, y=[0] * len(x),
            mode="lines",
            line=dict(width=0),
            showlegend=False,
            hoverinfo="skip",
        ))

        fig.add_trace(go.Scatter(
            x=x, y=roll,
            mode="lines",
            line=dict(width=0),
            fill="tonexty",
            fillcolor=f"rgba{tuple(int(color[i:i+2], 16) for i in (1,3,5)) + (0.12,)}",
            hoverinfo="skip",
            showlegend=False,
        ))

        fig.add_trace(go.Scatter(
            x=x, y=roll,
            name=label,
            mode="lines",
            line=dict(color=color, width=2.6),
            line_shape="spline",
            line_smoothing=0.7,
            hovertemplate=f"{label}: %{{y:.1f}}<extra></extra>",
            visible="legendonly" if label == "Mood" else True,
        ))

    fig.update_layout(
        title="Daily Wellness Trends",
        xaxis_title="",
        yaxis=dict(
            title="Scale (1–5)",
            range=[0.8, 5.2],
            tickvals=[1, 2, 3, 4, 5],
        ),
        hovermode="x unified",
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

    if view_mode == "daily":
        x = d["Date"]

        fig.add_bar(
            x=x, y=speed,
            name="Speed exposure",
            marker=dict(
                color="rgba(37,99,235,0.35)",
                line=dict(color=BLUE, width=1.6),
            ),
            hovertemplate="Speed: %{y:,.0f} m<extra></extra>",
        )

        fig.add_bar(
            x=x, y=tempo,
            name="Tempo exposure",
            marker=dict(
                color="rgba(245,158,11,0.35)",
                line=dict(color=ORANGE, width=1.6),
            ),
            hovertemplate="Tempo: %{y:,.0f} m<extra></extra>",
        )

        fig.add_trace(go.Scatter(
            x=x,
            y=speed.rolling(7, min_periods=1).mean(),
            name="Speed 7d",
            mode="lines",
            showlegend=False,
            line=dict(color=BLUE, width=2.4, dash="dot"),
            line_shape="spline",
            line_smoothing=0.7,
            hovertemplate="Speed 7d: %{y:,.0f} m<extra></extra>",
        ))

        fig.add_trace(go.Scatter(
            x=x,
            y=speed.rolling(28, min_periods=1).mean(),
            name="Speed 28d",
            mode="lines",
            showlegend=False,
            line=dict(color=BLUE, width=2.4, dash="dash"),
            line_shape="spline",
            line_smoothing=0.7,
            hovertemplate="Speed 28d: %{y:,.0f} m<extra></extra>",
            visible="legendonly",
        ))

        fig.add_trace(go.Scatter(
            x=x,
            y=tempo.rolling(7, min_periods=1).mean(),
            name="Tempo 7d",
            mode="lines",
            showlegend=False,
            line=dict(color=ORANGE, width=2.4, dash="dot"),
            line_shape="spline",
            line_smoothing=0.7,
            hovertemplate="Tempo 7d: %{y:,.0f} m<extra></extra>",
        ))

        fig.add_trace(go.Scatter(
            x=x,
            y=tempo.rolling(28, min_periods=1).mean(),
            name="Tempo 28d",
            mode="lines",
            showlegend=False,
            line=dict(color=ORANGE, width=2.4, dash="dash"),
            line_shape="spline",
            line_smoothing=0.7,
            hovertemplate="Tempo 28d: %{y:,.0f} m<extra></extra>",
            visible="legendonly",
        ))

        fig.update_layout(
            title="Daily Speed & Tempo Volumes",
            xaxis_title="",
            yaxis_title="Metres",
            barmode="stack",
            hovermode="x unified",
            **MOBILE_PLOT_LAYOUT,
        )

        return fig

    d["Week"]        = _week_agg_date(d["Date"])
    d["Speed_clean"] = speed
    d["Tempo_clean"] = tempo

    g = d.groupby("Week", as_index=False).agg(
        Speed=("Speed_clean", lambda s: s.sum(min_count=1)),
        Tempo=("Tempo_clean", lambda s: s.sum(min_count=1)),
    )

    x = g["Week"]

    fig.add_bar(
        x=x, y=g["Speed"],
        name="Speed exposure",
        marker=dict(
            color="rgba(37,99,235,0.35)",
            line=dict(color=BLUE, width=1.6),
        ),
        hovertemplate="Speed: %{y:,.0f} m<extra></extra>",
    )

    fig.add_bar(
        x=x, y=g["Tempo"],
        name="Tempo exposure",
        marker=dict(
            color="rgba(245,158,11,0.35)",
            line=dict(color=ORANGE, width=1.6),
        ),
        hovertemplate="Tempo: %{y:,.0f} m<extra></extra>",
    )

    fig.add_trace(go.Scatter(
        x=x,
        y=g["Speed"].rolling(1, min_periods=1).mean(),
        name="Speed 7d",
        mode="lines",
        line=dict(color=BLUE, width=2.4, dash="dot"),
        line_shape="spline",
        line_smoothing=0.7,
        hovertemplate="Speed 7d: %{y:,.0f} m<extra></extra>",
    ))

    fig.add_trace(go.Scatter(
        x=x,
        y=g["Speed"].rolling(4, min_periods=1).mean(),
        name="Speed 28d",
        mode="lines",
        line=dict(color=BLUE, width=2.4, dash="dash"),
        line_shape="spline",
        line_smoothing=0.7,
        hovertemplate="Speed 28d: %{y:,.0f} m<extra></extra>",
    ))

    fig.add_trace(go.Scatter(
        x=x,
        y=g["Tempo"].rolling(1, min_periods=1).mean(),
        name="Tempo 7d",
        mode="lines",
        line=dict(color=ORANGE, width=2.4, dash="dot"),
        line_shape="spline",
        line_smoothing=0.7,
        hovertemplate="Tempo 7d: %{y:,.0f} m<extra></extra>",
    ))

    fig.add_trace(go.Scatter(
        x=x,
        y=g["Tempo"].rolling(4, min_periods=1).mean(),
        name="Tempo 28d",
        mode="lines",
        line=dict(color=ORANGE, width=2.4, dash="dash"),
        line_shape="spline",
        line_smoothing=0.7,
        hovertemplate="Tempo 28d: %{y:,.0f} m<extra></extra>",
    ))

    fig.update_layout(
        title="Weekly Speed & Tempo Volumes",
        xaxis_title="",
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

    start_weekday = first_day.weekday()
    start_offset = (start_weekday + 1) % 7
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

        rpe = load = None
        notes_val = ""

        if not match.empty:
            row = match.iloc[-1]
            rpe = pd.to_numeric(row.get("sRPE", np.nan), errors="coerce")
            load = pd.to_numeric(row.get("Load", np.nan), errors="coerce")
            notes_val = str(row.get("Athlete_Notes", "")).strip()

        if pd.isna(rpe):
            pill_color = "#CFD8DC"
        elif rpe <= 2:
            pill_color = "#4285F4"
        elif rpe <= 5:
            pill_color = "#4CAF50"
        elif rpe <= 7:
            pill_color = "#FF9800"
        else:
            pill_color = "#F44336"

        status = get_day_status(ddf, day)
        logged_session = status.get("logged", False)

        classes = ["calendar-day"]

        if day == today:
            classes.append("today")

        if logged_session:
            classes.append("logged")

        if day.month != month:
            classes.append("out-month")

        if selected_date and day == selected_date:
            classes.append("selected")

        tooltip_parts = [day.strftime("%a %d %b %Y")]

        if pd.notna(rpe):
            tooltip_parts.append(f"sRPE: {int(rpe)}/10")

        if pd.notna(load):
            tooltip_parts.append(f"Load: {round(load, 1)}")

        if notes_val and notes_val.lower() not in ["nan", "none", "nil", "0"]:
            tooltip_parts.append(f"Notes: {notes_val[:60]}")

        tooltip_text = " | ".join(tooltip_parts)

        cells.append(
            html.Div(
                [
                    html.Div(str(day.day), className="cal-day-number"),
                    html.Div(
                        className="rpe-dot",
                        style={"backgroundColor": pill_color},
                        title=tooltip_text,
                    ),
                ],
                id={"type": "calendar-day", "date": str(day)},
                n_clicks=0,
                className=" ".join(classes),
            )
        )

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
                style={"height": "50px", "marginRight": "10px", "verticalAlign": "middle"},
            ),
            html.Div(
                [
                    html.H3("Adaptive Coaching Intelligence", style={"margin": 0, "fontWeight": 600, "textAlign": align}),
                    html.Small("Empowering performance through athlete insight",
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
                className="g-2 align-items-stretch mt-1 dial-row",
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
            html.Div(id="welcome-message", className="mt-3"),
            html.Div(id="motivational-message"),

            html.Div(id="garmin-status-badge", className="mt-2"),

            html.Div(
                dbc.Button(
                    [html.I(className="bi bi-share me-2", style={"fontSize": "12px"}), "Share today's stats"],
                    id="btn-share-card",
                    color="primary",
                    outline=True,
                    size="sm",
                    style={"fontSize": "12px", "padding": "5px 14px", "borderRadius": "20px"},
                ),
                style={"textAlign": "center", "marginTop": "12px"},
            ),

            dbc.Modal(
                [
                    dbc.ModalHeader(dbc.ModalTitle("Share today's stats"), close_button=True),
                    dbc.ModalBody(
                        [
                            html.Div(id="share-card-container"),
                        ],
                        style={"padding": "0", "background": "#111"},
                    ),
                ],
                id="share-card-modal",
                is_open=False,
                centered=True,
                size="lg",
            ),

            html.Div(
                logout_button,
                style={"display": "flex", "justifyContent": "flex-end", "marginTop": "10px", "marginRight": "4px"},
            ),
        ],
        style={"display": "block"},
    )

    # CALENDAR VIEW
    calendar_view = html.Div(
        id="calendar-view",
        children=[
            html.H4("Training Program", className="mt-3"),
            html.P("Your scheduled sessions and athlete logging",
                   style={"color": "#6e6e6e", "fontSize": "13px", "margin": "-8px 0 12px 0"}),

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

            html.H4("Selected Session & Athlete Input", className="mt-3 mb-1"),
            html.P("Log your session data and generate coaching feedback",
                   style={"color": "#6e6e6e", "fontSize": "13px", "margin": "0 0 12px 0"}),

            # SESSION CONTAINER
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

                    html.Div(
                        [
                            html.Div(id="ctx-workout"),
                            html.Div(id="ctx-focus"),
                            html.Div(id="ctx-venue"),
                        ],
                        id="session-context-wrapper",
                    ),

                    dbc.Row([

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
                            dcc.Slider(id="slider-session-rpe", min=1, max=5, step=1, value=3),

                            dbc.Label("Session Quality (1 = poor, 5 = excellent)"),
                            dcc.Slider(id="slider-session-quality", min=1, max=5, step=1, value=3),

                            dbc.Label("Sleep (1 = tired, 5 = well-rested)"),
                            dcc.Slider(id="slider-sleep", min=1, max=5, step=1, value=3),

                            dbc.Label("Mood (1 = sad, 5 = upbeat)"),
                            dcc.Slider(id="slider-mood", min=1, max=5, step=1, value=3),

                            dbc.Label("Fatigue (1 = low energy, 5 = energetic)"),
                            dcc.Slider(id="slider-fatigue", min=1, max=5, step=1, value=3),

                            dbc.Label("Soreness (1 = low, 5 = high)"),
                            dcc.Slider(id="slider-soreness", min=1, max=5, step=1, value=3),

                        ], md=6),

                        dbc.Col([

                            dbc.Row(
                                [
                                    dbc.Col(
                                        [
                                            dbc.Label("Primary Coaching Feedback"),
                                            dcc.Dropdown(
                                                id="ai-mode-1",
                                                options=[
                                                    {"label": "Acceleration & Speed Coach", "value": "Acceleration & Speed Coach"},
                                                    {"label": "Tempo & Endurance Coach", "value": "Tempo & Endurance Coach"},
                                                    {"label": "Technical Sprint Coach", "value": "Technical Sprint Coach"},
                                                    {"label": "Strength & Power Coach", "value": "Strength & Power Coach"},
                                                    {"label": "Recovery & Readiness Coach", "value": "Recovery & Readiness Coach"},
                                                ],
                                                value=None,
                                                placeholder="Select Coach Feedback",
                                                searchable=False,
                                                clearable=False,
                                                optionHeight=44,
                                                style={"fontSize": "13px"},
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
                                                    {"label": "Acceleration & Speed Coach", "value": "Acceleration & Speed Coach"},
                                                    {"label": "Tempo & Endurance Coach", "value": "Tempo & Endurance Coach"},
                                                    {"label": "Technical Sprint Coach", "value": "Technical Sprint Coach"},
                                                    {"label": "Strength & Power Coach", "value": "Strength & Power Coach"},
                                                    {"label": "Recovery & Readiness Coach", "value": "Recovery & Readiness Coach"},
                                                ],
                                                value=None,
                                                placeholder="Select Coach Feedback",
                                                searchable=False,
                                                clearable=False,
                                                optionHeight=44,
                                                style={"fontSize": "13px"},
                                                className="aw-dropdown",
                                            ),
                                        ],
                                        md=6,
                                    ),
                                ],
                                className="g-3",
                            ),
                            dbc.Button(
                                "Log Session & Generate Coaching Feedback",
                                id="btn-generate-ai",
                                className="mt-4 w-100 ai-save-btn",
                            ),

                            html.Div(id="save-status", className="mt-2"),

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

    # GRAPH VIEW
    graphs_view = html.Div(
        id="graphs-view",
        style={"display": "none"},
        children=[
            html.H4("Training Analytics", className="mt-3 mb-1"),
            html.P("Load, wellness trends and speed/tempo volumes",
                   style={"color": "#6e6e6e", "fontSize": "13px", "margin": "0 0 20px 0"}),
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
            html.Div(
                style={"marginBottom": "16px"},
                children=[
                    html.Div(
                        className="d-flex align-items-center justify-content-between mb-1",
                        children=[
                            html.H4("Training Session Builder", style={"margin": 0, "fontWeight": 600}),
                            html.Div(
                                style={
                                    "background": "rgba(30,136,229,0.10)",
                                    "border": "1px solid rgba(30,136,229,0.25)",
                                    "color": "#1e88e5",
                                    "fontSize": "11px",
                                    "padding": "4px 10px",
                                    "borderRadius": "999px",
                                    "fontWeight": 700,
                                    "display": "inline-flex",
                                    "alignItems": "center",
                                    "gap": "6px",
                                },
                                children=[
                                    html.Div(className="pill-dot"),
                                    html.Span("ACI"),
                                ],
                            ),
                        ],
                    ),
                    html.P(
                        "AI-generated sessions built around your recent data.",
                        style={"color": "#6e6e6e", "fontSize": "13px", "margin": "2px 0 0 0"},
                    ),
                ],
            ),
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

    # ── SESSION LOG POPUP MODAL ──────────────────────────────
    # IDs exist at layout time so Dash can register callbacks on them.
    session_log_modal = dbc.Modal(
        [
            dbc.ModalHeader(
                dbc.ModalTitle(html.Div(id="popup-modal-title")),
                close_button=True,
            ),
            dbc.ModalBody(id="popup-modal-body"),
            dbc.ModalFooter([
                dbc.Button(
                    "Edit this session",
                    id="session-log-popup-edit",
                    color="primary",
                    size="sm",
                    n_clicks=0,
                ),
                dbc.Button(
                    "Close",
                    id="session-log-popup-close",
                    color="secondary",
                    outline=True,
                    size="sm",
                    n_clicks=0,
                ),
            ]),
        ],
        id="session-log-modal",
        is_open=False,
        scrollable=True,
        size="lg",
    )

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
            session_log_modal,   # ← modal lives here, outside any view div
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

        weekly_ui = dial_flip(apple_sessions_ring(weekly_exposure_pct), " ", "No data yet.")
        streak_ui = dial_flip(streak_dial(streak), " ", "No data yet.")
        readiness_ui = dial_flip(apple_readiness_ring(readiness_val), " ", "No data yet.")
        neuro_ui = dial_flip(apple_neuromuscular_ring(neuro_val), " ", "No data yet.")

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

    if planned_count > 0:
        weekly_exposure_pct = int(round((completed_count / planned_count) * 100))
        weekly_exposure_pct = max(0, min(weekly_exposure_pct, 100))
    else:
        weekly_exposure_pct = None

    streak, best = compute_streaks(df)

    NEURO_WINDOW = 14
    NEURO_DECAY_RATE = 3.5
    NEURO_MAX_PENALTY = 35.0

    # ── Check if this athlete has Garmin linked ───────────────
    garmin_token, garmin_secret = garmin_get_athlete_tokens(df)
    garmin_linked = bool(garmin_token and garmin_secret)

    # ── Try to fetch live Garmin data for today ───────────────
    garmin_source_today = False
    if garmin_linked:
        try:
            raw_garmin = garmin_fetch_today(garmin_token, garmin_secret, today)
            parsed     = garmin_parse_to_scales(raw_garmin)
            if parsed.get("Garmin_Synced") == "yes":
                # Write to today's sheet row so it persists
                df2 = df.copy()
                df2["Date"] = pd.to_datetime(df2["Date"], errors="coerce").dt.date
                today_matches = df2.index[df2["Date"] == today].tolist()
                if today_matches:
                    write_row(athlete_id, today_matches[0], parsed)
                    # Reload df with Garmin data written in
                    df = load_tab(athlete_id)
                garmin_source_today = True
        except Exception as e:
            print(f"⚠️ Garmin fetch failed for {athlete_id}: {e}")

    # ── Get today's enriched row (Garmin or manual) ──────────
    today_enriched = garmin_enrich_df_row(df, today)
    data_source = today_enriched["source"]  # "garmin" or "manual"

    df_neuro = df.copy()
    df_neuro["Date"] = pd.to_datetime(df_neuro["Date"], errors="coerce").dt.date
    df_neuro = df_neuro.sort_values("Date")

    cutoff = today - dt.timedelta(days=NEURO_WINDOW)
    recent_neuro = df_neuro[df_neuro["Date"] >= cutoff]

    def _last_wellness_col(frame, col):
        s = pd.to_numeric(frame.get(col, pd.Series(dtype=float)), errors="coerce")
        valid = s.dropna()
        return float(valid.iloc[-1]) if not valid.empty else None

    # Use today's enriched values if available, otherwise scan recent history
    sleep_last    = today_enriched["sleep"]    or _last_wellness_col(recent_neuro, "Sleep_1_5")
    fatigue_last  = today_enriched["fatigue"]  or _last_wellness_col(recent_neuro, "Fatigue_1_5")
    soreness_last = today_enriched["soreness"] or _last_wellness_col(recent_neuro, "Soreness_1_5")
    mood_last     = today_enriched["mood"]     or _last_wellness_col(recent_neuro, "Mood_1_5")

    if any(v is None for v in [sleep_last, fatigue_last, soreness_last, mood_last]):
        neuro_val = None
    else:
        neuro_val = calc_neuro_readiness(
            sleep=sleep_last,
            fatigue=fatigue_last,
            soreness=soreness_last,
            mood=mood_last,
            history_df=recent_neuro,
            span=3,
        )

        wellness_cols = ["Sleep_1_5", "Fatigue_1_5", "Mood_1_5", "Soreness_1_5"]
        present_cols = [c for c in wellness_cols if c in df_neuro.columns]
        df_neuro["_has_wellness"] = df_neuro[present_cols].apply(
            lambda row: any(pd.to_numeric(row, errors="coerce").gt(0).dropna()),
            axis=1,
        )
        logged_wellness_dates = df_neuro[df_neuro["_has_wellness"]]["Date"]

        if not logged_wellness_dates.empty:
            last_wellness_date = logged_wellness_dates.max()
            days_silent = (today - last_wellness_date).days
            if days_silent > 0:
                penalty = min(NEURO_DECAY_RATE * days_silent, NEURO_MAX_PENALTY)
                neuro_val = float(np.clip(neuro_val - penalty, 0, 100))

        neuro_val = float(np.clip(neuro_val, 0, 100))

    df_time = df.copy()
    df_time["Date"] = pd.to_datetime(df_time["Date"], errors="coerce")
    df_time = df_time.sort_values("Date").set_index("Date")

    full_range = pd.date_range(start=df_time.index.min(), end=today, freq="D")
    df_time = df_time.reindex(full_range)

    load_series = pd.to_numeric(df_time.get("Load"), errors="coerce")
    rpe_col     = "RPE_Post_Session" if "RPE_Post_Session" in df_time.columns else None
    rpe_series  = pd.to_numeric(
        df_time[rpe_col] if rpe_col else pd.Series(dtype=float),
        errors="coerce"
    )
    quality_series = pd.to_numeric(df_time.get("Session_1_5"), errors="coerce")

    readiness_val = calc_daily_readiness(
        load_series=load_series,
        rpe_series=rpe_series,
        quality_series=quality_series,
        span=7
    )

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

    _src_badge = " · via Garmin" if data_source == "garmin" else " · manual entry"

    neuro_ui = dial_flip(
        apple_neuromuscular_ring(neuro_val),
        " ",
        f"Neuromuscular Readiness reflects nervous system and movement state using fatigue, mood, sleep, and soreness. Lower scores indicate neuromuscular fatigue and reduced coordination.{_src_badge}"
    )

    readiness_ui = dial_flip(
        apple_readiness_ring(readiness_val),
        " ",
        f"Daily Readiness reflects how well you're coping with recent training. Combines load, post-session effort, and session quality, compared against your recent baseline. Lower scores suggest accumulated fatigue.{_src_badge}"
    )

    return today_date_str, weekly_ui, streak_ui, neuro_ui, readiness_ui, load_fig, wellness_fig, speed_fig


# ============================================================
#  ✅ UNIFIED on_day_click — handles calendar click, popup
#     close, and edit-session button all in one callback.
#     This avoids any duplicate Output conflicts.
# ============================================================

@app.callback(
    Output("session-log-modal", "is_open"),
    Output("popup-modal-title", "children"),
    Output("popup-modal-body", "children"),
    Output("session-input-container", "style"),
    Output("selected-date-store", "data"),
    Output("selected-date-header", "children"),
    Input({"type": "calendar-day", "date": ALL}, "n_clicks"),
    Input("session-log-popup-close", "n_clicks"),
    Input("session-log-popup-edit", "n_clicks"),
    State("athlete-dropdown", "value"),
    prevent_initial_call=True,
)
def on_day_click(n_clicks_list, close_n, edit_n, athlete_name):
    ctx = callback_context
    if not ctx.triggered:
        raise PreventUpdate

    triggered_id = ctx.triggered[0]["prop_id"].split(".")[0]

    # ── Close button ──
    if triggered_id == "session-log-popup-close":
        return False, no_update, no_update, no_update, no_update, no_update

    # ── Edit button: close modal, open session form ──
    if triggered_id == "session-log-popup-edit":
        return False, no_update, no_update, {"display": "block"}, no_update, no_update

    # ── Calendar day clicked ──
    if not n_clicks_list or all((n or 0) == 0 for n in n_clicks_list):
        raise PreventUpdate

    try:
        triggered = json.loads(triggered_id)
    except Exception:
        raise PreventUpdate

    if triggered.get("type") != "calendar-day":
        raise PreventUpdate

    clicked_date_str = triggered["date"]
    clicked_date = pd.to_datetime(clicked_date_str, errors="coerce").date()
    header = html.H5(f"Selected session: {clicked_date_str}")

    # No athlete selected — just open the form
    if not athlete_name:
        return False, no_update, no_update, {"display": "block"}, clicked_date_str, header

    df = load_tab(athlete_name)
    if df is None or df.empty or "Date" not in df.columns:
        return False, no_update, no_update, {"display": "block"}, clicked_date_str, header

    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date

    status = get_day_status(df, clicked_date)

    # ── Not yet logged → open the input form directly ──
    if not status.get("logged", False):
        return False, no_update, no_update, {"display": "block"}, clicked_date_str, header

    # ── Already logged → build and show the read-only popup ──
    match = df[df["Date"] == clicked_date]
    if match.empty:
        return False, no_update, no_update, {"display": "block"}, clicked_date_str, header

    row = match.iloc[-1]

    def v(col, default="—"):
        val = row.get(col, "")
        if pd.isna(val) or str(val).strip().lower() in ("", "nan", "none", "nil"):
            return default
        return str(val).strip()

    def num(col):
        val = pd.to_numeric(row.get(col, np.nan), errors="coerce")
        return int(val) if pd.notna(val) and val > 0 else None

    workout    = v("Workout")
    focus      = v("Focus")
    venue      = v("Venue")
    notes      = v("Athlete_Notes")
    sets       = v("Sets_Reps_Load")
    track      = v("Track_Reps_Times")
    ai1        = v("AI_Suggestion_1")
    ai2        = v("AI_Suggestion_2")

    sleep_v    = num("Sleep_1_5")
    fatigue_v  = num("Fatigue_1_5")
    mood_v     = num("Mood_1_5")
    soreness_v = num("Soreness_1_5")
    rpe_v      = num("RPE_Post_Session")
    quality_v  = num("Session_1_5")

    def metric_box(label, val):
        return html.Div([
            html.Div(label, style={"fontSize": "11px", "color": "#888", "marginBottom": "2px"}),
            html.Div(
                [str(val), html.Span("/5", style={"fontSize": "11px", "color": "#aaa"})],
                style={"fontSize": "20px", "fontWeight": "600"},
            ) if val is not None else html.Div("—", style={"fontSize": "16px", "color": "#ccc"}),
        ], style={"background": "#f5f5f5", "borderRadius": "8px", "padding": "8px 10px"})

    def section_label(text):
        return html.Div(text, style={
            "fontSize": "11px", "fontWeight": "600", "color": "#999",
            "textTransform": "uppercase", "letterSpacing": "0.05em",
            "margin": "14px 0 6px",
        })

    pill_style = {
        "display": "inline-block", "fontSize": "12px",
        "padding": "3px 10px", "borderRadius": "999px",
        "background": "#f0f0f0", "color": "#555",
        "marginRight": "6px", "marginBottom": "4px",
    }

    body = []

    # Workout / venue / focus strip
    body.append(html.Div([
        html.Span("Workout: ", style={"color": "#888", "fontSize": "13px"}),
        html.Span(workout, style={"fontWeight": "600", "fontSize": "13px"}),
        html.Span("  ·  Venue: ", style={"color": "#888", "fontSize": "13px"}),
        html.Span(venue, style={"fontSize": "13px"}),
    ], style={"marginBottom": "4px"}))

    body.append(html.Div([
        html.Span("Focus: ", style={"color": "#888", "fontSize": "13px"}),
        html.Span(focus, style={"fontSize": "13px"}),
    ], style={"marginBottom": "10px"}))

    body.append(html.Hr(style={"margin": "6px 0 4px 0"}))

    # Wellness grid
    body.append(section_label("Wellness"))
    body.append(html.Div([
        metric_box("Sleep",    sleep_v),
        metric_box("Fatigue",  fatigue_v),
        metric_box("Mood",     mood_v),
        metric_box("Soreness", soreness_v),
        metric_box("Post RPE", rpe_v),
        metric_box("Quality",  quality_v),
    ], style={
        "display": "grid",
        "gridTemplateColumns": "repeat(3, 1fr)",
        "gap": "8px",
    }))

    # Notes
    if notes != "—":
        body.append(section_label("Athlete notes"))
        body.append(html.Div(notes, style={
            "background": "#f5f5f5", "borderRadius": "8px",
            "padding": "10px 12px", "fontSize": "13px",
            "color": "#444", "lineHeight": "1.5",
        }))

    # Gym / Track pills
    gym_track = []
    if sets != "—":
        gym_track.append(html.Span(f"Gym: {sets}", style=pill_style))
    if track != "—":
        gym_track.append(html.Span(f"Track: {track}", style=pill_style))
    if gym_track:
        body.append(section_label("Gym / Track"))
        body.append(html.Div(gym_track))

    # AI feedback
    if ai1 != "—":
        body.append(section_label("Primary Coaching Feedback"))
        body.append(html.Div(ai1, style={
            "borderLeft": "3px solid #1565C0", "background": "#e3f2fd",
            "borderRadius": "0 8px 8px 0", "padding": "10px 12px",
            "fontSize": "12px", "color": "#0d47a1", "lineHeight": "1.5",
        }))
    if ai2 != "—":
        body.append(section_label("Secondary Coaching Feedback"))
        body.append(html.Div(ai2, style={
            "borderLeft": "3px solid #2E7D32", "background": "#e8f5e9",
            "borderRadius": "0 8px 8px 0", "padding": "10px 12px",
            "fontSize": "12px", "color": "#1b5e20", "lineHeight": "1.5",
        }))

    modal_title = html.Div([
        html.Span(
            clicked_date.strftime("%A, %d %B %Y"),
            style={"fontSize": "15px", "fontWeight": "600", "marginRight": "10px"},
        ),
        html.Span("Logged", style={
            "fontSize": "11px", "background": "#e8f5e9", "color": "#2E7D32",
            "padding": "2px 8px", "borderRadius": "999px", "fontWeight": "600",
        }),
    ])

    # Open modal, keep session form hidden (no_update keeps it as-is)
    return True, modal_title, html.Div(body), no_update, clicked_date_str, header


# ============================================================
#  Session context population (unchanged)
# ============================================================

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

    workout      = val("Workout")
    key_distance = val("Key_Distance")
    focus        = val("Focus")
    srpe         = val("sRPE")
    duration     = val("Duration")
    load         = val("Load")
    venue        = val("Venue")
    notes        = val("Notes")

    workout_card = [
        html.Div("🏃 Workout", className="ctx-title"),
        html.Div(workout or "—", className="ctx-main"),
        html.Div(f"📏 Key Distance: {key_distance}", className="ctx-sub") if key_distance else None,
    ]

    focus_card = [
        html.Div("🎯 Session Focus", className="ctx-title"),
        html.Div(focus or "—", className="ctx-main"),
        html.Div([
            html.Div(f"🔥 Planned sRPE: {srpe}") if srpe else None,
            html.Div(f"⏱ Duration: {duration} min") if duration else None,
            html.Div(f"⚖️ Load: {load}") if load else None,
        ], className="ctx-sub"),
    ]

    venue_card = [
        html.Div("📍 Venue & Notes", className="ctx-title"),
        html.Div(venue or "—", className="ctx-main"),
        html.Div(f"📝 {notes}", className="ctx-sub") if notes else None,
    ]

    return workout_card, focus_card, venue_card


# ============================================================
#  Save + AI callback
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
    n_clicks, athlete_name, selected_date,
    ai_mode_1, ai_mode_2,
    notes, sets_reps_load, track_reps_times,
    rpe, session_quality, sleep, fatigue, mood, soreness,
):
    if not n_clicks:
        raise PreventUpdate

    if not athlete_name:
        return no_update, no_update, "⚠️ Please select an athlete first."

    if not ai_mode_1 or not ai_mode_2:
        return no_update, no_update, "⚠️ Please select coaching feedback."

    if not selected_date:
        return no_update, no_update, "⚠️ Please select a date from the calendar first."

    rpe             = 3.0 if rpe is None else float(rpe)
    session_quality = 3.0 if session_quality is None else float(session_quality)
    sleep           = 3.0 if sleep is None else float(sleep)
    fatigue         = 3.0 if fatigue is None else float(fatigue)
    mood            = 3.0 if mood is None else float(mood)
    soreness        = 3.0 if soreness is None else float(soreness)

    notes            = (notes or "").strip()
    sets_reps_load   = (sets_reps_load or "").strip()
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

    athlete_email   = safe(df, row_idx, "Athlete_email") if "Athlete_email" in df.columns else ""
    athlete_display = safe(df, row_idx, "Athlete", athlete_name)
    date_display    = str(selected_date_dt)
    focus           = safe(df, row_idx, "Focus", "")
    venue           = safe(df, row_idx, "Venue", "")
    workout         = safe(df, row_idx, "Workout", "")

    status_msg = "✅ Saved, coaching feedback generated & email sent to Coach."

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
        status_msg = f"⚠️ Saved + coaching feedback generated, but email failed: {e}"

    ai1_div = html.Div(html.Div([
        html.Div("💡 Coaching Feedback 1", className="ai-title"),
        html.P(ai1),
    ], className="ai-card ai-card-green"))

    ai2_div = html.Div(html.Div([
        html.Div("💡 Coaching Feedback 2", className="ai-title"),
        html.P(ai2),
    ], className="ai-card ai-card-blue"))

    return ai1_div, ai2_div, html.Span(
        status_msg,
        style={"color": "#2E7D32" if status_msg.startswith("✅") else "#C62828", "fontWeight": 600}
    )


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
    return no_update, 3, 3, 3, 3, 3, 3, "", "", ""


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


@app.callback(
    Output("welcome-message", "children"),
    Input("athlete-dropdown", "value"),
    Input("today-date", "children"),
)
def update_welcome(athlete_id, _today):
    if not athlete_id:
        raise PreventUpdate

    first_name = athlete_id.strip().split()[0] if athlete_id.strip() else "Athlete"
    today = today_adl()
    hour = dt.datetime.now(ADL_TZ).hour

    greeting = (
        "Good morning" if hour < 12
        else "Good afternoon" if hour < 17
        else "Good evening"
    )

    readiness_val = None
    neuro_val = None
    streak = 0

    try:
        df = load_tab(athlete_id)

        if not df.empty:
            streak, _ = compute_streaks(df)

            df_time = df.copy()
            df_time["Date"] = pd.to_datetime(df_time["Date"], errors="coerce")
            df_time = df_time.sort_values("Date").set_index("Date")

            full_range = pd.date_range(start=df_time.index.min(), end=today, freq="D")
            df_time = df_time.reindex(full_range)

            rpe_col = "RPE_Post_Session" if "RPE_Post_Session" in df_time.columns else None
            rpe_series = pd.to_numeric(
                df_time[rpe_col] if rpe_col else pd.Series(dtype=float),
                errors="coerce"
            )
            load_series = pd.to_numeric(df_time.get("Load"), errors="coerce")
            quality_series = pd.to_numeric(df_time.get("Session_1_5"), errors="coerce")

            readiness_val = calc_daily_readiness(load_series, rpe_series, quality_series)

            df_neuro = df.copy()
            df_neuro["Date"] = pd.to_datetime(df_neuro["Date"], errors="coerce").dt.date
            df_neuro = df_neuro.sort_values("Date")

            cutoff = today - dt.timedelta(days=14)
            recent_neuro = df_neuro[df_neuro["Date"] >= cutoff]

            def _last(frame, col):
                s = pd.to_numeric(frame.get(col, pd.Series(dtype=float)), errors="coerce")
                v = s.dropna()
                return float(v.iloc[-1]) if not v.empty else None

            sl = _last(recent_neuro, "Sleep_1_5")
            fa = _last(recent_neuro, "Fatigue_1_5")
            so = _last(recent_neuro, "Soreness_1_5")
            mo = _last(recent_neuro, "Mood_1_5")

            if all(v is not None for v in [sl, fa, so, mo]):
                neuro_val = calc_neuro_readiness(sl, fa, so, mo, history_df=recent_neuro)

    except Exception:
        streak, readiness_val, neuro_val = 0, None, None

    r = readiness_val if readiness_val is not None else 0
    n = neuro_val if neuro_val is not None else 0

    if readiness_val is None and neuro_val is None:
        msg = "Log your first session to start tracking readiness."
        sub = "Your dials will activate once you've entered some data."
        color = "#6e6e6e"
        icon = "—"
    elif r >= 75 and n >= 75:
        msg = "You're in great shape today — go get it."
        sub = "Both readiness markers are high. This is a good day to push."
        color = "#2E7D32"
        icon = "↑"
    elif r >= 60 and n >= 60:
        msg = "Good to go — solid session ahead."
        sub = "Numbers look steady. Execute your plan and stay sharp."
        color = "#1565C0"
        icon = "→"
    elif r >= 40 or n >= 40:
        msg = "Manage your load carefully today."
        sub = "Some fatigue showing. Focus on quality over quantity."
        color = "#E65100"
        icon = "↓"
    else:
        msg = "Recovery day recommended."
        sub = "Both readiness markers are low. Prioritise recovery today."
        color = "#C62828"
        icon = "⚠"

    streak_txt = f" • {streak}-day streak 🔥" if streak >= 3 else ""

    return html.Div(
        [
            html.Div(
                [
                    html.Span(f"{icon} ", style={"fontSize": "18px", "fontWeight": 900, "color": color, "marginRight": "4px"}),
                    html.Span(f"{greeting}, {first_name}. {msg}", style={"fontWeight": 800, "fontSize": "16px", "color": color}),
                    html.Span(streak_txt, style={"fontSize": "13px", "color": "#E65100", "marginLeft": "6px"}),
                ],
                style={"marginBottom": "4px"},
            ),
            html.Div(sub, style={"fontSize": "13px", "color": "#6e6e6e", "lineHeight": "1.4"}),
        ],
        style={"maxWidth": "1000px", "margin": "10px auto 4px auto", "textAlign": "center", "padding": "0px", "background": "transparent", "border": "none"},
    )


@app.callback(
    Output("motivational-message", "children"),
    Input("today-date", "children"),
    State("athlete-dropdown", "value"),
    prevent_initial_call=True,
)
def update_motivational_message(today_date, athlete_id):
    if not athlete_id:
        raise PreventUpdate

    first_name = athlete_id.strip().split()[0] if athlete_id.strip() else "Athlete"

    try:
        df = load_tab(athlete_id)
        context = ""

        if not df.empty:
            streak, _ = compute_streaks(df)
            summary = build_context_summary(df, days=7)
            wellness = build_wellness_flags(df, days=7)

            df_time = df.copy()
            df_time["Date"] = pd.to_datetime(df_time["Date"], errors="coerce")
            df_time = df_time.sort_values("Date").set_index("Date")
            full_range = pd.date_range(start=df_time.index.min(), end=today_adl(), freq="D")
            df_time = df_time.reindex(full_range)
            rpe_col = "RPE_Post_Session" if "RPE_Post_Session" in df_time.columns else None
            rpe_series = pd.to_numeric(
                df_time[rpe_col] if rpe_col else pd.Series(dtype=float),
                errors="coerce"
            )
            load_series = pd.to_numeric(df_time.get("Load"), errors="coerce")
            quality_series = pd.to_numeric(df_time.get("Session_1_5"), errors="coerce")
            readiness_val = calc_daily_readiness(load_series, rpe_series, quality_series)

            context = (
                f"Athlete first name: {first_name}\n"
                f"Current streak: {streak} days\n"
                f"7-day summary: {summary}\n"
                f"Wellness scan: {wellness}\n"
                f"Daily readiness score: {round(readiness_val, 1) if readiness_val else 'unknown'}/100\n"
                f"Day of week: {dt.datetime.now(ADL_TZ).strftime('%A')}\n"
            )
        else:
            context = f"Athlete first name: {first_name}\nNo training data logged yet.\n"

    except Exception:
        context = f"Athlete first name: {first_name}\n"

    system_msg = (
        "You are a high-performance sprint and strength coach writing a single motivational line "
        "for an athlete when they open their training app. "
        "Rules:\n"
        "- 1 sentence ONLY. Never more.\n"
        "- Address the athlete by first name.\n"
        "- Focus on PROCESS, IDENTITY, or CONSISTENCY — NOT on today's readiness score.\n"
        "- Draw from streak length, day of week, training history, or upcoming sessions.\n"
        "- Tone: like a coach who has worked with this athlete for years. Sharp, personal, not generic.\n"
        "- Rotate between themes: competitive identity, process discipline, long-term vision, quiet confidence.\n"
        "- No hashtags. No emojis. No exclamation marks.\n"
        "- Never mention readiness, fatigue scores, or wellness numbers — the banner above already covers that.\n"
        "- Never start with 'It's [day]' — that's too predictable.\n"
    )

    user_msg = f"Athlete context:\n{context}\n\nWrite the motivational message now. 1–2 sentences only."

    raw = call_openai_chat(
        [{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}],
        max_tokens=80,
    )

    if not raw or "unavailable" in raw.lower():
        import random
        fallbacks = [
            f"{first_name}, the work you put in today compounds into who you become.",
            f"Every session is data, {first_name}. Make this one count.",
            f"{first_name}, consistency beats intensity. Show up.",
            f"The best athletes in the world trained today, {first_name}. So will you.",
            f"{first_name}, trust the process — the numbers don't lie.",
        ]
        raw = random.choice(fallbacks)

    return html.Div(
        [html.Span(raw.strip(), style={"fontSize": "15px", "fontStyle": "normal", "color": "#424242", "fontWeight": 500, "lineHeight": "1.4", "opacity": 0.85})],
        style={"textAlign": "center", "padding": "4px 0", "marginTop": "4px", "marginBottom": "8px"}
    )


@app.callback(
    Output("ai-plan-output", "children"),
    Output("ai-plan-status", "children"),
    Input("btn-generate-plan", "n_clicks"),
    State("athlete-dropdown", "value"),
    State("ai-plan-coach", "value"),
    State("ai-plan-goal", "value"),
    State("ai-plan-duration", "value"),
    prevent_initial_call=True,
)
def generate_session_plan(n_clicks, athlete_id, coach_style, goal, duration):
    if not n_clicks:
        raise PreventUpdate

    if not coach_style:
        return no_update, "⚠️ Please select a coaching focus."
    if not goal or not goal.strip():
        return no_update, "⚠️ Please enter a session goal."

    duration = duration or 45
    persona = persona_prompt(coach_style)

    context_block = ""
    if athlete_id:
        try:
            df = load_tab(athlete_id)
            if not df.empty:
                summary = build_context_summary(df, days=7)
                wellness = build_wellness_flags(df, days=7)
                upcoming = build_upcoming_context(df, today_adl(), n=3)
                context_block = (
                    f"Athlete context (last 7 days): {summary}\n"
                    f"Wellness scan: {wellness}\n"
                    f"Upcoming sessions: {upcoming}\n\n"
                )
        except Exception:
            pass

    COACH_STRUCTURE = {
        "Acceleration & Speed Coach": (
            "Structure: Warm-Up (CNS activation, A-drills, wickets), Primary (max velocity or acceleration reps — "
            "specify exact distances e.g. 3×30m fly, rest periods, surface cues), Secondary (speed endurance or "
            "plyometrics — box jumps, bounds, hurdle hops with contact time cues), Cool-Down (parasympathetic reset). "
            "Every block must include exact rep counts, distances, rest periods, and at least one technical cue per block."
        ),
        "Tempo & Endurance Coach": (
            "Structure: Warm-Up (aerobic activation, dynamic mobility), Primary (tempo runs — specify distances, "
            "target % effort e.g. 10×100m @70%, rest:work ratio), Secondary (aerobic volume or lactate threshold work "
            "with pace guidance), Cool-Down (active recovery, breathing). "
            "Specify exact volumes, pacing targets, and rhythm cues."
        ),
        "Technical Sprint Coach": (
            "Structure: Warm-Up (posture drills, arm mechanics, tall running), Primary (technical drill series — "
            "A-skip, B-skip, wicket runs, wall drills — with specific coaching cues on projection angle, shin angle, "
            "arm drive), Secondary (short acceleration runs applying the technical focus — e.g. 4×20m), "
            "Cool-Down (movement review, feedback). Each block must name the specific technical fault being addressed "
            "and the corrective cue."
        ),
        "Strength & Power Coach": (
            "Structure: Warm-Up (potentiation — glute activation, bar warm-up sets), Primary (main lift — specify "
            "exercise, sets×reps×%1RM or RPE, rest, bar speed cue), Secondary (supplementary — jumps, pulls, or "
            "accessory lifts with sets×reps), Tertiary (single-leg or posterior chain accessory), "
            "Cool-Down (tissue work, breathing). Must include exact loading parameters and bar speed or RPE targets."
        ),
        "Recovery & Readiness Coach": (
            "Structure: Warm-Up (gentle mobilisation — hip 90/90, thoracic rotation), Primary (low-CNS aerobic "
            "work — e.g. 20min easy bike, pool walk, or breath-work circuit at <65% HRmax), Secondary (parasympathetic "
            "activation — foam roll, contrast breathing, progressive muscle relaxation), Cool-Down (sleep hygiene cue, "
            "nutrition timing reminder). Flag any wellness markers from context that influenced session design."
        ),
    }

    coach_structure = COACH_STRUCTURE.get(coach_style, (
        "Structure the session with Warm-Up, Primary, Secondary, and Cool-Down blocks. "
        "Be specific: include exact sets, reps, distances, rest periods and coaching cues."
    ))

    system_msg = (
        f"{persona}\n\n"
        f"STRUCTURAL REQUIREMENTS FOR THIS SESSION TYPE:\n{coach_structure}\n\n"
        "Output ONLY a JSON object — no markdown, no preamble, no explanation:\n"
        '{"blocks": ['
        '{"title": "Warm-Up", "duration_min": 10, "details": "..."},'
        '{"title": "Primary", "duration_min": 20, "details": "..."},'
        '{"title": "Secondary", "duration_min": 10, "details": "..."},'
        '{"title": "Tertiary", "duration_min": 8, "details": "..."},'
        '{"title": "Cool-Down", "duration_min": 5, "details": "..."}'
        "]}\n\n"
        "Rules: Always include all 5 blocks. Every 'details' field must be dense with specifics. No generic filler."
    )

    user_msg = (
        f"{context_block}"
        f"Session goal: {goal.strip()}\n"
        f"Approx duration: {duration} minutes\n"
        f"Coach style: {coach_style}\n\n"
        "Build the session plan now. Return only valid JSON."
    )

    raw = call_openai_chat(
        [{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}],
        max_tokens=900,
    )

    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean)
        blocks = data.get("blocks", [])
    except Exception:
        return html.Div([
            html.Div("Session Plan", className="fw-semibold mb-2"),
            html.Pre(raw, style={"whiteSpace": "pre-wrap", "fontSize": "13px"}),
        ]), ""

    if not blocks:
        return html.Div("No plan generated. Try adjusting your goal.", className="text-muted"), ""

    BLOCK_COLORS = {
        "Warm-Up":   ("#e3f2fd", "#1565C0"),
        "Primary":   ("#e8f5e9", "#2E7D32"),
        "Secondary": ("#fff3e0", "#E65100"),
        "Tertiary":  ("#f3e5f5", "#6A1B9A"),
        "Cool-Down": ("#fce4ec", "#880E4F"),
    }

    cards = []
    for b in blocks:
        title = b.get("title", "Block")
        dur = b.get("duration_min", "")
        details = b.get("details", "")
        bg, accent = BLOCK_COLORS.get(title, ("#f5f5f5", "#333"))

        cards.append(html.Div(
            [
                html.Div([
                    html.Span(title, style={"fontWeight": 800, "fontSize": "14px", "color": accent}),
                    html.Span(f"~{dur} min", style={"fontSize": "12px", "color": accent, "opacity": "0.75", "marginLeft": "8px", "fontWeight": 600}) if dur else None,
                ], style={"marginBottom": "6px"}),
                html.Div(details, style={"fontSize": "13px", "lineHeight": "1.5", "color": "#1a1a1a"}),
            ],
            style={
                "background": bg,
                "border": f"1px solid {accent}33",
                "borderLeft": f"4px solid {accent}",
                "borderRadius": "10px",
                "padding": "14px 16px",
                "marginBottom": "10px",
            }
        ))

    total = sum(b.get("duration_min", 0) for b in blocks)
    cards.append(html.Div(
        f"Total: ~{total} min  •  {coach_style}",
        style={"fontSize": "12px", "color": "#666", "textAlign": "right", "marginTop": "4px"},
    ))

    return html.Div(cards), ""



@app.callback(
    Output("share-card-modal", "is_open"),
    Output("share-card-container", "children"),
    Input("btn-share-card", "n_clicks"),
    State("athlete-dropdown", "value"),
    State("share-card-modal", "is_open"),
    prevent_initial_call=True,
)
def show_share_card(n, athlete_id, is_open):
    if not n:
        raise PreventUpdate
    if is_open:
        return False, no_update

    today      = today_adl()
    date_str   = today.strftime("%d %b %Y")
    first_name = (athlete_id or "Athlete").strip().split()[0]

    readiness_val = 0
    neuro_val     = 0
    streak        = 0
    weekly_pct    = 0
    rpe_v = load_v = acwr_v = None
    sleep_v = fatigue_v = mood_v = soreness_v = None
    session_label = "Training day"
    session_sub   = ""

    try:
        df = load_tab(athlete_id)
        if not df.empty:
            streak, _ = compute_streaks(df)

            dow = today.weekday()
            days_since_sat = (dow - 5) % 7
            week_start = today - dt.timedelta(days=days_since_sat)
            week_end   = week_start + dt.timedelta(days=6)
            planned    = count_planned_sessions_in_week(df, week_start, week_end)
            logged_n   = count_logged_sessions_in_week(df, week_start, week_end)
            weekly_pct = int(round(logged_n / planned * 100)) if planned > 0 else 0

            dft = df.copy()
            dft["Date"] = pd.to_datetime(dft["Date"], errors="coerce")
            dft = dft.sort_values("Date").set_index("Date")
            dft = dft.reindex(pd.date_range(dft.index.min(), today, freq="D"))

            load_series    = pd.to_numeric(dft.get("Load"), errors="coerce")
            rpe_col        = "RPE_Post_Session" if "RPE_Post_Session" in dft.columns else None
            rpe_series     = pd.to_numeric(dft[rpe_col] if rpe_col else pd.Series(dtype=float), errors="coerce")
            quality_series = pd.to_numeric(dft.get("Session_1_5"), errors="coerce")
            readiness_val  = calc_daily_readiness(load_series, rpe_series, quality_series) or 0

            df_neuro     = df.copy()
            df_neuro["Date"] = pd.to_datetime(df_neuro["Date"], errors="coerce").dt.date
            recent_neuro = df_neuro[df_neuro["Date"] >= today - dt.timedelta(days=14)]

            def _last(col):
                s = pd.to_numeric(recent_neuro.get(col, pd.Series(dtype=float)), errors="coerce").dropna()
                return float(s.iloc[-1]) if not s.empty else None

            sl = _last("Sleep_1_5")
            fa = _last("Fatigue_1_5")
            so = _last("Soreness_1_5")
            mo = _last("Mood_1_5")

            if all(v is not None for v in [sl, fa, so, mo]):
                neuro_val  = calc_neuro_readiness(sl, fa, so, mo, history_df=recent_neuro) or 0
                sleep_v    = int(sl)
                fatigue_v  = int(fa)
                soreness_v = int(so)
                mood_v     = int(mo)

            df2 = df.copy()
            df2["Date"] = pd.to_datetime(df2["Date"], errors="coerce").dt.date
            today_row = df2[df2["Date"] == today]
            if not today_row.empty:
                r = today_row.iloc[-1]
                rpe_raw  = pd.to_numeric(r.get("RPE_Post_Session"), errors="coerce")
                load_raw = pd.to_numeric(r.get("Load"), errors="coerce")
                rpe_v    = rpe_raw  if pd.notna(rpe_raw)  else None
                load_v   = load_raw if pd.notna(load_raw) else None
                session_label = str(r.get("Workout") or "Training day")
                focus = str(r.get("Focus") or "")
                venue = str(r.get("Venue") or "")
                session_sub = " · ".join(
                    x for x in [venue, focus]
                    if x and x.lower() not in ("nan", "none", "nil", "")
                )

            load_clean = load_series.dropna()
            if len(load_clean) >= 28:
                acwr_v = round(float(
                    load_clean.ewm(span=7,  adjust=False).mean().iloc[-1] /
                    load_clean.ewm(span=28, adjust=False).mean().iloc[-1]
                ), 2)

    except Exception as e:
        print("Share card error:", e)

    def safe_num(v, decimals=0):
        try:
            f = float(v)
            if pd.isna(f):
                return "—"
            return str(round(f, decimals)) if decimals else str(int(round(f)))
        except Exception:
            return "—"

    circ = 131.9

    def ro(pct):
        return round(circ * (1 - min(max(pct, 0), 100) / 100), 1)

    d_r   = int(round(min(max(readiness_val or 0, 0), 100)))
    d_n   = int(round(min(max(neuro_val     or 0, 0), 100)))
    d_e   = int(round(min(max(weekly_pct    or 0, 0), 100)))
    d_sp  = int(round(min((streak / 31) * 100, 100)))
    d_sn  = streak

    # Pre-compute stroke-dashoffset values for SVG dials
    ro_r  = round(circ * (1 - min(max(d_r,  0), 100) / 100), 1)
    ro_n  = round(circ * (1 - min(max(d_n,  0), 100) / 100), 1)
    ro_e  = round(circ * (1 - min(max(d_e,  0), 100) / 100), 1)
    ro_sp = round(circ * (1 - min(max(d_sp, 0), 100) / 100), 1)

    # Share card: fixed colours per dial position, streak = pink
    # These are intentionally different from the app dials which are score-dynamic
    c_r  = "#1E88E5"   # readiness  — always blue
    c_n  = "#43A047"   # neuro      — always green
    c_e  = "#FB8C00"   # exposure   — always orange
    c_sp = "#E91E8C"   # streak     — hot pink
    d_rpe = (safe_num(rpe_v) + "/5") if rpe_v is not None else "—"
    d_ld  = safe_num(load_v)
    d_aw  = safe_num(acwr_v, 2)
    d_slp = safe_num(sleep_v)
    d_fat = safe_num(fatigue_v)
    d_mo  = safe_num(mood_v)
    d_sor = safe_num(soreness_v)
    p_slp = int(round((sleep_v    or 0) / 5 * 100)) if sleep_v    is not None else 0
    p_fat = int(round((fatigue_v  or 0) / 5 * 100)) if fatigue_v  is not None else 0
    p_mo  = int(round((mood_v     or 0) / 5 * 100)) if mood_v     is not None else 0
    p_sor = int(round((soreness_v or 0) / 5 * 100)) if soreness_v is not None else 0
    dl_name = date_str.replace(" ", "-")
    s_lbl = session_label[:32]
    s_sub = (session_sub[:44] or "Training day")


    # ── get motivational quote for share card ────────────────
    try:
        mot_sys = (
            "You are a high-performance sprint and strength coach. "
            "Write ONE sentence for an athlete's shareable training card. "
            "Rules:\n"
            "- Max 14 words.\n"
            "- Address the athlete by first name.\n"
            "- Blend something concrete (streak, readiness, sessions completed) with one sharp image or phrase that lands emotionally.\n"
            "- Rotate between styles: sometimes data-led ('10 days, 78 readiness — the track is yours'), sometimes identity-led ('Dylan, this is what consistent looks like'), sometimes forward-looking ('the work compounds, Dylan — keep going').\n"
            "- BANNED words: greatness, dedication, potential, journey, warrior, beast, grind, hustle, amazing, incredible, champion.\n"
            "- No hashtags. No exclamation marks. Never generic fitness-brand filler.\n"
            "- Tone: sharp, personal — like a coach texting an athlete they know well."
        )
        mot_usr = (
            f"Athlete: {first_name}. "
            f"Readiness: {d_r}/100. "
            f"Streak: {d_sn} consecutive days. "
            f"Date: {date_str}. "
            f"Exposure (sessions completed this week): {d_e}%."
        )
        mot_quote = call_openai_chat(
            [{"role": "system", "content": mot_sys}, {"role": "user", "content": mot_usr}],
            max_tokens=40,
        )
        if not mot_quote or "unavailable" in mot_quote.lower():
            mot_quote = f"Every session builds the athlete you're becoming, {first_name}."
    except Exception:
        mot_quote = f"Every session builds the athlete you're becoming, {first_name}."




    html_src = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#111;font-family:system-ui,sans-serif;display:flex;flex-direction:column;align-items:center;padding:12px}}
#preview{{
  width:100%;max-width:320px;
  aspect-ratio:9/16;
  border-radius:18px;overflow:hidden;
  position:relative;background:#111;
  border:1px solid rgba(255,255,255,0.1);
}}
#bgCanvas{{
  position:absolute;inset:0;
  width:100%;height:100%;
  display:block;
}}
#scrim{{
  position:absolute;inset:0;
  background:linear-gradient(to bottom,rgba(0,0,0,0.12) 0%,rgba(0,0,0,0.04) 30%,rgba(0,0,0,0.55) 58%,rgba(0,0,0,0.82) 100%);
}}
#card-overlay{{
  position:absolute;bottom:0;left:0;right:0;
  padding:16px 18px 22px;
}}
.topbar{{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}}
.brand{{font-size:8px;letter-spacing:.16em;color:rgba(255,255,255,.55);text-transform:uppercase}}
.dials{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:14px}}
.dial-item{{display:flex;flex-direction:column;align-items:center;gap:4px}}
.dial-lbl{{font-size:7px;letter-spacing:.05em;text-transform:uppercase;color:rgba(255,255,255,.5);text-align:center}}
.divider{{height:1px;background:rgba(255,255,255,.15);margin:0 0 12px}}
.quote{{font-size:12px;font-style:italic;color:rgba(255,255,255,.9);line-height:1.5;text-align:center;padding:0 4px;margin-bottom:14px;text-shadow:0 1px 4px rgba(0,0,0,0.5)}}
.footer{{display:flex;justify-content:flex-start;align-items:center}}
.footer-date{{font-size:9px;color:rgba(255,255,255,.32)}}
#controls{{width:100%;max-width:320px;margin-top:10px;display:flex;flex-direction:column;gap:7px}}
#photolabel{{display:flex;align-items:center;justify-content:center;gap:7px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.14);border-radius:10px;padding:10px;cursor:pointer;color:rgba(255,255,255,.65);font-size:11px}}
#photolabel:hover{{background:rgba(255,255,255,.11)}}
#photoInput{{display:none}}
#dlBtn{{background:#1E88E5;border:none;border-radius:10px;padding:11px;color:#fff;font-size:12px;font-weight:600;cursor:pointer;width:100%}}
#dlBtn:hover{{background:#1565C0}}
#hint{{text-align:center;font-size:9px;color:rgba(255,255,255,.3);margin-top:2px}}
</style></head><body>

<div id="preview">
  <canvas id="bgCanvas"></canvas>
  <div id="scrim"></div>
  <div id="card-overlay">

    <div class="topbar">
      <span class="brand">ACI &middot; Adaptive Coaching</span>
    </div>

    <div class="dials">
      <div class="dial-item">
        <svg width="52" height="52" viewBox="0 0 52 52">
          <circle cx="26" cy="26" r="21" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="4"/>
          <circle cx="26" cy="26" r="21" fill="none" stroke="{c_r}" stroke-width="4"
            stroke-linecap="round" stroke-dasharray="{circ}" stroke-dashoffset="{ro_r}"
            transform="rotate(-90 26 26)"/>
          <text x="26" y="30" text-anchor="middle" font-size="13" font-weight="700" fill="white" font-family="system-ui">{d_r}</text>
        </svg>
        <div class="dial-lbl">Readiness</div>
      </div>
      <div class="dial-item">
        <svg width="52" height="52" viewBox="0 0 52 52">
          <circle cx="26" cy="26" r="21" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="4"/>
          <circle cx="26" cy="26" r="21" fill="none" stroke="{c_n}" stroke-width="4"
            stroke-linecap="round" stroke-dasharray="{circ}" stroke-dashoffset="{ro_n}"
            transform="rotate(-90 26 26)"/>
          <text x="26" y="30" text-anchor="middle" font-size="13" font-weight="700" fill="white" font-family="system-ui">{d_n}</text>
        </svg>
        <div class="dial-lbl">Neuro</div>
      </div>
      <div class="dial-item">
        <svg width="52" height="52" viewBox="0 0 52 52">
          <circle cx="26" cy="26" r="21" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="4"/>
          <circle cx="26" cy="26" r="21" fill="none" stroke="{c_e}" stroke-width="4"
            stroke-linecap="round" stroke-dasharray="{circ}" stroke-dashoffset="{ro_e}"
            transform="rotate(-90 26 26)"/>
          <text x="26" y="30" text-anchor="middle" font-size="13" font-weight="700" fill="white" font-family="system-ui">{d_e}</text>
        </svg>
        <div class="dial-lbl">Exposure</div>
      </div>
      <div class="dial-item">
        <svg width="52" height="52" viewBox="0 0 52 52">
          <circle cx="26" cy="26" r="21" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="4"/>
          <circle cx="26" cy="26" r="21" fill="none" stroke="{c_sp}" stroke-width="4"
            stroke-linecap="round" stroke-dasharray="{circ}" stroke-dashoffset="{ro_sp}"
            transform="rotate(-90 26 26)"/>
          <text x="26" y="30" text-anchor="middle" font-size="13" font-weight="700" fill="white" font-family="system-ui">{d_sn}</text>
        </svg>
        <div class="dial-lbl">Streak</div>
      </div>
    </div>

    <div class="divider"></div>
    <div class="quote">&ldquo;{mot_quote}&rdquo;</div>

    <div class="footer">
      <div class="footer-date">{date_str}</div>
    </div>

  </div>
</div>

<div id="controls">
  <label id="photolabel" for="photoInput">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/>
      <polyline points="21 15 16 10 5 21"/>
    </svg>
    Choose a background photo
  </label>
  <input type="file" id="photoInput" accept="image/*">
  <button id="dlBtn">Download story (1080&times;1920)</button>
  <div id="hint">Full phone story size &mdash; ready for Instagram, Strava or WhatsApp</div>
</div>

<script>
  const EXPORT_W = 1080;
  const EXPORT_H = 1920;
  const previewEl = document.getElementById('preview');
  const canvasEl  = document.getElementById('bgCanvas');
  let userImage   = null;

  function drawPreviewBg() {{
    const w = previewEl.offsetWidth;
    const h = previewEl.offsetHeight;
    canvasEl.width  = w;
    canvasEl.height = h;
    const ctx = canvasEl.getContext('2d');
    ctx.clearRect(0, 0, w, h);
    if (userImage) {{
      const scale = Math.max(w / userImage.width, h / userImage.height);
      const dw = userImage.width * scale;
      const dh = userImage.height * scale;
      ctx.drawImage(userImage, (w - dw) / 2, (h - dh) / 2, dw, dh);
    }} else {{
      const g = ctx.createLinearGradient(0, 0, w, h);
      g.addColorStop(0,   '#0f2027');
      g.addColorStop(0.5, '#203a43');
      g.addColorStop(1,   '#2c5364');
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
    }}
  }}

  window.addEventListener('load', drawPreviewBg);
  window.addEventListener('resize', drawPreviewBg);

  document.getElementById('photoInput').addEventListener('change', function(e) {{
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = function(ev) {{
      const img = new Image();
      img.onload = function() {{
        userImage = img;
        drawPreviewBg();
      }};
      img.src = ev.target.result;
    }};
    reader.readAsDataURL(file);
    document.getElementById('photolabel').innerHTML =
      '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      '<rect x="3" y="3" width="18" height="18" rx="3"/>' +
      '<circle cx="8.5" cy="8.5" r="1.5"/>' +
      '<polyline points="21 15 16 10 5 21"/></svg> Change photo';
  }});

  document.getElementById('dlBtn').addEventListener('click', function() {{
    const btn = this;
    btn.textContent = 'Generating\u2026';
    btn.disabled = true;

    const out = document.createElement('canvas');
    out.width  = EXPORT_W;
    out.height = EXPORT_H;
    const ctx  = out.getContext('2d');

    // 1. Background photo or gradient
    if (userImage) {{
      const scale = Math.max(EXPORT_W / userImage.width, EXPORT_H / userImage.height);
      const dw = userImage.width * scale;
      const dh = userImage.height * scale;
      ctx.drawImage(userImage, (EXPORT_W - dw) / 2, (EXPORT_H - dh) / 2, dw, dh);
    }} else {{
      const g = ctx.createLinearGradient(0, 0, EXPORT_W, EXPORT_H);
      g.addColorStop(0,   '#0f2027');
      g.addColorStop(0.5, '#203a43');
      g.addColorStop(1,   '#2c5364');
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, EXPORT_W, EXPORT_H);
    }}

    // 2. Scrim
    const scrim = ctx.createLinearGradient(0, 0, 0, EXPORT_H);
    scrim.addColorStop(0,    'rgba(0,0,0,0.12)');
    scrim.addColorStop(0.30, 'rgba(0,0,0,0.04)');
    scrim.addColorStop(0.58, 'rgba(0,0,0,0.55)');
    scrim.addColorStop(1.0,  'rgba(0,0,0,0.82)');
    ctx.fillStyle = scrim;
    ctx.fillRect(0, 0, EXPORT_W, EXPORT_H);

    const PAD      = 80;
    const CARD_TOP = EXPORT_H * 0.52;

    // 3. Brand text top-left
    ctx.font         = '26px system-ui';
    ctx.fillStyle    = 'rgba(255,255,255,0.55)';
    ctx.textAlign    = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.fillText('ACI \u00b7 ADAPTIVE COACHING', PAD, 92);

    // 4. Four dials
    const DIAL_R  = 90;
    const DIAL_SW = 18;
    const DIAL_Y  = CARD_TOP + 70;
    const dialSpacing = (EXPORT_W - PAD * 2) / 4;

    const dialData = [
      {{ val: {d_r},  pct: {d_r},  color: '{c_r}',  label: 'READINESS' }},
      {{ val: {d_n},  pct: {d_n},  color: '{c_n}',  label: 'NEURO' }},
      {{ val: {d_e},  pct: {d_e},  color: '{c_e}',  label: 'EXPOSURE' }},
      {{ val: {d_sn}, pct: {d_sp}, color: '{c_sp}', label: 'STREAK' }},
    ];

    dialData.forEach(function(d, i) {{
      const cx = PAD + dialSpacing * i + dialSpacing / 2;
      const cy = DIAL_Y;

      // Track
      ctx.beginPath();
      ctx.arc(cx, cy, DIAL_R, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(255,255,255,0.12)';
      ctx.lineWidth   = DIAL_SW;
      ctx.lineCap     = 'butt';
      ctx.stroke();

      // Arc
      const pct   = Math.min(Math.max(d.pct, 0), 100) / 100;
      const start = -Math.PI / 2;
      const end   = start + pct * Math.PI * 2;
      ctx.beginPath();
      ctx.arc(cx, cy, DIAL_R, start, end);
      ctx.strokeStyle = d.color;
      ctx.lineWidth   = DIAL_SW;
      ctx.lineCap     = 'round';
      ctx.stroke();
      ctx.lineCap     = 'butt';

      // Number
      ctx.font         = 'bold 60px system-ui';
      ctx.fillStyle    = '#ffffff';
      ctx.textAlign    = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(String(d.val), cx, cy);

      // Label
      ctx.font         = '21px system-ui';
      ctx.fillStyle    = 'rgba(255,255,255,0.5)';
      ctx.textAlign    = 'center';
      ctx.textBaseline = 'alphabetic';
      ctx.fillText(d.label, cx, cy + DIAL_R + 40);
    }});

    // 5. Divider
    const divY = DIAL_Y + DIAL_R + 80;
    ctx.beginPath();
    ctx.moveTo(PAD, divY);
    ctx.lineTo(EXPORT_W - PAD, divY);
    ctx.strokeStyle = 'rgba(255,255,255,0.18)';
    ctx.lineWidth   = 2;
    ctx.stroke();

    // 6. Quote
    const quoteText = '\u201c{mot_quote}\u201d';
    const quoteY    = divY + 72;
    const maxWidth  = EXPORT_W - PAD * 2;
    ctx.font         = 'italic 44px system-ui,sans-serif';
    ctx.fillStyle    = 'rgba(255,255,255,0.90)';
    ctx.textAlign    = 'center';
    ctx.textBaseline = 'alphabetic';
    wrapText(ctx, quoteText, EXPORT_W / 2, quoteY, maxWidth, 64);

    // 7. Footer date
    const footY = EXPORT_H - 80;
    ctx.font         = '26px system-ui';
    ctx.fillStyle    = 'rgba(255,255,255,0.30)';
    ctx.textAlign    = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.fillText('{date_str}', PAD, footY);

    // Export
    const a = document.createElement('a');
    a.download = 'aci-{dl_name}.png';
    a.href = out.toDataURL('image/png');
    a.click();
    btn.textContent = 'Download story (1080\u00d71920)';
    btn.disabled = false;
  }});

  function wrapText(ctx, text, x, y, maxWidth, lineHeight) {{
    const words = text.split(' ');
    let line = '';
    let cy   = y;
    for (let i = 0; i < words.length; i++) {{
      const test = line + words[i] + ' ';
      if (ctx.measureText(test).width > maxWidth && i > 0) {{
        ctx.fillText(line.trim(), x, cy);
        line = words[i] + ' ';
        cy  += lineHeight;
      }} else {{
        line = test;
      }}
    }}
    ctx.fillText(line.trim(), x, cy);
  }}
</script>
</body></html>"""

    widget = html.Iframe(
        srcDoc=html_src,
        style={
            "width": "100%",
            "height": "720px",
            "border": "none",
            "background": "#111",
        },
    )

    return True, widget


@app.callback(
    Output("garmin-status-badge", "children"),
    Input("athlete-dropdown", "value"),
    prevent_initial_call=True,
)
def update_garmin_badge(athlete_id):
    if not athlete_id:
        raise PreventUpdate

    try:
        df = load_tab(athlete_id)
        token, _ = garmin_get_athlete_tokens(df)
        garmin_linked = bool(token)
    except Exception:
        garmin_linked = False

    if garmin_linked:
        badge = html.Div(
            [
                html.Span(
                    "⌚ Garmin connected",
                    style={
                        "fontSize": "11px",
                        "background": "#e8f5e9",
                        "color": "#2E7D32",
                        "padding": "3px 10px",
                        "borderRadius": "20px",
                        "fontWeight": "600",
                        "border": "1px solid #a5d6a7",
                    }
                ),
                html.Span(
                    " · dials powered by device data",
                    style={"fontSize": "11px", "color": "#888", "marginLeft": "4px"},
                ),
            ],
            style={"textAlign": "center", "marginTop": "4px"},
        )
    else:
        badge = html.Div(
            [
                html.A(
                    "⌚ Connect Garmin",
                    href=f"/garmin/connect?athlete={athlete_id}",
                    target="_blank",
                    style={
                        "fontSize": "11px",
                        "color": "#1565C0",
                        "textDecoration": "none",
                        "background": "#e3f2fd",
                        "padding": "3px 10px",
                        "borderRadius": "20px",
                        "border": "1px solid #90caf9",
                        "fontWeight": "500",
                    }
                ),
                html.Span(
                    " · optional — dials will use device data",
                    style={"fontSize": "11px", "color": "#aaa", "marginLeft": "4px"},
                ),
            ],
            style={"textAlign": "center", "marginTop": "4px"},
        )

    return badge


# ============================================================
#  Garmin OAuth + Push webhook routes (Flask)
# ============================================================

# Temp store for OAuth secrets during handshake
_garmin_temp_secrets = {}


@server.route("/garmin/connect")
def garmin_connect():
    """
    Send athlete to this URL to link their Garmin account.
    e.g. https://yourapp.com/garmin/connect?athlete=Dylan+Hicks
    """
    if not GARMIN_ENABLED:
        return "Garmin integration not configured (missing API keys).", 503

    athlete_id = flask_request.args.get("athlete", "unknown")
    try:
        token, secret = garmin_get_request_token()
        _garmin_temp_secrets[athlete_id] = (token, secret)
        auth_url = f"https://connect.garmin.com/oauthConfirm?oauth_token={token}&state={athlete_id}"
        return redirect(auth_url)
    except Exception as e:
        return f"Error starting Garmin OAuth: {e}", 500


@server.route("/garmin/callback")
def garmin_callback():
    """
    Garmin redirects here after the athlete approves access.
    Exchanges the verifier for permanent access tokens and
    stores them in the athlete's Google Sheet.
    """
    oauth_token    = flask_request.args.get("oauth_token", "")
    oauth_verifier = flask_request.args.get("oauth_verifier", "")
    athlete_id     = flask_request.args.get("state", "unknown")

    temp = _garmin_temp_secrets.get(athlete_id)
    if not temp:
        return "Session expired. Please try linking again.", 400

    req_token, req_secret = temp

    try:
        user_token, user_secret = garmin_get_access_token(
            req_token, req_secret, oauth_verifier
        )
    except Exception as e:
        return f"OAuth error: {e}", 500

    # Store tokens in row 0 of the athlete sheet
    try:
        df = load_tab(athlete_id)
        if not df.empty:
            write_row(athlete_id, 0, {
                "Garmin_Token":  user_token,
                "Garmin_Secret": user_secret,
            })
            print(f"✅ Garmin tokens stored for {athlete_id}")
    except Exception as e:
        print(f"⚠️ Could not store Garmin tokens for {athlete_id}: {e}")

    _garmin_temp_secrets.pop(athlete_id, None)

    return """
    <html><body style="font-family:system-ui;text-align:center;padding:60px;background:#f0f4f8">
      <h2 style="color:#2E7D32">&#10003; Garmin connected!</h2>
      <p>Your Garmin account is now linked to ACI.</p>
      <p>Data will sync automatically each time you sync your Garmin device.</p>
      <p style="color:#888;font-size:13px">You can close this tab.</p>
    </body></html>
    """


@server.route("/garmin/push", methods=["POST"])
def garmin_push():
    """
    Garmin POSTs data here after every device sync.
    Register this URL in the Garmin Developer Portal as the
    push endpoint for: Dailies, Sleeps, Activities, HRV, Body Battery.

    URL to register: https://yourapp.com/garmin/push
    """
    try:
        payload = flask_request.get_json(force=True) or {}
        parsed  = garmin_parse_to_scales(payload)
        scales  = {k: v for k, v in parsed.items() if v is not None}

        # Identify which athlete this belongs to using the Garmin userId
        # The userId comes in the dailies/activities array
        garmin_user_id = None
        for key in ["dailies", "activities", "sleeps"]:
            items = payload.get(key, [])
            if items and "userId" in items[0]:
                garmin_user_id = str(items[0]["userId"])
                break

        if garmin_user_id and sh is not None:
            # Find which athlete sheet has this Garmin userId
            for ws in sh.worksheets():
                try:
                    df = load_tab(ws.title)
                    tok, _ = garmin_get_athlete_tokens(df)
                    if tok:
                        # Write today's Garmin data to the matching date row
                        today = today_adl()
                        df["Date"] = pd.to_datetime(df.get("Date", pd.Series(dtype=str)),
                                                     errors="coerce").dt.date
                        matches = df.index[df["Date"] == today].tolist()
                        if matches:
                            write_row(ws.title, matches[0], scales)
                            print(f"✅ Garmin push written to {ws.title} row {matches[0]}")
                            break
                except Exception:
                    continue

    except Exception as e:
        print(f"❌ Garmin push error: {e}")

    # Always return 200 — Garmin will retry on non-200
    return jsonify({"status": "ok"}), 200


@server.route("/garmin/status")
def garmin_status():
    """Quick check endpoint — shows which athletes have Garmin linked."""
    if sh is None:
        return jsonify({"error": "Google Sheets not connected"}), 503
    linked = []
    for ws in sh.worksheets():
        try:
            df = load_tab(ws.title)
            tok, _ = garmin_get_athlete_tokens(df)
            linked.append({"athlete": ws.title, "garmin_linked": tok is not None})
        except Exception:
            pass
    return jsonify(linked)



if __name__ == "__main__":
    app.run(debug=True)