"""Загрузка конфигурации из .env."""
import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан в .env")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
if ADMIN_ID == 0:
    raise SystemExit("ADMIN_ID не задан или равен 0 в .env")
EXCEL_FILE = os.getenv("EXCEL_FILE", "schedule.xlsx")
