# Đánh giá chất lượng tìm kiếm và so khớp (30/10/2026)

Phạm vi: `src/matcher/search.py`, `src/matcher/synonyms.py`, `src/common/text_utils.py`,
`src/web_search/live_boards.py`, `src/etl/transformer.py`, `src/etl/loader.py`,
`src/bot/handler.py`, `src/matcher/alerts.py`. Đây là bản review chỉ đọc, không sửa `src/`.
Số dòng được ghi theo working tree ngày 30/10/2026. Lúc review, các agent khác đang sửa
scraper/transformer nên số dòng có thể lệch vài dòng.

Công cụ đi kèm:

- `scripts/eval_search.py` là harness tái lập được. Nó có probe tất định, 33 truy vấn chạy
  trên corpus thật, chẩn đoán dữ liệu, đo thời gian và mô hình chi phí Scan.
- `tests/test_search_quality.py` có 37 ca chặn hồi quy (đang pass) và 42 ca
  `xfail(strict=False)`. Mỗi ca xfail ứng với một phát hiện bên dưới.

```bash
export PYTHONUTF8=1
.venv/Scripts/python.exe scripts/eval_search.py             # dùng dist/eval_corpus.json, tự cào nếu chưa có
.venv/Scripts/python.exe scripts/eval_search.py --refresh   # cào lại (~40 s, 1 trang/từ khóa/nguồn)
.venv/Scripts/python.exe scripts/eval_search.py --only-probes
```

**Corpus dùng trong báo cáo.** Cào ngày 30/10/2026 với 6 từ khóa (kế toán, kinh doanh,
lập trình, marketing, nhân sự, logistics), 1 trang mỗi từ khóa cho mỗi nguồn qua
`batch_scrapers()`. Kết quả là **1.967 tin duy nhất từ 11 nguồn**: CareerViet 300,
VietnamWorks 280, JobsGO 286, Chợ Tốt 259, ViecLam24h 180, Indeed 456, LinkedIn 60,
TopDev 75, ITviec 34, TimViec365 20, YBox 17. CareerLink trả 0 tin. TimViec365 bị
ConnectionError từ từ khóa thứ hai nên harness dừng gọi nguồn này. Scraper LinkedIn/Indeed
đang được agent khác viết dở nhưng đã có trong registry, nên cũng được dùng. Harness
không gọi AWS và không gọi You.com (TopCV bị loại).

**Cách đọc chỉ số.** Harness gắn nhãn kết quả bằng regex trên tiêu đề đã bỏ dấu
(với truy vấn kỹ năng thì dùng cả mô tả). Đây là heuristic để phân loại nhanh, không phải
ground truth.

- P@10: tỉ lệ đúng trong 10 kết quả đầu.
- P@all: tỉ lệ đúng trên **toàn bộ** kết quả. Alert gửi mọi kết quả nên chỉ số này đo đúng
  thứ người dùng nhận qua thông báo.
- recall: tỉ lệ tin có tiêu đề rõ ràng đúng nghề mà `rank_jobs` tìm được.

## Tóm tắt

| Chỉ số (33 truy vấn) | Hiện tại | Nếu alert chỉ nhận score ≥ 80 |
|---|---:|---:|
| P@10 trung bình | 84% | – |
| P@all trung bình (thứ alert gửi đi) | **58%** | **87%** |
| Recall trung bình (heuristic) | 66% | 52% |
| Truy vấn không có kết quả | 5 (BD, “kế toán, kiểm toán”, “ke toan tai chinh”, remote python, điều dưỡng) | |
| Lỗi tách truy vấn | 5 | |
| Probe tất định sai | 37 | |

Top 10 kết quả cho truy vấn nghề phổ biến nhìn chung tốt: “kế toán” và “HR” đạt P@10 100%.
Vấn đề nằm ở ba chỗ:

1. **Đuôi kết quả** mà alert và `/myjobs` gửi đi: kết quả chỉ khớp ở mô tả hoặc tên công ty,
   cộng với va chạm do bỏ dấu.
2. **Hiểu truy vấn**: dấu phẩy, chữ “tai” không dấu, tên thành phố viết cuối câu, lương viết
   trong câu.
3. **Dữ liệu đầu vào**: ngày đăng, lương, địa điểm, tag.

Chi phí xếp hạng tăng tuyến tính và sẽ vượt ngân sách webhook 30 giây ở khoảng 50.000–100.000
tin (xem phần 3).

## 1. Phát hiện, xếp theo mức ảnh hưởng

Ước lượng công sức: S ≤ 1 ngày, M 1–3 ngày, L > 3 ngày.

### F1. Alert và `/myjobs` gửi kết quả chỉ khớp ở mô tả hoặc tên công ty (ảnh hưởng: rất cao, công sức S)

- **Ở đâu:**
  - `src/matcher/search.py:76-85`: blob gồm cả `company` và `search_blob`. `search_blob` chứa
    title + company + mô tả + yêu cầu + tag (`src/etl/transformer.py:137-143`).
  - `search.py:96-102`: mọi kết quả có điểm từ 40 trở lên đều được giữ.
  - `src/matcher/alerts.py:118-125` và `src/bot/handler.py:733-742` lấy toàn bộ kết quả, không
    có ngưỡng điểm.
- **Ví dụ từ harness:**
  - `IT`: 190 kết quả, P@all 8%. Kết quả #1 trong phần đuôi là “Trưởng Phòng Marketing” vì tên
    công ty là “Công Ty TNHH Công Nghệ Thông Tin…”. Nhiều tin khác khớp vì có câu “có **ít**
    nhất 3 năm”.
  - `chăm sóc khách hàng`: 141 kết quả, P@all 18%. Ví dụ “Nhân viên Sale Admin… phối hợp
    cskh”.
  - `nhân viên kho`: 26% (“phiếu xuất kho”, “tồn kho”).
  - `BA`: 21%. Khớp với tên công ty “HTX… Lục **Ba**”, “Tập đoàn **Ba** Thành”, với “**Bà**
    Rịa”, “quảng **bá**”.
  - `kế toán`: 81%. Ví dụ “Nhân viên kinh doanh… hỗ trợ kế toán thu hồi công nợ”.
- **Hệ quả:** khoảng 4 trên 10 tin alert gửi đi không đúng nghề. Đây là nguyên nhân trực tiếp
  của cảm giác “bot gửi rác”.
- **Đề xuất:**
  1. Tách `company` khỏi blob dùng để khớp nghề. Chỉ khớp tên công ty khi người dùng gõ đúng
     cụm tên công ty, hoặc dùng cú pháp `@công ty` sau này.
  2. Với alert và `/myjobs`, mỗi khái niệm nghề phải có mặt trong title (score ≥ 80). Riêng
     kỹ năng (token thuộc `TECH_SKILLS`, tag) được phép khớp ở mô tả.
  3. Với `/search`, giữ kết quả chỉ khớp ở mô tả nhưng đưa xuống sau và gắn nhãn
     “liên quan”.
- **Bằng chứng:** chỉ áp ngưỡng score ≥ 80 đã nâng P@all từ 58% lên 87%. Recall giảm chủ yếu
  ở truy vấn kỹ năng (java 58%, javascript 5%, c++ 11%), nên cần ngoại lệ kỹ năng ở bước 2.
- **Test:** `test_f1_company_name_is_not_an_occupation_match`,
  `test_f1_description_only_mentions_are_not_returned_for_alert_grade_search`.

### F2. Bỏ dấu làm va chạm token ngắn hoặc mơ hồ (ảnh hưởng: rất cao, công sức M)

- **Ở đâu:**
  - `src/common/text_utils.py:15-24, 31-66`: `clean_vn_text` bỏ toàn bộ dấu.
  - `search.py:15-16`: `contains` chạy trên chuỗi đã bỏ dấu.
  - `synonyms.py:134, 136, 147`: các nhóm `kho`, `it`, `ba`.
- **Ví dụ từ harness:**
  - `DE` trả 522 kết quả, P@10 0%. #1 là “📣 **DÊ** TƯƠI ĐỒNG CHÍ TUYỂN DỤNG” (quán dê, score
    100). 531/1.967 tin (27%) có “de” sau khi bỏ dấu (“để”). Hai tin Data Engineer thật thì
    bị bỏ sót.
  - `IT`: 127 tin chứa “ít nhất”.
  - `kỹ sư điện`: P@all 19%. Khớp “toàn đạc **điện tử**”, “**đại diện**”, “**điện thoại**”.
    112 tin chứa “điện thoại” hoặc “đại diện”.
  - `BA`: 47 tin có “ba”.
- **Đề xuất:**
  1. Lưu thêm bản lowercase còn dấu (NFC) của title và blob, tính một lần khi nạp snapshot
     hoặc ở ETL.
  2. Term của truy vấn **có dấu** thì khớp trên bản còn dấu.
  3. Term ASCII ngắn (≤ 3 ký tự) hoặc nằm trong danh sách viết tắt (`it, de, ba, hr, qa, qc,
     ai, pr, bd, cs`) chỉ khớp với từ gốc vốn không dấu. Nhờ đó “ít” không còn trùng “it”.
     Ở title nên ưu tiên khớp phân biệt hoa thường (“IT”, “BA”).
  4. Mở rộng `DE` thành “data engineer” hoặc từ chối viết tắt hai chữ không có trong từ điển.
  5. Thêm ngữ cảnh loại trừ cho từ ghép: “điện tử”, “điện thoại”, “đại diện” không thỏa “điện”.
  6. Thêm `ð/Ð` (U+00F0/U+00D0) → `d` vào `_CHAR_GROUPS`, xem F4.
- **Test:** 5 ca trong `test_f2_accent_collisions_do_not_match`.

### F3. `parse_search` và hiểu truy vấn (ảnh hưởng: cao, công sức M)

- **Ở đâu:** `search.py:260-267` và `search.py:27-43` (`concepts`).
- **Ví dụ từ harness:**

  | Truy vấn | `parse_search` trả về | Kết quả |
  |---|---|---|
  | “kế toán, kiểm toán” | location = “kiểm toán” | 0 kết quả, bỏ sót 293 |
  | “python, django” | location = “django” | |
  | “ke toan tai chinh” | (“ke toan”, “chinh”); chữ “tai” không dấu bị coi là “tại” | 0 kết quả, bỏ sót 301 |
  | “nhân viên kinh doanh Hà Nội” | không nhận ra địa điểm; “ha” và “noi” thành token bắt buộc phải có trong mô tả | recall 40% |
  | “logistics Bình Dương” | tương tự | P@10 40% |
  | “lương trên 15 triệu kế toán” | “luong”, “tren”, “15”, “trieu” đều bắt buộc | 3 kết quả, recall 1% |
  | “B2B/B2C” | AND hai token | người dùng thường muốn OR |

- **Đề xuất:** thêm hàm `understand_query()` dựa trên luật, đúng hướng §7.6 của kế hoạch:
  1. Chỉ coi `|` là dấu tách địa điểm. Với “ tại/ở ” có dấu cũng vậy. Với “tai”, “o” không dấu,
     chỉ tách khi phần sau tra ra một địa danh trong gazetteer. Dấu phẩy và “/” nghĩa là OR
     giữa các từ khóa.
  2. Nhận diện địa danh ở đầu hoặc cuối truy vấn bằng gazetteer (xem F4).
  3. Regex lương `(lương )?(trên|từ|>=?) N (triệu|tr|m)` → `salary_min`. “dưới N” → `salary_max`.
  4. Cấp bậc (intern, fresher, junior, senior, lead, trưởng phòng) chuyển thành bộ lọc mềm hoặc
     điểm cộng, không còn là token bắt buộc. Hiện “senior .net” chỉ có recall 20% vì “Lập Trình
     Viên .NET” bị loại.
  5. remote/từ xa → cờ `remote`.
- **Test:** `test_f3_parse_search` (5 ca), `test_f3_salary_phrase_in_query_does_not_zero_results`.

### F4. Chuẩn hóa địa điểm: thiếu alias, mất quận, loại tin toàn quốc (ảnh hưởng: cao, công sức M)

- **Ở đâu:**
  - `src/etl/transformer.py:29-58`: `LOCATION_MAP` chỉ có 9 tỉnh.
  - `transformer.py:185-198`: lấy mapping khớp đầu tiên, nên chuỗi nhiều thành phố chỉ còn
    một tỉnh.
  - `search.py:105-114`: quận được thay bằng tên thành phố.
  - `search.py:144-148`: bộ lọc dùng `contains`.
- **Ví dụ từ harness:**
  - “kế toán | Thủ Đức” trả cả 180 tin kế toán ở HCM, P@10 0%, P@all 1%.
  - “kế toán | BD” trả 0 kết quả, bỏ sót 9 tin ở Bình Dương.
  - `HCMC`, `ĐN`, `Q.1` không được map.
  - Indeed ghi “**Ð**à Nẵng, DN” bằng chữ Ð Latin (U+00D0), và `clean_vn_text` không đổi ký tự
    này. Vì vậy “marketing | Đà Nẵng” chỉ có recall 60%.
  - Location chưa map trong corpus gồm `''` (50 tin), các mã số “39”, “37”, “71” do Indeed trả
    về, “SG” (15 tin, có tin thực ra ở Bình Dương), Long An, Bà Rịa - Vũng Tàu, Tây Ninh.
  - Tin “Toàn quốc” và tin remote luôn bị loại khi có bộ lọc thành phố.
- **Đề xuất:**
  1. Lập gazetteer gồm tên tỉnh cũ và tỉnh mới sau sáp nhập 2025, viết tắt (HCM, HCMC, SG, HN,
     ĐN, BD, BR-VT…), và quận/huyện/TP thuộc tỉnh (Thủ Đức, Dĩ An, Thủ Dầu Một…).
  2. Ở ETL lưu `provinces: set` và `districts: set`.
  3. Bộ lọc thành phố so khớp theo giao tập hợp. Bộ lọc quận: khớp quận nếu tin có ghi quận,
     nếu không thì rơi về cấp tỉnh với điểm thấp hơn.
  4. Tin `toàn quốc` hoặc `remote` vẫn được nhận dưới bộ lọc thành phố nhưng xếp sau.
  5. Chuẩn hóa NFKC và thêm `ð → d`.
- **Lưu ý:** từ 2025 nhiều tỉnh đã sáp nhập (ví dụ Bình Dương và Bà Rịa - Vũng Tàu nhập vào
  TP.HCM) trong khi các tin vẫn dùng tên cũ. Cần quyết định truy vấn “HCM” có kèm Bình Dương
  hay không. Đề xuất: không kèm mặc định, có gợi ý mở rộng.
- **Test:** `test_f4_location_aliases_and_nationwide` (4 ca),
  `test_f4_district_filter_is_not_whole_city`.

### F5. Parse `posted_at` sai và tin không có ngày đăng bị ẩn khỏi `/search` (ảnh hưởng: cao, công sức S)

- **Ở đâu:**
  - `transformer.py:278-343`, đặc biệt dòng `elif "giờ" in date_str or "g" in date_str` (≈331):
    chữ “g” trong “ngày”, “tháng” và “ago” bị hiểu là “giờ”.
  - `search.py:137-143`: `posted_since` bắt buộc phải có `posted_at`.
  - `handler.py:796-802`: nhánh DB của `/search` dùng `posted_since` 7 ngày.
- **Ví dụ:**
  - “3 ngày trước” → 3 giờ. “2 tháng trước” → 2 giờ, nên tin 2 tháng tuổi vẫn lọt cửa sổ
    7 ngày. “1 year ago” → 1 giờ. “5 minutes ago” → 5 giờ.
  - “18 phút trước”, “Hôm nay”, “Hôm qua” → None.
  - Corpus: 66 tin JobsGO dạng “N ngày trước” bị hiểu thành giờ. 137 tin dạng “N phút trước”
    có `posted_at = None`.
  - **665/1.967 tin (34%) không có `posted_at`**: CareerViet 300/300, ViecLam24h 180/180,
    TimViec365 20/20, JobsGO 137, TopDev 28. Nhánh DB của `/search` loại toàn bộ số tin này.
    Trong lần cào production 08/10, CareerViet và ViecLam24h chiếm khoảng 43% bản ghi.
  - `dd/mm/yyyy` đang được hiểu là nửa đêm UTC thay vì nửa đêm giờ Việt Nam (lệch 7 giờ).
- **Đề xuất:**
  1. Dùng regex đơn vị có ranh giới từ theo thứ tự: phút|giờ|ngày|tuần|tháng|năm và
     minute|hour|day|week|month|year. Thêm “hôm nay”, “hôm qua”, “an hour ago”.
  2. Đặt timezone `Asia/Ho_Chi_Minh` cho ngày tuyệt đối.
  3. Bộ lọc và xếp hạng dùng `posted_at or first_seen_at`. `first_seen_at` đã có trong
     `SEARCH_FIELDS` và không bị ghi đè khi upsert.
- **Test:** `test_f5_relative_dates` (8 ca), `test_f5_vietnamese_date_uses_local_timezone`.

### F6. Parse lương sai (ảnh hưởng: cao với alert có lọc lương, công sức S–M)

- **Ở đâu:** `transformer.py:201-243`. Dòng 229 xóa dấu chấm thập phân. Dòng 235 dùng
  `"tr" in salary`. Dòng 241 lấy số cuối cùng làm max.
- **Ví dụ:**

  | Chuỗi lương | Kết quả hiện tại | Ghi chú |
  |---|---|---|
  | “8.5 - 10 triệu” | (85.000.000, 10.000.000) | 15 tin Chợ Tốt trong corpus bị lỗi này |
  | “Lương 15-20 triệu + 2 tháng lương” | max = 2.000.000 | Bộ lọc `salary_min = 15tr` loại nhầm tin này |
  | “Up to $2,000 (attractive)” | 2 **tỷ** VND | “tr” trong “a**tt**ractive” làm nhân thêm 1e6 |
  | “$30,000/year” | 750 triệu/tháng | Không chia theo năm |
  | “Lên đến 20 triệu” | min = 20 triệu | |

- **Đề xuất:** viết lại parser theo token.
  1. Số: dấu phân cách có ≤ 2 chữ số phía sau là phần thập phân.
  2. Đơn vị: triệu|tr|m|k|usd|$|vnđ|đ, có ranh giới từ.
  3. Kỳ: /năm|year → chia 12, /ngày|giờ → bỏ hoặc gắn cờ.
  4. Cận: lên đến|up to|tới → chỉ đặt max; từ|trên|from → chỉ đặt min.
  5. Chỉ lấy khoảng đầu tiên. Giữ `salary_raw` để hiển thị.
- **Test:** `test_f6_salary_parsing` (6 ca).

### F7. Dedup liên nguồn gần như không hoạt động (ảnh hưởng: trung bình–cao, công sức S)

- **Ở đâu:** `search.py:155-168`. `identity()` băm title + company + `normalize_location`
  (location thô).
- **Ví dụ:** corpus có 33 nhóm trùng giữa các board (khớp mờ title + company), **32 nhóm không
  được gộp**. Ví dụ JobsGO ghi “Hà Nội”, Indeed ghi “Hà Nội, HN” cho cùng tin “Java Developer /
  Công ty Cổ phần Giải Pháp Thiên Hoàng”. Hậu tố pháp lý (“TNHH … VIỆT NAM”) và tiền tố quận
  cũng làm tách đôi.
- **Đề xuất:** khóa mới gồm ba phần:
  1. Title đã chuẩn hóa: bỏ ngoặc, bỏ đuôi “- Lương…” và “Tại …”.
  2. `company_key`: bỏ công ty|cty|tnhh|cp|cổ phần|jsc|ltd|mtv|việt nam.
  3. Tập tỉnh canonical lấy từ F4.

  Gộp thành một thẻ và hiển thị “Có trên X, Y” (§6.3 kế hoạch). **Lưu ý migration:** `SENT#` dùng
  `identity()`. Trong một chu kỳ TTL cần kiểm tra cả khóa cũ lẫn khóa mới để không gửi lại tin.
- **Test:** `test_f7_identity_across_boards`.

### F8. Trích tag làm bẩn blob (ảnh hưởng: trung bình, công sức S)

- **Ở đâu:** `transformer.py:261-275`. Skill dài hơn 3 ký tự được so khớp bằng substring. Skill
  ngắn dùng `\b`, nhưng `\b` không bao giờ khớp sau “+”/“#”. Tag sau đó được nối vào blob
  (dòng 137-143).
- **Ví dụ:**
  - Văn bản chỉ có JavaScript vẫn được gắn tag `java`, nên `/search java` trả tin JavaScript.
    Corpus có 2 tin như vậy.
  - `rust` khớp trong “t**rust**” (6 tin), `scala` khớp trong “**scala**ble” (3 tin).
  - `c++` và `c#` không bao giờ được gắn tag.
- **Đề xuất:** dùng regex `(?<![\w.+#])skill(?![\w+#])` và bảng alias (reactjs→react,
  nodejs/node.js, golang). Tag không nên làm khái niệm nghề thỏa mãn, chỉ cộng điểm.
- **Test:** `test_f8_*` (5 ca).

### F9. Xếp hạng: bậc điểm thô, độ mới chỉ để phá hòa, xen kẽ nguồn đẩy tin cũ lên (ảnh hưởng: trung bình, công sức M)

- **Ở đâu:** `search.py:98-102` (bậc 100/80/40–60) và `search.py:202-217` (sắp theo điểm,
  thời gian, rồi round-robin theo nguồn).
- **Ví dụ:**
  - Truy vấn “accountant”: mọi title tiếng Anh đạt 100, còn “Kế toán…” chỉ đạt 80, bất kể độ
    mới. Đây là thiên lệch ngôn ngữ do so khớp đúng nguyên văn.
  - Tin khớp nguyên văn đăng 59 ngày trước vẫn đứng trên tin đồng nghĩa đăng 1 giờ trước.
  - Round-robin cho mỗi nguồn một suất ngang nhau. Một nguồn chỉ có tin 40 ngày tuổi vẫn chen
    vào vị trí #2 (`test_f9_interleaving_does_not_promote_stale_jobs`).
  - `matches_filters` kiểm tra `max_age_days` bằng `scraped_at` (`search.py:132-134`). Mỗi lần
    cào lại làm mới giá trị này, nên tin cũ không bao giờ hết hạn (`test_f9_age_limit_uses_posting_date`).
  - Độ dài blob rất chênh giữa các nguồn: CareerViet trung bình 62 ký tự, LinkedIn 48,
    ViecLam24h 76, trong khi Indeed 2.203 và Chợ Tốt 998. Kết quả khớp ở mô tả vì thế lệch hẳn
    về Indeed và Chợ Tốt.
- **Đề xuất:**
  1. Tính điểm liên tục: BM25 theo trường (title ×3, tags ×2, mô tả ×1, chuẩn hóa theo độ dài).
     Khớp đồng nghĩa được tính bằng khớp nguyên văn.
  2. Cộng điểm độ mới, ví dụ `0.3·exp(-age/7d)`.
  3. Thay round-robin bằng phạt nhẹ khi trùng nguồn kiểu MMR, chỉ áp trong cùng nhóm điểm và
     cùng “ngày”.
  4. Tuổi tin tính theo `posted_at or first_seen_at`.

### F10. Cụm đồng nghĩa nhiều từ bị khóa thành cụm liền và thiếu nhóm (ảnh hưởng: trung bình, công sức S)

- **Ở đâu:** `synonyms.py:186-211` (`tokenize_query` tách cụm dài nhất trước) và
  `synonyms.py:116-158` (`_MATCH_GROUPS`).
- **Ví dụ:**
  - “kỹ sư xây dựng” bắt buộc cụm liền, nên recall chỉ 12%. “Kỹ Sư Tư Vấn Giám Sát (HSE, MEP,
    Xây Dựng)” chỉ được 40 điểm, “Kỹ sư giám sát xây dựng” bị bỏ.
  - “ke toan tong hop” bỏ sót 9 tin “General Accountant”.
  - “javascript” có recall 55%: bỏ sót ReactJS, Node.js, TypeScript.
  - “C&B” bỏ sót “Tiền lương”, “Compensation”.
  - “e-commerce” bị tách thành `e` + `commerce`.
- **Đề xuất:**
  1. Khi không tìm thấy cụm liền, rơi về AND các thành phần với điểm thấp hơn.
  2. Thêm nhóm: JS ecosystem, `kế toán tổng hợp ↔ general accountant`,
     `c&b ↔ compensation & benefits ↔ tiền lương`, `thương mại điện tử ↔ e-commerce`.
  3. Giữ nguyên từ có gạch nối hoặc dấu chấm (`e-commerce`, `front-end`, `node.js`) như một
     cụm.
  4. Lưu ý: `get_category_for_text` (`synonyms.py:214-241`) vẫn dùng `SYNONYM_GROUPS` cũ với
     substring (`"it" in text`). Nếu hàm này được dùng cho category sau này thì sẽ sai hàng loạt.

### F11. Ranh giới token (ảnh hưởng: thấp–trung bình, công sức S)

- `search.py:15-16`: `(?<!\w)` đứng trước “.net”, nên “asp.net” không khớp “.net”
  (`test_f11_dotnet_inside_aspnet`).
- Query được tách theo `[,\s\-/|]` (`synonyms.py:208`) trong khi tài liệu không tách như vậy:
  “B2B/B2C” thành AND, “ui/ux” thành hai token.
- **Đề xuất:** dùng chung một tokenizer cho query và document, chạy ở ETL. Lưu
  `tokens: list[str]` để khớp theo tập hợp thay cho regex (giải quyết luôn chi phí ở F13).

### F12. Live boards: không đẩy địa điểm xuống board, gửi query thô, cache kết quả thiếu (ảnh hưởng: trung bình, công sức S)

- **Ở đâu:**
  - `src/web_search/live_boards.py:91` gọi `scraper.scrape(query, 1)`, không truyền
    `location`, dù `BaseScraper.scrape_safe` đã hỗ trợ.
  - `live_boards.py:37-58` gửi query thô (gồm “tìm việc”, tên thành phố, “lương trên…”).
  - `live_boards.py:194` cache kết quả khi `ranked` khác rỗng **dù còn board pending**. Kết quả
    thiếu bị giữ 5 phút.
  - `_cache` (dòng 28) không có giới hạn kích thước.
- **Ví dụ:** “logistics Bình Dương” lấy trang 1 toàn quốc của từng board rồi lọc tại chỗ:
  25 kết quả, P@10 40%.
- **Đề xuất:**
  1. Gửi board phần từ khóa đã làm sạch từ F3, cùng `location` cho các scraper hỗ trợ.
  2. Chỉ cache khi tất cả board đã trả lời, hoặc cache theo từng board.
  3. Dùng LRU tối đa 256 khóa.
  4. Gộp `_board_query`, `_topdev_query` và `query_variants` thành một chỗ. Hiện cả ba lặp lại
     logic “AI intern”, và đây là ngoại lệ duy nhất.

### F13. Chi phí: Scan toàn bảng và xếp hạng bằng regex mỗi request (ảnh hưởng: cao khi > 30k tin, công sức M–L)

Xem phần 3. `/myjobs` xếp hạng lại toàn snapshot **một lần cho mỗi đăng ký**
(`handler.py:733-742`), rồi nối danh sách theo thứ tự đăng ký chứ không xếp chung.

### F14. Vấn đề dữ liệu đầu vào ảnh hưởng tới so khớp (ảnh hưởng: thấp–trung bình)

- 277/280 blob VietnamWorks còn thẻ HTML (`</p><p>•`). Không làm hỏng việc khớp nhưng tốn chỗ
  trong giới hạn 4.000 ký tự của `search_blob`.
- Indeed trả location dạng mã số (“39”, “71”), “SG” cho tin ở Bình Dương, và ký tự “Ð”. Báo
  lại cho agent đang viết `indeed_scraper.py`.
- `Subscription.keyword_normalized` chỉ dùng `lower()` (`src/common/models.py:226`), nên
  “Kế toán” và “ke toan” thành hai đăng ký khác nhau. Nên dùng `clean_vn_text` cho khóa `SUB#`.

## 2. Những điểm đang tốt (đã có test chặn hồi quy)

- Không phân biệt dấu, khớp Việt–Anh cho nghề phổ biến. “kế toán”, “accountant”, “HR”,
  “thực tập marketing”, “chăm sóc khách hàng” đạt recall 100% và P@10 100%.
- Ranh giới từ đúng: `java` không khớp `JavaScript` ở title, `it` không khớp `digital`.
  Data Engineer khác Data Analyst, điều dưỡng khác bác sĩ.
- Alias HCM/TP.HCM/Sài Gòn/HN, chuỗi nhiều thành phố, lọc lương giữ tin không công bố lương.
- Top 10 đa dạng nguồn (6–10 nguồn) và mới (trung vị tuổi 0–0,6 ngày với truy vấn phổ biến).

## 3. Scan hay chỉ mục: số đo và quyết định

### 3.1 Số đo (`scripts/eval_search.py`, máy phát triển, Python 3.12)

| Đại lượng | Giá trị |
|---|---|
| `rank_jobs` trên corpus 1.967 tin | 155–370 ms một truy vấn |
| Chi phí mỗi tin | **80–190 µs** (hai lần chạy; máy đang tải bởi agent khác) |
| Bản nhân 10.000 tin | 0,79–2,1 s một truy vấn |
| Phần thời gian tốn cho `re.search` (cProfile) | ~55%. Phần còn lại chủ yếu là `clean_vn_text` gọi lại title/company/tags cho từng tin |
| Ngoại suy 50.000 tin | 4,3–9,3 s (máy local), **~11–23 s** trên Lambda 1.024 MB (≈0,6 vCPU, nhân 2,5) |
| Ngoại suy 100.000 tin | 8,6–18,6 s local, **~22–47 s** trên Lambda. Vượt ngân sách webhook 30 s, chưa tính Scan |
| `/myjobs` với 10 đăng ký, 100.000 tin | ~220–470 s trên Lambda: không khả thi |
| Kích thước item DynamoDB (corpus, chủ yếu thẻ listing) | trung bình 2,2 KB, p90 5,2 KB. Heap Python ~4,6 KB/item |
| Item đầy trần (description 2.000 + requirements 2.000 + blob 4.000 ký tự) | ~8–10 KB |

Thời gian còn tăng theo độ dài blob, vì regex quét toàn chuỗi. Blob production có thể dài tới
4.000 ký tự, trong khi trung bình corpus chỉ 829 ký tự.

**Chi phí Scan đầy đủ** (eventually consistent: 0,5 RRU cho mỗi 4 KB **dữ liệu item đọc**.
`ProjectionExpression` không giảm RRU vì RRU tính theo cả item):

| Số tin | Item 2,2 KB: MB / RRU / trang 1 MB / heap | Item ~8 KB: MB / RRU / trang / heap |
|---:|---|---|
| 10.000 | 22 MB / 2,7k / 21 / ~50 MB | 80 MB / 9,8k / 78 / ~180 MB |
| 50.000 | 112 MB / 13,7k / 107 / ~240 MB | 400 MB / 49k / 390 / ~0,9 GB |
| 100.000 | 224 MB / 27k / 214 / ~470 MB | 800 MB / 98k / 780 / **~1,8 GB** |

Các trang Scan được đọc tuần tự, khoảng 80–200 ms mỗi trang 1 MB (giả định, chưa đo trên AWS).
Ở 100.000 tin, chỉ riêng Scan đã mất 17–43 s (item 2,2 KB) hoặc 60–160 s (item 8 KB). Heap vượt
1.024 MB của webhook khi item lớn.

Tiền thì không phải vấn đề. Với giá on-demand khoảng $0,125–0,15 cho 1 triệu RRU (cần kiểm tra
giá vùng ap-southeast-1), một Scan 100.000 tin tốn $0,003–0,015. Kể cả với cache snapshot
5 phút, mỗi container ấm tốn dưới $4/ngày. **Giới hạn thật là độ trễ và bộ nhớ.**

Thử nhanh một chỉ mục token trong bộ nhớ (ngoài harness, prototype) trên 50.000 tin:

- Dựng chỉ mục unigram/bigram/trigram mất 13 s.
- Tìm ứng viên mất 0,5–3 ms.
- Xếp hạng chỉ trên ứng viên mất 3–1.500 ms, so với 4,3–4,6 s khi quét toàn bộ.
- Số kết quả lệch nhẹ do tokenizer chưa đồng nhất.

### 3.2 Quyết định đề xuất

1. **Ngay bây giờ (dưới ~20.000 tin, công sức S):**
   - Bỏ phần văn bản cồng kềnh khỏi item mà Scan đọc. Chuyển `description`/`requirements` đầy
     đủ sang S3 hoặc item `DETAIL#` riêng. `search_blob` chỉ giữ title + tags + ~500 ký tự đầu
     hoặc danh sách token. Kết quả: RRU và số trang Scan giảm 3–8 lần, heap giảm tương ứng.
   - Khi nạp snapshot, dựng một lần chỉ mục token trong bộ nhớ (token → id) và cache theo
     container. `rank_jobs` chỉ chấm điểm ứng viên.
   - Chuẩn hóa title, company và tags sẵn ở ETL thay vì gọi `clean_vn_text` mỗi request.
   - `/myjobs` gom mọi đăng ký vào một lượt xếp hạng.
2. **v2 (khi vượt ~30.000 tin hoặc 20 nguồn trở lên; p95 `/search` trên 10 s là ngưỡng
   kích hoạt; công sức M–L):**
   - Dựng chỉ mục offline SQLite FTS5 (unicode61 + cột không dấu), như §7.1 kế hoạch, mỗi giờ
     trong ETL hoặc indexer. Lưu ở S3, webhook tải về `/tmp` khi cold start.
   - Ước lượng: 100.000 tin × ~1 KB văn bản đã cắt cho ra file 100–150 MB. Lambda `/tmp` có
     thể cấu hình tới 10 GB, truy vấn mất vài ms.
   - Scan chỉ còn ở indexer: 27k–98k RRU mỗi giờ, tức $0,08–0,35/ngày.
   - Alert dùng GSI `posted_day` (§9) để đọc “tin mới từ lần chạy trước”, không cần Scan.
3. **Không khuyến nghị:**
   - OpenSearch hoặc Elasticsearch managed: chi phí cố định hằng tháng và gánh vận hành quá lớn
     với quy mô bot này.
   - Bảng inverted index trong DynamoDB: khuếch đại ghi khoảng 200 token/tin, tức 20 triệu item
     ở 100.000 tin.

## 4. Thứ tự áp dụng đề xuất

| # | Việc | Phát hiện | Công sức | Lý do |
|---|---|---|---|---|
| 1 | Ngưỡng title-hit cho alert và `/myjobs`, tách `company` khỏi blob khớp nghề | F1 | S | P@all của alert 58% → 87%; người dùng thấy ngay |
| 2 | Sửa `_parse_date` + fallback `first_seen_at` | F5 | S | 34% tin hiện vô hình với `/search` DB; tin cũ giả làm tin mới |
| 3 | Khớp có dấu và chặn viết tắt ngắn (IT/DE/BA/kho/điện), thêm `ð` | F2 | M | Truy vấn ngắn hiện sai gần như hoàn toàn |
| 4 | `understand_query`: dấu phẩy, “tai” không dấu, địa danh cuối câu, lương, cấp bậc | F3 | M | 5/33 truy vấn hỏng parse; 3 truy vấn ra 0 kết quả |
| 5 | Parser lương + tag theo ranh giới token | F6, F8 | S | Sai số liệu và lọc lương sai |
| 6 | Gazetteer địa điểm (tỉnh, quận, toàn quốc, remote) | F4 | M | Lọc theo quận và viết tắt |
| 7 | `identity()` liên nguồn, có migration `SENT#` | F7 | S | 32/33 nhóm trùng không gộp |
| 8 | Live boards: đẩy location xuống, query sạch, không cache kết quả thiếu | F12 | S | |
| 9 | Snapshot gọn và chỉ mục token trong bộ nhớ, sau đó FTS5 | F13 | M → L | Cần trước khi thêm nhiều nguồn |
| 10 | Điểm liên tục kiểu BM25 + độ mới + đa dạng nguồn | F9, F10 | M | Nền cho cá nhân hóa ở v2 |

## 5. `tests/test_search_quality.py`

Có 37 ca đang pass, dùng để chặn hồi quy:

- Khớp Việt–Anh ở title.
- Không khớp substring hay nghề anh em.
- Khớp nguyên văn xếp trên khớp ở mô tả.
- Alias địa điểm (gồm “binh duong” ↔ “Thủ Dầu Một, Bình Dương” và “Vũng Tàu” ↔ “Bà Rịa - Vũng Tàu”).
- Tách địa điểm khi người dùng ghi rõ.
- Các dạng lương và ngày đang parse đúng.
- Lọc lương giữ tin không công bố lương.
- `identity` gộp tin chỉ khác hoa thường.
- `normalize_location` idempotent.

Có 42 ca `xfail(strict=False)`. Mỗi ca ghi lý do, và tên test mang đúng mã phát hiện
trong tài liệu này:

| Phát hiện | Test | Số ca |
|---|---|---:|
| F1 | `test_f1_company_name_is_not_an_occupation_match`, `test_f1_description_only_mentions_are_not_returned_for_alert_grade_search` | 2 |
| F2 | `test_f2_accent_collisions_do_not_match` | 5 |
| F3 | `test_f3_parse_search`, `test_f3_salary_phrase_in_query_does_not_zero_results` | 6 |
| F4 | `test_f4_location_aliases_and_nationwide`, `test_f4_district_filter_is_not_whole_city` | 5 |
| F5 | `test_f5_relative_dates`, `test_f5_vietnamese_date_uses_local_timezone` | 9 |
| F6 | `test_f6_salary_parsing` | 6 |
| F7 | `test_f7_identity_across_boards` | 1 |
| F8 | `test_f8_no_substring_tags`, `test_f8_symbol_skills_are_tagged`, `test_f8_java_query_does_not_return_javascript_job_via_tags` | 5 |
| F9 | `test_f9_interleaving_does_not_promote_stale_jobs`, `test_f9_age_limit_uses_posting_date` | 2 |
| F11 | `test_f11_dotnet_inside_aspnet` | 1 |

Khi một bản sửa làm ca xfail chuyển sang XPASS, xóa marker để ca đó thành test chặn hồi quy.
Các phát hiện F10, F12, F13 và F14 chưa có test tự động: chúng cần fixture live board,
số đo hiệu năng, hoặc là lỗi dữ liệu thuộc scraper. Harness `scripts/eval_search.py` đo các
phát hiện này.
