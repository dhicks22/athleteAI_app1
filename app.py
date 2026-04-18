# app.py — consolidated + FIXED version
# Key fixes:
# 1) ✅ No more NaTType.start_time crash: _week_agg_date() is now NaT-safe (uses week_bucket()).
# 2) ✅ Removed duplicate/contradictory definitions (dial_class_from_score, bottom_nav, imports, etc.)
# 3) ✅ Removed the broken commented-out _build_dial block that was causing indentation/parse issues.
# 4) ✅ Kept your structure + UI intact, but made the weekly bucketing + plotting robust.
# 5) ✅ Session log popup: clicking a logged day shows a read-only summary modal.
#       Clicking an unlogged day opens the session input form as before.
# 6) ✅ Duplicate date deduplication before reindex (fixes ValueError: cannot reindex on duplicate labels).
# 7) ✅ load_tab now fetches FORMATTED_VALUE for Date column so serial numbers (=C206+1) parse correctly.

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
    margin=dict(l=24, r=16, t=48, b=80),
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
        y=-0.22,
        xanchor="center",
        x=0.5,
        font=dict(size=10),
        tracegroupgap=0,
        itemsizing="constant",
        itemwidth=40,
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
#  ✅ NaT-safe weekly bucketing
# ============================================================

def week_bucket(dates: pd.Series, week_anchor: str = "W-SAT") -> pd.Series:
    s = pd.to_datetime(dates, errors="coerce")
    if s.notna().sum() == 0:
        return pd.Series([pd.NaT] * len(s), index=s.index)
    p = s.dt.to_period(week_anchor)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    mask = p.notna()
    out.loc[mask] = p[mask].dt.start_time
    return out


def _week_agg_date(d: pd.Series) -> pd.Series:
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
#  Readiness calculations
# ============================================================

def streak_colour_from_days(days: int):
    return "dial-pink"


def calc_daily_readiness(load_series, rpe_series, quality_series, span=7):
    df = pd.DataFrame({
        "load": pd.to_numeric(load_series, errors="coerce"),
        "rpe":  pd.to_numeric(rpe_series,  errors="coerce"),
        "qual": pd.to_numeric(quality_series, errors="coerce"),
    })

    load_ref = df["load"].quantile(0.90)
    if pd.isna(load_ref) or load_ref <= 0:
        load_ref = df["load"].max()

    # If no RPE logged at all but we have load data, return a moderate base score
    # so the dial isn't permanently blank for athletes who haven't post-logged yet
    rpe_valid = df["rpe"].dropna()
    if rpe_valid.empty:
        if pd.isna(load_ref) or load_ref <= 0:
            return None
        # Load exists but no post-session RPE — assume moderate RPE (3/5) as placeholder
        df["rpe"] = 3.0

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
        penalty   = min(DECAY_RATE * days_silent, MAX_PENALTY)
        readiness = float(np.clip(base_readiness - penalty, 0, 100))
    else:
        readiness = base_readiness

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
        sleep    = float(np.clip(sleep,    1, 5))
        fatigue  = float(np.clip(fatigue,  1, 5))
        soreness = float(np.clip(soreness, 1, 5))
        mood     = float(np.clip(mood,     1, 5))
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
            s  = float(np.clip(pd.to_numeric(r.get("Sleep_1_5"),    errors="coerce"), 1, 5))
            f  = float(np.clip(pd.to_numeric(r.get("Fatigue_1_5"),  errors="coerce"), 1, 5))
            so = float(np.clip(pd.to_numeric(r.get("Soreness_1_5"), errors="coerce"), 1, 5))
            m  = float(np.clip(pd.to_numeric(r.get("Mood_1_5"),     errors="coerce"), 1, 5))
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


# Columns where we want to preserve hyperlinks from Google Sheets
HYPERLINK_COLS = {"Sets_Reps_Load", "Workout", "Focus"}

def _parse_hyperlink(cell: str):
    if not cell:
        return None, cell
    m = re.match(r'=HYPERLINK\("([^"]+)","([^"]+)"\)', str(cell).strip(), re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    m2 = re.match(r"=HYPERLINK\('([^']+)','([^']+)'\)", str(cell).strip(), re.IGNORECASE)
    if m2:
        return m2.group(1), m2.group(2)
    return None, cell


# ============================================================
#  ✅ FIXED load_tab — dual fetch: FORMULA for hyperlinks,
#     FORMATTED_VALUE for dates (handles =C206+1 serials)
# ============================================================

def load_tab(tab_name: str) -> pd.DataFrame:
    if sh is None or not tab_name:
        return pd.DataFrame()

    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.get_worksheet(0)

    # Fetch formula render so we get =HYPERLINK(...) formulas intact
    try:
        all_values = ws.get_all_values(value_render_option="FORMULA")
    except Exception:
        try:
            all_values = ws.get_all_values()
        except Exception:
            return pd.DataFrame()

    # Also fetch formatted values — this gives us human-readable dates
    # even when the cell contains a formula like =C206+1 (serial number)
    try:
        formatted_values = ws.get_all_values(value_render_option="FORMATTED_VALUE")
    except Exception:
        formatted_values = all_values

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

    # Override formula-computed columns with FORMATTED_VALUE results.
    # The FORMULA render returns raw formula strings (e.g. "=IFERROR(G2*H2,\"\")")
    # for computed cells — pd.to_numeric can't parse these, so we must use
    # the evaluated/formatted values for all numeric and date columns.
    NUMERIC_OVERRIDE_COLS = {
        "Date", "Load", "SPEED (m)", "TEMPO (m)", "sRPE", "RPE", "Duration",
        "EWMA 28", "EMWA 28", "EWMA 7", "EMWA 7",
        "Sleep_1_5", "Fatigue_1_5", "Mood_1_5", "Soreness_1_5",
        "RPE_Post_Session", "Session_1_5",
    }
    if formatted_values and len(formatted_values) > 1:
        fmt_headers = formatted_values[0]
        fmt_rows    = formatted_values[1:]
        for col in NUMERIC_OVERRIDE_COLS:
            if col in df.columns and col in fmt_headers:
                col_idx = fmt_headers.index(col)
                df[col] = [
                    row[col_idx] if col_idx < len(row) else ""
                    for row in fmt_rows
                ]

    # Parse hyperlinks for relevant cols
    for col in HYPERLINK_COLS:
        if col not in df.columns:
            continue
        urls = []
        texts = []
        for cell in df[col]:
            url, text = _parse_hyperlink(str(cell) if cell else "")
            urls.append(url)
            texts.append(text)
        df[col] = texts
        df[f"{col}_url"] = urls

    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce", dayfirst=True).dt.date

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


def append_row_for_date(tab_name: str, date_obj: dt.date, payload: dict) -> int:
    if sh is None:
        raise RuntimeError("Google Sheets not connected")
    ws = sh.worksheet(tab_name)
    sheet_vals = ws.get_all_values()
    if not sheet_vals:
        raise RuntimeError("Sheet is empty — no headers found")

    headers = sheet_vals[0]
    new_row = [""] * len(headers)

    if "Date" in headers:
        new_row[headers.index("Date")] = str(date_obj)
    if "Athlete" in headers:
        new_row[headers.index("Athlete")] = tab_name

    for col_name, value in payload.items():
        if col_name in headers:
            new_row[headers.index(col_name)] = "" if value is None else str(value)

    ws.append_row(new_row, value_input_option="USER_ENTERED")
    return len(sheet_vals) - 1


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

    # Sheet may use "RPE" (planned) or "RPE_Post_Session" (athlete-logged)
    sRPE7     = safe_mean("RPE_Post_Session") if "RPE_Post_Session" in recent.columns else safe_mean("RPE")
    load7     = safe_mean("Load")
    sleep7    = safe_mean("Sleep_1_5")
    fat7      = safe_mean("Fatigue_1_5")
    mood7     = safe_mean("Mood_1_5")
    soreness7 = safe_mean("Soreness_1_5")

    # Sheet headers use "EMWA" (transposed) — check both spellings
    ew7_col  = next((c for c in ["EWMA 7",  "EMWA 7"]  if c in df.columns), None)
    ew28_col = next((c for c in ["EWMA 28", "EMWA 28"] if c in df.columns), None)

    if ew7_col and ew28_col:
        try:
            ew7  = pd.to_numeric(df[ew7_col],  errors="coerce")
            ew28 = pd.to_numeric(df[ew28_col], errors="coerce").replace(0, np.nan)
            acwr = (ew7 / ew28).replace([np.inf, -np.inf], np.nan)
            acwr7 = "n/a" if acwr.dropna().empty else round(float(acwr.tail(days).mean(skipna=True)), 2)
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
    end_val   = recent.iloc[-1]
    avg       = round(float(recent.mean()), 1)
    delta     = end_val - start_val

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

    if "Load"        in recent.columns: lines.append(_describe_trend(recent["Load"],        "Training Load"))
    if "Fatigue_1_5" in recent.columns: lines.append(_describe_trend(recent["Fatigue_1_5"], "Fatigue (1–5)"))
    if "Mood_1_5"    in recent.columns: lines.append(_describe_trend(recent["Mood_1_5"],    "Mood (1–5)"))
    if "Sleep_1_5"   in recent.columns: lines.append(_describe_trend(recent["Sleep_1_5"],   "Sleep quality (1–5)"))

    ew7_col  = "EWMA 7"  if "EWMA 7"  in recent.columns else ("EMWA 7"  if "EMWA 7"  in recent.columns else None)
    ew28_col = "EWMA 28" if "EWMA 28" in recent.columns else ("EMWA 28" if "EMWA 28" in recent.columns else None)

    if ew7_col and ew28_col:
        try:
            ew7  = pd.to_numeric(recent[ew7_col],  errors="coerce")
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
                    "RPE_Post_Session", "RPE", "Sleep_1_5", "Fatigue_1_5",
                    "Mood_1_5", "Soreness_1_5"]

    present = [c for c in athlete_cols if c in d.columns]
    if not present:
        return "No previous session data available."

    tail  = d.tail(max_rows)
    lines = []

    for _, r in tail.iterrows():
        date_str = str(r.get("Date", "unknown"))
        note     = str(r.get("Athlete_Notes",    "")).strip()
        sets     = str(r.get("Sets_Reps_Load",   "")).strip()
        track    = str(r.get("Track_Reps_Times", "")).strip()
        ai1_prev = str(r.get("AI_Suggestion_1",  "")).strip()

        rpe      = pd.to_numeric(r.get("RPE_Post_Session"), errors="coerce")
        sleep    = pd.to_numeric(r.get("Sleep_1_5"),        errors="coerce")
        fatigue  = pd.to_numeric(r.get("Fatigue_1_5"),      errors="coerce")
        mood     = pd.to_numeric(r.get("Mood_1_5"),         errors="coerce")
        soreness = pd.to_numeric(r.get("Soreness_1_5"),     errors="coerce")

        has_data = (
            any(s.lower() not in ("", "nan", "none", "nil") for s in [note, sets, track])
            or any(pd.notna(v) and v > 0 for v in [rpe, sleep, fatigue, mood, soreness])
        )
        if not has_data:
            continue

        parts = [f"[{date_str}]"]
        flags = []

        if pd.notna(soreness):
            if soreness >= 4:   flags.append(f"HIGH soreness ({int(soreness)}/5)")
            elif soreness >= 3: flags.append(f"moderate soreness ({int(soreness)}/5)")
        if pd.notna(fatigue):
            if fatigue <= 2:   flags.append(f"LOW energy/fatigue ({int(fatigue)}/5)")
            elif fatigue <= 3: flags.append(f"moderate fatigue ({int(fatigue)}/5)")
        if pd.notna(sleep):
            if sleep <= 2:   flags.append(f"POOR sleep ({int(sleep)}/5)")
            elif sleep <= 3: flags.append(f"average sleep ({int(sleep)}/5)")
        if pd.notna(mood):
            if mood <= 2: flags.append(f"LOW mood ({int(mood)}/5)")

        if pd.notna(rpe):
            parts.append(f"RPE {int(rpe)}/5")

        if flags:
            parts.append("Wellness: " + ", ".join(flags))
        else:
            if any(pd.notna(v) for v in [sleep, fatigue, mood, soreness]):
                parts.append("Wellness: all markers within normal range")

        if note  and note.lower()  not in ("nan", "none", "nil"): parts.append(f"Note: {note}")
        if sets  and sets.lower()  not in ("nan", "none", "nil"): parts.append(f"Gym: {sets}")
        if track and track.lower() not in ("nan", "none", "nil"): parts.append(f"Track: {track}")

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
            flags.append(f"Soreness trending elevated (avg {avg_sor:.1f}/5 over {len(soreness)} sessions).")

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
            flags.append(f"Fatigue/energy trending low (avg {avg_fat:.1f}/5 over {len(fatigue)} sessions).")

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
        first_half  = rpe.iloc[:len(rpe)//2].mean()
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
        take  = future.head(n)

        for _, r in take.iterrows():
            date_str = str(r.get("Date", ""))
            workout  = str(r.get("Workout", "")).strip()
            focus    = str(r.get("Focus", "")).strip() if "Focus" in future.columns else ""
            venue    = str(r.get("Venue", "")).strip() if "Venue" in future.columns else ""

            extras = []
            if focus and focus.lower() not in ("nan", "none", "nil"): extras.append(f"Focus: {focus}")
            if venue and venue.lower() not in ("nan", "none", "nil"): extras.append(f"Venue: {venue}")

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
    "Acceleration & Speed Coach":  ["acceleration", "speed", "max velocity", "explosive", "contact time", "fast reps"],
    "Tempo & Endurance Coach":     ["tempo", "aerobic", "endurance", "pacing", "conditioning"],
    "Technical Sprint Coach":      ["posture", "angles", "mechanics", "arm action", "technique", "rhythm"],
    "Strength & Power Coach":      ["strength", "load", "gym", "sets", "reps", "bar speed", "plyometric"],
    "Recovery & Readiness Coach":  ["fatigue", "recovery", "sleep", "soreness", "readiness", "stress"],
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
    athlete_name, selected_date, session_rpe, session_quality,
    sleep, fatigue, mood, soreness, notes, sets_reps_load,
    track_reps_times, ai_mode_1, ai_mode_2,
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
    row_idx     = row_matches[0] if row_matches else None

    workout   = safe(df, row_idx, "Workout",  "not specified") if row_idx is not None else "not specified"
    focus_txt = safe(df, row_idx, "Focus",    "not specified") if row_idx is not None else "not specified"
    venue     = safe(df, row_idx, "Venue",    "not specified") if row_idx is not None else "not specified"
    upcoming  = build_upcoming_context(df, selected_date_dt, n=4)

    first_name = athlete_name.strip().split()[0] if athlete_name.strip() else "Athlete"

    summary       = build_context_summary(df, days=7)
    trend_context = build_trend_context(df, days=14)
    wellness_scan = build_wellness_flags(df, days=7)
    history_text  = build_text_history(df, max_rows=5)

    notes            = (notes or "").strip()            or "none provided"
    sets_reps_load   = (sets_reps_load or "").strip()   or "none provided"
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
    system_1  = (
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
        [{"role": "system", "content": system_1}, {"role": "user", "content": user_1}],
        max_tokens=500,
    )

    persona_2  = persona_prompt(ai_mode_2)
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
        [{"role": "system", "content": system_2}, {"role": "user", "content": user_2}],
        max_tokens=400,
    )

    return (ai1 or "").strip(), (ai2 or "").strip()


# ============================================================
#  Email webhook
# ============================================================

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
    if df is None or df.empty:
        return None, None
    for col_t, col_s in [("Garmin_Token", "Garmin_Secret"), ("garmin_token", "garmin_secret")]:
        if col_t in df.columns and col_s in df.columns:
            tok = df[col_t].dropna().astype(str)
            sec = df[col_s].dropna().astype(str)
            tok = tok[tok.str.strip().str.len() > 5]
            sec = sec[sec.str.strip().str.len() > 5]
            if not tok.empty and not sec.empty:
                return tok.iloc[0].strip(), sec.iloc[0].strip()
    return None, None


def garmin_fetch_today(user_token: str, user_secret: str, date: dt.date) -> dict:
    auth     = _garmin_oauth1(user_token, user_secret)
    start_ts = int(dt.datetime.combine(date, dt.time.min).timestamp())
    end_ts   = int(dt.datetime.combine(date, dt.time.max).timestamp())

    endpoints = {
        "dailies":       f"{GARMIN_API_BASE}/dailies?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
        "sleeps":        f"{GARMIN_API_BASE}/sleeps?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
        "activities":    f"{GARMIN_API_BASE}/activities?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
        "hrv":           f"{GARMIN_API_BASE}/hrv?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
        "bodyBattery":   f"{GARMIN_API_BASE}/bodyBattery?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
        "stressDetails": f"{GARMIN_API_BASE}/stressDetails?uploadStartTimeInSeconds={start_ts}&uploadEndTimeInSeconds={end_ts}",
    }

    results = {}
    for key, url in endpoints.items():
        try:
            r = requests.get(url, auth=auth, timeout=10)
            results[key] = r.json() if r.status_code == 200 else {}
        except Exception:
            results[key] = {}

    return results


def garmin_parse_to_scales(raw: dict) -> dict:
    out = {}

    dailies = raw.get("dailies", [])
    if dailies:
        d = dailies[0]
        out["Garmin_Steps"]          = d.get("steps")
        out["Garmin_Resting_HR"]     = d.get("restingHeartRateInBeatsPerMinute")
        out["Garmin_Avg_HR"]         = d.get("averageHeartRateInBeatsPerMinute")
        out["Garmin_Stress_Avg"]     = d.get("averageStressLevel")
        out["Garmin_BB_Low"]         = d.get("bodyBatteryLowestValue")
        out["Garmin_BB_High"]        = d.get("bodyBatteryHighestValue")
        out["Garmin_Intensity_Mins"] = (
            (d.get("moderateIntensityMinutes") or 0) +
            (d.get("vigorousIntensityMinutes") or 0)
        )
        bb_high = d.get("bodyBatteryHighestValue")
        if bb_high is not None:
            out["Fatigue_1_5"] = max(1, min(5, round(bb_high / 20)))
        stress = d.get("averageStressLevel")
        if stress is not None:
            out["Soreness_1_5"] = max(1, min(5, 6 - round(stress / 20)))

    sleeps = raw.get("sleeps", [])
    if sleeps:
        s = sleeps[0]
        out["Garmin_Sleep_Dur_s"]  = s.get("durationInSeconds")
        out["Garmin_Sleep_Deep_s"] = s.get("deepSleepDurationInSeconds")
        out["Garmin_Sleep_REM_s"]  = s.get("remSleepInSeconds")
        out["Garmin_Sleep_Score"]  = (s.get("overallSleepScore") or {}).get("value")
        out["Garmin_SpO2_Avg"]     = s.get("averageSpO2Value")
        sleep_score = out.get("Garmin_Sleep_Score")
        if sleep_score is not None:
            out["Sleep_1_5"] = max(1, min(5, round(sleep_score / 20)))

    hrv_list = raw.get("hrv", [])
    if hrv_list:
        h = hrv_list[0].get("hrvSummary", {})
        out["Garmin_HRV_Weekly_Avg"] = h.get("weeklyAvg")
        out["Garmin_HRV_LastNight"]  = h.get("lastNight")
        out["Garmin_HRV_Status"]     = h.get("status")
        hrv_map    = {"POOR": 1, "LOW": 2, "UNBALANCED": 2, "BALANCED": 4, "HIGH": 5}
        hrv_status = (h.get("status") or "").upper()
        if hrv_status in hrv_map:
            out["Mood_1_5"] = hrv_map[hrv_status]

    activities = raw.get("activities", [])
    if activities:
        a = activities[0]
        out["Garmin_Activity_Name"]   = a.get("activityName")
        out["Garmin_Activity_Type"]   = a.get("activityType")
        out["Garmin_Duration_s"]      = a.get("durationInSeconds")
        out["Garmin_Distance_m"]      = a.get("distanceInMeters")
        out["Garmin_Activity_HR"]     = a.get("averageHeartRateInBeatsPerMinute")
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
    result = {
        "sleep": None, "fatigue": None, "mood": None, "soreness": None,
        "load": None, "rpe": None, "quality": None, "source": "manual",
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

    garmin_synced = str(row.get("Garmin_Synced", "")).strip().lower() == "yes"
    manual_rpe    = _n("RPE_Post_Session")

    result["source"]   = "garmin" if garmin_synced else "manual"
    result["sleep"]    = _n("Sleep_1_5")
    result["fatigue"]  = _n("Fatigue_1_5")
    result["soreness"] = _n("Soreness_1_5")
    result["mood"]     = _n("Mood_1_5")
    result["load"]     = _n("Load")
    result["rpe"]      = manual_rpe
    result["quality"]  = _n("Session_1_5")

    return result



# ============================================================
#  Shared neuro readiness computation — used by dashboard + share card
#  to ensure both always show the same value
# ============================================================

def compute_neuro_for_athlete(df: pd.DataFrame, today: dt.date) -> float | None:
    """Compute neuromuscular readiness exactly as update_dashboard does."""
    NEURO_WINDOW  = 14
    NEURO_DECAY   = 3.5
    NEURO_MAX_PEN = 35.0

    if df is None or df.empty:
        return None

    df_neuro = df.copy()
    df_neuro["Date"] = pd.to_datetime(df_neuro["Date"], errors="coerce").dt.date
    df_neuro = df_neuro.sort_values("Date")
    recent_neuro = df_neuro[df_neuro["Date"] >= today - dt.timedelta(days=NEURO_WINDOW)]

    def _last_col(frame, col):
        s = pd.to_numeric(frame.get(col, pd.Series(dtype=float)), errors="coerce").dropna()
        return float(s.iloc[-1]) if not s.empty else None

    sleep_last    = _last_col(recent_neuro, "Sleep_1_5")
    fatigue_last  = _last_col(recent_neuro, "Fatigue_1_5")
    soreness_last = _last_col(recent_neuro, "Soreness_1_5")
    mood_last     = _last_col(recent_neuro, "Mood_1_5")

    if any(v is None for v in [sleep_last, fatigue_last, soreness_last, mood_last]):
        return None

    neuro_val = calc_neuro_readiness(sleep_last, fatigue_last, soreness_last, mood_last,
                                     history_df=recent_neuro, span=3)

    wellness_cols = ["Sleep_1_5", "Fatigue_1_5", "Mood_1_5", "Soreness_1_5"]
    present_cols  = [c for c in wellness_cols if c in df_neuro.columns]
    if present_cols:
        df_neuro["_hw"] = df_neuro[present_cols].apply(
            lambda row: any(pd.to_numeric(row, errors="coerce").gt(0).dropna()), axis=1)
        logged_dates = df_neuro[df_neuro["_hw"]]["Date"]
        if not logged_dates.empty:
            last_date = logged_dates.max()
            if hasattr(last_date, "date"):
                last_date = last_date.date()
            days_silent = (today - last_date).days
            if days_silent > 0:
                neuro_val = float(np.clip(
                    neuro_val - min(NEURO_DECAY * days_silent, NEURO_MAX_PEN), 0, 100))

    return float(np.clip(neuro_val, 0, 100))

# ============================================================
#  Plot builders
# ============================================================

def build_load_plot(df: pd.DataFrame, view_mode: str):
    fig = go.Figure()

    if df.empty or "Date" not in df.columns or "Load" not in df.columns:
        fig.update_layout(**MOBILE_PLOT_LAYOUT)
        return fig

    _BLUE       = "#1E6BD6"
    _TEAL       = "#1BA39C"
    _GREEN_DARK = "#6B7280"
    _PURPLE     = "#7B61FF"

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.sort_values("Date")
    d["Load"] = pd.to_numeric(d["Load"], errors="coerce")

    if view_mode == "weekly":
        # Fill full date range, then aggregate by week keeping ALL weeks (including rest weeks)
        d = d.dropna(subset=["Date"]).set_index("Date")
        full_idx = pd.date_range(d.index.min(), d.index.max(), freq="D")
        d = d.reindex(full_idx).reset_index().rename(columns={"index": "Date"})
        d["Load"] = pd.to_numeric(d["Load"], errors="coerce").fillna(0)
        d["Week"] = _week_agg_date(d["Date"])
        g = d.groupby("Week", as_index=False).agg(Load=("Load", lambda s: s.sum()))
        # Keep rest weeks (Load=0) — they contribute to decay, don't drop them
        # But hide zero bars visually
        g["Load_display"] = g["Load"].replace(0, float("nan"))

        # EWMA using 1/N alpha convention — computed over all weeks including rest
        alpha_a = 1 / 4    # 4-week acute
        alpha_c = 1 / 16   # 16-week chronic
        load_w  = g["Load"].values
        n_w     = len(load_w)
        ewma_a  = np.full(n_w, np.nan)
        ewma_c  = np.full(n_w, np.nan)
        for i in range(n_w):
            li = float(load_w[i])  # 0 for rest weeks, decays naturally
            if i == 0:
                ewma_a[i] = li; ewma_c[i] = li
            else:
                ewma_a[i] = alpha_a * li + (1 - alpha_a) * ewma_a[i-1]
                ewma_c[i] = alpha_c * li + (1 - alpha_c) * ewma_c[i-1]

        g["Acute"]   = ewma_a
        g["Chronic"] = ewma_c
        g["ACWR"]    = np.where(g["Chronic"] > 50,
                                (g["Acute"] / g["Chronic"]).clip(0, 2.5),
                                np.nan)
        x = g["Week"]

        fig.add_bar(x=x, y=g["Load_display"], name="Load",
                    marker=dict(color="rgba(30,107,214,0.35)", line=dict(color=_BLUE, width=1.8)),
                    hovertemplate="Load: %{y:,.0f}<extra></extra>")
        fig.add_trace(go.Scatter(x=x, y=g["Acute"], name="Acute (4wk)", mode="lines",
                                 line=dict(color=_TEAL, width=2.0, dash="dot"), line_shape="spline", line_smoothing=0.75,
                                 hovertemplate="Acute: %{y:,.0f}<extra></extra>"))
        fig.add_trace(go.Scatter(x=x, y=g["Chronic"], name="Chronic (16wk)", mode="lines",
                                 line=dict(color=_GREEN_DARK, width=2.0, dash="dash"), line_shape="spline",
                                 line_smoothing=0.75, opacity=0.7,
                                 hovertemplate="Chronic: %{y:,.0f}<extra></extra>"))
        fig.add_trace(go.Scatter(x=x, y=g["ACWR"], name="ACWR", mode="lines", yaxis="y2",
                                 line=dict(color=_PURPLE, width=1.2, dash="dot"), line_shape="spline", line_smoothing=0.75,
                                 opacity=0.5, hovertemplate="ACWR: %{y:.2f}<extra></extra>"))
        fig.add_shape(type="rect", xref="paper", x0=0, x1=1, yref="y2", y0=0.9, y1=1.25,
                      fillcolor="rgba(56,189,248,0.12)", line_width=0, layer="below")
        fig.update_layout(title="Weekly Training Load & Balance", xaxis_title="",
                          yaxis=dict(title="Load"),
                          yaxis2=dict(title="ACWR", overlaying="y", side="right", range=[0, 2], showgrid=False),
                          hovermode="x unified", **MOBILE_PLOT_LAYOUT)
        return fig

    # Reindex to full daily range — keep NaN for rest days so EWMA decays naturally
    # (filling with 0 causes EWMA to crash to zero then spike on return = bad ACWR)
    d = d.dropna(subset=["Date"]).set_index("Date")
    full_idx = pd.date_range(d.index.min(), d.index.max(), freq="D")
    d = d.reindex(full_idx)
    d["Load"] = pd.to_numeric(d["Load"], errors="coerce")

    # Sports-science EWMA convention: alpha = 1/N (matches Google Sheets EW formulas)
    # This gives slower, more realistic decay through rest periods
    alpha7  = 1 / 7
    alpha28 = 1 / 28
    load_vals = d["Load"].values
    n = len(load_vals)
    ewma7  = np.full(n, np.nan)
    ewma28 = np.full(n, np.nan)

    for i in range(n):
        v = load_vals[i]
        load_i = 0.0 if np.isnan(v) else float(v)  # rest day contributes 0
        if i == 0:
            ewma7[i]  = load_i
            ewma28[i] = load_i
        else:
            ewma7[i]  = alpha7  * load_i + (1 - alpha7)  * ewma7[i-1]
            ewma28[i] = alpha28 * load_i + (1 - alpha28) * ewma28[i-1]

    d["EWMA7"]  = ewma7
    d["EWMA28"] = ewma28

    # Only show ACWR once chronic load is established (suppress first ~4 weeks)
    # Use 50 as threshold — well below typical session loads of 300-900
    d["ACWR"]   = np.where(d["EWMA28"] > 50,
                           (d["EWMA7"] / d["EWMA28"]).clip(0, 2.5),
                           np.nan)

    # Bars only on training days (NaN = no bar)
    d = d.reset_index().rename(columns={"index": "Date"})
    d["Load_display"] = d["Load"]  # NaN gaps already blank
    x = d["Date"]

    fig.add_bar(x=x, y=d["Load_display"], name="Load",
                marker=dict(color="rgba(30,107,214,0.35)", line=dict(color=_BLUE, width=1.8)),
                hovertemplate="Load: %{y:,.0f}<extra></extra>")
    fig.add_trace(go.Scatter(x=x, y=d["EWMA7"], name="7d EWMA", mode="lines",
                             line=dict(color=_TEAL, width=2.0, dash="dot"), line_shape="spline", line_smoothing=0.75,
                             hovertemplate="7d: %{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=x, y=d["EWMA28"], name="28d EWMA", mode="lines",
                             line=dict(color=_GREEN_DARK, width=2.0, dash="dash"), line_shape="spline",
                             line_smoothing=0.75, opacity=0.7,
                             hovertemplate="28d: %{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=x, y=d["ACWR"], name="ACWR", mode="lines", yaxis="y2",
                             line=dict(color=_PURPLE, width=1.2, dash="dot"), line_shape="spline", line_smoothing=0.75,
                             opacity=0.6, hovertemplate="ACWR: %{y:.2f}<extra></extra>"))
    fig.add_shape(type="rect", xref="paper", x0=0, x1=1, yref="y2", y0=0.9, y1=1.25,
                  fillcolor="rgba(56,189,248,0.12)", line_width=0, layer="below")
    fig.update_layout(title="Daily Training Load & Balance", xaxis_title="",
                      yaxis=dict(title="Load"),
                      yaxis2=dict(title="ACWR", overlaying="y", side="right", range=[0, 2], showgrid=False),
                      hovermode="x unified", **MOBILE_PLOT_LAYOUT)
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
            fig.add_trace(go.Scatter(x=x, y=roll, mode="lines", line=dict(width=0),
                                     fill="tozeroy",
                                     fillcolor=f"rgba{tuple(int(color[i:i+2], 16) for i in (1,3,5)) + (0.12,)}",
                                     hoverinfo="skip", showlegend=False))
            fig.add_trace(go.Scatter(x=x, y=roll, name=label, mode="lines",
                                     line=dict(color=color, width=2.6), line_shape="spline", line_smoothing=0.7,
                                     hovertemplate=f"{label}: %{{y:.1f}}<extra></extra>"))

        fig.update_layout(
            title="Weekly Wellness Trends", xaxis_title="",
            yaxis=dict(title="Scale (1–5)", range=[0.8, 5.2], tickvals=[1, 2, 3, 4, 5]),
            hovermode="x unified",
            legend=dict(
                orientation="h", yanchor="top", y=-0.20,
                xanchor="center", x=0.5,
                font=dict(size=10), itemwidth=40,
                tracegroupgap=0,
            ),
            margin=dict(l=24, r=16, t=48, b=90),
        )
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
        fig.add_trace(go.Scatter(x=x, y=[0] * len(x), mode="lines", line=dict(width=0),
                                 showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=x, y=roll, mode="lines", line=dict(width=0),
                                 fill="tonexty",
                                 fillcolor=f"rgba{tuple(int(color[i:i+2], 16) for i in (1,3,5)) + (0.12,)}",
                                 hoverinfo="skip", showlegend=False))
        fig.add_trace(go.Scatter(x=x, y=roll, name=label, mode="lines",
                                 line=dict(color=color, width=2.6), line_shape="spline", line_smoothing=0.7,
                                 hovertemplate=f"{label}: %{{y:.1f}}<extra></extra>",
                                 visible="legendonly" if label == "Mood" else True))

    fig.update_layout(
        title="Daily Wellness Trends", xaxis_title="",
        yaxis=dict(title="Scale (1–5)", range=[0.8, 5.2], tickvals=[1, 2, 3, 4, 5]),
        hovermode="x unified",
        legend=dict(
            orientation="h", yanchor="top", y=-0.20,
            xanchor="center", x=0.5,
            font=dict(size=10), itemwidth=40,
            tracegroupgap=0,
        ),
        margin=dict(l=24, r=16, t=48, b=90),
    )
    return fig


def build_speed_tempo_plot(df: pd.DataFrame, view_mode: str):
    fig = go.Figure()

    if df.empty or "Date" not in df.columns:
        fig.update_layout(**MOBILE_PLOT_LAYOUT)
        return fig

    _BLUE   = "#2563EB"
    _ORANGE = "#F59E0B"

    d = df.copy()
    d["Date"] = pd.to_datetime(d["Date"], errors="coerce")
    d = d.sort_values("Date")

    speed = pd.to_numeric(d.get("SPEED (m)", np.nan), errors="coerce")
    tempo = pd.to_numeric(d.get("TEMPO (m)", np.nan), errors="coerce")

    if view_mode == "daily":
        x = d["Date"]
        fig.add_bar(x=x, y=speed, name="Speed",
                    marker=dict(color="rgba(37,99,235,0.35)", line=dict(color=_BLUE, width=1.6)),
                    hovertemplate="Speed: %{y:,.0f} m<extra></extra>")
        fig.add_bar(x=x, y=tempo, name="Tempo",
                    marker=dict(color="rgba(245,158,11,0.35)", line=dict(color=_ORANGE, width=1.6)),
                    hovertemplate="Tempo: %{y:,.0f} m<extra></extra>")
        fig.add_trace(go.Scatter(x=x, y=speed.ewm(span=7, adjust=False, min_periods=1).mean(),
                                 name="Speed trend", mode="lines",
                                 line=dict(color=_BLUE, width=1.8, dash="dot"), line_shape="spline", line_smoothing=0.7,
                                 hovertemplate="Speed trend: %{y:,.0f} m<extra></extra>"))
        fig.add_trace(go.Scatter(x=x, y=tempo.ewm(span=7, adjust=False, min_periods=1).mean(),
                                 name="Tempo trend", mode="lines",
                                 line=dict(color=_ORANGE, width=1.8, dash="dot"), line_shape="spline", line_smoothing=0.7,
                                 hovertemplate="Tempo trend: %{y:,.0f} m<extra></extra>"))
        fig.update_layout(title="Daily Speed & Tempo Volumes", xaxis_title="", yaxis_title="Metres",
                          barmode="stack", hovermode="x unified", **MOBILE_PLOT_LAYOUT)
        return fig

    d["Week"]        = _week_agg_date(d["Date"])
    d["Speed_clean"] = speed
    d["Tempo_clean"] = tempo

    g = d.groupby("Week", as_index=False).agg(
        Speed=("Speed_clean", lambda s: s.sum(min_count=1)),
        Tempo=("Tempo_clean", lambda s: s.sum(min_count=1)),
    )
    x = g["Week"]

    fig.add_bar(x=x, y=g["Speed"], name="Speed",
                marker=dict(color="rgba(37,99,235,0.35)", line=dict(color=_BLUE, width=1.6)),
                hovertemplate="Speed: %{y:,.0f} m<extra></extra>")
    fig.add_bar(x=x, y=g["Tempo"], name="Tempo",
                marker=dict(color="rgba(245,158,11,0.35)", line=dict(color=_ORANGE, width=1.6)),
                hovertemplate="Tempo: %{y:,.0f} m<extra></extra>")
    fig.add_trace(go.Scatter(x=x, y=g["Speed"].ewm(span=4, adjust=False, min_periods=1).mean(),
                             name="Speed trend", mode="lines",
                             line=dict(color=_BLUE, width=1.8, dash="dot"), line_shape="spline", line_smoothing=0.7,
                             hovertemplate="Speed trend: %{y:,.0f} m<extra></extra>"))
    fig.add_trace(go.Scatter(x=x, y=g["Tempo"].ewm(span=4, adjust=False, min_periods=1).mean(),
                             name="Tempo trend", mode="lines",
                             line=dict(color=_ORANGE, width=1.8, dash="dot"), line_shape="spline", line_smoothing=0.7,
                             hovertemplate="Tempo trend: %{y:,.0f} m<extra></extra>"))
    fig.update_layout(title="Weekly Speed & Tempo Volumes", xaxis_title="", yaxis_title="Metres",
                      barmode="stack", hovermode="x unified", **MOBILE_PLOT_LAYOUT)
    return fig


# ============================================================
#  Calendar UI
# ============================================================

def build_month_calendar(df: pd.DataFrame, month_date: dt.date, selected_date_str: str | None):
    if df.empty or "Date" not in df.columns:
        return html.Div("No data", className="text-muted")

    ddf = df.copy()
    ddf["Date"] = pd.to_datetime(ddf["Date"], errors="coerce").dt.date

    year  = month_date.year
    month = month_date.month
    first_day    = dt.date(year, month, 1)
    start_offset = (first_day.weekday() + 1) % 7
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
        match     = ddf[ddf["Date"] == day]
        rpe = load = None
        notes_val  = ""

        if not match.empty:
            row       = match.iloc[-1]
            # Sheet uses "RPE" for planned sRPE; fall back to RPE_Post_Session if present
            rpe_raw   = row.get("sRPE") if "sRPE" in row.index else row.get("RPE", np.nan)
            rpe       = pd.to_numeric(rpe_raw, errors="coerce")
            load      = pd.to_numeric(row.get("Load",         np.nan), errors="coerce")
            notes_val = str(row.get("Athlete_Notes", "")).strip()

        if pd.isna(rpe):         pill_color = "#CFD8DC"
        elif rpe <= 2:           pill_color = "#4285F4"
        elif rpe <= 5:           pill_color = "#4CAF50"
        elif rpe <= 7:           pill_color = "#FF9800"
        else:                    pill_color = "#F44336"

        status         = get_day_status(ddf, day)
        logged_session = status.get("logged", False)

        classes = ["calendar-day"]
        if day == today:                          classes.append("today")
        if logged_session:                        classes.append("logged")
        if day.month != month:                    classes.append("out-month")
        if selected_date and day == selected_date: classes.append("selected")

        tooltip_parts = [day.strftime("%a %d %b %Y")]
        if pd.notna(rpe):  tooltip_parts.append(f"sRPE: {int(rpe)}/10")
        if pd.notna(load): tooltip_parts.append(f"Load: {round(load, 1)}")
        if notes_val and notes_val.lower() not in ["nan", "none", "nil", "0"]:
            tooltip_parts.append(f"Notes: {notes_val[:60]}")

        cells.append(
            html.Div(
                [
                    html.Div(str(day.day), className="cal-day-number"),
                    html.Div(className="rpe-dot",
                             style={"backgroundColor": pill_color},
                             title=" | ".join(tooltip_parts)),
                ],
                id={"type": "calendar-day", "date": str(day)},
                n_clicks=0,
                className=" ".join(classes),
            )
        )

    grid = html.Div(cells, style={
        "display": "grid", "gridTemplateColumns": "repeat(7, 1fr)", "gap": "4px", "padding": "6px",
    })
    weekdays = html.Div(
        [html.Div(d, style={"textAlign": "center", "fontWeight": "600"})
         for d in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]],
        style={"display": "grid", "gridTemplateColumns": "repeat(7, 1fr)", "marginBottom": "4px"},
    )
    legend = html.Div([
        html.Small("RPE Colour Scale:", className="fw-bold me-2"),
        html.Span("1–2",  style={"background": "#4285F4", "color": "white", "padding": "2px 8px", "borderRadius": "6px", "marginRight": "6px", "fontSize": "12px"}),
        html.Span("3–5",  style={"background": "#4CAF50", "color": "white", "padding": "2px 8px", "borderRadius": "6px", "marginRight": "6px", "fontSize": "12px"}),
        html.Span("6–7",  style={"background": "#FF9800", "color": "white", "padding": "2px 8px", "borderRadius": "6px", "marginRight": "6px", "fontSize": "12px"}),
        html.Span("8–10", style={"background": "#F44336", "color": "white", "padding": "2px 8px", "borderRadius": "6px", "fontSize": "12px"}),
    ], style={"textAlign": "center", "marginTop": "8px"})

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
        <style>
          .coach-radio {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 8px;
          }
          .coach-radio-input {
            position: absolute !important;
            opacity: 0 !important;
            width: 0 !important;
            height: 0 !important;
            pointer-events: none !important;
          }
          .coach-radio-label {
            display: inline-block;
            padding: 9px 14px;
            border-radius: 22px;
            border: 1.5px solid #d0d0d0;
            background: #f5f5f5;
            color: #555;
            font-size: 13px;
            cursor: pointer;
            user-select: none;
            -webkit-user-select: none;
            transition: background 0.12s, border-color 0.12s, color 0.12s;
            white-space: nowrap;
            -webkit-tap-highlight-color: transparent;
          }
          .coach-radio-label:active { opacity: 0.8; }
          .coach-radio div:has(input:checked) .coach-radio-label {
            background: #1E88E5 !important;
            border-color: #1E88E5 !important;
            color: white !important;
            font-weight: 600 !important;
          }
          .aw-dropdown .Select-menu-outer {
            max-height: 300px !important;
            overflow-y: auto !important;
            z-index: 9999 !important;
          }
          /* Responsive dial layout */
          .dial-responsive-wrap {
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 12px 0 4px 0;
          }
          .dial-hero-row {
            display: flex;
            flex-direction: column;
            align-items: center;
            margin-bottom: 20px;
          }
          .dial-secondary-row {
            display: flex;
            justify-content: space-around;
            align-items: flex-end;
            width: 100%;
            padding-bottom: 8px;
            margin-top: 16px;
          }
          .dial-secondary-item {
            display: flex;
            flex-direction: column;
            align-items: center;
            flex: 1;
          }
          /* Arc the three dials — left tilts up-left, right tilts up-right */
          @media (max-width: 767px) {
            .dial-secondary-item:nth-child(1) {
              transform: translateY(-18px) rotate(-4deg);
            }
            .dial-secondary-item:nth-child(3) {
              transform: translateY(-18px) rotate(4deg);
            }
            .dial-secondary-item:nth-child(2) {
              transform: translateY(0px);
            }
          }
          .dial-secondary-label {
            font-size: 10px;
            color: #aaa;
            text-align: center;
            margin-top: 4px;
          }
          @media (min-width: 768px) {
            .dial-responsive-wrap {
              flex-direction: row;
              justify-content: space-around;
              align-items: flex-start;
              padding: 8px 0;
            }
            .dial-hero-row {
              flex-direction: column;
              margin-bottom: 0;
              flex: 1;
            }
            .dial-hero-row .dial-center { --dial-size: 120px !important; }
            .dial-secondary-row { display: contents; }
            .dial-secondary-item { flex: 1; }
            .dial-secondary-item .dial-center { --dial-size: 120px !important; }
            .dial-secondary-label { display: none; }
          }
          @media (max-width: 767px) {
            .dial-hero-row .dial-center { --dial-size: 140px !important; }
            .dial-secondary-item .dial-center { --dial-size: 88px !important; }
            .dial-label-desktop-only { display: none !important; }
          }
          @media (min-width: 768px) {
            .mobile-dial-label { display: none !important; }
            .desktop-dial-label { display: block !important; }
          }

          /* Dial colours handled by assets/dashboard.css */
        </style>
        <script>
        // Make share card draggable
        document.addEventListener('DOMContentLoaded', function() {
          function initDrag() {
            const modal = document.getElementById('share-card-modal');
            const handle = document.getElementById('share-card-drag-handle');
            if (!modal || !handle) { setTimeout(initDrag, 500); return; }
            let isDragging = false, startX, startY, origLeft, origTop;
            handle.addEventListener('mousedown', function(e) {
              isDragging = true;
              startX = e.clientX; startY = e.clientY;
              const rect = modal.getBoundingClientRect();
              origLeft = rect.left; origTop = rect.top;
              modal.style.transform = 'none';
              modal.style.left = origLeft + 'px';
              modal.style.top = origTop + 'px';
              handle.style.cursor = 'grabbing';
              e.preventDefault();
            });
            document.addEventListener('mousemove', function(e) {
              if (!isDragging) return;
              modal.style.left = (origLeft + e.clientX - startX) + 'px';
              modal.style.top  = (origTop  + e.clientY - startY) + 'px';
            });
            document.addEventListener('mouseup', function() {
              isDragging = false;
              handle.style.cursor = 'grab';
            });
            // Touch drag
            handle.addEventListener('touchstart', function(e) {
              isDragging = true;
              startX = e.touches[0].clientX; startY = e.touches[0].clientY;
              const rect = modal.getBoundingClientRect();
              origLeft = rect.left; origTop = rect.top;
              modal.style.transform = 'none';
              modal.style.left = origLeft + 'px';
              modal.style.top = origTop + 'px';
            }, {passive: true});
            document.addEventListener('touchmove', function(e) {
              if (!isDragging) return;
              modal.style.left = (origLeft + e.touches[0].clientX - startX) + 'px';
              modal.style.top  = (origTop  + e.touches[0].clientY - startY) + 'px';
            }, {passive: true});
            document.addEventListener('touchend', function() { isDragging = false; });
          }
          setTimeout(initDrag, 1000);
        });
        </script>
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
            html.Img(src="/assets/app_icon.png",
                     style={"height": "50px", "marginRight": "10px", "verticalAlign": "middle"}),
            html.Div(
                [
                    html.H3("Adaptive Coaching Intelligence",
                            style={"margin": 0, "fontWeight": 600, "textAlign": align}),
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
                        dbc.CardBody([
                            html.H4("Secure Access", className="mb-3", style={"textAlign": "center"}),
                            dcc.Input(type="password", style={"display": "none"}, autoComplete="new-password"),
                            dcc.Input(id="user_input", type="text", placeholder="Username",
                                      className="form-control mb-3", autoComplete="off", name="fake-username"),
                            dcc.Input(id="pass_input", type="password", placeholder="Password",
                                      className="form-control mb-3", autoComplete="new-password", name="fake-password"),
                            dbc.Button("Login", id="login-button", color="primary", style={"width": "100%"}),
                            html.Div(id="login-error", className="text-danger mt-2", style={"textAlign": "center"}),
                        ]),
                        className="login-card shadow-sm",
                    ),
                    width=12, lg=4,
                ),
                justify="center", className="mt-4",
            ),
        ],
        fluid=True, className="pt-5",
    )


def build_main_layout(auth_data):
    athlete_sheet = auth_data.get("athlete_sheet")
    is_coach      = auth_data.get("is_coach", False)

    tabs = list_tabs()
    if athlete_sheet and athlete_sheet in tabs:
        default_tab = athlete_sheet
    elif tabs:
        default_tab = tabs[0]
    else:
        default_tab = None

    if is_coach:
        options = [
            {"label": info.get("sheet", ""), "value": info.get("sheet", "")}
            for _, info in USER_LOGINS.items()
            if info.get("sheet", "")
            # Include even if not in tabs list — Sheets may be slow on cold start
        ]
    else:
        options = [{"label": athlete_sheet, "value": athlete_sheet}]

    if default_tab is None and options:
        default_tab = options[0]["value"]
    # Ensure default_tab is always set if we have options
    if not default_tab and options:
        default_tab = options[0]["value"]

    home_view = html.Div(
        id="home-view",
        children=[
            dbc.Row(
                className="g-2 align-items-end mb-2",
                children=[
                    dbc.Col([
                        html.Div("Athlete", className="mini-label"),
                        dcc.Dropdown(id="athlete-dropdown", options=options, value=default_tab,
                                     clearable=False, disabled=not is_coach, className="compact-dd"),
                    ], lg=6, md=6, width=12),
                    dbc.Col([
                        html.Div("Today", className="mini-label"),
                        html.Div(id="today-date", className="compact-today"),
                    ], lg=6, md=6, width=12),
                ],
            ),
            dbc.Row(
                className="g-2 align-items-stretch mt-1 dial-row",
                children=[
                    dbc.Col(html.Div([html.Div("Daily Readiness",        className="dial-label"),
                                      html.Div(id="readiness-dial-container",    className="dial-center")], className="dial-block"),
                            lg=3, md=3, sm=6, xs=6, width=6),
                    dbc.Col(html.Div([html.Div("Neuromuscular Readiness", className="dial-label"),
                                      html.Div(id="neuromuscular-dial-container", className="dial-center")], className="dial-block"),
                            lg=3, md=3, sm=6, xs=6, width=6),
                    dbc.Col(html.Div([html.Div("Training Exposure",       className="dial-label"),
                                      html.Div(id="weekly-dial-container",        className="dial-center")], className="dial-block"),
                            lg=3, md=3, sm=6, xs=6, width=6),
                    dbc.Col(html.Div([html.Div("Training Streak",         className="dial-label"),
                                      html.Div(id="streak-dial-container",        className="dial-center")], className="dial-block"),
                            lg=3, md=3, sm=6, xs=6, width=6),
                ],
            ),
            html.Div(id="welcome-message", className="mt-3"),
            html.Div(id="motivational-message", style={"display": "none"}),
            html.Div(id="garmin-status-badge", className="mt-2"),
            html.Div(
                dbc.Button(
                    [html.I(className="bi bi-share me-2", style={"fontSize": "12px"}), "Share today's stats"],
                    id="btn-share-card", color="primary", outline=True, size="sm",
                    style={"fontSize": "12px", "padding": "5px 14px", "borderRadius": "20px"},
                ),
                style={"textAlign": "center", "marginTop": "12px"},
            ),
            # Draggable share card overlay
            html.Div(
                id="share-card-modal",
                style={"display": "none", "position": "fixed", "top": "60px", "left": "50%",
                       "transform": "translateX(-50%)", "width": "min(420px, 96vw)",
                       "maxHeight": "90vh", "background": "#111", "borderRadius": "16px",
                       "boxShadow": "0 8px 40px rgba(0,0,0,0.7)", "zIndex": "10000",
                       "overflow": "hidden", "resize": "both"},
                children=[
                    # Drag handle header
                    html.Div([
                        html.Div("Share today's stats",
                                 style={"color": "white", "fontWeight": "600", "fontSize": "15px"}),
                        html.Div("✕", id="share-card-close",
                                 style={"color": "white", "cursor": "pointer", "fontSize": "20px",
                                        "padding": "0 4px", "lineHeight": "1"}),
                    ], id="share-card-drag-handle",
                       style={"display": "flex", "justifyContent": "space-between",
                              "alignItems": "center", "padding": "12px 16px",
                              "background": "#1a1a1a", "cursor": "grab",
                              "borderBottom": "1px solid rgba(255,255,255,0.1)"}),
                    html.Div(id="share-card-container",
                             style={"overflowY": "auto", "maxHeight": "calc(90vh - 50px)"}),
                ]
            ),
            html.Div(logout_button,
                     style={"display": "flex", "justifyContent": "flex-end",
                            "marginTop": "10px", "marginRight": "4px"}),
        ],
        style={"display": "block"},
    )

    calendar_view = html.Div(
        id="calendar-view",
        children=[
            html.H4("Training Program", className="mt-3"),
            html.P("Your scheduled sessions and athlete logging",
                   style={"color": "#6e6e6e", "fontSize": "13px", "margin": "-8px 0 12px 0"}),
            html.Div([
                html.Div([
                    dbc.Button("◀", id="calendar-prev", size="sm", color="secondary", outline=True, className="me-2"),
                    html.Div(id="calendar-window-label",
                             className="flex-grow-1 text-center small text-muted",
                             style={"minHeight": "24px"}),
                    dbc.Button("▶", id="calendar-next", size="sm", color="secondary", outline=True, className="ms-2"),
                ], className="d-flex align-items-center justify-content-between mb-2"),
                html.Div(id="calendar-grid", className="mb-4"),
            ]),
            html.Hr(),
            html.H4("Selected Session & Athlete Input", className="mt-3 mb-1"),
            html.P("Log your session data and generate coaching feedback",
                   style={"color": "#6e6e6e", "fontSize": "13px", "margin": "0 0 12px 0"}),
            html.Div(
                id="session-input-container",
                style={"display": "none"},
                children=[
                    dbc.Button("Close", id="close-session-button", color="secondary", outline=True,
                               size="sm", className="mb-3"),
                    dbc.Button("Reset", id="reset-session-button", color="warning", outline=True,
                               size="sm", className="mb-3 ms-2"),
                    html.H5(id="selected-date-header", className="mb-2"),
                    html.Div(
                        [html.Div(id="ctx-workout"), html.Div(id="ctx-focus"), html.Div(id="ctx-venue")],
                        id="session-context-wrapper",
                    ),
                    dbc.Row([
                        dbc.Col([
                            input_card([html.Label("Athlete Notes"),
                                        dcc.Textarea(id="athlete-notes",
                                                     placeholder="e.g., Last two reps were my best...",
                                                     style={"width": "100%", "height": "80px", "border": "none"})]),
                            input_card([html.Label("Sets × Reps × Load"),
                                        dcc.Textarea(id="sets-reps-load", placeholder="e.g., add here",
                                                     style={"width": "100%", "height": "80px", "border": "none"})]),
                            input_card([html.Label("Track Reps & Times"),
                                        dcc.Textarea(id="track-reps-times", placeholder="e.g., add here",
                                                     style={"width": "100%", "height": "80px", "border": "none"})]),
                            html.Div(
                                id="unplanned-session-fields",
                                style={"display": "none"},
                                children=[
                                    html.Div([
                                        html.I(className="bi bi-pencil-square me-2"),
                                        html.Strong("Session details not yet filled — please complete below"),
                                    ], style={"background": "#fff8e1", "border": "1px solid #ffe082",
                                              "borderRadius": "8px", "padding": "9px 13px",
                                              "marginBottom": "12px", "fontSize": "13px", "color": "#5d4037"}),
                                    input_card([html.Label("Workout / Session Type"),
                                                dcc.Input(id="unplanned-workout", type="text",
                                                          placeholder="e.g., Speed session, Gym, Tempo run",
                                                          style={"width": "100%", "border": "none",
                                                                 "fontSize": "14px", "padding": "4px 0"})]),
                                    input_card([html.Label("Focus"),
                                                dcc.Input(id="unplanned-focus", type="text",
                                                          placeholder="e.g., Max velocity, Lower body strength",
                                                          style={"width": "100%", "border": "none",
                                                                 "fontSize": "14px", "padding": "4px 0"})]),
                                    input_card([html.Label("Venue / Location"),
                                                dcc.Input(id="unplanned-venue", type="text",
                                                          placeholder="e.g., Track, Gym, Park",
                                                          style={"width": "100%", "border": "none",
                                                                 "fontSize": "14px", "padding": "4px 0"})]),
                                    input_card([html.Label("Key Distance (m)"),
                                                dcc.Input(id="unplanned-key-distance", type="text",
                                                          placeholder="e.g., 30, 60, 100",
                                                          style={"width": "100%", "border": "none",
                                                                 "fontSize": "14px", "padding": "4px 0"})]),
                                    dbc.Row([
                                        dbc.Col([input_card([html.Label("Duration (min)"),
                                                             dcc.Input(id="unplanned-duration", type="number",
                                                                       min=1, max=300, step=1, placeholder="e.g., 60",
                                                                       style={"width": "100%", "border": "none",
                                                                              "fontSize": "14px", "padding": "4px 0"})])],
                                                width=6),
                                        dbc.Col([input_card([html.Label("Planned sRPE"),
                                                             dcc.Input(id="unplanned-srpe", type="number",
                                                                       min=1, max=10, step=0.5, placeholder="e.g., 6",
                                                                       style={"width": "100%", "border": "none",
                                                                              "fontSize": "14px", "padding": "4px 0"})])],
                                                width=6),
                                    ]),
                                ],
                            ),
                            dbc.Label("Session RPE (1 = very easy, 5 = maximal)"),
                            dcc.Slider(id="slider-session-rpe",     min=1, max=5, step=1, value=3),
                            dbc.Label("Session Quality (1 = poor, 5 = excellent)"),
                            dcc.Slider(id="slider-session-quality", min=1, max=5, step=1, value=3),
                            dbc.Label("Sleep (1 = tired, 5 = well-rested)"),
                            dcc.Slider(id="slider-sleep",           min=1, max=5, step=1, value=3),
                            dbc.Label("Mood (1 = sad, 5 = upbeat)"),
                            dcc.Slider(id="slider-mood",            min=1, max=5, step=1, value=3),
                            dbc.Label("Fatigue (1 = low energy, 5 = energetic)"),
                            dcc.Slider(id="slider-fatigue",         min=1, max=5, step=1, value=3),
                            dbc.Label("Soreness (1 = low, 5 = high)"),
                            dcc.Slider(id="slider-soreness",        min=1, max=5, step=1, value=3),
                        ], md=6),
                        dbc.Col([
                            html.Div([
                                dbc.Label("Primary Coaching Feedback (select one)"),
                                dcc.RadioItems(
                                    id="ai-mode-1",
                                    options=[
                                        {"label": "Acceleration & Speed",  "value": "Acceleration & Speed Coach"},
                                        {"label": "Tempo & Endurance",     "value": "Tempo & Endurance Coach"},
                                        {"label": "Technical Sprint",      "value": "Technical Sprint Coach"},
                                        {"label": "Strength & Power",      "value": "Strength & Power Coach"},
                                        {"label": "Recovery & Readiness",  "value": "Recovery & Readiness Coach"},
                                    ],
                                    value=None, className="coach-radio",
                                    inputClassName="coach-radio-input", labelClassName="coach-radio-label",
                                ),
                            ], style={"marginBottom": "16px"}),
                            html.Div([
                                dbc.Label("Secondary Coaching Feedback (select one)"),
                                dcc.RadioItems(
                                    id="ai-mode-2",
                                    options=[
                                        {"label": "Acceleration & Speed",  "value": "Acceleration & Speed Coach"},
                                        {"label": "Tempo & Endurance",     "value": "Tempo & Endurance Coach"},
                                        {"label": "Technical Sprint",      "value": "Technical Sprint Coach"},
                                        {"label": "Strength & Power",      "value": "Strength & Power Coach"},
                                        {"label": "Recovery & Readiness",  "value": "Recovery & Readiness Coach"},
                                    ],
                                    value=None, className="coach-radio",
                                    inputClassName="coach-radio-input", labelClassName="coach-radio-label",
                                ),
                            ], style={"marginBottom": "4px"}),
                            dbc.Button("Log Session & Generate Coaching Feedback",
                                       id="btn-generate-ai", className="mt-4 w-100 ai-save-btn"),
                            html.Div(id="save-status", className="mt-2"),
                            dcc.Loading(id="ai-loader", type="circle", children=[
                                html.Div(id="ai-suggestion-1", className="mt-3"),
                                html.Div(id="ai-suggestion-2", className="mt-3"),
                            ]),
                        ], md=6),
                    ]),
                ],
            ),
        ],
    )

    graphs_view = html.Div(
        id="graphs-view",
        style={"display": "none"},
        children=[
            html.H4("Training Analytics", className="mt-3 mb-1"),
            html.P("Load, wellness trends and speed/tempo volumes",
                   style={"color": "#6e6e6e", "fontSize": "13px", "margin": "0 0 20px 0"}),
            dbc.Row([
                dbc.Col([
                    html.Div("View mode", className="fw-semibold text-muted mb-1"),
                    dcc.RadioItems(id="view-mode",
                                   options=[{"label": "Weekly", "value": "weekly"},
                                            {"label": "Daily",  "value": "daily"}],
                                   value="weekly", inline=True, className="view-toggle",
                                   inputClassName="view-toggle-input", labelClassName="view-toggle-label"),
                ], width="auto"),
                dbc.Col(
                    dbc.Button("Refresh", id="refresh-btn", color="light", size="sm", className="refresh-btn"),
                    width="auto", className="d-flex align-items-end",
                ),
            ], className="g-3 align-items-end mb-4"),
            dcc.Graph(id="load-plot",      config={"displayModeBar": False}),
            dcc.Graph(id="wellness-plot",  config={"displayModeBar": False}),
            dcc.Graph(id="speedtempo-plot",config={"displayModeBar": False}),
        ],
    )

    ai_view = html.Div(id="ai-view", style={"display": "none"}, children=[
        html.Div(className="page-wrap", children=[
            html.Div(style={"marginBottom": "16px"}, children=[
                html.Div(className="d-flex align-items-center justify-content-between mb-1", children=[
                    html.H4("Training Session Builder", style={"margin": 0, "fontWeight": 600}),
                    html.Div(style={"background": "rgba(30,136,229,0.10)", "border": "1px solid rgba(30,136,229,0.25)",
                                    "color": "#1e88e5", "fontSize": "11px", "padding": "4px 10px",
                                    "borderRadius": "999px", "fontWeight": 700,
                                    "display": "inline-flex", "alignItems": "center", "gap": "6px"},
                             children=[html.Div(className="pill-dot"), html.Span("ACI")]),
                ]),
                html.P("AI-generated sessions built around your recent data.",
                       style={"color": "#6e6e6e", "fontSize": "13px", "margin": "2px 0 0 0"}),
            ]),
            dbc.Row(className="g-3", children=[
                dbc.Col(md=5, children=[
                    dbc.Card(className="premium-card", children=[
                        dbc.CardHeader("Session Inputs"),
                        dbc.CardBody(children=[
                            html.Div("Keep the goal tight and specific. The plan will follow your recent trends.",
                                     className="card-muted"),
                            html.Div(className="divider-soft"),
                            dbc.Label("Coaching Focus"),
                            dcc.Dropdown(id="ai-plan-coach",
                                         options=[{"label": k, "value": k} for k in [
                                             "Acceleration & Speed Coach", "Tempo & Endurance Coach",
                                             "Technical Sprint Coach", "Strength & Power Coach",
                                             "Recovery & Readiness Coach",
                                         ]],
                                         placeholder="Select your coach style", clearable=False,
                                         className="aw-dropdown premium-input"),
                            html.Br(),
                            dbc.Label("Main session goal / focus"),
                            dcc.Textarea(id="ai-plan-goal",
                                         placeholder="e.g., Lower body speed-strength + low CNS cost.",
                                         className="form-control premium-textarea"),
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

    session_log_modal = dbc.Modal(
        [
            dbc.ModalHeader(dbc.ModalTitle(html.Div(id="popup-modal-title")), close_button=True),
            dbc.ModalBody(id="popup-modal-body"),
            dbc.ModalFooter([
                dbc.Button("Edit this session", id="session-log-popup-edit",
                           color="primary", size="sm", n_clicks=0),
                dbc.Button("Close", id="session-log-popup-close",
                           color="secondary", outline=True, size="sm", n_clicks=0),
            ]),
        ],
        id="session-log-modal", is_open=False, scrollable=True, size="lg",
    )

    # Squad view — coach only, but squad-cards-container always in DOM for callback stability
    squad_view = html.Div(
        id="squad-view",
        style={"display": "none"},
        children=[
            html.H4("Squad Overview", className="mt-3 mb-1"),
            html.P("All athletes — readiness, wellness and session status",
                   style={"color": "#6e6e6e", "fontSize": "13px", "margin": "0 0 16px 0"}),
            dbc.Button(
                [html.I(className="bi bi-arrow-clockwise me-2"), "Load squad data"],
                id="squad-refresh-btn", color="primary", outline=True, size="sm",
                className="mb-3"
            ) if is_coach else html.Div(id="squad-refresh-btn", style={"display": "none"}),
            dcc.Loading(type="circle", children=html.Div(
                id="squad-cards-container",
                children=html.Div("Tap 'Load squad data' to refresh.",
                                  className="text-muted") if is_coach else None
            )),
        ],
    )

    # Bottom nav — add Squad tab for coaches
    nav_cols = [
        dbc.Col(html.Div([html.I(id="icon-home",     className="bi bi-house nav-icon"),
                          html.Div("Home",     className="nav-label")],
                         id="nav-home",     n_clicks=0, className="nav-item")),
        dbc.Col(html.Div([html.I(id="icon-calendar", className="bi bi-calendar-event nav-icon"),
                          html.Div("Calendar", className="nav-label")],
                         id="nav-calendar", n_clicks=0, className="nav-item")),
        dbc.Col(html.Div([html.I(id="icon-graphs",   className="bi bi-bar-chart-line nav-icon"),
                          html.Div("Graphs",   className="nav-label")],
                         id="nav-graphs",   n_clicks=0, className="nav-item")),
        dbc.Col(html.Div([html.I(id="icon-ai",       className="bi bi-cpu nav-icon"),
                          html.Div("AI",       className="nav-label")],
                         id="nav-ai",       n_clicks=0, className="nav-item")),
    ]
    if is_coach:
        nav_cols.append(
            dbc.Col(html.Div([html.I(id="icon-squad", className="bi bi-people nav-icon"),
                              html.Div("Squad", className="nav-label")],
                             id="nav-squad", n_clicks=0, className="nav-item"))
        )
    else:
        nav_cols.append(dbc.Col(html.Div(id="nav-squad", n_clicks=0, style={"display": "none"})))

    bottom_nav = html.Div(
        [
            html.Div(id="nav-underline", className="nav-underline"),
            dbc.Row(nav_cols, className="g-0"),
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
            squad_view,
            session_log_modal,
            bottom_nav,
        ],
        fluid=True, className="pb-5 app-shell",
    )


app.layout = html.Div([
    dcc.Location(id="url", refresh=False),
    dcc.Store(id="auth-store", storage_type="session"),
    dcc.Store(id="active-tab-store", data="home"),
    html.Div(
        id="splash-screen",
        children=[
            html.Img(src="/assets/app_icon.png", className="splash-logo"),
            html.H2("Adaptive Coaching Intelligence", className="splash-title"),
            html.P("Empowering performance through athlete insight", className="splash-subtitle"),
            html.Div(className="spinner"),
        ]
    ),
    html.Div(id="page-content", style={"display": "block"}, children=build_login_layout()),
])


# ============================================================
#  Callbacks
# ============================================================

@app.callback(Output("page-content", "children"), Input("auth-store", "data"))
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
    prevent_initial_call=True,
)
def do_login(n_clicks, username, password):
    if not n_clicks:
        raise PreventUpdate
    if not username or not password:
        return {"authed": False}, "Enter both username and password."

    for athlete_key, info in USER_LOGINS.items():
        u    = str(info.get("username", "")).strip().lower()
        p    = str(info.get("password", "")).strip()
        role = str(info.get("role", "athlete")).lower()
        sheet = info.get("sheet", "")
        if username.strip().lower() == u and password.strip() == p:
            return ({"authed": True, "username": username.strip(),
                     "athlete_name": athlete_key, "athlete_sheet": sheet,
                     "is_coach": (role == "coach")}, "")

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
    [Output("home-view",     "style"),
     Output("calendar-view","style"),
     Output("graphs-view",  "style"),
     Output("ai-view",      "style"),
     Output("squad-view",   "style"),
     Output("bottom-nav-click", "data")],
    [Input("nav-home",     "n_clicks"),
     Input("nav-calendar", "n_clicks"),
     Input("nav-graphs",   "n_clicks"),
     Input("nav-ai",       "n_clicks"),
     Input("nav-squad",    "n_clicks")],
)
def show_section(h, c, g, a, s):
    ctx = callback_context
    tab = "home" if not ctx.triggered else ctx.triggered[0]["prop_id"].split(".")[0].replace("nav-", "")
    out = [{"display": "block"} if key == tab else {"display": "none"}
           for key in ["home", "calendar", "graphs", "ai", "squad"]]
    out.append(tab)
    return out


@app.callback(
    Output("calendar-window-start",  "data"),
    Output("calendar-window-label", "children"),
    Input("athlete-dropdown",  "value"),
    Input("calendar-prev",     "n_clicks"),
    Input("calendar-next",     "n_clicks"),
    State("calendar-window-start", "data"),
)
def update_calendar_window(athlete_tab, prev_clicks, next_clicks, current_month):
    today = today_adl()
    month_date = today.replace(day=1) if current_month is None else pd.to_datetime(current_month).date()
    triggered  = callback_context.triggered[0]["prop_id"].split(".")[0]

    if triggered == "calendar-prev":
        m = month_date.month - 1
        y = month_date.year
        if m == 0: m, y = 12, y - 1
        month_date = dt.date(y, m, 1)
    elif triggered == "calendar-next":
        m = month_date.month + 1
        y = month_date.year
        if m == 13: m, y = 1, y + 1
        month_date = dt.date(y, m, 1)

    return str(month_date), month_date.strftime("%B %Y")


@app.callback(
    Output("calendar-grid", "children"),
    Input("athlete-dropdown",       "value"),
    Input("calendar-window-start",  "data"),
    Input("selected-date-store",    "data"),
)
def update_calendar(athlete_tab, window_start, selected_date):
    if not athlete_tab:
        return "Select athlete."
    df = load_tab(athlete_tab)
    month_date = pd.to_datetime(window_start).date().replace(day=1) if window_start else dt.date.today().replace(day=1)
    return build_month_calendar(df, month_date, selected_date)


@app.callback(
    Output("today-date",                  "children"),
    Output("weekly-dial-container",       "children"),
    Output("streak-dial-container",       "children"),
    Output("neuromuscular-dial-container","children"),
    Output("readiness-dial-container",    "children"),
    Output("load-plot",      "figure"),
    Output("wellness-plot",  "figure"),
    Output("speedtempo-plot","figure"),
    Input("athlete-dropdown", "value"),
    Input("view-mode",        "value"),
    Input("refresh-btn",      "n_clicks"),
)
def update_dashboard(athlete_id, view_mode, n_clicks):
    if not athlete_id:
        today_date_str = today_adl().strftime("%d %b %Y")
        return (today_date_str,
                dial_flip(apple_sessions_ring(None),       "Weekly Training Exposure",    "—"),
                dial_flip(streak_dial(0),                  "Training Streak",             "—"),
                dial_flip(apple_neuromuscular_ring(None),  "Neuromuscular State",         "—"),
                dial_flip(apple_readiness_ring(None),      "Training Readiness Index",    "—"),
                go.Figure(), go.Figure(), go.Figure())

    today          = today_adl()
    today_date_str = today.strftime("%d %b %Y")
    df             = load_tab(athlete_id)

    dow            = today.weekday()
    days_since_sat = (dow - 5) % 7
    week_start     = today - dt.timedelta(days=days_since_sat)
    week_end       = week_start + dt.timedelta(days=6)

    if df is None or df.empty:
        weekly_ui    = dial_flip(apple_sessions_ring(None),      " ", "No data yet.")
        streak_ui    = dial_flip(streak_dial(0),                 " ", "No data yet.")
        readiness_ui = dial_flip(apple_readiness_ring(None),     " ", "No data yet.")
        neuro_ui     = dial_flip(apple_neuromuscular_ring(None), " ", "No data yet.")
        empty_fig    = go.Figure()
        empty_fig.update_layout(**MOBILE_PLOT_LAYOUT)
        return today_date_str, weekly_ui, streak_ui, neuro_ui, readiness_ui, empty_fig, empty_fig, empty_fig

    try:
        load_fig     = build_load_plot(df, view_mode)
        wellness_fig = build_wellness_plot(df, view_mode)
        speed_fig    = build_speed_tempo_plot(df, view_mode)
    except Exception as e:
        print("❌ Plot build error:", e)
        load_fig = wellness_fig = speed_fig = go.Figure()
        load_fig.update_layout(title=f"Plot error: {e}")

    planned_count   = count_planned_sessions_in_week(df, week_start, week_end)
    completed_count = count_logged_sessions_in_week(df, week_start, week_end)

    if planned_count > 0:
        weekly_exposure_pct = int(round(min(max((completed_count / planned_count) * 100, 0), 100)))
    else:
        weekly_exposure_pct = None

    streak, best = compute_streaks(df)

    NEURO_WINDOW    = 14
    NEURO_DECAY     = 3.5
    NEURO_MAX_PEN   = 35.0

    garmin_token, garmin_secret = garmin_get_athlete_tokens(df)
    if garmin_token and garmin_secret:
        try:
            raw_garmin = garmin_fetch_today(garmin_token, garmin_secret, today)
            parsed     = garmin_parse_to_scales(raw_garmin)
            if parsed.get("Garmin_Synced") == "yes":
                df2 = df.copy()
                df2["Date"] = pd.to_datetime(df2["Date"], errors="coerce").dt.date
                today_matches = df2.index[df2["Date"] == today].tolist()
                if today_matches:
                    write_row(athlete_id, today_matches[0], parsed)
                    df = load_tab(athlete_id)
        except Exception as e:
            print(f"⚠️ Garmin fetch failed for {athlete_id}: {e}")

    today_enriched = garmin_enrich_df_row(df, today)
    data_source    = today_enriched["source"]

    # Use shared helper — guarantees same result as share card
    neuro_val = compute_neuro_for_athlete(df, today)

    df_time = df.copy()
    df_time["Date"] = pd.to_datetime(df_time["Date"], errors="coerce")
    df_time = df_time.sort_values("Date")
    df_time = df_time[~df_time["Date"].duplicated(keep="last")]
    df_time = df_time.set_index("Date")
    full_range = pd.date_range(start=df_time.index.min(), end=today, freq="D")
    df_time    = df_time.reindex(full_range)

    load_series    = pd.to_numeric(df_time.get("Load"), errors="coerce")

    # Prefer post-session RPE; fall back to planned RPE col if no athlete logs yet
    rpe_post = pd.to_numeric(df_time.get("RPE_Post_Session"), errors="coerce")
    rpe_plan = pd.to_numeric(df_time.get("RPE"), errors="coerce")
    if rpe_post.notna().sum() > 0:
        rpe_series = rpe_post
    elif rpe_plan.notna().sum() > 0:
        # Scale planned RPE (1-10 sRPE scale) down to 1-5 if needed
        rpe_plan_vals = rpe_plan.dropna()
        if not rpe_plan_vals.empty and rpe_plan_vals.max() > 5:
            rpe_series = rpe_plan / 2.0
        else:
            rpe_series = rpe_plan
    else:
        rpe_series = pd.Series(dtype=float, index=df_time.index)

    quality_series = pd.to_numeric(df_time.get("Session_1_5"), errors="coerce")
    readiness_val  = calc_daily_readiness(load_series, rpe_series, quality_series, span=7)

    _src = " · via Garmin" if data_source == "garmin" else ""

    weekly_ui = dial_flip(
        apple_sessions_ring(weekly_exposure_pct), " ",
        f"Planned: {planned_count}  •  Completed: {completed_count}\n"
        f"Exposure: {weekly_exposure_pct if weekly_exposure_pct is not None else '—'}/100\n"
        "100 = every planned session logged."
    )
    streak_ui = dial_flip(
        streak_dial(streak), " ",
        f"Current streak = {streak} consecutive days logged.\n"
        "Keep low-cost work going to protect the streak."
    )
    neuro_ui = dial_flip(
        apple_neuromuscular_ring(neuro_val), " ",
        f"Neuromuscular Readiness reflects nervous system state using fatigue, mood, sleep, soreness.{_src}"
    )
    readiness_ui = dial_flip(
        apple_readiness_ring(readiness_val), " ",
        f"Daily Readiness reflects load-to-recovery balance vs your recent baseline.{_src}"
    )

    return today_date_str, weekly_ui, streak_ui, neuro_ui, readiness_ui, load_fig, wellness_fig, speed_fig


@app.callback(
    Output("session-log-modal",        "is_open"),
    Output("popup-modal-title",        "children"),
    Output("popup-modal-body",         "children"),
    Output("session-input-container",  "style"),
    Output("selected-date-store",      "data"),
    Output("selected-date-header",     "children"),
    Input({"type": "calendar-day", "date": ALL}, "n_clicks"),
    Input("session-log-popup-close",   "n_clicks"),
    Input("session-log-popup-edit",    "n_clicks"),
    State("athlete-dropdown",          "value"),
    prevent_initial_call=True,
)
def on_day_click(n_clicks_list, close_n, edit_n, athlete_name):
    ctx = callback_context
    if not ctx.triggered:
        raise PreventUpdate

    triggered_id = ctx.triggered[0]["prop_id"].split(".")[0]

    if triggered_id == "session-log-popup-close":
        return False, no_update, no_update, no_update, no_update, no_update
    if triggered_id == "session-log-popup-edit":
        return False, no_update, no_update, {"display": "block"}, no_update, no_update

    if not n_clicks_list or all((n or 0) == 0 for n in n_clicks_list):
        raise PreventUpdate

    try:
        triggered = json.loads(triggered_id)
    except Exception:
        raise PreventUpdate

    if triggered.get("type") != "calendar-day":
        raise PreventUpdate

    clicked_date_str = triggered["date"]
    clicked_date     = pd.to_datetime(clicked_date_str, errors="coerce").date()
    header           = html.H5(f"Selected session: {clicked_date_str}")

    if not athlete_name:
        return False, no_update, no_update, {"display": "block"}, clicked_date_str, header

    df = load_tab(athlete_name)
    if df is None or df.empty or "Date" not in df.columns:
        return False, no_update, no_update, {"display": "block"}, clicked_date_str, header

    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    status = get_day_status(df, clicked_date)

    if not status.get("logged", False):
        return False, no_update, no_update, {"display": "block"}, clicked_date_str, header

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

    workout = v("Workout"); focus = v("Focus"); venue = v("Venue")
    notes   = v("Athlete_Notes"); sets = v("Sets_Reps_Load"); track = v("Track_Reps_Times")
    ai1     = v("AI_Suggestion_1"); ai2 = v("AI_Suggestion_2")

    sleep_v    = num("Sleep_1_5");    fatigue_v  = num("Fatigue_1_5")
    mood_v     = num("Mood_1_5");     soreness_v = num("Soreness_1_5")
    rpe_v      = num("RPE_Post_Session"); quality_v = num("Session_1_5")

    def metric_box(label, val, invert=False):
        # Traffic light: higher=better for sleep/fatigue/mood/quality
        # invert=True for soreness/RPE where lower=better
        if val is None:
            bg, dot, txt = "#f5f5f5", "#ccc", "#ccc"
        else:
            score = (6 - val) if invert else val  # flip so 1=worst always
            if score >= 4:
                bg, dot, txt = "#e8f5e9", "#2E7D32", "#1b5e20"   # green
            elif score == 3:
                bg, dot, txt = "#fff8e1", "#F9A825", "#5d4037"   # amber
            else:
                bg, dot, txt = "#ffebee", "#C62828", "#B71C1C"   # red
        return html.Div([
            html.Div(label, style={"fontSize": "11px", "color": "#888", "marginBottom": "2px"}),
            html.Div([str(val), html.Span("/5", style={"fontSize": "11px", "color": txt, "opacity": "0.7"})],
                     style={"fontSize": "20px", "fontWeight": "700", "color": txt})
            if val is not None else html.Div("—", style={"fontSize": "16px", "color": "#ccc"}),
        ], style={"background": bg, "borderRadius": "8px", "padding": "8px 10px",
                  "border": f"1px solid {dot}33"})

    def section_label(text):
        return html.Div(text, style={"fontSize": "11px", "fontWeight": "600", "color": "#999",
                                     "textTransform": "uppercase", "letterSpacing": "0.05em",
                                     "margin": "14px 0 6px"})

    pill_style = {"display": "inline-block", "fontSize": "12px", "padding": "3px 10px",
                  "borderRadius": "999px", "background": "#f0f0f0", "color": "#555",
                  "marginRight": "6px", "marginBottom": "4px"}

    body = []
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
    body.append(section_label("Wellness"))
    body.append(html.Div([
        metric_box("Sleep",    sleep_v,    invert=False),
        metric_box("Fatigue",  fatigue_v,  invert=False),
        metric_box("Mood",     mood_v,     invert=False),
        metric_box("Soreness", soreness_v, invert=True),
        metric_box("Post RPE", rpe_v,      invert=True),
        metric_box("Quality",  quality_v,  invert=False),
    ], style={"display": "grid", "gridTemplateColumns": "repeat(3, 1fr)", "gap": "8px"}))

    if notes != "—":
        body.append(section_label("Athlete notes"))
        body.append(html.Div(notes, style={"background": "#f5f5f5", "borderRadius": "8px",
                                           "padding": "10px 12px", "fontSize": "13px",
                                           "color": "#444", "lineHeight": "1.5"}))

    sets_url  = str(row.get("Sets_Reps_Load_url",  "") or "").strip()
    track_url = str(row.get("Track_Reps_Times_url","") or "").strip()

    # Rectangle style matching athlete notes — no pills
    rect_base = {"background": "#f5f5f5", "borderRadius": "8px", "padding": "10px 12px",
                 "fontSize": "13px", "color": "#444", "lineHeight": "1.5", "marginBottom": "6px"}
    rect_link = {"background": "#e3f2fd", "borderRadius": "8px", "padding": "10px 12px",
                 "fontSize": "13px", "color": "#1565C0", "lineHeight": "1.5", "marginBottom": "6px",
                 "textDecoration": "none", "display": "block"}

    gym_items = []
    if sets != "—":
        if sets_url and sets_url != "None":
            gym_items.append(html.A([
                html.Span("Gym  ", style={"fontWeight": "600", "fontSize": "11px",
                                          "color": "#1565C0", "textTransform": "uppercase",
                                          "letterSpacing": "0.04em", "marginRight": "6px"}),
                sets,
            ], href=sets_url, target="_blank", style=rect_link))
        else:
            gym_items.append(html.Div([
                html.Span("Gym  ", style={"fontWeight": "600", "fontSize": "11px",
                                          "color": "#888", "textTransform": "uppercase",
                                          "letterSpacing": "0.04em", "marginRight": "6px"}),
                sets,
            ], style=rect_base))
    if track != "—":
        if track_url and track_url != "None":
            gym_items.append(html.A([
                html.Span("Track  ", style={"fontWeight": "600", "fontSize": "11px",
                                             "color": "#1565C0", "textTransform": "uppercase",
                                             "letterSpacing": "0.04em", "marginRight": "6px"}),
                track,
            ], href=track_url, target="_blank", style=rect_link))
        else:
            gym_items.append(html.Div([
                html.Span("Track  ", style={"fontWeight": "600", "fontSize": "11px",
                                             "color": "#888", "textTransform": "uppercase",
                                             "letterSpacing": "0.04em", "marginRight": "6px"}),
                track,
            ], style=rect_base))
    if gym_items:
        body.append(section_label("Gym / Track"))
        body.append(html.Div(gym_items))

    if ai1 != "—":
        body.append(section_label("Primary Coaching Feedback"))
        body.append(html.Div(ai1, style={"borderLeft": "3px solid #1565C0", "background": "#e3f2fd",
                                         "borderRadius": "0 8px 8px 0", "padding": "10px 12px",
                                         "fontSize": "12px", "color": "#0d47a1", "lineHeight": "1.5"}))
    if ai2 != "—":
        body.append(section_label("Secondary Coaching Feedback"))
        body.append(html.Div(ai2, style={"borderLeft": "3px solid #2E7D32", "background": "#e8f5e9",
                                         "borderRadius": "0 8px 8px 0", "padding": "10px 12px",
                                         "fontSize": "12px", "color": "#1b5e20", "lineHeight": "1.5"}))

    modal_title = html.Div([
        html.Span(clicked_date.strftime("%A, %d %B %Y"),
                  style={"fontSize": "15px", "fontWeight": "600", "marginRight": "10px"}),
        html.Span("Logged", style={"fontSize": "11px", "background": "#e8f5e9", "color": "#2E7D32",
                                   "padding": "2px 8px", "borderRadius": "999px", "fontWeight": "600"}),
    ])

    return True, modal_title, html.Div(body), no_update, clicked_date_str, header


@app.callback(
    Output("unplanned-session-fields", "style"),
    Input("selected-date-store", "data"),
    State("athlete-dropdown",    "value"),
    prevent_initial_call=True,
)
def toggle_unplanned_fields(selected_date, athlete_name):
    if not selected_date or not athlete_name:
        raise PreventUpdate
    try:
        df = load_tab(athlete_name)
        if df is None or df.empty or "Date" not in df.columns:
            return {"display": "block"}
        df = df.copy()
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
        d = pd.to_datetime(selected_date, errors="coerce").date()
        match = df[df["Date"] == d]
        if match.empty:
            return {"display": "block"}
        row = match.iloc[-1]
        workout_val = str(row.get("Workout", "")).strip().lower()
        invalid = {"", "nan", "none", "nil", "-", "—", "tbc"}
        return {"display": "none"} if workout_val not in invalid else {"display": "block"}
    except Exception:
        return {"display": "none"}


@app.callback(
    Output("ctx-workout", "children"),
    Output("ctx-focus",   "children"),
    Output("ctx-venue",   "children"),
    Input("selected-date-store", "data"),
    State("athlete-dropdown",    "value"),
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
        if col not in row.index: return None
        v = row[col]
        if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "nan", "None"):
            return None
        return v

    workout      = val("Workout");      key_distance = val("Key_Distance")
    focus        = val("Focus");        srpe         = val("sRPE")
    duration     = val("Duration");     load         = val("Load")
    venue        = val("Venue");        notes        = val("Notes")
    workout_url  = str(row.get("Workout_url", "") or "").strip()
    focus_url    = str(row.get("Focus_url",   "") or "").strip()

    workout_card = [
        html.Div("🏃 Workout", className="ctx-title"),
        html.A(workout, href=workout_url, target="_blank", className="ctx-main",
               style={"color": "#1565C0", "textDecoration": "underline"})
        if (workout and workout_url and workout_url != "None")
        else html.Div(workout or "—", className="ctx-main"),
        html.Div(f"📏 Key Distance: {key_distance}", className="ctx-sub") if key_distance else None,
    ]
    focus_card = [
        html.Div("🎯 Session Focus", className="ctx-title"),
        html.A(focus, href=focus_url, target="_blank", className="ctx-main",
               style={"color": "#1565C0", "textDecoration": "underline"})
        if (focus and focus_url and focus_url != "None")
        else html.Div(focus or "—", className="ctx-main"),
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


@app.callback(
    [Output("ai-suggestion-1", "children"),
     Output("ai-suggestion-2", "children"),
     Output("save-status",     "children")],
    Input("btn-generate-ai",   "n_clicks"),
    [State("athlete-dropdown",       "value"),
     State("selected-date-store",    "data"),
     State("ai-mode-1",              "value"),
     State("ai-mode-2",              "value"),
     State("athlete-notes",          "value"),
     State("sets-reps-load",         "value"),
     State("track-reps-times",       "value"),
     State("unplanned-workout",      "value"),
     State("unplanned-focus",        "value"),
     State("unplanned-venue",        "value"),
     State("unplanned-key-distance", "value"),
     State("unplanned-duration",     "value"),
     State("unplanned-srpe",         "value"),
     Input("slider-session-rpe",     "value"),
     Input("slider-session-quality", "value"),
     Input("slider-sleep",           "value"),
     Input("slider-fatigue",         "value"),
     Input("slider-mood",            "value"),
     Input("slider-soreness",        "value")],
    prevent_initial_call=True,
)
def save_and_ai(
    n_clicks, athlete_name, selected_date,
    ai_mode_1, ai_mode_2,
    notes, sets_reps_load, track_reps_times,
    unplanned_workout, unplanned_focus, unplanned_venue,
    unplanned_key_distance, unplanned_duration, unplanned_srpe,
    rpe, session_quality, sleep, fatigue, mood, soreness,
):
    if not n_clicks:          raise PreventUpdate
    if not athlete_name:      return no_update, no_update, "⚠️ Please select an athlete first."
    if not ai_mode_1 or not ai_mode_2:
        return no_update, no_update, "⚠️ Please select coaching feedback."
    if not selected_date:     return no_update, no_update, "⚠️ Please select a date from the calendar first."

    rpe             = 3.0 if rpe             is None else float(rpe)
    session_quality = 3.0 if session_quality is None else float(session_quality)
    sleep           = 3.0 if sleep           is None else float(sleep)
    fatigue         = 3.0 if fatigue         is None else float(fatigue)
    mood            = 3.0 if mood            is None else float(mood)
    soreness        = 3.0 if soreness        is None else float(soreness)

    notes            = (notes            or "").strip()
    sets_reps_load   = (sets_reps_load   or "").strip()
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
        try:
            _dur  = int(unplanned_duration) if unplanned_duration else None
            _srpe = float(unplanned_srpe)   if unplanned_srpe    else None
            _load = round(_srpe * _dur, 1)  if (_srpe and _dur)  else None
            new_payload = {
                "Date":         str(selected_date_dt),
                "Workout":      (unplanned_workout or "").strip() or "Unplanned session",
                "Focus":        (unplanned_focus   or "").strip(),
                "Venue":        (unplanned_venue   or "").strip(),
                "Key_Distance": str((unplanned_key_distance or "")).strip(),
                "Duration":     str(_dur)  if _dur  else "",
                "sRPE":         str(_srpe) if _srpe else "",
                "Load":         str(_load) if _load else "",
            }
            row_idx = append_row_for_date(athlete_name, selected_date_dt, new_payload)
            df = load_tab(athlete_name)
            if df is not None and not df.empty:
                df = df.copy()
                df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
                row_matches = df.index[df["Date"] == selected_date_dt].tolist()
                if row_matches: row_idx = row_matches[0]
        except Exception as e:
            return no_update, no_update, f"❌ Could not create row: {e}"
    else:
        row_idx = row_matches[0]
        row = df.iloc[row_idx]
        existing_workout = str(row.get("Workout", "")).strip().lower()
        invalid_vals = {"", "nan", "none", "nil", "-", "—", "tbc"}
        if existing_workout in invalid_vals and any([
            unplanned_workout, unplanned_focus, unplanned_venue,
            unplanned_key_distance, unplanned_duration, unplanned_srpe
        ]):
            _dur2  = int(unplanned_duration) if unplanned_duration else None
            _srpe2 = float(unplanned_srpe)   if unplanned_srpe    else None
            _load2 = round(_srpe2 * _dur2, 1) if (_srpe2 and _dur2) else None
            detail_payload = {k: v for k, v in {
                "Workout":      (unplanned_workout or "").strip(),
                "Focus":        (unplanned_focus   or "").strip(),
                "Venue":        (unplanned_venue   or "").strip(),
                "Key_Distance": str((unplanned_key_distance or "")).strip(),
                "Duration":     str(_dur2)  if _dur2  else "",
                "sRPE":         str(_srpe2) if _srpe2 else "",
                "Load":         str(_load2) if _load2 else "",
            }.items() if v}
            try:
                write_row(athlete_name, row_idx, detail_payload)
            except Exception as e:
                print(f"⚠️ Could not write session details: {e}")

    ai1, ai2 = make_ai_suggestions(
        athlete_name=athlete_name, selected_date=selected_date_dt,
        session_rpe=rpe, session_quality=session_quality,
        sleep=sleep, fatigue=fatigue, mood=mood, soreness=soreness,
        notes=notes, sets_reps_load=sets_reps_load, track_reps_times=track_reps_times,
        ai_mode_1=ai_mode_1, ai_mode_2=ai_mode_2,
    )

    unplanned_extras = {}
    if unplanned_workout and unplanned_workout.strip(): unplanned_extras["Workout"]  = unplanned_workout.strip()
    if unplanned_focus   and unplanned_focus.strip():   unplanned_extras["Focus"]    = unplanned_focus.strip()
    if unplanned_venue   and unplanned_venue.strip():   unplanned_extras["Venue"]    = unplanned_venue.strip()
    if unplanned_duration:                              unplanned_extras["Duration"] = str(int(unplanned_duration))

    payload = {
        **unplanned_extras,
        "RPE_Post_Session": rpe, "Session_1_5": session_quality,
        "Sleep_1_5": sleep, "Fatigue_1_5": fatigue, "Mood_1_5": mood, "Soreness_1_5": soreness,
        "Athlete_Notes": notes, "Sets_Reps_Load": sets_reps_load, "Track_Reps_Times": track_reps_times,
        "AI_Suggestion_1": ai1, "AI_Suggestion_2": ai2,
        "Last_Updated": dt.datetime.now().isoformat(timespec="seconds"),
    }

    try:
        write_row(athlete_name, row_idx, payload)
    except Exception as e:
        return no_update, no_update, f"❌ Save failed: {e}"

    athlete_email   = safe(df, row_idx, "Athlete_email") if "Athlete_email" in df.columns else ""
    athlete_display = safe(df, row_idx, "Athlete", athlete_name)
    focus_val       = safe(df, row_idx, "Focus",   "")
    venue_val       = safe(df, row_idx, "Venue",   "")
    workout_val     = safe(df, row_idx, "Workout", "")
    status_msg      = "✅ Saved, coaching feedback generated & email sent to Coach."

    try:
        send_email_payload({
            "sheet_name": athlete_name, "row": row_idx + 1,
            "Athlete": athlete_display, "Date": str(selected_date_dt),
            "Focus": focus_val, "Venue": venue_val, "Workout": workout_val,
            "RPE_Post_Session": rpe, "Session_1_5": session_quality,
            "Sleep_1_5": sleep, "Fatigue_1_5": fatigue, "Mood_1_5": mood, "Soreness_1_5": soreness,
            "Athlete_Notes": notes, "Sets_Reps_Load": sets_reps_load, "Track_Reps_Times": track_reps_times,
            "AI_Suggestion_1": ai1, "AI_Suggestion_2": ai2, "Athlete_email": athlete_email,
        })
    except Exception as e:
        status_msg = f"⚠️ Saved + coaching feedback generated, but email failed: {e}"

    ai1_div = html.Div(html.Div([html.Div("💡 Coaching Feedback 1", className="ai-title"), html.P(ai1)],
                                 className="ai-card ai-card-green"))
    ai2_div = html.Div(html.Div([html.Div("💡 Coaching Feedback 2", className="ai-title"), html.P(ai2)],
                                 className="ai-card ai-card-blue"))

    return ai1_div, ai2_div, html.Span(
        status_msg,
        style={"color": "#2E7D32" if status_msg.startswith("✅") else "#C62828", "fontWeight": 600}
    )


@app.callback(
    [Output("athlete-dropdown",      "value"),
     Output("slider-session-rpe",    "value"),
     Output("slider-session-quality","value"),
     Output("slider-sleep",          "value"),
     Output("slider-fatigue",        "value"),
     Output("slider-mood",           "value"),
     Output("slider-soreness",       "value"),
     Output("athlete-notes",         "value"),
     Output("sets-reps-load",        "value"),
     Output("track-reps-times",      "value")],
    Input("reset-session-button", "n_clicks"),
    prevent_initial_call=True,
)
def reset_inputs(n):
    if not n: raise PreventUpdate
    return no_update, 3, 3, 3, 3, 3, 3, "", "", ""


app.clientside_callback(
    """
    function(val1, val2) {
        function styleRadioPills(containerSelector) {
            var labels = document.querySelectorAll(containerSelector + ' .coach-radio-label');
            var inputs = document.querySelectorAll(containerSelector + ' .coach-radio-input');
            labels.forEach(function(lbl, i) {
                var inp = inputs[i];
                var isChecked = inp && inp.checked;
                lbl.style.background  = isChecked ? '#1E88E5' : '#f8f8f8';
                lbl.style.borderColor = isChecked ? '#1E88E5' : '#d0d0d0';
                lbl.style.color       = isChecked ? 'white'   : '#444';
                lbl.style.fontWeight  = isChecked ? '600'     : 'normal';
            });
        }
        setTimeout(function() {
            styleRadioPills('#ai-mode-1');
            styleRadioPills('#ai-mode-2');
            document.querySelectorAll('.coach-radio').forEach(function(el) {
                var inputs = el.querySelectorAll('.coach-radio-input');
                var labels = el.querySelectorAll('.coach-radio-label');
                inputs.forEach(function(inp, i) {
                    if (labels[i]) {
                        labels[i].style.background  = inp.checked ? '#1E88E5' : '#f8f8f8';
                        labels[i].style.borderColor = inp.checked ? '#1E88E5' : '#d0d0d0';
                        labels[i].style.color       = inp.checked ? 'white'   : '#444';
                        labels[i].style.fontWeight  = inp.checked ? '600'     : 'normal';
                    }
                });
            });
        }, 50);
        return window.dash_clientside.no_update;
    }
    """,
    Output("active-tab-store", "data", allow_duplicate=True),
    Input("ai-mode-1", "value"),
    Input("ai-mode-2", "value"),
    prevent_initial_call=True,
)

app.clientside_callback(
    """
    function(activeTab){
        const tabs = ["home", "calendar", "graphs", "ai", "squad"];
        tabs.forEach(t => {
            const nav  = document.getElementById("nav-"  + t);
            const icon = document.getElementById("icon-" + t);
            if(nav)  nav.classList.remove("active");
            if(icon){ icon.classList.remove("bounce"); icon.classList.remove("wobble"); }
        });
        const active = document.getElementById("nav-"  + activeTab);
        const icon   = document.getElementById("icon-" + activeTab);
        if(active){
            active.classList.add("active");
            if(icon){
                icon.classList.add("bounce");
                setTimeout(() => icon.classList.add("wobble"), 120);
            }
            const underline = document.getElementById("nav-underline");
            const index = tabs.indexOf(activeTab);
            if(underline){ underline.style.transform = `translateX(${index * 100}%)`; }
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output("active-tab-store", "data"),
    Input("bottom-nav-click",  "data"),
)


@app.callback(
    Output("welcome-message", "children"),
    Input("athlete-dropdown", "value"),
    Input("today-date",       "children"),
    prevent_initial_call=True,
)
def update_welcome(athlete_id, _today):
    if not athlete_id:
        raise PreventUpdate

    first_name = athlete_id.strip().split()[0] if athlete_id.strip() else "Athlete"
    today      = today_adl()
    hour       = dt.datetime.now(ADL_TZ).hour
    greeting   = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"

    readiness_val = neuro_val = None
    streak = 0

    try:
        df = load_tab(athlete_id)
        if not df.empty:
            streak, _ = compute_streaks(df)

            df_time = df.copy()
            df_time["Date"] = pd.to_datetime(df_time["Date"], errors="coerce")
            df_time = df_time.sort_values("Date")
            df_time = df_time[~df_time["Date"].duplicated(keep="last")]
            df_time = df_time.set_index("Date")
            full_range = pd.date_range(start=df_time.index.min(), end=today, freq="D")
            df_time    = df_time.reindex(full_range)

            load_series    = pd.to_numeric(df_time.get("Load"), errors="coerce")
            rpe_post_w = pd.to_numeric(df_time.get("RPE_Post_Session"), errors="coerce")
            rpe_plan_w = pd.to_numeric(df_time.get("RPE"), errors="coerce")
            if rpe_post_w.notna().sum() > 0:
                rpe_series = rpe_post_w
            elif rpe_plan_w.notna().sum() > 0:
                rpe_vals_w = rpe_plan_w.dropna()
                rpe_series = rpe_plan_w / 2.0 if (not rpe_vals_w.empty and rpe_vals_w.max() > 5) else rpe_plan_w
            else:
                rpe_series = pd.Series(dtype=float, index=df_time.index)
            quality_series = pd.to_numeric(df_time.get("Session_1_5"), errors="coerce")
            readiness_val  = calc_daily_readiness(load_series, rpe_series, quality_series)

            df_neuro = df.copy()
            df_neuro["Date"] = pd.to_datetime(df_neuro["Date"], errors="coerce").dt.date
            df_neuro = df_neuro.sort_values("Date")
            recent_neuro = df_neuro[df_neuro["Date"] >= today - dt.timedelta(days=14)]

            def _last(frame, col):
                s = pd.to_numeric(frame.get(col, pd.Series(dtype=float)), errors="coerce").dropna()
                return float(s.iloc[-1]) if not s.empty else None

            sl = _last(recent_neuro, "Sleep_1_5");   fa = _last(recent_neuro, "Fatigue_1_5")
            so = _last(recent_neuro, "Soreness_1_5"); mo = _last(recent_neuro, "Mood_1_5")
            if all(v is not None for v in [sl, fa, so, mo]):
                neuro_val = calc_neuro_readiness(sl, fa, so, mo, history_df=recent_neuro)
    except Exception:
        streak = 0

    r = readiness_val if readiness_val is not None else 0
    n = neuro_val     if neuro_val     is not None else 0

    if readiness_val is None and neuro_val is None:
        color = "#6e6e6e"; icon = "—"; band = "no_data"
    elif r >= 75 and n >= 75:
        color = "#2E7D32"; icon = "↑"; band = "high"
    elif r >= 60 and n >= 60:
        color = "#1565C0"; icon = "→"; band = "good"
    elif r >= 40 or n >= 40:
        color = "#E65100"; icon = "↓"; band = "moderate"
    else:
        color = "#C62828"; icon = "⚠"; band = "low"

    streak_txt = f" • {streak}-day streak 🔥" if streak >= 3 else ""

    try:
        df_safe  = df if not df.empty else pd.DataFrame()
        summary  = build_context_summary(df_safe, days=7) if not df_safe.empty else "No data."
        wellness = build_wellness_flags(df_safe, days=7)  if not df_safe.empty else ""
        sys_msg  = (
            "You are a high-performance sprint and strength coach who knows this athlete well. "
            "You open every session with a brief, direct check-in — like a coach walking up before training. "
            "Write exactly TWO lines separated by a pipe character |:\n"
            "Line 1 (headline): Talk directly to the athlete. Reference their readiness score as a whole number. "
            "Sound like a coach who has looked at the data and has a read on where they are today — sharp, honest, no fluff.\n"
            "Line 2 (sub): One sentence of specific coaching context — what does the data mean for today? "
            "Reference a wellness score (rated X/5), load trend, streak, or a flag. Tell them what to do with it.\n"
            "CRITICAL: Sleep/fatigue/mood/soreness are 1-5 SCALE scores, not hours or minutes. Say rated X/5 not X hours. "
            "BANNED words: greatness, dedication, potential, journey, warrior, champion, champions, amazing, incredible, outstanding, path, destiny, mindset, process. "
            "Tone: like a trusted coach — direct, warm, grounded in numbers. No hype, no corporate wellness speak. "
            "No hashtags, no exclamation marks, no emoji. Format strictly: headline | sub"
        )
        usr_msg = (
            f"Athlete: {first_name}. Greeting: {greeting}. "
            f"Readiness: {int(round(r))}/100. Neuro: {int(round(n))}/100. "
            f"Band: {band}. Streak: {streak} days. "
            f"7-day summary: {summary} Wellness: {wellness}"
        )
        raw = call_openai_chat([{"role": "system", "content": sys_msg}, {"role": "user", "content": usr_msg}], max_tokens=100)
        if raw and "unavailable" not in raw.lower() and "|" in raw:
            headline, sub_line = [p.strip() for p in raw.split("|", 1)]
        else:
            raise ValueError("bad response")
    except Exception:
        fallback = {
            "no_data":  (f"{greeting}, {first_name}. Log your first session to activate your dials.",
                         "Readiness, neuro, exposure and streak will update automatically."),
            "high":     (f"{greeting}, {first_name}. Readiness {int(r)} — both markers are primed.",
                         "Load and recovery are balanced. Good conditions to push quality today."),
            "good":     (f"{greeting}, {first_name}. Readiness {int(r)} — solid platform for today.",
                         "Numbers are steady. Execute your plan and stay sharp."),
            "moderate": (f"{greeting}, {first_name}. Readiness {int(r)} — some fatigue in the data.",
                         "Focus on quality over quantity and monitor how the session feels."),
            "low":      (f"{greeting}, {first_name}. Readiness {int(r)} — recovery is the priority.",
                         "Both markers are suppressed. Prioritise sleep and light movement today."),
        }
        headline, sub_line = fallback.get(band, fallback["no_data"])

    return html.Div([
        html.Div([
            html.Span(f"{icon} ", style={"fontSize": "18px", "fontWeight": 900, "color": color, "marginRight": "4px"}),
            html.Span(headline,   style={"fontWeight": 800, "fontSize": "15px", "color": color}),
            html.Span(streak_txt, style={"fontSize": "13px", "color": "#E65100", "marginLeft": "6px"}),
        ], style={"marginBottom": "4px"}),
        html.Div(sub_line, style={"fontSize": "13px", "color": "#6e6e6e", "lineHeight": "1.4"}),
    ], style={"maxWidth": "1000px", "margin": "10px auto 4px auto", "textAlign": "center",
              "padding": "0px", "background": "transparent", "border": "none"})


@app.callback(
    Output("ai-plan-output", "children"),
    Output("ai-plan-status", "children"),
    Input("btn-generate-plan",  "n_clicks"),
    State("athlete-dropdown",   "value"),
    State("ai-plan-coach",      "value"),
    State("ai-plan-goal",       "value"),
    State("ai-plan-duration",   "value"),
    prevent_initial_call=True,
)
def generate_session_plan(n_clicks, athlete_id, coach_style, goal, duration):
    if not n_clicks: raise PreventUpdate
    if not coach_style:              return no_update, "⚠️ Please select a coaching focus."
    if not goal or not goal.strip(): return no_update, "⚠️ Please enter a session goal."

    duration = duration or 45
    persona  = persona_prompt(coach_style)

    context_block = ""
    if athlete_id:
        try:
            df = load_tab(athlete_id)
            if not df.empty:
                context_block = (
                    f"Athlete context (last 7 days): {build_context_summary(df, days=7)}\n"
                    f"Wellness scan: {build_wellness_flags(df, days=7)}\n"
                    f"Upcoming sessions: {build_upcoming_context(df, today_adl(), n=3)}\n\n"
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

    coach_structure = COACH_STRUCTURE.get(coach_style,
        "Structure the session with Warm-Up, Primary, Secondary, and Cool-Down blocks. "
        "Be specific: include exact sets, reps, distances, rest periods and coaching cues.")

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

    raw = call_openai_chat([{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}], max_tokens=900)

    try:
        clean  = raw.replace("```json", "").replace("```", "").strip()
        data   = json.loads(clean)
        blocks = data.get("blocks", [])
    except Exception:
        return html.Div([html.Div("Session Plan", className="fw-semibold mb-2"),
                         html.Pre(raw, style={"whiteSpace": "pre-wrap", "fontSize": "13px"})]), ""

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
        title  = b.get("title",       "Block")
        dur    = b.get("duration_min", "")
        details= b.get("details",     "")
        bg, accent = BLOCK_COLORS.get(title, ("#f5f5f5", "#333"))
        cards.append(html.Div([
            html.Div([
                html.Span(title, style={"fontWeight": 800, "fontSize": "14px", "color": accent}),
                html.Span(f"~{dur} min", style={"fontSize": "12px", "color": accent, "opacity": "0.75",
                                                  "marginLeft": "8px", "fontWeight": 600}) if dur else None,
            ], style={"marginBottom": "6px"}),
            html.Div(details, style={"fontSize": "13px", "lineHeight": "1.5", "color": "#1a1a1a"}),
        ], style={"background": bg, "border": f"1px solid {accent}33", "borderLeft": f"4px solid {accent}",
                  "borderRadius": "10px", "padding": "14px 16px", "marginBottom": "10px"}))

    total = sum(b.get("duration_min", 0) for b in blocks)
    cards.append(html.Div(f"Total: ~{total} min  •  {coach_style}",
                          style={"fontSize": "12px", "color": "#666", "textAlign": "right", "marginTop": "4px"}))
    return html.Div(cards), ""


@app.callback(
    Output("share-card-modal",    "style"),
    Output("share-card-container","children"),
    Input("btn-share-card",       "n_clicks"),
    Input("share-card-close",     "n_clicks"),
    State("athlete-dropdown",     "value"),
    State("share-card-modal",     "style"),
    prevent_initial_call=True,
)
def show_share_card(n, close_n, athlete_id, current_style):
    ctx = callback_context
    if not ctx.triggered: raise PreventUpdate
    trigger = ctx.triggered[0]["prop_id"].split(".")[0]
    if trigger == "share-card-close":
        return {"display": "none"}, no_update
    if not n: raise PreventUpdate
    if current_style and current_style.get("display") == "block":
        return {"display": "none"}, no_update

    today      = today_adl()
    date_str   = today.strftime("%d %b %Y")
    first_name = (athlete_id or "Athlete").strip().split()[0]

    readiness_val = neuro_val = 0
    streak = weekly_pct = 0
    rpe_v = load_v = acwr_v = None
    sleep_v = fatigue_v = mood_v = soreness_v = None
    session_label = "Training day"
    session_sub   = ""

    try:
        df = load_tab(athlete_id)
        if not df.empty:
            streak, _ = compute_streaks(df)
            dow            = today.weekday()
            days_since_sat = (dow - 5) % 7
            week_start     = today - dt.timedelta(days=days_since_sat)
            week_end       = week_start + dt.timedelta(days=6)
            planned        = count_planned_sessions_in_week(df, week_start, week_end)
            logged_n       = count_logged_sessions_in_week(df, week_start, week_end)
            weekly_pct     = int(round(logged_n / planned * 100)) if planned > 0 else 0

            dft = df.copy()
            dft["Date"] = pd.to_datetime(dft["Date"], errors="coerce")
            dft = dft.sort_values("Date")
            dft = dft[~dft["Date"].duplicated(keep="last")]
            dft = dft.set_index("Date")
            dft = dft.reindex(pd.date_range(dft.index.min(), today, freq="D"))

            load_series    = pd.to_numeric(dft.get("Load"), errors="coerce")
            rpe_post_m = pd.to_numeric(dft.get("RPE_Post_Session"), errors="coerce")
            rpe_plan_m = pd.to_numeric(dft.get("RPE"), errors="coerce")
            if rpe_post_m.notna().sum() > 0:
                rpe_series = rpe_post_m
            elif rpe_plan_m.notna().sum() > 0:
                rpe_vals_m = rpe_plan_m.dropna()
                rpe_series = rpe_plan_m / 2.0 if (not rpe_vals_m.empty and rpe_vals_m.max() > 5) else rpe_plan_m
            else:
                rpe_series = pd.Series(dtype=float, index=dft.index)
            quality_series = pd.to_numeric(dft.get("Session_1_5"), errors="coerce")
            readiness_val  = calc_daily_readiness(load_series, rpe_series, quality_series) or 0

            # Use shared helper — same result as main dashboard
            neuro_val = compute_neuro_for_athlete(df, today) or 0

            # Still grab wellness values for display
            _df_n = df.copy()
            _df_n["Date"] = pd.to_datetime(_df_n["Date"], errors="coerce").dt.date
            _recent = _df_n[_df_n["Date"] >= today - dt.timedelta(days=14)]
            def _last(col):
                s = pd.to_numeric(_recent.get(col, pd.Series(dtype=float)), errors="coerce").dropna()
                return float(s.iloc[-1]) if not s.empty else None
            sl = _last("Sleep_1_5"); fa = _last("Fatigue_1_5")
            so = _last("Soreness_1_5"); mo = _last("Mood_1_5")
            if all(v is not None for v in [sl, fa, so, mo]):
                sleep_v    = int(sl); fatigue_v  = int(fa)
                soreness_v = int(so); mood_v     = int(mo)

            df2 = df.copy()
            df2["Date"] = pd.to_datetime(df2["Date"], errors="coerce").dt.date
            today_row   = df2[df2["Date"] == today]
            if not today_row.empty:
                r        = today_row.iloc[-1]
                rpe_raw  = pd.to_numeric(r.get("RPE_Post_Session"), errors="coerce")
                load_raw = pd.to_numeric(r.get("Load"),             errors="coerce")
                rpe_v    = rpe_raw  if pd.notna(rpe_raw)  else None
                load_v   = load_raw if pd.notna(load_raw) else None
                session_label = str(r.get("Workout") or "Training day")
                focus = str(r.get("Focus") or ""); venue = str(r.get("Venue") or "")
                session_sub = " · ".join(x for x in [venue, focus]
                                         if x and x.lower() not in ("nan", "none", "nil", ""))

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
            return "—" if pd.isna(f) else (str(round(f, decimals)) if decimals else str(int(round(f))))
        except Exception:
            return "—"

    circ  = 131.9
    d_r   = int(round(min(max(readiness_val or 0, 0), 100)))
    d_n   = int(round(min(max(neuro_val     or 0, 0), 100)))
    d_e   = int(round(min(max(weekly_pct    or 0, 0), 100)))
    d_sp  = int(round(min((streak / 31) * 100, 100)))
    d_sn  = streak
    ro_r  = round(circ * (1 - d_r  / 100), 1)
    ro_n  = round(circ * (1 - d_n  / 100), 1)
    ro_e  = round(circ * (1 - d_e  / 100), 1)
    ro_sp = round(circ * (1 - d_sp / 100), 1)
    c_r   = "#1E88E5"; c_n = "#43A047"; c_e = "#FB8C00"; c_sp = "#E91E8C"
    dl_name = date_str.replace(" ", "-")

    try:
        mot_sys = (
            "You are a straight-talking performance coach writing a one-liner for an athlete's shareable training card. "
            "Write ONE punchy sentence — sounds like something a great coach would say after glancing at the data. "
            "Rules: Max 14 words. Address the athlete by first name. Ground it in a real number (readiness, streak, or exposure). "
            "Tone: direct, warm, a little dry — like a coach who doesn't do corporate speak. "
            "BANNED: optimal, peak performance, indicates, recovery needed, warrior, beast, hustle, champion, champions, journey, incredible, amazing, greatness, path, destiny. "
            "No hashtags. No exclamation marks. No emoji."
        )
        mot_usr = (
            f"Athlete: {first_name}. Readiness: {d_r}/100. Neuro: {d_n}/100. "
            f"Streak: {d_sn} days. Exposure: {d_e}%. Date: {date_str}."
        )
        mot_quote = call_openai_chat(
            [{"role": "system", "content": mot_sys}, {"role": "user", "content": mot_usr}], max_tokens=40)
        if not mot_quote or "unavailable" in mot_quote.lower():
            mot_quote = f"Every session builds the athlete you're becoming, {first_name}."
    except Exception:
        mot_quote = f"Every session builds the athlete you're becoming, {first_name}."

    # Logo is served by Dash from /assets/ — load it in the iframe via URL, no disk read needed
    # Read logo server-side — no CORS issues, guaranteed to work on Render
    import base64, os
    logo_b64 = ""
    for _lp in ["assets/app_icon.png", "/app/assets/app_icon.png", "app/assets/app_icon.png"]:
        try:
            with open(_lp, "rb") as _lf:
                logo_b64 = base64.b64encode(_lf.read()).decode("utf-8")
            break
        except Exception:
            pass

    html_src = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#111;font-family:system-ui,sans-serif;display:flex;flex-direction:column;align-items:center;padding:12px;overflow-x:hidden}}
#previewWrap{{width:100%;max-width:320px;position:relative;touch-action:none;user-select:none}}
#preview{{width:100%;aspect-ratio:9/16;border-radius:18px;overflow:hidden;position:relative;background:#111;border:1px solid rgba(255,255,255,0.1);transform-origin:center center;will-change:transform}}
#bgCanvas{{position:absolute;inset:0;width:100%;height:100%;display:block}}
#scrim{{position:absolute;inset:0;background:linear-gradient(to bottom,rgba(0,0,0,0.12) 0%,rgba(0,0,0,0.04) 30%,rgba(0,0,0,0.55) 58%,rgba(0,0,0,0.82) 100%);pointer-events:none}}
#card-overlay{{position:absolute;bottom:0;left:0;right:0;padding:16px 18px 22px;pointer-events:none}}
.topbar{{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}}
.brand{{font-size:8px;letter-spacing:.16em;color:rgba(255,255,255,.55);text-transform:uppercase;display:flex;align-items:center;gap:5px}}
.brand img{{width:20px;height:20px;border-radius:3px;object-fit:contain;filter:brightness(0) invert(1);opacity:0.85}}
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
#resetBtn{{background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.15);border-radius:10px;padding:8px;color:rgba(255,255,255,0.5);font-size:11px;cursor:pointer;width:100%}}
</style></head><body>
<div id="previewWrap">
  <div id="preview">
    <canvas id="bgCanvas"></canvas>
    <div id="scrim"></div>
    <div id="card-overlay">
      <div class="topbar">
        <span class="brand">
          {"<img src='data:image/png;base64," + logo_b64 + "' alt='ACI' style='width:18px;height:18px;object-fit:contain;filter:brightness(0) invert(1);opacity:0.85'/>" if logo_b64 else ""}
          ACI &middot; Adaptive Coaching
        </span>
      </div>
      <div class="dials">
        <div class="dial-item">
          <svg width="52" height="52" viewBox="0 0 52 52">
            <circle cx="26" cy="26" r="21" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="4"/>
            <circle cx="26" cy="26" r="21" fill="none" stroke="{c_r}" stroke-width="4" stroke-linecap="round" stroke-dasharray="{circ}" stroke-dashoffset="{ro_r}" transform="rotate(-90 26 26)"/>
            <text x="26" y="30" text-anchor="middle" font-size="13" font-weight="700" fill="white" font-family="system-ui">{d_r}</text>
          </svg><div class="dial-lbl">Readiness</div>
        </div>
        <div class="dial-item">
          <svg width="52" height="52" viewBox="0 0 52 52">
            <circle cx="26" cy="26" r="21" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="4"/>
            <circle cx="26" cy="26" r="21" fill="none" stroke="{c_n}" stroke-width="4" stroke-linecap="round" stroke-dasharray="{circ}" stroke-dashoffset="{ro_n}" transform="rotate(-90 26 26)"/>
            <text x="26" y="30" text-anchor="middle" font-size="13" font-weight="700" fill="white" font-family="system-ui">{d_n}</text>
          </svg><div class="dial-lbl">Neuro</div>
        </div>
        <div class="dial-item">
          <svg width="52" height="52" viewBox="0 0 52 52">
            <circle cx="26" cy="26" r="21" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="4"/>
            <circle cx="26" cy="26" r="21" fill="none" stroke="{c_e}" stroke-width="4" stroke-linecap="round" stroke-dasharray="{circ}" stroke-dashoffset="{ro_e}" transform="rotate(-90 26 26)"/>
            <text x="26" y="30" text-anchor="middle" font-size="13" font-weight="700" fill="white" font-family="system-ui">{d_e}</text>
          </svg><div class="dial-lbl">Exposure</div>
        </div>
        <div class="dial-item">
          <svg width="52" height="52" viewBox="0 0 52 52">
            <circle cx="26" cy="26" r="21" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="4"/>
            <circle cx="26" cy="26" r="21" fill="none" stroke="{c_sp}" stroke-width="4" stroke-linecap="round" stroke-dasharray="{circ}" stroke-dashoffset="{ro_sp}" transform="rotate(-90 26 26)"/>
            <text x="26" y="30" text-anchor="middle" font-size="13" font-weight="700" fill="white" font-family="system-ui">{d_sn}</text>
          </svg><div class="dial-lbl">Streak</div>
        </div>
      </div>
      <div class="divider"></div>
      <div class="quote">&ldquo;{mot_quote}&rdquo;</div>
      <div class="footer"><div class="footer-date">{date_str}</div></div>
    </div>
  </div>
</div>
<div id="controls">
  <label id="photolabel" for="photoInput">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/>
      <polyline points="21 15 16 10 5 21"/>
    </svg>Choose a background photo
  </label>
  <input type="file" id="photoInput" accept="image/*">
  <button id="dlBtn">Download story (1080&times;1920)</button>
  <button id="resetBtn">Reset position &amp; size</button>
  <div id="hint">Pinch to resize &middot; Drag to reposition &middot; Tap Reset to restore</div>
</div>
<script>
  const EXPORT_W=1080,EXPORT_H=1920;
  const wrap=document.getElementById('previewWrap'),previewEl=document.getElementById('preview'),canvasEl=document.getElementById('bgCanvas');
  let userImage=null,logoImg=null;
  let userImageDataURL=null; // persist across redraws

  // Logo injected as base64 from server — no CORS, always works
  const LOGO_DATA_URL="{logo_b64 and ('data:image/png;base64,' + logo_b64) or ''}";
  if(LOGO_DATA_URL){{
    logoImg=new Image();
    logoImg.src=LOGO_DATA_URL;
  }}

  function drawPreviewBg(){{
    // Get actual rendered size — use getBoundingClientRect for accuracy on mobile
    const rect=previewEl.getBoundingClientRect();
    const w=rect.width||previewEl.clientWidth||320;
    const h=rect.height||previewEl.clientHeight||Math.round(w*16/9);
    if(w===0||h===0){{setTimeout(drawPreviewBg,100);return;}}
    canvasEl.width=w;canvasEl.height=h;
    const ctx=canvasEl.getContext('2d');
    ctx.clearRect(0,0,w,h);
    if(userImage){{
      // Cover fit — always fill the canvas
      const scale=Math.max(w/userImage.naturalWidth,h/userImage.naturalHeight);
      const dw=userImage.naturalWidth*scale,dh=userImage.naturalHeight*scale;
      ctx.drawImage(userImage,(w-dw)/2,(h-dh)/2,dw,dh);
    }}else{{
      const g=ctx.createLinearGradient(0,0,w,h);
      g.addColorStop(0,'#0f2027');g.addColorStop(0.5,'#203a43');g.addColorStop(1,'#2c5364');
      ctx.fillStyle=g;ctx.fillRect(0,0,w,h);
    }}
  }}

  // Redraw on resize but don't lose the image
  let resizeTimer;
  window.addEventListener('resize',function(){{clearTimeout(resizeTimer);resizeTimer=setTimeout(drawPreviewBg,80);}});
  // Initial draw — delayed to let modal/iframe fully render
  setTimeout(drawPreviewBg,200);

  // Transform state — allow scale 0.3 to 4
  let currentScale=1,translateX=0,translateY=0;
  let isDragging=false,dragStartX=0,dragStartY=0;
  let startDist=0,startScale=1,startTX=0,startTY=0,pinchMidX=0,pinchMidY=0;

  function applyTransform(){{
    previewEl.style.transform=`translate(${{translateX}}px,${{translateY}}px) scale(${{currentScale}})`;
    previewEl.style.transformOrigin='center center';
  }}
  function dist(a,b){{const dx=a.clientX-b.clientX,dy=a.clientY-b.clientY;return Math.sqrt(dx*dx+dy*dy);}}
  function midpoint(a,b){{return{{x:(a.clientX+b.clientX)/2,y:(a.clientY+b.clientY)/2}};}}

  previewEl.style.transition='none';previewEl.style.cursor='grab';previewEl.style.touchAction='none';

  previewEl.addEventListener('touchstart',function(e){{
    e.preventDefault();
    if(e.touches.length===2){{
      startDist=dist(e.touches[0],e.touches[1]);
      startScale=currentScale;
      startTX=translateX;startTY=translateY;
      const mid=midpoint(e.touches[0],e.touches[1]);
      const rect=previewEl.getBoundingClientRect();
      pinchMidX=mid.x-rect.left-rect.width/2;
      pinchMidY=mid.y-rect.top-rect.height/2;
      isDragging=false;
    }}else if(e.touches.length===1){{
      isDragging=true;
      dragStartX=e.touches[0].clientX-translateX;
      dragStartY=e.touches[0].clientY-translateY;
    }}
  }},{{passive:false}});

  previewEl.addEventListener('touchmove',function(e){{
    e.preventDefault();
    if(e.touches.length===2){{
      const newDist=dist(e.touches[0],e.touches[1]);
      const newScale=Math.min(4,Math.max(0.3,startScale*(newDist/startDist)));
      const scaleDelta=newScale/startScale;
      translateX=startTX+pinchMidX*(1-scaleDelta);
      translateY=startTY+pinchMidY*(1-scaleDelta);
      currentScale=newScale;
      applyTransform();
    }}else if(e.touches.length===1&&isDragging){{
      translateX=e.touches[0].clientX-dragStartX;
      translateY=e.touches[0].clientY-dragStartY;
      applyTransform();
    }}
  }},{{passive:false}});

  previewEl.addEventListener('touchend',function(e){{
    isDragging=false;
  }});

  // Mouse wheel zoom (desktop)
  wrap.addEventListener('wheel',function(e){{
    e.preventDefault();
    currentScale=Math.min(4,Math.max(0.3,currentScale*(e.deltaY>0?0.9:1.1)));
    applyTransform();
  }},{{passive:false}});

  // Double-tap or reset button
  let lastTap=0;
  previewEl.addEventListener('touchend',function(e){{
    const now=Date.now();
    if(now-lastTap<300){{resetTransform();}}
    lastTap=now;
  }});
  document.getElementById('resetBtn').addEventListener('click',resetTransform);
  function resetTransform(){{
    currentScale=1;translateX=0;translateY=0;
    previewEl.style.transition='transform 0.25s ease-out';
    applyTransform();
    setTimeout(()=>previewEl.style.transition='none',260);
  }}

  // Photo picker — store dataURL so redraws never lose the image
  document.getElementById('photoInput').addEventListener('change',function(e){{
    const file=e.target.files[0];if(!file)return;
    const reader=new FileReader();
    reader.onload=function(ev){{
      userImageDataURL=ev.target.result;
      const img=new Image();
      img.onload=function(){{
        userImage=img;
        drawPreviewBg();
      }};
      img.onerror=function(){{console.error('Image load failed');}};
      img.src=userImageDataURL;
    }};
    reader.onerror=function(){{alert('Could not read file. Try a different photo.');}};
    reader.readAsDataURL(file);
    document.getElementById('photolabel').innerHTML='<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg> Change photo';
  }});

  const isIOS=/iPad|iPhone|iPod/.test(navigator.userAgent)&&!window.MSStream;
  const isSafari=/^((?!chrome|android).)*safari/i.test(navigator.userAgent);
  const isiOSSafari=isIOS||isSafari;
  if(isiOSSafari){{
    document.getElementById('hint').innerHTML='\u26a0\ufe0f iPhone: tap Download, then long-press the image \u2192 Save to Photos';
    document.getElementById('hint').style.color='rgba(255,200,80,0.9)';
  }}

  document.getElementById('dlBtn').addEventListener('click',function(){{
    const btn=this;btn.textContent='Generating\u2026';btn.disabled=true;

    function doExport(){{
    const out=document.createElement('canvas');out.width=EXPORT_W;out.height=EXPORT_H;
    const ctx=out.getContext('2d');

    // Background — use naturalWidth for reliability
    if(userImage&&userImage.naturalWidth>0){{
      const scale=Math.max(EXPORT_W/userImage.naturalWidth,EXPORT_H/userImage.naturalHeight);
      const dw=userImage.naturalWidth*scale,dh=userImage.naturalHeight*scale;
      ctx.drawImage(userImage,(EXPORT_W-dw)/2,(EXPORT_H-dh)/2,dw,dh);
    }}else{{
      const g=ctx.createLinearGradient(0,0,EXPORT_W,EXPORT_H);
      g.addColorStop(0,'#0f2027');g.addColorStop(0.5,'#203a43');g.addColorStop(1,'#2c5364');
      ctx.fillStyle=g;ctx.fillRect(0,0,EXPORT_W,EXPORT_H);
    }}

    // Scrim
    const scrim=ctx.createLinearGradient(0,0,0,EXPORT_H);
    scrim.addColorStop(0,'rgba(0,0,0,0.12)');scrim.addColorStop(0.30,'rgba(0,0,0,0.04)');
    scrim.addColorStop(0.58,'rgba(0,0,0,0.55)');scrim.addColorStop(1.0,'rgba(0,0,0,0.82)');
    ctx.fillStyle=scrim;ctx.fillRect(0,0,EXPORT_W,EXPORT_H);

    const PAD=80,CARD_TOP=EXPORT_H*0.48;

    // Dials — positioned first so brand sits directly above them
    const DIAL_R=90,DIAL_SW=18,DIAL_Y=CARD_TOP+120,dialSpacing=(EXPORT_W-PAD*2)/4;

    // Logo + brand — centred, directly above the dials
    const LOGO_SIZE=64;
    const BRAND_Y=DIAL_Y-DIAL_R-90; // sit above dials
    const BRAND_CX=EXPORT_W/2;
    function drawBrand(){{
      // Brand text centred
      ctx.font='500 28px system-ui';ctx.fillStyle='rgba(255,255,255,0.70)';
      ctx.textAlign='center';ctx.textBaseline='middle';
      ctx.fillText('ACI \u00b7 ADAPTIVE COACHING',BRAND_CX+(logoImg?LOGO_SIZE/2+8:0),BRAND_Y+LOGO_SIZE/2);
    }}
    if(logoImg&&logoImg.naturalWidth>0){{
      const oc=document.createElement('canvas');
      oc.width=LOGO_SIZE;oc.height=LOGO_SIZE;
      const octx=oc.getContext('2d');
      octx.drawImage(logoImg,0,0,LOGO_SIZE,LOGO_SIZE);
      octx.globalCompositeOperation='source-in';
      octx.fillStyle='rgba(255,255,255,0.90)';
      octx.fillRect(0,0,LOGO_SIZE,LOGO_SIZE);
      // Measure brand text to centre logo+text together
      ctx.font='500 28px system-ui';
      const textW=ctx.measureText('ACI \u00b7 ADAPTIVE COACHING').width;
      const totalW=LOGO_SIZE+14+textW;
      const startX=BRAND_CX-totalW/2;
      ctx.drawImage(oc,startX,BRAND_Y,LOGO_SIZE,LOGO_SIZE);
      ctx.font='500 28px system-ui';ctx.fillStyle='rgba(255,255,255,0.70)';
      ctx.textAlign='left';ctx.textBaseline='middle';
      ctx.fillText('ACI \u00b7 ADAPTIVE COACHING',startX+LOGO_SIZE+14,BRAND_Y+LOGO_SIZE/2);
    }}else{{
      drawBrand();
    }}
    const dialData=[
      {{val:{d_r},pct:{d_r},color:'{c_r}',label:'READINESS'}},
      {{val:{d_n},pct:{d_n},color:'{c_n}',label:'NEURO'}},
      {{val:{d_e},pct:{d_e},color:'{c_e}',label:'EXPOSURE'}},
      {{val:{d_sn},pct:{d_sp},color:'{c_sp}',label:'STREAK'}},
    ];
    dialData.forEach(function(d,i){{
      const cx=PAD+dialSpacing*i+dialSpacing/2,cy=DIAL_Y;
      ctx.beginPath();ctx.arc(cx,cy,DIAL_R,0,Math.PI*2);
      ctx.strokeStyle='rgba(255,255,255,0.12)';ctx.lineWidth=DIAL_SW;ctx.lineCap='butt';ctx.stroke();
      const pct=Math.min(Math.max(d.pct,0),100)/100,start=-Math.PI/2,end=start+pct*Math.PI*2;
      ctx.beginPath();ctx.arc(cx,cy,DIAL_R,start,end);
      ctx.strokeStyle=d.color;ctx.lineWidth=DIAL_SW;ctx.lineCap='round';ctx.stroke();ctx.lineCap='butt';
      ctx.font='bold 60px system-ui';ctx.fillStyle='#ffffff';
      ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(String(d.val),cx,cy);
      ctx.font='21px system-ui';ctx.fillStyle='rgba(255,255,255,0.5)';
      ctx.textAlign='center';ctx.textBaseline='alphabetic';ctx.fillText(d.label,cx,cy+DIAL_R+40);
    }});

    // Divider
    const divY=DIAL_Y+DIAL_R+80;
    ctx.beginPath();ctx.moveTo(PAD,divY);ctx.lineTo(EXPORT_W-PAD,divY);
    ctx.strokeStyle='rgba(255,255,255,0.18)';ctx.lineWidth=2;ctx.stroke();

    // Quote
    const quoteText='\u201c{mot_quote}\u201d',quoteY=divY+72,maxWidth=EXPORT_W-PAD*2;
    ctx.font='italic 44px system-ui,sans-serif';ctx.fillStyle='rgba(255,255,255,0.90)';
    ctx.textAlign='center';ctx.textBaseline='alphabetic';
    wrapText(ctx,quoteText,EXPORT_W/2,quoteY,maxWidth,64);

    // Footer
    const footY=EXPORT_H-80;
    ctx.font='26px system-ui';ctx.fillStyle='rgba(255,255,255,0.30)';
    ctx.textAlign='left';ctx.textBaseline='alphabetic';
    ctx.fillText('{date_str}',PAD,footY);

    const imgData=out.toDataURL('image/png');
    if(isiOSSafari){{
      const newTab=window.open();
      if(newTab){{
        newTab.document.write('<html><head><title>ACI Share Card</title><meta name="viewport" content="width=device-width"></head><body style="margin:0;background:#111;display:flex;flex-direction:column;align-items:center;padding:16px;"><p style="color:#fff;font-size:14px;margin-bottom:12px;">Long-press the image \u2192 Save to Photos</p><img src="'+imgData+'" style="max-width:100%;border-radius:12px;"/></body></html>');
        newTab.document.close();
      }}
      btn.textContent='\u2705 Opened \u2014 save from new tab';btn.style.background='#2E7D32';
    }}else{{
      const a=document.createElement('a');a.download='aci-{dl_name}.png';a.href=imgData;a.click();
      btn.textContent='Download story (1080\u00d71920)';
    }}
    btn.disabled=false;
    }} // end doExport

    // Restore image from dataURL if needed, then export
    if((!userImage||userImage.naturalWidth===0) && userImageDataURL){{
      const restored=new Image();
      restored.onload=function(){{userImage=restored;doExport();}};
      restored.src=userImageDataURL;
    }}else{{
      doExport();
    }}
  }});

  function wrapText(ctx,text,x,y,maxWidth,lineHeight){{
    const words=text.split(' ');let line='',cy=y;
    for(let i=0;i<words.length;i++){{
      const test=line+words[i]+' ';
      if(ctx.measureText(test).width>maxWidth&&i>0){{ctx.fillText(line.trim(),x,cy);line=words[i]+' ';cy+=lineHeight;}}
      else{{line=test;}}
    }}
    ctx.fillText(line.trim(),x,cy);
  }}
</script>
</body></html>"""

    return {"display": "block"}, html.Iframe(
        srcDoc=html_src,
        style={"width": "100%", "height": "700px", "border": "none", "background": "#111"}
    )


@app.callback(
    Output("garmin-status-badge", "children"),
    Input("athlete-dropdown",     "value"),
    Input("today-date",           "children"),
    prevent_initial_call=True,
)
def update_garmin_badge(athlete_id, _today):
    if not athlete_id: raise PreventUpdate
    try:
        df = load_tab(athlete_id)
        token, _ = garmin_get_athlete_tokens(df)
        garmin_linked = bool(token)
    except Exception:
        garmin_linked = False

    if garmin_linked:
        badge = html.Div([
            html.Span("⌚ Garmin connected",
                      style={"fontSize": "11px", "background": "#e8f5e9", "color": "#2E7D32",
                             "padding": "3px 10px", "borderRadius": "20px", "fontWeight": "600",
                             "border": "1px solid #a5d6a7"}),

        ], style={"textAlign": "center", "marginTop": "4px"})
    else:
        badge = html.Div([
            html.A("⌚ Connect Garmin", href=f"/garmin/connect?athlete={athlete_id}", target="_blank",
                   style={"fontSize": "11px", "color": "#1565C0", "textDecoration": "none",
                          "background": "#e3f2fd", "padding": "3px 10px", "borderRadius": "20px",
                          "border": "1px solid #90caf9", "fontWeight": "500"}),

        ], style={"textAlign": "center", "marginTop": "4px"})

    return badge


# ============================================================
#  Garmin OAuth + Push webhook routes (Flask)
# ============================================================

_garmin_temp_secrets = {}


@server.route("/garmin/connect")
def garmin_connect():
    if not GARMIN_ENABLED:
        return "Garmin integration not configured (missing API keys).", 503
    athlete_id = flask_request.args.get("athlete", "unknown")
    try:
        token, secret = garmin_get_request_token()
        _garmin_temp_secrets[athlete_id] = (token, secret)
        return redirect(f"https://connect.garmin.com/oauthConfirm?oauth_token={token}&state={athlete_id}")
    except Exception as e:
        return f"Error starting Garmin OAuth: {e}", 500


@server.route("/garmin/callback")
def garmin_callback():
    oauth_token    = flask_request.args.get("oauth_token",    "")
    oauth_verifier = flask_request.args.get("oauth_verifier", "")
    athlete_id     = flask_request.args.get("state",          "unknown")
    temp = _garmin_temp_secrets.get(athlete_id)
    if not temp:
        return "Session expired. Please try linking again.", 400
    req_token, req_secret = temp
    try:
        user_token, user_secret = garmin_get_access_token(req_token, req_secret, oauth_verifier)
    except Exception as e:
        return f"OAuth error: {e}", 500
    try:
        df = load_tab(athlete_id)
        if not df.empty:
            write_row(athlete_id, 0, {"Garmin_Token": user_token, "Garmin_Secret": user_secret})
            print(f"✅ Garmin tokens stored for {athlete_id}")
    except Exception as e:
        print(f"⚠️ Could not store Garmin tokens for {athlete_id}: {e}")
    _garmin_temp_secrets.pop(athlete_id, None)
    return """<html><body style="font-family:system-ui;text-align:center;padding:60px;background:#f0f4f8">
      <h2 style="color:#2E7D32">&#10003; Garmin connected!</h2>
      <p>Your Garmin account is now linked to ACI.</p>
      <p style="color:#888;font-size:13px">You can close this tab.</p>
    </body></html>"""


@server.route("/garmin/push", methods=["POST"])
def garmin_push():
    try:
        payload = flask_request.get_json(force=True) or {}
        parsed  = garmin_parse_to_scales(payload)
        scales  = {k: v for k, v in parsed.items() if v is not None}
        garmin_user_id = None
        for key in ["dailies", "activities", "sleeps"]:
            items = payload.get(key, [])
            if items and "userId" in items[0]:
                garmin_user_id = str(items[0]["userId"])
                break
        if garmin_user_id and sh is not None:
            for ws in sh.worksheets():
                try:
                    df = load_tab(ws.title)
                    tok, _ = garmin_get_athlete_tokens(df)
                    if tok:
                        today = today_adl()
                        df["Date"] = pd.to_datetime(df.get("Date", pd.Series(dtype=str)), errors="coerce").dt.date
                        matches = df.index[df["Date"] == today].tolist()
                        if matches:
                            write_row(ws.title, matches[0], scales)
                            break
                except Exception:
                    continue
    except Exception as e:
        print(f"❌ Garmin push error: {e}")
    return jsonify({"status": "ok"}), 200


@server.route("/garmin/status")
def garmin_status():
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




# ============================================================
#  Squad Overview callback — coach-only
# ============================================================

@app.callback(
    Output("squad-cards-container", "children"),
    Input("nav-squad", "n_clicks"),
    Input("squad-refresh-btn", "n_clicks"),
    State("auth-store", "data"),
    prevent_initial_call=True,
)
def update_squad_view(nav_clicks, refresh_clicks, auth_data):
    print(f"🏟️ Squad callback fired — nav={nav_clicks} refresh={refresh_clicks} auth={auth_data}")
    if not auth_data:
        print("⚠️ No auth_data")
        return html.Div("Not logged in.", className="text-muted")
    if not auth_data.get("is_coach"):
        print(f"⚠️ Not a coach — is_coach={auth_data.get('is_coach')}, keys={list(auth_data.keys())}")
        return html.Div("Coach access only.", className="text-muted")

    today = today_adl()
    print(f"✅ Squad loading for coach {auth_data.get('username')} — {len(USER_LOGINS)} users in config")

    # All sheets except "Default" template
    EXCLUDE = {"Default", "default"}
    all_sheets = sorted([
        info.get("sheet", "")
        for _, info in USER_LOGINS.items()
        if info.get("sheet", "") and info.get("sheet", "") not in EXCLUDE
    ])

    TRAFFIC = {
        "green":  {"bg": "#e8f5e9", "border": "#2E7D32", "dot": "#2E7D32"},
        "amber":  {"bg": "#fff8e1", "border": "#F9A825", "dot": "#F9A825"},
        "red":    {"bg": "#ffebee", "border": "#C62828", "dot": "#C62828"},
        "grey":   {"bg": "#f5f5f5", "border": "#bdbdbd", "dot": "#bdbdbd"},
    }

    def score_colour(val, invert=False):
        if val is None: return "grey"
        v = (100 - val) if invert else val
        if v >= 70: return "green"
        if v >= 45: return "amber"
        return "red"

    def mini_ring(value, color, size=52):
        """Small ring dial using CSS — reliable top-fill, no Plotly quirks."""
        colour_map = {"green": "#2E7D32", "amber": "#F9A825", "red": "#C62828", "grey": "#e0e0e0"}
        c = colour_map.get(score_colour(value), "#e0e0e0")
        txt = "—" if value is None else str(int(round(value)))
        pct = 0 if value is None else min(max(float(value), 0), 100)

        # Use SVG via dcc.Graph with Scatter arc — works on all browsers
        # Build arc path for the filled portion
        import math
        if pct >= 100:
            # Full circle — use two semicircle arcs
            arc_d = "M 26 6 A 20 20 0 1 1 25.999 6 Z"
        elif pct <= 0:
            arc_d = ""
        else:
            angle = (pct / 100) * 360
            # Start at top (270deg in standard math = -90deg from east)
            start_rad = math.radians(-90)
            end_rad   = math.radians(-90 + angle)
            x1 = 26 + 20 * math.cos(start_rad)
            y1 = 26 + 20 * math.sin(start_rad)
            x2 = 26 + 20 * math.cos(end_rad)
            y2 = 26 + 20 * math.sin(end_rad)
            large = 1 if angle > 180 else 0
            arc_d = f"M {x1:.2f} {y1:.2f} A 20 20 0 {large} 1 {x2:.2f} {y2:.2f}"

        svg_parts = [
            f'<svg viewBox="0 0 52 52" width="{size}" height="{size}" xmlns="http://www.w3.org/2000/svg">',
            f'<circle cx="26" cy="26" r="20" fill="none" stroke="#f0f0f0" stroke-width="6"/>',
        ]
        if arc_d:
            svg_parts.append(
                f'<path d="{arc_d}" fill="none" stroke="{c}" stroke-width="6" '
                f'stroke-linecap="round"/>'
            )
        svg_parts.append(
            f'<text x="26" y="31" text-anchor="middle" font-size="13" '
            f'font-weight="700" fill="#333" font-family="system-ui">{txt}</text>'
        )
        svg_parts.append('</svg>')
        svg_str = "".join(svg_parts)

        # Use an img tag with SVG data URI — works everywhere including iOS Safari
        import base64
        svg_b64 = base64.b64encode(svg_str.encode()).decode()
        data_uri = f"data:image/svg+xml;base64,{svg_b64}"

        return html.Div(
            html.Img(src=data_uri, style={"width": f"{size}px", "height": f"{size}px"}),
            style={"width": f"{size}px", "height": f"{size}px"},
        )

    cards = []

    for sheet_name in sorted(all_sheets):
        first_name = sheet_name.strip().split()[0] if sheet_name.strip() else sheet_name

        try:
            df = load_tab(sheet_name)
        except Exception:
            df = pd.DataFrame()

        # Compute all metrics
        readiness_val = None
        neuro_val     = None
        streak        = 0
        last_logged   = None
        days_ago      = None
        weekly_pct    = None
        session_note  = ""
        session_rpe   = None

        if not df.empty:
            try:
                # Streak
                streak, _ = compute_streaks(df)

                # Weekly exposure
                dow = today.weekday()
                ws  = today - dt.timedelta(days=(dow - 5) % 7)
                we  = ws + dt.timedelta(days=6)
                planned   = count_planned_sessions_in_week(df, ws, we)
                logged_n  = count_logged_sessions_in_week(df, ws, we)
                weekly_pct = int(round(logged_n / planned * 100)) if planned > 0 else 0

                # Readiness
                dft = df.copy()
                dft["Date"] = pd.to_datetime(dft["Date"], errors="coerce")
                dft = dft.sort_values("Date")
                dft = dft[~dft["Date"].duplicated(keep="last")].set_index("Date")
                dft = dft.reindex(pd.date_range(dft.index.min(), today, freq="D"))

                load_s   = pd.to_numeric(dft.get("Load"), errors="coerce")
                rpe_post = pd.to_numeric(dft.get("RPE_Post_Session"), errors="coerce")
                rpe_plan = pd.to_numeric(dft.get("RPE"), errors="coerce")
                if rpe_post.notna().sum() > 0:
                    rpe_s = rpe_post
                elif rpe_plan.notna().sum() > 0:
                    vals_p = rpe_plan.dropna()
                    rpe_s = rpe_plan / 2.0 if (not vals_p.empty and vals_p.max() > 5) else rpe_plan
                else:
                    rpe_s = pd.Series(dtype=float, index=dft.index)
                qual_s = pd.to_numeric(dft.get("Session_1_5"), errors="coerce")
                readiness_val = calc_daily_readiness(load_s, rpe_s, qual_s)

                # Neuro (shared helper)
                neuro_val = compute_neuro_for_athlete(df, today)

                # Last logged session
                df2 = df.copy()
                df2["Date"] = pd.to_datetime(df2["Date"], errors="coerce").dt.date
                df2 = df2.sort_values("Date")
                logged_days = [d for d in df2["Date"].dropna().unique()
                               if get_day_status(df2, d).get("logged", False)]
                if logged_days:
                    last_logged = max(logged_days)
                    days_ago    = (today - last_logged).days

                # Today's session note/RPE
                today_rows = df2[df2["Date"] == today]
                if not today_rows.empty:
                    row = today_rows.iloc[-1]
                    session_note = str(row.get("Athlete_Notes", "") or "").strip()
                    if session_note.lower() in ("", "nan", "none", "nil", "example", "test", "n/a", "-", "—"):
                        session_note = ""
                    rpe_raw = pd.to_numeric(row.get("RPE_Post_Session", np.nan), errors="coerce")
                    session_rpe = int(rpe_raw) if pd.notna(rpe_raw) and rpe_raw > 0 else None

            except Exception as ex:
                print(f"Squad card error for {sheet_name}: {ex}")

        # ── Status badge ──────────────────────────────────────────────────────
        if days_ago is None:
            status_label = "No data"
            status_bg    = "#f5f5f5"
            status_color = "#999"
        elif days_ago == 0:
            status_label = "Logged today ✓"
            status_bg    = "#e8f5e9"
            status_color = "#2E7D32"
        elif days_ago == 1:
            status_label = "Yesterday"
            status_bg    = "#fff8e1"
            status_color = "#F9A825"
        elif days_ago <= 3:
            status_label = f"{days_ago}d ago"
            status_bg    = "#fff3e0"
            status_color = "#E65100"
        else:
            status_label = f"{days_ago}d ago ⚠"
            status_bg    = "#ffebee"
            status_color = "#C62828"

        # ── Readiness colour ──────────────────────────────────────────────────
        r_col = score_colour(readiness_val)
        n_col = score_colour(neuro_val)
        card_border = TRAFFIC[r_col]["border"]

        # ── Build card ────────────────────────────────────────────────────────
        card = html.Div([

            # Header row: name + status badge
            html.Div([
                html.Div(sheet_name, style={"fontWeight": "700", "fontSize": "15px", "color": "#1a1a1a"}),
                html.Div(status_label, style={
                    "fontSize": "11px", "fontWeight": "600", "padding": "2px 10px",
                    "borderRadius": "999px", "background": status_bg, "color": status_color,
                }),
            ], style={"display": "flex", "justifyContent": "space-between",
                      "alignItems": "center", "marginBottom": "12px"}),

            # Dials row
            html.Div([
                # Readiness
                html.Div([
                    mini_ring(readiness_val, r_col),
                    html.Div("Readiness", style={"fontSize": "10px", "color": "#888",
                                                  "textAlign": "center", "marginTop": "3px"}),
                ], style={"display": "flex", "flexDirection": "column", "alignItems": "center"}),
                # Neuro
                html.Div([
                    mini_ring(neuro_val, n_col),
                    html.Div("Neuro", style={"fontSize": "10px", "color": "#888",
                                              "textAlign": "center", "marginTop": "3px"}),
                ], style={"display": "flex", "flexDirection": "column", "alignItems": "center"}),
                # Exposure
                html.Div([
                    mini_ring(weekly_pct, score_colour(weekly_pct)),
                    html.Div("Exposure", style={"fontSize": "10px", "color": "#888",
                                                 "textAlign": "center", "marginTop": "3px"}),
                ], style={"display": "flex", "flexDirection": "column", "alignItems": "center"}),
                # Streak
                html.Div([
                    mini_ring(streak if streak else None,
                              "grey" if not streak else score_colour(min((streak / 14) * 100, 100))),
                    html.Div("Streak", style={"fontSize": "10px", "color": "#888",
                                              "textAlign": "center", "marginTop": "3px"}),
                ], style={"display": "flex", "flexDirection": "column", "alignItems": "center"}),
            ], style={"display": "flex", "justifyContent": "space-around",
                      "marginBottom": "10px" if (session_note or session_rpe) else "0"}),

            # Today's session summary if logged
            html.Div([
                html.Div([
                    html.Span("Today: ", style={"fontSize": "11px", "color": "#888", "fontWeight": "600"}),
                    html.Span(f"RPE {session_rpe}/5  ", style={"fontSize": "12px", "color": "#444"})
                    if session_rpe else None,
                    html.Span(session_note[:80] + ("…" if len(session_note) > 80 else ""),
                              style={"fontSize": "12px", "color": "#555", "fontStyle": "italic"})
                    if session_note else None,
                ]) if (session_note or session_rpe) else None,
            ]),

        ], style={
            "background": "white",
            "borderRadius": "14px",
            "padding": "14px 16px",
            "boxShadow": "0 2px 8px rgba(0,0,0,0.08)",
            "borderLeft": f"4px solid {card_border}",
            "marginBottom": "12px",
        })

        cards.append(card)

    if not cards:
        return html.Div("No athletes found.", className="text-muted mt-3")

    total = len(all_sheets)
    summary = html.Div([
        html.Div([
            html.Div(str(total), style={"fontSize": "28px", "fontWeight": "800", "color": "#1565C0"}),
            html.Div("Athletes", style={"fontSize": "11px", "color": "#888"}),
        ], style={"textAlign": "center", "flex": "1"}),
        html.Div([
            html.Div(today.strftime("%a"), style={"fontSize": "22px", "fontWeight": "700", "color": "#333"}),
            html.Div(today.strftime("%d %b %Y"), style={"fontSize": "11px", "color": "#888"}),
        ], style={"textAlign": "center", "flex": "1"}),
    ], style={"display": "flex", "background": "#f8f9fa", "borderRadius": "12px",
              "padding": "12px", "marginBottom": "16px"})

    return [summary] + cards

if __name__ == "__main__":
    app.run(debug=True)