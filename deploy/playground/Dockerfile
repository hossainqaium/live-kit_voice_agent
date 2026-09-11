# LiveKit Agents Playground — a browser test client (Plan 2b.10).
#
# The upstream repository ships no Dockerfile, so this one exists here rather
# than being vendored: the playground is a third-party development tool and
# copying its source into this repo would make us responsible for maintaining
# it.
#
# Development only. It is behind the "testing" compose profile, so a plain
# `make up` does not start it and a production compose run cannot.
FROM node:22-slim

# git to fetch the app; the rest is what its native dependencies need.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable

WORKDIR /app

# Pinned to a tag rather than main. An unpinned clone means the test client
# can change under a measurement, which is the one thing a measuring
# instrument must not do.
ARG PLAYGROUND_REF=main
RUN git clone --depth 1 --branch "${PLAYGROUND_REF}" \
    https://github.com/livekit/agents-playground.git . \
    && pnpm install --frozen-lockfile

EXPOSE 3000
CMD ["pnpm", "dev", "--port", "3000", "--hostname", "0.0.0.0"]
