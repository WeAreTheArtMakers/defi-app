"""Kraken CAD dislocation, 20-21 Nov 2026: every figure of the case study, recomputed from Kraken's public data in data/ (retrieved by fetch_trades.py
on 25 Nov 2026, AFTER the event; deduplicated by trade_id: the Trades `since` cursor repeats the last trade of each page). WATAM did not collect
these markets live. Output: evidence.json + printed summary. Run: python3 evidence.py
Inputs in data/: <pair>.jsonl(.gz) from fetch_trades.py (see its docstring), and ohlc15_<pair>.json from
https://api.kraken.com/0/public/OHLC?pair=<pair>&interval=15 for XXBTZCAD and XBTUSDT. That endpoint returns only the latest 720 candles (7.5 days):
after 28 Nov 2026 use Kraken's downloadable OHLCVT archive, or download trades to 21 Nov 18:00 UTC instead."""
import gzip, json, statistics as st, datetime as dt, collections
U = lambda t: dt.datetime.fromtimestamp(t, dt.UTC)
TSI = lambda t: (U(t) + dt.timedelta(hours=3)).strftime("%d %b %H:%M:%S")
BOC_20NOV = 1.4325                      # Bank of Canada FXUSDCAD, 20 Nov 2026 (Valet); 21-22 Nov is a weekend
R0, R1 = dt.datetime(2026, 11, 20, 18, tzinfo=dt.UTC).timestamp(), dt.datetime(2026, 11, 20, 19, tzinfo=dt.UTC).timestamp()

def _open(name):
    import os
    return gzip.open(f"data/{name}.jsonl.gz", "rt") if os.path.exists(f"data/{name}.jsonl.gz") else open(f"data/{name}.jsonl")


def load(name):
    out, seen = [], set()
    for l in _open(name):
        p, v, t, s, ty, m, i = json.loads(l)
        if i not in seen:
            seen.add(i); out.append((float(p), float(v), float(t), s, ty, int(i)))
    return sorted(out, key=lambda x: (x[2], x[5]))

PAIRS = {"XXBTZCAD": "BTC/CAD", "ZUSDZCAD": "USD/CAD", "XETHZCAD": "ETH/CAD", "SOLCAD": "SOL/CAD", "XXRPZCAD": "XRP/CAD", "USDTCAD": "USDT/CAD",
         "USDCCAD": "USDC/CAD", "EURCAD": "EUR/CAD", "XDGCAD": "DOGE/CAD", "PEPECAD": "PEPE/CAD", "XDCCAD": "XDC/CAD",
         "XBTUSDT": "BTC/USDT", "XETHZUSD": "ETH/USD", "USDTZUSD": "USDT/USD", "USDCUSD": "USDC/USD"}
ev = {"window_utc": ["2026-11-20T18:00:00Z", "2026-11-21T12:00:00Z"], "boc_usdcad_20nov": BOC_20NOV, "pairs": {}}
data = {k: load(k) for k in PAIRS}
for k, T in data.items():
    pre = [x[0] for x in T if R0 <= x[2] < R1]
    basis = "median trade 20 Nov 18:00-19:00 UTC" if pre else "median of the first 20 trades of the window (no trade 18:00-19:00 UTC)"
    ref = st.median(pre or [x[0] for x in T[:20]])
    lo = min(T, key=lambda x: (x[0], x[2]))
    below = [x for x in T if x[0] < ref * 0.9]
    ev["pairs"][PAIRS[k]] = {"kraken_pair": k, "trades": len(T), "volume_base": round(sum(x[1] for x in T), 6), "ref_median_20nov_18_19utc": ref, "ref_basis": basis,
        "low": lo[0], "low_utc": U(lo[2]).isoformat(), "low_tsi": TSI(lo[2]), "low_trade_id": lo[5], "low_trade_volume": lo[1],
        "trades_at_low_price": sum(1 for x in T if x[0] == lo[0]), "volume_at_low_price": round(sum(x[1] for x in T if x[0] == lo[0]), 6),
        "max_deviation_pct": round((lo[0] / ref - 1) * 100, 2), "trades_below_90pct_ref": len(below),
        "volume_below_90pct_ref": round(sum(x[1] for x in below), 6),
        "first_below_90pct_utc": U(below[0][2]).isoformat() if below else None, "last_below_90pct_utc": U(below[-1][2]).isoformat() if below else None}

# BTC/USD at the BTC/CAD low (separate download 07:50-08:20 UTC)
btc = data["XXBTZCAD"]; low = min(btc, key=lambda x: (x[0], x[2]))
usd = load("XXBTZUSD_0750_0820")
prev = [x for x in usd if x[2] <= low[2]][-1]
near = [x[0] for x in usd if abs(x[2] - low[2]) <= 300]
ev["low"] = {"btc_cad": low[0], "utc": U(low[2]).isoformat(), "tsi": TSI(low[2]), "trade_id": low[5], "usd_equivalent_at_boc": round(low[0] / BOC_20NOV, 2),
             "btc_usd_last_trade_before": prev[0], "btc_usd_last_trade_before_utc": U(prev[2]).isoformat(), "btc_usd_trade_id": prev[5],
             "btc_usd_range_pm5min": [min(near), max(near)], "gap_vs_btc_usd_pct": round((low[0] / BOC_20NOV / prev[0] - 1) * 100, 2),
             "implied_cad_per_usd": round(low[0] / prev[0], 4)}
w0, w1 = dt.datetime(2026, 11, 21, 8, tzinfo=dt.UTC).timestamp(), dt.datetime(2026, 11, 21, 8, 15, tzinfo=dt.UTC).timestamp()
sub = [x for x in btc if w0 <= x[2] < w1 and x[0] < 20000 * BOC_20NOV]
ev["low"]["trades_below_usd20k_equiv_0800_0815"] = len(sub); ev["low"]["volume_below_usd20k_equiv_0800_0815"] = round(sum(x[1] for x in sub), 4)

# implied CAD per USD, 15-min VWAP BTC/CAD / BTC/USDT (BTC/USDT used as the USD leg: complete for the window; USDT/USD stayed 0.9975-0.999)
def vwap15(T):
    b = collections.defaultdict(lambda: [0.0, 0.0, 0])
    for p, v, t, *_ in T:
        r = b[int(t // 900 * 900)]; r[0] += p * v; r[1] += v; r[2] += 1
    return {k: (r[0] / r[1], r[1], r[2]) for k, r in b.items() if r[1] > 0}
cad, ut, uc = vwap15(btc), vwap15(data["XBTUSDT"]), vwap15(data["ZUSDZCAD"])
ev["implied_15m"] = [{"utc": U(k).isoformat(), "btc_cad_vwap": round(cad[k][0], 2), "btc_usdt_vwap": round(ut[k][0], 2), "implied_cad_per_usd": round(cad[k][0] / ut[k][0], 4),
                      "vs_boc_pct": round((cad[k][0] / ut[k][0] / BOC_20NOV - 1) * 100, 2), "kraken_usdcad_vwap": round(uc[k][0], 4) if k in uc else None,
                      "btc_cad_trades": cad[k][2], "btc_cad_volume": round(cad[k][1], 4)} for k in sorted(set(cad) & set(ut))]
first = next(x for x in ev["implied_15m"] if abs(x["vs_boc_pct"]) > 2)
ev["dislocation_start_15m_utc"] = first["utc"]

# durations, from the 15-min implied series (executed-trade VWAPs; not order-book quotes)
q = [(dt.datetime.fromisoformat(x["utc"]), x["vs_boc_pct"]) for x in ev["implied_15m"]]
f5 = next(t for t, v in q if abs(v) >= 5); last5 = max(t for t, v in q if abs(v) >= 5)
runs, cur = [], None
for t, v in q:
    if abs(v) >= 5:
        cur = cur or [t, t]; cur[1] = t
    elif cur:
        runs.append(cur); cur = None
if cur: runs.append(cur)
Q = dt.timedelta(minutes=15)
ev["durations"] = {"definition": "15-min implied CAD per USD = BTC/CAD VWAP / BTC/USDT VWAP vs the Bank of Canada 20 Nov rate 1.4325",
    "first_deviation_over_2pct_quarter_utc": ev["dislocation_start_15m_utc"], "first_quarter_at_or_over_5pct_utc": f5.isoformat(),
    "episode_at_or_over_5pct_utc": [f5.isoformat(), (last5 + Q).isoformat()], "episode_hours": round((last5 + Q - f5).total_seconds() / 3600, 2),
    "quarters_in_episode": sum(1 for t, v in q if f5 <= t <= last5), "quarters_at_or_over_5pct": sum(1 for t, v in q if f5 <= t <= last5 and abs(v) >= 5),
    "continuous_runs_at_or_over_5pct_utc": [[a.isoformat(), (b + Q).isoformat()] for a, b in runs],
    "worst_quarter": {"utc": min(q, key=lambda x: x[1])[0].isoformat(), "vs_boc_pct": min(q, key=lambda x: x[1])[1]},
    "first_quarter_back_under_5pct_after_low_utc": next(t for t, v in q if t > U(low[2]) and abs(v) < 5).isoformat()}

# recovery from Kraken 15-min OHLC (VWAP column): implied rate within 2 % of the BoC rate for 2 hours in a row after the low
def ohlc(name):
    d = json.load(open(f"data/ohlc15_{name}.json"))
    return {int(r[0]): float(r[5]) for r in next(v for k, v in d["result"].items() if k != "last") if float(r[5]) > 0}
oc, ou = ohlc("XXBTZCAD"), ohlc("XBTUSDT")
seq = [(k, oc[k] / ou[k]) for k in sorted(set(oc) & set(ou))]
for i, (k, v) in enumerate(seq):
    if k > low[2] and all(abs(x / BOC_20NOV - 1) <= 0.02 for _, x in seq[i:i + 8]):
        ev["sustained_recovery_utc"] = U(k).isoformat(); break
ev["durations"]["sustained_normalisation_utc"] = ev["sustained_recovery_utc"]
ev["durations"]["first_deviation_to_sustained_normalisation_hours"] = round((dt.datetime.fromisoformat(ev["sustained_recovery_utc"]) - dt.datetime.fromisoformat(ev["dislocation_start_15m_utc"])).total_seconds() / 3600, 2)

# execution clusters on BTC/CAD: market-sell trades within 5 ms of each other, >= 5 BTC in total. Kraken's public trades carry NO order id, so a
# cluster is an INFERRED group of executions (most likely one taker order, but not established).
groups, cur = [], None
for p, v, t, s, ty, i in btc:
    if cur and s == cur["s"] and ty == cur["ty"] and t - cur["t1"] <= 0.005:
        cur.update(v=cur["v"] + v, n=cur["n"] + 1, t1=t, lo=min(cur["lo"], p), hi=max(cur["hi"], p), last=i)
    else:
        cur = {"t0": t, "t1": t, "s": s, "ty": ty, "v": v, "n": 1, "lo": p, "hi": p, "first": i, "last": i}; groups.append(cur)
big = [g for g in groups if g["s"] == "s" and g["ty"] == "m" and g["v"] >= 5]
ev["execution_clusters_note"] = "inferred from timestamps; Kraken public trades have no order id"
ev["large_market_sells"] = [{"utc": U(g["t0"]).isoformat(timespec="milliseconds"), "btc": round(g["v"], 4), "fills": g["n"], "from_cad": g["hi"], "to_cad": g["lo"],
                             "trade_ids": [g["first"], g["last"]]} for g in big]
ev["btc_cad_window"] = {"trades": len(btc), "volume_btc": round(sum(x[1] for x in btc), 4),
                        "market_sell_volume_btc": round(sum(x[1] for x in btc if x[3] == "s" and x[4] == "m"), 4),
                        "large_market_sells_btc": round(sum(g["v"] for g in big), 4), "large_market_sells": len(big)}
TT = lambda x: dt.datetime.fromisoformat(x["utc"])
hourly = [x for x in ev["large_market_sells"] if TT(x).minute == 4 and TT(x).second in (34, 35)]
quarter = [x for x in ev["large_market_sells"] if TT(x).second == 43]
ev["pattern"] = {"note": "execution clusters inferred from timestamps (market-sell trades within 5 ms); Kraken public trades carry no order id",
    "hourly_orders": len(hourly), "hourly_btc": round(sum(x["btc"] for x in hourly), 4), "hourly_full_size_20_8333": sum(1 for x in hourly if abs(x["btc"] - 20.8333) < 1e-3),
    "quarter_orders": len(quarter), "quarter_btc": round(sum(x["btc"] for x in quarter), 4), "quarter_full_size_10_4167": sum(1 for x in quarter if abs(x["btc"] - 10.4167) < 1e-3),
    "other_orders": [{"utc": x["utc"], "btc": x["btc"]} for x in ev["large_market_sells"] if x not in hourly and x not in quarter]}
json.dump(ev, open("evidence.json", "w"), indent=1)
print(json.dumps({k: v for k, v in ev.items() if k not in ("implied_15m", "pairs", "large_market_sells")}, indent=1))
for name, r in ev["pairs"].items():
    print(f"{name:9} trades {r['trades']:>6} ref {r['ref_median_20nov_18_19utc']:<11g} low {r['low']:<11g} {r['low_utc'][:19]} UTC ({r['low_tsi']} TSİ) "
          f"{r['max_deviation_pct']:>7}%  below-90%: {r['trades_below_90pct_ref']} trades {r['first_below_90pct_utc'] and r['first_below_90pct_utc'][5:19]} .. {r['last_below_90pct_utc'] and r['last_below_90pct_utc'][5:19]}")
for g in ev["large_market_sells"]: print("  sell", g["utc"][5:23], g["btc"], "BTC", g["fills"], "fills", g["from_cad"], "->", g["to_cad"])
