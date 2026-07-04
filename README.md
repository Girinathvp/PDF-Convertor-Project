# PDF Bank Statement → Excel Converter

A small Flask app that converts bank statement PDFs to Excel, handling three
real-world layouts:

1. **Lined tables** — bank draws visible ruling lines → `pdfplumber`'s native
   line-based table detector.
2. **Unlined / spaced-text tables** — no gridlines, columns implied only by
   consistent horizontal spacing → reconstructed by clustering word x0
   (columns) and top (rows) positions, the same way a human eye lines up a
   table.
3. **Scanned / vector-text pages** — some bank PDF generators flatten text
   into vector paths or scan the page as an image, so there are literally
   zero extractable characters. **This is the case for the sample PDF you
   provided** (`SIB_2871-...pdf`, 86 pages, 0 chars/page). For these pages
   the app rasterizes with `pdfplumber`'s image renderer and runs Tesseract
   OCR, then applies the same position-clustering logic to the OCR word
   boxes.

The strategy is chosen automatically, per page.

## Directory structure

```
bank_statement_converter/
├── backend/
│   ├── app.py            # Flask API (upload / process / status / download)
│   ├── extractor.py       # extraction + Excel-writing logic
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── script.js
└── README.md
```

## System dependencies

Tesseract OCR must be installed on the host (Python's `pytesseract` is just
a wrapper around the `tesseract` binary):

```bash
# Debian/Ubuntu
sudo apt-get install -y tesseract-ocr

# macOS
brew install tesseract

# Windows: install from https://github.com/UB-Mannheim/tesseract/wiki
# and add the install dir to PATH.
```

No Poppler/`pdf2image` dependency is needed — page rasterization for OCR is
done via `pdfplumber`'s built-in renderer.

## Setup

```bash
cd bank_statement_converter/backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run (development)

```bash
cd bank_statement_converter/backend
python3 app.py
```

Open **http://localhost:5000** in your browser.

## Run (production)

Use gunicorn with multiple threads so status-polling requests aren't blocked
by an in-progress conversion job:

```bash
pip install gunicorn
gunicorn -w 2 --threads 4 -b 0.0.0.0:8000 app:app
```

For heavier concurrent load, swap the in-process `threading.Thread` job
runner in `app.py` for a real task queue (Celery + Redis / RQ) — the
`process_pdf(..., progress_callback=...)` function in `extractor.py` is
already structured to report per-page progress, so it drops in easily.

## How the UI flow works

1. **Upload** (`POST /api/upload`) — file is saved, pages are counted via
   `pdfplumber` (fast, no full extraction yet), and a TAT estimate is
   returned immediately: `"Estimated time: ~45s"`.
2. User ticks **"Merge all pages into a single Excel sheet"** (default) or
   unticks it to get **one Excel tab per PDF page**.
3. **Convert** (`POST /api/process/<job_id>`) — starts a background thread;
   the request returns instantly so large files never hit an HTTP timeout.
4. Frontend polls `GET /api/status/<job_id>` every second and updates a
   progress bar from `pages_done / total_pages`.
5. When `status == "done"`, the **Download** button points at
   `GET /api/download/<job_id>`.

## Deploying for a limited audience (production)

The goal: a real HTTPS URL that only people you share it with can use,
without paying for or managing a full server yourself.

### Security note (read this first)

An "unlisted" URL is **not** real access control — it ends up in browser
history, referrer headers, screenshots, forwarded emails, and proxy/server
logs. Since this app processes real bank statement data, we've added
**optional HTTP Basic Auth** that gates every route. You turn it on by
setting two environment variables (`APP_USERNAME`, `APP_PASSWORD`) on the
host — no code changes needed. Strongly recommended for anything beyond a
same-day personal test.

We've also added:
- `robots.txt` (blocks search engine indexing/crawling of the URL)
- automatic deletion of uploaded PDFs and generated Excel files 1 hour after
  each job completes (`JOB_TTL_SECONDS` in `app.py`), since these contain
  real account/transaction data and shouldn't accumulate on a shared server
- `debug=False` by default outside local dev (Flask's debugger, if left on,
  can expose a remote code execution console)

### Step-by-step: Render (recommended — free tier, Docker-based, auto HTTPS)

1. **Push the code to a GitHub repo** (Render deploys from a repo, not a
   zip upload). Create a new repo, add these files, commit, push. Make sure
   `Dockerfile` sits at the repo root (already the case in this project).

2. **Create a Render account** at https://render.com (free tier is fine to
   start).

3. **New → Web Service** → connect your GitHub repo.

4. Render will detect the `Dockerfile` automatically. Confirm:
   - **Environment**: Docker
   - **Region**: closest to your users
   - **Instance type**: at least the smallest paid tier if you're on the
     larger sample PDFs — OCR is memory/CPU hungry and the free tier's
     512MB RAM will struggle on many-page scanned statements. Start free,
     upgrade if you see out-of-memory errors in the logs.

5. **Add environment variables** (Render dashboard → your service →
   Environment):
   - `APP_USERNAME` = a username you choose
   - `APP_PASSWORD` = a strong password you choose
   (Skip these two if you genuinely want zero login gate — not recommended.)

6. Click **Create Web Service**. Render builds the Docker image (installs
   Tesseract + Python deps) and deploys it. First build takes a few
   minutes.

7. You'll get a URL like `https://your-service-name.onrender.com`. Share
   only that link with your intended users. When they open it, the browser
   will prompt for the username/password you set in step 5.

8. **Optional — a harder-to-guess URL**: rename the Render service to
   something non-obvious (e.g. `stmt-conv-x7k2p`) instead of something like
   `bank-statement-converter`, so the subdomain itself isn't guessable even
   before the login prompt.

9. **Optional — custom domain**: Render → your service → Settings →
   Custom Domain, if you'd rather share `convert.yourdomain.com`.

### Alternative platforms

- **Railway** (https://railway.app): same idea — connect repo, it detects
  the Dockerfile, add `APP_USERNAME`/`APP_PASSWORD` as variables, deploy.
  Slightly less free-tier runway than Render.
- **Fly.io** (https://fly.io): `fly launch` in the repo directory detects
  the Dockerfile; `fly secrets set APP_USERNAME=... APP_PASSWORD=...` for
  the auth vars; `fly deploy`. Good free allowance, slightly more CLI-heavy.
- **A VPS you already have** (DigitalOcean/Linode/EC2/etc.): 
  ```bash
  git clone <your-repo> && cd bank_statement_converter
  docker build -t bank-statement-converter .
  docker run -d -p 80:5000 \
    -e APP_USERNAME=youruser -e APP_PASSWORD=yourpass \
    --restart unless-stopped \
    bank-statement-converter
  ```
  Then put Caddy or nginx + certbot in front for free HTTPS, or use a
  provider with a built-in load balancer/TLS terminator.

### After deploying: quick checklist

- [ ] Visit the URL yourself first — confirm the Basic Auth prompt appears
      (if you set the env vars) and that upload → convert → download works
      end-to-end on the live URL, not just locally.
- [ ] Try uploading a large multi-page scanned PDF and confirm it doesn't
      time out (bump `--timeout` in the Dockerfile's `CMD` further if it
      does, and/or upgrade the instance size).
- [ ] Share the URL (and credentials, via a separate channel like a
      password manager or encrypted message, not the same email as the
      link) only with the people who should have access.
- [ ] Periodically check the host's logs for unexpected traffic if this
      stays up long-term.

### Known scaling limits of this setup

- Job state (`jobs` dict) lives in server memory. This is fine for a
  single small instance with a handful of concurrent users, but means: (a)
  restarting the service loses in-progress job status, and (b) you must
  keep `gunicorn -w 1` (one worker) — multiple workers wouldn't share the
  same in-memory job list. For real multi-user scale, swap this for
  Redis-backed job storage and a proper task queue (Celery/RQ), and you can
  then safely run multiple workers.
- OCR pages are slow (~1-3s/page) and memory-hungry; a very large all-scanned
  PDF on a small instance may still be slow or hit memory limits. Consider
  a queue + worker architecture if this becomes the primary workload rather
  than an occasional one-off conversion.


- Rows/columns are normalized (padded to the widest row) before being
  written, so no cells are silently dropped even when different pages infer
  different column counts.
- When merging pages into one sheet, a blank row is inserted between pages
  so page boundaries stay visually identifiable (and Excel filters won't
  merge unrelated statement pages' headers).
- OCR accuracy depends on scan quality; for consistently OCR'd statements
  you may want to tune `dpi` in `extractor._extract_ocr` (higher = more
  accurate but slower) or add a bank-specific column-boundary override if a
  bank's layout is known ahead of time.

## Known limitation

Tesseract OCR is noticeably slower than text-layer extraction (roughly
1–3s/page vs <0.1s/page). For large all-scanned statements (e.g. the 86-page
sample), expect actual runtime above the initial TAT estimate — the
estimate is refined implicitly by the live progress bar once processing
starts. If you routinely receive this type of "flattened text" PDF, you can
also try Camelot/tabula first as an alternative to pdfplumber for the lined
case; they're not included by default here to keep the dependency footprint
(and install time) minimal, but can be added in `extractor.py`.
