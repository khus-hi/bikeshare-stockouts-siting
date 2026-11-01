# Image for the Kubeflow version of the collector. Build it inside minikube so no
# registry is needed (free, nothing leaves the laptop):
#   minikube image build -t bikeshare-gbfs:0.1 -f ingest/kubeflow/Dockerfile .
FROM python:3.11-slim

WORKDIR /app
# Only what the collector and health check need, to keep the image small.
RUN pip install --no-cache-dir pandas duckdb pyarrow requests python-dotenv pytz "psycopg[binary]"
COPY ingest/ ingest/

ENV GBFS_DATA_DIR=/data/gbfs
ENTRYPOINT ["python"]
