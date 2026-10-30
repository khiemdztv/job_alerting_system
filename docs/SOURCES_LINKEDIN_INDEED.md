# LinkedIn / Indeed / Glassdoor Vietnam (30/10/2026)

Endpoints were read from `python-jobspy` 1.x source (MIT) and called with plain `requests`.
`jobspy` itself cannot be imported on the dev machine (pandas DLL blocked by Application Control), and
needs pandas + curl_cffi (Glassdoor) anyway, so the logic was re-implemented directly.

## Indeed VN - feasible, implemented (`src/scrapers/indeed_scraper.py`)
- `POST https://apis.indeed.com/graphql`, JSON `{"query": ...}`, headers `indeed-api-key` (public app key),
  `indeed-co: VN`, `indeed-locale: vi-VN`, iOS app User-Agent. No cookies, no login.
- Query `jobSearch(what, location{where,radius}, limit<=100, cursor, filters[date dateOnIndeed "72h"])`;
  next page via `pageInfo.nextCursor`. Result carries description HTML, compensation, attributes, employer.
- Experiments (5 searches, 4 s apart, all HTTP 200, 0.8-1.2 s, 360-470 KB for limit 100):

| query / location | results | date | salary | city | job type | description | company | remote |
|---|---|---|---|---|---|---|---|---|
| kế toán / Vietnam | 100 (+next) | 100 | 74 | 79 | 100 | 100 | 100 | 0 |
| nhân viên kinh doanh / HCMC | 100 (+next) | 100 | 61 | 94 | 98 | 100 | 100 | 0 |
| data engineer / Hanoi | 9 | 9 | 0 | 9 | 2 | 9 | 9 | 0 |
| marketing / Vietnam | 100 (+next) | 100 | 82 | 63 | 96 | 100 | 100 | 0 |
| python / Vietnam | 61 | 61 | 11 | 55 | 37 | 61 | 61 | 0 |

- Salary is real VND (employer-declared), mostly from aggregated boards (many results are JobsGO/other boards
  re-listed, `recruit.viewJobUrl` points to the original). Only 1 extra verification call was made after the 5.
- No 403/429. Risk: the app key can be rotated; Indeed ToS also restricts scraping.

## LinkedIn - feasible with limits, implemented (`src/scrapers/linkedin_scraper.py`)
- `GET https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords&location&f_TPR=r259200&start=N`,
  returns HTML `<li>` cards, 10 per page, max start 1000. No login. Listing has title, company, location,
  `<time datetime>`, canonical job id. No salary/description/job type (needs `/jobs/view/{id}` per job: not done).
- Experiments: kế toán / Vietnam 10 cards (date 10, location 10, salary 0), nhân viên kinh doanh / HCMC 10 cards,
  then the 3rd request (data engineer / Hanoi) got an SSL EOF (connection reset = soft throttle) after only 2
  requests. Stopped per rules; one later single verification call ("python") succeeded 10/10 about 15 min later.
  Latency about 1 s. English titles dominate; location strings are English ("Ho Chi Minh City, Vietnam").
- Fields covered: title, company, location, posted date, url, remote hint. Not covered: salary, description,
  requirements, job type.

## Glassdoor VN - NOT implemented
- jobspy uses `POST https://www.glassdoor.com/graph` with a hard-coded `gd-csrf-token` and a `curl_cffi`
  TLS-impersonating session plus `/autocomplete/location` lookup. Without TLS impersonation it is very likely
  blocked (Cloudflare), and impersonation is excluded by the brief. Not experimented. Verdict: skip; revisit
  only via external runner with curl_cffi.

## Recommendations
- Registry: `indeed` (live_priority medium-high, live_timeout 6-8 s, batch yes), `linkedin` (live_priority low,
  live_timeout 5 s, batch only or live opt-in, `needs_proxy`/external-runner flag if throttled from AWS IPs).
- Rate limit: Indeed <= 1 req/s, max 2 pages per keyword per run; LinkedIn <= 1 search per 5 s from one IP,
  1 page per keyword, a few runs per hour at most (hourly/daily alert cadence), back off on SSL EOF/429/999.
- ToS: LinkedIn and Indeed forbid scraping; treat as best-effort "nice to have", never log in, always link back
  to the original listing, drop the source on repeated failure.
