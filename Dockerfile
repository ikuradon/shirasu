FROM python:3.12.16@sha256:cad37dd20c7cac861af9003af8796d03ea7a998ec1e3a664429740fc9d7d2691 AS runtime
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY requirements/receiver.txt requirements/receiver.txt
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements/receiver.txt

COPY shirasu/ shirasu/
COPY LICENSE ./
COPY licenses/ licenses/
# Retain the small build inputs for digest-based release restoration.
COPY Dockerfile .dockerignore ./
COPY requirements/test.txt requirements/test.txt
CMD ["python", "-m", "shirasu"]

FROM runtime AS test
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements/test.txt
COPY tests/ tests/
COPY tools/ tools/
COPY image_service/examples/ image_service/examples/

# Keep the default deployment image free of test-only dependencies.
FROM runtime AS production
