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

## Configuration

All settings come from the environment or a local `.env` file (see
`config/setting.py` for the full list). Minimum useful overrides:

```dotenv
ENVIRONMENT=production
LOG_LEVEL=INFO
REQUIRE_MAGIC=true              # enforce content sniffing in prod
STORAGE_BACKEND=s3
S3_BUCKET=my-bucket
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

## Docker

```bash
docker compose up --build       # starts backend + celery + redis
```
