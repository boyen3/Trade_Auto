from core.client import TopstepXClient
from models.account import AccountSearchResponse, AccountModel
from core.config import Config
from typing import List, Optional

class AccountService:
    def __init__(self, client: TopstepXClient):
        self.client = client
        self.config = Config()

    async def get_all_accounts(self) -> List[AccountModel]:
        """獲取所有可用帳戶"""
        payload = {"onlyActiveAccounts": True}
        data = await self.client.request("POST", "/api/Account/search", json=payload)
        
        response = AccountSearchResponse(**data)
        return response.accounts

    async def get_primary_account(self) -> Optional[AccountModel]:
        """
        自動獲取主要帳戶：
        1. 優先找 .env 裡指定的 DEFAULT_ACCOUNT_ID
        2. 若沒指定，則回傳第一個找到的帳戶
        """
        accounts = await self.get_all_accounts()
        if not accounts:
            return None

        # 如果 .env 有設定 ID，嘗試匹配
        if self.config.ACCOUNT_ID:
            for acc in accounts:
                if acc.id == self.config.ACCOUNT_ID:
                    print(f"📍 使用 .env 指定帳戶: {acc.name} ({acc.id})")
                    return acc
        
        # 若無指定或找不到，自動回傳第一個
        primary = accounts[0]
        print(f"🤖 自動選取首位帳戶: {primary.name} ({primary.id})")
        return primary

    async def get_account_by_name(self, name_snippet: str) -> Optional[AccountModel]:
        """支援模糊搜尋，方便切換帳戶 (例如傳入 '6927')"""
        accounts = await self.get_all_accounts()
        for acc in accounts:
            if name_snippet in acc.name:
                return acc
        return None
