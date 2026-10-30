# Nguồn việc làm của ViecLamBot (cập nhật 30/10/2026)

Nguồn sự thật về danh sách là `src/scrapers/registry.py` (18 nguồn đăng ký). Tài liệu khảo sát chi tiết:
`docs/SOURCES_RESEARCH_VN.md` (123job, JobOKO, vieclam.gov.vn và các site bị bỏ), `docs/SOURCES_JSONLD.md`
(sitemap + JSON-LD), `docs/SOURCES_LINKEDIN_INDEED.md` (Indeed, LinkedIn, Glassdoor).

## Quy ước chung

- **Tốc độ**: chạy định kỳ (batch) dùng `scrape_delay_seconds` = 2 giây (+ jitter) giữa hai request cùng một site, mỗi
  nguồn một worker/một session; tối đa `scrape_max_pages` trang mỗi từ khóa và `scrape_latest_pages` (mặc định 3) trang
  "mới nhất". Live search (`/search`) chỉ gửi 1 request ngắn mỗi nguồn, không retry, không delay, timeout theo
  `live_timeout` của nguồn. Khi nguồn trả 403/429/503 hoặc không kết nối được, scraper dừng sớm và ghi `blocked`/`error`.
- **Tự tạm dừng**: 5 lần chạy định kỳ liên tiếp `blocked`/`error`/`layout_changed` thì nguồn bị bỏ qua 24 giờ
  (`HEALTH#<source>.paused_until`), admin nhận tin khi dừng và khi hoạt động lại; sáng thứ Hai có tóm tắt 7 ngày.
- **Không** dùng proxy, giải CAPTCHA, giả Googlebot hay TLS impersonation. Luôn link về tin gốc.
- Cột "Chạy" : `batch` = lần cào định kỳ 6 giờ lấp DynamoDB; `live` = truy vấn trực tiếp khi `/search`.
- Cột "Trường": N = ngày đăng, H = hạn nộp, L = lương (khoảng số), l = lương chỉ dạng chữ hoặc không ổn định,
  "-" = không có trên danh sách.

## Bảng nguồn

| Nguồn (`source`) | Cách truy cập | robots.txt / ToS | Tốc độ | Trường (N / H / L) | Chặn từ AWS | Chạy | Ghi công / lưu ý |
|---|---|---|---|---|---|---|---|
| VietnamWorks (`vietnamworks`) | API JSON `ms.vietnamworks.com/job-search/v1.0/search` (100 tin/request); fallback `__NEXT_DATA__` | API nội bộ của chính site; không có khóa; dùng mức thấp | batch 2 s; live 1 request | N (`approvedOn`) / H (expiry) / L | Không ghi nhận | batch + live | Link về trang tin gốc |
| CareerViet (`careerviet`) | HTML SSR Next.js (50 thẻ/trang), sắp xếp `sortdv` mới nhất; fallback sitemap + JSON-LD | Sitemap khai báo trong robots; trang rất nặng (~1,5 MB) | batch 2 s; live 1 trang | N ("Cập nhật") / H có trong JSON-LD chi tiết / l | Không ghi nhận | batch + live | Link tin gốc |
| ViecLam24h (`vieclam24h`) | `__NEXT_DATA__` JSON trong trang tìm kiếm; HTML thẻ làm dự phòng; fallback sitemap + JSON-LD | Dùng đường dẫn mà UI gọi; trang ~0,5-1 MB | batch 2 s; live timeout 5 s | N (`approved_at`) / - / L | Không ghi nhận | batch + live | Link tin gốc |
| JobsGO (`jobsgo`) | HTML SSR (`div.job-card`) | Cloudflare đôi khi thử thách; đã bỏ giả Googlebot, dùng UA thường | batch 2 s; live 1 trang | N ("cập nhật", là lần làm mới, không phải ngày đăng) / - / l | **403 từ IP AWS**; nên chạy runner ngoài | batch + live | Chạy `blocked` thì tự tạm dừng |
| CareerLink (`careerlink`) | HTML SSR Next.js (`/vieclam/list`), `<time datetime>`; fallback JSON-LD | Không có Sitemap trong robots | batch 2 s; live 1 trang | N / - / l | Từng trả trang rỗng từ AWS (theo dõi bằng health) | batch + live | Link tin gốc |
| ITviec (`itviec`) | HTML SSR (`div.job-card`, 20 thẻ/trang, `scrape_latest`); fallback sitemap + JSON-LD | Lương chỉ hiện khi đăng nhập | batch 2 s; live 1 trang | N ("Posted ... ago") / - / không (ẩn lương) | Không ghi nhận | batch + live | Chỉ IT |
| Việc Làm Tốt (`chotot`) | API JSON công khai `gateway.chotot.com/v1/public/ad-listing` (limit <= 50, `scrape_latest`) | API công khai của site | batch 2 s; live 1 request | N (`list_time`) / - / L | Không ghi nhận | batch + live | Lao động phổ thông, bán lẻ, kho vận |
| TimViec365 (`timviec365`) | HTML SSR trang danh mục/"mới nhất" (`/viec-lam?page=N`, 24 thẻ/trang) | `/tim-kiem` bị cấm: chỉ dùng trang danh mục | batch 2 s; live timeout 7 s | N / - / l | **403 từ IP AWS** | batch + live | Từ khóa ánh xạ sang danh mục gần nhất, lọc phía client |
| Jooble (`jooble`) | API aggregator `POST jooble.org/api/{key}` | Cần API key (`jooble_api_key`); điều khoản API Jooble | batch 2 s; live 1 request | N / - / l | Không ghi nhận | batch + live (chỉ khi có key) | Aggregator: giữ link về Jooble/tin gốc theo điều khoản API |
| TopDev (`topdev`) | API JSON `api.topdev.vn/td/v2/jobs` (page_size <= 50) | API công khai phía sau CDN Cloudflare | batch 2 s; live timeout 6 s | N / H / L (có cấu trúc) | Không ghi nhận | batch + live | Chỉ IT |
| Indeed VN (`indeed`) | GraphQL `POST apis.indeed.com/graphql` (public app key, <= 100 tin) | **ToS cấm cào**; khóa app có thể bị xoay; chỉ chạy lượng thấp | <= 1 req/s, tối đa 2 trang/từ khóa; live timeout 8 s | N / - / L (~60-80% tin có lương) | Không ghi nhận (local 0,8-1,2 s) | batch + live | **Bắt buộc ghi nguồn "Indeed" và link về tin gốc; không đăng lại nội dung**; bỏ nguồn nếu lỗi lặp lại |
| LinkedIn (`linkedin`) | Guest endpoint `jobs-guest/jobs/api/seeMoreJobPostings/search` (HTML, 10 thẻ/trang) | **ToS cấm cào**; không đăng nhập | <= 1 search/5 s, 1 trang/từ khóa; hay SSL EOF/429 | N (`<time>`) / - / không | Hay bị throttle; cần runner ngoài nếu lỗi | chỉ batch | **Bắt buộc ghi nguồn "LinkedIn" và link về tin gốc**; best-effort, tự tạm dừng khi lỗi |
| 123job (`123job`) | HTML SSR Laravel/Livewire `/tuyen-dung?q=&page=` (30 thẻ/trang); fallback JSON-LD | robots chỉ cấm `/ajax`, `/apply`; sitemap rỗng | batch 2 s; live timeout 5 s | N ("Đăng: 2 giờ trước") / - / l | Không ghi nhận | batch + live | Không lọc được thành phố phía server, lọc phía client |
| JobOKO (`joboko`) | HTML SSR trang SEO `/tim-viec-lam-<slug>` và `/viec-lam-moi` | Cấm `/jobs?*`, `/s/*`, `/ajax/`; chỉ dùng trang SEO được phép | batch 2 s; live timeout 6 s | **H (`item-date` là hạn nộp)**; N không có / l | Không ghi nhận | batch + live | Hạn nộp lưu ở `valid_through_raw` |
| Vieclam.gov.vn (`vieclam_gov`) | API JSON `POST api.vieclam.gov.vn/.../nld/vieclam` không cần đăng nhập | robots cho `/search/`, `Crawl-delay: 1` | >= 1 s/request; live timeout 5 s | N (`ngay_duyet`, đôi khi null) / H (`ngay_het_han`) / l | **Timeout kết nối từ AWS** (đã thấy) | batch + live | Cổng quốc gia, ít tin (khoảng 70 cho "kế toán") |
| Viecoi (`viecoi`) | Sitemap `viecoi.vn/job.xml` + JSON-LD chi tiết (`GenericJsonLdScraper`) | Cloudflare nhưng không chặn; trang ~1,6 MB, sitemap ~25 s | tối đa 100 trang chi tiết/lần batch, 2 s | N / H / L (JSON-LD `JobPosting` nếu có) | Không ghi nhận | chỉ batch | Không phù hợp live vì chậm |
| YBox (`ybox`) | HTML SSR một trang (`window.__INITIAL_ADS__`, ~42 tin, `scrape_latest`) | Bỏ qua tham số tìm kiếm, lọc cục bộ | 1 request | N / - / l | Không ghi nhận | batch + live | Chủ yếu thực tập, part-time cho sinh viên |
| TopCV (`topcv`) | Qua chỉ mục You.com (`include_domains=topcv.vn`), không đọc trực tiếp | TopCV trả 403 cho mọi client không phải trình duyệt (cả sitemap) | Tính vào ngân sách You.com (`VIECLAMBOT_YOU_DAILY_LIMIT`, mặc định 200 request/ngày) | N (`page_age`) / - / không (chỉ tiêu đề + snippet) | Cloudflare chặn truy cập trực tiếp | chỉ batch (cần `you_search_enabled`); live đã có /search You.com | Ghi công "Web · TopCV" và link tin gốc |

## Nguồn dùng chung hoặc chưa là nguồn riêng

| Nhóm | Trạng thái |
|---|---|
| You.com Web Search (`src/web_search/you_search.py`) | Dùng trong live `/search` trên các domain TopCV, VietnamWorks, CareerViet, JobsGO, ViecLam24h, TopDev; giới hạn 200 request/ngày (item DynamoDB `BUDGET`/`YOU#<ngày>`) |
| `GenericJsonLdScraper` | Dự phòng tự động cho CareerViet, ViecLam24h, CareerLink, ITviec, 123job khi parser riêng trả 0 tin; là scraper chính của Viecoi |
| Glints, Careerjet, Jobhopin, 1900.com.vn, MyWork, TimViecNhanh, Vieclam.tuoitre, Xóm Jobs, Glassdoor | Không tích hợp (robots cấm, 403, site chết hoặc trùng nguồn); lý do ở các tài liệu khảo sát |
| API remote (Himalayas, Remotive, Arbeitnow, Jobicy, RemoteOK) và ATS (Greenhouse, Lever, Ashby...) | Trong kế hoạch v2, chưa có trong registry. Khi thêm: **Himalayas/Remotive/Jobicy/RemoteOK phải link-back về trang gốc và ghi nguồn**; RemoteOK trễ 24 giờ và cấm đăng lại lên board khác |

## Vận hành

- Nguồn chặn IP AWS (JobsGO, TimViec365, Vieclam.gov.vn) nên chuyển sang runner ngoài AWS; trong lúc đó cơ chế tạm dừng
  sau 5 lần lỗi giữ cho lần chạy không bị kéo dài.
- Probe thủ công: `python scripts/test_scrapers_live.py "kế toán" [--live] [--latest] [--location "Hà Nội"] [--json]`.
- Fixture parser được ghi ngày chụp trong `tests/fixtures/MANIFEST.json`; quá 120 ngày thì
  `tests/test_fixtures_are_fresh.py` báo lỗi, xem `scripts/refresh_fixtures.py` để biết cách chụp lại.
