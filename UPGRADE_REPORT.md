# Bản sửa alert và tìm việc

## Tìm việc trên web bằng You.com — 21/09/2026

- Thêm lệnh `/web` và nút Telegram **🌐 Tìm thêm trên web**.
- Khi cơ sở dữ liệu có dưới năm kết quả, bot có thể tự tìm bổ sung qua You.com Search API. Mỗi
  request có timeout tám giây nên lỗi nhà cung cấp không chiếm hết thời gian webhook Telegram.
- Chỉ nhận kết quả có URL HTTP(S), đúng nghề và không xung đột địa điểm. Bot loại khóa học, bài
  hướng nghiệp, hồ sơ cá nhân, trang danh sách chung và URL trùng.
- Tìm trong một tháng gần nhất trước; chỉ mở rộng sang các nền tảng tuyển dụng trong một năm khi
  còn dưới ba kết quả đã xác minh.
- Key được đọc từ AWS Secrets Manager qua `VIECLAMBOT_YOU_API_KEY_SECRET_ARN`, không nằm trong
  source hoặc file môi trường đã commit. `scripts/configure_you_search.py` cấp riêng quyền đọc
  secret cho webhook Lambda.

## Cập nhật menu và chống lặp webhook — 21/09/2026

- Thêm bàn phím Telegram cố định gồm: tìm việc, việc phù hợp, tạo thông báo, danh sách đăng ký,
  xem thêm, hủy thông báo và hướng dẫn. Các luồng tạo/hủy thông báo nhận dữ liệu ở tin nhắn kế
  tiếp nên người dùng không cần nhớ cú pháp lệnh.
- Lưu `update_id` vào DynamoDB trong 24 giờ bằng conditional write. Retry từ Telegram hoặc một
  Lambda container mới không còn chạy lại cùng yêu cầu và không tạo nhiều tin “Đang tìm việc”.
- CloudWatch của phiên lỗi cũ ghi nhận liên tiếp nhiều lần `Duration: 30000 ms, Status: timeout`,
  xác nhận Telegram đã retry vì webhook chạm trần 30 giây; đây là nguyên nhân của chuỗi tin chờ
  trong ảnh người dùng cung cấp.
- Webhook tái sử dụng bot và snapshot tìm kiếm tối đa 5 phút trong warm Lambda. Quét DynamoDB có
  deadline theo thời gian còn lại của Lambda; nếu kho dữ liệu phản hồi chậm, bot dùng snapshot một
  phần thay vì để API Gateway timeout.
- Kiểm tra hồi quy sau thay đổi: 51 test pass; Ruff pass trên toàn bộ file được sửa ở đợt này;
  Telegram `getMe` xác nhận `cty_khong_bot`, webhook có URL, pending update = 0 và không báo lỗi.
- Đã đăng nhập lại profile `vieclambot`, sao lưu Lambda webhook cũ vào
  `dist/cloud-backup-20260921/vieclambot-webhook.zip` và chỉ cập nhật
  `vieclambot-webhook`; scraper, ETL và matcher không bị triển khai lại. Hash code AWS sau triển
  khai là `s5NxId03/6pv7HoSRJghE7enDA//KE3++WsF4oji8ck=` và khớp gói local.
- Cold start thật trên AWS trả HTTP 200, không có `FunctionError`. Danh sách lệnh Telegram đã được
  đăng ký lại với `/menu`, `/search`, `/subscribe`, `/myjobs`, `/list`, `/more`, `/unsubscribe`,
  `/help` và `/cancel`.
- Đã sửa lỗi dấu tiếng Việt trong menu lệnh Telegram do chuỗi UTF-8 từng bị PowerShell chuyển sai
  encoding. `scripts/configure_telegram_menu.py` hiện gửi menu từ file UTF-8 và đọc lại
  `getMyCommands` để bắt buộc nội dung trên Telegram khớp chính xác trước khi báo thành công.

Đã triển khai lên cả 4 Lambda tại `ap-southeast-1` ngày **07/09/2026**, sau khi đăng nhập lại bằng profile `vieclambot`. Các thông số và kết quả dưới đây được kiểm tra trực tiếp trên AWS; không cần phiên đăng nhập trên máy duy trì để Lambda chạy bằng IAM role.

## Những lỗi xác định được trong code

- `search_jobs` chỉ đọc hai trang Scan DynamoDB: job ở trang tiếp theo không bao giờ được tìm thấy. Projection thiếu description/requirements dù logic fallback cần các trường này.
- Alert giới hạn 50 kết quả trước khi lọc thời gian và địa điểm; một job phù hợp có thể bị loại trước khi được xét. Cửa sổ 6 giờ 30 phút không bù được những lần lịch chạy bị lỡ.
- Chưa lưu lịch sử gửi: job cào lại có thể bị báo lặp; job bị cắt khỏi tin nhắn không có cơ chế theo dõi riêng.
- Gộp 10–20 job thành một tin có thể vượt giới hạn Telegram; nhãn nguồn chứa dấu ngoặc chưa escape trong MarkdownV2. Telegram giới hạn `sendMessage` ở 4096 ký tự sau xử lý entities: [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage).
- Trang thứ hai khi tải subscriptions làm mất bộ lọc lương và địa điểm; nhánh matcher cũ gọi `tokenize_query` nhưng chưa import.
- Scraper luôn nhận `max_pages=1`; nhóm seed thứ năm không được chọn vì dùng `hour // 6`; log tham chiếu biến `SEED_KEYWORDS` không tồn tại.
- TimViec365 dùng `/viec-lam?key=...` thay cho `/tim-kiem?keyword=...` mà JavaScript tìm kiếm hiện tại sử dụng. Khi một URL xuất hiện nhiều lần, parser giữ link ngày đăng trước link tiêu đề, dẫn tới các job mang tên “3 ngày”, “10 giờ”. Đã sửa cả route và cách chọn link tiêu đề.
- SQS có thể trả lỗi từng phần nhưng code bỏ qua; ETL có thể báo thành công khi dữ liệu chưa ghi xong, khiến bản tin mất cơ hội retry. Lambda cần nhận lỗi để giữ batch cho retry: [AWS SQS error handling](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html).
- ID chỉ dựa title/company/source làm các tin khác URL hoặc địa điểm ghi đè nhau.
- Kiểm tra dữ liệu thật sau triển khai phát hiện dấu `...` trong thông tin lương gây lỗi chuyển số và retry ETL liên tục. Đã sửa để chỉ nhận token có chữ số, kèm kiểm thử hồi quy.
- Build chưa chỉ định ABI/Python đích; quick deploy che lỗi khi matcher không tồn tại. Log lưu tháng 6 có `pydantic_core` import error, nhưng ZIP hiện có đã chứa binary Linux CPython 3.12 đúng loại. Đây là bằng chứng lịch sử, chưa phải kết luận về lỗi production hiện tại.

## Hành vi sau sửa

- Một bộ tìm kiếm dùng chung cho `/search`, `/myjobs` và alert; đọc đủ trang, cache trong mỗi request/run; lọc trước khi giới hạn.
- Hiểu tiếng Việt không dấu, cụm nghề Việt/Anh, bỏ từ dẫn như “tìm việc”, “nhân viên”; so khớp theo ranh giới từ để `IT` không khớp `digital`, `Java` không khớp `JavaScript`. Tách nghề khác nhau như điều dưỡng/bác sĩ và Data Engineer/Data Analyst.
- Xếp hạng theo độ phù hợp rồi thời gian; xen kẽ nguồn trong cùng mức phù hợp; gộp bản trùng khi hiển thị. Tìm cả description/requirements của dữ liệu cũ. Job còn hoạt động không bị loại chỉ vì đăng hơn 7 ngày.
- `/search kế toán | HCM` hoặc `tìm việc kế toán tại HCM`; mỗi trang tối đa 5 job, tự giảm khi nội dung dài. `/more` xem tiếp, mặc định lưu tối đa 100 kết quả trong 1 giờ. Nội dung HTML và liên kết được escape, kích thước tin được kiểm soát.
- Tối đa 10 subscriptions (cấu hình được); đăng ký lại cùng từ khóa cập nhật địa điểm. `/myjobs` dùng cùng bộ lọc và có phân trang.
- Alert tìm trong 7 ngày cào gần nhất để bù lịch bị lỡ, lưu `SENT#` sau mỗi tin được Telegram xác nhận, bỏ job đã gửi kể cả trùng nhiều keyword/nguồn. Có khóa từng người dùng để hạn chế chạy chồng nhau. Lỗi một người không ngăn xử lý người khác; run chưa hoàn tất báo lỗi để Lambda retry.
- Mỗi lần chạy gửi tối đa 15 job/người, chia hạn mức cho các đăng ký; phần chưa gửi được giữ lại để xét ở lần sau, tránh dồn hàng trăm kết quả hồi phục thành tin nhắn.
- Scraper chạy tối đa 4 nguồn đồng thời, tôn trọng số trang cấu hình, ưu tiên/luân phiên từ khóa đăng ký; đẩy dữ liệu ngay sau mỗi từ khóa. SQS chỉ retry entry bị từ chối. ETL giữ các bản theo nguồn và báo lỗi khi parse/transform/write chưa hoàn tất.
- ID mới ưu tiên URL ổn định, bỏ tracking query thông dụng; giữ được các bài đăng riêng biệt. Bản ghi cũ vẫn tìm được và gộp trùng khi hiển thị, không cần xóa DB.
- `scripts/diagnose.py` đọc trạng thái Telegram, lịch Lambda, SQS và tùy chọn thử nguồn live; không gửi tin hay sửa cloud. `scripts/build_reviewed_package.py` tạo ZIP mới và giữ ZIP cũ để rollback. `quick_deploy.ps1` mặc định chỉ build, `-Deploy` mới cập nhật đủ 4 Lambda và dừng nếu có lỗi.

## Kiểm chứng và triển khai

Kiểm chứng ngày 06–07/09/2026:

- **47 test pass**, gồm retry sau gửi một phần, không gửi lại job đã ghi SENT, dry-run không ghi/gửi, lease chống chạy chồng, giới hạn alert mỗi người, tìm qua hơn hai trang DB, bộ lọc trước limit, phân trang, lỗi ghi SQS/ETL và thông tin lương chứa dấu câu. Có cảnh báo quyền ghi cache pytest trên Windows, không ảnh hưởng kết quả test.
- Ruff kiểm tra lỗi F trên `src`, `lambdas` và các script/test mới: pass. Kiểm tra cú pháp và build ZIP: pass.
- Telegram `getMe`: bot `cty_khong_bot` hợp lệ. `getWebhookInfo`: có URL, pending updates = 0, không có lỗi webhook được báo tại thời điểm kiểm tra. Không gửi tin thử tới người dùng.
- Probe 1 trang/nguồn với `data engineer`: 131 bản ghi, 52 phù hợp trước gộp trùng. Probe `kế toán`: 208 bản ghi, 184 phù hợp sau gộp trùng từ 7 nguồn. Đây là mẫu live, không phải tổng toàn bộ thị trường hay số tin đang có trong DB production.
- Sau sửa riêng TimViec365, probe 1 trang tăng từ 0 lên 1 job phù hợp cho Data Engineer, từ 1 lên 13 job phù hợp cho kế toán. Số tổng ở dòng trên là mẫu trước bản sửa riêng nguồn này.
- Lỗi credentials trên máy đã giải quyết bằng `aws login --profile vieclambot`. Đã sao lưu code đang chạy của cả 4 Lambda vào `dist/cloud-backup-20260907/`, triển khai ZIP mới và đối chiếu SHA256. Bản vá cuối: `yYvXHl3l+dJWsaxOPMmXd1Af3p6UoARN1QuXYsC2VAI=`.
- Log cũ cho thấy matcher vẫn gửi một số thông báo ngày 06/09; chưa có bằng chứng alert dừng hoàn toàn. Tìm kiếm bị cắt dữ liệu và bộ lọc cũ có thể khiến từng người không nhận được job phù hợp.
- Đợt cào thực tế hoàn tất trong khoảng 4 phút 34 giây, xử lý 14 từ khóa, thu **6.673 bản ghi** từ 6 nguồn. Đây là số bản ghi thô, có thể trùng giữa các từ khóa; không phải 6.673 tin mới duy nhất. ITviec 848, CareerViet 2.540, Vieclam24h 1.877, Chotot 1.274, Jooble 107, YBox 27. CareerLink trả rỗng và TimViec365 lỗi khi chạy từ AWS.
- Matcher dry-run trên dữ liệu thật: 5 người có đăng ký, 1.123 kết quả phù hợp, thời gian khoảng 14 giây, bộ nhớ tối đa 198 MB. Con số này được tính trước giới hạn gửi và có thể có trùng giữa người/đăng ký. Không gửi tin hoặc ghi SENT trong dry-run; chưa dùng việc gửi thử tới người dùng làm bằng chứng phục hồi.
- Webhook nhận event rỗng trả HTTP 200. Telegram không có pending update hay lỗi webhook tại thời điểm kiểm tra.
- Đã bật TTL `ttl` trên bảng users; SQS visibility 180 giây, DLQ giữ 14 ngày, tối đa 5 lần nhận trước khi chuyển DLQ. Tài khoản giới hạn 10 Lambda đồng thời; đã giới hạn ETL tối đa 4 để giảm throttling và dành dung lượng cho webhook/matcher.
- Sau bản vá thông tin lương, hàng đợi chính và DLQ đều còn 0 bản tin chờ/đang xử lý. Bản tin từng lỗi đã được retry qua pipeline; không xóa hay bỏ qua thủ công.
- Lịch scraper: `cron(0 */6 * * ? *)`; matcher: `cron(20 */6 * * ? *)`, đều ENABLED. Giờ Việt Nam: cào lúc **01:00, 07:00, 13:00, 19:00**; alert sau đó 20 phút. Trước sửa hai lịch lệch nhau khoảng 3 giờ.
- Cấu hình Lambda: ETL 256 MB/30 giây, webhook 1.024 MB/30 giây, matcher 1.024 MB/300 giây, scraper 512 MB/900 giây. Tăng bộ nhớ webhook/matcher để đủ CPU xử lý dữ liệu thực tế.

Chạy kiểm thử bằng Python 3.12 đã cài dependency, hoặc Python portable trong `dist/python`:

```powershell
& ./dist/python/python.exe -m pytest tests
& ./dist/python/python.exe scripts/diagnose.py
& ./dist/python/python.exe scripts/diagnose.py --sources --keyword "data engineer"
& ./dist/python/python.exe scripts/build_reviewed_package.py
```

Để triển khai lại cùng cấu hình, build ZIP rồi chạy `./dist/python/python.exe scripts/deploy_reviewed.py --profile vieclambot`. Script giữ các biến môi trường hiện có, cập nhật code/cấu hình, TTL, hàng đợi và lịch chạy. `quick_deploy.ps1 -Deploy -Profile vieclambot` chỉ cập nhật code. ZIP dành cho Linux x86_64 / Python 3.12. Các ZIP trong `dist/cloud-backup-20260907/` là bản sao code production trước thay đổi, có thể dùng rollback riêng từng Lambda; cấu hình vận hành không nằm trong ZIP.

Lịch và target, event mapping SQS, quyền IAM và TTL đã được kiểm tra trên cloud. PROFILE/SUB không có TTL nên không bị cơ chế này xóa. Khi dùng boto3 trên máy với `aws login`, cài `boto3[crt]` (đã cập nhật requirements). Không đưa access key, Telegram token hoặc file credential vào Git.

Matcher hỗ trợ event `{"dry_run": true}`: thống kê việc chưa gửi, không ghi trạng thái và không gửi Telegram. Chạy dry-run trước khi dùng lại lịch alert. Lần triển khai đầu chưa có lịch sử SENT nên có thể báo lại các tin trong cửa sổ hồi phục 7 ngày.

## Giới hạn cần biết

- Code và lịch chạy đã triển khai, pipeline cào/nạp và matcher dry-run đã kiểm tra. Cần quan sát lần chạy theo lịch tiếp theo để xác nhận giao tin thực tế; dry-run không kiểm tra việc Telegram nhận tin của từng người.
- Gửi Telegram và ghi DynamoDB không phải một transaction: nếu tiến trình chết ngay sau Telegram nhận tin nhưng trước khi ghi SENT, lần retry có thể lặp lại đúng trang đó. Thiết kế ưu tiên không mất alert; không cam kết exactly-once.
- Tìm kiếm vẫn dùng DynamoDB Scan đầy đủ, đã đo trên snapshot hơn 15.000 job và matcher thực tế khoảng 9–14 giây sau tối ưu cache/chuẩn hóa văn bản. Khi kho lớn hơn cần chỉ mục tìm kiếm chuyên dụng hoặc chỉ mục token để giảm thời gian và chi phí đọc.
- Synonyms mở rộng các nghề tương đương, không phải hiểu ngôn ngữ tùy ý bằng LLM. Chưa tự sửa mọi lỗi chính tả. Lương chưa công bố vẫn được chấp nhận khi có bộ lọc lương.
- Số job thực phụ thuộc nguồn có truy cập được, API key, layout và lịch cào. Không thể đảm bảo mọi website luôn trả dữ liệu. Trường hợp ngân sách cào gần hết được ghi `keywords_deferred`; từ khóa được luân phiên ở lần sau.

## Cấu hình mới

| Biến môi trường | Mặc định | Ý nghĩa |
| --- | ---: | --- |
| `VIECLAMBOT_ALERT_RECOVERY_DAYS` | 7 | Cửa sổ cào để bù alert bị lỡ |
| `VIECLAMBOT_ALERT_MAX_JOBS_PER_USER` | 15 | Trần job gửi mỗi người trong một lần chạy |
| `VIECLAMBOT_SEARCH_RESULT_LIMIT` | 100 | Số kết quả giữ cho `/more`, tối đa 200 và có trần byte |
| `VIECLAMBOT_MAX_SUBSCRIPTIONS` | 10 | Số từ khóa tối đa mỗi người |
| `VIECLAMBOT_SCRAPE_WORKERS` | 4 | Số nguồn cào đồng thời |
| `VIECLAMBOT_SCRAPE_MAX_PAGES` | 5 | Số trang tối đa mỗi từ khóa/nguồn, nay được sử dụng |
