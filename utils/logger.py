# utils/logger.py
import logging
from pathlib import Path
from datetime import datetime

# 使用絕對路徑，無論從哪個目錄啟動都正確
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

def setup_logger(name: str = "ProjectX") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # Logger 本身接收所有等級

    if logger.handlers:
        return logger

    today = datetime.now().strftime("%Y-%m-%d")
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%H:%M:%S"
    )

    # --- INFO 以上 → logs/YYYY-MM-DD_info.log ---
    info_handler = logging.FileHandler(
        LOG_DIR / f"{today}_info.log", encoding="utf-8"
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)

    # --- WARNING 以上 → logs/YYYY-MM-DD_error.log ---
    error_handler = logging.FileHandler(
        LOG_DIR / f"{today}_error.log", encoding="utf-8"
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)

    # --- Console：INFO 以上，Win7 終端機友善（不用 emoji 著色） ---
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(info_handler)
    logger.addHandler(error_handler)
    logger.addHandler(console_handler)

    return logger


# 全局 logger，所有模組 import 這一個即可
logger = setup_logger()


# ──────────────────────────────────────────────
# 結構化日誌輔助函式
# 讓各模組用統一格式記錄，方便事後搜尋
# ──────────────────────────────────────────────

def log_order_placed(contract_id: str, side: int, size: int, order_type: int,
                     order_id: int, sl_ticks: int = None, tp_ticks: int = None):
    side_str = "BUY" if side == 0 else "SELL"
    type_str = {1: "MARKET", 2: "LIMIT", 3: "STOP"}.get(order_type, str(order_type))
    sl_str = f" | SL:{sl_ticks}ticks" if sl_ticks else ""
    tp_str = f" | TP:{tp_ticks}ticks" if tp_ticks else ""
    logger.info(f"[ORDER_PLACED] #{order_id} | {contract_id} | {side_str} {size}口 | {type_str}{sl_str}{tp_str}")


def log_order_cancelled(order_id: int, contract_id: str = ""):
    logger.info(f"[ORDER_CANCELLED] #{order_id} | {contract_id}")


def log_order_filled(order_id: int, contract_id: str, side: int, size: int, price: float):
    side_str = "BUY" if side == 0 else "SELL"
    logger.info(f"[ORDER_FILLED] #{order_id} | {contract_id} | {side_str} {size}口 @ {price:.2f}")


def log_account_snapshot(balance: float, realized_pnl: float):
    logger.info(f"[ACCOUNT] 餘額:${balance:,.2f} | 已實現盈虧:${realized_pnl:+.2f}")


def log_position_pnl(contract_id: str, side: int, size: int,
                     avg_price: float, current_price: float, unrealized_pnl: float):
    side_str = "BUY" if side == 0 else "SELL"
    logger.info(
        f"[POSITION] {contract_id} | {side_str} {size}口 | "
        f"均價:{avg_price:.2f} 現價:{current_price:.2f} | 浮盈:${unrealized_pnl:+.2f}"
    )


def log_risk_triggered(reason: str, total_pnl: float, limit: float):
    logger.warning(f"[RISK_TRIGGERED] {reason} | 當前損益:${total_pnl:.2f} | 限制:${limit:.2f}")


def log_api_error(endpoint: str, status_code: int, message: str, attempt: int = 1):
    logger.warning(f"[API_ERROR] {endpoint} | HTTP {status_code} | {message} | 第{attempt}次嘗試")


def log_api_retry(endpoint: str, attempt: int, wait_sec: float):
    logger.warning(f"[API_RETRY] {endpoint} | 第{attempt}次重試 | 等待{wait_sec}s")
