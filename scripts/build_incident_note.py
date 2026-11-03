#!/usr/bin/env python3
"""Build a neutral, evidence-based incident data note: incidents/<id>/index.html + incidents/<id>/data.json.

Standard library only. It runs where the DeFi Intelligence backend and its ClickHouse are reachable (the operator's machine);
the GitHub Action does NOT run it. For every tracked Ethereum token (plus tokens named in the addresses file) it reports:

  * coverage of the incident window: hours fully covered by successful ingestion runs against hours expected
    (an hour not collected yet is "not covered yet", never zero);
  * verified anomaly findings whose window overlaps the incident window, with Evidence Card links and block ranges;
  * swaps per hour and token volume per hour in the window against the median of the covered hours of the 24 h
    (and the N days) before the window;
  * if reported addresses are given: swaps in our tracked pools whose transaction was SENT by such an address (origin read
    with eth_getTransactionByHash from a public node, batched, capped and cached); a router / solver in between = "not
    attributed". Near-real-time rows of defi_chain.live_swaps for hours verified data does not cover yet are shown apart
    as "live, unverified".

  python3 scripts/build_incident_note.py \\
      --incident binance-2026-10-29 --window-start 2026-10-29T20:00Z --window-end 2026-10-30T00:00Z \\
      --baseline-days 7 --addresses ../plans/incident-binance-addresses.json [--index]
  python3 scripts/build_incident_note.py --incident binance-2026-10-29 --render-only     # re-render from data.json

Addresses file (optional): {"initiator": [...], "receiver": [...], "tokens": [...], "sources": [...]} where a list item is
"0x…" or {"address": "0x…", "label": "…", "source": "…", "source_url": "https://…"}; tokens: {"symbol": "…", "address": "0x…"}.

The operator token (APP_ACCESS_TOKEN) is read from --env-file and only sent to the local backend; it is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the site repo
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_token_pages import APP, EMAIL, METHOD_URL, ORG, PRICING_URL, SITE, document, esc  # noqa: E402

CHAIN_ID = 1
NETWORK = "ethereum"
NETWORK_NAME = "Ethereum"
EXPLORER = "https://etherscan.io"
EVM_RE = re.compile(r"0x[0-9a-fA-F]{40}")
HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,63}")
URL_RE = re.compile(r"https://[^\s\"'<>]{4,300}")
DEFAULT_ENV = "/Users/bg/Desktop/1inch/1inch-defi-intelligence/.env"
DEFAULT_ADDRESSES = "/Users/bg/Desktop/1inch/commercial-audit/plans/incident-binance-addresses.json"
SHOW_TX = 10
SWAP_TOPICS = [
    ("Uniswap v3-like Swap", "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67", "topic1 = sender, topic2 = recipient"),
    ("Uniswap v2-like Swap", "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822", "topic1 = sender, topic2 = to"),
    ("Uniswap v4 PoolManager Swap", "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f", "topic1 = pool id, topic2 = sender"),
]

# Facts about the incident itself come only from public reporting and are marked "reported". Figures differ between outlets.
INCIDENTS = {
    "binance-2026-10-29": {
        "title": "Reported Binance hot-wallet outflow, 29 October 2026: DEX data note",
        "short": "reported Binance hot-wallet outflow of 29 October 2026",
        "reported_at": "2026-10-29T20:24:00Z",
        # text + optional on-chain links (label, url); third-party claims stay "reported"
        "facts": [
            ("At about 20:24 UTC on 29 October 2026 a hot wallet labelled “Binance 14” was reported to have sent about 167.5k ETH, "
             "about 1.6 billion USDT and more than 60 other tokens (reported examples: RLUSD, wBETH, XAUt, UNI, USD1) to a newly "
             "created address in one transaction.",
             [("main transaction, block 26,300,770", f"{EXPLORER}/tx/0x53bd38b816c23e74134786c29a8c18d792777998a3b9326047b583100e88e2f3")]),
            ("A later transfer of about 780 million USDC was reported. Reported totals differ between outlets, from about "
             "$2.5 billion upward.", []),
            ("Reports say the address that initiated the transaction had been funded through Tornado Cash; this comes from a "
             "single researcher's alert and was reported as not independently confirmed.", []),
            ("Tether was reported to have frozen about 971 million USDT and Circle about 823 million USDC; other reports give "
             "different totals.", []),
            ("Binance was reported to be investigating; statements quoted in the reports said user funds were safe. At the time of "
             "these reports Binance had not formally confirmed the cause.", []),
            ("On-chain, the reported receiving address sent 7,018,792 UNI to address 0x42cd…7810 in block 26,301,181. Public "
             "on-chain research reported that this address then sold about 700k UNI into Uniswap pools between blocks "
             "26,301,203 and 26,301,244; our own observation of tracked pools is in the table below.",
             [("UNI transfer", f"{EXPLORER}/tx/0x028bdd8b5f0bda6503a760b734090f1cdbc213e90b71394b8a089b3d66eada74"),
              ("address 0x42cd…7810", f"{EXPLORER}/address/0x42cdf00c85005630cc8605731b9364f712077810")]),
        ],
        # labels for addresses whose role in the addresses file is broader than "initiator" / "receiver"
        "address_labels": {
            "0x42cdf00c85005630cc8605731b9364f712077810": "received 7,018,792 UNI from the reported receiving address (on-chain)",
        },
        "sources": [
            {"name": "PANews", "url": "https://panews.io/articles/01a1896e-ed91-7224-8d9d-9d2d87e87522"},
            {"name": "BeInCrypto", "url": "https://beincrypto.com/is-binance-being-hacked-customer-funds/"},
            {"name": "Cointelegraph", "url": "https://cointelegraph.com/news/binance-linked-wallet-faces-possible-exploit-blockchain-researcher-warns"},
            {"name": "Crypto Briefing (stablecoin freezes)", "url": "https://cryptobriefing.com/tether-freezes-971m-usdt-binance-hack/"},
            {"name": "Crypto Briefing (funding report)", "url": "https://cryptobriefing.com/binance-outflows-tornado-cash-funded-wallets/"},
            {"name": "Crypto Briefing (statements)", "url": "https://cryptobriefing.com/binance-suspected-hack-user-funds-safe/"},
            {"name": "Blockchain Reporter", "url": "https://blockchainreporter.net/binance-hot-wallet-hack-2-5-billion/"},
        ],
    },
}
ROLE_LABEL = {"initiator": "reported initiating address", "receiver": "reported receiving address"}


# ---------------------------------------------------------------- helpers

def utc(value: str) -> datetime:
    v = value.strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\+00:00", v):
        v = v.replace("+00:00", ":00+00:00")
    dt = datetime.fromisoformat(v)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def human(dt: datetime | str | None) -> str:
    if isinstance(dt, str):
        dt = utc(dt)
    return dt.strftime("%d %b %Y, %H:%M UTC") if dt else "n/a"


def hours(start: datetime, end: datetime) -> list[datetime]:
    out, h = [], start
    while h < end:
        out.append(h)
        h += timedelta(hours=1)
    return out


def ch_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def ch_array(values: list[str]) -> str:
    assert all(EVM_RE.fullmatch(v) for v in values)
    return "[" + ",".join(f"'{v}'" for v in values) + "]"


def short(addr: str) -> str:
    return f"{addr[:8]}…{addr[-6:]}" if len(addr) > 16 else addr


def fnum(x, digits: int = 1) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, int):
        return f"{x:,}"
    return f"{x:,.{digits}f}"


def read_token(env_file: str) -> str | None:
    if os.environ.get("APP_ACCESS_TOKEN"):
        return os.environ["APP_ACCESS_TOKEN"]
    try:
        for line in Path(env_file).read_text().splitlines():
            if line.startswith("APP_ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


class Backend:
    def __init__(self, base: str, token: str | None):
        self.base = base.rstrip("/")
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local backend: never via a system proxy

    def get(self, path: str, **params) -> dict:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Accept": "application/json", "User-Agent": "watam-incident-note/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(f"{self.base}{path}{'?' + q if q else ''}", headers=headers)
        for attempt in range(6):
            try:
                with self.opener.open(req, timeout=120) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:   # the backend rate-limits per minute: wait and retry
                if exc.code != 429 or attempt == 5:
                    raise
                wait = exc.headers.get("Retry-After")
                time.sleep(min(int(wait) if wait and wait.isdigit() else 10 * (attempt + 1), 60))
        raise RuntimeError("unreachable")


def clickhouse(cmd: list[str], sql: str, params: dict[str, str]) -> list[dict]:
    args = cmd + [f"--param_{k}={v}" for k, v in params.items()] + ["-q", sql + " FORMAT JSONEachRow"]
    res = subprocess.run(args, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if res.returncode:
        raise RuntimeError(f"clickhouse query failed ({res.returncode}): {res.stderr.strip()[:500]}")
    out = res.stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]



class TxOrigin:
    """Transaction hash -> transaction origin (`from`) via eth_getTransactionByHash on a public node: batched, capped, cached.

    A transaction's `from` never changes, so resolved origins are cached on disk between runs (outside the site repo)."""

    def __init__(self, rpc: str, cache_file: str, cap: int, batch: int = 50):
        self.rpc, self.cap, self.batch = rpc, cap, batch
        self.cache_path = Path(cache_file).expanduser()
        try:
            self.cache: dict[str, str] = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            self.cache = {}
        self.fetched = 0
        self.failed = 0

    def _call(self, hashes: list[str]) -> dict[str, str]:
        body = json.dumps([{"jsonrpc": "2.0", "id": i, "method": "eth_getTransactionByHash", "params": [h]}
                           for i, h in enumerate(hashes)]).encode()
        req = urllib.request.Request(self.rpc, data=body, headers={"Content-Type": "application/json",
                                                                   "User-Agent": "watam-incident-note/1.0"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed https RPC URL)
                    out = json.load(resp)
                break
            except (urllib.error.URLError, TimeoutError, ValueError):
                if attempt == 3:
                    return {}
                time.sleep(2 * (attempt + 1))
        res = {}
        for item in out if isinstance(out, list) else []:
            tx = item.get("result") or {}
            frm, h = str(tx.get("from") or ""), str(tx.get("hash") or "").lower()
            if EVM_RE.fullmatch(frm) and h in hashes:
                res[h] = frm.lower()
        return res

    def resolve(self, hashes: list[str]) -> dict[str, str]:
        todo = [h for h in dict.fromkeys(hashes) if h not in self.cache][: max(self.cap, 0)]
        for i in range(0, len(todo), self.batch):
            chunk = todo[i:i + self.batch]
            got = self._call(chunk)
            self.cache.update(got)
            self.fetched += len(got)
            self.failed += len(chunk) - len(got)
            time.sleep(0.2)
        if todo:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache))
        return {h: self.cache[h] for h in hashes if h in self.cache}


# ---------------------------------------------------------------- inputs

def load_addresses(path: str | None) -> dict:
    res = {"file": None, "addresses": [], "tokens": [], "sources": [], "rejected": 0}
    if not path or not Path(path).is_file():
        return res
    raw = json.loads(Path(path).read_text())
    res["file"] = Path(path).name
    seen = set()
    for role in ("initiator", "receiver"):
        for item in raw.get(role) or []:
            item = {"address": item} if isinstance(item, str) else dict(item or {})
            addr = str(item.get("address") or "").strip()
            if not EVM_RE.fullmatch(addr):
                res["rejected"] += 1
                continue
            if addr.lower() in seen:
                continue
            seen.add(addr.lower())
            url = str(item.get("source_url") or "")
            res["addresses"].append({
                "address": addr.lower(), "role": role, "label": str(item.get("label") or ROLE_LABEL[role])[:120],
                "source": str(item.get("source") or "")[:120] or None, "source_url": url if URL_RE.fullmatch(url) else None,
            })
    for t in raw.get("tokens") or []:
        t = {"address": t} if isinstance(t, str) else dict(t or {})
        if EVM_RE.fullmatch(str(t.get("address") or "")):
            res["tokens"].append({"address": t["address"].lower(), "symbol": re.sub(r"[^\w.+-]", "", str(t.get("symbol") or ""))[:16] or None})
    for s in raw.get("sources") or []:
        s = {"name": s} if isinstance(s, str) else dict(s or {})
        url = str(s.get("url") or "")
        res["sources"].append({"name": str(s.get("name") or "")[:80], "url": url if URL_RE.fullmatch(url) else None})
    return res


# ---------------------------------------------------------------- collection

def covered_hours(cov: dict, start: datetime, end: datetime) -> set[datetime]:
    intervals = [(utc(a), utc(b)) for a, b in cov.get("intervals") or []]
    return {h for h in hours(start, end) if any(a <= h and h + timedelta(hours=1) <= b for a, b in intervals)}


def median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def collect(args, incident: dict) -> dict:
    now = datetime.now(timezone.utc)
    ws, we = utc(args.window_start), utc(args.window_end)
    b24, b7 = ws - timedelta(hours=24), ws - timedelta(days=args.baseline_days)
    be = Backend(args.backend, read_token(args.env_file))
    ch_cmd = args.clickhouse_cmd.split()
    addrs = load_addresses(args.addresses)
    for a in addrs["addresses"]:
        if a["label"] == ROLE_LABEL[a["role"]] and a["address"] in incident.get("address_labels", {}):
            a["label"] = incident["address_labels"][a["address"]]
    catalog = be.get("/api/public/catalog")

    tokens: list[dict] = []
    for e in catalog.get("entries") or []:
        if e.get("chain_id") == CHAIN_ID and e.get("network") == NETWORK and EVM_RE.fullmatch(str(e.get("address") or "")):
            tokens.append({"address": e["address"].lower(), "symbol": e.get("symbol"), "name": e.get("name"), "tracked": True,
                           "catalog_status": e.get("status"), "catalog_covered_until": e.get("covered_until")})
    known = {t["address"] for t in tokens}
    for t in addrs["tokens"]:
        if t["address"] not in known:
            tokens.append({"address": t["address"], "symbol": t["symbol"], "name": None, "tracked": False,
                           "catalog_status": None, "catalog_covered_until": None})
            known.add(t["address"])

    tracked = [t["address"] for t in tokens if t["tracked"]]
    metrics: dict[tuple[str, datetime], dict] = {}
    pools: dict[str, list[str]] = {}
    if tracked:
        for r in clickhouse(ch_cmd,
                            "SELECT token, toString(hour) AS hour_s, pool_swaps, transactions, token_volume "
                            "FROM defi_chain.token_metrics_hourly FINAL WHERE chain_id = {chain:UInt32} AND covered = 1 "
                            "AND has({tokens:Array(String)}, token) AND hour >= {f:DateTime('UTC')} AND hour < {t:DateTime('UTC')}",
                            {"chain": str(CHAIN_ID), "tokens": ch_array(tracked), "f": ch_ts(b7), "t": ch_ts(we)}):
            metrics[(r["token"], utc(r["hour_s"].replace(" ", "T")))] = r
        for r in clickhouse(ch_cmd,
                            "SELECT tok, groupUniqArray(pool) AS pools FROM (SELECT arrayJoin([token0, token1]) AS tok, pool "
                            "FROM defi_chain.decoded_swaps WHERE chain_id = {chain:UInt32} AND block_time >= {f:DateTime('UTC')} "
                            "AND block_time < {t:DateTime('UTC')}) WHERE has({tokens:Array(String)}, tok) GROUP BY tok",
                            {"chain": str(CHAIN_ID), "tokens": ch_array(tracked), "f": ch_ts(b7), "t": ch_ts(we)}):
            pools[r["tok"]] = sorted(r["pools"])

    window_blocks = None
    rows = clickhouse(ch_cmd, "SELECT min(block_number) AS lo, max(block_number) AS hi, count() AS n FROM defi_chain.decoded_swaps "
                              "WHERE chain_id = {chain:UInt32} AND block_time >= {f:DateTime('UTC')} AND block_time < {t:DateTime('UTC')}",
                      {"chain": str(CHAIN_ID), "f": ch_ts(ws), "t": ch_ts(we)})
    if rows and int(rows[0]["n"]):
        window_blocks = {"from": int(rows[0]["lo"]), "to": int(rows[0]["hi"]),
                         "source": "first and last block of decoded swaps in tracked pools inside the window"}

    window_hours = hours(ws, we)
    elapsed = [h for h in window_hours if h + timedelta(hours=1) <= now]
    cov_sets: dict[str, set[datetime]] = {}
    cov_errors: dict[str, str] = {}
    for a in tracked:
        try:
            cov = be.get(f"/api/platform/explorer/{CHAIN_ID}/{a}/coverage", from_time=iso(b7), to_time=iso(we))
            cov_sets[a] = covered_hours(cov, b7, we)
        except Exception as exc:  # noqa: BLE001 — reported as missing coverage, never guessed
            cov_sets[a], cov_errors[a] = set(), type(exc).__name__

    # Reported addresses: a swap is attributed only when the transaction origin (`from`) is a reported address. Swap
    # sender / recipient are usually a router, aggregator or solver, so a field match alone is "not attributed".
    verified_rows: list[dict] = []
    live_rows: list[dict] = []
    origins: dict[str, str] = {}
    origin_stats = None
    if addrs["addresses"] and tracked:
        p = {"chain": str(CHAIN_ID), "tokens": ch_array(tracked), "f": ch_ts(ws), "t": ch_ts(we)}
        verified_rows = clickhouse(ch_cmd,
                                   "SELECT pool, family, block_number, toString(block_time) AS bt, tx_hash, lower(sender) AS sender, "
                                   "lower(recipient) AS recipient, token0, token1, toString(amount0_raw) AS a0, toString(amount1_raw) AS a1 "
                                   "FROM defi_chain.decoded_swaps FINAL WHERE chain_id = {chain:UInt32} "
                                   "AND block_time >= {f:DateTime('UTC')} AND block_time < {t:DateTime('UTC')} "
                                   "AND (has({tokens:Array(String)}, token0) OR has({tokens:Array(String)}, token1)) "
                                   "ORDER BY block_number, tx_hash, log_index", p)
        live_rows = clickhouse(ch_cmd,
                               "SELECT token, pool, family, block_number, toString(block_time) AS bt, tx_hash, side, token_amount, "
                               "lower(sender) AS sender, lower(recipient) AS recipient FROM defi_chain.live_swaps FINAL "
                               "WHERE chain_id = {chain:UInt32} AND block_time >= {f:DateTime('UTC')} AND block_time < {t:DateTime('UTC')} "
                               "AND has({tokens:Array(String)}, token) ORDER BY block_number, tx_hash, log_index", p)
        for r in verified_rows + live_rows:
            r["block_number"] = int(r["block_number"])
            r["block_time"] = r.pop("bt").replace(" ", "T") + "Z"
            r["hour"] = utc(r["block_time"]).replace(minute=0, second=0)
            r["tx_hash"] = str(r["tx_hash"]).lower()
        # verified swaps count only inside covered hours; live rows only where verified data does not cover the hour yet
        verified_rows = [r for r in verified_rows if any(r["hour"] in cov_sets.get(tk, set()) for tk in (r["token0"], r["token1"]))]
        live_rows = [r for r in live_rows if r["hour"] not in cov_sets.get(r["token"], set())]
        resolver = TxOrigin(args.rpc, args.tx_cache, args.max_tx_lookups)
        wanted = [r["tx_hash"] for r in verified_rows + live_rows if HASH_RE.fullmatch(r["tx_hash"])]
        origins = resolver.resolve(wanted)
        origin_stats = {"rpc": args.rpc, "transactions": len(set(wanted)), "resolved": len(set(wanted) & set(origins)),
                        "fetched_this_run": resolver.fetched, "failed_this_run": resolver.failed, "cap": args.max_tx_lookups}
    reported = {a["address"]: a for a in addrs["addresses"]}

    out_tokens = []
    for t in tokens:
        a = t["address"]
        row = dict(t, pools=pools.get(a, []), coverage_error=cov_errors.get(a))
        cov_set = cov_sets.get(a, set())

        def series(hs: list[datetime]) -> list[dict]:
            res = []
            for h in hs:
                if h not in cov_set:
                    res.append({"hour": iso(h), "covered": False})
                    continue
                m = metrics.get((a, h)) or {}
                res.append({"hour": iso(h), "covered": True, "pool_swaps": int(m.get("pool_swaps") or 0),
                            "transactions": int(m.get("transactions") or 0), "token_volume": float(m.get("token_volume") or 0.0)})
            return res

        win = series(window_hours)
        base24 = [x for x in series(hours(b24, ws)) if x["covered"]]
        base7 = [x for x in series(hours(b7, ws)) if x["covered"]]
        wcov = [x for x in win if x["covered"]]
        row["window"] = {
            "hours_expected": len(window_hours), "hours_elapsed": len(elapsed), "hours_covered": len(wcov),
            "hourly": win,
            "pool_swaps": sum(x["pool_swaps"] for x in wcov) if wcov else None,
            "swaps_per_hour": (sum(x["pool_swaps"] for x in wcov) / len(wcov)) if wcov else None,
            "max_swaps_per_hour": max((x["pool_swaps"] for x in wcov), default=None),
            "token_volume_per_hour": (sum(x["token_volume"] for x in wcov) / len(wcov)) if wcov else None,
        }
        row["baseline_24h"] = {"hours_covered": len(base24), "hours_expected": 24,
                               "median_swaps_per_hour": median([x["pool_swaps"] for x in base24]),
                               "median_token_volume_per_hour": median([x["token_volume"] for x in base24])}
        row["baseline_days"] = {"days": args.baseline_days, "hours_covered": len(base7), "hours_expected": 24 * args.baseline_days,
                                "median_swaps_per_hour": median([x["pool_swaps"] for x in base7]),
                                "median_token_volume_per_hour": median([x["token_volume"] for x in base7])}
        w, b = row["window"], row["baseline_24h"]
        row["multiple_swaps_vs_24h"] = (w["swaps_per_hour"] / b["median_swaps_per_hour"]) if w["swaps_per_hour"] is not None and b["median_swaps_per_hour"] else None
        row["multiple_volume_vs_24h"] = (w["token_volume_per_hour"] / b["median_token_volume_per_hour"]) if w["token_volume_per_hour"] is not None and b["median_token_volume_per_hour"] else None

        row["findings"] = []
        if t["tracked"]:
            for f in be.get("/api/platform/findings", chain_id=CHAIN_ID, token=a, limit=200).get("findings") or []:
                if f.get("data_quality") != "verified":
                    continue
                fs, fe = utc(f["observation_start"]), utc(f["observation_end"]) + timedelta(hours=1)
                if not (fs < we and fe > ws):
                    continue
                fid = str(f["finding_id"])
                if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", fid):
                    continue
                ev = be.get(f"/api/platform/findings/{fid}/evidence")
                evd, trust = ev.get("evidence") or {}, ev.get("trust") or {}
                rpc = (ev.get("reproduce") or {}).get("public_rpc") or {}
                row["findings"].append({
                    "finding_id": fid, "anomaly_type": f.get("anomaly_type"), "severity": f.get("severity"),
                    "observation_start": iso(fs), "observation_end": iso(fe), "headline": ev.get("headline"),
                    "block_range": evd.get("block_range"), "swap_count": evd.get("swap_count"),
                    "pools": [p.get("address") for p in evd.get("pools") or []],
                    "coverage_share": (trust.get("coverage") or {}).get("share"),
                    "evidence_card": f"{APP}/platform/anomalies?evidence={urllib.parse.quote(fid)}",
                    "public_rpc": {k: rpc.get(k) for k in ("method", "address", "topics", "fromBlock", "toBlock") if k in rpc},
                })

        vr = [r for r in verified_rows if a in (r["token0"], r["token1"])]
        txs = {r["tx_hash"] for r in vr}
        attributed, field_only = [], 0
        for r in vr:
            origin = origins.get(r["tx_hash"])
            raw = int(r["a0"] if r["token0"] == a else r["a1"])
            item = {"tx_hash": r["tx_hash"], "block_number": r["block_number"], "block_time": r["block_time"], "pool": r["pool"],
                    "family": r["family"], "side": "sell" if raw > 0 else "buy"}
            if origin in reported:
                attributed.append(dict(item, origin=origin, origin_label=reported[origin]["label"]))
            elif r["sender"] in reported or r["recipient"] in reported:
                field_only += 1
        lr = [r for r in live_rows if r["token"] == a]
        live_attr = [{"tx_hash": r["tx_hash"], "block_number": r["block_number"], "block_time": r["block_time"], "pool": r["pool"],
                      "family": r["family"], "side": r["side"], "token_amount": float(r["token_amount"] or 0),
                      "origin": origins[r["tx_hash"]], "origin_label": reported[origins[r["tx_hash"]]]["label"]}
                     for r in lr if origins.get(r["tx_hash"]) in reported]
        row["address_check"] = {
            "run": bool(addrs["addresses"]), "method": "transaction origin (from) via eth_getTransactionByHash",
            "transactions": len(txs), "transactions_checked": len(txs & set(origins)),
            "matches": len(attributed), "field_only_not_attributed": field_only,
            "blocks": [attributed[0]["block_number"], attributed[-1]["block_number"]] if attributed else None,
            "swaps": attributed,
        }
        row["live_unverified"] = {
            "swaps": len(lr), "transactions": len({r["tx_hash"] for r in lr}),
            "transactions_checked": len({r["tx_hash"] for r in lr} & set(origins)),
            "attributed": live_attr,
            "attributed_sold": sum(x["token_amount"] for x in live_attr if x["side"] == "sell"),
            "blocks": [live_attr[0]["block_number"], live_attr[-1]["block_number"]] if live_attr else None,
        }
        out_tokens.append(row)

    return {
        "version": "incident-note/1.0",
        "incident_id": args.incident,
        "generated_at": iso(now),
        "catalog_generated_at": catalog.get("generated_at"),
        "network": {"key": NETWORK, "name": NETWORK_NAME, "chain_id": CHAIN_ID},
        "window": {"start": iso(ws), "end": iso(we), "hours": len(window_hours)},
        "baseline": {"h24": [iso(b24), iso(ws)], "days": args.baseline_days, "range": [iso(b7), iso(ws)]},
        "window_blocks": window_blocks,
        "addresses": {k: addrs[k] for k in ("file", "addresses", "sources", "rejected")},
        "tx_origin_lookup": origin_stats,
        "tokens": out_tokens,
    }


# ---------------------------------------------------------------- rendering

def tok_label(t: dict) -> str:
    return t.get("symbol") or f"Token {short(t['address'])}"


def cov_cell(t: dict) -> str:
    w = t["window"]
    if not t["tracked"]:
        return "Not tracked"
    if w["hours_covered"] == 0:
        return f"0 / {w['hours_expected']} h · not covered yet"
    badge = "ok" if w["hours_covered"] == w["hours_expected"] else "col"
    rest = "" if w["hours_covered"] == w["hours_expected"] else " · rest not covered yet"
    return f'<span class="badge {badge}">{w["hours_covered"]} / {w["hours_expected"]} h</span>{rest}'


def addr_cell(t: dict) -> str:
    txt = _addr_cell(t)
    lv = (t.get("live_unverified") or {}).get("attributed") or []
    if lv:
        txt += f" · live, unverified: {len(lv)} swap(s) in transactions sent by a reported address"
    return txt


def _addr_cell(t: dict) -> str:
    ac, w = t["address_check"], t["window"]
    if not t["tracked"]:
        return "n/a"
    if not ac["run"]:
        return "Not checked: no confirmed addresses yet"
    if ac["matches"]:
        return (f"Observed: {ac['matches']} swap(s) in transactions sent by a reported address, "
                f"blocks {fnum(ac['blocks'][0])}–{fnum(ac['blocks'][1])}")
    if w["hours_covered"] == 0:
        return "Not covered yet"
    unchecked = ac["transactions"] - ac["transactions_checked"]
    txt = (f"Not attributed: none of {fnum(ac['transactions_checked'])} checked transaction(s) was sent by a reported address"
           + (f"; {fnum(unchecked)} not checked" if unchecked else ""))
    if w["hours_covered"] < w["hours_expected"]:
        txt += f"; {w['hours_expected'] - w['hours_covered']} h not covered yet"
    return txt


def mult(x: float | None) -> str:
    return f"{x:.2f}×" if x is not None else "n/a"


def render(data: dict, incident: dict, index: bool) -> str:
    iid = data["incident_id"]
    url = f"{SITE}/incidents/{iid}/"
    ws, we = data["window"]["start"], data["window"]["end"]
    toks = data["tokens"]
    tracked = [t for t in toks if t["tracked"]]
    exp = sum(t["window"]["hours_expected"] for t in tracked)
    cov = sum(t["window"]["hours_covered"] for t in tracked)
    n_find = sum(len(t["findings"]) for t in tracked)
    addr_run = bool(data["addresses"]["addresses"])
    n_match = sum(t["address_check"]["matches"] for t in tracked)
    win_label = f"{human(ws)} → {human(we)}"
    summary = (f"As of {human(data['generated_at'])}, {cov} of {exp} token-hours of the window {win_label} are covered by verified data "
               f"for {len(tracked)} tracked {NETWORK_NAME} tokens. Hours not collected yet are shown as “not covered yet”, never as zero.")

    body = [
        f'<p class="crumbs"><a href="/">Home</a> / Incidents / {esc(iid)}</p>\n',
        f'<p class="kicker">Incident data note · {esc(NETWORK_NAME)} · updated as data arrives</p>\n',
        f"<h1>{esc(incident['title'])}</h1>\n",
        f'<p class="asof">Data as of {esc(human(data["generated_at"]))} · window {esc(win_label)} · '
        f'baseline: 24 h and {data["baseline"]["days"]} days before the window · <a href="data.json">data (JSON)</a></p>\n',
        f'<p class="intro">{esc(summary)}</p>\n',
        '<p class="note">An independent data note. It describes what our verified DEX swap data for tracked tokens shows around the '
        f'{esc(incident["short"])}. It is not a forensic trace, makes no accusation, and makes no statement about causes, '
        "customer impact or recovery of funds.</p>\n",
        '<h2 id="reported">What was reported</h2>\n<ul class="doc">\n',
    ]
    for text, flinks in incident["facts"]:
        extra = " ".join(f'(<a href="{esc(u)}" rel="nofollow noopener">{esc(lbl)}</a>)' for lbl, u in flinks)
        body.append(f"  <li>{esc(text)}{' ' + extra if extra else ''}</li>\n")
    body.append("</ul>\n")
    srcs = incident["sources"] + [s for s in data["addresses"].get("sources") or [] if s.get("name")]
    links = [f'<a href="{esc(s["url"])}" rel="nofollow noopener">{esc(s["name"])}</a>' if s.get("url") else f"{esc(s['name'])} (link pending review)"
             for s in srcs]
    body.append(f'<p class="fine">Reported by: {", ".join(links)}. These facts are reported by third parties and are not '
                "measured by us; figures differ between outlets and may change.</p>\n")

    # observed / not observed
    def row(what: str, status: str, detail: str) -> str:
        return f"<tr><th scope=\"row\">{esc(what)}</th><td>{status}</td><td>{detail}</td></tr>\n"

    if cov == 0:
        cov_status, cov_detail = "Not covered yet", f"0 of {exp} token-hours collected; the next ingestion run adds the window hours."
    else:
        cov_status = "Observed" if cov == exp else "Partly covered"
        cov_detail = f"{cov} of {exp} token-hours fully covered by successful ingestion runs."
    if cov == 0:
        f_status, f_detail = "Not covered yet", "No covered hours in the window yet."
    elif n_find:
        f_status, f_detail = "Observed", f"{n_find} verified finding(s) overlap the window; each links to its Evidence Card below."
    else:
        f_status, f_detail = "Not observed", f"No verified finding overlaps the window in the {cov} covered token-hours."
    high = [t for t in tracked if (t["multiple_swaps_vs_24h"] or 0) >= 2]
    if cov == 0:
        v_status, v_detail = "Not covered yet", "Window swaps cannot be compared with the baseline until the hours are collected."
    elif high:
        v_status = "Observed"
        v_detail = "Swaps per hour at least 2× the 24 h median: " + ", ".join(f"{esc(tok_label(t))} ({mult(t['multiple_swaps_vs_24h'])})" for t in high) + "."
    else:
        v_status, v_detail = "Not observed", "No tracked token reached 2× its 24 h median of swaps per hour in the covered window hours."
    n_tx = sum(t["address_check"]["transactions"] for t in tracked)
    n_chk = sum(t["address_check"]["transactions_checked"] for t in tracked)
    n_live = sum(len((t.get("live_unverified") or {}).get("attributed") or []) for t in tracked)
    if not addr_run:
        a_status, a_detail = "Not checked yet", "No reported address has been confirmed from public sources yet; the check runs once they are."
    elif n_match:
        a_status = "Observed"
        a_detail = (f"{n_match} swap(s) in tracked pools in transactions whose origin (the transaction's from address) is a reported "
                    "address (details below).")
    elif cov == 0:
        a_status, a_detail = "Not covered yet", f"{len(data['addresses']['addresses'])} reported address(es) to check; no covered hours yet."
    else:
        a_status = "Not attributed"
        a_detail = (f"None of {n_chk} checked transaction(s) with swaps in tracked pools was sent by a reported address"
                    + (f"; {n_tx - n_chk} transaction(s) not checked" if n_tx > n_chk else "")
                    + ("." if cov == exp else f"; {exp - cov} token-hours not covered yet."))
    if addr_run and n_live:
        a_detail += (f" Live, unverified: {n_live} swap(s) in transactions sent by a reported address, shown separately below until the "
                     "next scheduled run verifies them.")
    body += [
        '<h2 id="observed">What we observed and did not observe</h2>\n<div class="tbl"><table>\n',
        '<thead><tr><th scope="col">Question</th><th scope="col">Status</th><th scope="col">Detail</th></tr></thead>\n<tbody>\n',
        row("Coverage of the window", esc(cov_status), esc(cov_detail)),
        row("Verified anomaly findings in the window", esc(f_status), esc(f_detail)),
        row("Swap activity against the 24 h baseline", esc(v_status), v_detail if v_status == "Observed" else esc(v_detail)),
        row("Swaps sent by reported addresses in tracked pools", esc(a_status), esc(a_detail)),
        "</tbody></table></div>\n",
        '<p class="fine">“Not observed” is stated only for hours that are fully covered. A swap is attributed to a reported address '
        "only when that address sent the transaction; a router, aggregator or solver in between is “not attributed”. "
        "Scope: the verified pools we ingest for each "
        "token on Ethereum; other venues, other chains and centralised exchanges are not counted.</p>\n",
    ]

    # per token
    days = data["baseline"]["days"]
    body += [
        f'<h2 id="tokens">Tracked {esc(NETWORK_NAME)} tokens</h2>\n<div class="tbl"><table>\n<thead><tr>'
        '<th scope="col">Token</th><th scope="col">Window coverage</th><th scope="col" class="n">Swaps / h in window</th>'
        '<th scope="col" class="n">24 h median</th><th scope="col" class="n">vs 24 h</th>'
        f'<th scope="col" class="n opt">{days}-day median</th><th scope="col" class="n">Token volume / h vs 24 h</th>'
        '<th scope="col" class="n">Findings</th><th scope="col">Reported addresses</th></tr></thead>\n<tbody>\n',
    ]
    for t in toks:
        w, b, bd = t["window"], t["baseline_24h"], t["baseline_days"]
        name = esc(tok_label(t))
        if t["tracked"] and t.get("symbol"):
            name = f'<a href="/tokens/{NETWORK}/{esc(t["address"])}/">{name}</a>'
        sub = f'<span class="sub"><code>{esc(short(t["address"]))}</code></span>'
        body.append(
            f"<tr><td>{name}{sub}</td><td>{cov_cell(t)}</td>"
            f'<td class="n">{fnum(w["swaps_per_hour"]) if w["hours_covered"] else "not covered yet"}</td>'
            f'<td class="n">{fnum(b["median_swaps_per_hour"])}<span class="sub">{b["hours_covered"]} of 24 h covered</span></td><td class="n">{mult(t["multiple_swaps_vs_24h"])}</td>'
            f'<td class="n opt">{fnum(bd["median_swaps_per_hour"])}<span class="sub">{bd["hours_covered"]} of {bd["hours_expected"]} h</span></td><td class="n">{mult(t["multiple_volume_vs_24h"])}</td>'
            f'<td class="n">{len(t["findings"]) if w["hours_covered"] else "n/a"}</td><td>{esc(addr_cell(t))}</td></tr>\n'
        )
    body.append("</tbody></table></div>\n")
    body.append('<p class="fine">Medians use covered hours only (a covered hour without swaps counts as 0). Token volume is in token units; '
                "USD values are not part of the evidence. Pool lists per token are in the JSON data file.</p>\n")

    # hourly grid
    if data["window"]["hours"] <= 12:
        hs = [h["hour"] for h in (tracked[0]["window"]["hourly"] if tracked else [])]
        body.append('<h3 id="hourly">Swaps per hour in the window</h3>\n<div class="tbl"><table>\n<thead><tr><th scope="col">Token</th>')
        body += [f'<th scope="col" class="n">{esc(utc(h).strftime("%H:%M"))}</th>' for h in hs]
        body.append("</tr></thead>\n<tbody>\n")
        for t in tracked:
            cells = "".join(f'<td class="n">{fnum(x["pool_swaps"]) if x["covered"] else "not covered yet"}</td>' for x in t["window"]["hourly"])
            body.append(f"<tr><td>{esc(tok_label(t))}</td>{cells}</tr>\n")
        body.append("</tbody></table></div>\n")

    # findings
    body.append('<h2 id="findings">Verified findings in the window</h2>\n')
    fl = [(t, f) for t in tracked for f in t["findings"]]
    if not fl:
        body.append(f'<p class="intro">{"None in the covered hours." if cov else "Not covered yet: no window hours have been collected."}</p>\n')
    else:
        body.append('<div class="tbl"><table>\n<thead><tr><th scope="col">Token</th><th scope="col">Finding</th><th scope="col">Window (UTC)</th>'
                    '<th scope="col" class="n">Blocks</th><th scope="col" class="n">Swaps</th><th scope="col" class="n">Coverage</th>'
                    '<th scope="col">Evidence</th></tr></thead>\n<tbody>\n')
        for t, f in fl:
            br = f.get("block_range") or {}
            blocks = f"{fnum(br.get('from'))}–{fnum(br.get('to'))}" if br.get("from") is not None else "n/a"
            share = f"{f['coverage_share'] * 100:.0f} %" if isinstance(f.get("coverage_share"), (int, float)) else "n/a"
            body.append(
                f"<tr><td>{esc(tok_label(t))}</td><td>{esc(f.get('headline') or f.get('anomaly_type') or '')}"
                f'<span class="sub">{esc(f.get("anomaly_type") or "")} · {esc(f.get("severity") or "")}</span></td>'
                f"<td>{esc(human(f['observation_start']))} → {esc(human(f['observation_end']))}</td>"
                f'<td class="n">{blocks}</td><td class="n">{fnum(f.get("swap_count"))}</td><td class="n">{share}</td>'
                f'<td><a href="{esc(f["evidence_card"])}">Evidence Card</a></td></tr>\n'
            )
        body.append("</tbody></table></div>\n")

    # address matches
    if addr_run:
        body.append('<h2 id="addresses">Reported addresses checked</h2>\n<div class="tbl"><table>\n<thead><tr><th scope="col">Address</th>'
                    '<th scope="col">Label (as reported)</th><th scope="col">Source</th></tr></thead>\n<tbody>\n')
        for a in data["addresses"]["addresses"]:
            src = esc(a.get("source") or "")
            if a.get("source_url"):
                src = f'<a href="{esc(a["source_url"])}" rel="nofollow noopener">{src or "source"}</a>'
            body.append(f'<tr><td><a href="{EXPLORER}/address/{esc(a["address"])}"><code>{esc(a["address"])}</code></a></td>'
                        f"<td>{esc(a['label'])}</td><td>{src or 'as reported in the sources above'}</td></tr>\n")
        body.append("</tbody></table></div>\n")
        def swap_table(items: list, live: bool) -> None:
            amt = '<th scope="col" class="n">Token amount</th>' if live else ""
            body.append('<div class="tbl"><table>\n<thead><tr><th scope="col">Token</th><th scope="col" class="n">Block</th>'
                        '<th scope="col">Time (UTC)</th><th scope="col">Transaction</th><th scope="col">Pool</th>'
                        f'<th scope="col">Side</th>{amt}<th scope="col">Transaction sent by</th></tr></thead>\n<tbody>\n')
            for t, x in items[: SHOW_TX * 5]:
                tx = x["tx_hash"] if HASH_RE.fullmatch(str(x["tx_hash"])) else ""
                a_cell = f'<td class="n">{fnum(x.get("token_amount"))}</td>' if live else ""
                body.append(f'<tr><td>{esc(tok_label(t))}</td><td class="n">{fnum(x["block_number"])}</td><td>{esc(x["block_time"])}</td>'
                            f'<td><a href="{EXPLORER}/tx/{esc(tx)}"><code>{esc(short(tx))}</code></a></td>'
                            f'<td><code>{esc(short(str(x["pool"])))}</code></td><td>{esc(x["side"])}</td>{a_cell}'
                            f'<td><code>{esc(short(x["origin"]))}</code> ({esc(x["origin_label"])})</td></tr>\n')
            body.append("</tbody></table></div>\n")
            if len(items) > SHOW_TX * 5:
                body.append('<p class="fine">More rows are listed in the JSON data file.</p>\n')

        ms = [(t, x) for t in tracked for x in t["address_check"]["swaps"]]
        if ms:
            body.append('<h3 id="attributed">Verified swaps in transactions sent by a reported address</h3>\n')
            swap_table(ms, live=False)
        ol = data.get("tx_origin_lookup") or {}
        if ol:
            body.append(f'<p class="fine">Transaction origins resolved with eth_getTransactionByHash: {fnum(ol.get("resolved"))} of '
                        f'{fnum(ol.get("transactions"))} transaction(s). Swap rows whose sender or recipient field is a reported address but '
                        f'whose transaction was sent by another address: {fnum(sum(t["address_check"]["field_only_not_attributed"] for t in tracked))} '
                        "(not attributed).</p>\n")
        lv = [(t, x) for t in tracked for x in (t.get("live_unverified") or {}).get("attributed") or []]
        if lv:
            body.append('<h3 id="live">Live data, unverified (confirmed after the next 12-hour run)</h3>\n')
            body.append('<p class="note">Near-real-time swaps from our live feed, for hours that verified data does not cover yet. They are '
                        "not verified and carry no Evidence Card; they are replaced by verified data after the next scheduled run.</p>\n")
            sold = ", ".join(f"{esc(tok_label(t))}: {fnum(t['live_unverified']['attributed_sold'])} sold in "
                             f"{len(t['live_unverified']['attributed'])} swap(s), blocks {fnum(t['live_unverified']['blocks'][0])}–"
                             f"{fnum(t['live_unverified']['blocks'][1])}" for t in tracked if (t.get("live_unverified") or {}).get("attributed"))
            body.append(f'<p class="intro">{sold}.</p>\n')
            swap_table(lv, live=True)

    # method + verify
    wb = data.get("window_blocks")
    wb_txt = (f"Decoded swaps in the window span blocks {fnum(wb['from'])}–{fnum(wb['to'])}."
              if wb else "Block range of the window: not covered yet.")
    body += [
        '<h2 id="method">Method</h2>\n<ul class="doc">\n',
        "  <li>Data: Swap events of each tracked token's verified pools (Uniswap v2-like, v3-like and v4 families), read with "
        "eth_getLogs from free public Ethereum RPC endpoints and decoded once per transaction and log index.</li>\n",
        "  <li>Coverage: an hour counts only when successful ingestion runs cover all of it. Uncovered hours are missing data, "
        "never zero, and are shown as “not covered yet”.</li>\n",
        f"  <li>Baseline: median of the covered hours in the 24 h before the window (the anomaly engine's lookback) and in the "
        f"{days} days before it, for swaps per hour and token volume per hour.</li>\n",
        "  <li>Findings: verified anomaly findings whose observation window overlaps the incident window. A finding is a "
        "statistical change in the measured pools; it does not identify a cause.</li>\n",
        "  <li>Reported addresses: for every transaction with a swap in a tracked pool inside covered hours, the transaction's "
        "origin (its from address) is read with eth_getTransactionByHash from a public node. A swap counts as observed only "
        "when that origin is a reported address. Swaps filled by a solver or relayer on someone's behalf are sent by the "
        "solver, so they are “not attributed” even when the tokens came from a reported address.</li>\n",
        "</ul>\n",
        '<h2 id="verify">How to verify</h2>\n',
        f'<p class="intro">Anyone can re-read the same logs from a public {esc(NETWORK_NAME)} JSON-RPC node, without access to our database. {esc(wb_txt)}</p>\n',
        "<pre>// Swap logs of the tracked pools in the window; to test one address, put it (left-padded to 32 bytes)\n"
        "// in the sender or recipient topic position, or leave the position null for all swaps\n"
        '{"method": "eth_getLogs", "params": [{\n'
        '  "address":   ["&lt;tracked pools of the token (JSON data file)&gt;"],\n'
        '  "topics":    ["&lt;Swap topic&gt;", "&lt;topic1 or null&gt;", "&lt;topic2 or null&gt;"],\n'
        '  "fromBlock": "&lt;first block of the window, hex&gt;",\n'
        '  "toBlock":   "&lt;last block of the window, hex&gt;"\n'
        "}]}</pre>\n",
        '<p class="intro">To check who sent a listed transaction: <code>{"method": "eth_getTransactionByHash", "params": ["&lt;tx hash&gt;"]}</code> '
        "returns its <code>from</code> address.</p>\n",
        '<div class="tbl"><table>\n<thead><tr><th scope="col">Event</th><th scope="col">topic0</th><th scope="col">Indexed fields</th></tr></thead>\n<tbody>\n',
    ]
    body += [f"<tr><td>{esc(n)}</td><td><code>{esc(t0)}</code></td><td>{esc(d)}</td></tr>\n" for n, t0, d in SWAP_TOPICS]
    body += [
        "</tbody></table></div>\n",
        '<p class="fine">Uniswap v4 swaps are logged by the PoolManager contract with the pool id as topic1. Each finding\'s exact '
        "pools, block range and expected swap count are in its Evidence Card and in the JSON data file.</p>\n",
        '<h2 id="next">Evidence for one token</h2>\n<div class="ctas">\n',
        f'  <a class="btn primary" href="mailto:{EMAIL}?subject={urllib.parse.quote("Incident evidence snapshot: " + iid)}">Ask for an Incident Evidence Snapshot</a>\n',
        f'  <a class="btn" href="{PRICING_URL}">Pricing</a>\n  <a class="btn" href="{METHOD_URL}">Method</a>\n</div>\n',
        '<p class="fine">Independent analytics; not affiliated with Binance, Tether, Circle or any token team. Not investment, legal '
        "or forensic advice.</p>\n",
    ]

    graph = [ORG, {
        "@type": "Report", "@id": url, "url": url, "name": incident["title"], "description": summary,
        "datePublished": data["generated_at"], "dateModified": data["generated_at"], "inLanguage": "en",
        "publisher": {"@id": ORG["@id"]},
        "isPartOf": {"@type": "WebSite", "@id": f"{SITE}/#website", "url": f"{SITE}/", "name": "WATAM DeFi Intelligence"},
    }]
    return document(title=f"{incident['title']} | WATAM DeFi Intelligence", description=summary, canonical=url,
                    robots=None if index else "noindex", graph=graph, body="".join(body), current="incidents")


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--incident", default="binance-2026-10-29", choices=sorted(INCIDENTS))
    p.add_argument("--window-start", default="2026-10-29T20:00Z")
    p.add_argument("--window-end", default="2026-10-30T00:00Z")
    p.add_argument("--baseline-days", type=int, default=7)
    p.add_argument("--addresses", default=DEFAULT_ADDRESSES, help="JSON file of reported addresses (used only if it exists)")
    p.add_argument("--backend", default="http://127.0.0.1:8010")
    p.add_argument("--env-file", default=DEFAULT_ENV, help="file with APP_ACCESS_TOKEN (never printed)")
    p.add_argument("--clickhouse-cmd", default="docker exec defi-clickhouse clickhouse-client")
    p.add_argument("--rpc", default="https://ethereum-rpc.publicnode.com", help="public JSON-RPC for transaction origins")
    p.add_argument("--max-tx-lookups", type=int, default=5000, help="cap on new eth_getTransactionByHash lookups per run")
    p.add_argument("--tx-cache", default="~/.cache/watam-incident/tx-origin-ethereum.json")
    p.add_argument("--out", default=str(Path(__file__).resolve().parent.parent))
    p.add_argument("--index", action="store_true", help="allow indexing (default: noindex until reviewed)")
    p.add_argument("--render-only", action="store_true", help="re-render index.html from the existing data.json")
    args = p.parse_args()
    if not ID_RE.fullmatch(args.incident) or not 1 <= args.baseline_days <= 30:
        p.error("invalid incident id or baseline days")
    if utc(args.window_end) <= utc(args.window_start):
        p.error("window end must be after window start")

    incident = INCIDENTS[args.incident]
    out_dir = Path(args.out) / "incidents" / args.incident
    data_file = out_dir / "data.json"
    if args.render_only:
        data = json.loads(data_file.read_text())
    else:
        data = collect(args, incident)
        out_dir.mkdir(parents=True, exist_ok=True)
        data_file.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    (out_dir / "index.html").write_text(render(data, incident, args.index))
    tracked = [t for t in data["tokens"] if t["tracked"]]
    errs = [t["symbol"] or t["address"] for t in tracked if t.get("coverage_error")]
    if errs:
        print(f"warning: coverage unavailable (shown as not covered yet) for: {', '.join(errs)}", file=sys.stderr)
    print(f"{out_dir}/index.html: {len(tracked)} tracked tokens, "
          f"{sum(t['window']['hours_covered'] for t in tracked)}/{sum(t['window']['hours_expected'] for t in tracked)} token-hours covered, "
          f"{sum(len(t['findings']) for t in tracked)} findings, {len(data['addresses']['addresses'])} addresses, "
          f"{sum(t['address_check']['matches'] for t in tracked)} attributed swaps, "
          f"{sum(len((t.get('live_unverified') or {}).get('attributed') or []) for t in tracked)} live unverified attributed, robots={'index' if args.index else 'noindex'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
