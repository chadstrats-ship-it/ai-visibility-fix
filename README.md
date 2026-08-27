# OutreachEngine

AI Visibility Fix pipeline. CSV on disk is the CRM.

    python engine.py discover --n 200
    python engine.py enrich
    python engine.py audit <domain>
    python engine.py draft
    python engine.py followup

`docs/index.html` is the deployed offer page (GitHub Pages, source = main /docs).
`site/index.html` is the source of truth; `docs/` is the published copy.
