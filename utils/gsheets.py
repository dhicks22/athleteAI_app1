import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import os

def get_gsheet_connection():
    key_path = os.getenv("GS_SERVICE_JSON", "service-account.json")
    scopes = ["https://spreadsheets.google.com/feeds",
              "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(key_path, scopes)
    client = gspread.authorize(creds)
    return client

def get_tabs(sheet_id):
    client = get_gsheet_connection()
    sh = client.open_by_key(sheet_id)
    return [w.title for w in sh.worksheets()]

def load_tab(sheet_id, tab_name):
    client = get_gsheet_connection()
    ws = client.open_by_key(sheet_id).worksheet(tab_name)
    data = ws.get_all_records()
    df = pd.DataFrame(data)
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    return df

def write_row(sheet_id, tab_name, row_idx, row_dict):
    client = get_gsheet_connection()
    ws = client.open_by_key(sheet_id).worksheet(tab_name)
    for col, val in row_dict.items():
        try:
            col_idx = ws.row_values(1).index(col) + 1
            ws.update_cell(row_idx + 1, col_idx, val)
        except Exception as e:
            print(f"⚠️ Write error for {col}: {e}")
