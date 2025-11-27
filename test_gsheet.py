from google.oauth2 import service_account
from googleapiclient.discovery import build
import os
from dotenv import load_dotenv

load_dotenv()

SERVICE_FILE = os.getenv("GS_SERVICE_JSON")
SHEET_ID = os.getenv("GSHEET_ID")

creds = service_account.Credentials.from_service_account_file(SERVICE_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
service = build("sheets", "v4", credentials=creds)
result = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()

print("✅ Connected to Google Sheet!")
print("Sheet tabs:", [s['properties']['title'] for s in result['sheets']])
