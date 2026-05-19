FROM python:3.11-slim AS builder
WORKDIR /app
COPY applications/worker_autoscaler/requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY applications/ /app/applications/
ENV PATH=/root/.local/bin:$PATH
CMD ["python", "-m", "applications.worker_autoscaler.main"]
