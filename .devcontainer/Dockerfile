FROM debian:trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash git python3 shellcheck php8.4-cli php8.4-xml \
        rsync coreutils findutils diffutils util-linux tar gzip bzip2 unzip \
    && rm -rf /var/lib/apt/lists/*

ENV HOME=/tmp
WORKDIR /workspace
CMD ["bash", ".github/scripts/check.sh"]
