# atlas-gateway — go-gin (multi-stage: dev = local 모드, runtime = prod 모드)
FROM golang:1.26 AS dev
WORKDIR /src
ENV GOFLAGS=-mod=mod
COPY go.mod go.sum* ./
RUN go mod download || true
COPY . .
RUN go mod tidy
EXPOSE 8080
CMD ["sh", "-c", "go mod tidy && go run ./cmd/server"]

# 멀티아치: 빌드 호스트 아키텍처에서 크로스 컴파일 (에뮬레이션 없음)
FROM --platform=$BUILDPLATFORM golang:1.26 AS build
ARG VERSION=0.0.0-dev TARGETOS TARGETARCH
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o /out/server ./cmd/server

# make db-* 타겟이 컨테이너 셸을 쓰므로 distroless 대신 alpine (정적 바이너리)
FROM alpine:3.20 AS runtime
ARG VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="atlas-gateway" org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/prodai/atlas" org.opencontainers.image.licenses="Apache-2.0" \
      org.prodai.agent.unit-id="GW"
RUN adduser -D -u 65532 app && mkdir -p /app/data && chown app /app/data
COPY --from=build /out/server /server
USER 65532
HEALTHCHECK --interval=10s --timeout=3s --retries=6 CMD ["/server", "-healthcheck"]
EXPOSE 8080
ENTRYPOINT ["/server"]
