# ViecLamBot - Vietnam Job Aggregator and Telegram Alert System

ViecLamBot is a serverless job aggregator and automated notification system designed to crawl, process, filter, and deliver job opportunities in Vietnam directly to users via Telegram. It supports multiple major job boards, real-time live parallel search, Vietnamese spelling tolerance, and intelligent search results distribution.

---

## Architecture Overview

The system is built on a serverless microservices architecture deployed to AWS, utilizing AWS Lambda, Amazon DynamoDB, Amazon S3, Amazon SQS, Amazon EventBridge, and Amazon API Gateway.

The system consists of four primary pipelines:

### 1. Scraper Pipeline
Triggered periodically by an EventBridge scheduled rule (every 6 hours). It runs all configured scrapers for active search keywords (combining multi-industry seed keywords and active user subscriptions) and publishes raw job postings to an SQS queue.

### 2. ETL and Ingestion Pipeline
Triggered by messages arriving in the SQS raw jobs queue. A Lambda consumer processes the raw jobs:
- Normalizes job titles, locations, and salaries.
- Deduplicates postings using a deterministic hash of the job URL.
- Cleans and formats the text (combining diacritics into NFC unicode format).
- Loads the processed jobs into DynamoDB (with a 60-day Time-To-Live expiration) and uploads the raw payload into an S3 Data Lake.

### 3. Matcher Pipeline
Triggered by an EventBridge scheduled rule (every 6 hours, offset 15 minutes after the scraper pipeline starts). It:
- Queries newly ingested jobs from DynamoDB.
- Matches them against active user keyword subscriptions.
- Groups matches by user and sends aggregated job alert digests via the Telegram Bot API.

### 4. Interactive Bot Webhook Pipeline
Triggered instantly when a user interacts with the Telegram bot. API Gateway proxies the Telegram webhook payloads to a Lambda function, which routes commands:
- Subscription management (subscribe, unsubscribe, list).
- Live parallel search (scrapes all 6 platforms in real time, processes jobs through ETL, updates the database, and returns interleaved results directly to the user).

---

## Folder Structure

The repository is organized as follows:

```text
Project DE/
├── .env                  - Environment variables for local execution
├── requirements.txt      - Python dependencies
├── pyproject.toml        - Test configurations (pytest, ruff)
├── dist/                 - Target directory for Lambda zip deployment packages
├── lambdas/              - Entry points for AWS Lambda handlers
│   ├── scraper_handler.py
│   ├── etl_handler.py
│   ├── matcher_handler.py
│   └── bot_webhook_handler.py
├── scripts/              - Local helper, testing, and deployment scripts
│   ├── local_bot_polling.py  - Runs Telegram bot locally using long polling
│   ├── local_pipeline_run.py - Runs the scraper/ETL pipeline locally
│   ├── setup_aws_resources.py- Provisions DynamoDB tables, SQS, S3, and roles
│   └── deploy_lambdas.py     - Packages and deploys code to AWS Lambda
└── src/                  - Core application logic
    ├── bot/
    │   └── handler.py        - Telegram Bot commands and interactive routing
    ├── common/
    │   ├── aws_clients.py    - Centralized cached boto3 client initializers
    │   ├── logger.py         - JSON log format configurations
    │   └── models.py         - Pydantic schemas for Job, RawJob, User, Subscription
    ├── config.py             - Central configuration settings (Pydantic Settings)
    ├── data_quality/
    │   └── validators.py     - Schema validator and filters for crawled raw data
    ├── etl/
    │   ├── loader.py         - DynamoDB and S3 loading/deduplication queries
    │   └── transformer.py    - Data cleaning, normalization, and tag extraction
    ├── matcher/
    │   └── keyword_matcher.py- Matches jobs to users based on keywords
    └── scrapers/
        ├── base_scraper.py   - Base abstract scraper with rate limits and session retries
        ├── careerlink_scraper.py
        ├── careerviet_scraper.py
        ├── itviec_scraper.py
        ├── jooble_scraper.py
        ├── timviec365_scraper.py
        ├── vieclam24h_scraper.py
        ├── mywork_scraper.py  - Merged with ViecLam24h (skips to avoid duplicates)
        └── timviecnhanh_scraper.py - Merged with ViecLam24h (skips to avoid duplicates)
```

---

## Supported Job Sources

The system aggregates job openings from six major platforms:

- **CareerLink.vn**: Parsed from Next.js server-rendered HTML.
- **ViecLam24h.vn**: Crawled from TailWind-based Tailwind-CSS static HTML.
- **ITviec.com**: Scrapes technology-specific jobs.
- **CareerViet.vn**: (Formerly CareerBuilder Vietnam) Parsed from server-side rendered HTML.
- **TimViec365.vn**: Parsed from static HTML using custom card parsing selectors.
- **Jooble API**: Queries the Jooble job aggregator API using a developer API key.

---

## Database Design (DynamoDB Schema)

A single-table design concept is partially used for user profiles and subscriptions, while job postings are stored in a separate table due to different lifecycle and querying requirements.

### Table 1: Users and Subscriptions (vieclambot-users)
- **Partition Key (PK)**: `user_id` (Telegram Chat ID, e.g., `123456789`)
- **Sort Key (SK)**:
  - For user profiles: `USER`
  - For subscriptions: `SUB#<normalized_keyword>` (e.g., `SUB#data engineer`)
- **Key Attributes**:
  - User: `username`, `first_name`, `registered_at`, `is_active`
  - Subscription: `keyword_raw`, `keyword_normalized`, `location_filter`, `subscribed_at`, `is_active`

### Table 2: Jobs (vieclambot-jobs)
- **Partition Key (PK)**: `job_id` (Hex string hash generated deterministically from the job's URL)
- **Key Attributes**: `title`, `title_normalized`, `company`, `location`, `location_normalized`, `salary_raw`, `salary_min`, `salary_max`, `description`, `source`, `source_url`, `tags`, `posted_at`, `scraped_at`, `ttl` (Time-to-live timestamp)

---

## Data Engineering Principles and Implementations

The architecture of ViecLamBot is designed in accordance with core data engineering patterns to build scalable, resilient, and high-quality data pipelines.

### 1. Decoupled Ingestion with Message Queue Buffering
The ingestion of scraped job postings is decoupled from downstream ETL processing using Amazon SQS:
- **Load Leveling**: The Scraper Lambda pushes raw items to the SQS queue immediately upon scraping, protecting the DynamoDB database from write-throttling during high-volume spikes.
- **Resilience and Retries**: If the ETL consumer Lambda fails to process a batch (due to database unavailability or malformed schema), the messages remain in the queue or are sent to a dead-letter queue, ensuring zero data loss.

### 2. Idempotent Ingestion and Deterministic Hashing
To prevent duplicate job records in DynamoDB and duplicate alerts to users:
- Job postings from different sources are mapped to a deterministic `job_id` using the SHA-256 hash of their canonical URL.
- DynamoDB uses `job_id` as its Partition Key. This ensures that repeated ingestion of the same job posting acts as a clean upsert (update/insert) rather than generating duplicate records.

### 3. Multi-Stage Data Quality and Validation Gates
Data quality is enforced at multiple check gates within the pipeline using validators:
- **Raw Validation Gate (Post-Scrape)**: The `RawJobValidator` checks that critical fields like the job title and source URL are present, and computes batch completeness metrics (such as the Completeness Score and Pass Rate) to monitor scraping quality over time.
- **Processed Validation Gate (Post-Transform)**: Before saving to the database, the `ProcessedJobValidator` performs logical sanity checks, ensuring normalized salary ranges are valid (e.g., `salary_min <= salary_max` and values are positive) and critical primary keys are present.

### 4. Raw Archival and the S3 Data Lake Pattern
Following standard data lake architectures, raw and processed data are kept separate:
- **Raw Ingestion Archive**: The raw JSON payload returned by every scraper run is archived directly to Amazon S3 (partitioned chronologically under `raw/all_sources/YYYY/MM/DD/`).
- **Auditability and Backfill**: Archiving raw payloads allows the entire pipeline to be audited, and enables future historical backfilling if the ETL transformation schema changes or if additional tags need to be extracted retroactively.

### 5. Standardized Data Normalization (ETL)
The ETL Transformer class converts unstructured raw data into high-fidelity structured formats:
- **Unicode Normalization**: Text is standardized to NFC form to ensure consistency across multiple Vietnamese keyboard encodings.
- **Location Standardization**: Raw location strings are mapped to standard Vietnamese provinces and cities using a predefined dictionary map to ensure accurate geography-based querying.
- **Numerical Salary Extraction**: Various currency strings (e.g., USD, VND, million-VND ranges) are parsed using regular expressions and converted into uniform numerical integer values representing min/max salaries in VND.
- **Tag Extraction**: Standardized technical tags and skill sets are extracted from descriptions and requirements using word-boundary regular expression token matching.

---


## Search Features and Algorithms

Interactive searching via the bot implements two advanced matching algorithms:

### 1. Vietnamese Accent and Spelling Tolerance
To ensure query matching succeeds even when users type spelling variations or accents inconsistently:
- Text is converted to precomposed NFC Unicode form.
- Accents are stripped from all characters (e.g., "năng lượng" becomes "nang luong").
- Diacritic positioning differences (e.g., "hoà" vs "hòa") are automatically resolved during accent removal.
- The vowels "i" and "y" are treated as interchangeable (e.g., "kĩ" and "kỹ" both normalize to "ki").
- Cleaned query tokens must be subsets of the cleaned job title or tag strings.

### 2. Time-Based Filtering and Round-Robin Interleaving
To prevent a single active website from dominating the top results (for instance, when one site returns 50 matches and others only return 2):
- **Age Filter**: Results are filtered to show only jobs posted or scraped within the last 7 days. If no recent jobs are found, it falls back to older listings to prevent empty screens.
- **Interleaving**: Results are grouped by job board source. Within each source, jobs are sorted chronologically. The final result set is built by taking the first job from each source, then the second job from each, in a round-robin cycle up to a maximum limit of 20 display entries.

---

## Configuration Settings

The application loads configuration parameters from environment variables with a `VIECLAMBOT_` prefix.

Key variables configured in `.env` include:
- `VIECLAMBOT_TELEGRAM_BOT_TOKEN`: The API token obtained from Telegram BotFather.
- `VIECLAMBOT_AWS_REGION`: The target AWS region (default: `ap-southeast-1`).
- `VIECLAMBOT_DYNAMODB_JOBS_TABLE`: Name of the DynamoDB jobs table.
- `VIECLAMBOT_DYNAMODB_USERS_TABLE`: Name of the DynamoDB users table.
- `VIECLAMBOT_DYNAMODB_SUBSCRIPTIONS_TABLE`: Name of the DynamoDB subscriptions table (for legacy systems; now mapped to the users table).
- `VIECLAMBOT_S3_DATA_LAKE_BUCKET`: S3 bucket name used to archive raw scraped payloads.
- `VIECLAMBOT_SQS_RAW_JOBS_QUEUE`: SQS queue name for raw scraped postings.
- `VIECLAMBOT_JOOBLE_API_KEY`: Developer key used for Jooble API scraper.
- `VIECLAMBOT_LOG_LEVEL`: Logger level (e.g., `INFO`, `DEBUG`).

---

## Development and Testing

All scripts must be run using the Python launcher. Ensure the console encoding is set to UTF-8 on Windows environments.

### Setting Up Environment Variables
Create a `.env` file in the project root containing your AWS credentials and Telegram bot token:
```text
VIECLAMBOT_TELEGRAM_BOT_TOKEN=your_token_here
VIECLAMBOT_AWS_REGION=ap-southeast-1
VIECLAMBOT_DYNAMODB_JOBS_TABLE=vieclambot-jobs
VIECLAMBOT_DYNAMODB_USERS_TABLE=vieclambot-users
VIECLAMBOT_JOOBLE_API_KEY=your_jooble_key_here
```

### Running Tests
Execute the test suite using pytest:
```bash
set PYTHONUTF8=1
py -m pytest
```

### Running Scrapers Locally
You can test the scraper and pipeline ingestion scripts locally using the following helper scripts:
```bash
# Test all scrapers live and output transformed results to the terminal
set PYTHONUTF8=1
py scripts/test_scrapers_live.py

# Run a full ETL pipeline iteration locally using your AWS credentials
set PYTHONUTF8=1
py scripts/local_pipeline_run.py
```

### Running Bot Locally (Long Polling)
For local testing of Bot commands without registering an API Gateway URL:
```bash
set PYTHONUTF8=1
py scripts/local_bot_polling.py
```
This script will temporarily disable the live webhook, read messages directly from Telegram, and execute the command logic using your local code.

---

## Deployment to AWS

To deploy or update the serverless infrastructure on AWS:

1. **Provision Resources**:
   Run the AWS setup script to create the necessary DynamoDB tables, SQS queues, S3 buckets, and IAM roles:
   ```bash
   py scripts/setup_aws_resources.py
   ```

2. **Deploy Lambdas & API Gateway**:
   Build the Linux-compatible deployment archive containing python package dependencies and project source files, update the 4 AWS Lambda functions, configure triggers, and publish the API Gateway REST API endpoints:
   ```bash
   py scripts/deploy_lambdas.py
   ```
   Upon successful deployment, the script automatically updates the webhook URL in the Telegram API to point to your new API Gateway deployment.

---

## Bot Telegram Commands

The bot supports the following commands:

- `/start`: Registers the user's profile and displays the welcome menu.
- `/subscribe <keyword> [| location]`: Subscribes the user to receive alerts. (e.g., `/subscribe ke toan | ho chi minh`). Maximum 3 active subscriptions per account.
- `/unsubscribe <keyword>`: Unsubscribes from the specified keyword.
- `/list`: Lists all current active subscription keywords for the account.
- `/myjobs` or `/jobs`: Compiles and returns the top 20 latest jobs matching the user's subscribed keywords using the age-filtering and interleaving distribution.
- `/search <keyword> [| location]`: Performs a live parallel search across all 6 scraper sources, ingests the results, and displays the top 20 interleaved matches.
- `/help`: Displays a detailed user manual and help guide.
- **Direct Text Input**: If the user sends a non-command text message, the bot automatically handles it as a search query.
