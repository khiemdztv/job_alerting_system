# ViecLamBot v2 — Kế hoạch Big Update

Ngày lập: 30/10/2026. Trạng thái: bản đề xuất, chưa triển khai.

Mục tiêu của tài liệu: đưa bot từ "demo chạy được" thành sản phẩm dùng hằng ngày:
phủ gần hết tin tuyển dụng công khai ở Việt Nam, tìm/so khớp chính xác hơn, menu đủ
chức năng cho người tìm việc, vận hành ổn định với chi phí thấp.

---

## 0. Tóm tắt điều hành

| Hạng mục | Hiện tại | Mục tiêu v2 |
|---|---|---|
| Nguồn tin | 12 đăng ký, 7 chạy ổn từ máy cá nhân, 3 bị chặn từ IP AWS, TopCV chỉ qua You.com | 25+ nguồn: thêm Indeed VN, LinkedIn, Glassdoor, Glints, Careerjet, JobOKO, 123job, 1900.com.vn, Viecoi, trang career công ty (ATS), API remote (Himalayas/Remotive), cổng việc làm quốc gia vieclam.gov.vn |
| Cách cào | Tìm theo 25 từ khóa seed xoay vòng mỗi 6h (mỗi nhóm được cào 1 lần/30h) | Cào trang "việc mới nhất", sitemap, RSS, API ẩn → lấy toàn bộ tin mới, không phụ thuộc từ khóa |
| Chống chặn | UA rotation | curl_cffi impersonate + proxy residential VN cho nguồn Cloudflare + runner ngoài AWS cho nguồn chặn dải IP AWS |
| Tìm kiếm | Scan toàn bảng DynamoDB mỗi lần, keyword + synonym | Chỉ mục full-text (SQLite FTS5/Postgres) + embedding tiếng Việt, lọc lương/cấp bậc/hình thức/remote |
| Dữ liệu tin | Thẻ listing, mô tả bị cắt 2000 ký tự, kỹ năng chỉ nhận IT | Fetch trang chi tiết, trích xuất cấu trúc (kỹ năng, cấp bậc, kinh nghiệm, lương, phúc lợi) bằng rule + LLM |
| Menu bot | 12 lệnh, 8 nút reply keyboard | Hồ sơ người dùng, bộ lọc, lưu/ẩn tin, inline button mỗi tin, tần suất thông báo, theo dõi công ty, thống kê thị trường |
| Vận hành | Test unit, /health, log CloudWatch | CI GitHub Actions, fixture HTML từng nguồn, dashboard sức khỏe nguồn, cảnh báo thay đổi layout, giới hạn chi phí |

Ước tính: 6 giai đoạn, ~10–12 tuần nếu 1 người làm bán thời gian. Giai đoạn 1–2 (coverage + chống chặn)
mang lại giá trị lớn nhất và nên làm trước.

---

## 1. Hiện trạng (audit ngày 30/10/2026)

### 1.1 Kiến trúc
- 4 Lambda (scraper 6h, ETL qua SQS, matcher 6h+15m, webhook Telegram), DynamoDB 2 bảng, S3 raw, EventBridge.
- 83 test pass. Code sạch, có registry nguồn thống nhất, live search song song có deadline, ledger `SENT#` chống gửi lặp.

### 1.2 Nguồn và kết quả probe thật (`scripts/test_scrapers_live.py "kế toán" --live`, máy cá nhân)

| Nguồn | Kết quả | Ghi chú |
|---|---|---|
| VietnamWorks | 50 tin, 3.9s | Đọc JSON trong script tag; có Algolia API phía sau |
| CareerViet | 50 tin, 1.3s | HTML SSR, ổn |
| ViecLam24h | 30 tin, 2.6s | HTML, ổn |
| JobsGO | 50 tin, 1.5s | OK local, **403 từ AWS** |
| Việc Làm Tốt (Chợ Tốt) | 50 tin, 1.1s | API JSON, ổn |
| ITviec | 2 tin | Chỉ IT, ổn |
| YBox | 2 tin | Thực tập/sinh viên |
| CareerLink | 0 tin | Trang trả về rỗng, cả local lẫn AWS |
| TimViec365 | timeout | Chậm, 403 từ AWS |
| TopDev | timeout | Server chậm thường xuyên |
| TopCV | không cào trực tiếp | Cloudflare 403, đang đọc qua You.com (tốn credit, thiếu lương/địa điểm) |
| Jooble | cần API key | API, giới hạn free |

### 1.3 Điểm yếu chính
1. **Coverage theo từ khóa**: scraper chỉ tìm 25 từ khóa seed + từ khóa đăng ký. Tin không chứa từ khóa đó không bao giờ vào DB. Mỗi nhóm seed chỉ được cào 1 lần/30 giờ.
2. **Thiếu các nền tảng lớn**: LinkedIn, Indeed, Glassdoor, JobStreet, Glints, JobOKO, 123job, 1900.com.vn, trang career công ty, cổng nhà nước, nhóm Facebook/Telegram.
3. **Bị chặn từ AWS**: JobsGO, TimViec365 (403), TopCV (Cloudflare), CareerLink (rỗng). Không có proxy, không có TLS impersonation.
4. **Tìm kiếm không mở rộng được**: `search_jobs` scan toàn bảng (hiện ~10k tin/run; với 25 nguồn sẽ 50–100k tin) rồi xếp hạng trong Lambda. Tốn RCU và thời gian, sẽ chạm trần webhook 30s.
5. **So khớp nông**: chỉ keyword + synonym thủ công; không có cấp bậc (intern/junior/senior), kinh nghiệm, hình thức, remote, lương theo đăng ký (có `salary_min_filter` trong model nhưng bot chưa cho nhập), không loại trừ từ khóa, không blacklist công ty.
6. **Dữ liệu nghèo**: chỉ lấy thẻ listing, không fetch chi tiết; `TECH_SKILLS` chỉ bao IT; ngày đăng nhiều nguồn là ngày cào; trùng tin giữa các board chỉ dựa URL.
7. **UX bot**: không có inline button (lưu, ẩn, "tin tương tự"), không hồ sơ người dùng, không chọn tần suất/giờ nhận tin, không xem lịch sử tin đã nhận, không feedback để học sở thích.
8. **Vận hành**: không CI; không fixture HTML để phát hiện layout đổi; health chỉ ở `/health`; không có ngân sách/giới hạn chi phí You.com, proxy.

---

## 2. Nghiên cứu giải pháp hiện có (GitHub, Hugging Face, Kaggle, sản phẩm)

Khảo sát ngày 30/10/2026. Số sao/ngày cập nhật lấy từ trang GitHub/HF hôm nay; mục nào chưa mở được có ghi rõ.

### 2.1 Thư viện cào việc và API công khai (GitHub)

| Dự án | Tình trạng | Dùng được gì cho ta |
|---|---|---|
| [JobSpy](https://github.com/speedyapply/JobSpy) (`python-jobspy`, MIT, ~4.6k★, cập nhật 10/2026) | LinkedIn, Indeed, Glassdoor, ZipRecruiter, Bayt, Naukri…; `country_indeed="Vietnam"` được hỗ trợ (Glassdoor cũng có VN). Indeed dùng GraphQL nội bộ, HTTP thuần, không giới hạn mạnh; LinkedIn 429 khi burst; Glassdoor ~2 req/s; trần ~1000 tin/truy vấn | Nguồn Indeed VN + LinkedIn VN gần như "cắm là chạy". Pandas nặng cho Lambda → vendor riêng `jobspy/indeed` hoặc chạy ở external runner |
| [JobFunnel](https://github.com/PaulMcInnis/JobFunnel) | **Archived 12/2025** vì các board chống bot quá mạnh | Bài học: thiết kế blocklist công ty/tiêu đề, master CSV dedup; không dùng code |
| [AIHawk](https://github.com/feder-cr/Jobs_Applier_AI_Agent_AIHawk) (AGPL, ~29k★) | Auto-apply đã gỡ, LinkedIn automation dừng theo yêu cầu LinkedIn, push cuối 5/2025 | Không dùng; chỉ tham khảo prompt CV↔JD |
| [Hunter](https://github.com/frankyu77/Hunter) | Poll ATS (Greenhouse, Ashby, Lever, Workday, SmartRecruiters, Workable…), dedup `seen_jobs.json`, Telegram alert, GitHub Actions cron, tự khám phá board công ty (≤3/tuần), **tạm dừng nguồn sau 5 lần lỗi**, tóm tắt tuần | Mẫu cho external runner + quản trị sức khỏe nguồn |
| [JobNavigator](https://github.com/vesaias/JobNavigator) (MIT, 168★) | FastAPI+Postgres, 13 ATS qua Playwright, JobSpy, Apify; dedup posting-key xuyên 30+ hệ thống; chấm điểm AI (Claude/OpenAI/Ollama, chế độ nhẹ/đầy đủ, prompt caching); Telegram: tin mới, digest ngày, cảnh báo sức khỏe cào, **inline action**, auto-reject rule | Tham chiếu trực tiếp cho UX bot v2 (mục 8) và dedup (mục 6.3) |
| ATS JSON API công khai | Greenhouse `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`, Lever `api.lever.co/v0/postings/{slug}?mode=json`, Ashby `api.ashbyhq.com/posting-api/job-board/{slug}`, SmartRecruiters `api.smartrecruiters.com/v1/companies/{id}/postings`, Workable widget | Nguồn tin công ty đa quốc gia tuyển ở VN (Grab, Shopee, Atlassian…), chạy tốt từ Lambda, lọc `location` chứa Vietnam/Ho Chi Minh/Hanoi |

API aggregator (free tier và phủ VN): **Adzuna** không có VN. **Jooble** có vn.jooble.org (đã tích hợp). **JSearch** (RapidAPI, Google for Jobs) free 100–200 req/tháng, Pro ~25 USD/10k. **Careerjet** có locale `vi_VN`/`en_VN`, key affiliate miễn phí, [client Python chính thức](https://github.com/careerjet/careerjet-api-client-python), phải chuyển tiếp IP/UA người dùng cuối. Nguồn remote không cần key: **Himalayas** (`himalayas.app/jobs/api/search?q=&country=VN`), **Remotive**, **Arbeitnow**, **Jobicy**, **RemoteOK** (trễ 24h, cấm đăng lại lên board khác, phải link-back). Reed/USAJobs/The Muse không liên quan VN.

### 2.2 Dự án cào board Việt Nam

| Dự án | Nội dung | Giá trị |
|---|---|---|
| [vnk8071/goodjobs](https://github.com/vnk8071/goodjobs) (MIT, cập nhật 10/2026) | FastAPI+Redis "một ô tìm → mọi board": HTML tĩnh cho ITviec, TopCV, VietnamWorks, TopDev, JobsGO, CareerLink, CareerViet, ViecOi, Indeed VN, Xóm Jobs; Chromium cho LinkedIn, Glints, Google Careers; LLM chuẩn hóa ý định/thành phố và tóm tắt; embedding "việc liên quan"; regex gắn kỹ năng; warm-up 30 cặp từ khóa×thành phố mỗi 2h | **Dự án giống ViecLamBot nhất**; mỏ parser và ghi chú debug từng board |
| [La1do/job-scraper](https://github.com/La1do/job-scraper) | TopCV (HTTP → fallback Chromium khi 403), ITviec, CareerViet; chế độ URL bất kỳ đọc **JSON-LD schema.org JobPosting** rồi OpenGraph; schema 30 cột kiểu LinkedIn (salary min/max/med, currency, pay_period, remote…) | Mẫu cho scraper fallback không cần selector + chuẩn hóa lương |
| [tcd93/python-web-scraping](https://github.com/tcd93/python-web-scraping) | VietnamWorks qua **Algolia index `vnw_job_v2`** (app id + search key lộ trong DevTools); ITviec cần cookie `_ITViec_session` để thấy lương; JobHopin API `admin.jobhop.vn/api/public/jobs/` | API ẩn ổn định nhất cho VietnamWorks |
| epsi10nvn/vn-job-data-crawler, Eakan-Git/TopCV-scraper, tienlonghungson/IT-Jobs-TopCV-Crawler, PyPI `ghibli-job-crawler` | Scrapy/Selenium/Playwright cho TopCV, VietnamWorks | Đều xác nhận TopCV trả 403 cho HTTP thuần; chỉ qua được bằng trình duyệt thật hoặc proxy residential |
| Apify actors (TopCV, VietnamWorks, CareerViet, CareerLink, Glints, JobStreet) | Trả phí theo tin (~<1 USD/200 tin); actor Glints đọc dữ liệu Next.js nhúng; actor CareerLink đọc JSON-LD; `anxuanng/vietnam-jobs-scraper` đã DEPRECATED | Dự phòng khi nguồn hỏng; cách họ lấy dữ liệu (JSON-LD, Next.js data) đáng học |

Kiểm tra trực tiếp hôm nay (HTTP thuần, UA Chrome): Glints VN 200, 123job 200; jobstreet.vn **301 về jora.com** (Jora VN trả 403), TopCV sitemap 403, Indeed VN 403, JobsGO RSS 403 (chặn TLS fingerprint curl, scraper requests của ta vẫn qua), CareerLink sitemap 404, JobOKO/1900/vieclamvietnam.gov.vn không phản hồi đúng URL thử. Cổng việc làm quốc gia mới là **vieclam.gov.vn** (Bộ Nội vụ, 2025); data.gov.vn chưa có dataset việc làm. Facebook group: Graph API không trả feed nhóm cho app bên thứ ba.

### 2.3 Hugging Face — dataset và mô hình

- **[VietJobs](https://huggingface.co/datasets/dinhieufam/VietJobs)** (VinNLP, LREC 2026, MIT): **48.092 tin tuyển dụng tiếng Việt** (7–10/2025, 34 tỉnh), 18 cột gồm category, qualifications, technical_skills, soft_skills, languages, experience, salary min/max/avg, contract_type, benefits, requirements. Benchmark phân loại ngành và ước lượng lương. → Bộ dữ liệu huấn luyện/kiểm thử tốt nhất cho phân loại nghề, parse lương, từ điển kỹ năng, fixture dedup.
- Skill extraction: `jjzha/jobbert_skill_extraction`, `TechWolf/JobBERT-v2` (MIT, 1024-d, title→skill), `TechWolf/ConTeXT-Skill-Extraction-base` (ESCO), `nestauk/ojd_daps_skills`, `AnasAito/SkillNER` — **toàn tiếng Anh**; dùng cho tin IT (phần lớn tiếng Anh) hoặc sau khi dịch; tiếng Việt dùng từ điển regex xây từ cột `technical_skills` của VietJobs.
- Embedding tiếng Việt (VN-MTEB, arXiv 2507.21500, điểm retrieval/tổng): m-e5-large-instruct 560M 40.9/68.0; bge-m3 568M 39.8/64.9; **gte-multilingual-base 305M 38.4/65.2**; AITeamVN/Vietnamese_Embedding 34.2/63.3 (mạnh ở legal); **multilingual-e5-small 118M 34.1/60.7**; bkai vietnamese-bi-encoder 135M 25.4/54.9 (cần pyvi). Trên Lambda: e5-small (~120 MB int8 ONNX) hoặc gte-base chạy được trong container 1–2 GB; bge-m3/e5-large nên gọi qua API (Bedrock/Cohere multilingual/HF Endpoint). Rerank: `AITeamVN/Vietnamese_Reranker`.

### 2.4 Kaggle

`arshkon/linkedin-job-postings` (~124k+100k tin, có job_skills, salaries, benefits, ODbL) — tốt cho thiết kế schema 30 cột và test chuẩn hóa lương. Các bộ Indeed/Glassdoor khác thiên Mỹ. Notebook điển hình: SBERT MiniLM + TF-IDF cosine CV↔JD; chưa thấy notebook VN đáng chú ý. "Vietnam Jobs Dataset (Nguyen 2025)" chỉ có tiêu đề.

### 2.5 Sản phẩm để so chuẩn tính năng

Teal (tracker không giới hạn, extension 40+ board, chấm điểm từ khóa, parse email alert), Huntr (kanban, free 100 việc), Simplify (autofill), Careerflow (~24 USD/tháng), LinkedIn alert (ngay/ngày/tuần, trễ trung vị ~31h so với trang career), Google Jobs alert, Indeed (bỏ RSS 4/2025), Glassdoor (2 lần/ngày). Bot mã nguồn mở: JobNavigator (trên), Gold Rush (điểm 0–100 + digest sáng/tối), job_search_tool (dedup URL + company+title), fernandobouchet/job-alert-bot (ngưỡng điểm → kênh Telegram).

**Checklist tính năng rút ra**: saved search đủ bộ lọc; instant vs digest ngày/tuần; dedup xuyên board (URL + company+title+location chuẩn hóa + hash nội dung); blacklist công ty / auto-reject; điểm hợp CV kèm giải thích; chuẩn hóa lương + insight; theo dõi ứng tuyển; kiểm tra "còn tuyển không" bằng re-fetch; cảnh báo sức khỏe cào; xem cả tin đã bị lọc; inline button lưu / không quan tâm / đã nộp.

### 2.6 Hạ tầng cào và chống chặn

- **[Scrapling](https://github.com/D4Vinci/Scrapling)** (BSD-3, ~90k★): `Fetcher` (TLS impersonation trên curl_cffi), `StealthyFetcher` (Chromium giả fingerprint, tuyên bố qua Cloudflare Turnstile), selector thích nghi khi layout đổi. Tầng trình duyệt cần container.
- **curl_cffi**: wheel nhỏ, chạy trong Lambda zip, giả JA3 Chrome — thử đầu tiên cho 403 do fingerprint. _Chưa kiểm chứng được trong phiên này với TopCV/Indeed (lệnh thử bị chặn bởi chính sách công cụ); cần người dùng tự chạy `scripts/test_scrapers_live.py` sau khi tích hợp._ Không giải quyết chặn theo IP.
- **Crawlee-python** (Apache-2, ~9k★) và **Scrapy + scrapy-playwright**: framework nặng, không hợp Lambda cold start.
- **Playwright trên Lambda**: [lambda-theatre](https://github.com/dgallitelli/lambda-theatre) (MIT, Python 3.13, image ~1.2 GB, 2048 MB RAM, cold ~4s, warm 0.1–2s, ~0.00015 USD/lần). Mẫu cho Lambda "trình duyệt dự phòng" chỉ dùng cho TopCV/Glints/LinkedIn.
- **FlareSolverr** v3.5 vẫn bảo trì nhưng thua Turnstile mới (5/2026); cần container chạy lâu.
- Dịch vụ trả phí: Zyte API ~0.13–1.27 USD/1k HTTP (min 100 USD/tháng), Bright Data Unlocker ~1.5–2.5 USD/1k, ScraperAPI từ 49 USD/tháng. Ở quy mô vài nghìn trang TopCV/ngày chỉ vài USD/tháng nếu không có mức tối thiểu.
- **RSS/sitemap**: board VN không công bố RSS, nhưng hầu hết có **JSON-LD JobPosting** (bắt buộc cho Google for Jobs) và sitemap XML có `<lastmod>` → cào bằng "diff sitemap + parse JSON-LD" không cần selector, rẻ và bền khi đổi giao diện.

### 2.7 Trích xuất và so khớp bằng LLM

- **Crawl4AI** (Apache-2, ~87k★, v0.9.5 10/2026): `LLMExtractionStrategy` schema Pydantic qua LiteLLM + `JsonCssExtractionStrategy` tự sinh schema; **Firecrawl** (AGPL core, ~197k★) `/scrape` với JSON schema. Cả hai nặng trình duyệt → chỉ mượn ý tưởng, chạy trích xuất trên HTML đã tải.
- PyPI `JobExtractor`, `jobstruct` (có gắn taxonomy kỹ năng), `mazzasaverio/data-job-parser`; bài "AI-JIE" cần 33 lần sửa prompt + bước làm sạch rule → kỳ vọng tương tự.
- So khớp: `srbhr/Resume-Matcher` (Ollama local), `resu-matchr`, ResumeFlow; arXiv 2511.14767 "LLM agent phân tích thị trường IT Việt Nam" (11/2025). goodjobs chỉ dùng LLM cho ý định/thành phố và tóm tắt — cách chia hợp lý để kiểm soát chi phí.

### 2.8 Top 15 thứ tái sử dụng (xếp theo giá trị ÷ công sức)

1. Dataset **VietJobs** — eval/training phân loại nghề, lương, kỹ năng, dedup.
2. Scraper tổng quát **sitemap diff + JSON-LD JobPosting** (mẫu La1do) — một fallback cho hầu hết board VN và board mới.
3. **JobSpy Indeed VN** (HTTP thuần) + LinkedIn guest qua proxy — nguồn lớn, ít công.
4. **curl_cffi** thay `requests.Session` — thử trước khi trả tiền trình duyệt/unblocker.
5. **VietnamWorks Algolia `vnw_job_v2`** — API ẩn ổn định nhất.
6. **goodjobs** parsers + ghi chú debug cho ITviec/TopCV/TopDev/JobsGO/CareerLink/CareerViet/ViecOi/Glints/Indeed VN.
7. **JobNavigator** dedup + UX Telegram (digest, health alert, inline button, auto-reject).
8. **multilingual-e5-small / gte-multilingual-base** ONNX int8 trong container Lambda; bge-m3/AITeamVN qua API khi cần chất lượng.
9. **Himalayas `country=VN` + Remotive + Arbeitnow** — kênh "remote từ Việt Nam" không cần key.
10. **Careerjet `vi_VN`** — aggregator VN miễn phí có client chính thức.
11. **ATS JSON API** + danh sách công ty đa quốc gia tuyển ở VN; học cách Hunter tự mở rộng danh sách.
12. **lambda-theatre** — Lambda Playwright dự phòng cho 403.
13. **Hunter**: tạm dừng nguồn sau 5 lần lỗi + tóm tắt sức khỏe tuần.
14. **JobBERT-v2 / ConTeXT / ojd_daps_skills** cho tin IT tiếng Anh; từ điển kỹ năng VN từ VietJobs cho regex.
15. **Mẫu trích xuất LLM theo schema của Crawl4AI** chỉ cho tin rule không parse được lương/cấp bậc.

Chưa kiểm chứng được: PyPI ghibli-job-crawler, `docs.openapi.vieclam24h.vn` (DNS lỗi; có thể là API đăng tin cho nhà tuyển dụng), giá JSearch, danh sách quốc gia Adzuna chính thức, trạng thái spinlud/linkedin-jobs-scraper, endpoint JobStreet/Jora VN, alert của Otta/WTTJ.

---

## 3. Mục tiêu v2 và nguyên tắc thiết kế

1. **Coverage trước, thông minh sau**: tin không có trong DB thì không thuật toán nào cứu được. Ưu tiên cào toàn bộ tin mới mỗi giờ từ listing/sitemap/API, không phụ thuộc từ khóa.
2. **Mỗi nguồn một adapter mỏng, cùng một schema**: giữ `SourceSpec` registry, thêm `mode` (keyword / listing / sitemap / rss / api / aggregator) và `runner` (lambda / external).
3. **Không chống chặn bằng mọi giá**: nguồn chặn dải IP AWS thì chạy từ runner khác; nguồn Cloudflare thì dùng proxy residential có ngân sách; nguồn cấm scraping trong ToS (LinkedIn) chỉ lấy qua thư viện/API công khai với tần suất thấp.
4. **Search là dịch vụ riêng**: DynamoDB giữ nguồn sự thật; chỉ mục tìm kiếm tách biệt, xây lại được từ S3.
5. **Tiếng Việt là công dân hạng nhất**: không dấu, i/y, viết tắt (HCM, HN, BD), tên nghề Việt–Anh, lương "triệu", "tr", "k", USD.
6. **Người dùng điều khiển được**: mọi bộ lọc có thể bật/tắt từ menu, không cần nhớ cú pháp.
7. **Chi phí trần**: mục tiêu < 10 USD/tháng hạ tầng + proxy; LLM chỉ dùng cho tin mới, cache kết quả.

---

## 4. Kiến trúc v2

```
┌───────────────────────────── Coverage layer ─────────────────────────────┐
│ Lambda scraper (nguồn không chặn)   GitHub Actions / VPS runner (nguồn   │
│  • listing/sitemap/API adapters      chặn AWS IP, Playwright, JobSpy)    │
│  • curl_cffi + proxy residential     • chạy mỗi 1–2h, đẩy vào cùng SQS   │
└──────────────┬────────────────────────────────┬──────────────────────────┘
               ▼                                ▼
        SQS raw-jobs  ──►  ETL Lambda (normalize, detail fetch, enrich, dedup cross-source)
                                   │
                 ┌─────────────────┼──────────────────┐
                 ▼                 ▼                  ▼
          DynamoDB jobs      S3 (raw + parquet)   Search index (SQLite FTS5 trên S3/EFS
          (nguồn sự thật)    (lịch sử, analytics)  hoặc Postgres Neon/Supabase + pgvector)
                                                        │
                       Matcher (mỗi 1h) ◄───────────────┘────► Webhook bot (/search, menu)
                            │
                       Telegram (digest theo tần suất người dùng, inline buttons)
```

Thay đổi chính so với v1:
- Thêm **external runner** (GitHub Actions cron hoặc VPS 5 USD) để cào nguồn chặn AWS và chạy Playwright. Runner dùng cùng code, cùng registry, chỉ khác `runner="external"`.
- ETL thêm bước **detail fetch có hạn mức** (N tin mới/nguồn/run) và **enrichment** (rule-based + LLM tùy chọn).
- Thêm **search index** để thay scan DynamoDB. Phương án đề xuất: SQLite FTS5 + bảng embedding, file index build lại mỗi giờ bởi Lambda "indexer" và lưu S3; webhook tải về `/tmp` khi cold start (10–50 MB, 1–2s). Phương án B: Postgres managed (Neon free tier) với `pg_trgm` + `pgvector`.
- Matcher chạy mỗi giờ nhưng chỉ gửi theo **tần suất người dùng chọn** (ngay lập tức / 2 lần/ngày / 1 lần/ngày).

---

## 5. Mở rộng nguồn tin

Nguyên tắc: mỗi nguồn ghi rõ cách lấy dữ liệu, có chạy được từ AWS không, và cần gì.

### 5.1 Nguồn hiện có — nâng cấp cách cào

| Nguồn | Việc cần làm |
|---|---|
| VietnamWorks | Chuyển sang Algolia API (lấy key từ script tag), lấy toàn bộ tin mới theo `sort=date`, có lương ẩn, kỹ năng, cấp bậc |
| TopCV | Thử curl_cffi impersonate Chrome + proxy residential VN; fallback sitemap `sitemap-job.xml`; giữ You.com làm dự phòng |
| JobsGO, TimViec365 | Chạy từ external runner; dùng trang "việc làm mới nhất" thay keyword |
| CareerLink | Sửa parser (trang đổi layout, trả rỗng); dùng sitemap |
| TopDev | Kiểm tra DevTools xem có endpoint JSON (chưa xác nhận); nếu không, đọc dữ liệu nhúng/JSON-LD; timeout riêng |
| CareerViet, ViecLam24h, Việc Làm Tốt | Thêm chế độ listing "mới nhất" + phân trang theo thời gian |
| ITviec | Fetch chi tiết lấy kỹ năng; lương cần đăng nhập → không lấy |
| YBox | Giữ nguyên, thêm phân loại intern/part-time |
| **Tất cả** | Thêm `GenericJsonLdScraper`: diff sitemap theo `<lastmod>` → tải trang tin → parse JSON-LD `JobPosting` (fallback OpenGraph). Dùng làm fallback khi parser riêng trả 0 tin, và làm adapter mặc định cho board mới |

### 5.2 Nguồn mới — Tier 1 (giá trị cao, làm ngay)

| Nguồn | Cách lấy | Chạy từ AWS? | Ghi chú |
|---|---|---|---|
| LinkedIn Jobs | `python-jobspy` (guest endpoint, không login), `location="Vietnam"`, `hours_old=24` | Có, nhưng 429 khi burst → external runner + proxy | Nhiều tin công ty nước ngoài, remote |
| Indeed VN | `python-jobspy` `country_indeed="vietnam"` | Có | API GraphQL ổn định |
| Glassdoor VN | `python-jobspy` (VN có trong danh sách Glassdoor) | Không chắc | Ít tin hơn, có lương; ~2 req/s |
| Jora VN (jobstreet.vn nay 301 về jora.com) | HTML; trả 403 với HTTP thuần → cần curl_cffi/external runner | Không | Aggregator của SEEK; ưu tiên thấp hơn Indeed |
| Glints VN | Trang trả 200; dữ liệu Next.js nhúng (`__NEXT_DATA__`) theo cách actor Apify làm | Cần test | Startup, mức junior; ~79 tin/từ khóa |
| Careerjet VN | API affiliate chính thức, locale `vi_VN`/`en_VN`, client Python | Có | Aggregator phủ rộng; phải chuyển tiếp IP/UA người dùng, không dùng để xây dataset |
| Trang career công ty qua ATS | Greenhouse, Lever, Ashby, Workable, SmartRecruiters có JSON API công khai; seed ~50 slug công ty tuyển ở VN, mở rộng kiểu Hunter | Có | Tin chất lượng cao, không trùng board |
| Remote cho người ở VN | Himalayas (`country=VN`), Remotive, Arbeitnow, Jobicy — không cần key | Có | Phải link-back, không đăng lại; gắn nhãn `remote_global` |
| Cổng nhà nước | **vieclam.gov.vn** (Sàn giao dịch việc làm quốc gia), trung tâm DVVL HN/HCM | Có (HTML) | Việc phổ thông, nhà nước; chưa có API/open data |

### 5.3 Nguồn mới — Tier 2 (mở rộng phổ thông/đa ngành)

JobOKO, 123job, 1900.com.vn, Viecoi, Jobhopin, Jobhub, MyWork (nay thuộc Vieclam24h), TimViecNhanh,
Vieclam.tuoitre, Jobsgo RSS, VietnamSalary, Jobtest. Hầu hết là HTML SSR, có sitemap. Thêm 1–2 nguồn/tuần
theo template adapter + fixture.

### 5.4 Tier 3 (cân nhắc, rủi ro cao)

- Nhóm Facebook: không có API công khai; Graph API không trả bài trong group. Chỉ làm nếu người dùng tự
  forward bài vào bot (tính năng "📥 Gửi tin cho bot" → bot parse và thêm vào DB với nguồn `user_submitted`).
- Kênh Telegram tuyển dụng: dùng Telethon đọc kênh công khai từ external runner; nguồn `telegram_channel`.
- Aggregator trả phí (Adzuna, JSearch/RapidAPI, Apify actors): dự phòng khi nguồn chính hỏng; giới hạn ngân sách.

### 5.5 Chiến lược chống chặn

1. `BaseScraper` đổi sang `curl_cffi.requests.Session(impersonate="chrome")` (drop-in, giữ API requests). Bước đầu tiên của P0 là đo lại 12 nguồn + TopCV/Indeed/Jora bằng `scripts/test_scrapers_live.py` để biết nguồn nào chỉ cần fingerprint, nguồn nào cần proxy/trình duyệt (chưa đo được trong phiên lập kế hoạch này).
   Thay thế: Scrapling `Fetcher` (cùng nền curl_cffi, thêm selector thích nghi) nếu muốn một thư viện cho cả hai tầng.
2. Lớp `ProxyPool`: đọc danh sách proxy từ Secrets Manager; chỉ nguồn gắn cờ `needs_proxy` mới dùng; đếm byte/ngày để chặn vượt ngân sách.
3. `runner="external"` cho nguồn chặn AWS: GitHub Actions workflow cron (free 2.000 phút/tháng) hoặc VPS nhỏ chạy cùng `scripts/local_pipeline_run.py --sources jobsgo,timviec365,topcv`.
4. Playwright chỉ ở external runner, hoặc Lambda container theo mẫu lambda-theatre (2 GB, ~0.00015 USD/lần) làm "trình duyệt dự phòng" gọi khi HTTP trả 403; chỉ cho nguồn không có HTML/JSON tĩnh.
6. Quản trị nguồn theo kiểu Hunter: nguồn lỗi 5 run liên tiếp → tự tạm dừng + báo admin; tóm tắt sức khỏe hằng tuần.
5. Tôn trọng robots/ToS: rate limit 1 req/s/nguồn, cache ETag, không cào trang yêu cầu đăng nhập.

---

## 6. Dữ liệu và làm giàu (ETL v2)

### 6.1 Mở rộng schema `Job`
Thêm: `level` (intern/fresher/junior/mid/senior/lead/manager), `experience_years_min/max`,
`job_type` (đã có, chuẩn hóa thêm hybrid/remote), `skills[]` (chuẩn hóa), `industry`, `benefits[]`,
`deadline_at`, `salary_min/max/currency/period` (tháng/năm/giờ), `address`, `district`, `company_size`,
`company_id` (chuẩn hóa tên), `canonical_group_id` (nhóm tin trùng giữa các board), `lang`, `quality_score`.

### 6.2 Trích xuất
- Rule-based trước (regex tiếng Việt cho lương, kinh nghiệm, cấp bậc, địa chỉ quận/huyện). Dữ liệu
  mẫu: ~10k tin trong S3 hiện có để viết test.
- Từ điển kỹ năng đa ngành: xây từ cột `technical_skills`/`soft_skills` của dataset **VietJobs** (48k tin VN, MIT) + ESCO cho tin tiếng Anh + kỹ năng phổ biến VN (kế toán: MISA, Excel, thuế; marketing: Meta Ads, Google Ads; v.v.). Bổ sung bằng đếm n-gram trên 10k tin có sẵn trong S3.
- Phân loại nghề (`category`) và ước lượng lương khi tin không ghi: huấn luyện classifier nhẹ (TF-IDF + linear hoặc e5-small fine-tune) trên VietJobs; dùng làm bộ lọc và cho `/trending`.
- LLM (tùy chọn, bật bằng cờ): chỉ cho tin mới chưa có cấu trúc, batch 20 tin/prompt, output JSON theo schema; cache theo `job_id`. Ước tính < 1 USD/ngày với model nhỏ.

### 6.3 Dedup liên nguồn
- Khóa mờ: `normalize(title) + normalize(company)` + SimHash của mô tả; gộp vào `canonical_group_id`.
- Khi hiển thị: một thẻ, nhiều link nguồn ("Có trên TopCV, VietnamWorks").

### 6.4 Ngày đăng
- Parse "x giờ/ngày trước", "Hôm nay", dd/mm/yyyy; nếu nguồn không có thì `posted_at = first_seen_at`
  (lần đầu thấy, không phải lần cào lại) — cần thêm trường `first_seen_at` và không ghi đè khi upsert.

---

## 7. Tìm kiếm và so khớp v2

1. **Chỉ mục full-text**: SQLite FTS5 với tokenizer unicode61 + cột `text_no_accent`; truy vấn BM25 trên title (trọng số cao), skills, company, description. Build mỗi giờ từ DynamoDB/S3, lưu S3, Lambda tải khi cold start.
2. **Embedding tiếng Việt**: bắt đầu với `multilingual-e5-small` (118M, ONNX int8 ~120 MB, 384-d) hoặc `gte-multilingual-base` (305M, điểm retrieval VN-MTEB cao hơn) chạy ở indexer (external runner hoặc Lambda container 1–2 GB) cho `title + skills + 300 ký tự đầu mô tả`; nâng lên bge-m3/AITeamVN qua API nếu đo thấy cần. Đánh giá trên 50 truy vấn mẫu + VietJobs trước khi chọn. Truy vấn: hybrid BM25 + cosine, reciprocal rank fusion; rerank tùy chọn bằng `AITeamVN/Vietnamese_Reranker` cho top 30.
3. **Bộ lọc cứng**: địa điểm (tỉnh/quận), lương tối thiểu, cấp bậc, hình thức, remote, nguồn, công ty loại trừ, từ khóa loại trừ, ngày đăng.
4. **Xếp hạng cá nhân hóa**: điểm = relevance + freshness + feedback (👍/👎/ẩn/lưu) + độ hợp hồ sơ.
5. **Hồ sơ người dùng**: người dùng nhập/kéo thả CV (PDF/DOCX) → trích kỹ năng, kinh nghiệm, địa điểm mong muốn → tạo embedding hồ sơ → `/match` gợi ý tin phù hợp nhất và giải thích ngắn vì sao.
6. **Query understanding**: "kế toán tổng hợp lương trên 15tr ở Thủ Đức" → keyword + salary_min=15.000.000 + district=Thủ Đức. Rule trước, LLM fallback.

---

## 8. Bot Telegram v2 — menu và chức năng

### 8.1 Lệnh / nút mới

| Nhóm | Lệnh | Chức năng |
|---|---|---|
| Tìm | `/search`, nhập tự do | Như hiện tại + hiểu lọc trong câu |
| Tìm | `/filter` 🎛 Bộ lọc | Inline keyboard bật/tắt: địa điểm, lương, cấp bậc, hình thức, remote, nguồn, ngày đăng. Lưu làm mặc định |
| Tìm | `/company <tên>` | Tin đang tuyển của một công ty; nút "Theo dõi công ty" |
| Tìm | `/similar` (nút dưới mỗi tin) | Tin tương tự theo embedding |
| Tìm | `/trending` 📈 | Top kỹ năng/nghề/công ty tuần này, lương trung vị theo nghề/thành phố (từ dữ liệu đã cào) |
| Hồ sơ | `/profile` 👤 | Nhập hoặc gửi CV; chỉnh kỹ năng, kinh nghiệm, địa điểm, lương mong muốn, cấp bậc |
| Hồ sơ | `/match` ✨ | Tin phù hợp hồ sơ nhất hôm nay, có lý do |
| Đăng ký | `/subscribe` | Thêm bộ lọc đầy đủ (lương, cấp bậc, hình thức, loại trừ) qua wizard inline |
| Đăng ký | `/settings` ⚙️ | Tần suất (ngay/2 lần/ngày/hằng ngày/tuần), giờ nhận, giờ im lặng, ngôn ngữ, số tin/lần |
| Đăng ký | `/follow <công ty>` | Báo khi công ty đăng tin mới |
| Đăng ký | `/exclude <từ khóa|công ty>` | Blacklist |
| Tin | Inline dưới mỗi tin: 💾 Lưu · 🙈 Ẩn · 👍 · 👎 · 🔁 Tương tự · 📤 Chia sẻ | Feedback học sở thích |
| Tin | `/saved` 📌 | Tin đã lưu, trạng thái ứng tuyển (đã nộp / phỏng vấn / từ chối) |
| Tin | `/history` | Tin đã được gửi 7 ngày qua |
| Tin | `/submit` 📥 | Gửi link/bài đăng (Facebook, Zalo) để bot parse và chia sẻ cho người khác |
| Khác | `/stats`, `/health` (admin) | Thống kê nguồn, số tin/ngày, lỗi, chi phí |
| Khác | `/feedback` | Góp ý gửi admin |

### 8.2 Thay đổi giao diện
- Reply keyboard rút còn 6 nút chính; chức năng còn lại vào inline keyboard theo ngữ cảnh.
- Mỗi tin có thẻ rút gọn (tiêu đề, công ty, lương, địa điểm, cấp bậc, kỹ năng chính, ngày đăng, nguồn) + nút.
- Thẻ "chi tiết" khi bấm vào tin: mô tả 1.000 ký tự, yêu cầu, phúc lợi, hạn nộp, các nguồn trùng.
- Digest buổi sáng: "Hôm nay có 23 tin mới cho 3 đăng ký của bạn", nhóm theo đăng ký, tối đa N tin, nút "Xem thêm".
- Onboarding 3 bước khi `/start`: nghề → địa điểm → cấp bậc/lương → tạo đăng ký đầu tiên.

### 8.3 Kỹ thuật bot
- Chuyển webhook sang trả 200 ngay và xử lý nặng qua hàng đợi (SQS → Lambda worker) để không chạm trần 30s; tin "Đang tìm..." được sửa khi xong.
- `callback_query` handler + bảng `USER#<id>` / `PREF`, `SAVED#`, `HIDDEN#`, `FOLLOW#`, `FEEDBACK#` trong bảng users (single-table).
- i18n tối thiểu VI/EN.

---

## 9. Thay đổi lưu trữ

| Bảng | Thay đổi |
|---|---|
| `vieclambot-jobs` | Thêm GSI `posted_day` (PK=`posted_date`, SK=`posted_at`) để matcher đọc "tin mới từ lần chạy trước" thay vì scan; thêm trường mục 6.1; `first_seen_at` không ghi đè |
| `vieclambot-users` | Thêm SK: `PREF`, `PROFILE_V2`, `SAVED#`, `HIDDEN#`, `FOLLOW#`, `FEEDBACK#`, `EXCLUDE#` |
| S3 | Thêm `processed/parquet/yyyy/mm/dd` cho analytics (`/trending`) và để build lại index |
| Index | `s3://.../index/jobs.sqlite` (FTS5 + vectors) build mỗi giờ |
| Secrets | Proxy, LLM key, You.com key |

---

## 10. Vận hành và chất lượng

- **CI GitHub Actions**: ruff + pytest trên mỗi PR; job cron riêng chạy external scraper.
- **Fixture HTML/JSON** mỗi nguồn (`tests/fixtures/<source>/listing.html`) + test parser; script `scripts/refresh_fixtures.py`.
- **Canary mỗi giờ**: cào 1 trang/nguồn, so với fixture; nếu 0 tin hoặc selector không khớp → trạng thái `layout_changed` → Telegram admin.
- **Dashboard** `/stats` admin + CloudWatch metrics đã có; thêm biểu đồ tin/ngày/nguồn trong S3 → Athena (tùy chọn).
- **Ngân sách**: biến môi trường trần cho You.com, proxy GB/ngày, LLM token/ngày; vượt thì tắt tính năng tương ứng, không chết pipeline.
- **Pháp lý**: file `docs/SOURCES.md` ghi ToS/robots từng nguồn, tần suất, cách lấy; không lưu dữ liệu cá nhân ứng viên; CV người dùng mã hóa và xóa được bằng `/profile delete`.

---

## 11. Lộ trình

| Giai đoạn | Nội dung | Ước tính | Tiêu chí hoàn thành |
|---|---|---|---|
| **P0 — Nền tảng (tuần 1)** | curl_cffi trong BaseScraper + đo lại 403 từng nguồn (local và AWS); fixture + test cho 12 nguồn; CI; sửa CareerLink, TopDev, VietnamWorks Algolia; `GenericJsonLdScraper`; `first_seen_at` | 1 tuần | 12/12 nguồn có fixture test; 10/12 trả tin từ local; bảng "nguồn × môi trường × trạng thái" |
| **P1 — Coverage (tuần 2–4)** | Chế độ listing/sitemap cho nguồn hiện có; external runner (GitHub Actions) cho JobsGO/TimViec365/TopCV; JobSpy (Indeed VN trước, LinkedIn/Glassdoor sau); Glints, Careerjet, Himalayas; ATS adapters + 50 slug seed; vieclam.gov.vn; 123job/JobOKO/1900 qua JSON-LD | 3 tuần | ≥ 20 nguồn; ≥ 30k tin mới/tuần; tỷ lệ tin có `posted_at` thật > 90% |
| **P2 — Dữ liệu (tuần 5–6)** | Detail fetch có hạn mức; schema mới; trích xuất rule (lương/cấp bậc/kinh nghiệm/kỹ năng đa ngành) kiểm thử trên VietJobs; classifier ngành; dedup liên nguồn; parquet S3 | 2 tuần | ≥ 80% tin có cấp bậc + kỹ năng; trùng liên nguồn gộp đúng ≥ 90% trên mẫu 200 tin; parse lương đúng ≥ 95% trên 1k tin VietJobs |
| **P3 — Search index (tuần 7–8)** | Indexer Lambda/runner; SQLite FTS5 + embedding; hybrid ranking; bộ lọc cứng; thay scan DynamoDB trong `/search`, `/myjobs`, matcher; GSI `posted_day` | 2 tuần | p95 `/search` < 3s; recall trên bộ 50 truy vấn mẫu tăng so với v1 |
| **P4 — Bot UX (tuần 9–10)** | Webhook async qua SQS; inline buttons; `/filter`, `/settings`, `/saved`, `/profile`, `/match`, `/company`, `/follow`, `/exclude`, `/history`, onboarding; digest theo tần suất | 2 tuần | Người dùng tạo đăng ký đầy đủ không cần gõ lệnh; feedback được lưu |
| **P5 — Thông minh + vận hành (tuần 11–12)** | LLM enrichment có ngân sách; `/trending`; `/submit`; canary layout; admin `/stats`; tài liệu SOURCES.md; tối ưu chi phí | 2 tuần | Chi phí/tháng đo được và < trần; canary báo đúng khi mô phỏng layout đổi |

Mỗi giai đoạn kết thúc bằng deploy + probe thật trên AWS như quy trình hiện tại (backup Lambda, hash code, log).

---

## 12. Rủi ro và quyết định cần chốt

| Rủi ro / quyết định | Đề xuất |
|---|---|
| Proxy residential tốn tiền (≈ 3–8 USD/GB) | Chỉ bật cho TopCV, giới hạn 1 GB/tháng; ưu tiên sitemap + detail fetch tối thiểu; dự phòng Zyte/Bright Data theo lượt (vài USD/tháng ở quy mô này) |
| LinkedIn/Glassdoor cấm scraping trong ToS; JobFunnel đã bỏ cuộc vì chống bot | Chỉ dùng guest endpoint tần suất thấp (1 lần/giờ, ≤ 100 tin), không login, không lưu dữ liệu cá nhân; coi là nguồn "có thì tốt", không là nguồn chính |
| API remote/aggregator có điều khoản link-back, cấm đăng lại | Luôn giữ link gốc + tên nguồn trong thẻ tin; không đưa vào `/submit` chia sẻ lại |
| External runner trên GitHub Actions bị trễ/cấm | Dự phòng VPS 5 USD/tháng chạy cùng workflow |
| Search index SQLite vs Postgres | Bắt đầu SQLite (không thêm dịch vụ), chuyển Postgres nếu > 300k tin hoặc cần đa người ghi |
| LLM provider | Bật sau cùng, chỉ khi rule-based không đủ; chọn model rẻ, batch |
| Thời gian webhook 30s | Chuyển sang async (P4) |

Cần người dùng chốt: (1) có chi tiền proxy/VPS không và mức trần; (2) có dùng LLM không; (3) ưu tiên ngành nào trước (IT, kế toán/văn phòng, phổ thông) để chọn thứ tự nguồn Tier 2.

---

## 13. Tiến độ thực hiện

### 30/10/2026 — P0 nền tảng + phần lớn P1 coverage (7 agent song song)

**Nguồn tin: 12 → 19 đăng ký, 15/16 nguồn live trả dữ liệu cho "kế toán" trong 1–4 giây** (`scripts/test_scrapers_live.py "kế toán" --live`).

| Nguồn | Trạng thái | Thay đổi chính |
|---|---|---|
| VietnamWorks | 50 tin, 2.3s | Chuyển sang API JSON của trang (`ms.vietnamworks.com/job-search`), có lương, cấp bậc, kỹ năng, hạn nộp; gỡ User-Agent Googlebot cũ |
| CareerViet | 50 tin, 2.0s | Sửa phân trang (trước đây mọi trang đều trả trang 1), đọc ngày cập nhật và hạn nộp, lọc thành phố |
| ViecLam24h | 30 tin, 2.2s | Đọc `__NEXT_DATA__`, URL sạch, mọi thành phố, ngày duyệt thật, map 34 tỉnh mới |
| CareerLink | 0 → 50 tin, 1.4s | Trang đã chuyển sang Next.js; parser mới theo `data-testid`, sắp xếp mới nhất, 29 thành phố |
| TimViec365 | timeout → 24 tin | `/tim-kiem` chậm 7.5s và bị robots cấm → dùng 95 trang danh mục + trang việc mới; sửa lỗi lấy hạn nộp làm ngày đăng |
| TopDev | timeout → 50 tin, 1.7s | Dùng API JSON `api.topdev.vn/td/v2/jobs` |
| Việc Làm Tốt | 50 tin | Sửa tên trường lương (trước đây luôn trống), ngày đăng gốc, loại hợp đồng, lọc vùng |
| ITviec | ổn | Bỏ "Sign in to view salary" khỏi lương, đọc đủ thành phố, hybrid/remote |
| YBox | ổn | Ngày ISO, loại hình intern/part-time, regex lương |
| JobsGO | 50 tin, 1.8s | Gỡ giả Googlebot; chạy với UA thường, nếu Cloudflare chặn thì ghi `blocked` |
| **Indeed VN (mới)** | 40 tin, 1.6s | GraphQL công khai, 100 tin/trang, có lương + mô tả + ngày; mã tỉnh → tên tỉnh |
| **LinkedIn (mới)** | 10 tin | Guest endpoint, chỉ batch, tần suất thấp |
| **123job (mới)** | 30 tin, 1.3s | HTML SSR, ~7k tin kế toán |
| **JobOKO (mới)** | 20 tin, 1.7s | HTML SSR, chỉ dùng trang SEO được robots cho phép |
| **Vieclam.gov.vn (mới)** | 20 tin, 1.3s | API JSON của cổng việc làm quốc gia |
| **Viecoi (mới)** | batch | Sitemap + JSON-LD, ~25s nên không live |
| Glassdoor | bỏ | Cần vượt anti-bot |
| Glints | chưa | robots cấm URL tìm kiếm |
| Jobhopin, Careerjet web, 1900.com.vn, Xóm Jobs, MyWork, TimViecNhanh | bỏ | Cloudflare / chết / redirect về Vieclam24h |

**Hạ tầng cào**
- `GenericJsonLdScraper`: diff sitemap theo `lastmod` + parse JSON-LD JobPosting, đã kiểm chứng trên 6 board; dùng làm **fallback tự động** khi parser riêng trả 0 tin (CareerViet, ViecLam24h, CareerLink, ITviec, 123job).
- `BaseScraper`: `scrape_latest` (tin mới nhất, không cần từ khóa; tự nhận diện), `location` chuyển tiếp cho scraper hỗ trợ (cả scheduled lẫn live search), `layout_changed` tách khỏi `blocked`/`error`, clamp timeout POST ở live mode.
- Scraper Lambda đọc trang "mới nhất" của mọi board trước khi chạy từ khóa (`VIECLAMBOT_SCRAPE_LATEST_PAGES`), rồi fallback JSON-LD nếu nguồn trả 0.
- Loader DynamoDB: upsert giữ `first_seen_at`; `RawJob.valid_through_raw` → `expires_at`.
- CI GitHub Actions (ruff + pytest); fixture HTML/JSON thật cho mọi nguồn; `live_search_max_sources` mặc định 16.

**Chất lượng tìm kiếm** (`docs/SEARCH_QUALITY_REVIEW.md`, `scripts/eval_search.py`, corpus 1.967 tin thật): precision top-10 84%, nhưng precision toàn bộ kết quả alert chỉ 58%; "DE" khớp "dê", "IT" khớp "ít"; "3 ngày trước" đọc thành giờ; "8.5 triệu" thành 85 triệu; scan DynamoDB vượt 30s ở ~50–100k tin. Đang áp 5 nhóm sửa ưu tiên theo 42 test xfail.

**Quyết định đạo đức**: hai scraper cũ (JobsGO, VietnamWorks) từng giả User-Agent Googlebot để qua Cloudflare. Đã gỡ; dự án không giả mạo crawler. TimViec365 và JobOKO chỉ dùng URL được robots.txt cho phép.

**Chưa làm / còn lại của P1**: adapter ATS (Greenhouse/Lever/Ashby), Himalayas/Remotive, external runner GitHub Actions, đo lại từ IP AWS, Glints khi có đường hợp lệ.

### 30/10/2026 — Chuyển hạ tầng sang tài khoản AWS mới và deploy v2

- Tài khoản mới `132058735660` (ap-southeast-1): tạo S3 `vieclambot-data-lake-132058735660`, DynamoDB `vieclambot-jobs`/`vieclambot-users`, SQS `vieclambot-raw-jobs` (+ DLQ), IAM role `job-alert-lambda-role` (quyền giới hạn theo tài nguyên), 4 Lambda, API Gateway `vieclambot-api`, 2 lịch EventBridge, Secrets Manager `vieclambot/you-api-key`.
- Bảng người dùng của tài khoản cũ (7 hồ sơ, 14 đăng ký, 2.329 bản ghi đã gửi) đã sao chép sang bảng mới nên alert không gửi lặp.
- Webhook Telegram trỏ sang `https://44qmvs1898.execute-api.ap-southeast-1.amazonaws.com/prod/`; `getWebhookInfo` không lỗi, pending 0.
- Kiểm chứng trên AWS: matcher `dry_run` 200; một `/search kế toán | HCM` thử (chat không tồn tại) hoàn tất trong 15,5s, live search gộp 238/261 tin từ 15 board. Từ IP AWS: JobsGO và TimViec365 trả 403, vieclam.gov.vn timeout kết nối; các board còn lại trả dữ liệu.
- Tài khoản cũ `585534524041`: đã tắt 2 lịch EventBridge; code 4 Lambda và cấu hình sao lưu tại `dist/cloud-backup-20261030-old/` (ngoài git). Tài nguyên cũ chưa xoá, chờ chủ dự án xác nhận.
- Script cài máy mới: `winget install Amazon.AWSCLI` → `aws login --profile vieclambot` (duyệt trên trình duyệt) → `AWS_PROFILE=vieclambot python scripts/setup_aws_resources.py` → `python scripts/deploy_lambdas.py`.
- 31/10/2026 (đêm 30/10): sau khi bot chạy ổn trên tài khoản mới, đã xoá toàn bộ tài nguyên ViecLamBot ở tài khoản cũ `585534524041` (4 Lambda, 2 lịch, API Gateway, 2 bảng, 2 hàng đợi, bucket với 71.748 object, secret You.com, role, log group). Bản sao lưu code/cấu hình/bảng người dùng vẫn ở `dist/cloud-backup-20261030-old/` trên máy phát triển.

### 31/10/2026 — P1 còn lại, P2, P3, P4, P5 (5 agent song song) — 538 test pass

- **P1**: 35 trang career công ty qua ATS (Greenhouse/Lever/Ashby/SmartRecruiters/Workable; 773 tin ở VN), 4 API remote (Himalayas, Remotive, Arbeitnow, Jobicy; có ghi nguồn), workflow GitHub Actions `scrape-external.yml` cho JobsGO/TimViec365/vieclam.gov.vn (`docs/EXTERNAL_RUNNER.md`, cần tạo secret AWS trên GitHub). Registry: 21 nguồn.
- **P2**: schema `Job` thêm level, kinh nghiệm, skills (từ điển 2.633 kỹ năng đa ngành xây từ VietJobs: 92,6% tin có kỹ năng, trước đây 6%), industry (20 nhóm, ~56% khớp nhãn VietJobs vì VietJobs gắn nhãn theo ngành công ty), benefits, salary_period, district, `canonical_group_id` (gộp trùng liên board), quality_score; đọc trang chi tiết có hạn mức 5 trang/lần ETL; xuất NDJSON gz lên S3 `processed/ndjson/`.
- **P3**: chỉ mục SQLite FTS5 build mỗi giờ (Lambda `vieclambot-indexer`) lên S3, webhook/matcher tải về `/tmp` theo ETag; top-20 trùng 100% với scan, độ trễ giảm ~8 lần; tự fallback scan khi chỉ mục thiếu/cũ.
- **P4**: nút inline mỗi tin (💾 🙈 👍 👎), `/saved`, `/history`, `/settings` (tần suất ngay/2 lần/ngày/ngày/tuần, giờ im lặng, số tin), `/filter` (thành phố, lương, cấp bậc, hình thức), `/exclude`, `/company`, `/follow`, `/trending`, onboarding 3 bước, bàn phím 6 nút, digest có tiêu đề.
- **P5**: nguồn lỗi 5 lần liên tiếp tự tạm dừng 24h + báo admin, tóm tắt tuần, trần You.com 200 lượt/ngày, `docs/SOURCES.md`, canary fixture 120 ngày, probe `--latest/--location/--json`.
- Quyết định chi phí: giữ DynamoDB on-demand và kiến trúc hiện tại (~5–10 USD/tháng), không chuyển Azure.
- **Chưa làm**: LLM enrichment (mục 6.2, cần API key và ngân sách), `/submit`, `/match` theo CV, embedding semantic, 🔁 tin tương tự, GSI `posted_day`.
