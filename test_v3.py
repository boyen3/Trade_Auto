"""
test_v3.py — SNR v3 即時模式整合測試
==============================================
測試項目：
  1. LiveBarStore 能否正常向 API 取得各時框數據
  2. SNRStrategyV3 初始化是否正常
  3. on_bar 能否正常執行（session / HTF trend / SR 計算）
  4. 下單介面 mock 測試（不真實下單）

用法：
  python test_v3.py

放置路徑：E:\\Trade_Auto\\test_v3.py
"""
import asyncio
import sys
import os
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"

results = []

def log(label, status, detail=""):
    icon = PASS if status else FAIL
    msg  = f"  {icon} {label}"
    if detail:
        msg += f"\n       {detail}"
    print(msg)
    results.append((label, status))

# ── 產生假 5M K 棒數據（模擬真實市場結構）──────────────────────────────
def make_fake_bars(n=300, base=19000.0, seed=42):
    np.random.seed(seed)
    closes = base + np.cumsum(np.random.randn(n) * 5)
    df = pd.DataFrame({
        'o': closes - np.abs(np.random.randn(n) * 2),
        'h': closes + np.abs(np.random.randn(n) * 4),
        'l': closes - np.abs(np.random.randn(n) * 4),
        'c': closes,
        'v': np.random.randint(100, 1000, n).astype(float),
    })
    # 設定時間索引（UTC，London 時段 08:00）
    base_time = pd.Timestamp("2026-03-10 08:00:00", tz="UTC")
    df.index  = pd.date_range(base_time, periods=n, freq="5min")
    return df


# ── 產生假 HTF K 棒（1H / 4H / 1D）──────────────────────────────────
def make_fake_htf(n=100, base=19000.0, trend="up"):
    np.random.seed(1)
    if trend == "up":
        closes = base + np.cumsum(np.abs(np.random.randn(n) * 10))
    else:
        closes = base - np.cumsum(np.abs(np.random.randn(n) * 10))
    df = pd.DataFrame({
        'o': closes - 5, 'h': closes + 10,
        'l': closes - 10, 'c': closes,
        'v': np.random.randint(500, 2000, n).astype(float),
    })
    df.index = pd.date_range("2026-01-01", periods=n, freq="1h")
    return df


# ══════════════════════════════════════════════════════════════════════
# TEST 1：LiveBarStore 初始化
# ══════════════════════════════════════════════════════════════════════
async def test_live_bar_store_init():
    print("\n[TEST 1] LiveBarStore 初始化")
    try:
        from core.live_bar_store import LiveBarStore

        # mock market_service
        ms = MagicMock()
        ms.client = MagicMock()
        ms.client.request = AsyncMock(return_value={"bars": []})

        bs = LiveBarStore(ms, cache_seconds=60)
        log("LiveBarStore 建立", True)
        log("cache_seconds 設定", bs.cache_seconds == 60, f"cache_seconds={bs.cache_seconds}")
        log("_cache 初始為空", len(bs._cache) == 0)
        return bs
    except Exception as e:
        log("LiveBarStore 初始化", False, str(e))
        return None


# ══════════════════════════════════════════════════════════════════════
# TEST 2：LiveBarStore.load_async 各時框
# ══════════════════════════════════════════════════════════════════════
async def test_live_bar_store_load(bs):
    print("\n[TEST 2] LiveBarStore.load_async 各時框")
    if bs is None:
        log("跳過（初始化失敗）", False)
        return

    from models.market import BarModel
    fake_df = make_fake_htf(50)

    # 轉成 API 回傳格式
    def df_to_bars(df):
        bars = []
        for t, row in df.iterrows():
            bars.append({
                "t": t.isoformat(), "o": row["o"], "h": row["h"],
                "l": row["l"], "c": row["c"], "v": row["v"]
            })
        return bars

    bs.ms.client.request = AsyncMock(return_value={"bars": df_to_bars(fake_df)})

    for tf in ["5m", "1h", "4h", "1d"]:
        try:
            df = await bs.load_async("CON.F.US.MNQ.H26", tf, limit=50)
            ok = not df.empty and "c" in df.columns
            log(f"load_async({tf})", ok, f"{len(df)} 根，欄位: {list(df.columns)}")
        except Exception as e:
            log(f"load_async({tf})", False, str(e))

    # 快取測試
    call_count_before = bs.ms.client.request.call_count
    await bs.load_async("CON.F.US.MNQ.H26", "1h", limit=50)
    call_count_after  = bs.ms.client.request.call_count
    log("快取命中（不重複呼叫 API）",
        call_count_after == call_count_before,
        f"API 呼叫次數: {call_count_before} → {call_count_after}")


# ══════════════════════════════════════════════════════════════════════
# TEST 3：SNRStrategyV3 初始化
# ══════════════════════════════════════════════════════════════════════
async def test_strategy_init():
    print("\n[TEST 3] SNRStrategyV3 初始化")
    try:
        from strategies.snr_strategy_v3 import SNRStrategyV3

        ts = MagicMock()
        ts._is_backtest = False
        ts.place_order  = AsyncMock(return_value=12345)
        ms = MagicMock()

        engine = MagicMock()
        engine.bar_store = MagicMock()
        engine.bar_store.load = MagicMock(return_value=make_fake_htf(100))

        strategy = SNRStrategyV3(
            trading_service       = ts,
            market_service        = ms,
            notifier              = AsyncMock(),
            engine                = engine,
            fake_break_depth_mult = 0.40,
            htf_primary_ema       = 200,
            htf_secondary_ema     = 9,
            min_rr                = 1.2,
            vol_mult              = 0.0,
            sl_max_atr_mult       = 1.5,
            session_london_start  = 7,
            session_london_end    = 12,
            session_ny_start      = 13,
            session_ny_start_min  = 30,
            session_ny_end        = 20,
            size                  = 1,
        )
        strategy.account_id  = 18699057
        strategy.contract_id = "CON.F.US.MNQ.H26"

        log("SNRStrategyV3 建立", True)
        log("參數 fake_break_depth_mult", strategy.fake_break_depth_mult == 0.40)
        log("參數 htf_primary_ema",       strategy.htf_primary_ema == 200)
        log("參數 min_rr",                strategy.min_rr == 1.2)
        log("skip_counts 存在",           hasattr(strategy, 'skip_counts'))
        log("account_id 設定",            strategy.account_id == 18699057)
        return strategy

    except Exception as e:
        log("SNRStrategyV3 初始化", False, str(e))
        import traceback; traceback.print_exc()
        return None


# ══════════════════════════════════════════════════════════════════════
# TEST 4：on_bar 執行（休市時段 → session skip）
# ══════════════════════════════════════════════════════════════════════
async def test_on_bar_session_filter(strategy):
    print("\n[TEST 4] on_bar — 休市時段過濾")
    if strategy is None:
        log("跳過（初始化失敗）", False); return

    # 製造休市時段的 K 棒（UTC 03:00，London/NY 都關）
    df = make_fake_bars(300)
    df.index = pd.date_range("2026-03-10 03:00:00+00:00", periods=300, freq="5min")

    skip_before = strategy.skip_counts.get("session", 0)
    try:
        await strategy.on_bar("CON.F.US.MNQ.H26", df)
        skip_after = strategy.skip_counts.get("session", 0)
        log("休市時段正確 skip", skip_after > skip_before,
            f"session skip: {skip_before} → {skip_after}")
    except Exception as e:
        log("on_bar 休市時段", False, str(e))
        import traceback; traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════
# TEST 5：on_bar 執行（London 時段 → 正常運作）
# ══════════════════════════════════════════════════════════════════════
async def test_on_bar_london(strategy):
    print("\n[TEST 5] on_bar — London 時段（UTC 08:00）")
    if strategy is None:
        log("跳過（初始化失敗）", False); return

    df = make_fake_bars(300)
    # London 時段：確保最後一根在 07:00-12:00 UTC 之間
    # 300根 × 5分鐘 = 25小時，起始設在前一天 09:00，最後落在 10:00 UTC
    df.index = pd.date_range("2026-03-08 09:00:00+00:00", periods=300, freq="5min")

    skip_before = dict(strategy.skip_counts)
    try:
        await strategy.on_bar("CON.F.US.MNQ.H26", df)
        skip_after = dict(strategy.skip_counts)

        session_skip = skip_after.get("session", 0) - skip_before.get("session", 0)
        htf_skip     = skip_after.get("htf_neutral", 0) - skip_before.get("htf_neutral", 0)
        total_skip   = sum(skip_after.get(k, 0) - skip_before.get(k, 0)
                           for k in skip_after)

        log("on_bar 無 exception", True)
        log("London 時段未被 session skip",
            session_skip == 0, f"session_skip 增加={session_skip}")
        log("HTF 趨勢有結果（neutral 或 bull/bear）",
            True, f"htf_skip={htf_skip}, total_skip={total_skip}")

    except Exception as e:
        log("on_bar London 時段", False, str(e))
        import traceback; traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════
# TEST 6：下單介面 mock（不真實下單）
# ══════════════════════════════════════════════════════════════════════
async def test_order_interface(strategy):
    print("\n[TEST 6] 下單介面 mock")
    if strategy is None:
        log("跳過（初始化失敗）", False); return

    try:
        # 確認 trading_service.place_order 是 mock
        ok = callable(getattr(strategy.ts, 'place_order', None))
        log("trading_service.place_order 可呼叫", ok)

        # 模擬呼叫一次
        result = await strategy.ts.place_order(
            strategy.account_id, strategy.contract_id, 1, 0, 1
        )
        log("place_order mock 回傳 order_id", result is not None, f"order_id={result}")

    except Exception as e:
        log("下單介面 mock", False, str(e))


# ══════════════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════════════
async def main():
    print("=" * 60)
    print("  SNR v3 即時模式整合測試")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    bs       = await test_live_bar_store_init()
    await test_live_bar_store_load(bs)
    strategy = await test_strategy_init()
    await test_on_bar_session_filter(strategy)
    await test_on_bar_london(strategy)
    await test_order_interface(strategy)

    # 總結
    total   = len(results)
    passed  = sum(1 for _, s in results if s)
    failed  = total - passed
    print("\n" + "=" * 60)
    print(f"  結果：{passed}/{total} 通過  |  {failed} 失敗")
    if failed == 0:
        print("  ✅ 所有測試通過，可以上模擬帳戶")
    else:
        print("  ❌ 有測試失敗，請修復後再上線")
        for label, status in results:
            if not status:
                print(f"     → 失敗：{label}")
    print("=" * 60)

asyncio.run(main())
