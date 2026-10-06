# atlas-sandbox — 코드 실행 격리 (FR-MA-ANL-01·05 · X-SBX-01·02). Python(pandas)·Node·sh, 비루트.
FROM python:3.12-slim AS runtime
ARG VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="atlas-sandbox" org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/prodai/atlas" org.opencontainers.image.licenses="Apache-2.0" \
      org.prodai.agent.unit-id="MA-ANL-SBX"
RUN pip install --no-cache-dir "pandas==2.2.*" && useradd -u 61000 -M -s /usr/sbin/nologin sbx \
 && apt-get update && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/*   # X-SBX-01 JavaScript
COPY server.py /srv/server.py
# 전용 UID: RLIMIT_NPROC 은 UID 전체 합산이라 다른 비루트(65532) 컨테이너와 공유하면 스레드 생성이 막힌다
USER 61000
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"]
CMD ["python", "/srv/server.py"]
