# backtest/engine.py
"""
SNR v3 回測引擎
================
直接使用 strategies/snr_strategy_v3.py，策略邏輯零複製。

放置位置：
  E:\\Trade_Auto\\backtest\\__init__.py   （空檔）
  E:\\Trade_Auto\\backtest\\engine.py     （本檔）

執行方式（在 E:\\Trade_Auto 目錄下）：
  python -m backtest.engine

或在其他腳本中使用：
  import asyncio
  from backtest.engine import SNRBacktestRunner
  runner = SNRBacktestRunner()
  result = asyncio.run(runner.run())
  result.print_summary()

資料來源（自動讀取）：
  E:\\Trade_Auto\\data\\CON_F_US_MNQ_H26_15m.parquet
  E:\\Trade_Auto\\data\\CON_F_US_MNQ_H26_1h.parquet
  E:\\Trade_Auto\\data\\CON_F_US_MNQ_H26_4h.parquet
  E:\\Trade_Auto\\data\\CON_F_US_MNQ_H26_1d.parquet
"""

import asyncio
import sys
import os
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

# ── 確保能 import 專案模組 ────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── 常數 ──────────────────────────────────────────────────────────────────────
DATA_DIR  = _PROJECT_ROOT / "data_backtest"
TICK      = 0.25      # MNQ 最小跳動
POINT_VAL = 2.0       # MNQ $2 / point


# =============================================================================
# 模擬 BarStore
# =============================================================================

class BacktestBarStore:
    """
    回測用 BarStore（高速版）。
    - 所有時框一次預載入記憶體，不重複讀磁碟
    - 用 searchsorted 取代布林篩選，速度快 10-50 倍
    - iloc slice 不 copy，直接回傳 view（策略只讀，安全）
    """

    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self._cache:     Dict[str, pd.DataFrame] = {}
        self._idx_cache: Dict[str, np.ndarray]   = {}  # int64 時間戳陣列
        self.current_bar_time: Optional[datetime] = None

    def _load_full(self, contract_id: str, tf: str) -> pd.DataFrame:
        key = f"{contract_id}_{tf}"
        if key not in self._cache:
            fname = contract_id.replace(".", "_") + f"_{tf}.parquet"
            path  = self.data_dir / fname
            if path.exists():
                df = pd.read_parquet(path)
                if hasattr(df.index, 'tz') and df.index.tz is not None:
                    df.index = df.index.tz_convert(None)
                df = df.sort_index()
                self._cache[key]     = df
                self._idx_cache[key] = df.index.values.astype(np.int64)
            else:
                print(f"[BacktestBarStore] ⚠️  找不到 {path}，{tf} 時框將無法使用")
                self._cache[key]     = pd.DataFrame()
                self._idx_cache[key] = np.array([], dtype=np.int64)
        return self._cache[key]

    def preload_all(self, contract_id: str):
        """啟動時預載所有時框，避免首根 bar 觸發多次 I/O"""
        for tf in ["5m", "1h", "4h", "1d"]:
            df = self._load_full(contract_id, tf)
            if not df.empty:
                print(f"  [BarStore] 預載 {tf:>4} : {len(df):>7,} 根  "
                      f"({df.index[0].date()} ~ {df.index[-1].date()})")

    def load(self, contract_id: str, tf: str, limit: int = 500) -> pd.DataFrame:
        """
        回傳截止到 current_bar_time 的最新 limit 根 K 棒。
        searchsorted 比布林篩選快約 20 倍。
        """
        df = self._load_full(contract_id, tf)
        if df.empty:
            return df

        if self.current_bar_time is not None:
            cutoff_ns = np.int64(pd.Timestamp(self.current_bar_time).value)
            idx_arr   = self._idx_cache[f"{contract_id}_{tf}"]
            pos = int(np.searchsorted(idx_arr, cutoff_ns, side='right'))
            if pos == 0:
                return df.iloc[0:0]
            start = max(0, pos - limit)
            return df.iloc[start:pos]   # view，不 copy
        else:
            return df.tail(limit)


# =============================================================================
# 回測成交記錄
# =============================================================================

@dataclass
class BacktestTrade:
    order_id:     int
    side:         int          # 0=long, 1=short
    entry_price:  float
    sl_price:     float        # 初始止損（可能被 modify_order 更新）
    tp_price:     float
    entry_bar:    int
    entry_time:   datetime

    exit_price:   float = 0.0
    exit_bar:     int   = 0
    exit_time:    Optional[datetime] = None
    exit_reason:  str   = ""   # "SL" | "TP" | "trailing_SL" | "strategy_close" | "end_of_data"
    pnl_pts:      float = 0.0
    pnl_usd:      float = 0.0
    partial_done: bool  = False
    partial_pnl:  float = 0.0  # 部分止盈已入帳的金額（加入最終 pnl_usd）
    trailing_active: bool = False

    @property
    def side_str(self) -> str:
        return "long" if self.side == 0 else "short"

    @property
    def r_multiple(self) -> float:
        """這筆交易的 R 倍數（相對於初始風險）"""
        initial_risk = abs(self.entry_price - self.sl_price)
        if initial_risk <= 0:
            return 0.0
        return self.pnl_pts / initial_risk


# =============================================================================
# 模擬 TradingService
# =============================================================================

class BacktestTradingService:
    """
    攔截策略的所有交易呼叫：
      place_order   → 記錄新倉位
      modify_order  → 更新 SL（移動止損）
      close_position → 主動平倉
    """

    def __init__(self, runner: "SNRBacktestRunner"):
        self._runner  = runner
        self._next_id = 1000
        self.client       = self   # 讓 _find_bracket_order_ids 用 self.client.request
        self._pending_order: Optional[dict] = None
        self._is_backtest   = True   # 讓策略的 _do_partial_close 知道處於回測模式   # 暫存下單參數，等 active_trade 設好後再建立

    async def place_order(self, account_id, contract_id,
                          order_type, side, size,
                          sl_ticks=None, tp_ticks=None, **kwargs) -> Optional[int]:
        oid = self._next_id
        self._next_id += 1
        self._pending_order = {"oid": oid, "side": int(side)}
        return oid

    async def modify_order(self, account_id, order_id,
                           stop_price=None, limit_price=None, size=None) -> bool:
        if stop_price is not None:
            self._runner._on_modify_sl(order_id, float(stop_price))
        return True

    async def close_position(self, account_id, contract_id) -> bool:
        self._runner._on_strategy_close()
        return True

    async def partial_close_position(self, account_id, contract_id, size) -> bool:
        """
        回測中停用部分止盈：靜默忽略，讓剩餘部位繼續跑到 TP 或 trailing SL。
        策略會因為 partial_closed=True 不再重複呼叫，但平倉動作不執行。
        """
        if self._runner._strategy and self._runner._strategy.active_trade:
            self._runner._strategy.active_trade.partial_closed = True
        return True

    async def request(self, method, path, **kwargs) -> dict:
        """
        模擬 /api/Order/searchOpen：
        讓策略的 _find_bracket_order_ids 能找到 SL/TP 子單 ID，
        這樣 modify_order（移動止損）才能正常觸發。
        """
        if "searchOpen" in path and self._runner._active_trade:
            t = self._runner._active_trade
            return {
                "success": True,
                "orders": [
                    {   # SL 子單
                        "id": t.order_id + 1,
                        "contractId": self._runner.contract_id,
                        "side": 1 - t.side,        # 與進場方向相反
                        "stopPrice":  t.sl_price,
                        "limitPrice": None,
                    },
                    {   # TP 子單
                        "id": t.order_id + 2,
                        "contractId": self._runner.contract_id,
                        "side": 1 - t.side,
                        "stopPrice":  None,
                        "limitPrice": t.tp_price,
                    },
                ]
            }
        return {"success": True, "orders": []}


# =============================================================================
# 模擬 Engine（提供 bar_store 給策略）
# =============================================================================

class BacktestEngineProxy:
    def __init__(self, bar_store: BacktestBarStore):
        self.bar_store       = bar_store
        self.strategy_paused = False


# =============================================================================
# 模擬 Notifier（靜音，不發 Telegram）
# =============================================================================

class SilentNotifier:
    async def send_message(self, *args, **kwargs):
        pass


# =============================================================================
# 回測結果
# =============================================================================

@dataclass
class BacktestResult:
    trades:          List[BacktestTrade]
    equity_curve:    List[float]
    initial_balance: float
    params:          dict = field(default_factory=dict)

    @property
    def final_balance(self) -> float:
        return self.equity_curve[-1] if self.equity_curve else self.initial_balance

    @property
    def net_pnl(self) -> float:
        return self.final_balance - self.initial_balance

    def print_summary(self):
        trades = self.trades
        n = len(trades)

        if n == 0:
            print("=" * 55)
            print("  回測完成：0 筆交易")
            print("  建議：降低 sr_sensitivity 或 fake_break_depth_mult")
            print("=" * 55)
            return

        pnls   = [t.pnl_usd for t in trades]
        r_muls = [t.r_multiple for t in trades]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        wr     = len(wins) / n * 100
        pf     = abs(sum(wins) / sum(losses)) if losses else float("inf")
        avg_r  = np.mean(r_muls)

        eq  = np.array(self.equity_curve)
        pk  = np.maximum.accumulate(eq)
        dd  = (eq - pk) / pk * 100
        mdd = float(dd.min())

        exits   = {}
        for t in trades:
            exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1

        partial = sum(1 for t in trades if t.partial_done)
        longs   = sum(1 for t in trades if t.side == 0)
        shorts  = n - longs

        # 月度分析
        monthly: Dict[str, float] = {}
        for t in trades:
            if t.entry_time:
                key = t.entry_time.strftime("%Y-%m")
                monthly[key] = monthly.get(key, 0) + t.pnl_usd
        profit_months = sum(1 for v in monthly.values() if v > 0)
        total_months  = len(monthly)

        print("=" * 60)
        print("            SNR v3 策略回測結果")
        print("=" * 60)
        print(f"  總交易數  : {n} 筆  （多:{longs} / 空:{shorts}）")
        print(f"  勝率      : {wr:.1f}%  （{len(wins)}勝 / {len(losses)}敗）")
        print(f"  獲利因子  : {pf:.2f}")
        print(f"  平均 R    : {avg_r:+.2f}R")
        if wins:
            print(f"  平均獲利  : ${np.mean(wins):+.2f}  最大: ${max(wins):+.2f}")
        if losses:
            print(f"  平均虧損  : ${np.mean(losses):+.2f}  最大: ${min(losses):+.2f}")
        print(f"  淨利      : ${self.net_pnl:+.2f}  ({self.net_pnl/self.initial_balance*100:+.1f}%)")
        print(f"  最大回撤  : {mdd:.2f}%  (${(self.initial_balance * mdd/100):+.2f})")
        print(f"  部分止盈  : {partial}/{n} 筆 ({partial/n*100:.0f}%)")
        print(f"  獲利月份  : {profit_months}/{total_months} 個月")
        print(f"  出場原因  : {' | '.join(f'{k}:{v}' for k,v in sorted(exits.items()))}")
        print(f"  連勝最大  : {_max_streak(pnls, True)}  連虧最大: {_max_streak(pnls, False)}")
        print("-" * 60)
        if monthly:
            print("  月度損益:")
            for ym, pnl in sorted(monthly.items()):
                bar = "█" * min(int(abs(pnl) / 50), 20)
                sign = "+" if pnl >= 0 else "-"
                print(f"    {ym}  {sign}${abs(pnl):>7.0f}  {bar}")
        print("=" * 60)

        if self.params:
            print("  回測參數:")
            for k, v in self.params.items():
                print(f"    {k} = {v}")
            print("=" * 60)


def _max_streak(pnls, is_win):
    best = cur = 0
    for p in pnls:
        if (p > 0) == is_win:
            cur += 1; best = max(best, cur)
        else:
            cur = 0
    return best


# =============================================================================
# 主回測 Runner
# =============================================================================


class _NumpyDF:
    """
    輕量 DataFrame 替代品。
    欄位存取直接回傳預提取的 numpy slice，完全繞開 pandas 開銷。
    策略程式碼不需修改（df['c']、df.index[-1] 等介面相容）。
    """
    __slots__ = ('_arrays', '_index_vals', '_slice')

    def __init__(self, arrays: dict, index_vals, slc: slice):
        self._arrays     = arrays
        self._index_vals = index_vals
        self._slice      = slc

    def __getitem__(self, key):
        arr = self._arrays.get(key)
        if arr is None:
            raise KeyError(key)
        return _NumpySeries(arr[self._slice], self._index_vals[self._slice])

    def __len__(self):
        s = self._slice
        return len(range(*s.indices(len(self._index_vals))))

    @property
    def columns(self):
        return list(self._arrays.keys())

    @property
    def index(self):
        return _NumpyIndex(self._index_vals[self._slice])

    def tail(self, n):
        vals = self._index_vals[self._slice]
        start = max(0, len(vals) - n)
        full_start = self._slice.start or 0
        new_slc = slice(full_start + start, self._slice.stop)
        return _NumpyDF(self._arrays, self._index_vals, new_slc)

    def empty(self):
        return len(self) == 0

    @property
    def iloc(self):
        return _NumpyIloc(self)


class _NumpySeries:
    __slots__ = ('values', '_index')

    def __init__(self, arr, idx):
        self.values = arr
        self._index = idx

    def __len__(self):
        return len(self.values)

    def iloc(self, i):
        return self.values[i]

    def tail(self, n):
        return _NumpySeries(self.values[-n:], self._index[-n:])

    def mean(self):
        return float(np.mean(self.values))

    def __sub__(self, other):
        if isinstance(other, _NumpySeries):
            return _NumpySeries(self.values - other.values, self._index)
        return _NumpySeries(self.values - other, self._index)

    def __getitem__(self, key):
        return self.values[key]


class _NumpyIndex:
    __slots__ = ('_vals',)

    def __init__(self, vals):
        self._vals = vals

    def __getitem__(self, i):
        return self._vals[i]

    def __len__(self):
        return len(self._vals)

    @property
    def tz(self):
        return None


class _NumpyIloc:
    __slots__ = ('_df',)

    def __init__(self, df):
        self._df = df

    def __getitem__(self, i):
        # Return a row-like object for df.iloc[i]
        arrays = self._df._arrays
        slc    = self._df._slice
        idx    = self._df._index_vals[slc]
        if isinstance(i, int):
            row = {k: v[slc][i] for k, v in arrays.items()}
            row['_time'] = idx[i]
            return _NumpyRow(row)
        # slice
        start = (slc.start or 0) + (i.start or 0)
        stop  = (slc.start or 0) + (i.stop or len(idx))
        return _NumpyDF(arrays, self._df._index_vals, slice(start, stop))


class _NumpyRow:
    __slots__ = ('_data',)

    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]



# ── 輕量 DataFrame 替代品，讓策略不改任何程式碼的前提下完全繞開 pandas ────────

class _FastSeries:
    """df['c'] 的回傳值，支援 .values 和 .iloc[-1]"""
    __slots__ = ('values',)
    def __init__(self, arr):
        self.values = arr
    def __len__(self):
        return len(self.values)
    def __getitem__(self, i):
        return self.values[i]
    @property
    def iloc(self):
        return self.values   # numpy supports [-1] indexing directly
    def tail(self, n):
        return _FastSeries(self.values[-n:])
    def mean(self):
        return float(np.mean(self.values))
    def __sub__(self, other):
        if isinstance(other, _FastSeries):
            return _FastSeries(self.values - other.values)
        return _FastSeries(self.values - other)


class _FastIndex:
    """df.index 的替代品"""
    __slots__ = ('_vals',)
    def __init__(self, vals):
        self._vals = vals   # numpy datetime64 array
    def __getitem__(self, i):
        # 回傳 pandas Timestamp（策略用 .hour/.minute）
        import pandas as pd
        return pd.Timestamp(self._vals[i])
    def __len__(self):
        return len(self._vals)
    @property
    def tz(self):
        return None


class FastDF:
    """
    輕量 DataFrame 包裝。
    從真實 DataFrame 預提取 numpy arrays，後續存取完全繞開 pandas。
    策略所有 df['c']、df.index[-1]、len(df) 等介面全部支援。
    """
    __slots__ = ('_arrays', '_index', '_len')

    def __init__(self, arrays: dict, index_vals, length: int):
        self._arrays = arrays
        self._index  = _FastIndex(index_vals)
        self._len    = length

    def __getitem__(self, key):
        return _FastSeries(self._arrays[key])

    def __len__(self):
        return self._len

    @property
    def empty(self):
        return self._len == 0

    @property
    def columns(self):
        return list(self._arrays.keys())

    @property
    def index(self):
        return self._index

    def tail(self, n):
        new_arrays = {k: v[-n:] for k, v in self._arrays.items()}
        return FastDF(new_arrays, self._index._vals[-n:], min(n, self._len))

    def __contains__(self, key):
        return key in self._arrays


class SNRBacktestRunner:
    """
    核心邏輯：
      1. 讀取 15M Parquet
      2. 逐根 K 棒 → 先掃描 SL/TP → 再呼叫 strategy.on_bar
      3. 收集所有成交記錄，計算統計數據

    每根 K 棒的處理順序（重要）：
      ① 更新 bar_store.current_bar_time（防止未來洩漏）
      ② 掃描這根 K 棒的 H/L 是否觸及 SL 或 TP → 先結算
      ③ 更新 DataHub.positions（讓策略的 _manage_trade 看到正確持倉狀態）
      ④ 呼叫 strategy.on_bar(symbol, df_slice)
    """

    def __init__(self,
                 contract_id:     str   = "CON.F.US.MNQ.H26",
                 data_dir:        Path  = DATA_DIR,
                 initial_balance: float = 50000.0,
                 warmup_bars:     int   = 1800,
                 date_from:       str   = None,   # "2016-01-01"
                 date_to:         str   = None,   # "2021-12-31"
                 **strategy_kwargs):

        self.contract_id     = contract_id
        self.initial_balance = initial_balance
        self.warmup_bars     = warmup_bars
        self.date_from       = pd.Timestamp(date_from) if date_from else None
        self.date_to         = pd.Timestamp(date_to)   if date_to   else None
        self.strategy_kwargs = strategy_kwargs

        self._bar_store   = BacktestBarStore(data_dir)
        self._balance     = initial_balance
        self._equity      = [initial_balance]
        self._trades:     List[BacktestTrade] = []
        self._active_trade: Optional[BacktestTrade] = None
        self._strategy    = None

        # 當前 bar 資訊（給 _on_strategy_close 用）
        self._bar_idx     = 0
        self._bar_time    = None
        self._bar_close   = 0.0

    # ── DataHub patch ────────────────────────────────────────────────────────

    def _set_datahub_position(self, has_position: bool):
        """
        patch DataHub.positions，讓策略的 _manage_trade
        能透過 DataHub.positions.get(contract_id) 判斷持倉是否還在。
        """
        try:
            from core.data_hub import DataHub
            from models.order import PositionModel
            if has_position and self._active_trade:
                t = self._active_trade
                pos = PositionModel(
                    id           = 1,
                    contractId   = self.contract_id,
                    symbolName   = "MNQ",
                    side         = t.side,
                    size         = 1,
                    averagePrice = t.entry_price,
                    currentPrice = self._bar_close,
                )
                DataHub.positions = {self.contract_id: pos}
            else:
                DataHub.positions = {}
        except Exception:
            pass   # DataHub 未載入時靜默跳過

    # ── 策略回調 ─────────────────────────────────────────────────────────────

    def _finalize_pending_order(self):
        """
        在每根 on_bar 結束後呼叫。
        此時策略已設好 active_trade，可以讀取 sl/tp 價格建立 BacktestTrade。
        """
        trading_svc = getattr(self._strategy, 'trading_service', None)
        pending = getattr(trading_svc, '_pending_order', None) if trading_svc else None
        if pending is None:
            return

        if self._active_trade is not None:
            trading_svc._pending_order = None
            return

        strat = self._strategy
        if strat is None or strat.active_trade is None:
            trading_svc._pending_order = None
            return

        t = strat.active_trade
        bt = BacktestTrade(
            order_id    = pending["oid"],
            side        = 0 if t.side == "long" else 1,
            entry_price = t.entry_price,
            sl_price    = t.sl_price,
            tp_price    = t.tp_price,
            entry_bar   = self._bar_idx,
            entry_time  = self._bar_time,
        )
        self._active_trade = bt
        trading_svc._pending_order = None
        self._set_datahub_position(True)
        from utils.logger import logger
        logger.info(f"[Backtest] 開倉 #{bt.order_id} | "
                    f"{'多' if bt.side==0 else '空'} @ {bt.entry_price:.2f} "
                    f"| SL={bt.sl_price:.2f} TP={bt.tp_price:.2f}")

    def _on_modify_sl(self, order_id: int, new_sl: float):
        """策略呼叫 modify_order 移動止損"""
        if self._active_trade:
            self._active_trade.sl_price = new_sl
            self._active_trade.trailing_active = True
            # 同步回策略的 active_trade
            if self._strategy and self._strategy.active_trade:
                self._strategy.active_trade.sl_price = new_sl

    def _on_strategy_close(self):
        """策略主動平倉（部分止盈 / trailing 觸發）"""
        if self._active_trade is None:
            return
        self._settle(self._bar_close, "strategy_close")

    # ── 結算 ─────────────────────────────────────────────────────────────────

    def _settle(self, exit_price: float, reason: str):
        if self._active_trade is None:
            return

        bt = self._active_trade
        if bt.side == 0:   # long
            pnl_pts = exit_price - bt.entry_price
        else:              # short
            pnl_pts = bt.entry_price - exit_price

        pnl_usd = pnl_pts * POINT_VAL

        bt.exit_price  = exit_price
        bt.exit_bar    = self._bar_idx
        bt.exit_time   = self._bar_time
        bt.exit_reason = reason
        bt.pnl_pts     = pnl_pts
        bt.pnl_usd     = pnl_usd
        if self._strategy and self._strategy.active_trade:
            bt.partial_done = self._strategy.active_trade.partial_closed

        self._balance += pnl_usd
        self._trades.append(bt)
        self._active_trade = None

        # 清除策略狀態
        if self._strategy:
            self._strategy.active_trade = None
            self._strategy._sl_lookup_attempts = 0

        self._set_datahub_position(False)

    # ── SL / TP 掃描 ─────────────────────────────────────────────────────────

    def _scan_exits(self, bar: pd.Series):
        """舊介面保留，轉呼叫 numpy 版"""
        if bar is not None:
            self._scan_exits_np(float(bar['h']), float(bar['l']))

    def _scan_exits_np(self, h: float, l: float):
        """numpy 版，直接傳 h/l，完全繞開 pandas"""
        bt = self._active_trade
        if bt is None:
            return

        if bt.side == 0:   # long
            sl_hit = l <= bt.sl_price
            tp_hit = h >= bt.tp_price
        else:              # short
            sl_hit = h >= bt.sl_price
            tp_hit = l <= bt.tp_price

        if sl_hit:
            reason = "trailing_SL" if bt.trailing_active else "SL"
            self._settle(bt.sl_price, reason)
        elif tp_hit:
            self._settle(bt.tp_price, "TP")

    # ── HTF EMA 預算 ─────────────────────────────────────────────────────────

    def _precompute_htf_ema(self, tf1: str, period1: int,
                             tf2: str, period2: int) -> dict:
        """
        把 HTF EMA 全部預算成 {timestamp_ns: direction} 字典。
        策略每根 bar 直接查表，不再重複跑 EMA 迴圈。
        """
        result = {}
        for tf, period in [(tf1, period1), (tf2, period2)]:
            df = self._bar_store._load_full(self.contract_id, tf)
            if df.empty or len(df) < period + 5:
                result[f"{tf}_{period}"] = {}
                continue

            closes = df['c'].values
            times  = df.index.values.astype(np.int64)  # ns timestamps

            # 計算完整 EMA 序列
            k      = 2.0 / (period + 1)
            emas   = np.zeros(len(closes))
            emas[0] = closes[0]
            for i in range(1, len(closes)):
                emas[i] = closes[i] * k + emas[i-1] * (1 - k)

            # 建立 timestamp → direction 字典
            margin  = 0.001
            lookup  = {}
            for i in range(period, len(closes)):
                c, e = closes[i], emas[i]
                if c > e * (1 + margin):
                    d = "bull"
                elif c < e * (1 - margin):
                    d = "bear"
                else:
                    d = "neutral"
                lookup[times[i]] = d

            # 儲存排序好的 times 陣列 + directions 陣列，查表用 searchsorted
            sorted_times = np.sort(np.array(list(lookup.keys()), dtype=np.int64))
            sorted_dirs  = np.array([lookup[t] for t in sorted_times])
            result[f"{tf}_{period}"] = (sorted_times, sorted_dirs)
            print(f"    {tf} EMA{period}: {len(sorted_times):,} 個時間點")
        return result

    # ── 主流程 ────────────────────────────────────────────────────────────────

    async def run(self) -> BacktestResult:
        from strategies.snr_strategy_v3 import SNRStrategyV3

        # 讀取完整 15M 數據
        df_full = self._bar_store._load_full(self.contract_id, "5m")
        if df_full.empty:
            fname = self.contract_id.replace(".", "_") + "_5m.parquet"
            raise FileNotFoundError(
                f"找不到 5M 數據！\n"
                f"預期位置：{self._bar_store.data_dir / fname}\n"
                f"請確認 BarStore 已初始化並有儲存數據。"
            )

        df_full = df_full.sort_index()

        # ── 日期範圍過濾（保留 warmup 前綴讓 SR 正確初始化）──────────────
        if self.date_from is not None or self.date_to is not None:
            # 統一移除時區，避免 tz-aware vs tz-naive 比較失敗
            idx = df_full.index
            if hasattr(idx, 'tz') and idx.tz is not None:
                idx = idx.tz_localize(None)
                df_full.index = idx

            if self.date_from is not None:
                date_from = self.date_from.tz_localize(None) if self.date_from.tzinfo else self.date_from
                from_idx  = idx.searchsorted(date_from)
                from_idx  = max(0, from_idx - self.warmup_bars)
            else:
                from_idx = 0

            if self.date_to is not None:
                date_to = self.date_to.tz_localize(None) if self.date_to.tzinfo else self.date_to
                # date_to 是當天結束，加一天確保包含當天最後一根
                date_to_end = date_to + pd.Timedelta(days=1)
                to_idx = idx.searchsorted(date_to_end, side='left')
            else:
                to_idx = len(df_full)

            df_full = df_full.iloc[from_idx:to_idx]
            print(f"  日期過濾後 : {len(df_full)} 根 ({df_full.index[0].date()} ~ {df_full.index[-1].date()})")

        total   = len(df_full)
        start_t = df_full.index[self.warmup_bars]

        # ── 預提取所有欄位為 numpy，主迴圈用 FastDF 完全繞開 pandas ─────
        _cols = ['o', 'h', 'l', 'c', 'v']
        _np_arrays = {}
        for col in _cols:
            if col in df_full.columns:
                _np_arrays[col] = df_full[col].values.copy()
            elif col == 'o':
                _np_arrays[col] = df_full['c'].values.copy()
            else:
                _np_arrays[col] = np.ones(total)
        _np_index = df_full.index.values.copy()
        end_t   = df_full.index[-1]

        print("=" * 60)
        print("  SNR v3 回測引擎啟動")
        print("=" * 60)
        print(f"  合約     : {self.contract_id}")
        print(f"  5M 數據  : {total} 根")
        print(f"  回測期間 : {start_t} ~ {end_t}")
        print(f"  預熱根數 : {self.warmup_bars}（SR 識別用）")
        print(f"  初始資金 : ${self.initial_balance:,.0f}")
        if self.strategy_kwargs:
            print(f"  自訂參數 : {self.strategy_kwargs}")
        print("-" * 60)

        # 預載所有時框（一次性 I/O，後續全走記憶體）
        print("  預載歷史數據...")
        self._bar_store.preload_all(self.contract_id)
        print("-" * 60)

        # 建立策略
        engine_proxy = BacktestEngineProxy(self._bar_store)
        trading_svc  = BacktestTradingService(self)

        strategy = SNRStrategyV3(
            trading_service = trading_svc,
            market_service  = None,
            notifier        = SilentNotifier(),
            engine          = engine_proxy,
            contract_id     = self.contract_id,
            **self.strategy_kwargs
        )
        strategy.account_id = 1
        strategy._sr_update_interval = 288
        strategy.trading_service  = trading_svc
        strategy.market_service   = None
        strategy._backtest_mode   = True

        # 回測期間關掉 debug logging
        import logging as _logging
        _logging.getLogger().setLevel(_logging.WARNING)

        # ── 注入 bar_store 讓策略能存取（回測用）──────────────────────────
        strategy._backtest_bar_store = self._bar_store

        # ── 預算 HTF EMA，注入策略，讓 _htf_direction 直接查表 ──────────────
        print("  預算 HTF EMA...", end=" ", flush=True)
        strategy._htf_precomputed = self._precompute_htf_ema(
            strategy.htf_primary,   strategy.htf_primary_ema,
            strategy.htf_secondary, strategy.htf_secondary_ema,
        )
        print("完成")

        self._strategy = strategy

        # 逐根回測
        for i in range(self.warmup_bars, total):
            self._bar_idx   = i
            self._bar_time = pd.Timestamp(_np_index[i]).to_pydatetime()
            self._bar_time_ns = int(_np_index[i])   # ns timestamp for fast HTF lookup
            self._bar_store.current_bar_time    = self._bar_time
            self._bar_store.current_bar_time_ns = self._bar_time_ns
            self._bar_close = float(_np_arrays['c'][i])

            # ① bar_store 截止時間已在上方設定

            # ② 掃描這根 K 棒是否觸及 SL/TP（在策略看到這根 bar 之前先結算）
            self._scan_exits_np(float(_np_arrays['h'][i]), float(_np_arrays['l'][i]))

            # ③ 更新 DataHub 持倉狀態（讓策略的 _manage_trade 判斷持倉是否還在）
            self._set_datahub_position(self._active_trade is not None)

            # ④ 餵給策略（FastDF 直接做 numpy slice，完全繞開 pandas）
            _s = max(0, i - 600)
            _e = i + 1
            df_slice = FastDF(
                {k: v[_s:_e] for k, v in _np_arrays.items()},
                _np_index[_s:_e],
                _e - _s,
            )
            await strategy.on_bar(self.contract_id, df_slice)

            # ④.5 on_bar 結束後，策略已設好 active_trade，現在可以建立 BacktestTrade
            self._finalize_pending_order()

            # ⑤ 記錄 equity
            self._equity.append(self._balance)

            # 進度顯示（每 500 根顯示一次）
            if (i - self.warmup_bars) % 500 == 0 and i > self.warmup_bars:
                done = i - self.warmup_bars
                pct  = done / (total - self.warmup_bars) * 100
                print(f"  進度 {pct:>5.1f}% | bar {i}/{total} | "
                      f"交易數: {len(self._trades)} | 餘額: ${self._balance:,.0f}")

        # 回測結束：強制平倉（若還有持倉）
        if self._active_trade:
            self._settle(float(df_full['c'].iloc[-1]), "end_of_data")

        print(f"\n  回測完成 | 總交易: {len(self._trades)} 筆")

        # ── Skip 原因統計 ──────────────────────────────────────────────────
        if hasattr(strategy, 'skip_counts'):
            sc = strategy.skip_counts
            total_skips = sum(sc.values())
            active_bars = total - self.warmup_bars
            print(f"\n  {'─'*40}")
            print(f"  Skip 原因統計（共 {active_bars:,} 根有效 bar）")
            print(f"  {'─'*40}")
            labels = {
                "session":        "休市時段",
                "htf_neutral":    "HTF 趨勢中立",
                "no_sr":          "無 SR 區域",
                "no_fakebr":      "無假突破訊號",
                "vol_filter":     "成交量不足",
                "pin_bar":        "require_pin_bar",
                "sl_too_large":   "止損超出上限",
                "no_opposite_sr": "找不到對面 SR",
                "rr_too_low":     "RR 不足",
            }
            for key, label in labels.items():
                n = sc.get(key, 0)
                if n > 0:
                    pct = n / active_bars * 100
                    print(f"  {label:<18} : {n:>7,} 次  ({pct:>5.1f}%)")
            print(f"  {'─'*40}")
            print(f"  進場訊號通過    : {len(self._trades):>7,} 次")

        print("=" * 60)

        return BacktestResult(
            trades          = self._trades,
            equity_curve    = self._equity,
            initial_balance = self.initial_balance,
            params          = self.strategy_kwargs,
        )


# =============================================================================
# 參數掃描工具（Phase 3 用）
# =============================================================================

async def param_scan(param_name: str, values: list, base_kwargs: dict = None,
                     contract_id: str = "CON.F.US.MNQ.H26",
                     initial_balance: float = 50000.0) -> pd.DataFrame:
    """
    單一參數掃描。固定其他參數，掃描 param_name 的不同值。

    用法：
        results = asyncio.run(param_scan(
            param_name = "sr_sensitivity",
            values     = [0.8, 1.0, 1.5, 2.0, 2.5],
        ))
        print(results.to_string())
    """
    rows = []
    for v in values:
        kwargs = dict(base_kwargs or {})
        kwargs[param_name] = v
        runner = SNRBacktestRunner(
            contract_id     = contract_id,
            initial_balance = initial_balance,
            **kwargs
        )
        result = await runner.run()
        trades = result.trades
        n = len(trades)
        if n == 0:
            rows.append({param_name: v, "trades": 0, "win_rate": 0,
                         "pf": 0, "net_pnl": 0, "mdd": 0})
            continue

        pnls   = [t.pnl_usd for t in trades]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        pf     = abs(sum(wins) / sum(losses)) if losses else 999.0
        eq     = np.array(result.equity_curve)
        pk     = np.maximum.accumulate(eq)
        mdd    = float(((eq - pk) / pk * 100).min())

        rows.append({
            param_name:  v,
            "trades":    n,
            "win_rate":  round(len(wins) / n * 100, 1),
            "pf":        round(pf, 2),
            "net_pnl":   round(result.net_pnl, 0),
            "mdd_%":     round(mdd, 2),
        })

    df = pd.DataFrame(rows)
    print(f"\n── 參數掃描：{param_name} ──")
    print(df.to_string(index=False))
    return df


# =============================================================================
# 執行入口
# =============================================================================

async def main():
    # ── Phase 1：基線回測（預設參數）──────────────────────────────────────
    runner = SNRBacktestRunner(
        contract_id     = "CON.F.US.MNQ.H26",
        initial_balance = 50000.0,
        warmup_bars     = 600,

        # 回測放寬止損距離上限（歷史數據波動比現在大）
        # 預設 0.50 太嚴，會過濾掉大部分 2016-2022 年的訊號
        sl_max_atr_mult = 1.5,

        # 其他策略參數（不傳則用預設值）：
        # sr_sensitivity        = 1.5,
        # fake_break_depth_mult = 0.20,
        # vol_mult              = 1.5,
        # min_rr                = 1.5,
        # htf_swing_len         = 3,
        # require_pin_bar       = False,
    )

    result = await runner.run()
    result.print_summary()

    # ── Phase 3 範例：參數掃描（Phase 1 完成後解開）──────────────────────
    # await param_scan(
    #     param_name = "sr_sensitivity",
    #     values     = [0.8, 1.0, 1.5, 2.0, 2.5, 3.0],
    # )
    # await param_scan(
    #     param_name = "fake_break_depth_mult",
    #     values     = [0.10, 0.15, 0.20, 0.25, 0.30],
    # )


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
