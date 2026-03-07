# core/data_hub.py
import pandas as pd
from typing import Dict, List, Optional
from models.order import PositionModel
from models.account import AccountModel

class DataHub:
    """
    中央數據匯流排：負責存儲系統所有組件共享的最新狀態。
    這是一個單例模式 (Singleton) 的設計思想。
    """
    # 存儲各商品的 K 線 DataFrame: { "contract_id": pd.DataFrame }
    bars: Dict[str, pd.DataFrame] = {}
    
    # 存儲當前帳戶持倉: { "contract_id": PositionModel }
    positions: Dict[str, PositionModel] = {}
    
    # 帳戶概況 (原本的變數名稱)
    account_info: Optional[AccountModel] = None
    
    # 全局風險標誌
    is_system_safe: bool = True

    @classmethod
    def update_bars(cls, contract_id: str, df: pd.DataFrame):
        cls.bars[contract_id] = df

    @classmethod
    def update_positions(cls, pos_list: List[PositionModel]):
        # 先清空舊有的，確保同步
        cls.positions = {p.contractId: p for p in pos_list}

    @classmethod
    def update_account(cls, account_data: AccountModel):
        """更新帳戶資訊 (供 OrderMonitor 呼叫)"""
        cls.account_info = account_data

    @classmethod
    def get_bars(cls, contract_id: str) -> pd.DataFrame:
        return cls.bars.get(contract_id, pd.DataFrame())

    @classmethod
    def get_position(cls, contract_id: str) -> Optional[PositionModel]:
        return cls.positions.get(contract_id)
    
    @classmethod
    def get_account(cls, account_id: int = None) -> Optional[AccountModel]:
        """
        獲取帳戶的緩存數據。
        因為目前設計是單一帳戶監控，所以直接回傳 cls.account_info。
        """
        return cls.account_info