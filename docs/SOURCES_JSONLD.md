# JSON-LD sources (`GenericJsonLdScraper`)

Module: `src/scrapers/generic_jsonld_scraper.py`. Sites below are in `KNOWN_SITES`.
Verified live on 2026-10-30 from a home connection with plain `requests`, the project
User-Agent, ≥1 s between requests and ≤25 requests per site. No anti-bot bypass was used.

## How it works

1. **Discovery**
   - **Sitemap** (`sitemap_urls`): sitemap indexes are followed recursively. Child sitemaps are
     read newest `<lastmod>` first. When two have the same lastmod, the later one in the file is
     read first. Gzipped sitemaps are supported. A URL is kept when it matches `job_url_pattern`
     and its lastmod is within `max_age_days`. A URL with no lastmod is kept but sorted last.
   - **Listing** (`listing_urls`, which may contain `{page}`) and **search** (`search_url`, with
     `{keyword}` and `{page}`): every anchor that matches `job_url_pattern` is collected.
2. **Detail pages**: the scraper reads every `application/ld+json` block. It handles arrays,
   `@graph`, nested nodes such as `mainEntity`, HTML-comment wrappers and trailing commas.
   - If a page has no `JobPosting`, it falls back to `og:title`/`og:description`, then `<title>`.
   - If the page has none of these, it is skipped.
3. **Budgets**
   - Batch runs fetch at most `max_detail_pages` detail pages (default 100) and `max_sitemaps`
     sitemaps (default 6).
   - Live mode lowers the limits to 10 detail pages, 2 sitemaps and 1 listing or search page.
   - The scraper respects `deadline_at`. A 403, 429 or 503 response stops the run, and
     `last_error` is set to `HTTP<status>`.
   - When every page it fetched has no `JobPosting`, the scraper sets
     `last_error = "layout_changed"`.
4. **Keyword**: when `search_url` is set, the site's own search is used. Otherwise the scraper
   discovers jobs normally and keeps those whose title or description contains the keyword
   (compared with `clean_vn_text`). URLs whose slug contains the keyword are fetched first.

## Verified boards

| Board | Discovery | URLs | lastmod usable? | Page size |
|---|---|---|---|---|
| CareerViet | `https://careerviet.vn/sitemap/job_current_date.xml` (from the `robots.txt` index) | ~800 (vi and en; pattern keeps `/vi/`) | Date only, today | ~1.5 MB |
| ViecLam24h | `https://cdn1.vieclam24h.vn/file/sitemap/daily/job-0.xml`, an index of `tintuyendung-0..3.xml` | ~16k | No. Every entry has the generation time. The newest IDs are at the end of the highest-numbered file, so document order is used | ~510 KB |
| Viecoi | `https://viecoi.vn/job.xml` | ~5.5k (1.1 MB) | Yes, per URL | ~1.6 MB, slow |
| ITviec | `https://itviec.com/twinnings_jobs_desc_vn.xml` (from the index `dunggiatminh.xml`) | 639 | No, every entry has the same value | ~350 KB |
| CareerLink | No `Sitemap:` line, and `/sitemap.xml` returns 404. Uses listing `/vieclam/list?page={page}` and search `?keyword={keyword}&page={page}` | 50 per page | n/a | ~180 KB |
| 123job | `sitemap.xml` lists only category pages. Uses listing `https://123job.vn/viec-lam` | 12 per page | n/a | ~150 KB |

### JobPosting field coverage

| Field | CareerViet | ViecLam24h | Viecoi | ITviec | CareerLink | 123job |
|---|---|---|---|---|---|---|
| title | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| hiringOrganization.name | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| jobLocation | dict (locality = province) | list | list | list ("Not Available" locality) | list, multiple places | dict |
| baseSalary | min/max or text "Cạnh tranh" | min/max | min/max | min/max USD or "You'll love it" (needs login) | value/min or "Thương lượng" | min/max or none |
| datePosted | ISO datetime | date | ISO datetime | date | date | date |
| validThrough | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| employmentType | `['"FULL_TIME"']` (quotes stripped) | ✓ | ✓ | ✓ | ✓ | list |
| description (HTML) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| skills | ✓ | – | ✓ | ✓ | – | list |
| qualifications | – | sometimes | – | – | – | – |
| experience/education | text | months only | – | text | months only | junk string (ignored) |

### Timing

Each run fetched 1 discovery document and 4 detail pages. "Network" is the time spent in HTTP
requests. "Batch" is the wall-clock time, including the 2 s + jitter delay between requests.

| Board | Network | Batch | Live-mode fit |
|---|---|---|---|
| CareerViet | 1.3 s | 13.4 s | Good |
| ViecLam24h | 4.9 s | 15.6 s | Fair (2 sitemaps of 0.3–1.1 MB) |
| Viecoi | 24.5 s | 25.0 s | **Batch only.** Pages take 2.8–9.6 s each |
| ITviec | 4.9 s | 13.1 s | Fair |
| CareerLink (search) | 1.3 s | 12.3 s | Good |
| 123job | 1.2 s | 11.7 s | Good |

## Not usable without bypass or not checked

- **JobsGO**: Cloudflare returns 403 even for `robots.txt`.
- **TopDev**: the TLS handshake fails (`SSLEOFError`) with plain `requests`.
- **1900.com.vn**: DNS resolution fails.
- **JobOKO** (`vn.joboko.com`): `robots.txt` has no `Sitemap:` line and `/sitemap.xml` returns 404. It was not checked further.
