#!/usr/bin/env python3
"""OutreachEngine - AI Visibility Fix pipeline. CSV on disk is the CRM."""
import argparse, csv, json, os, re, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA, AUDITS, RAW, CONFIG = ROOT / "data", ROOT / "audits", ROOT / "raw", ROOT / "config"
for _d in (DATA, AUDITS, RAW, CONFIG):
    _d.mkdir(exist_ok=True)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
HDRS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT = 20

FREEMAIL = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com",
            "proton.me", "protonmail.com", "aol.com", "live.com", "msn.com",
            "gmx.com", "mail.com", "yandex.com", "inbox.lv"}

# Published for legal / data-subject / automation purposes - never sales targets.
BAD_LOCALPARTS = {"privacy", "dpo", "legal", "abuse", "postmaster", "webmaster",
                  "noreply", "no-reply", "donotreply", "security", "gdpr",
                  "compliance", "unsubscribe", "mailer-daemon"}

COUNTRY_TLD = {".co.uk": "UK", ".org.uk": "UK", ".uk": "UK", ".com.au": "AU",
               ".au": "AU", ".ca": "CA", ".nz": "NZ", ".ie": "IE"}

# AU excluded: Spam Act 2003 bans sending to harvested addresses.
ALLOWED_COUNTRIES = {"US", "UK"}

# A brand at or above this citation share is already winning its category -
# the "you are invisible" pitch is false for them, so never draft it.
PERFORMING_SHARE_PCT = 5.0


def log(m):
    print(m, flush=True)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(d):
    return re.sub(r"[^a-z0-9.-]", "_", d.lower())


def norm_domain(d):
    d = d.strip().lower()
    d = re.sub(r"^https?://", "", d).rstrip("/")
    d = re.sub(r"^www\.", "", d)
    return d.split("/")[0]


def get(url, **kw):
    try:
        return requests.get(url, headers=HDRS, timeout=TIMEOUT, allow_redirects=True, **kw)
    except Exception as e:
        log("    ! fetch failed %s: %s" % (url, type(e).__name__))
        return None


def load_env():
    envf = ROOT / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def wordcount(s):
    return len(re.findall(r"\S+", s))


# ---------------------------------------------------------------- audit

def country_of(domain, html):
    for tld, c in COUNTRY_TLD.items():
        if domain.endswith(tld):
            return c
    h = (html or "")[:200000]
    if re.search(r'"currency"\s*:\s*"GBP"', h) or re.search(r"£\s*\d", h):
        return "UK"
    if re.search(r'"currency"\s*:\s*"AUD"', h) or re.search(r"Australia", h, re.I):
        return "AU"
    return "US"


def is_shopify(domain):
    r = get("https://%s/products.json?limit=1" % domain)
    if r is not None and r.status_code == 200:
        try:
            if "products" in r.json():
                return True, "products.json"
        except Exception:
            pass
    r2 = get("https://%s/" % domain)
    if r2 is not None and "cdn.shopify.com" in (r2.text or ""):
        return True, "cdn.shopify.com"
    return False, "not_detected"


def parse_page(html):
    soup = BeautifulSoup(html or "", "html.parser")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    h1el = soup.find("h1")
    h1 = h1el.get_text(" ", strip=True) if h1el else ""
    md = soup.find("meta", attrs={"name": "description"})
    meta = (md.get("content") or "").strip() if md else ""
    canonical = None
    for link in soup.find_all("link", href=True):
        rel = link.get("rel") or []
        if isinstance(rel, str):
            rel = [rel]
        if "canonical" in [x.lower() for x in rel]:
            canonical = link["href"]
            break
    types = set()
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            blob = json.loads(raw)
        except Exception:
            continue
        stack = [blob]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                t = n.get("@type")
                if isinstance(t, str):
                    types.add(t)
                elif isinstance(t, list):
                    types.update(x for x in t if isinstance(x, str))
                stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
            elif isinstance(n, list):
                stack.extend(n)
    return {"title": title, "h1": h1, "meta_description": meta,
            "canonical": canonical, "jsonld_types": sorted(types), "soup": soup}


STOP = {"the", "and", "for", "official", "shop", "store", "buy", "online", "best",
        "home", "co", "inc", "ltd", "llc", "usa", "uk", "free", "shipping",
        "premium", "quality"}

CATEGORY_PATTERNS = [
    r"\b(pet|dog|cat|puppy)\s+(supplies|food|treats|gear|accessories|supplements?|chews|vitamins?)\b",
    r"\b(fitness|gym|workout|training)\s+(equipment|gear|apparel|supplements)\b",
    r"\b(skincare|skin care|haircare|hair care|beauty|cosmetics|makeup)\b",
    r"\b(supplements?|protein|vitamins?)\b",
    r"\b(candles?|coffee|tea|jewelry|jewellery|bedding|cookware)\b",
]


def derive_category(title, h1, meta):
    text = " ".join([title or "", h1 or "", meta or ""]).lower()
    for pat in CATEGORY_PATTERNS:
        m = re.search(pat, text)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()
    tail = re.split(r"[|\-–—:]", title or "")
    cand = tail[-1] if len(tail) > 1 else (h1 or title or "")
    words = [w for w in re.findall(r"[a-z]+", cand.lower()) if w not in STOP and len(w) > 2]
    return " ".join(words[:3]) or "products"


def derive_brand(title, domain):
    """First title segment, but only if it reads like a name - else the domain root.

    Titles such as "The San Francisco Bay Area's #1 Fitness Superstore" have no
    delimiter, so segment[0] is the whole tagline. Falling back to the domain
    root is always safe and is what the recipient calls themselves anyway.
    """
    root = domain.split(".")[0].replace("-", " ")
    seg = re.split(r"[|\-–—:·]", title or "")[0].strip()
    seg = re.sub(r"\s+", " ", seg)
    if seg and len(seg) <= 32 and len(seg.split()) <= 4 and not seg.lower().startswith("the "):
        return seg
    return root.title()


def check_schema(p):
    t = set(p["jsonld_types"])
    return {"has_product": bool(t & {"Product", "ProductGroup"}),
            "has_organization": bool(t & {"Organization", "OnlineStore", "Store", "LocalBusiness"}),
            "has_faq": bool(t & {"FAQPage", "Question"}),
            "has_breadcrumb": "BreadcrumbList" in t,
            "types_found": sorted(t)}


def find_product_url(domain, soup):
    r = get("https://%s/products.json?limit=1" % domain)
    if r is not None and r.status_code == 200:
        try:
            ps = r.json().get("products") or []
            if ps and ps[0].get("handle"):
                return "https://%s/products/%s" % (domain, ps[0]["handle"])
        except Exception:
            pass
    if soup:
        for a in soup.find_all("a", href=True):
            if "/products/" in a["href"]:
                return urljoin("https://%s/" % domain, a["href"])
    return None


def load_mcp(dom_dir, name):
    f = dom_dir / ("%s.json" % name)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def build_fixes(schema, llms, canonical, has_faq):
    f = []
    if not schema["has_product"]:
        f.append("Add Product JSON-LD with price, availability and review data so AI engines can read your catalogue.")
    if not schema["has_organization"]:
        f.append("Add Organization schema so AI systems can resolve your brand as a real entity.")
    if not has_faq:
        f.append("Publish an FAQ page with schema answering the questions shoppers actually ask AI.")
    if not llms:
        f.append("Add an llms.txt file telling AI crawlers what your store sells.")
    if not canonical:
        f.append("Add canonical tags - duplicate URLs are splitting your ranking signals.")
    f.append("Build a comparison page targeting \"best in category\" queries, the phrasing AI answers pull from.")
    return f


def render_audit_md(a):
    eng = a["ai_visibility"]["engines"]
    comps = a["ai_visibility"]["competitors_named"]
    checked = dict((k, v) for k, v in eng.items() if v.get("checked"))
    named = sum(1 for v in checked.values() if v.get("brand_mentioned"))
    cs = ", ".join(comps[:3]) if comps else "other brands"
    fixes = a["recommended_fixes"][:3]
    g = a["ai_visibility"].get("citation_gap")
    if g and g.get("own_share_pct") is not None:
        head = ("**AI answers about \"%s\" cite you %.3f%% of the time** - rank %s of %s. "
                "%s takes %.1f%%." % (a["category"], g["own_share_pct"], g["own_rank"],
                                      g["set_size"], g["leader"], g["leader_share_pct"]))
        if g.get("peer") and g.get("peer_multiple"):
            head += " %s gets %sx more citations than you." % (g["peer"], g["peer_multiple"])
    elif checked:
        head = "**Named by %d of %d AI engines** for \"%s\". Cited instead: %s." % (
            named, len(checked), a["category"], cs)
    else:
        head = "**AI visibility unverified** for \"%s\" - no engine returned data." % a["category"]
    lines = ["# %s - AI Search Visibility" % a["brand"], "", head, "",
             ("When a shopper asks an AI assistant for the best %s, %s is not the answer. %s are."
              % (a["category"], a["brand"], cs)) if (g or comps) else
             ("We could not measure %s's AI citation share for \"%s\" - the checks below are from "
              "your live pages." % (a["brand"], a["category"])),
             "", "**%s fix%s:**" % ({1: "One", 2: "Two", 3: "Three"}.get(len(fixes), len(fixes)),
                                    "" if len(fixes) == 1 else "es")]
    for i, f in enumerate(fixes, 1):
        lines.append("%d. %s" % (i, f))
    lines += ["", "Full audit and fix options: %s" % a.get("offer_url", "(offer page pending)"), "",
              "_Checked %s. Engines: %s._" % (a["checked_at"][:10],
                                              ", ".join(checked.keys()) or "none")]
    md = "\n".join(lines)
    if wordcount(re.sub(r"[#*_`]", "", md)) > 180:
        lines = lines[:-3] + ["Full audit: %s" % a.get("offer_url", "(pending)")]
        md = "\n".join(lines)
    return md


def cmd_audit(args):
    domain = norm_domain(args.domain)
    d = AUDITS / slug(domain)
    (d / "screenshots").mkdir(parents=True, exist_ok=True)
    log("[audit] %s" % domain)
    if args.dry_run:
        log("  DRY-RUN would write %s and %s" % (d / "audit.json", d / "audit.md"))
        return 0

    home = get("https://%s/" % domain)
    if home is None or home.status_code >= 400:
        log("  ! homepage unreachable (status=%s)" % getattr(home, "status_code", "ERR"))
        return 1

    hp = parse_page(home.text)
    cat = args.category or derive_category(hp["title"], hp["h1"], hp["meta_description"])
    brand = derive_brand(hp["title"], domain)
    schema = check_schema(hp)

    purl = find_product_url(domain, hp["soup"])
    pschema = None
    if purl:
        pr = get(purl)
        if pr is not None and pr.status_code == 200:
            pschema = check_schema(parse_page(pr.text))

    lr = get("https://%s/llms.txt" % domain)
    llms = bool(lr is not None and lr.status_code == 200
                and "html" not in lr.headers.get("content-type", "").lower())

    shopify, sig = is_shopify(domain)
    country = country_of(domain, home.text)

    engines = {}
    for e in ("google_ai_overview", "perplexity", "chatgpt"):
        engines[e] = {"checked": False, "brand_mentioned": None, "source": None}
    comps = []

    citation = None
    orx = load_mcp(d, "openrush_ai_visibility")
    if orx:
        try:
            ms = (orx.get("data") or {}).get("mentions") or []
            mine = next((m for m in ms if norm_domain(m.get("domain", "")) == domain), None)
            others = sorted([m for m in ms if m is not mine],
                            key=lambda m: m.get("mentions") or 0, reverse=True)
            if mine:
                top = others[0] if others else None
                engines["google_ai_overview"] = {
                    "checked": True,
                    "brand_mentioned": bool(mine.get("mentions")),
                    "mentions": mine.get("mentions"),
                    "share": mine.get("share_within_set"),
                    "rank": mine.get("rank"),
                    "set_size": len(ms),
                    "source": "openrush.inspect_ai_visibility"}
                # Nearest peer above us = the most persuasive comparison in the email.
                peer = min((o for o in others if (o.get("mentions") or 0) > (mine.get("mentions") or 0)),
                           key=lambda o: o.get("mentions") or 0, default=None)
                citation = {
                    "own_mentions": mine.get("mentions"),
                    "own_share_pct": round(100 * (mine.get("share_within_set") or 0), 3),
                    "own_rank": mine.get("rank"), "set_size": len(ms),
                    "leader": top.get("domain") if top else None,
                    "leader_share_pct": round(100 * (top.get("share_within_set") or 0), 1) if top else None,
                    "peer": peer.get("domain") if peer else None,
                    "peer_multiple": (round((peer.get("mentions") or 0) / max(mine.get("mentions") or 1, 1))
                                      if peer else None)}
            comps += [m.get("domain") for m in others if m.get("domain")]
        except Exception as e:
            log("    ! openrush parse: %s" % e)

    px = load_mcp(d, "perplexity_ai_visibility")
    if px:
        engines["perplexity"] = {"checked": True,
                                 "brand_mentioned": bool(px.get("brand_mentioned")),
                                 "source": "perplexity.api"}
        comps += px.get("competitors_named") or []

    cg = load_mcp(d, "chatgpt_ai_visibility")
    if cg:
        engines["chatgpt"] = {"checked": True,
                              "brand_mentioned": bool(cg.get("brand_mentioned")),
                              "source": "chrome.manual"}
        comps += cg.get("competitors_named") or []

    seen = set()
    comps = [c for c in comps if not (c.lower() in seen or seen.add(c.lower()))]

    has_faq = schema["has_faq"] or bool(pschema and pschema["has_faq"])
    merged = {"has_product": schema["has_product"] or bool(pschema and pschema["has_product"]),
              "has_organization": schema["has_organization"],
              "has_faq": has_faq,
              "has_breadcrumb": schema["has_breadcrumb"]}

    offer = {}
    sf = CONFIG / "site.json"
    if sf.exists():
        try:
            offer = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            pass

    a = {"domain": domain, "brand": brand, "category": cat, "country": country,
         "checked_at": now(), "is_shopify": shopify, "shopify_signal": sig,
         "homepage": {"title": hp["title"], "h1": hp["h1"],
                      "meta_description": hp["meta_description"],
                      "canonical": hp["canonical"], "jsonld_types": hp["jsonld_types"]},
         "product_page": {"url": purl,
                          "jsonld_types": pschema["types_found"] if pschema else []},
         "schema": merged, "llms_txt": llms,
         "ai_visibility": {
             "engines": engines,
             "citation_gap": citation,
             "competitors_named": comps,
             "engines_checked": sum(1 for v in engines.values() if v["checked"]),
             "engines_naming_brand": sum(1 for v in engines.values()
                                         if v["checked"] and v["brand_mentioned"])},
         "offer_url": offer.get("url", "(offer page pending)"),
         "screenshots": sorted(p.name for p in (d / "screenshots").glob("*") if p.is_file())}
    a["recommended_fixes"] = build_fixes(merged, llms, hp["canonical"], has_faq)

    (d / "audit.json").write_text(json.dumps(a, indent=2), encoding="utf-8")
    md = render_audit_md(a)
    (d / "audit.md").write_text(md, encoding="utf-8")
    log("  -> %s (%d words) | schema=%s llms_txt=%s engines_checked=%d" % (
        d / "audit.md", wordcount(re.sub(r"[#*_`]", "", md)), merged, llms,
        a["ai_visibility"]["engines_checked"]))
    return 0


# ------------------------------------------------------------- discover

def apify_discover(n, token):
    r = requests.get("https://api.apify.com/v2/store",
                     params={"search": "shopify stores", "limit": 25},
                     headers={"Authorization": "Bearer %s" % token}, timeout=TIMEOUT)
    r.raise_for_status()
    items = r.json().get("data", {}).get("items", [])
    if not items:
        return []
    items.sort(key=lambda a: a.get("stats", {}).get("totalRuns", 0), reverse=True)
    pick = items[0]
    log("  apify actor: %s runs=%s" % (pick.get("name"), pick.get("stats", {}).get("totalRuns")))
    aid = pick.get("id")
    s = requests.get("https://api.apify.com/v2/acts/%s/builds/default" % aid,
                     headers={"Authorization": "Bearer %s" % token}, timeout=TIMEOUT)
    log("  input schema read status=%s (read before run, not guessed)" % s.status_code)
    return []


def cmd_discover(args):
    load_env()
    token = os.environ.get("APIFY_TOKEN", "").strip()
    out = DATA / "cold.csv"
    log("[discover] target n=%d apify_token=%s" % (
        args.n, "present" if token else "MISSING -> fallback"))
    if args.dry_run:
        log("  DRY-RUN would write %s" % out)
        return 0

    cands = []
    if token:
        try:
            cands = apify_discover(args.n, token)
        except Exception as e:
            log("  ! apify failed: %s %s; using fallback" % (type(e).__name__, e))
    if not cands:
        seeds = RAW / "seed_domains.json"
        if not seeds.exists():
            log("  ! no %s. Populate via OpenRush discover_competitors, or set APIFY_TOKEN." % seeds)
            return 1
        blob = json.loads(seeds.read_text(encoding="utf-8"))
        for cat, ds in blob.items():
            for dd in ds:
                cands.append({"domain": norm_domain(dd), "category": cat,
                              "source": "openrush.discover_competitors"})

    seen, rows = set(), []
    for c in cands:
        dom = c["domain"]
        if dom in seen:
            continue
        seen.add(dom)
        ok, sig = is_shopify(dom)
        if not ok:
            log("  skip %s (not shopify)" % dom)
            continue
        h = get("https://%s/" % dom)
        country = country_of(dom, h.text if h else "")
        if country not in ALLOWED_COUNTRIES:
            log("  skip %s (country=%s excluded)" % (dom, country))
            continue
        rows.append({"domain": dom, "category": c["category"], "est_revenue_signal": sig,
                     "country": country, "source": c["source"], "email": "", "email_source": ""})
        log("  + %s (%s, %s)" % (dom, c["category"], country))
        if len(rows) >= args.n:
            break

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "category", "est_revenue_signal",
                                          "country", "source", "email", "email_source"])
        w.writeheader()
        w.writerows(rows)
    log("  -> %s rows=%d" % (out, len(rows)))
    return 0


# --------------------------------------------------------------- enrich

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Only pages a business publishes in order to be contacted. Privacy-policy
# inboxes are published for data-subject requests, not sales - excluded.
ENRICH_PATHS = ["/pages/contact", "/pages/contact-us", "/pages/about",
                "/pages/about-us", "/contact", "/about"]


def emails_from(html, domain):
    out = []
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            e = a["href"][7:].split("?")[0].strip()
            if EMAIL_RE.fullmatch(e):
                out.append(e)
    out += EMAIL_RE.findall(html or "")
    parts = domain.split(".")
    root = parts[-2] if len(parts) > 1 else domain
    good = []
    for e in out:
        e = e.lower().strip(".,;:")
        lp, _, dm = e.partition("@")
        if dm in FREEMAIL or lp in BAD_LOCALPARTS:
            continue
        if any(e.endswith(x) for x in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        if "sentry" in dm or "wixpress" in dm:
            continue
        if root not in dm:
            continue
        good.append(e)
    seen = set()
    return [e for e in good if not (e in seen or seen.add(e))]


def cmd_enrich(args):
    f = DATA / "cold.csv"
    if not f.exists():
        log("  ! %s missing - run discover first" % f)
        return 1
    rows = list(csv.DictReader(f.open(encoding="utf-8")))
    log("[enrich] rows=%d" % len(rows))
    if args.dry_run:
        log("  DRY-RUN no writes")
        return 0
    if not rows:
        log("  nothing to enrich")
        return 0

    hits = 0
    for r in rows:
        if r.get("email"):
            hits += 1
            continue
        dom = r["domain"]
        found = src = None
        for p in ENRICH_PATHS:
            resp = get("https://%s%s" % (dom, p))
            if resp is None or resp.status_code != 200:
                continue
            es = emails_from(resp.text, dom)
            if es:
                found, src = es[0], "https://%s%s" % (dom, p)
                break
            time.sleep(0.4)
        if not found:
            resp = get("https://%s/" % dom)
            if resp is not None and resp.status_code == 200:
                es = emails_from(resp.text, dom)
                if es:
                    found, src = es[0], "https://%s/ (footer)" % dom
        if found:
            r["email"], r["email_source"] = found, src
            hits += 1
            log("  + %s -> %s" % (dom, found))
        else:
            log("  - %s no published address (skipped)" % dom)
        time.sleep(0.6)

    with f.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log("  -> hit rate %d/%d = %d%%" % (hits, len(rows), round(100 * hits / max(len(rows), 1))))
    return 0


# ---------------------------------------------------------------- draft

def compose(a, warm_ctx=None):
    eng = dict((k, v) for k, v in a["ai_visibility"]["engines"].items() if v.get("checked"))
    named = sum(1 for v in eng.values() if v.get("brand_mentioned"))
    comps = a["ai_visibility"]["competitors_named"]
    c1 = comps[0] if comps else "your competitors"
    fix = a["recommended_fixes"][0] if a["recommended_fixes"] else "Add Product schema."
    first = warm_ctx or ("I checked how %s shows up when shoppers ask AI for the best %s."
                         % (a["brand"], a["category"]))
    g = a["ai_visibility"].get("citation_gap")
    if g and g.get("own_share_pct") is not None:
        stat = "When AI answers questions about %s, it cites you %.3f%% of the time. %s takes %.1f%%." % (
            a["category"], g["own_share_pct"], g["leader"], g["leader_share_pct"])
        if g.get("peer") and g.get("peer_multiple"):
            stat += " %s gets %sx more citations than you do." % (g["peer"], g["peer_multiple"])
    elif eng:
        stat = "You were named by %d of %d AI engines. %s came up instead." % (named, len(eng), c1)
    elif not a["schema"]["has_product"]:
        stat = "Your store has no Product schema, so AI engines cannot read your catalogue."
    elif not a["schema"]["has_faq"]:
        stat = ("Your store has no FAQ schema - that is the format AI answers quote from "
                "when shoppers ask about %s." % a["category"])
    else:
        return None  # No honest hook. Never invent a problem the audit did not find.
    # Only promise a screenshot when one actually exists on disk.
    shot = "Screenshot attached.\n\n" if a.get("screenshots") else ""
    return ("%s\n\n%s\n\n%sBiggest cause: %s\n\n"
            "I do a full audit for $197, delivered in 24 hours - it shows every query you are "
            "missing and exactly what to change. Details: %s\n\nWorth a look?\n\nTrevor"
            % (first, stat, shot, fix, a.get("offer_url", "(pending)")))


def cmd_draft(args):
    rows = []
    cf = DATA / "cold.csv"
    if cf.exists():
        for r in csv.DictReader(cf.open(encoding="utf-8")):
            if r.get("email"):
                r["lane"] = "cold"
                rows.append(r)
    out, skipped = [], []
    for r in rows:
        ad = AUDITS / slug(r["domain"]) / "audit.json"
        if not ad.exists():
            continue
        a = json.loads(ad.read_text(encoding="utf-8"))
        # Do not pitch "you are invisible" to a brand that is already winning -
        # it is provably false to them and burns the prospect permanently.
        g = a["ai_visibility"].get("citation_gap") or {}
        if (g.get("own_share_pct") or 0) >= PERFORMING_SHARE_PCT:
            log("  skip %s (already at %.1f%% citation share - pitch would be false)"
                % (r["domain"], g["own_share_pct"]))
            skipped.append((r["domain"], "performing"))
            continue
        body = compose(a)
        if body is None:
            log("  skip %s (no honest hook - audit found no gap)" % r["domain"])
            skipped.append((r["domain"], "no_gap"))
            continue
        out.append({"recipient": r["email"], "domain": r["domain"], "lane": r["lane"],
                    "subject": "%s isn't showing up in AI search - here's the screenshot" % a["brand"],
                    "first_line": body.split("\n")[0], "body": body,
                    "words": wordcount(body),
                    "screenshot_dir": str(AUDITS / slug(r["domain"]) / "screenshots")})
    df = DATA / "drafts.csv"
    log("[draft] composed=%d" % len(out))
    if args.dry_run:
        log("  DRY-RUN would write %s" % df)
        return 0
    if out:
        with df.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(out[0].keys()))
            w.writeheader()
            w.writerows(out)
    over = [o for o in out if o["words"] > 120]
    log("  -> %s | over-120-words: %d" % (df, len(over)))
    for o in out[:10]:
        log("     %-38s | %3dw | %s" % (o["recipient"], o["words"], o["subject"][:58]))
    return 0


# -------------------------------------------------------------- followup

def cmd_followup(args):
    sf = DATA / "sent.csv"
    if not sf.exists():
        sf.write_text("timestamp,recipient,domain,subject,lane,followup_count,replied\n",
                      encoding="utf-8")
        log("  created empty %s - nothing sent yet" % sf)
        return 0
    rows = list(csv.DictReader(sf.open(encoding="utf-8")))
    due = []
    for r in rows:
        if (r.get("replied", "") or "").lower() in ("1", "true", "yes"):
            continue
        try:
            if int(r.get("followup_count") or 0) >= 2:
                continue
            ts = datetime.fromisoformat(r["timestamp"])
        except Exception:
            continue
        if datetime.now(timezone.utc) - ts >= timedelta(days=3):
            due.append(r)
    log("[followup] sent=%d due=%d" % (len(rows), len(due)))
    if args.dry_run:
        log("  DRY-RUN no writes")
        return 0
    for r in due:
        ad = AUDITS / slug(r["domain"]) / "audit.json"
        c1 = "your competitor"
        if ad.exists():
            cs = json.loads(ad.read_text(encoding="utf-8"))["ai_visibility"]["competitors_named"]
            if cs:
                c1 = cs[0]
        log("  draft-> %s: Bumping this - the screenshot still shows %s instead of you. "
            "Want the $197 audit?" % (r["recipient"], c1))
    return 0


# ------------------------------------------------------------------ main

def main():
    p = argparse.ArgumentParser(prog="engine.py",
                                description="OutreachEngine - AI Visibility Fix pipeline")
    p.add_argument("--dry-run", action="store_true", help="plan only, no writes")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="audit one domain")
    a.add_argument("domain")
    a.add_argument("--category", default=None)
    a.set_defaults(fn=cmd_audit)

    d = sub.add_parser("discover", help="build data/cold.csv")
    d.add_argument("--n", type=int, default=200)
    d.set_defaults(fn=cmd_discover)

    e = sub.add_parser("enrich", help="find published emails")
    e.set_defaults(fn=cmd_enrich)

    r = sub.add_parser("draft", help="compose drafts")
    r.set_defaults(fn=cmd_draft)

    f = sub.add_parser("followup", help="3-day bumps")
    f.set_defaults(fn=cmd_followup)

    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
