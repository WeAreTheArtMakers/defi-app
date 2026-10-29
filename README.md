# defi.wearetheartmakers.com

- `/` — landing page: animated (illustrative) pipeline field, the five-question Evidence Card, the networks grid (Available / Collecting,
  with a status filter), the pipeline steps and the value section; links to the application, catalog, method, pricing and partners.
  One self-contained `index.html` (inline CSS and vanilla JS, no external scripts); dark and light colour schemes; responsive
  from 320 px; respects reduced motion. Network facts follow the core repo's `docs/support-matrix.md` and the public catalog.
- `/app/…` — redirects (keeping the path) to https://app.wearetheartmakers.com (named Cloudflare tunnel).
  `app/index.html` and `404.html` are regenerated from `app.template.html` automatically when that address changes.

DNS: `defi` CNAME `wearetheartmakers.github.io`.
