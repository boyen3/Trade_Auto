# core/state_store.py
import json
import os
from utils.logger import logger

class StateStore:
    def __init__(self, file_path="data/system_state.json"):
        """
        初始化持久化倉庫。
        :param file_path: 狀態檔案儲存路徑
        """
        self.file_path = file_path
        self._ensure_dir()
        # 初始化數據結構，確保包含 strategies 和 global 兩個主要欄位
        self.data = self._load()

    def _ensure_dir(self):
        """確保儲存目錄存在"""
        directory = os.path.dirname(self.file_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)

    def _load(self):
        """從硬碟載入記憶"""
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"❌ 讀取持久化檔案失敗: {e}")
        
        # 如果檔案不存在或讀取失敗，回傳初始結構
        return {
            "strategies": {}, 
            "global": {
                "daily_pnl": 0.0,
                "last_run": ""
            }
        }

    def save(self):
        """將目前記憶寫入硬碟"""
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
            logger.error(f"❌ 儲存持久化檔案失敗: {e}")

    def update_strategy_state(self, strategy_name: str, state: dict):
        """
        更新特定策略的內部狀態。
        例如：紀錄某策略是否已達到今日交易次數上限。
        """
        if strategy_name not in self.data["strategies"]:
            self.data["strategies"][strategy_name] = {}
        
        # 進行增量更新，不覆蓋原有的其他變數
        self.data["strategies"][strategy_name].update(state)
        self.save()

    def get_strategy_state(self, strategy_name: str) -> dict:
        """獲取特定策略的歷史狀態"""
        return self.data["strategies"].get(strategy_name, {})

    def update_global_state(self, key, value):
        """更新全局狀態（如帳戶總損益限制）"""
        self.data["global"][key] = value
        self.save()