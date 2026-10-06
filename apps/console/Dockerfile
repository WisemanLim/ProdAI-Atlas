# prodai-atlas console — React + Vite (multi-stage: dev = local 모드, runtime = prod 모드)
FROM node:22-alpine AS dev
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml* ./
RUN pnpm install
COPY . .
EXPOSE 5173
CMD ["sh", "-c", "pnpm install && pnpm dev"]

# 정적 산출물은 아키텍처 무관 → 빌드 호스트에서 한 번만 (멀티아치 에뮬레이션 회피)
FROM --platform=$BUILDPLATFORM node:22-alpine AS build
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm build

# 비루트 nginx (UID 101) — 읽기 전용 rootfs 에서 /tmp 만 쓴다
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
ARG VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="atlas-console" org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/prodai/atlas" org.opencontainers.image.licenses="Apache-2.0"
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 5173
