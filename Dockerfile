FROM python:3.12-slim

ARG RCLONE_RELEASE=1.75.1
ARG TARGETARCH=amd64

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

WORKDIR /app

COPY . .

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        7zip \
        ca-certificates \
        curl \
        pigz \
        poppler-utils \
        qpdf \
        unzip \
        xz-utils \
        zpaq \
    && case "$TARGETARCH" in \
        amd64) \
            rclone_arch="amd64"; \
            rclone_sha256="982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab" \
            ;; \
        arm64) \
            rclone_arch="arm64"; \
            rclone_sha256="03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9" \
            ;; \
        *) \
            echo "Arquitetura sem binario rclone configurado: $TARGETARCH" >&2; \
            exit 1 \
            ;; \
       esac \
    && rclone_archive="rclone-v${RCLONE_RELEASE}-linux-${rclone_arch}.zip" \
    && curl -fsSLo "/tmp/${rclone_archive}" \
        "https://downloads.rclone.org/v${RCLONE_RELEASE}/${rclone_archive}" \
    && echo "${rclone_sha256}  /tmp/${rclone_archive}" | sha256sum -c - \
    && unzip -q "/tmp/${rclone_archive}" -d /tmp/rclone-install \
    && install -m 0755 \
        "/tmp/rclone-install/rclone-v${RCLONE_RELEASE}-linux-${rclone_arch}/rclone" \
        /usr/local/bin/rclone \
    && rclone version \
    && rm -rf /tmp/rclone-install "/tmp/${rclone_archive}" \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

ENTRYPOINT ["python", "-m", "cotacoes_ceasa.main"]
