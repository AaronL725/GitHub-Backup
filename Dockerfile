FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 LANG=C.UTF-8 HOME=/var/lib/ghbackup TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates tzdata fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY deploy/tools.lock.json /app/deploy/tools.lock.json
COPY scripts/install-tools.py /app/scripts/install-tools.py
RUN python scripts/install-tools.py --bin-dir /usr/local/bin
COPY requirements.lock requirements-telegram.lock requirements-build.lock /app/
RUN pip install --no-cache-dir --require-hashes -r requirements.lock -r requirements-telegram.lock -r requirements-build.lock
ARG GHBACKUP_UID=10001
ARG GHBACKUP_GID=10001
RUN groupadd -g ${GHBACKUP_GID} ghbackup && useradd -u ${GHBACKUP_UID} -g ghbackup -d /var/lib/ghbackup ghbackup
COPY pyproject.toml /app/
COPY ghbackup /app/ghbackup
RUN pip install --no-deps --no-build-isolation --no-cache-dir .
USER ghbackup
ENTRYPOINT ["python", "-m", "ghbackup"]
CMD ["scheduler"]
