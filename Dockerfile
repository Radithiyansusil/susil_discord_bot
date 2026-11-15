FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py .

# Run as a normal user instead of root
RUN useradd --create-home botuser
USER botuser

CMD ["python", "bot.py"]
