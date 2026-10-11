#!/usr/bin/env bash
# قطره — اجرای محلی وب‌اپ
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install -q fastapi "uvicorn[standard]" sqlalchemy jinja2 \
  python-frontmatter markdown jdatetime apscheduler python-multipart

PORT="${PORT:-8000}"
echo "قطره روی http://127.0.0.1:${PORT} بالا می‌آید… (Ctrl+C برای توقف)"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" "$@"
