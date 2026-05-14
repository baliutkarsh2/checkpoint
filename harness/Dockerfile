FROM python:3.11-slim
WORKDIR /harness
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY harness.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
# At runtime, entrypoint.sh merges system CAs + sidecar CA into /tmp/combined-ca.crt
# so OpenAI calls pass real TLS and sidecar-intercepted calls pass the minted CA.
ENV REQUESTS_CA_BUNDLE=/archal-out/ca.crt \
    SSL_CERT_FILE=/archal-out/ca.crt \
    NODE_EXTRA_CA_CERTS=/archal-out/ca.crt \
    PYTHONUNBUFFERED=1
CMD ["./entrypoint.sh"]
