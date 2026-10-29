# PORC Automation - Zeus PO / Rate Confirmation bot, packaged as an AWS Lambda container image.
# Base image ships Chromium + every OS dependency for this exact Playwright release.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORC_HEADLESS=true \
    TZ=America/New_York

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt boto3 awslambdaric

COPY run_bot.py lambda_handler.py ./
COPY porc ./porc

# Lambda: the Runtime Interface Client calls lambda_handler.handler.
# Local / ECS / EC2: override the entrypoint, e.g.
#   docker run --rm --env-file .env --entrypoint python porc-automation run_bot.py --dry-run --limit 3
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["lambda_handler.handler"]
