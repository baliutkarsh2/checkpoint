FROM python:3.11-slim
WORKDIR /harness
COPY requirements.txt /harness/
RUN pip install --no-cache-dir -r requirements.txt
COPY harness.py harness_fake.py /harness/
CMD ["python", "harness.py"]
