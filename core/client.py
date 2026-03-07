# core/client.py
import httpx
import asyncio
import json
from datetime import datetime, timedelta
from core.config import Config
from utils.logger import logger


class TopstepXClient:
    def __init__(self):
        self.config = Config()
        self.base_url = self.config.BASE_URL
        self.access_token = None
        self.token_expiry = None
        self._login_lock = asyncio.Lock()  # 防止並發重登

        self.http_client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={
                "accept": "application/json",
                "Content-Type": "application/json"
            }
        )

    @property
    def token(self):
        return self.access_token

    async def login(self) -> bool:
        async with self._login_lock:
            if not self.config.validate():
                return False
            try:
                response = await self.http_client.post("/api/Auth/loginKey", json={
                    "userName": self.config.USERNAME,
                    "apiKey": self.config.API_KEY
                })
                data = response.json()
                if data.get("success"):
                    self.access_token = data.get("token")
                    self.token_expiry = datetime.now() + timedelta(hours=23)
                    self.http_client.headers.update({"Authorization": f"Bearer {self.access_token}"})
                    logger.info(f"✅ 登入成功 | Token 有效至 {self.token_expiry.strftime('%H:%M:%S')}")
                    return True
                logger.error(f"❌ 登入失敗: {data.get('errorMessage', '未知錯誤')}")
                return False
            except Exception as e:
                logger.error(f"❌ 登入異常: {e}")
                return False

    async def start_token_refresh(self):
        """
        背景 Task：每分鐘檢查 Token 剩餘時間。
        剩不到 30 分鐘時主動重登，避免交易中途 Token 過期。
        """
        while True:
            await asyncio.sleep(60)
            if self.token_expiry:
                remaining = (self.token_expiry - datetime.now()).total_seconds()
                if 0 < remaining < 1800:
                    logger.info(f"🔄 Token 剩餘 {remaining/60:.0f} 分鐘，主動重登...")
                    await self.login()
                elif remaining <= 0:
                    logger.warning("⚠️ Token 已過期，嘗試重登...")
                    await self.login()

    async def request(self, method: str, endpoint: str, max_retries: int = 3, **kwargs):
        # Token 過期時重登
        if not self.access_token or datetime.now() >= self.token_expiry:
            await self.login()

        for attempt in range(max_retries):
            try:
                response = await self.http_client.request(method, endpoint, **kwargs)

                if response.status_code == 429:
                    wait_time = (attempt + 1) * 2
                    logger.warning(f"⚠️ API 頻率受限，{wait_time}s 後第 {attempt+1} 次重試...")
                    await asyncio.sleep(wait_time)
                    continue

                if response.status_code >= 400:
                    logger.warning(f"🔴 API 錯誤響應 ({response.status_code}): {endpoint} | {response.text}")
                    if response.status_code == 401 and attempt < max_retries - 1:
                        await self.login()
                        continue

                if not response.text:
                    return {"success": False, "errorMessage": "Empty Response"}

                return response.json()

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                if attempt == max_retries - 1:
                    return {"success": False, "errorMessage": f"網路連線最終失敗: {str(e)}"}
                wait_time = (attempt + 1) * 2
                logger.warning(f"⚠️ 網路異常，{wait_time}s 後第 {attempt+1} 次重試...")
                await asyncio.sleep(wait_time)

            except json.JSONDecodeError:
                return {"success": False, "errorMessage": "Invalid JSON"}
            except Exception as e:
                return {"success": False, "errorMessage": str(e)}

    async def close(self):
        await self.http_client.aclose()
