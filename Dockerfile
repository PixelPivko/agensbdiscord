FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt
RUN python -c "import discord, aiosqlite, yaml, dotenv; print('Dependencies OK; discord.py', discord.__version__)"

COPY bot.py /app/bot.py
COPY cogs /app/cogs
COPY services /app/services
COPY config /app/config
RUN mkdir -p /app/data

# Supply DISCORD_TOKEN as a runtime environment variable; never bake .env into the image.
CMD ["python", "/app/bot.py"]
