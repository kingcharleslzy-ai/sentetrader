# Build static assets natively; the final nginx image keeps the requested target platform.
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps/client/package.json apps/client/package.json
COPY apps/desktop/package.json apps/desktop/package.json
COPY packages/ui-events/package.json packages/ui-events/package.json
RUN npm ci
COPY apps/client apps/client
COPY packages packages
# The interface surface table is the Runtime's file; the client imports it (uiSurfaces.ts).
COPY services/trader-runtime/src/trader_runtime/ui_surfaces.json services/trader-runtime/src/trader_runtime/ui_surfaces.json
ARG VITE_CLOUD_RUNTIME_URL
ARG VITE_ALLOW_INSECURE_LOCAL_CLOUD=0
ARG VITE_DEFAULT_RUNTIME_MODE=cloud
ARG VITE_CLOUD_AUTH_MODE=owner-password
ARG VITE_APP_BASE_PATH=/
ARG AI_TRADER_BUILD_SHA=development
ARG AI_TRADER_BUILD_VERSION=1.0.1
ENV VITE_CLOUD_RUNTIME_URL=${VITE_CLOUD_RUNTIME_URL}
ENV VITE_ALLOW_INSECURE_LOCAL_CLOUD=${VITE_ALLOW_INSECURE_LOCAL_CLOUD}
ENV VITE_DEFAULT_RUNTIME_MODE=${VITE_DEFAULT_RUNTIME_MODE}
ENV VITE_CLOUD_AUTH_MODE=${VITE_CLOUD_AUTH_MODE}
ENV VITE_APP_BASE_PATH=${VITE_APP_BASE_PATH}
ENV AI_TRADER_BUILD_SHA=${AI_TRADER_BUILD_SHA}
ENV AI_TRADER_BUILD_VERSION=${AI_TRADER_BUILD_VERSION}
RUN node -e 'require("node:fs").writeFileSync("apps/client/public/version.json", JSON.stringify({version:process.env.AI_TRADER_BUILD_VERSION,commit:process.env.AI_TRADER_BUILD_SHA,liveTrading:"disabled"})+"\n")'
RUN npm --workspace @ai-trader/client run build

FROM nginx:1.29-alpine
COPY apps/client/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/apps/client/dist /usr/share/nginx/html
EXPOSE 8080
