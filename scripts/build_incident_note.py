#!/usr/bin/env python3
"""Build a neutral, evidence-based incident data note: incidents/<id>/index.html + incidents/<id>/data.json.

Standard library only. It runs where the DeFi Intelligence backend and its ClickHouse are reachable (the operator's machine);
the GitHub Action does NOT run it. For every tracked Ethereum token (plus tokens named in the addresses file) it reports:

  * coverage of the incident window: hours fully covered by successful ingestion runs against hours expected
    (an hour not collected yet is "not covered yet", never zero);
  * verified anomaly findings whose window overlaps the incident window, with Evidence Card links and block ranges;
  * swaps per hour and token volume per hour in the window against the median of the covered hours of the 24 h
    (and the N days) before the window;
  * if reported addresses are given: swaps in our tracked pools with such an address as Swap sender or recipient.

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
MAX_MATCH_ROWS = 2000
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
        "facts": [
            "At about 20:24 UTC on 29 October 2026 a hot wallet labelled “Binance 14” was reported to have sent about 167.5k ETH, "
            "about 1.6 billion USDT and more than 60 other tokens (reported examples: RLUSD, wBETH, XAUt, UNI, USD1) to a newly "
            "created address in one transaction.",
            "A later transfer of about 780 million USDC was reported. Reported totals differ between outlets, from about "
            "$2.5 billion upward.",
            "Reports say the address that initiated the transaction (reported as 0x7C86…5CA0) had been funded through Tornado Cash.",
            "Tether was reported to have frozen about 971 million USDT and Circle about 823 million USDC; later reports give higher "
            "USDT figures.",
            "Binance was reported to be investigating; statements quoted in the reports said user funds were safe. At the time of "
            "these reports Binance had not formally confirmed the cause.",
        ],
        "sources": [
            {"name": "PANews", "url": None},
            {"name": "BeInCrypto", "url": "https://beincrypto.com/is-binance-being-hacked-customer-funds/"},
            {"name": "Cointelegraph", "url": "https://cointelegraph.com/news/binance-linked-wallet-faces-possible-exploit-blockchain-researcher-warns"},
            {"name": "Crypto Briefing (stablecoin freezes)", "url": "https://cryptobriefing.com/tether-freezes-971m-usdt-binance-hack/"},
            {"name": "Crypto Briefing (statements)", "url": "https://cryptobriefing.com/binance-suspected-hack-user-funds-safe/"},
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
        with self.opener.open(req, timeout=120) as resp:
            return json.load(resp)


def clickhouse(cmd: list[str], sql: str, params: dict[str, str]) -> list[dict]:
    args = cmd + [f"--param_{k}={v}" for k, v in params.items()] + ["-q", sql + " FORMAT JSONEachRow"]
    res = subprocess.run(args, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if res.returncode:
        raise RuntimeError(f"clickhouse query failed ({res.returncode}): {res.stderr.strip()[:500]}")
    out = res.stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


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

    matches: list[dict] = []
    if addrs["addresses"]:
        alist = [a["address"] for a in addrs["addresses"]]
        matches = clickhouse(ch_cmd,
                             "SELECT pool, family, block_number, toString(block_time) AS bt, tx_hash, lower(sender) AS sender, "
                             "lower(recipient) AS recipient, token0, token1 FROM defi_chain.decoded_swaps FINAL "
                             "WHERE chain_id = {chain:UInt32} AND block_time >= {f:DateTime('UTC')} AND block_time < {t:DateTime('UTC')} "
                             "AND (has({a:Array(String)}, lower(sender)) OR has({a:Array(String)}, lower(recipient))) "
                             f"ORDER BY block_number, tx_hash LIMIT {MAX_MATCH_ROWS}",
                             {"chain": str(CHAIN_ID), "a": ch_array(alist), "f": ch_ts(ws), "t": ch_ts(we)})
        role = {a["address"]: a["role"] for a in addrs["addresses"]}
        for m in matches:
            m["block_number"] = int(m["block_number"])
            m["block_time"] = m.pop("bt").replace(" ", "T") + "Z"
            m["roles"] = sorted({f"{'sender' if a == m['sender'] else 'recipient'}: {role[a]}"
                                 for a in (m["sender"], m["recipient"]) if a in role})

    window_hours = hours(ws, we)
    elapsed = [h for h in window_hours if h + timedelta(hours=1) <= now]
    out_tokens = []
    for t in tokens:
        a = t["address"]
        row = dict(t, pools=pools.get(a, []), coverage_error=None)
        if t["tracked"]:
            try:
                cov = be.get(f"/api/platform/explorer/{CHAIN_ID}/{a}/coverage", from_time=iso(b7), to_time=iso(we))
                cov_set = covered_hours(cov, b7, we)
            except Exception as exc:  # noqa: BLE001 — reported as missing coverage, never guessed
                cov_set, row["coverage_error"] = set(), type(exc).__name__
        else:
            cov_set = set()

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

        tm = [m for m in matches if a in (m["token0"], m["token1"])]
        row["address_check"] = {
            "run": bool(addrs["addresses"]), "matches": len(tm),
            "blocks": [tm[0]["block_number"], tm[-1]["block_number"]] if tm else None,
            "swaps": [{k: m[k] for k in ("tx_hash", "block_number", "block_time", "pool", "family", "roles")} for m in tm],
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
        "address_matches_truncated": len(matches) >= MAX_MATCH_ROWS,
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
    ac, w = t["address_check"], t["window"]
    if not t["tracked"]:
        return "n/a"
    if not ac["run"]:
        return "Not checked: no confirmed addresses yet"
    if ac["matches"]:
        return f"Observed: {ac['matches']} swap(s), blocks {fnum(ac['blocks'][0])}–{fnum(ac['blocks'][1])}"
    if w["hours_covered"] == 0:
        return "Not covered yet"
    if w["hours_covered"] == w["hours_expected"]:
        return f"Not observed in {w['hours_covered']} covered hours"
    return f"Not observed in {w['hours_covered']} covered hours; {w['hours_expected'] - w['hours_covered']} not covered yet"


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
    body += [f"  <li>{esc(f)}</li>\n" for f in incident["facts"]]
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
    if not addr_run:
        a_status, a_detail = "Not checked yet", "No reported address has been confirmed from public sources yet; the check runs once they are."
    elif n_match:
        a_status, a_detail = "Observed", f"{n_match} swap(s) in tracked pools with a reported address as Swap sender or recipient (details below)."
    elif cov == 0:
        a_status, a_detail = "Not covered yet", f"{len(data['addresses']['addresses'])} reported address(es) to check; no covered hours yet."
    else:
        a_status = "Not observed"
        a_detail = (f"No swap in tracked pools with a reported address as Swap sender or recipient in {cov} covered token-hours"
                    + ("." if cov == exp else f"; {exp - cov} token-hours not covered yet."))
    body += [
        '<h2 id="observed">What we observed and did not observe</h2>\n<div class="tbl"><table>\n',
        '<thead><tr><th scope="col">Question</th><th scope="col">Status</th><th scope="col">Detail</th></tr></thead>\n<tbody>\n',
        row("Coverage of the window", esc(cov_status), esc(cov_detail)),
        row("Verified anomaly findings in the window", esc(f_status), esc(f_detail)),
        row("Swap activity against the 24 h baseline", esc(v_status), v_detail if v_status == "Observed" else esc(v_detail)),
        row("Swaps by reported addresses in tracked pools", esc(a_status), esc(a_detail)),
        "</tbody></table></div>\n",
        '<p class="fine">“Not observed” is stated only for hours that are fully covered. Scope: the verified pools we ingest for each '
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
                        f"<td>{esc(a['label'])}</td><td>{src or 'n/a'}</td></tr>\n")
        body.append("</tbody></table></div>\n")
        ms = [(t, s) for t in tracked for s in t["address_check"]["swaps"]]
        if ms:
            body.append('<div class="tbl"><table>\n<thead><tr><th scope="col">Token</th><th scope="col" class="n">Block</th>'
                        '<th scope="col">Time (UTC)</th><th scope="col">Transaction</th><th scope="col">Pool</th><th scope="col">Match</th></tr></thead>\n<tbody>\n')
            for t, s in ms[: SHOW_TX * 5]:
                tx = s["tx_hash"] if HASH_RE.fullmatch(str(s["tx_hash"])) else ""
                body.append(f'<tr><td>{esc(tok_label(t))}</td><td class="n">{fnum(s["block_number"])}</td><td>{esc(s["block_time"])}</td>'
                            f'<td><a href="{EXPLORER}/tx/{esc(tx)}"><code>{esc(short(tx))}</code></a></td>'
                            f'<td><code>{esc(short(str(s["pool"])))}</code></td><td>{esc("; ".join(s["roles"]))}</td></tr>\n')
            body.append("</tbody></table></div>\n")
            if len(ms) > SHOW_TX * 5 or data.get("address_matches_truncated"):
                body.append('<p class="fine">More matches are listed in the JSON data file.</p>\n')

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
        "  <li>Reported addresses: matched against the sender and recipient fields of decoded Swap events. A trade routed through "
        "an aggregator or router shows the router as sender, and the transaction signer is not checked, so “not observed” "
        "means not observed in these fields of these pools only.</li>\n",
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
    print(f"{out_dir}/index.html: {len(tracked)} tracked tokens, "
          f"{sum(t['window']['hours_covered'] for t in tracked)}/{sum(t['window']['hours_expected'] for t in tracked)} token-hours covered, "
          f"{sum(len(t['findings']) for t in tracked)} findings, {len(data['addresses']['addresses'])} addresses, "
          f"{sum(t['address_check']['matches'] for t in tracked)} address matches, robots={'index' if args.index else 'noindex'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
