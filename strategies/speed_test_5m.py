"""測試 5M 回測速度"""
import asyncio, time, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from backtest.engine import SNRBacktestRunner

async def main():
    configs = [
        dict(fake_break_depth_mult=0.30, htf_primary_ema=100, htf_secondary_ema=21,
             min_rr=1.5, vol_mult=0.0, sl_max_atr_mult=1.5),
        dict(fake_break_depth_mult=0.50, htf_primary_ema=50,  htf_secondary_ema=9,
             min_rr=1.2, vol_mult=1.0, sl_max_atr_mult=1.5),
        dict(fake_break_depth_mult=0.20, htf_primary_ema=200, htf_secondary_ema=50,
             min_rr=1.8, vol_mult=1.5, sl_max_atr_mult=1.5),
    ]
    times = []
    for i, cfg in enumerate(configs):
        t0 = time.time()
        r  = await SNRBacktestRunner(**cfg).run()
        elapsed = time.time() - t0
        times.append(elapsed)
        print(f"  組{i+1}: {elapsed:.1f}秒  交易數={len(r.trades)}  淨利=${r.net_pnl:.0f}")

    avg = sum(times) / len(times)
    print(f"\n  平均: {avg:.1f}秒/組")
    print(f"  972組預估: {972 * avg / 3600:.1f} 小時")

asyncio.run(main())
