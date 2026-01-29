FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl wget git \
    ripgrep jq sed gawk coreutils findutils \
    iproute2 iptables procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
