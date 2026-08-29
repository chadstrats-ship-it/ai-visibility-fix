# OutreachEngine

AI Visibility Fix pipeline. CSV on disk is the CRM.

    python engine.py discover --n 200
    python engine.py enrich
    python engine.py audit <domain>
    python engine.py draft
    python engine.py followup

`docs/index.html` is the deployed offer page (GitHub Pages, source = main /docs).
`site/index.html` is the source of truth; `docs/` is the published copy.

## Seeding new categories (OpenRush `discover_competitors`)

Use **plain head terms**. Transactional modifiers return an empty set — this is a
property of the keyword index, not of the category, and it repeatedly looked like
"discovery is exhausted" when it was only bad phrasing:

| Seed keywords | Result |
|---|---|
| `best loose leaf tea online`, `... subscription` | 0 competitors |
| `best loose leaf tea`, `best matcha` | 12 returned, 64 available |
| `best soy candles online`, `hand poured candle shop online` | 0 competitors |
| `best scented candles`, `best candle brands` | 12 returned, 64 available |

Rule: seed with `best <product>` / `best <product> brands`. Avoid `buy`, `online`,
`shop`, `subscription`. Then strip publishers, social and marketplaces from the
result before adding to `raw/seed_domains.json` — `DIRECTORY_DOMAINS` and
`NATIONAL_CHAINS` in `engine.py` are the backstop, not the first filter.

`coverage.total_available` in the response tells you how deep the category goes
before you invest in it.
