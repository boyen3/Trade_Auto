class TopstepXError(Exception):
    """所有 ProjectX API 錯誤的基底類別"""
    pass

class AuthError(TopstepXError):
    """認證或 Token 相關錯誤"""
    pass

class RateLimitError(TopstepXError):
    """觸發 API 頻率限制 (429)"""
    pass

class APIResponseError(TopstepXError):
    """API 返回非預期的狀態碼或錯誤訊息"""
    def __init__(self, status_code, message):
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")
