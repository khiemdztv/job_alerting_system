# Khảo sát nguồn việc làm Việt Nam chưa tích hợp (30/10/2026)

Phương pháp: `requests` + UA Chrome 125, >=1 s giữa các request, <=25 request/site, không dùng trình duyệt headless, curl_cffi, proxy hay giải CAPTCHA. 403/Cloudflare thì ghi nhận rồi bỏ qua. Số liệu "khối lượng" lấy từ tiêu đề/phân trang của chính trang tìm kiếm.

## Tổng hợp

| # | Nền tảng | HTTP (requests+UA) | Độ khả thi | Cách truy cập khuyến nghị | Trạng thái |
|---|---|---|---|---|---|
| 1 | 123job.vn | 200 | **easy** | HTML SSR `/tuyen-dung?q=&page=` (30 thẻ/trang, ~7000 kế toán) | **Đã làm adapter** `job123_scraper.py` |
| 2 | JobOKO (vn.joboko.com) | 200 | **easy** | HTML SSR trang SEO `/tim-viec-lam-<slug>?p=` (20/trang; 7040 kế toán, 15793 NVKD) + `/viec-lam-moi` (100/trang) | **Đã làm adapter** `joboko_scraper.py` |
| 3 | vieclam.gov.vn | 200 (shell SPA rỗng) | **easy** (qua JSON API) | `POST api.vieclam.gov.vn/.../nld/vieclam` không cần đăng nhập; ít tin (72 kế toán) | **Đã làm adapter** `vieclam_gov_scraper.py` |
| 4 | Viecoi.vn | 200 (Cloudflare nhưng không chặn) | **medium** | HTML SSR + JSON-LD, nhưng mỗi trang 2.6 MB (Next.js RSC); 30 thẻ/trang; ~4.7k kế toán, ~7.1k NVKD | Chưa làm (trang nặng, đã có `viecoi_detail` trong generic JSON-LD; nên dùng sitemap `job.xml` + JSON-LD) |
| 5 | Glints VN | 200 (Cloudflare) | **hard (chính sách)** | `__NEXT_DATA__` đầy đủ 30 tin/trang, NHƯNG robots.txt cấm `*/opportunities/jobs/explore?*` (toàn bộ URL tìm kiếm) | Không làm; chỉ có thể dùng sitemap + trang chi tiết (JSON-LD) |
| 6 | Jobhopin | **403** Cloudflare (cả robots.txt) | **blocked** | Không có đường hợp lệ | Bỏ |
| 7 | Careerjet VN | 200 nhưng chỉ trả stub 11 KB (không có thẻ tin) | **blocked** (web) / API affiliate | Chỉ API affiliate chính thức; robots cấm `/search/query.html`, `/job/` | Chỉ ghi chép |
| 8 | 1900.com.vn | DNS không phân giải (ConnectionError, cả `www`/http) | **blocked** (site chết) | - | Bỏ |
| 9 | MyWork.com.vn | Redirect sang vieclam24h.vn (timeout khi theo redirect) | **n/a** | Đã gộp vào Vieclam24h (đã tích hợp) | Bỏ |
| 10 | TimViecNhanh | 200 sau 301 `timviecnhanh.com` -> `vieclam24h.vn?redirect_from=tvn...` | **n/a** | Đã gộp vào Vieclam24h | Bỏ |
| 11 | Vieclam.tuoitre.vn | 200 | **medium/n/a** | Chạy trên hạ tầng CareerViet (robots: `Sitemap: careerviet.vn/...`, cấm `/vi/jobs/*`); trùng kho tin CareerViet đã tích hợp | Bỏ (trùng) |
| 12 | Xóm Jobs (xomjobs.com/.vn) | ConnectionError (domain không phản hồi) | **blocked** (site chết) | - | Bỏ |

Jobtest không khảo sát (giới hạn 12 nền tảng).

## Bảng chi tiết

### 1. 123job.vn - easy

| Hạng mục | Kết quả |
|---|---|
| Truy cập | 200, nginx, không Cloudflare |
| robots.txt | `Disallow: /ajax`, `/apply`; listing/search được phép |
| Newest | `https://123job.vn/tuyen-dung` (mới nhất trước; "90000+ việc làm") |
| Search | `https://123job.vn/tuyen-dung?q=<kw>`; tham số `city=` bị bỏ qua -> lọc địa điểm phía client |
| Phân trang | `&page=N`, 30 thẻ/trang |
| Dạng dữ liệu | HTML SSR (Laravel + Livewire/Alpine); không có `__NEXT_DATA__`/JSON nhúng |
| Chi tiết | `/viec-lam/<slug>-<10 ký tự>`; có JSON-LD `JobPosting` |
| Sitemap | `Sitemap: /sitemap.xml` nhưng sitemapindex RỖNG |
| Trường trên thẻ | title, công ty, lương (`__` = không hiện), địa điểm (huyện, tỉnh), "Đăng: 2 giờ trước"; không có hạn nộp |
| Khối lượng | kế toán ~7000+, nhân viên kinh doanh ~6700+ |
| Truy cập khuyến nghị | `requests` + bs4, 1 request/trang |

### 2. JobOKO - easy

| Hạng mục | Kết quả |
|---|---|
| Truy cập | 200, IIS, không Cloudflare |
| robots.txt | Cấm `/jobs?*`, `/s/*`, `/ajax/`; trang SEO `/tim-viec-lam-*` và `/viec-lam-moi` được phép; chặn nhiều bot SEO |
| Newest | `/viec-lam-moi` (thẻ gọn, 100 thẻ/trang, `?p=N`) |
| Search | `/tim-viec-lam-<slug>` (chỉ slug đã index: `nhan-vien-ke-toan` có, `ke-toan` 404); địa điểm `/tim-viec-lam-<slug>-tai-<tp>` (chỉ thành phố lớn) |
| Phân trang | `?p=N`, 20 thẻ/trang (trang từ khóa) |
| Dạng dữ liệu | HTML SSR (`div.item[data-jid]`); không JSON nhúng |
| Chi tiết | `-xvi<id>`; có JSON-LD `JobPosting` (datePosted, validThrough, baseSalary) |
| Sitemap | không có (`/sitemap.xml`, `/sitemap_index.xml` 404; robots không khai báo) |
| Trường trên thẻ | title, công ty, địa điểm, lương, vài gạch đầu dòng mô tả, **hạn nộp** (`item-date`, KHÔNG phải ngày đăng) |
| Khối lượng | kế toán 7040, nhân viên kinh doanh 15793 (tiêu đề trang) |
| Truy cập khuyến nghị | `requests` + bs4; thứ tự trang từ khóa có tin HOT/UP lên trước nên không phải "mới nhất" |

### 3. vieclam.gov.vn - easy (API)

| Hạng mục | Kết quả |
|---|---|
| Truy cập | 200 nhưng HTML chỉ là shell Vite/React (`<div id="root">`); bundle `/assets/index-*.js` lộ API |
| robots.txt | `Allow: /search/`, `/search/job-detail/`; `Crawl-delay: 1` |
| Newest | cùng API với `tencv: ""`, mới duyệt trước |
| Search | `POST https://api.vieclam.gov.vn/svl-admin/webhook/api/svl/nld/vieclam?page_num=N&page_size=20`, body `{"tencv": kw, "noi_lam_viec": [id_tỉnh]}`; danh sách tỉnh `GET .../api/svl/tinh_thanhpho/list` (đã xác minh: `noi_lam_viec` phải là mảng) |
| Phân trang | `page_num` (từ 1), `page_size` 20; phản hồi có `pagination.total_pages/total_items` |
| Dạng dữ liệu | JSON API công khai, không cần token/cookie |
| Chi tiết | `/search/job-detail?id=<id>` (SPA, không JSON-LD) |
| Sitemap | không có |
| Trường | vị trí, công ty, mức lương (khoảng chữ), tỉnh, hình thức, kinh nghiệm, chức vụ, ngành, quy mô, `ngay_duyet` (ngày đăng; đôi khi null), `ngay_het_han` (hạn) |
| Khối lượng | kế toán 72 tin; ít hơn các board thương mại (việc phổ thông, nhà nước) |
| Truy cập khuyến nghị | gọi JSON API trực tiếp, 1 request/trang, ~0.5 s |

### 4. Viecoi.vn - medium

| Hạng mục | Kết quả |
|---|---|
| Truy cập | 200, Cloudflare nhưng không thách thức; trang 2.6 MB |
| robots.txt | Cho phép `/tim-viec/*.html`, `/viec-lam/*.html`, `?page=`; cấm `/employer/`, `/jobseeker/`, `/index.php` |
| Newest | `/tim-viec/all.html` (1961 trang x 30 = ~58.8k) |
| Search | `/tim-viec/all.html?keyword=<kw>` redirect 301 sang `/tim-viec/key-<slug>.html` |
| Phân trang | `?page=N`, 30 `article.public-job-card`/trang |
| Dạng dữ liệu | HTML SSR + Next.js RSC flight data (không phải `__NEXT_DATA__`) |
| Chi tiết | `/viec-lam/<slug>-<id>.html`, 1.6 MB, có JSON-LD `JobPosting` (datePosted, validThrough) |
| Sitemap | `sitemap.xml` (index: `job.xml`, `job-closed.xml`, `jobsearch*.xml`...) |
| Khối lượng | kế toán ~156 trang (~4.7k), NVKD ~237 trang (~7.1k) |
| Ghi chú | Khả thi nhưng nặng cho Lambda (2.6 MB/trang); ưu tiên sitemap `job.xml` + JSON-LD (đã có generic JSON-LD scraper) |

### 5. Glints VN - hard (chính sách robots)

| Hạng mục | Kết quả |
|---|---|
| Truy cập | 200, Cloudflare, trang 0.8 MB |
| robots.txt | `Disallow: */opportunities/jobs/explore?*` (URL tìm kiếm/lọc), `*/en/opportunities/jobs/explore?*`; `Sitemap: /sitemap_index.xml`, `/sitemaps/explore-page-id-sitemap.xml` |
| Newest/Search | `/vn/opportunities/jobs/explore?keyword=<kw>&country=VN&locationName=...` (bị robots cấm) |
| Phân trang | Apollo cache `searchJobsV3` `page`/`pageSize=30`, `hasMore` |
| Dạng dữ liệu | `__NEXT_DATA__` (`props.pageProps.initialJobs.jobsInPage`, 30 tin: title, company, salaries min/max, city, createdAt...) - kỹ thuật dễ |
| Chi tiết | `/vn/opportunities/jobs/<uuid>` 200; có JSON-LD `JobPosting` |
| Khối lượng | ~79 tin/từ khóa theo kế hoạch; trang kế toán trả 30/trang với `hasMore=true` |
| Ghi chú | Đã gửi 1 request tìm kiếm khi khảo sát (chỉ để đo, không dùng sản xuất). Không xây adapter vì vi phạm robots; nếu cần: sitemap + trang chi tiết JSON-LD |

### 6. Jobhopin - blocked

| Hạng mục | Kết quả |
|---|---|
| Truy cập | **403** Cloudflare cho `/` và `/robots.txt` |
| Ghi chú | Kế hoạch nhắc API `admin.jobhop.vn/api/public/jobs/` (từ repo bên thứ ba); chưa kiểm vì không phải đường truy cập hợp lệ; cần xác minh riêng nếu muốn |

### 7. Careerjet VN - blocked (web), API affiliate

| Hạng mục | Kết quả |
|---|---|
| Truy cập | `careerjet.vn` và `careerjet.com.vn` 200 nhưng chỉ trả stub 11 KB (header, không có thẻ tin), kể cả `/jobs?s=kế+toán` |
| robots.txt | Cấm `/search/query.html`, `/job/`, `/jobview/`, `/apply/` |
| Truy cập khuyến nghị | **API đối tác chính thức** (`locale_code=vi_VN`, cần affiliate key; bắt buộc truyền IP/UA người dùng cuối; điều khoản không cho dựng dataset). Không cào web |

### 8-10, 12. Đã chết / sáp nhập

| Nền tảng | Kết quả |
|---|---|
| 1900.com.vn | `nslookup` không có bản ghi; HTTP/HTTPS, `www` đều ConnectionError |
| MyWork.com.vn | Redirect về `vieclam24h.vn` (robots.txt phục vụ bởi Cloudflare, chặn AI bot); coi là Vieclam24h |
| TimViecNhanh | `301 -> vieclam24h.vn?redirect_from=tvn&role=2&utm_source=tvn&utm_medium=redirect&utm_campaign=merge` |
| Xóm Jobs | `xomjobs.com`, `www.xomjobs.com`, `xomjobs.vn` đều ConnectionError |

### 11. Vieclam.tuoitre.vn - trùng nguồn

| Hạng mục | Kết quả |
|---|---|
| Truy cập | 200 (openresty), trang chủ 1.4 MB, có 2 JSON-LD `JobPosting` |
| robots.txt | `Sitemap: https://careerviet.vn/sitemap/sitemap.xml`; cấm `/vi/jobs/*`, `/qckiemviec/`, `/api/*`; cho phép Googlebot/ClaudeBot/GPTBot, chặn CCBot... |
| Ghi chú | Là cổng đồng thương hiệu với CareerViet (đã tích hợp) nên không thêm giá trị |

## Khuyến nghị đăng ký registry (không sửa trong lượt này)

| Nguồn | label | batch | live | live_priority | live_timeout |
|---|---|---|---|---|---|
| `Job123Scraper` | "123job" | True | True | 20 (sau Vieclam24h/CareerViet) | 5.0 s (đo 0.45-0.6 s) |
| `JobokoScraper` | "JobOKO" | True | True | 25 | 6.0 s (0.9 s vì có 404 retry) |
| `VieclamGovScraper` | "Vieclam.gov.vn" | True | True | 40 (ít tin, ưu tiên thấp) | 5.0 s (~0.5 s) |

Cả ba đều có `scrape(keyword, max_pages, location=None)` và `scrape_latest(max_pages, location=None)`. `last_error="layout_changed"` khi HTTP 200 nhưng 0 thẻ; JobOKO đặt `no_listing_for_keyword` khi cả hai slug 404. Nên cho `scrape_latest` chạy trong batch định kỳ (đường "mới nhất") thay vì theo từ khóa.
