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

# Public site the audit pages are published under.
PUBLIC_BASE = "https://aivisibilityfix.co"

# Aggregators, directories and publishers. They rank for local queries but are
# not prospects - never let them into cold.csv.
DIRECTORY_DOMAINS = {
    "yelp.com", "yellowpages.com", "angi.com", "angieslist.com", "thumbtack.com",
    "healthgrades.com", "zocdoc.com", "vitals.com", "webmd.com", "ada.org",
    "bbb.org", "houzz.com", "homeadvisor.com", "porch.com", "networx.com",
    "groupon.com", "tripadvisor.com", "facebook.com", "instagram.com",
    "youtube.com", "reddit.com", "wikipedia.org", "amazon.com", "walmart.com",
    "target.com", "mapquest.com", "google.com", "apple.com", "nextdoor.com",
    "realself.com", "spafinder.com", "booksy.com", "vagaro.com", "opencare.com",
    "carecredit.com", "1-800-dentist.com", "expertise.com", "birdeye.com",
    "trustpilot.com", "indeed.com", "glassdoor.com", "linkedin.com", "yahoo.com",
    # surfaced by the local discovery runs
    "deltadental.com", "patientconnect365.com", "doctorsnetwork.com", "practo.com",
    "webmd.com", "medicalspalocator.com", "threebestrated.com", "forbes.com",
    "consumeraffairs.com", "homedepot.com", "lowes.com", "promptloop.com",
    "bestbuy.com", "businessinsider.com", "petmd.com", "akc.org", "avma.org",
    "dickssportinggoods.com", "academy.com", "chewy.com",
}

# National chains / franchise networks - real businesses, but not prospects for a
# $197 local audit, and they distort the "local" lane.
NATIONAL_CHAINS = {"airtron.com", "serviceexperts.com", "searsheatingcooling.com",
                   "skinspirit.com", "portraitcare.com"}


def is_directory(domain):
    d = norm_domain(domain)
    if d.endswith(".edu") or d.endswith(".gov") or d.endswith(".org"):
        return True
    if d in NATIONAL_CHAINS:
        return True
    return d in DIRECTORY_DOMAINS or any(d.endswith("." + x) for x in DIRECTORY_DOMAINS)


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


def get(url, _retry=True, **kw):
    try:
        return requests.get(url, headers=HDRS, timeout=TIMEOUT, allow_redirects=True, **kw)
    except Exception as e:
        # Many small-business hosts only answer on the www host.
        if _retry and "://www." not in url:
            alt = re.sub(r"^(https?://)", r"\1www.", url, count=1)
            return get(alt, _retry=False, **kw)
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
    # Currency codes only. The bare word "Australia" appears in shipping lists and
    # testimonials on US sites, and matching it dropped real US prospects
    # (allamerican-NC = North Carolina, greatskinKC = Kansas City).
    if re.search(r'"currency"\s*:\s*"AUD"', h) or re.search(r"\bAUD\s*\$", h):
        return "AU"
    if re.search(r'"currency"\s*:\s*"GBP"', h) or re.search(r"£\s*\d", h):
        return "UK"
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
    # Titles are often "<service> in <city> | <Brand>", so score every segment
    # instead of blindly taking the first.
    descriptive = re.compile(
        r"\b(in|near|serving|best|top|official|home|welcome|your|we|the)\b|,", re.I)
    best, best_score = None, -1
    for seg in re.split(r"[|\-–—:·]", title or ""):
        seg = re.sub(r"\s+", " ", seg).strip()
        if not seg or len(seg) > 32 or len(seg.split()) > 4:
            continue
        score = 0
        if not descriptive.search(seg):
            score += 3
        # A segment sharing the domain root is almost certainly the brand.
        if re.sub(r"[^a-z]", "", seg.lower())[:6] in re.sub(r"[^a-z]", "", root.lower()):
            score += 4
        if seg.istitle() or seg.isupper():
            score += 1
        if score > best_score:
            best, best_score = seg, score
    if best and best_score >= 3:
        return best
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
    citation_table = []
    orx = load_mcp(d, "openrush_ai_visibility")
    if orx:
        try:
            ms = (orx.get("data") or {}).get("mentions") or []
            citation_table = sorted(ms, key=lambda m: m.get("rank") or 99)
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
             "citation_table": citation_table,
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
    out = DATA / ("local.csv" if args.niche == "local" else "cold.csv")
    log("[discover] niche=%s target n=%d apify_token=%s" % (
        args.niche, args.n, "present" if token else "MISSING -> fallback"))
    if args.dry_run:
        log("  DRY-RUN would write %s" % out)
        return 0

    local = (args.niche == "local")
    seed_file = RAW / ("seed_local.json" if local else "seed_domains.json")

    cands = []
    if token and not local:
        try:
            cands = apify_discover(args.n, token)
        except Exception as e:
            log("  ! apify failed: %s %s; using fallback" % (type(e).__name__, e))
    if not cands:
        if not seed_file.exists():
            log("  ! no %s. Populate via OpenRush discover_competitors." % seed_file)
            return 1
        blob = json.loads(seed_file.read_text(encoding="utf-8"))
        for cat, ds in blob.items():
            for dd in ds:
                cands.append({"domain": norm_domain(dd), "category": cat,
                              "source": "openrush.discover_competitors"})

    # Merge with what is already on disk so re-running never destroys enrichment.
    existing = {}
    if out.exists():
        for r in csv.DictReader(out.open(encoding="utf-8")):
            existing[r["domain"]] = r

    seen, rows = set(), []
    for c in cands:
        dom = c["domain"]
        if dom in seen:
            continue
        seen.add(dom)
        if dom in existing:
            rows.append(existing[dom])
            continue
        if is_directory(dom):
            log("  skip %s (directory/aggregator, not a prospect)" % dom)
            continue
        if local:
            h = get("https://%s/" % dom)
            if h is None or h.status_code >= 400:
                log("  skip %s (unreachable)" % dom)
                continue
            sig = "local_business"
        else:
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
    kept = sum(1 for r in rows if r["domain"] in existing)
    log("  -> %s rows=%d (new=%d kept=%d)" % (out, len(rows), len(rows) - kept, kept))
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
    files = [DATA / "cold.csv", DATA / "local.csv"]
    files = [f for f in files if f.exists()]
    if not files:
        log("  ! no cold.csv/local.csv - run discover first")
        return 1
    if args.dry_run:
        for f in files:
            n = len(list(csv.DictReader(f.open(encoding="utf-8"))))
            log("[enrich] DRY-RUN %s rows=%d, no writes" % (f.name, n))
        return 0
    total_hits = total_rows = 0
    for f in files:
        h, n = enrich_file(f)
        total_hits += h
        total_rows += n
    log("  == overall hit rate %d/%d = %d%%"
        % (total_hits, total_rows, round(100 * total_hits / max(total_rows, 1))))
    return 0


def enrich_file(f):
    rows = list(csv.DictReader(f.open(encoding="utf-8")))
    log("[enrich] %s rows=%d" % (f.name, len(rows)))
    if not rows:
        return 0, 0

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
    log("  -> %s hit rate %d/%d = %d%%"
        % (f.name, hits, len(rows), round(100 * hits / max(len(rows), 1))))
    return hits, len(rows)


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
    for cf, lane in ((DATA / "cold.csv", "dtc"), (DATA / "local.csv", "local")):
        if not cf.exists():
            continue
        for r in csv.DictReader(cf.open(encoding="utf-8")):
            if r.get("email"):
                r["lane"] = lane
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
        # Only promise a screenshot in the subject when one actually exists.
        subj = ("%s isn't showing up in AI search - here's the screenshot" % a["brand"]
                if a.get("screenshots") else
                "%s isn't showing up in AI search - here are the numbers" % a["brand"])
        out.append({"recipient": r["email"], "domain": r["domain"], "lane": r["lane"],
                    "subject": subj,
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


# ---------------------------------------------------------------- export

# Role/shared inboxes - there is no person behind them, so first_name stays
# blank rather than greeting a business "Hi Info,".
ROLE_LOCALPARTS = {"info", "sales", "contact", "hello", "hi", "support", "admin",
                   "team", "orders", "help", "office", "mail", "enquiries",
                   "inquiries", "service", "customerservice", "care", "shop",
                   "bark", "woof", "reception", "frontdesk", "appointments",
                   "booking", "bookings", "schedule", "newpatients", "smile"}


def first_name_from(email):
    """Only return a name we can actually justify. Never invent one."""
    lp = (email or "").partition("@")[0].lower()
    lp = re.sub(r"[0-9]+$", "", lp)
    if "." in lp:
        cand = lp.split(".")[0]
    elif "_" in lp:
        cand = lp.split("_")[0]
    else:
        cand = lp
    if cand in ROLE_LOCALPARTS or len(cand) < 3 or not cand.isalpha():
        return ""
    # A bare single token is only a name if it is not a role word; a dotted
    # local part (john.smith) is strong evidence, a bare one is weak.
    if "." not in lp and "_" not in lp:
        return ""
    return cand.capitalize()


AUDIT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{brand} - AI Search Visibility Audit</title>
<style>
:root{{--bg:#fff;--fg:#0f1115;--muted:#5b6270;--line:#e4e7ec;--card:#f7f8fa;--accent:#1a5cff;--warn:#b42318}}
@media(prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0f1115;--fg:#f2f4f7;--muted:#98a2b3;--line:#252a33;--card:#171a21;--accent:#5b8cff;--warn:#ff6b5e}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
.wrap{{max-width:720px;margin:0 auto;padding:56px 20px 72px}}
h1{{font-size:clamp(24px,4vw,34px);letter-spacing:-.02em;margin:0 0 6px;line-height:1.15}}
.dom{{color:var(--muted);margin:0 0 26px;font-size:15px}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin:0 0 26px}}
.big{{font-size:clamp(26px,5vw,38px);font-weight:700;color:var(--warn);letter-spacing:-.02em;
font-variant-numeric:tabular-nums;line-height:1.1}}
.stat p{{margin:8px 0 0;color:var(--muted);font-size:15px}}
table{{width:100%;border-collapse:collapse;margin:0 0 26px;font-size:15px}}
th,td{{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}}
th{{color:var(--muted);font-weight:600;font-size:13px;text-transform:uppercase;letter-spacing:.04em}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
tr.you td{{font-weight:700;color:var(--warn)}}
img{{max-width:100%;border:1px solid var(--line);border-radius:10px;display:block;margin:0 0 8px}}
figcaption{{color:var(--muted);font-size:13px;margin:0 0 26px}}
ol{{padding-left:20px}}ol li{{margin-bottom:9px}}
.cta{{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;
padding:13px 20px;border-radius:8px;font-weight:650;margin-top:8px}}
footer{{margin-top:40px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px}}
</style></head><body><div class="wrap">
<h1>{brand}: AI search visibility</h1>
<p class="dom">{domain} &middot; measured {date}</p>
{stat_block}
{table_block}
{shot_block}
<h2>What to fix</h2>
<ol>{fixes}</ol>
<a class="cta" href="{base}/">See the $197 audit &rarr;</a>
<footer>Measured by 322 Media LLC. Citation share is a sampled observation of Google AI Overview
for this category, measured against the named competitor domains above. AI answers are
non-deterministic; treat movement as directional. No ranking or citation count is guaranteed.</footer>
</div></body></html>
"""


def build_audit_page(a, has_shot):
    g = a["ai_visibility"].get("citation_gap") or {}
    if g.get("own_share_pct") is not None:
        stat = ('<div class="stat"><div class="big">{p:.3f}%</div><p>Share of AI-answer citations '
                'you earn for "{c}" - rank {r} of {n}. {L} takes {ls:.1f}%.</p></div>').format(
            p=g["own_share_pct"], c=a["category"], r=g["own_rank"], n=g["set_size"],
            L=g["leader"], ls=g["leader_share_pct"])
        rows = ""
        for m in (a["ai_visibility"].get("citation_table") or []):
            you = ' class="you"' if norm_domain(m["domain"]) == a["domain"] else ""
            rows += '<tr{y}><td>{d}</td><td class="num">{s:.2f}%</td><td class="num">{k}</td></tr>'.format(
                y=you, d=m["domain"], s=100 * (m.get("share_within_set") or 0), k=m.get("rank"))
        table = ("<table><tr><th>Domain</th><th>Share of citations</th><th>Rank</th></tr>%s</table>"
                 % rows) if rows else ""
    else:
        stat = ('<div class="stat"><p>AI citation share could not be measured for "%s". '
                'The findings below come from your live pages.</p></div>' % a["category"])
        table = ""
    shot = ""
    if has_shot:
        shot = ('<figure><img src="proof.jpg" alt="AI answer for this category">'
                '<figcaption>The AI answer for this category. Your brand is not in it.</figcaption></figure>')
    fixes = "".join("<li>%s</li>" % f for f in a["recommended_fixes"][:4])
    return AUDIT_PAGE.format(brand=a["brand"], domain=a["domain"], date=a["checked_at"][:10],
                             stat_block=stat, table_block=table, shot_block=shot,
                             fixes=fixes, base=PUBLIC_BASE)


def cmd_export(args):
    df = DATA / "drafts.csv"
    if not df.exists():
        log("  ! no drafts.csv - run draft first")
        return 1
    drafts = list(csv.DictReader(df.open(encoding="utf-8")))
    outdir = ROOT / "docs" / "a"
    out_csv = DATA / "instantly.csv"
    log("[export] drafts=%d" % len(drafts))
    if args.dry_run:
        log("  DRY-RUN would publish %d pages under docs/a/ and write %s" % (len(drafts), out_csv))
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    rows, no_name = [], 0
    for d in drafts:
        dom = d["domain"]
        ad = AUDITS / slug(dom) / "audit.json"
        if not ad.exists():
            continue
        a = json.loads(ad.read_text(encoding="utf-8"))
        pdir = outdir / slug(dom)
        pdir.mkdir(parents=True, exist_ok=True)

        shots = sorted((AUDITS / slug(dom) / "screenshots").glob("*.jpg"))
        has_shot = bool(shots)
        if has_shot:
            (pdir / "proof.jpg").write_bytes(shots[0].read_bytes())
        (pdir / "index.html").write_text(build_audit_page(a, has_shot), encoding="utf-8")

        comps = a["ai_visibility"]["competitors_named"]
        g = a["ai_visibility"].get("citation_gap") or {}
        competitor = g.get("peer") or (comps[0] if comps else "")
        fn = first_name_from(d["recipient"])
        if not fn:
            no_name += 1
        rows.append({
            "email": d["recipient"],
            "first_name": fn,
            "brand": a["brand"],
            "competitor_named": competitor,
            "screenshot_url": ("%s/a/%s/proof.jpg" % (PUBLIC_BASE, slug(dom))) if has_shot else "",
            "audit_url": "%s/a/%s/" % (PUBLIC_BASE, slug(dom)),
        })

    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["email", "first_name", "brand",
                                           "competitor_named", "screenshot_url", "audit_url"])
        w.writeheader()
        w.writerows(rows)
    with_shot = sum(1 for r in rows if r["screenshot_url"])
    log("  -> %s rows=%d | with screenshot=%d | blank first_name=%d (role inboxes)"
        % (out_csv, len(rows), with_shot, no_name))
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

    d = sub.add_parser("discover", help="build data/cold.csv (dtc) or data/local.csv (local)")
    d.add_argument("--n", type=int, default=200)
    d.add_argument("--niche", choices=["dtc", "local"], default="dtc",
                   help="dtc = Shopify DTC stores; local = local service businesses")
    d.set_defaults(fn=cmd_discover)

    e = sub.add_parser("enrich", help="find published emails")
    e.set_defaults(fn=cmd_enrich)

    r = sub.add_parser("draft", help="compose drafts")
    r.set_defaults(fn=cmd_draft)

    f = sub.add_parser("followup", help="3-day bumps")
    f.set_defaults(fn=cmd_followup)

    x = sub.add_parser("export", help="publish audit pages + write Instantly CSV")
    x.set_defaults(fn=cmd_export)

    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
