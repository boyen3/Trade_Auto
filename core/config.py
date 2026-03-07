import os
from pathlib import Path
from dotenv import load_dotenv

# 定義根目錄 (E:\Trade_Auto)
ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"

# 確保以 UTF-8 讀取設定檔
if ENV_PATH.exists():
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        load_dotenv(stream=f)

class Config:
    USERNAME = os.getenv('TOPSTEP_USERNAME', '')
    API_KEY = os.getenv('TOPSTEP_API_KEY', '')
    BASE_URL = os.getenv('BASE_URL', 'https://api.topstepx.com')
    
    raw_account_id = os.getenv('DEFAULT_ACCOUNT_ID')
    ACCOUNT_ID = int(raw_account_id) if raw_account_id and raw_account_id.isdigit() else None

    @classmethod
    def validate(cls):
        if not cls.API_KEY or not cls.USERNAME:
            print(f"❌ 錯誤：.env 中缺少 USERNAME 或 API_KEY")
            return False
        return True