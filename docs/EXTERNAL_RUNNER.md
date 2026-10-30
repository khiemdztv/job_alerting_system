# Runner ngoài cho các board chặn IP AWS

> **Đây là thử nghiệm.** Runner của GitHub Actions cũng là IP datacenter (Azure), nên JobsGO, TimViec365
> hay vieclam.gov.vn có thể chặn cả runner này. Mục tiêu của lần chạy đầu là biết board nào đi qua được.

Workflow `.github/workflows/scrape-external.yml` chạy mỗi 2 giờ (và bằng tay qua *Run workflow*), mỗi
board một step với `continue-on-error`, tối đa 20 phút:

```
python scripts/local_pipeline_run.py --sources jobsgo
python scripts/local_pipeline_run.py --sources timviec365
python scripts/local_pipeline_run.py --sources vieclam_gov
```

Chế độ `--sources` chỉ cào, ghi bản thô lên S3 (`raw/<nguồn>/...`) và đẩy vào SQS `vieclambot-raw-jobs`;
ETL Lambda xử lý như tin từ scraper Lambda. Nó không chạy ETL/matcher cục bộ. Cờ khác:
`--keywords "kế toán,it"` (mặc định: nhóm từ khóa seed hiện tại) và `--latest-only` (chỉ trang tin mới
nhất mỗi board). Không có cờ nào thì script chạy pipeline cục bộ đầy đủ như trước.

## IAM user cho runner

Tạo user `vieclambot-external-runner` (chỉ access key, không có quyền console) với policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sqs:SendMessage", "sqs:GetQueueUrl"],
      "Resource": "arn:aws:sqs:ap-southeast-1:132058735660:vieclambot-raw-jobs"
    },
    {
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::vieclambot-data-lake-132058735660/raw/*"
    }
  ]
}
```

Tùy chọn: để runner ghi sức khỏe nguồn (trạng thái `blocked`/`ok`, dùng để đọc kết quả) thêm
`dynamodb:GetItem` và `dynamodb:PutItem` trên `arn:aws:dynamodb:ap-southeast-1:132058735660:table/vieclambot-users`.
Không có quyền này thì log vẫn có đủ thông tin, chỉ không có bản ghi sức khỏe. Lưu ý bản ghi này dùng chung
với scraper Lambda: 5 lần liên tiếp lỗi sẽ tự tạm dừng nguồn, nên với board vốn bị chặn ở cả hai nơi, hãy bỏ
quyền này nếu không muốn cảnh báo.

## Secrets của repository (Settings > Secrets and variables > Actions)

| Secret | Giá trị |
|---|---|
| `AWS_ACCESS_KEY_ID` | access key của user trên |
| `AWS_SECRET_ACCESS_KEY` | secret key của user trên |
| `VIECLAMBOT_S3_DATA_LAKE_BUCKET` | `vieclambot-data-lake-132058735660` |

Region cố định `ap-southeast-1`. Không cần `VIECLAMBOT_TELEGRAM_BOT_TOKEN` (workflow đặt giá trị giả).

## Đọc kết quả lần chạy đầu

1. Actions > *Scrape external* > *Run workflow*, mở từng step.
2. Cuối log mỗi step có JSON tóm tắt. Với mỗi board xem `sources.<nguồn>.status` và `valid`:
   - `ok` và `valid > 0`: runner đi qua được, tin đã vào SQS.
   - `blocked`: board trả HTTP 403/429/503 cho IP của GitHub. Thử lại sau vài lần chạy; nếu vẫn vậy thì
     board cần proxy hoặc VPS dân cư, hãy tắt step đó.
   - `empty`: tải được trang nhưng không có tin; `layout_changed`: board đổi giao diện (không phải bị chặn).
   - `error`: lỗi mạng/timeout (thường là IP bị chặn ở tầng kết nối).
3. Kiểm tra SQS/S3: có đối tượng mới dưới `raw/jobsgo/<ngày>/` hoặc tin mới trong DynamoDB sau vài phút.
4. Step đỏ không làm hỏng các step khác nhờ `continue-on-error`.

Chi phí: 3 board x vài phút x 12 lần/ngày, khoảng 700-1000 phút/tháng, trong hạn mức miễn phí 2.000 phút
của repo riêng tư (repo công khai không giới hạn).
