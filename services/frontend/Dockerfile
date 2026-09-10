# syntax=docker/dockerfile:1

# --------------------------------------------------------------------------- #
# Development: dependencies baked in, source bind-mounted for hot reload.
# --------------------------------------------------------------------------- #
FROM node:22-alpine AS development

WORKDIR /app

RUN apk add --no-cache curl libc6-compat

COPY services/frontend/package.json services/frontend/package-lock.json* ./
# `npm ci` needs a lockfile; on a first run there is none, so fall back to
# `npm install` and let it generate one.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY services/frontend/ ./

EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=3s --start-period=40s --retries=6 \
    CMD curl -fsS http://localhost:3000 || exit 1

CMD ["npm", "run", "dev"]

# --------------------------------------------------------------------------- #
# Production: compiled standalone build, non-root.
# --------------------------------------------------------------------------- #
FROM node:22-alpine AS builder

WORKDIR /app
RUN apk add --no-cache libc6-compat
COPY services/frontend/package.json services/frontend/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY services/frontend/ ./
RUN npm run build

FROM node:22-alpine AS production

WORKDIR /app
ENV NODE_ENV=production
RUN apk add --no-cache curl libc6-compat

COPY --from=builder /app/public ./public
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json

RUN addgroup -g 10001 -S nodegrp && adduser -u 10001 -S nodeusr -G nodegrp \
    && chown -R nodeusr:nodegrp /app
USER nodeusr

EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:3000 || exit 1

CMD ["npm", "run", "start"]
