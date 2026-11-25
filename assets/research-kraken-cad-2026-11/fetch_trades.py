"""Kraken public Trades, paginated with the documented `since` cursor (ns). Rate-limited (~1 req / 1.2 s). Writes <pair>.jsonl:
[price, volume, time, side b/s, type m/l, misc, trade_id]."""
import json, sys, time, urllib.request, datetime as dt
START = int(dt.datetime(2026, 11, 20, 18, tzinfo=dt.timezone.utc).timestamp())
END = int(dt.datetime(2026, 11, 21, 12, tzinfo=dt.timezone.utc).timestamp())
def get(pair, since):
    for a in range(6):
        try:
            with urllib.request.urlopen(f"https://api.kraken.com/0/public/Trades?pair={pair}&since={since}&count=1000", timeout=30) as r:
                d = json.load(r)
            if d["error"]:
                if any("Too many" in e or "Rate" in e for e in d["error"]): time.sleep(5 * (a + 1)); continue
                raise RuntimeError(d["error"])
            return d["result"]
        except Exception as e:
            if a == 5: raise
            time.sleep(3 * (a + 1))
START = int(float(__import__("os").environ.get("START_TS", START)))
END = int(float(__import__("os").environ.get("END_TS", END)))
OUT = __import__("os").environ.get("OUT_SUFFIX", "")
for pair in sys.argv[1:]:
    since, n, req, seen = int(START * 10**9), 0, 0, set()
    with open(f"{pair}{OUT}.jsonl", "w") as f:
        while True:
            res = get(pair, since); req += 1
            last = int(res.pop("last")); rows = next(iter(res.values()))
            stop = False
            for t in rows:
                if t[2] >= END: stop = True; break
                if t[2] >= START and t[6] not in seen: seen.add(t[6]); f.write(json.dumps(t) + "\n"); n += 1   # the since cursor repeats the page's last trade
            if stop or not rows or last == since: break
            since = last; time.sleep(1.2)
    print(pair, "trades", n, "requests", req, flush=True)
