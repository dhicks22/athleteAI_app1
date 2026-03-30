# garmin_integration.py
# Handles OAuth handshake + Push webhook receiver for Garmin Health API

import os
import json
import hmac
import hashlib
import requests
import datetime as dt
from requests_oauthlib import OAuth1
from flask import request, jsonify

GARMIN_CONSUMER_KEY    = os.getenv("GARMIN_CONSUMER_KEY")
GARMIN_CONSUMER_SECRET = os.getenv("GARMIN_CONSUMER_SECRET")

GARMIN_REQUEST_TOKEN_URL = "https://connectapi.garmin.com/oauth-service/oauth/request_token"
GARMIN_AUTHORIZE_URL     = "https://connect.garmin.com/oauthConfirm"
GARMIN_ACCESS_TOKEN_URL  = "https://connectapi.garmin.com/oauth-service/oauth/access_token"
GARMIN_API_BASE          = "https://apis.garmin.com/wellness-api/rest"


# ── Step 1: Get request token (start OAuth flow) ─────────────
def get_request_token():
    oauth = OAuth1(GARMIN_CONSUMER_KEY, GARMIN_CONSUMER_SECRET)
    resp = requests.post(GARMIN_REQUEST_TOKEN_URL, auth=oauth)
    resp.raise_for_status()
    # Returns: oauth_token=xxx&oauth_token_secret=yyy
    params = dict(p.split("=") for p in resp.text.split("&"))
    return params["oauth_token"], params["oauth_token_secret"]


# ── Step 2: Build authorisation URL to redirect athlete to ───
def get_authorize_url(oauth_token: str) -> str:
    return f"{GARMIN_AUTHORIZE_URL}?oauth_token={oauth_token}"


# ── Step 3: Exchange verifier for access token ───────────────
def get_access_token(oauth_token: str, oauth_token_secret: str, oauth_verifier: str):
    oauth = OAuth1(
        GARMIN_CONSUMER_KEY,
        GARMIN_CONSUMER_SECRET,
        resource_owner_key=oauth_token,
        resource_owner_secret=oauth_token_secret,
        verifier=oauth_verifier,
    )
    resp = requests.post(GARMIN_ACCESS_TOKEN_URL, auth=oauth)
    resp.raise_for_status()
    params = dict(p.split("=") for p in resp.text.split("&"))
    return params["oauth_token"], params["oauth_token_secret"]


# ── Pull data manually for a user ────────────────────────────
def pull_daily_summary(user_token: str, user_secret: str, date: dt.date) -> dict:
    """
    Pull a single day's wellness summary for a user.
    Returns dict with heart rate, steps, stress, sleep etc.
    """
    oauth = OAuth1(
        GARMIN_CONSUMER_KEY,
        GARMIN_CONSUMER_SECRET,
        resource_owner_key=user_token,
        resource_owner_secret=user_secret,
    )

    date_str = date.strftime("%Y-%m-%d")

    endpoints = {
        "dailies":       f"{GARMIN_API_BASE}/dailies?uploadStartTimeInSeconds=&uploadEndTimeInSeconds=",
        "sleep":         f"{GARMIN_API_BASE}/epochs/dailies?uploadStartTimeInSeconds=&uploadEndTimeInSeconds=",
        "heart_rate":    f"{GARMIN_API_BASE}/dailyHeartRate/{date_str}",
        "stress":        f"{GARMIN_API_BASE}/stressDetails?uploadStartTimeInSeconds=&uploadEndTimeInSeconds=",
        "body_battery":  f"{GARMIN_API_BASE}/bodyBattery?uploadStartTimeInSeconds=&uploadEndTimeInSeconds=",
        "hrv":           f"{GARMIN_API_BASE}/hrv?uploadStartTimeInSeconds=&uploadEndTimeInSeconds=",
        "respiration":   f"{GARMIN_API_BASE}/respiration?uploadStartTimeInSeconds=&uploadEndTimeInSeconds=",
    }

    results = {}
    for key, url in endpoints.items():
        try:
            r = requests.get(url, auth=oauth, timeout=10)
            if r.status_code == 200:
                results[key] = r.json()
        except Exception as e:
            results[key] = {"error": str(e)}

    return results


# ── Parse a Push payload into fields for your Google Sheet ───
def parse_push_payload(payload: dict) -> dict:
    """
    Garmin pushes JSON to your webhook after each sync.
    This extracts the fields relevant to your app's columns.
    """
    out = {}

    # Daily summary
    for daily in payload.get("dailies", []):
        out["garmin_steps"]          = daily.get("steps")
        out["garmin_avg_hr"]         = daily.get("averageHeartRateInBeatsPerMinute")
        out["garmin_max_hr"]         = daily.get("maxHeartRateInBeatsPerMinute")
        out["garmin_resting_hr"]     = daily.get("restingHeartRateInBeatsPerMinute")
        out["garmin_calories"]       = daily.get("activeKilocalories")
        out["garmin_intensity_mins"] = daily.get("moderateIntensityMinutes", 0) + \
                                       daily.get("vigorousIntensityMinutes", 0)
        out["garmin_stress_avg"]     = daily.get("averageStressLevel")
        out["garmin_body_battery_low"]  = daily.get("bodyBatteryLowestValue")
        out["garmin_body_battery_high"] = daily.get("bodyBatteryHighestValue")

    # Sleep
    for sleep in payload.get("sleeps", []):
        out["garmin_sleep_duration_s"]    = sleep.get("durationInSeconds")
        out["garmin_sleep_deep_s"]        = sleep.get("deepSleepDurationInSeconds")
        out["garmin_sleep_light_s"]       = sleep.get("lightSleepDurationInSeconds")
        out["garmin_sleep_rem_s"]         = sleep.get("remSleepInSeconds")
        out["garmin_sleep_awake_s"]       = sleep.get("awakeDurationInSeconds")
        out["garmin_sleep_score"]         = sleep.get("overallSleepScore", {}).get("value")
        out["garmin_sleep_avg_stress"]    = sleep.get("averageSpO2Value")
        out["garmin_avg_hrv"]             = sleep.get("averageHrvStatus")

    # Activity (from activity push)
    for activity in payload.get("activities", []):
        out["garmin_activity_name"]       = activity.get("activityName")
        out["garmin_activity_type"]       = activity.get("activityType")
        out["garmin_activity_duration_s"] = activity.get("durationInSeconds")
        out["garmin_activity_distance_m"] = activity.get("distanceInMeters")
        out["garmin_activity_avg_hr"]     = activity.get("averageHeartRateInBeatsPerMinute")
        out["garmin_activity_max_hr"]     = activity.get("maxHeartRateInBeatsPerMinute")
        out["garmin_training_effect"]     = activity.get("aerobicTrainingEffect")
        out["garmin_load"]                = activity.get("activityTrainingLoad")

    # HRV
    for hrv in payload.get("hrv", []):
        out["garmin_hrv_weekly_avg"]  = hrv.get("hrvSummary", {}).get("weeklyAvg")
        out["garmin_hrv_last_night"]  = hrv.get("hrvSummary", {}).get("lastNight")
        out["garmin_hrv_5min_high"]   = hrv.get("hrvSummary", {}).get("lastNight5MinHigh")
        out["garmin_hrv_status"]      = hrv.get("hrvSummary", {}).get("status")

    return out


# ── Convert Garmin wellness → your app's 1–5 slider scales ──
def garmin_to_app_scales(garmin_data: dict) -> dict:
    """
    Maps objective Garmin metrics to the 1-5 subjective scales
    your app already uses, so Garmin can pre-fill the sliders.
    These are suggestions — the athlete can still override them.
    """
    out = {}

    # Sleep quality 1–5 from Garmin sleep score (0–100)
    sleep_score = garmin_data.get("garmin_sleep_score")
    if sleep_score is not None:
        out["Sleep_1_5"] = max(1, min(5, round(sleep_score / 20)))

    # Fatigue (energy) 1–5 from Body Battery high point (0–100)
    # High body battery = energetic = high score
    bb_high = garmin_data.get("garmin_body_battery_high")
    if bb_high is not None:
        out["Fatigue_1_5"] = max(1, min(5, round(bb_high / 20)))

    # Stress → soreness proxy (high stress = higher soreness tendency)
    # Lower avg stress = lower soreness
    stress = garmin_data.get("garmin_stress_avg")
    if stress is not None:
        # Stress 0–100: invert and scale to 1–5
        out["Soreness_1_5"] = max(1, min(5, round(stress / 20)))

    # HRV status → readiness signal
    hrv_status = garmin_data.get("garmin_hrv_status")
    hrv_map = {"POOR": 1, "LOW": 2, "UNBALANCED": 2, "BALANCED": 4, "HIGH": 5}
    if hrv_status:
        out["hrv_readiness_signal"] = hrv_map.get(hrv_status.upper(), 3)

    # Load from activity
    garmin_load = garmin_data.get("garmin_load")
    if garmin_load is not None:
        out["Load"] = round(float(garmin_load), 1)


    return out