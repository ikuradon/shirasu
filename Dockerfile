FROM python:3.12.16@sha256:cad37dd20c7cac861af9003af8796d03ea7a998ec1e3a664429740fc9d7d2691 AS runtime

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt

COPY . .
CMD ["python", "./main.py"]

FROM runtime AS test
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements-test.txt

# Keep the default deployment image free of test-only dependencies.
FROM runtime AS production
