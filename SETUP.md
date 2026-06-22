# Setup & Run

## Prerequisites (system libraries)

The service shells out to `ffmpeg`/`ffprobe` and uses `libmagic` for content
sniffing. Install these at the OS level first:

```bash
# Debian / Ubuntu
sudo apt-get install -y ffmpeg libmagic1

# macOS
brew install ffmpeg libmagic
```

## Storage

This service stores all uploaded media in **S3-compatible object storage**
(configured here for Hetzner Object Storage). There is no local-disk storage
backend. You must create a bucket in the Hetzner Console first, then put its
name in `.env` (`S3_BUCKET`).

> Note: `/tmp/media_processing` is still used as scratch space — `ffmpeg`
> extracts/chunks audio to local temp files before processing. This is not
> persistent storage; it's working scratch only.

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Required keys: `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`S3_ENDPOINT_URL`, `S3_REGION`. The app will refuse to start if the required
S3 settings are missing.

## Virtual environment

```bash
# 1. create
python3.10 -m venv .venv

# 2. activate
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows (PowerShell)

# 3. install runtime + dev dependencies
pip install -r requirements.txt

# 4. run the API
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 5. run the Celery worker (separate terminal, needs Redis running)
celery -A workers.celery_app worker --loglevel=info --pool=solo

# 6. run the tests
pytest -q
```

## Docker

```bash
docker compose up --build       # starts backend + celery + redis
```
