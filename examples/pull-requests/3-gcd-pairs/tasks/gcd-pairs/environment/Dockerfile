# Planted flaw for the demo (gate TG302): the base image is named by tag only,
# so the environment can change under the task without a commit.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==9.1.1 \
 && useradd --create-home --uid 1000 agent \
 && mkdir /workspace && chown agent /workspace
WORKDIR /workspace
COPY --chown=agent workspace/ /workspace/
USER agent
