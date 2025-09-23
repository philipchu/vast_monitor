FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY vastwatch ./vastwatch
ENV VW_DB=/data/vastwatch.db
VOLUME ["/data"]
CMD ["python", "-m", "vastwatch.collector"]

