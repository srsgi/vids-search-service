FROM quickwit/quickwit:latest AS quickwit-src
FROM rclone/rclone:latest AS rclone-src

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    jq \
    bash \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Copy quickwit and rclone binaries
COPY --from=quickwit-src /quickwit/quickwit /usr/local/bin/quickwit
COPY --from=rclone-src /usr/local/bin/rclone /usr/local/bin/rclone

WORKDIR /app

COPY quickwit.yaml /app/quickwit.yaml
COPY index-config.yaml /app/index-config.yaml
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

ENV QW_CONFIG=/app/quickwit.yaml
ENV AWS_ACCESS_KEY_ID=fake
ENV AWS_SECRET_ACCESS_KEY=fake
ENV AWS_REGION=us-east-1

EXPOSE 10000

ENTRYPOINT ["/app/entrypoint.sh"]
