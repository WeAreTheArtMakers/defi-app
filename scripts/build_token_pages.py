#!/usr/bin/env python3
"""Build the static token pages, sitemap.xml and llms-full.txt from the public catalog.

Standard library only. Reads https://app.wearetheartmakers.com/api/public/catalog (or --catalog-file) and writes:

  tokens/index.html                       list of every catalog entry, grouped by network
  tokens/<network>/<address>/index.html   one page per entry (TON "0:…" addresses use "0-…" in the path)
  sitemap.xml                             home, method, pricing / terms / privacy / legal and the indexable token pages
  llms-full.txt                           long-form summary for LLM tools, with the indexable token list

Only fields of our own public catalog are used. Third-party-derived fields (for example the TON
"supported_share" volume ratio) are deliberately not rendered. Output is deterministic for a given
catalog, so a scheduled run commits only when the catalog changed.

  python3 scripts/build_token_pages.py [--catalog-file catalog.json] [--out .]
  python3 scripts/build_token_pages.py indexnow --key KEY < changed-paths.txt
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SITE = "https://defi.wearetheartmakers.com"
APP = "https://app.wearetheartmakers.com"
CATALOG_URL = f"{APP}/api/public/catalog"
COVERAGE_URL = f"{APP}/api/public/scorecard-coverage"   # the venues the Market Maker Scorecard reads (names only)
PRICING_URL = f"{SITE}/pricing/"
METHOD_URL = f"{SITE}/method/"
OG_IMAGE = f"{SITE}/assets/og-card.png"
TERMS_URL = f"{SITE}/terms/"
PRIVACY_URL = f"{SITE}/privacy/"
LEGAL_URL = f"{SITE}/legal/"
CASE_STUDY_URLS = (f"{SITE}/case-studies/zora-base/", f"{SITE}/research/binance-14-outflow-2026-10-29/",
                   f"{SITE}/research/market-reality-ena/", f"{SITE}/live-pro/", f"{SITE}/exit-check/")
STATIC_PAGES = (METHOD_URL, *CASE_STUDY_URLS, PRICING_URL, TERMS_URL, PRIVACY_URL, LEGAL_URL)  # hand-written pages listed in sitemap.xml
REPO_URL = "https://github.com/WeAreTheArtMakers/defi-intelligence"
EMAIL = "admin@wearetheartmakers.com"
INDEPENDENCE = "Independent analytics; not affiliated with the projects or exchanges listed."
STATUS_LABEL = {"available": "Available", "collecting": "Collecting", "stale": "Stale"}
STATUS_BADGE = {"available": "ok", "collecting": "col", "stale": "stale"}
VM_LABEL = {"evm": "EVM", "svm": "SVM", "tvm": "TVM"}
ADDRESS_LABEL = {"evm": "Contract address", "svm": "Mint address", "tvm": "Jetton master address"}
ORG = {
    "@type": "Organization",
    "@id": f"{SITE}/#org",
    "name": "WATAM Intelligence",
    "alternateName": ["WATAM", "We Are The Art Makers"],
    "url": f"{SITE}/",
    "email": EMAIL,
    "logo": f"{SITE}/assets/favicon.svg",
    "sameAs": [REPO_URL],
}

esc = html.escape


# ---------------------------------------------------------------- data helpers

def fetch_catalog(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "watam-site-builder/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed https URL)
        return json.load(resp)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def fmt_dt(value: str | None) -> str:
    dt = parse_ts(value)
    return dt.strftime("%d %b %Y, %H:%M UTC") if dt else "not yet"


def fmt_day(value: str | None) -> str:
    if not value:
        return "not yet"
    dt = parse_ts(value) if "T" in value else datetime.fromisoformat(value)
    return dt.strftime("%d %b %Y")


def iso_z(value: str | None) -> str | None:
    dt = parse_ts(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def num(n) -> str:
    return f"{n:,}" if isinstance(n, int) else "n/a"


# Catalog fields that become file paths must look like a network key and a contract / mint / TON raw address: anything else
# (e.g. "../") is skipped rather than written outside tokens/.
NETWORK_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}|-?[0-9]{1,3}:[0-9a-fA-F]{64}|[1-9A-HJ-NP-Za-km-z]{32,44}")
MAX_SHRINK = 0.8          # refuse a catalog with fewer than 80 % of the pages already built (a partial API answer)
# A deliberate removal of more than 20 % of the token pages needs a manual `rm -r tokens` before the build.


def valid_entry(entry: dict) -> bool:
    return bool(NETWORK_RE.fullmatch(str(entry.get("network") or "")) and ADDRESS_RE.fullmatch(str(entry.get("address") or "")))


def plain(s: object, limit: int = 48) -> str:
    """On-chain names / symbols as inert text for llms-full.txt: no markdown, links or line breaks."""
    s = re.sub(r"[^\w .,+&'/-]", "", str(s or ""), flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()[:limit]


def vm_of(entry: dict) -> str:
    return entry.get("vm") or "evm"


def slug(entry: dict) -> str:
    addr = entry["address"]
    if addr.lower().startswith("0x"):
        addr = addr.lower()
    return addr.replace(":", "-")


def page_path(entry: dict) -> str:
    return f"/tokens/{entry['network']}/{slug(entry)}/"


def short_addr(addr: str) -> str:
    return addr if len(addr) <= 14 else f"{addr[:8]}…{addr[-6:]}"


def display_symbol(entry: dict) -> str:
    return entry.get("symbol") or f"Token {short_addr(entry['address'])}"


def indexable(entry: dict, catalog: dict) -> bool:
    return entry.get("status") == "available" and (entry.get("history_hours") or 0) >= catalog.get("min_history_hours", 72)


def refresh_hours(entry: dict, catalog: dict) -> int:
    vm = vm_of(entry)
    if vm == "svm":
        return catalog.get("solana_refresh_every_hours", 1)
    if vm == "tvm":
        return catalog.get("ton_refresh_every_hours", 3)
    return catalog.get("refresh_every_hours", 12)


def detection_note(entry: dict) -> str | None:
    an = entry.get("anomalies")
    if an == "not_available_for_solana":
        return "Anomaly detection is not yet available for Solana. This page shows coverage and verified swaps only."
    if an == "not_available_for_ton":
        return "Anomaly detection is not yet available for TON. This page shows coverage and verified swaps only."
    return None


def jsonld(obj: dict) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return text.replace("</", "<\\/")


# ---------------------------------------------------------------- layout

_LOGO_MARK = ('<rect width="32" height="32" rx="8" fill="#0e1525"/><path d="M8 21l5-6 4 3 7-9" fill="none" stroke="url(#{g})" stroke-width="2.6" '
              'stroke-linecap="round" stroke-linejoin="round"/><circle cx="24" cy="9" r="2.2" fill="#38d3f0"/>')


def _logo(gid: str) -> str:
    """The app's mark and the "WATAM DeFi Intelligence" wordmark (inline SVG, same as the app's public pages)."""
    return (
        '<svg class="logo" viewBox="0 0 236 32" role="img" aria-label="WATAM DeFi Intelligence" xmlns="http://www.w3.org/2000/svg">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5b8cff"/><stop offset="1" stop-color="#38d3f0"/></linearGradient></defs>'
        + _LOGO_MARK.replace("{g}", gid)
        + '<rect x=".5" y=".5" width="31" height="31" rx="7.5" fill="none" stroke="currentColor" stroke-opacity=".28"/>'
        '<text x="42" y="21.5" font-size="16" font-weight="650" fill="currentColor" '
        'font-family="ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">WATAM <tspan fill-opacity=".72">DeFi Intelligence</tspan></text></svg>'
    )


NAV_LINKS = [
    ("Tokens", "/tokens/", "tokens"), ("Method", "/method/", "method"), ("Pricing", "/pricing/", "pricing"),
    ("Live Pro", "/live-pro/", "live-pro"),
    ("Türkiye Liquidity Index", f"{APP}/tr-index", ""),
    ("Partners", f"{APP}/partners", ""),
]
FOOTER_LINKS = [
    ("Türkiye Liquidity Index", f"{APP}/tr-index"), ("Türkiye Stress Atlas", f"{APP}/tr-atlas"),
    ("Method &amp; Trust", f"{APP}/methodology"), ("Data platform", f"{APP}/platform"),
    ("Token catalog", "/tokens/"), ("Pricing", "/pricing/"), ("Contact: " + EMAIL, f"mailto:{EMAIL}"),
    ("Privacy", "/privacy/"), ("Terms", "/terms/"), ("Legal", "/legal/"),
]


def header(current: str) -> str:
    links = "".join(
        f'<a href="{h}"{" aria-current=\"page\"" if k and k == current else ""}>{label}</a>' for label, h, k in NAV_LINKS
    )
    return (
        '<a class="skip" href="#main">Skip to content</a>\n'
        f'<header class="site" id="top"><div class="in"><a class="brand" href="/" aria-label="WATAM DeFi Intelligence">{_logo("wg1")}</a>'
        f'<nav class="main dk" aria-label="Main">{links}</nav>'
        f'<details class="menu"><summary>Menu</summary><nav class="main" aria-label="Menu">{links}</nav></details>'
        f'<a class="btn primary signin" href="{APP}/login">Sign in</a></div></header>\n'
    )


def footer() -> str:
    items = "".join(f'<li><a href="{h}">{label}</a></li>' for label, h in FOOTER_LINKS)
    return (
        f'<footer class="sitefoot"><div class="in"><div>{_logo("wg2")}'
        "<p>WATAM Intelligence measures public market data and publishes verifiable aggregates.</p>"
        f"<p>{esc(INDEPENDENCE)} Token and network names are trademarks of their owners. Informational, not investment advice.</p></div>"
        f"<ul>{items}</ul>"
        '<div class="legal"><span>© 2026 WATAM Intelligence</span><span>Not investment advice</span></div></div></footer>\n'
    )


def document(*, title: str, description: str, canonical: str, robots: str | None, graph: list, body: str, current: str) -> str:
    robots_tag = f'<meta name="robots" content="{robots}">\n' if robots else ""
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f"<title>{esc(title)}</title>\n"
        f'<meta name="description" content="{esc(description)}">\n'
        f"{robots_tag}"
        f'<link rel="canonical" href="{canonical}">\n'
        '<meta property="og:type" content="website">\n'
        '<meta property="og:site_name" content="WATAM DeFi Intelligence">\n'
        f'<meta property="og:url" content="{canonical}">\n'
        f'<meta property="og:title" content="{esc(title)}">\n'
        f'<meta property="og:description" content="{esc(description)}">\n'
        f'<meta property="og:image" content="{OG_IMAGE}">\n'
        '<meta property="og:image:width" content="1200">\n<meta property="og:image:height" content="630">\n'
        '<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:image" content="{OG_IMAGE}">\n'
        '<meta name="color-scheme" content="dark light">\n'
        '<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">\n'
        '<link rel="stylesheet" href="/assets/site.css">\n'
        f'<script type="application/ld+json">{jsonld({"@context": "https://schema.org", "@graph": graph})}</script>\n'
        "</head>\n<body>\n"
        f"{header(current)}"
        f'<main id="main">\n  <div class="wrap">\n{body}  </div>\n</main>\n'
        f"{footer()}"
        "</body>\n</html>\n"
    )


FIVE_QUESTIONS = [
    ("What was observed?", "the metric and its unit, the observed value against the expected baseline, and the window in UTC."),
    ("Where is the evidence?", "network, chain id and the token's contract; the block range; each measured pool with its protocol and decoder family; the swap count and the five largest transactions; the RPC providers and ingestion runs covering the window."),
    ("How was it computed?", "the method and its version, the baseline window and value (frozen when the anomaly starts), the parameters and the detector's facts."),
    ("How much can we trust it?", "hours expected against hours fully covered, every gap, the checks that passed, and the caveats and alternative explanations."),
    ("How can it be checked independently?", "the detection SQL with its SHA-256, and a recipe for any public JSON-RPC node that needs no access to our database."),
]


def five_questions_html() -> str:
    items = "".join(f"<li><b>{esc(q)}</b> {esc(a[0].upper() + a[1:])}</li>" for q, a in FIVE_QUESTIONS)
    return f'<ol class="q">{items}</ol>\n'


# ---------------------------------------------------------------- free 7-day report form

FREE_REPORT_JS = "/assets/free-report.js"


def free_report_form(entry: dict) -> str:
    """The "Get the free 7-day report" form (available EVM tokens only). Hidden until assets/free-report.js gets an answer from
    GET {APP}/api/public/free-reports/options (404 while the server's FREE_REPORTS_ENABLED is off), so the page never shows a
    form that cannot work. The consent text and the Turnstile site key come from that answer; the link is emailed, never shown."""
    sym = esc(display_symbol(entry))
    return (
        f'<section class="frq" id="free-report" hidden aria-labelledby="frq-h" data-api="{APP}" '
        f'data-chain-id="{int(entry["chain_id"])}" data-token="{esc(entry["address"].lower())}">\n'
        f'<h2 id="frq-h">Get the free 7-day report for {sym}</h2>\n'
        '<p class="intro">Coverage, verified swaps, pools and the top findings of the last 7 covered days, each with its Evidence '
        "Card and a public eth_getLogs recipe. We email you a private link; no call needed.</p>\n"
        '<form class="frq-form" novalidate>\n'
        '  <label><span>Work email</span><input type="email" name="email" required maxlength="200" autocomplete="email" inputmode="email"></label>\n'
        '  <label><span>Company <span class="opt-l">(optional)</span></span><input type="text" name="company" maxlength="120" autocomplete="organization"></label>\n'
        '  <label class="hp" aria-hidden="true">Website<input type="text" name="website" tabindex="-1" autocomplete="off"></label>\n'
        '  <label class="consent"><input type="checkbox" name="consent" required> <span class="frq-consent"></span></label>\n'
        '  <div class="frq-ts"></div>\n'
        '  <button class="btn primary" type="submit">Email me the free report</button>\n'
        '  <p class="frq-msg" role="status" aria-live="polite"></p>\n'
        "</form>\n"
        f'<script src="{FREE_REPORT_JS}" defer></script>\n'
        "</section>\n"
    )


# ---------------------------------------------------------------- per-token page

def status_text(entry: dict, catalog: dict) -> str:
    st = entry.get("status")
    if st == "available":
        return f"at least {catalog.get('min_history_hours', 72)} hours of verified history"
    if st == "collecting":
        since = f"since {fmt_dt(entry.get('history_from'))}" if entry.get("history_from") else "history not started yet"
        ready = f"; ready on about {fmt_day(entry['ready_on'])} (estimate)" if entry.get("ready_on") else ""
        return f"{since}{ready}"
    if st == "stale":
        return f"no fresh coverage for more than {catalog.get('stale_after_hours', 28)} hours; last covered {fmt_dt(entry.get('covered_until'))}"
    return esc(str(st))


def token_title(entry: dict) -> str:
    return f"{display_symbol(entry)} on {entry['network_name']}: on-chain DEX swaps and anomaly evidence | WATAM DeFi Intelligence"


def token_description(entry: dict) -> str:
    proof = entry.get("proof") or {}
    sym, net = display_symbol(entry), entry["network_name"]
    if not proof:
        return f"{sym} on {net} is in the WATAM DeFi Intelligence catalog and is being collected. Coverage and verified swap counts appear once history starts."
    parts = [f"{num(proof.get('verified_swaps'))} verified swaps across {num(proof.get('pools'))} pools on {net}"]
    if entry.get("history_from"):
        parts.append(f" since {fmt_day(entry['history_from'])}, covered until {fmt_dt(entry.get('covered_until'))}")
    text = "".join(parts) + "."
    if "findings" in proof:
        text += f" {num(proof['findings'])} anomaly findings, each with an Evidence Card."
    elif detection_note(entry):
        text += f" Anomaly detection is not yet available on {net}."
    return f"{sym}: {text}"


def facts_rows(entry: dict, catalog: dict) -> list[tuple[str, str]]:
    vm = vm_of(entry)
    proof = entry.get("proof") or {}
    net = esc(entry["network_name"])
    rows = [
        ("Network", f"{net} ({VM_LABEL.get(vm, vm.upper())}" + (f", chain id {entry['chain_id']})" if vm == "evm" else ")")),
        (ADDRESS_LABEL.get(vm, "Address"), f"<code>{esc(entry['address'])}</code>"),
    ]
    if entry.get("name"):
        rows.insert(0, ("Token", f"{esc(display_symbol(entry))} ({esc(entry['name'])})"))
    badge = STATUS_BADGE.get(entry.get("status"), "col")
    rows.append(("Status", f'<span class="badge {badge}">{esc(STATUS_LABEL.get(entry.get("status"), str(entry.get("status"))))}</span> {esc(status_text(entry, catalog))}'))
    if entry.get("history_from"):
        rows.append(("Coverage window", f"{esc(fmt_dt(entry['history_from']))} → {esc(fmt_dt(entry.get('covered_until')))}"))
        rows.append(("Hours of history", f'<span class="num">{num(entry.get("history_hours"))}</span>'))
    if proof:
        rows.append(("Verified swaps", f'<span class="num">{num(proof.get("verified_swaps"))}</span>'))
        rows.append(("Pools measured", f'<span class="num">{num(proof.get("pools"))}</span>'))
    if "findings" in proof:
        rows.append(("Anomaly findings", f'<span class="num">{num(proof["findings"])}</span>'))
    elif detection_note(entry):
        rows.append(("Anomaly findings", f"Not yet available on {net}"))
    elif proof:
        rows.append(("Anomaly findings", "None recorded"))
    lf = proof.get("latest_finding")
    if lf:
        rows.append(("Latest finding", f"{esc(lf.get('label') or lf.get('type') or '')} ({esc(fmt_day(lf.get('date')))})"))
    gaps = entry.get("gaps")
    if isinstance(gaps, dict):
        if not gaps.get("count"):
            rows.append(("Coverage gaps", "None recorded"))
        else:
            latest = gaps.get("latest") or {}
            txt = f"{num(gaps['count'])} gap(s), {num(gaps.get('hours'))} hour(s) in total"
            if latest:
                txt += f"; latest {esc(fmt_dt(latest.get('from')))} → {esc(fmt_dt(latest.get('to')))}"
            rows.append(("Coverage gaps", txt))
    if entry.get("scope") == "ingested_pools_only":
        rows.append(("Scope", "Swaps in the pools we ingest and decode; other venues are not counted"))
    return rows


def token_page(entry: dict, catalog: dict) -> str:
    sym = display_symbol(entry)
    net = entry["network_name"]
    url = SITE + page_path(entry)
    proof = entry.get("proof") or {}
    vm = vm_of(entry)
    is_index = indexable(entry, catalog)
    h1 = f"{esc(sym)} ({esc(entry['name'])}) on {esc(net)}" if entry.get("name") and entry.get("symbol") else f"{esc(sym)} on {esc(net)}"

    body = [
        f'<p class="crumbs"><a href="/">Home</a> / <a href="/tokens/">Tokens</a> / <a href="/tokens/#{esc(entry["network"])}">{esc(net)}</a> / {esc(sym)}</p>\n',
        f'<p class="kicker">Token coverage · {esc(net)}</p>\n<h1>{h1}</h1>\n',
        f'<p class="asof">Data as of {esc(fmt_dt(catalog["generated_at"]))} · refreshed every {refresh_hours(entry, catalog)} h · source: <a href="{CATALOG_URL}">public catalog</a></p>\n',
        f'<p class="intro">{esc(token_description(entry))}</p>\n',
        '<h2 id="facts">Facts</h2>\n<dl class="facts">\n',
    ]
    body += [f"  <dt>{k}</dt><dd>{v}</dd>\n" for k, v in facts_rows(entry, catalog)]
    body.append("</dl>\n")
    note = detection_note(entry)
    if note:
        body.append(f'<p class="note">{esc(note)}</p>\n')
    if entry.get("status") == "collecting":
        body.append(f'<p class="note">This token is still collecting history. Its page is not indexed until it has {catalog.get("min_history_hours", 72)} hours of verified coverage.</p>\n')
    body.append(
        '<p class="fine">Verified swaps are swaps decoded directly from the chain in pools that passed verification; '
        "findings describe the verified pools that were measured, never a token's whole market, and none is an accusation.</p>\n"
    )

    body.append(f'<h2 id="evidence">What an Evidence Card for {esc(sym)} contains</h2>\n')
    if vm == "evm":
        body.append('<p class="intro">Every anomaly finding comes with an Evidence Card that answers five questions, built only from stored data. A value that cannot be determined is shown as unknown, with the reason.</p>\n')
        body.append(five_questions_html())
        body.append('<h3>Check it on a public node</h3>\n<p class="intro">The reproduction recipe re-reads the same swap logs from any public JSON-RPC endpoint:</p>\n')
        body.append(
            "<pre>// eth_getLogs over the measured pools, one Swap topic per decoder family\n"
            '{"method": "eth_getLogs", "params": [{\n'
            '  "address":   ["&lt;measured pools&gt;"],\n'
            '  "topics":    ["&lt;Swap event topic&gt;"],\n'
            '  "fromBlock": "&lt;first block, hex&gt;",\n'
            '  "toBlock":   "&lt;last block, hex&gt;"\n'
            "}]}</pre>\n"
        )
        body.append('<p class="fine">The pool list, the exact block range and the expected swap count for each finding are in the application.</p>\n')
    else:
        body.append('<p class="intro">Evidence Cards cover anomaly findings on the EVM networks; anomaly detection is not yet available on this network, so the catalog reports coverage, verified swaps and pools only. For reference, an Evidence Card answers five questions:</p>\n')
        body.append(five_questions_html())

    if is_index and vm == "evm":
        body.append(free_report_form(entry))
    body.append('<h2 id="next">Get the evidence</h2>\n<div class="ctas">\n')
    mail = f"mailto:{EMAIL}?subject={urllib.parse.quote(f'{sym} on {net}')}"
    if is_index and vm == "evm":
        body.append(f'  <a class="btn primary" href="{APP}/checkout?plan=mm_scorecard_monthly">Buy Token for {esc(sym)} (USD 490 per month)</a>\n')
        body.append(f'  <a class="btn" href="{mail}">Free token check by email</a>\n')
    else:
        body.append(f'  <a class="btn primary" href="{mail}">Ask about this token</a>\n')
    body.append(f'  <a class="btn" href="{APP}/catalog">Open the catalog in the app</a>\n</div>\n')
    body.append(f'<p class="fine">Prices and plans: <a href="{PRICING_URL}">pricing</a>. USD values are not part of the evidence.</p>\n')

    crumbs = {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{SITE}/"},
            {"@type": "ListItem", "position": 2, "name": "Tokens", "item": f"{SITE}/tokens/"},
            {"@type": "ListItem", "position": 3, "name": f"{sym} on {net}"},
        ],
    }
    webpage = {
        "@type": "WebPage",
        "@id": url,
        "url": url,
        "name": f"{sym} on {net}: on-chain DEX swaps and anomaly evidence",
        "description": token_description(entry),
        "dateModified": iso_z(catalog["generated_at"]),
        "isPartOf": {"@type": "WebSite", "@id": f"{SITE}/#website", "url": f"{SITE}/", "name": "WATAM DeFi Intelligence"},
        "publisher": {"@id": ORG["@id"]},
        "breadcrumb": crumbs,
    }
    graph = [ORG, webpage]
    if proof and entry.get("history_from"):
        desc = (
            f"DEX swap coverage for {sym} on {net} collected directly from the chain: {num(proof.get('verified_swaps'))} verified swaps "
            f"across {num(proof.get('pools'))} pools from {fmt_dt(entry['history_from'])} to {fmt_dt(entry.get('covered_until'))}"
        )
        if "findings" in proof:
            desc += f", with {num(proof['findings'])} anomaly findings, each backed by an Evidence Card (observation, evidence, method, trust, reproduction)."
        else:
            desc += "."
        dataset = {
            "@type": "Dataset",
            "name": f"{sym} ({net}) verified DEX swap coverage",
            "description": desc,
            "url": url,
            "temporalCoverage": f"{iso_z(entry['history_from'])}/{iso_z(entry.get('covered_until'))}",
            "variableMeasured": ["verified swaps", "pools"] + (["anomaly findings"] if "findings" in proof else []),
            "creator": {"@id": ORG["@id"]},
            "distribution": {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": CATALOG_URL},
        }
        graph.append(dataset)
    return document(
        title=token_title(entry),
        description=token_description(entry),
        canonical=url,
        robots=None if is_index else "noindex",
        graph=graph,
        body="".join(body),
        current="tokens",
    )


# ---------------------------------------------------------------- list page

def network_order(catalog: dict) -> list[dict]:
    return list(catalog.get("networks") or [])


def sort_key(entry: dict):
    order = {"available": 0, "collecting": 1, "stale": 2}
    return (order.get(entry.get("status"), 9), (entry.get("symbol") or "~").lower(), entry["address"].lower())


def index_page(catalog: dict, entries: list[dict]) -> str:
    counts = {s: sum(1 for e in entries if e.get("status") == s) for s in ("available", "collecting", "stale")}
    nets = [n for n in network_order(catalog) if any(e["network"] == n["key"] for e in entries)]
    summary = (
        f"{len(entries)} tokens on {len(nets)} networks: {counts['available']} available, "
        f"{counts['collecting']} collecting, {counts['stale']} stale."
    )
    body = [
        '<p class="crumbs"><a href="/">Home</a> / Tokens</p>\n',
        '<p class="kicker">Token catalog</p>\n<h1>Tracked tokens: on-chain DEX swap coverage and anomaly evidence</h1>\n',
        f'<p class="asof">Data as of {esc(fmt_dt(catalog["generated_at"]))} · EVM refreshed every {catalog.get("refresh_every_hours", 12)} h, '
        f'TON every {catalog.get("ton_refresh_every_hours", 3)} h, Solana every {catalog.get("solana_refresh_every_hours", 1)} h · '
        f'machine-readable: <a href="{CATALOG_URL}">public catalog API (JSON)</a></p>\n',
        f'<p class="intro">{esc(summary)} Swaps are decoded directly from the chain; a token is <b>available</b> once it has at least '
        f'{catalog.get("min_history_hours", 72)} hours of verified history, <b>collecting</b> before that, and <b>stale</b> when coverage has not been refreshed '
        f'for more than {catalog.get("stale_after_hours", 28)} hours. Every anomaly finding on the EVM networks carries an Evidence Card.</p>\n',
        '<ul class="netnav" aria-label="Networks">' + "".join(f'<li><a href="#{esc(n["key"])}">{esc(n["name"])}</a></li>' for n in nets) + "</ul>\n",
    ]
    for n in nets:
        rows = sorted((e for e in entries if e["network"] == n["key"]), key=sort_key)
        fam = VM_LABEL.get(n.get("vm") or "evm", "EVM")
        sub = f"{fam}, chain id {n['chain_id']}" if fam == "EVM" else fam
        body.append(f'<h2 id="{esc(n["key"])}">{esc(n["name"])} <span class="h-sub">{esc(sub)} · {len(rows)} tokens</span></h2>\n')
        body.append(
            '<div class="tbl"><table>\n<thead><tr><th scope="col">Token</th><th scope="col">Status</th>'
            '<th scope="col" class="n">Verified swaps</th><th scope="col" class="n opt">Pools</th>'
            '<th scope="col" class="n opt">Findings</th><th scope="col" class="opt">History from</th></tr></thead>\n<tbody>\n'
        )
        for e in rows:
            proof = e.get("proof") or {}
            st = e.get("status")
            findings = num(proof["findings"]) if "findings" in proof else ("n/a" if detection_note(e) else "–")
            body.append(
                f'<tr><td><a href="{page_path(e)}">{esc(display_symbol(e))}</a><span class="sub">{esc(e.get("name") or short_addr(e["address"]))}</span></td>'
                f'<td><span class="badge {STATUS_BADGE.get(st, "col")}">{esc(STATUS_LABEL.get(st, str(st)))}</span></td>'
                f'<td class="n">{num(proof.get("verified_swaps")) if proof else "–"}</td>'
                f'<td class="n opt">{num(proof.get("pools")) if proof else "–"}</td>'
                f'<td class="n opt">{findings}</td>'
                f'<td class="opt">{esc(fmt_day(e.get("history_from")))}</td></tr>\n'
            )
        body.append("</tbody></table></div>\n")
    body.append(
        '<p class="fine">Findings describe the verified pools that were measured, never a token\'s whole market, and none is an accusation. '
        'n/a: anomaly detection is not yet available on Solana and TON. Token names identify the contracts we decode; they do not imply a partnership or endorsement.</p>\n'
    )
    body.append(
        '<div class="ctas">\n'
        f'  <a class="btn primary" href="{APP}/checkout?plan=mm_scorecard_monthly">Buy Token (USD 490 per month)</a>\n'
        f'  <a class="btn" href="mailto:{EMAIL}?subject=Token%20coverage">Ask about a token not listed</a>\n'
        "</div>\n"
    )

    items = [
        {"@type": "ListItem", "position": i + 1, "url": SITE + page_path(e), "name": f"{display_symbol(e)} on {e['network_name']}"}
        for i, e in enumerate(e for n in nets for e in sorted((x for x in entries if x["network"] == n["key"]), key=sort_key) if indexable(e, catalog))
    ]
    graph = [
        ORG,
        {
            "@type": "CollectionPage",
            "@id": f"{SITE}/tokens/",
            "url": f"{SITE}/tokens/",
            "name": "Tracked tokens: on-chain DEX swap coverage and anomaly evidence",
            "description": summary,
            "dateModified": iso_z(catalog["generated_at"]),
            "publisher": {"@id": ORG["@id"]},
            "breadcrumb": {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{SITE}/"},
                    {"@type": "ListItem", "position": 2, "name": "Tokens"},
                ],
            },
            "mainEntity": {"@type": "ItemList", "numberOfItems": len(items), "itemListElement": items},
        },
        {
            "@type": "Dataset",
            "name": "WATAM DeFi Intelligence token catalog",
            "description": f"Tracked tokens with on-chain DEX swap coverage: {summary} Each entry lists network, contract address, status, coverage window, verified swaps, pools and anomaly findings.",
            "url": f"{SITE}/tokens/",
            "dateModified": iso_z(catalog["generated_at"]),
            "variableMeasured": ["status", "coverage window", "verified swaps", "pools", "anomaly findings"],
            "creator": {"@id": ORG["@id"]},
            "distribution": {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": CATALOG_URL},
        },
    ]
    return document(
        title="Token catalog: on-chain DEX swap coverage and anomaly evidence | WATAM DeFi Intelligence",
        description=f"{summary} Verified swaps, pools, coverage windows and anomaly findings per token, decoded directly from the chain.",
        canonical=f"{SITE}/tokens/",
        robots=None,
        graph=graph,
        body="".join(body),
        current="tokens",
    )


# ---------------------------------------------------------------- sitemap, llms-full

def git_lastmod(path: Path) -> str | None:
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cI", "--", path.name], cwd=path.parent,
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return iso_z(out) if out else None


def sitemap(catalog: dict, entries: list[dict], out: Path) -> str:
    urls = []
    home_mod = git_lastmod(out / "index.html")
    urls.append((f"{SITE}/", home_mod))
    urls.append((f"{SITE}/tokens/", iso_z(catalog["generated_at"])))
    for url in STATIC_PAGES:
        urls.append((url, git_lastmod(out / url[len(SITE):].strip("/") / "index.html")))
    for e in sorted(entries, key=lambda x: page_path(x)):
        if indexable(e, catalog):
            urls.append((SITE + page_path(e), iso_z(e.get("covered_until")) or iso_z(catalog["generated_at"])))
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, mod in urls:
        lines.append(f"  <url><loc>{esc(loc)}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def llms_full(catalog: dict, entries: list[dict]) -> str:
    nets = ", ".join(n["name"] for n in network_order(catalog))
    out = [
        "# WATAM DeFi Intelligence (full)",
        "",
        "> On-chain DEX analytics for token teams, exchange listing and risk teams and funds. Swaps are decoded directly from the",
        f"> chain on {nets}. Every anomaly finding carries an Evidence Card that answers five questions. {INDEPENDENCE}",
        "",
        f"Data as of {fmt_dt(catalog['generated_at'])}. Source: {CATALOG_URL} (refreshed every {catalog.get('refresh_every_hours', 12)} h on EVM networks,"
        f" every {catalog.get('ton_refresh_every_hours', 3)} h on TON, every {catalog.get('solana_refresh_every_hours', 1)} h on Solana).",
        "",
        "## The five evidence questions",
        "",
    ]
    out += [f"{i}. {q} {a[0].upper()}{a[1:]}" for i, (q, a) in enumerate(FIVE_QUESTIONS, 1)]
    out += [
        "",
        "Evidence Cards cover anomaly findings on the EVM networks; Solana and TON entries have coverage and verified swaps but no anomaly",
        "findings yet. Findings describe the verified pools that were measured, never a token's whole market, and none is an accusation.",
        "USD values are not part of the evidence.",
        "",
        "## Catalog status rules",
        "",
        f"- available: at least {catalog.get('min_history_hours', 72)} hours of verified history",
        "- collecting: history is still being gathered; the ready date is an estimate",
        f"- stale: coverage not refreshed for more than {catalog.get('stale_after_hours', 28)} hours",
        "",
        "## Available tokens",
        "",
    ]
    for n in network_order(catalog):
        rows = [e for e in sorted(entries, key=sort_key) if e["network"] == n["key"] and indexable(e, catalog)]
        if not rows:
            continue
        out.append(f"### {n['name']}")
        out.append("")
        for e in rows:
            p = e.get("proof") or {}
            name = plain(e.get("name"))
            line = (f"- [{plain(display_symbol(e))}{f' ({name})' if name else ''} on {n['name']}]({SITE}{page_path(e)}): {num(p.get('verified_swaps'))} verified swaps, "
                    f"{num(p.get('pools'))} pools, history from {fmt_day(e.get('history_from'))}")
            if "findings" in p:
                line += f", {num(p['findings'])} anomaly findings"
            out.append(line)
        out.append("")
    out += [
        "## Links",
        "",
        f"- [Overview]({SITE}/)",
        f"- [Token catalog]({SITE}/tokens/)",
        f"- [Exit Check]({SITE}/exit-check/): free tool; value a wallet displays vs. what live aggregator sell quotes would pay out (KyberSwap, Velora, Jupiter, Raydium), shortfall, route and verified DEX exit depth for catalog tokens; quotes are indicative, not investment advice",
        f"- [Pricing]({PRICING_URL}): Token USD 490, Pro USD 990, Data USD 2,500, Enterprise from USD 5,000 per month; Listing Liquidity Dossier USD 750 per token, one-off",
        f"- [Buy online]({APP}/checkout): stablecoin or bank transfer",
        f"- [Become a partner]({APP}/partners)",
        f"- Contact: {EMAIL}",
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------- commands

# ---------------------------------------------------------------- homepage "Market Maker Scorecard" venue lists

SCORECARD_BEGIN = "<!-- scorecard:begin"
SCORECARD_END = "<!-- scorecard:end -->"
_VENUE = re.compile(r"^[A-Za-z0-9 .&+-]{2,24}$")


def scorecard_block(cov: dict) -> str | None:
    """The three venue lists, or None when the answer does not look right (then the page keeps what it has)."""
    if not isinstance(cov, dict):
        return None
    lists = {k: [v for v in (cov.get(k) if isinstance(cov.get(k), list) else []) if isinstance(v, str) and _VENUE.fullmatch(v)]
             for k in ("exchanges", "perpetuals", "liquidation_streams")}
    if len(lists["exchanges"]) < 5 or not lists["perpetuals"]:
        return None
    rows = [("Exchanges, one by one", lists["exchanges"]), ("Perpetual futures", lists["perpetuals"]),
            ("Liquidation streams", lists["liquidation_streams"])]
    out = ['      <dl class="sc-venues">']
    for title, names in rows:
        if names:
            out.append(f"        <div><dt>{esc(title)} <span>{len(names)}</span></dt><dd>{esc(', '.join(names))}</dd></div>")
    out.append("      </dl>")
    return "\n".join(out) + "\n"


def update_scorecard_section(out: Path, url: str = COVERAGE_URL) -> bool:
    """Rewrite only what lies between the scorecard markers in index.html from the public coverage route. False (file untouched)
    when the markers, the route or a sane answer are missing."""
    path = out / "index.html"
    text = path.read_text(encoding="utf-8")
    start, end = text.find(SCORECARD_BEGIN), text.find(SCORECARD_END)
    if start < 0 or end < start:
        print("::warning::index.html: scorecard markers not found; section left as it is", file=sys.stderr)
        return False
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "watam-site-builder/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https URL)
            block = scorecard_block(json.loads(resp.read().decode("utf-8")))
    except Exception as exc:  # noqa: BLE001 — a failing route must never fail the token-pages build
        print(f"::warning::scorecard coverage unavailable ({type(exc).__name__}); section left as it is", file=sys.stderr)
        return False
    if block is None:
        print("::warning::scorecard coverage answer not usable; section left as it is", file=sys.stderr)
        return False
    line_end = text.index("\n", start) + 1
    end_line = text.rfind("\n", 0, end) + 1                 # keep the end marker's own indentation
    new = text[:line_end] + block + text[end_line:]
    if new != text:
        path.write_text(new, encoding="utf-8")
    return True


# ---------------------------------------------------------------- homepage "networks" section

NETWORKS_BEGIN = "<!-- networks:begin"
NETWORKS_END = "<!-- networks:end -->"
RECENT_DAYS = 7
FIRST_ADDED_FILE = "data/catalog-first-added.json"   # network:address → date the token first appeared in the catalog
FIRST_ADDED: dict[str, str] = {}
HERO_IDS = {"eth": "ethereum", "arb": "arbitrum", "op": "optimism", "base": "base", "bnb": "bsc", "avax": "avalanche",
            "monad": "monad", "sol": "solana", "ton": "ton"}
RECENT_MAX = 6
_HEX = '<path class="mk-shape" d="M22 3l16.5 9.5v19L22 41 5.5 31.5v-19z" stroke="var(--blue)"{dash}/>'
_SQUARE = '<rect class="mk-shape" x="5" y="5" width="34" height="34" rx="9" stroke="var(--brass)"{dash}/>'
_DIAMOND = '<path class="mk-shape" d="M22 4l18 18-18 18L4 22z" stroke="var(--brass)"{dash}/>'
# Fixed order and wording. Venues: only what the backend decodes (backend/app/chain/protocols.py recorded factories and
# PROTOCOL_FAMILIES; backend/app/solana/programs.py SUPPORTED; config/ton_dex.toml), checked 3 Nov 2026. A trailing "*" marks
# a venue verified pool by pool. A network missing from the live catalog keeps the old section (see update_networks_section).
NETWORK_CARDS = [
    dict(key="ethereum", tag="ETH", shape=_HEX, name="Ethereum", fam="EVM, chain id 1",
         venues=["Uniswap v2", "Uniswap v3", "PancakeSwap v3", "Uniswap v4, per pool*"],
         note="History depth is shown on each token page."),
    dict(key="arbitrum", tag="ARB", shape=_HEX, name="Arbitrum One", fam="EVM, chain id 42161",
         venues=["Uniswap v3", "SushiSwap v2", "Uniswap v4, per pool*"],
         note="History depth is shown on each token page."),
    dict(key="optimism", tag="OP", shape=_HEX, name="OP Mainnet", fam="EVM, chain id 10",
         venues=["Uniswap v3", "Velodrome v2", "Velodrome Slipstream", "Uniswap v4, per pool*"],
         note="History depth is shown on each token page."),
    dict(key="base", tag="BASE", shape=_HEX, name="Base", fam="EVM, chain id 8453",
         venues=["Uniswap v3", "Aerodrome v2", "Aerodrome Slipstream", "MetaDEX Slipstream", "PancakeSwap v3", "Uniswap v4, per pool*"],
         note="History depth is shown on each token page."),
    dict(key="bsc", tag="BNB", shape=_HEX, name="BNB Smart Chain", fam="EVM, chain id 56",
         venues=["PancakeSwap v2", "PancakeSwap v3", "Uniswap v3", "PancakeSwap Infinity, per pool (verified hooked pools included)*",
                 "Uniswap v4, per pool*"],
         note="Collected forward only: windows starting more than 8 days back are refused."),
    dict(key="avalanche", tag="AVAX", shape=_HEX, name="Avalanche C&#8209;Chain", fam="EVM, chain id 43114",
         venues=["LFJ v1", "Pangolin v2", "Uniswap v3", "Pharaoh v3", "Uniswap v4, per pool*"]),
    dict(key="monad", tag="MON", shape=_HEX, name="Monad", fam="EVM, chain id 143",
         venues=["Uniswap v2", "Uniswap v3", "PancakeSwap v3", "Uniswap v4, per pool*"]),
    dict(key="solana", tag="SOL", shape=_SQUARE, name="Solana", fam="SVM, refreshed hourly",
         venues=["Raydium CPMM", "Raydium CLMM", "Orca Whirlpool", "Meteora DLMM", "Meteora DAMM v2", "Meteora dynamic AMM (v1)",
                 "PumpSwap"],
         desc="Swaps reconciled exactly against the pools' token balances. Pinned pools only; no anomaly findings yet."),
    dict(key="ton", tag="TON", shape=_DIAMOND, name="TON", fam="TVM, refreshed every 3 hours",
         venues=["STON.fi v1", "STON.fi v2", "DeDust (volatile pools)"],
         desc="Two independent providers must agree on every window. Pinned pools only; no anomaly findings yet."),
]


def _net_card(card: dict, rows: list[dict], now: datetime) -> str:
    avail = [e for e in rows if e.get("status") == "available"]
    status = "available" if avail else "collecting"
    dash = "" if avail else ' stroke-dasharray="4 3"'
    parts = [f"{len(rows)} token{'s' if len(rows) != 1 else ''}", f"{len(avail)} available"]
    for st, label in (("collecting", "collecting"), ("on_hold", "on hold"), ("stale", "stale")):
        n = sum(1 for e in rows if e.get("status") == st)
        if n:
            parts.append(f"{n} {label}")
    cutoff = now - timedelta(days=RECENT_DAYS)
    def added(e):     # the date the token first appeared in the catalog (not its history start: back-fills look new)
        d = FIRST_ADDED.get(f'{e["network"]}:{e["address"]}')
        return datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc) if d else None
    recent = sorted((e for e in rows if (added(e) or cutoff) > cutoff), key=lambda e: (added(e), e["address"]), reverse=True)
    when = []
    if recent:
        links = ", ".join(f'<a href="{page_path(e)}">{html.escape(display_symbol(e))}</a>' for e in recent[:RECENT_MAX])
        more = f" and {len(recent) - RECENT_MAX} more" if len(recent) > RECENT_MAX else ""
        when.append(f'<p class="recent">Added in the last {RECENT_DAYS} days: {links}{more}.</p>')
    firsts = [t for t in (parse_ts(e.get("history_from")) for e in rows) if t]
    if card.get("note"):
        line = card["note"]
    elif not avail and firsts:
        line = f"Collecting since {min(firsts).strftime('%d %b %Y')}; available after the verification period."
    elif not avail:
        line = "Available after the verification period."
    else:
        line = None
    venues = "".join(f'<li class="per">{html.escape(v[:-1])}</li>' if v.endswith("*") else f"<li>{html.escape(v)}</li>"
                     for v in card["venues"])
    return (
        f'        <li class="net{"" if avail else " collecting"}" data-s="{status}">\n'
        f'          <div class="net-h">\n'
        f'            <svg viewBox="0 0 44 44" aria-hidden="true">{card["shape"].format(dash=dash)}'
        f'<text class="mk-txt" x="22" y="26" text-anchor="middle" fill="var(--ink)">{card["tag"]}</text></svg>\n'
        f'            <div class="nm"><h3>{card["name"]}</h3><span class="fam">{card["fam"]}</span></div>\n'
        f'            <span class="badge {"ok" if avail else "col"}">{"Available" if avail else "Collecting"}</span>\n'
        f'          </div>\n'
        f'          <ul class="venues" aria-label="Venues">{venues}</ul>\n'
        f'          <p class="count">{" · ".join(parts)}</p>\n'
        + (f'          <p>{card["desc"]}</p>\n' if card.get("desc") else "")
        + "".join(f"          {w}\n" for w in when)
        + (f'          <p class="when">{line}</p>\n' if line else "")
        + "        </li>\n")


def networks_section(catalog: dict, entries: list[dict]) -> str | None:
    """The cards + footer line between the markers; None (keep the old section) if any network has no entries."""
    now = parse_ts(catalog["generated_at"])
    by_net: dict[str, list[dict]] = {}
    for e in entries:
        by_net.setdefault(e["network"], []).append(e)
    if any(not by_net.get(c["key"]) for c in NETWORK_CARDS):
        return None
    for extra in sorted(set(by_net) - {c["key"] for c in NETWORK_CARDS}):
        print(f"network {extra!r} is in the catalog but not in NETWORK_CARDS; add it to the homepage map", file=sys.stderr)
    if not FIRST_ADDED:
        print("first-added dates not loaded; no 'added' lines", file=sys.stderr)
    cards = "".join(_net_card(c, by_net[c["key"]], now) for c in NETWORK_CARDS)
    return (f'      <ul class="nets">\n{cards}      </ul>\n'
            f'      <p class="fine">Status of the catalog on {now.strftime("%d %b %Y")}; each token\'s live status, history length and proof '
            f'counts are in the <a href="https://app.wearetheartmakers.com/catalog">token catalog</a>. Dashed venues are verified pool by '
            f'pool: only pools with an accepted verification record are counted. Venue and network names identify the contracts we '
            f'decode; they do not imply a partnership or endorsement.</p>\n')


def update_networks_section(out: Path, catalog: dict, entries: list[dict]) -> bool:
    """Rewrite only what lies between the markers in index.html. False (file untouched) if markers or data are missing."""
    path = out / "index.html"
    text = path.read_text(encoding="utf-8")
    start = text.find(NETWORKS_BEGIN)
    end = text.find(NETWORKS_END)
    if start < 0 or end < start:
        print("::warning::index.html: networks markers not found; homepage section left as it is", file=sys.stderr)
        return False
    body = networks_section(catalog, entries)
    if body is None:
        print("::warning::catalog lacks entries for a homepage network; homepage section left as it is", file=sys.stderr)
        return False
    line_end = text.index("\n", start) + 1
    text = text[:line_end] + body + "      " + text[end:]
    collecting = {c["key"] for c in NETWORK_CARDS
                  if not any(e.get("status") == "available" for e in entries if e["network"] == c["key"])}
    for hid, key in HERO_IDS.items():   # the hero diagram draws collecting networks with a dashed ring
        text = re.sub(r'(\{ id:"%s", [^}\n]*?kind:"chain", x:[.0-9]+, y:[.0-9]+)(, collecting:true)?( \})' % hid,
                      lambda m: m.group(1) + (", collecting:true" if key in collecting else "") + m.group(3), text)
    path.write_text(text, encoding="utf-8")
    return True


def load_first_added(out: Path, entries: list[dict], now) -> None:
    """Fill FIRST_ADDED from the committed file; a token not in it yet is new today (the file is committed by the workflow).
    Seeded on 2026-11-03 from the first commit of each address in the core repo's config/ (git log -S)."""
    path = out / FIRST_ADDED_FILE
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    changed = False
    for e in entries:
        k = f'{e["network"]}:{e["address"]}'
        if not data.get(k):
            data[k] = now.strftime("%Y-%m-%d"); changed = True
    FIRST_ADDED.clear(); FIRST_ADDED.update(data)
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def build(args) -> int:
    if args.catalog_file:
        catalog = json.loads(Path(args.catalog_file).read_text(encoding="utf-8"))
    else:
        catalog = fetch_catalog(CATALOG_URL)
    entries = [e for e in catalog.get("entries") or [] if e.get("address") and e.get("network")]
    for e in [e for e in entries if not valid_entry(e)]:
        print(f"skipping an entry with an unexpected network / address: {e.get('network')!r} {e.get('address')!r}", file=sys.stderr)
    entries = [e for e in entries if valid_entry(e)]
    if catalog.get("status_source") != "ok" or len(entries) < 10 or not catalog.get("generated_at"):
        print(f"refusing to build: status_source={catalog.get('status_source')!r}, entries={len(entries)}", file=sys.stderr)
        return 2
    out = Path(args.out).resolve()
    built = len(list((out / "tokens").glob("*/*/index.html")))
    if built and len(entries) < MAX_SHRINK * built:
        print(f"refusing to build: {len(entries)} entries against {built} pages already built (a partial catalog?)", file=sys.stderr)
        return 2
    seen: dict[str, dict] = {}
    for e in entries:
        key = page_path(e).lower()
        if key in seen:
            print(f"duplicate page path {page_path(e)}", file=sys.stderr)
            return 2
        seen[key] = e

    tokens_dir = out / "tokens"
    if tokens_dir.exists():
        shutil.rmtree(tokens_dir)  # fully generated; removed entries must disappear
    for e in entries:
        target = out / page_path(e).strip("/") / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(token_page(e, catalog), encoding="utf-8")
    (tokens_dir / "index.html").write_text(index_page(catalog, entries), encoding="utf-8")
    (out / "sitemap.xml").write_text(sitemap(catalog, entries, out), encoding="utf-8")
    (out / "llms-full.txt").write_text(llms_full(catalog, entries), encoding="utf-8")
    load_first_added(out, entries, parse_ts(catalog["generated_at"]))
    update_networks_section(out, catalog, entries)
    update_scorecard_section(out)
    n_index = sum(1 for e in entries if indexable(e, catalog))
    print(f"wrote {len(entries)} token pages ({n_index} indexable) + tokens/index.html, sitemap.xml, llms-full.txt")
    return 0


def indexnow(args) -> int:
    """Ping IndexNow with the URLs of changed, indexable pages. Changed file paths come on stdin."""
    sitemap_text = (Path(args.out) / "sitemap.xml").read_text(encoding="utf-8")
    urls = []
    for line in sys.stdin:
        p = line.strip()
        if not p.endswith("index.html"):
            continue
        url = f"{SITE}/" + p[: -len("index.html")]
        if f"<loc>{esc(url)}</loc>" in sitemap_text and url not in urls:
            urls.append(url)
    if not urls:
        print("indexnow: nothing to submit")
        return 0
    payload = json.dumps({
        "host": SITE.split("//", 1)[1],
        "key": args.key,
        "keyLocation": f"{SITE}/{args.key}.txt",
        "urlList": urls[:10000],
    }).encode()
    req = urllib.request.Request("https://api.indexnow.org/indexnow", data=payload, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            print(f"indexnow: HTTP {resp.status} for {len(urls)} URLs")
    except urllib.error.HTTPError as exc:  # 202 is success; 4xx is reported but does not fail the job
        print(f"indexnow: HTTP {exc.code} {exc.reason}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="build", choices=["build", "indexnow"])
    ap.add_argument("--catalog-file", help="read the catalog from a local JSON file instead of the API")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent), help="site root (default: repo root)")
    ap.add_argument("--key", default=os.environ.get("INDEXNOW_KEY"), help="IndexNow key (indexnow command)")
    args = ap.parse_args()
    if args.command == "indexnow":
        if not args.key:
            print("indexnow: --key or INDEXNOW_KEY required", file=sys.stderr)
            return 2
        return indexnow(args)
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
