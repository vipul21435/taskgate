FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
RUN pip install --no-cache-dir pytest==9.1.1 \
 && useradd --create-home --uid 1000 agent \
 && mkdir /workspace && chown agent /workspace
WORKDIR /workspace
COPY --chown=agent workspace/ /workspace/
USER agent
