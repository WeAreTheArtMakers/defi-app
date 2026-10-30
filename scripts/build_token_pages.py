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
from datetime import datetime, timezone
from pathlib import Path

SITE = "https://defi.wearetheartmakers.com"
APP = "https://app.wearetheartmakers.com"
CATALOG_URL = f"{APP}/api/public/catalog"
PRICING_URL = f"{SITE}/pricing/"
METHOD_URL = f"{SITE}/method/"
OG_IMAGE = f"{SITE}/assets/og-card.png"
TERMS_URL = f"{SITE}/terms/"
PRIVACY_URL = f"{SITE}/privacy/"
LEGAL_URL = f"{SITE}/legal/"
STATIC_PAGES = (METHOD_URL, PRICING_URL, TERMS_URL, PRIVACY_URL, LEGAL_URL)  # hand-written pages listed in sitemap.xml
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

def header(current: str) -> str:
    def link(href: str, label: str, key: str, cls: str = "") -> str:
        cur = ' aria-current="page"' if key == current else ""
        c = f' class="{cls}"' if cls else ""
        return f'<a{c} href="{href}"{cur}>{label}</a>'

    return (
        '<a class="skip" href="#main">Skip to content</a>\n'
        '<header class="bar">\n  <div class="wrap">\n'
        '    <a class="mark" href="/"><i aria-hidden="true"></i>WATAM DeFi Intelligence</a>\n'
        '    <nav class="links" aria-label="Main">\n'
        f'      {link("/tokens/", "Tokens", "tokens")}\n'
        f'      {link("/method/", "Method", "method", "opt")}\n'
        f'      {link(PRICING_URL, "Pricing", "pricing")}\n'
        f'      {link(APP + "/partners", "Partners", "partners", "opt")}\n'
        f'      <a class="btn primary" href="/app/">Open the app</a>\n'
        "    </nav>\n  </div>\n</header>\n"
    )


def footer() -> str:
    return (
        "<footer>\n  <div class=\"wrap\">\n"
        f"    <p>{esc(INDEPENDENCE)} Token and network names are trademarks of their owners. USD values are not part of the evidence.<br>"
        "DeFi Intelligence is a product of WATAM Intelligence.<br>© 2026 WeAreTheArtMakers</p>\n"
        '    <nav aria-label="Footer">\n'
        '      <a href="/">Home</a>\n'
        '      <a href="/tokens/">Tokens</a>\n'
        f'      <a href="/method/">Method</a>\n'
        f'      <a href="{PRICING_URL}">Pricing</a>\n'
        f'      <a href="{APP}/partners">Become a partner</a>\n'
        f'      <a href="{TERMS_URL}">Terms</a>\n'
        f'      <a href="{PRIVACY_URL}">Privacy</a>\n'
        f'      <a href="{LEGAL_URL}">Legal</a>\n'
        f'      <a href="mailto:{EMAIL}">Contact</a>\n'
        "    </nav>\n  </div>\n</footer>\n"
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
        body.append('<p class="fine">The pool list, the exact block range and the expected swap count for each finding are in the Evidence Snapshot and in the application.</p>\n')
    else:
        body.append('<p class="intro">Evidence Cards cover anomaly findings on the EVM networks; anomaly detection is not yet available on this network, so the catalog reports coverage, verified swaps and pools only. For reference, an Evidence Card answers five questions:</p>\n')
        body.append(five_questions_html())

    body.append('<h2 id="next">Get the evidence</h2>\n<div class="ctas">\n')
    mail = f"mailto:{EMAIL}?subject={urllib.parse.quote(f'{sym} on {net}')}"
    if is_index and vm == "evm":
        body.append(f'  <a class="btn primary" href="{APP}/checkout">Get the Evidence Snapshot for {esc(sym)} ($900, 7 days)</a>\n')
        body.append(f'  <a class="btn" href="{mail}">Ask about a pilot</a>\n')
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
        f'  <a class="btn primary" href="{APP}/checkout">Buy an Evidence Snapshot ($900, 7 days)</a>\n'
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
        f"- [Pricing]({PRICING_URL}): Evidence Snapshot $900 (7 days, one token); subscriptions from $1,500/month",
        f"- [Buy online]({APP}/checkout): stablecoin or bank transfer",
        f"- [Become a partner]({APP}/partners)",
        f"- Contact: {EMAIL}",
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------- commands

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
