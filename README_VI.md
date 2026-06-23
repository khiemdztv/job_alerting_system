# ViecLamBot - Tài Liệu Kỹ Thuật Hệ Thống Cảnh Báo Việc Làm Tự Động (Serverless)

Tài liệu này cung cấp cái nhìn toàn diện về kiến trúc dự án **ViecLamBot**, các công nghệ cốt lõi đang sử dụng, luồng quy trình nghiệp vụ, các vấn đề thực tế hệ thống giải quyết, cùng các thử thách kỹ thuật lớn đã vượt qua và phương án giải quyết cụ thể.

---

## 1. Vấn đề giải quyết (Business Problems)

* **Phân tán dữ liệu tuyển dụng**: Người tìm việc tại Việt Nam phải theo dõi thủ công quá nhiều nền tảng (ITviec, ViecLam24h, CareerLink, YBox, CareerViet...). Việc này gây mất thời gian và dễ bỏ lỡ cơ hội tốt.
* **Thời gian cập nhật chậm**: Tin tuyển dụng mới đăng không được thông báo ngay lập tức, dẫn đến việc ứng viên gửi hồ sơ muộn hơn các đối thủ.
* **Tìm kiếm Tiếng Việt không chính xác**: Một số nền tảng tuyển dụng có bộ lọc từ khóa hoạt động chưa tối ưu, không hỗ trợ tốt việc gõ sai dấu, từ viết tắt hoặc chuyển đổi linh hoạt giữa gõ có dấu/không dấu (ví dụ: `kĩ` vs `kỹ`, `hoà` vs `hòa`).
* **Chi phí vận hành**: Việc chạy máy chủ cào tin 24/7 tốn kém và khó mở rộng khi lượng từ khóa đăng ký của người dùng tăng đột biến.

---

## 2. Công nghệ sử dụng (Technology Stack)

Hệ thống được thiết kế theo kiến trúc **Serverless Microservices** trên hạ tầng đám mây **AWS**, giúp tối ưu hóa chi phí (chỉ trả tiền khi chạy) và khả năng mở rộng tự động.

* **Ngôn ngữ lập trình chính**: Python (với Pydantic kiểm soát dữ liệu đầu vào và các thư viện phân tích cú pháp HTML/JSON như BeautifulSoup4).
* **AWS Lambda**: Thành phần tính toán phi máy chủ chính, chia làm 4 hàm Lambda chuyên biệt:
  * `vieclambot-scraper`: Cào tin định kỳ từ các trang web.
  * `vieclambot-etl`: Tiêu thụ hàng đợi, làm sạch và chuẩn hóa dữ liệu.
  * `vieclambot-matcher`: So khớp công việc với nhu cầu người dùng và gửi thông báo.
  * `vieclambot-webhook`: Tiếp nhận và xử lý tương tác thời gian thực từ Telegram.
* **Amazon EventBridge**: Trình lập lịch (Cron job) kích hoạt Scraper (mỗi 6 tiếng) và Matcher (mỗi 6 tiếng, lệch 15 phút sau Scraper).
* **Amazon SQS (Simple Queue Service)**: Hàng đợi tin nhắn trung gian để đệm dữ liệu (Load Leveling) giúp ETL Lambda tiêu thụ tin nhắn bất đồng bộ mà không gây nghẽn database.
* **Amazon S3**: Lưu trữ dữ liệu thô (raw JSON) theo dạng **Data Lake** để kiểm toán lịch sử và tái phục hồi (backfill) dữ liệu.
* **Amazon DynamoDB**: Cơ sở dữ liệu NoSQL hiệu năng cao:
  * Bảng `vieclambot-users`: Lưu trữ thông tin tài khoản và đăng ký nhận tin của người dùng (áp dụng thiết kế **Single-Table Design**).
  * Bảng `vieclambot-jobs`: Lưu trữ tin tuyển dụng đã chuẩn hóa, tích hợp tính năng tự động xóa tin cũ sau 60 ngày bằng **Time-To-Live (TTL)**.
* **Amazon API Gateway**: Cung cấp các endpoint REST API bảo mật làm Webhook tiếp nhận tin nhắn từ Telegram.
* **Telegram Bot API**: Giao diện người dùng tương tác trực quan.

---

## 3. Luồng quy trình hệ thống (System Flows)

Hệ thống bao gồm 5 luồng quy trình nghiệp vụ chính:

### Luồng 1: Cào dữ liệu thô (Scraper Pipeline)
Chạy tự động mỗi 6 giờ:
1. `EventBridge` kích hoạt `Scraper Lambda`.
2. Lambda quét bảng DynamoDB để lấy toàn bộ từ khóa đăng ký đang hoạt động của người dùng kết hợp với bộ từ khóa hạt giống (seed keywords).
3. Hệ thống chạy song song 7 bộ cào nguồn: **CareerLink, ViecLam24h, ITviec, CareerViet, TimViec365, Jooble API, và YBox**.
4. Với mỗi nguồn, dữ liệu thô cào được lập tức ghi lên S3 Data Lake và gửi danh sách dạng JSON vào hàng đợi SQS.

### Luồng 2: Làm sạch & Lưu trữ (ETL Ingestion Pipeline)
Chạy bất đồng bộ kích hoạt bởi SQS:
1. SQS gửi các gói tin tuyển dụng thô đến `ETL Lambda`.
2. Dữ liệu đi qua bộ lọc cấu trúc đầu vào (`RawJobValidator`).
3. Chuyển đổi dữ liệu thô (`Transformer`):
   * Chuẩn hóa bảng mã ký tự Tiếng Việt sang Unicode dạng chuẩn NFC.
   * Rút trích vị trí địa lý thô và ánh xạ sang tỉnh thành chuẩn (ví dụ: `TP.HCM`, `Sài Gòn` -> `Ho Chi Minh`).
   * Phân tích lương (chuyển đổi các chuỗi phức tạp như `$1000`, `15 - 20 triệu/tháng` thành số nguyên tối thiểu/tối đa bằng VND).
   * Rút trích kỹ năng (Tags) tự động từ mô tả công việc.
4. Ghi đè trực tiếp (Bulk-upsert) dữ liệu đã chuẩn hóa vào bảng DynamoDB `vieclambot-jobs`.

### Luồng 3: Khớp tin & Cảnh báo (Matcher & Notification Pipeline)
Chạy lệch 15 phút sau Scraper:
1. `EventBridge` kích hoạt `Matcher Lambda`.
2. Lambda quét toàn bộ các đăng ký của người dùng trong bảng `vieclambot-users`.
3. Với mỗi đăng ký, chạy truy vấn tìm kiếm việc làm trong bảng `vieclambot-jobs` bằng thuật toán tìm kiếm giống hệt lệnh tìm tay `/search` (được lọc tiếp theo địa điểm nếu có cấu hình).
4. Định dạng và gộp các công việc phù hợp thành một tin nhắn tóm tắt chất lượng.
5. Gửi thông báo trực tiếp tới Telegram Chat ID của người dùng.

### Luồng 4: Tương tác thời gian thực (Interactive Webhook Pipeline)
1. Người dùng gửi lệnh (ví dụ: `/subscribe`, `/unsubscribe`, `/list`) tới Bot.
2. Telegram chuyển hướng tin nhắn qua HTTPS POST tới `API Gateway`.
3. `Webhook Lambda` nhận payload, kiểm tra cấu trúc, cập nhật cấu hình đăng ký của người dùng vào bảng `vieclambot-users` trong DynamoDB và phản hồi lại kết quả ngay lập tức.

### Luồng 5: Tìm kiếm nhanh (Live Search Flow)
1. Người dùng gửi từ khóa tìm kiếm (hoặc lệnh `/search <từ khóa> [| khu vực]`).
2. Bot gửi ngay tin nhắn tạm thời: *"🔍 Đang tìm kiếm việc làm trực tiếp từ các nguồn, vui lòng đợi..."*.
3. Bot kích hoạt chạy song song các scraper trong một `ThreadPoolExecutor` cục bộ cào nhanh trang 1 của cả 7 nguồn dữ liệu, chạy ETL làm sạch tức thì và cập nhật vào database.
4. Bot truy vấn dữ liệu từ database, áp dụng bộ lọc thời gian (7 ngày gần nhất), thực hiện thuật toán **Round-Robin Interleaving** để trộn đều các nguồn.
5. Bot tự động sửa (edit) tin nhắn tạm thời ban đầu bằng danh sách kết quả đẹp mắt cuối cùng.

---

## 4. Khó khăn kỹ thuật & Giải pháp khắc phục (Challenges & Solutions)

### Thử thách 1: Lỗi mất Cảnh báo do trùng lặp dữ liệu (ETL Deduplication Loop)
* **Khó khăn**: Ban đầu, hệ thống chạy bộ lọc trùng lặp ở mức ứng dụng trước khi ghi vào database. Nếu ID công việc đã tồn tại trong DynamoDB, bản ghi mới bị bỏ qua hoàn toàn. Việc này dẫn đến việc thời gian cào `scraped_at` của bản ghi không bao giờ được cập nhật mới. Khi `Matcher` chạy quét các công việc trong 6 tiếng qua, nó sẽ bỏ lỡ các công việc này mặc dù chúng vừa được đăng tuyển lại ở chu kỳ mới.
* **Giải pháp**: Loại bỏ toàn bộ bước kiểm tra trùng lặp trước khi ghi. Chuyển sang cơ chế **Bulk-Upsert trực tiếp vào DynamoDB**. Khi một công việc đã có được cào lại, database sẽ tự động ghi đè thông tin mới và cập nhật timestamp `scraped_at` hiện tại cùng thời gian hết hạn tự động (TTL) mới. Nhờ đó, Matcher luôn nhận dạng được các tin tuyển dụng đang hoạt động để gửi thông báo kịp thời.

### Thử thách 2: Bất đồng nhất giữa kết quả Tìm kiếm thủ công và Cảnh báo tự động
* **Khó khăn**: Ban đầu luồng tìm kiếm `/search` sử dụng logic tìm kiếm linh hoạt trên DynamoDB, trong khi Matcher quét dữ liệu mới và lọc từ khóa cục bộ bằng mã Python. Điều này dẫn đến sự không nhất quán: người dùng tự gõ tìm kiếm thì thấy việc làm, nhưng khi đăng ký nhận tin tự động lại không nhận được cảnh báo gì.
* **Giải pháp**: Thống nhất công cụ truy vấn. Cả Matcher Lambda và Webhook Lambda đều sử dụng chung hàm `db_loader.search_jobs()`. Tất cả các thuật toán xử lý dấu tiếng Việt, loại bỏ ký tự đặc biệt, chuẩn hóa nguyên âm được đóng gói tập trung, đảm bảo kết quả tìm kiếm thủ công và cảnh báo định kỳ hoàn toàn đồng nhất.

### Thử thách 3: Khắc phục giới hạn Timeout/Memory của Lambda trong Scraper
* **Khó khăn**: AWS Lambda có giới hạn cứng về thời gian thực thi (tối đa 15 phút) và dung lượng bộ nhớ. Ban đầu, scraper cào toàn bộ các nguồn, gom hàng ngàn công việc vào RAM rồi mới lưu lên S3 và gửi vào SQS cuối phiên chạy. Khi số lượng từ khóa đăng ký của người dùng tăng lên, việc này gây lỗi tràn bộ nhớ Lambda và mất dữ liệu hoàn toàn nếu Lambda bị cưỡng chế tắt (Timeout).
* **Giải pháp**: Thiết kế lại luồng scraper theo kiến trúc **Stream-style (nguồn nào gọn nguồn nấy)**. Scraper Lambda lặp qua từng trang tuyển dụng độc lập. Ngay sau khi cào xong 1 nguồn, dữ liệu hợp lệ sẽ được lọc, lưu thô vào S3 (phân vùng theo cấu trúc `raw/{source}/YYYY/MM/DD/`) và bắn trực tiếp vào SQS theo từng block 10 tin nhắn. Đồng thời tăng thời gian Timeout của Lambda Scraper lên mức tối đa **900 giây (15 phút)**.

### Thử thách 4: Giải quyết sự độc chiếm của một nguồn tuyển dụng (Source Dominance)
* **Khó khăn**: Một số trang web có số lượng bài đăng cực kỳ lớn (ví dụ: ViecLam24h trả về 50 bài đăng cho từ khóa "kế toán" trong khi ITviec chỉ có 2 bài). Nếu chỉ hiển thị 20 kết quả mới nhất theo cách thông thường, danh sách trả về cho người dùng sẽ bị độc chiếm hoàn toàn bởi duy nhất một nguồn tuyển dụng đó.
* **Giải pháp**: Phát triển thuật toán **Round-Robin Interleaving (Xen kẽ xoay vòng)** kết hợp lọc thời gian:
  1. Lọc dữ liệu chỉ lấy công việc được đăng/cào trong vòng **7 ngày** gần nhất (nếu không có mới hạ dần tiêu chuẩn để tránh màn hình trống).
  2. Nhóm các công việc theo nguồn tuyển dụng, sắp xếp giảm dần theo thời gian trong từng nhóm.
  3. Lấy lần lượt công việc đầu tiên của nhóm 1, nhóm 2, nhóm 3... rồi quay vòng lấy công việc thứ hai, cho tới khi chạm giới hạn 20 công việc. Điều này giúp phân phối nguồn dữ liệu đa dạng và hiển thị trực quan hơn.

### Thử thách 5: Lỗi định dạng MarkdownV2 làm mất tin nhắn Bot Telegram
* **Khó khăn**: Telegram yêu cầu rất khắt khe về việc escape ngược bằng ký tự `\` đối với 18 ký tự đặc biệt trong định dạng MarkdownV2. Tiêu đề tuyển dụng và tên công ty cào trên mạng thường chứa các ký tự này (như dấu chấm `.`, gạch ngang `-`, ngoặc đơn `()`, dấu cảm thán `!`). Chỉ cần sót một ký tự chưa escape, Telegram API sẽ trả lỗi `400 Bad Request` và tin nhắn bị nuốt mất mà không có cảnh báo rõ ràng.
* **Giải pháp**: Xây dựng bộ lọc regex tự động escape các ký tự đặc biệt. Thêm cơ chế **Fallback gửi tin nhắn Plain Text tự động**: Nếu gửi tin bằng MarkdownV2 thất bại, hệ thống bắt ngoại lệ, sử dụng regex xóa bỏ tất cả các ký tự gạch chéo ngược `\` rồi gửi lại tin nhắn dưới dạng văn bản thường (Plain Text). Giải pháp này đảm bảo tin nhắn cảnh báo luôn được chuyển đến điện thoại người dùng an toàn.

### Thử thách 6: Cào dữ liệu từ YBox (Client-side Rendering)
* **Khó khăn**: YBox là trang tin tức và tuyển dụng giới trẻ hàng đầu nhưng giao diện hoạt động theo cơ chế Client-side Rendering (React/Redux). Gửi request GET thông thường với tham số `?q=...` chỉ trả về một bộ khung HTML giống nhau và không lọc được từ khóa trên server.
* **Giải pháp**: Phân tích mã nguồn và các file Javascript tĩnh của YBox, phát hiện ra hệ thống nhúng toàn bộ 40-50 tin tuyển dụng nổi bật hiện có trực tiếp vào biến Javascript `window.__INITIAL_ADS__` nằm trong thẻ `<script>`.
  Ta viết bộ trích xuất sử dụng Regex và Brace matching để bóc tách chuỗi JSON của biến này ngay sau khi tải trang tuyển dụng chung, sau đó tiến hành lọc từ khóa tuyển dụng cục bộ bằng mã Python trong Scraper. Phương án này chạy cực nhanh (chỉ mất ~0.5 giây), không cần dựng trình duyệt headless Chrome tốn tài nguyên mà vẫn lấy được toàn bộ tin tuyển dụng YBox mới nhất.
