FROM python:3.11-slim
WORKDIR /harness
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY harness.py entrypoint.sh ./
RUN chmod +x entrypoint.sh
ENV PYTHONUNBUFFERED=1
CMD ["./entrypoint.sh"]
