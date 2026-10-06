# egress 프록시 — 샌드박스·MCP 서버 송신 허용목록 (FR-X-SBX-02 · FR-MCP-SYS-04). 표준 라이브러리만.
FROM python:3.12-slim AS runtime
COPY server.py /srv/server.py
USER 65532
EXPOSE 3128 8080
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["python", "/srv/server.py"]
