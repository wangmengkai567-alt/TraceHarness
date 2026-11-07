FROM registry-1.docker.io/library/python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
LABEL org.harness.sandbox.version="1"
RUN mkdir -p /workspace && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent
ENV PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /workspace
USER 10001:10001
ENTRYPOINT []
CMD ["/bin/sh"]
