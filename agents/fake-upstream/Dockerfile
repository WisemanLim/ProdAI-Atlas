# 가짜 업스트림 (시험·개발 전용) — GitHub·Slack·Jira REST·문서 HTTP 흉내. 표준 라이브러리만.
FROM python:3.12-slim AS runtime
COPY server.py /srv/server.py
USER 65532
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "/srv/server.py"]
