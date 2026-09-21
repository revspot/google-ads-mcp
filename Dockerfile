# Google Ads MCP — HTTP deployment.
#
# Upstream is a stdio server with no Dockerfile; this serves it over streamable HTTP
# behind nginx, one container for every revspot client, with the tenant arriving per
# request (see http_server.py).
FROM python:3.12-slim

WORKDIR /app

# Dependencies first, so a code change does not rebuild scipy/matplotlib/google-ads.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY google_ads_mcp.py http_server.py ./
COPY managers/ ./managers/
COPY tools/ ./tools/
COPY utils/ ./utils/

ENV PORT=5008 \
    PYTHONUNBUFFERED=1
EXPOSE 5008

# /health is answered before the tenant middleware, so the probe needs no credentials.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','5008')}/health\", timeout=4)"

# uvicorn, not `python google_ads_mcp.py`: that entrypoint is main() -> mcp.run(), which
# is stdio and would leave the port unanswered.
CMD ["sh", "-c", "uvicorn http_server:app --host 0.0.0.0 --port ${PORT}"]
