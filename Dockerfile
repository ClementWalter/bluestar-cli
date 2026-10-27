# Always-on watcher image: Python + Playwright Chromium so the container can
# mint booking tokens headlessly, then poll over HTTP.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install runtime deps and the Chromium browser (+ its OS libraries).
# Same exact pins as the PEP 723 header of bluestar_cli.py.
RUN pip install requests==2.34.2 click==8.4.1 playwright==1.60.0 \
    && playwright install --with-deps chromium

WORKDIR /app
COPY bluestar_cli.py .

# Trip params and the ntfy target come from environment (set in fly.toml / secrets).
CMD ["python", "bluestar_cli.py", "poll"]
