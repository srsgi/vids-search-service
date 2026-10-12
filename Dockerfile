FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    jq \
    bash \
    procps \
    tar \
    unzip \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages fastapi uvicorn pydantic

# Install rclone
RUN curl https://rclone.org/install.sh | bash

# Install Quickwit v0.8.2
RUN curl -L https://github.com/quickwit-oss/quickwit/releases/download/v0.8.2/quickwit-v0.8.2-x86_64-unknown-linux-gnu.tar.gz | tar -xz -C /tmp \
    && mv /tmp/quickwit-v0.8.2/quickwit /usr/local/bin/quickwit \
    && rm -rf /tmp/quickwit-v0.8.2

WORKDIR /app

COPY quickwit.yaml /app/quickwit.yaml
COPY index-config.yaml /app/index-config.yaml
COPY db_proxy.py /app/db_proxy.py
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

ENV QW_CONFIG=/app/quickwit.yaml
ENV AWS_ACCESS_KEY_ID=fake
ENV AWS_SECRET_ACCESS_KEY=fake
ENV AWS_REGION=us-east-1

EXPOSE 10000

ENTRYPOINT ["/app/entrypoint.sh"]
