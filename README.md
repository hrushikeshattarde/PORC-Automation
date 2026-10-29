# PORC Automation — Zeus PO / Rate Confirmation bot

Automates the rate-confirmation loop for Transport Pro (TPro) customer **8828, "Walmart c/o SDI MRO"**:

1. find the customer's loads that reached **Delivered** in TPro,
2. create an order in **Zeus** (SDI Supplier Portal) from the load, attach the load's unsigned Bill of Lading, and submit it,
3. download the submitted order's PDF from Zeus,
4. attach that PDF to the TPro load as a **Rate Confirmation** document.

In production it runs every hour, 24/7, as an AWS Lambda container image.

## How it works

```
TPro public API ──> delivered loads ──> field mapping ──> BOL checks ──> Zeus (Playwright) ──> order PDF ──> TPro upload
  /load/search        not yet done        porc/mapper.py    porc/docs.py   porc/zeus_portal.py               /files/upload
  /load/{id}                                                                                                 type "Rate Confirmation"
  /files/search, /files/{id}
TPro Postgres (read-only) ──> load "Internal Comments" = Zeus "Requested By"   (porc/tpro_db.py)
```

| File | Role |
| --- | --- |
| `run_bot.py` | command-line entry point |
| `lambda_handler.py` | AWS Lambda entry point: loads credentials from SSM, runs with `--limit 8` unless the event passes other `args` |
| `porc/config.py` | settings from `.env` (locally) or the environment |
| `porc/tpro_client.py` | TPro public API: token auth, paged search, load detail, file list / download / upload |
| `porc/tpro_db.py` | read-only Postgres access for the load's Internal Comments (not exposed by the API) |
| `porc/mapper.py` | load -> Zeus order fields, BOL selection, Requested By parsing, Sam's Club "strip the S" rule |
| `porc/docs.py` | proves the BOL PDF is the Bill of Lading for this load before Zeus is opened |
| `porc/zeus_portal.py` | Playwright driver for the Zeus Blazor app: sign-in, order header, non-catalog item, upload, submit, PDF |
| `porc/runner.py` | orchestration, safety checks, health check, CSV run report |
| `porc/state.py` | ledger of each load's stage, so an interrupted run resumes instead of creating a second order |
| `porc/cloud.py` | S3 persistence of the ledger and run artifacts; SSM parameter loading |
| `deploy/put_parameters.py` | copies credentials from `.env` into SSM Parameter Store (SecureString) |
| `deploy/deploy.ps1` | builds the image, pushes it to ECR, points the Lambda at it |

## Field mapping (TPro -> Zeus)

| Zeus field | Source in TPro | Notes |
| --- | --- | --- |
| Work Order # | `reference.referenceNumber` | punctuation stripped, e.g. `(123456789)` -> `123456789`; falls back to the BOL # |
| Requested By | load **Internal Comments** (`public.loads.gn_comments`, read from Postgres) | formats seen: `123456789 Jane Smith` -> `Jane Smith`, `REF# 234567890 PAT PORTER` -> `PAT PORTER`, `(345678901) maria.lopez @example.com` -> `maria.lopez`. Fallbacks: a `(First Last)` name in the shipper instructions, then `PORC_REQUESTED_BY_DEFAULT` |
| Expected Delivery Date | today | `M/D/YYYY` |
| Delivery Method | always **Ship To** | |
| Ship To | code read from Zeus **Install Location** after the WO# is entered | trailing `S` removed for Sam's Club (`W08151S` -> `W08151`); store number cross-checked against the TPro consignee |
| Note To Customer | shipper + consignee stop Instructions/Directions | |
| Mfg. Name | shipper company name | |
| Mfg. Part # | Work Order # | |
| Supplier Part # | TPro load ID | |
| Description | consignee name + address | |
| Quantity / UoM | 1 / Each | Zeus defaults |
| Unit Price | freight bill total (`charges.totalFreight` + `totalFuel`) | the Zeus Order Total must equal it before Submit |
| File upload | the load's unsigned BOL PDF | see the five gates below |

**Requested By is a people lookup** in Zeus (`FIRST LAST e-mail`), not free text. The bot types the name and
clicks the suggestion that contains it. When nothing matches (a typo in TPro such as `maria.lopez` vs Zeus
`maria.lopes`), it re-queries with the first two thirds of the text and accepts a candidate only if exactly one
suggestion is at least 90% similar; otherwise the load fails before anything is submitted.

## Why the wrong file cannot reach Zeus

A submitted Zeus order cannot be deleted, so the Bill of Lading passes five gates, all before "Submit Order":

1. **Selection** (`mapper.select_bol`): PDF only, comment starts with `Bill Of Lading` or type *Bill Of Lading*,
   uploaded before delivery, never a carrier-invoice copy, driver-supplied document or POD. No candidate means
   the load is skipped.
2. **Download** (`tpro_client.download_file`): the API returns the file base64-encoded inside JSON; it is decoded
   and anything that is not a real PDF/image is rejected. A fresh copy is fetched on every run.
3. **Content check** (`docs.verify_bol_pdf`): the PDF's own text must say "Bill Of Lading" and contain this load's
   TPro load number (the unsigned BOL prints `Trip #: <load id>`). A scanned or signed copy without a text layer,
   another load's BOL, or any other document fails here and Zeus is never opened.
4. **Upload confirmation** (`zeus_portal._wait_for_upload`): Zeus must show the upload's success state *and* list
   the file name; an upload error aborts with the draft unsubmitted.
5. **Order Total**: the Zeus total must equal the freight bill right before Submit.

**No leftover drafts:** a draft left by an interrupted run is deleted and rebuilt from scratch, never reused.

**No duplicate orders:** before "Start New Order" the bot scans Zeus *Pending Approval* for an order with the same
Work Order # **and** the same total. If one exists (for example created by hand without its PDF being attached
in TPro), the bot creates nothing; it attaches that order's PDF to the TPro load instead.

## Other safety rules

* Only **Delivered** loads for the configured customer, delivery date within `PORC_LOOKBACK_DAYS` and never
  before the go-live date `PORC_START_DATE`.
* Skipped when TPro already has a **Rate Confirmation** (document type 143), or the ledger shows the load done.
* Skipped (and reported) when there is no Reference #, no qualifying BOL, or the **Freight Bill is $0** (below
  `PORC_MIN_FREIGHT`). Such loads are re-checked every run, so they are picked up once the data is fixed in TPro.
* The Zeus Install Location store number must match the TPro consignee's; with `PORC_STRICT_STORE_MATCH=true` a
  mismatch skips the load, otherwise it is logged as a warning.
* Ledger stages `zeus_submitted` -> `pdf_downloaded` -> `uploaded` are written as they happen; the submit is
  recorded the moment the button is clicked, so a crash can never lead to a second order.
* Every Zeus step leaves a screenshot and HTML snapshot (`discovery/`, or S3 in AWS) for troubleshooting.

## Configuration

Copy `.env.example` to `.env` and fill it in. Never commit `.env`.

| Variable | Purpose |
| --- | --- |
| `TPRO_BASE_URL`, `TPRO_USERNAME`, `TPRO_PASSWORD` | TPro public API |
| `ZEUS_URL`, `ZEUS_USERNAME`, `ZEUS_PASSWORD` | Zeus sign-in (lower-case `link` / `username` / `password` also accepted) |
| `TPRO_DB_HOST`, `TPRO_DB_PORT`, `TPRO_DB_NAME`, `TPRO_DB_USER`, `TPRO_DB_PASSWORD` | read-only Postgres for Internal Comments |
| `PORC_CUSTOMER_ID` | TPro customer (default `8828`) |
| `PORC_START_DATE` | go-live date `YYYY-MM-DD`; earlier deliveries are never processed |
| `PORC_LOOKBACK_DAYS` | delivery-date window (default 14, max 59) |
| `PORC_MIN_FREIGHT` | minimum freight bill (default `0.01`, so $0 is skipped) |
| `PORC_REQUESTED_BY_DEFAULT` | fallback requester when Internal Comments has no name |
| `PORC_STRICT_STORE_MATCH` | skip on a store-number mismatch (default `false`) |
| `PORC_HEADLESS` | run the browser without a window (default `true`) |
| `PORC_SSM_PREFIX`, `PORC_S3_BUCKET`, `PORC_DATA_DIR` | AWS only: credential prefix, state bucket, writable data folder |

## Local use

```bash
python -m pip install -r requirements.txt
```

```bash
python -m playwright install chromium
```

| Command | What it does |
| --- | --- |
| `python run_bot.py --check` | health check of the TPro API, TPro database, S3 and Zeus sign-in; touches no load |
| `python run_bot.py --dry-run --limit 10` | prints the exact Zeus order it would create for each load |
| `python run_bot.py --discover --headed` | signs in and saves screenshots/HTML of the Zeus screens; never submits |
| `python run_bot.py --load-id <id> --headed --no-submit` | fills the whole order with the browser visible, then deletes the draft |
| `python run_bot.py --load-id <id> --pause-before-submit` | waits for Enter before "Submit Order" |
| `python run_bot.py --load-id <id> --skip-upload` | submits in Zeus and keeps the PDF locally without attaching it to TPro |
| `python run_bot.py` | normal unattended run |

Each run writes `logs/porc_<date>.log` and `logs/porc_report_<timestamp>.csv` (load, WO#, action, detail, PDF path, TPro file id).

## AWS deployment

The cheapest fit for an hourly bot that works for seconds to a couple of minutes: a Lambda built from the
`Dockerfile` (Playwright base image + `awslambdaric`), triggered by EventBridge Scheduler. Expected cost is a
few dollars a month.

| Resource | Setting |
| --- | --- |
| ECR image | `<account-id>.dkr.ecr.us-east-1.amazonaws.com/porc-automation:latest` |
| Lambda function | `porc-automation`: 2048 MB, 900 s timeout, 1024 MB `/tmp`, handler `lambda_handler.handler` |
| Execution role | `porc-automation-lambda-role`: CloudWatch logs, the state bucket, `/porc-automation/*` parameters, VPC access |
| Network | the account's private subnets that egress through the shared NAT gateway. The TPro Postgres host only accepts connections from that gateway's allow-listed address; outside the VPC the database drops the connection. |
| State and artifacts | `s3://porc-automation-<account-id>/porc/state/processed_loads.json` and `porc/runs/<timestamp>/` (expire after 30 days) |
| Credentials | SSM Parameter Store `/porc-automation/*` (SecureString), loaded at start via `PORC_SSM_PREFIX` |
| Schedule | EventBridge Scheduler `porc-automation-hourly`, `cron(0 * * * ? *)` in America/New_York, role `porc-automation-scheduler-role` |
| Logs | CloudWatch `/aws/lambda/porc-automation` |

Deploy or update (after `aws sso login`):

```bash
python deploy/put_parameters.py
```

```powershell
.\deploy\deploy.ps1
```

`put_parameters.py` is only needed when a credential changes. Images must be built with
`--provenance=false --sbom=false` (the script does this); Lambda rejects BuildKit attestation manifests.

Manual dry run in AWS:

```bash
aws lambda invoke --region us-east-1 --function-name porc-automation --cli-binary-format raw-in-base64-out --payload "{\"args\":[\"--dry-run\",\"--limit\",\"3\"]}" out.json
```

Pause or resume: update the schedule `porc-automation-hourly` with `--state DISABLED` or `--state ENABLED`
(the update call must repeat the schedule expression, time window and target).

## Production notes

* **TPro API:** the edge blocks the default `python-requests` User-Agent; `/load/search` needs a date filter of at
  most 60 days; `GET /files/{id}` returns JSON with the file base64-encoded in `fileData`, not raw bytes.
* **Internal Comments** is not in the public API at all; it is `public.loads.gn_comments` in the database. It can
  hold a name, an e-mail with a zero-width space, or only a number (no requester); all three are handled.
* **Null data:** a canceled dispatch comes back with `"waypoints": null`; all list fields are null-safe.
* **Zeus lookups** re-render after every keystroke debounce; the suggestion list is read only once two consecutive
  reads agree.
* **Early incident:** before the base64 handling existed, one test order was submitted with the JSON wrapper
  attached instead of the BOL. Gates 2 and 3 above exist because of it; that order had to be corrected in Zeus
  by hand.
