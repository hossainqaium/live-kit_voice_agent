# Multi-Tenant AI Voice Agent Platform — developer tasks.
#
# Every target runs through Docker so behaviour matches CI and no local Python
# or Node version has to be maintained.

COMPOSE      := docker compose -f deploy/docker-compose.yml --env-file .env
API          := $(COMPOSE) exec -T configuration-api
WORKER       := $(COMPOSE) exec -T ai-agent-worker
API_RUN      := $(COMPOSE) run --rm --no-deps -T configuration-api
WORKER_RUN   := $(COMPOSE) run --rm --no-deps -T ai-agent-worker

# Host ports come from .env so messages match the running stack.
API_PORT      ?= $(shell grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2)
FRONTEND_PORT ?= $(shell grep -E '^FRONTEND_PORT=' .env 2>/dev/null | cut -d= -f2)
WORKER_PORT   ?= $(shell grep -E '^WORKER_HEALTH_PORT=' .env 2>/dev/null | cut -d= -f2)
GRAFANA_PORT  ?= $(shell grep -E '^GRAFANA_PORT=' .env 2>/dev/null | cut -d= -f2)

.DEFAULT_GOAL := help
.PHONY: help env preflight build up down restart logs ps urls health \
        migrate migration downgrade psql redis  \
        test-room browser-test refresh-ip detect-drift \
        typecheck-web build-web check-web \
        test test-unit test-api test-worker test-shared test-integration test-e2e \
        lint fmt fmt-check typecheck check load-test measure-latency clean nuke

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #

env: ## Create .env from the template with generated secrets
	@if [ -f .env ]; then \
		echo ".env already exists — leaving it alone"; \
	else \
		cp .env.example .env; \
		jwt=$$(openssl rand -hex 32); \
		fernet=$$(python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"); \
		sed -i'' -e "s|^JWT_SECRET=.*|JWT_SECRET=$$jwt|" .env; \
		sed -i'' -e "s|^CREDENTIAL_ENCRYPTION_KEY=.*|CREDENTIAL_ENCRYPTION_KEY=$$fernet|" .env; \
		echo ".env created with generated development secrets"; \
	fi

preflight: ## Check that every published container port is free
	@python3 scripts/preflight.py

# --------------------------------------------------------------------------- #
# Stack
# --------------------------------------------------------------------------- #

build: ## Build the application images
	$(COMPOSE) build

up: env preflight ## Start the full stack (checks ports first)
	$(COMPOSE) up -d
	@$(MAKE) --no-print-directory urls

down: ## Stop the stack, keeping volumes
	$(COMPOSE) down

restart: ## Restart every service
	$(COMPOSE) restart

logs: ## Follow logs (make logs S=configuration-api for one service)
	$(COMPOSE) logs -f $(S)

ps: ## Show service status
	@$(COMPOSE) ps

urls: ## Print the local endpoints
	@echo ""
	@echo "  Frontend            http://localhost:$(FRONTEND_PORT)"
	@echo "  Configuration API   http://localhost:$(API_PORT)"
	@echo "  OpenAPI docs        http://localhost:$(API_PORT)/docs"
	@echo "  Worker health       http://localhost:$(WORKER_PORT)/health"
	@echo "  Grafana             http://localhost:$(GRAFANA_PORT)"
	@echo ""

health: ## Probe every service's health and readiness
	@printf '  %-22s ' "configuration-api"; curl -fsS --max-time 5 http://localhost:$(API_PORT)/health >/dev/null && echo "healthy" || echo "DOWN"
	@printf '  %-22s ' "configuration-api ready"; curl -fsS --max-time 5 http://localhost:$(API_PORT)/ready >/dev/null && echo "ready" || echo "NOT READY"
	@printf '  %-22s ' "ai-agent-worker"; curl -fsS --max-time 5 http://localhost:$(WORKER_PORT)/health >/dev/null && echo "healthy" || echo "DOWN"
	@printf '  %-22s ' "ai-agent-worker ready"; curl -fsS --max-time 5 http://localhost:$(WORKER_PORT)/ready >/dev/null && echo "ready" || echo "NOT READY"

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

migrate: ## Apply all pending migrations
	$(API) alembic upgrade head

migration: ## Autogenerate a revision (make migration M="add routing_rules")
	@test -n "$(M)" || (echo 'usage: make migration M="description"' && exit 1)
	$(API) alembic revision --autogenerate -m "$(M)"

downgrade: ## Roll back one revision
	$(API) alembic downgrade -1

psql: ## Open a psql shell
	$(COMPOSE) exec postgresql psql -U voice_agent -d voice_agent

redis: ## Open a redis-cli shell
	$(COMPOSE) exec redis redis-cli

# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

test: test-unit ## Run every unit test suite

test-unit: test-shared test-api test-worker ## Unit tests across all Python packages

test-shared: ## Unit tests for the shared package
	@echo "── shared ──"
	$(API_RUN) sh -c 'pip install -q pytest pytest-asyncio && cd /opt/shared && python -m pytest'

test-api: ## Unit tests for the Configuration API
	@echo "── configuration-api ──"
	$(API_RUN) sh -c 'pip install -q -r requirements-dev.txt && python -m pytest'

test-worker: ## Unit tests for the AI agent worker
	@echo "── ai-agent-worker ──"
	$(WORKER_RUN) sh -c 'pip install -q -r requirements-dev.txt && python -m pytest'

test-integration: ## Integration tests: PBX -> SIP -> LiveKit -> agent -> STT/LLM/TTS
	@echo "Phase 1 delivers these (see docs/LiveKitVoiceAgentPlan.md)"

test-e2e: ## End-to-end tests over real SIP calls
	@echo "Phase 1 delivers these (see docs/LiveKitVoiceAgentPlan.md)"

# --------------------------------------------------------------------------- #
# Quality
# --------------------------------------------------------------------------- #

# Lint and format run from the repository root in a throwaway container so
# ruff discovers ./pyproject.toml, the same way CI invokes it. Running them
# inside a service container silently falls back to ruff's defaults, because
# the service images do not contain the repository-wide config.
PY_SOURCES := services/configuration-api/app services/configuration-api/tests \
              services/ai-agent-worker/worker services/ai-agent-worker/tests \
              services/shared/shared services/shared/tests scripts
TOOL_RUN   := docker run --rm -v "$(PWD)":/repo -w /repo python:3.11-slim sh -c

lint: ## Lint the Python sources
	$(TOOL_RUN) 'pip install -q ruff==0.8.6 && python -m ruff check $(PY_SOURCES)'

fmt: ## Format the Python sources
	$(TOOL_RUN) 'pip install -q ruff==0.8.6 && python -m ruff format $(PY_SOURCES)'

fmt-check: ## Verify formatting without rewriting files
	$(TOOL_RUN) 'pip install -q ruff==0.8.6 && python -m ruff format --check $(PY_SOURCES)'

typecheck: ## Type-check the Python sources
	$(API_RUN) sh -c 'pip install -q -r requirements-dev.txt && python -m mypy --config-file /opt/tooling/pyproject.toml app'

# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #

FRONTEND_RUN := $(COMPOSE) run --rm --no-deps -T
WEB_RUN      := $(FRONTEND_RUN) frontend

typecheck-web: ## Type-check the console
	$(WEB_RUN) npm run typecheck

# Two things are overridden so a build can run while the dev server is up.
# NODE_ENV, because the compose file sets it to development for the dev server
# and `next build` warns about a non-standard value and then misbehaves; and
# NEXT_DIST_DIR, because `.next` is on the bind mount the dev server is
# actively writing to. Sharing either one produces a failure that names the
# wrong cause (see the comment in next.config.ts).
build-web: ## Production build of the console
	$(FRONTEND_RUN) -e NODE_ENV=production -e NEXT_DIST_DIR=.next-build \
		frontend npm run build

check-web: typecheck-web build-web ## Console checks

check: lint typecheck test ## Everything CI runs

# --------------------------------------------------------------------------- #
# Latency measurement (Plan 2b.8) and load testing (Phase 8)
# --------------------------------------------------------------------------- #

REPEATS       ?= 15
CONCURRENCY   ?= 1,2,10
AUDIO_SECONDS ?= 3.6
STT_URL       ?=
STT_MODEL     ?= Systran/faster-whisper-tiny
LLM_URL       ?=
LLM_MODEL     ?= gpt-4o-mini
TTS_URL       ?=
TTS_MODEL     ?= tts-1
API_KEY       ?=
MEASURE_JSON  ?=

measure-latency: ## Repeat STT/LLM/TTS probes; report p50/p95 and realtime factor
	@test -n "$(STT_URL)$(LLM_URL)$(TTS_URL)" || (echo "usage: make measure-latency STT_URL=http://127.0.0.1:8000/v1" && exit 2)
	python3 scripts/measure_latency.py \
		--repeats $(REPEATS) \
		--concurrency $(CONCURRENCY) \
		--audio-seconds $(AUDIO_SECONDS) \
		$(if $(STT_URL),--stt-url $(STT_URL) --stt-model $(STT_MODEL)) \
		$(if $(LLM_URL),--llm-url $(LLM_URL) --llm-model $(LLM_MODEL)) \
		$(if $(TTS_URL),--tts-url $(TTS_URL) --tts-model $(TTS_MODEL)) \
		$(if $(API_KEY),--api-key $(API_KEY)) \
		$(if $(MEASURE_JSON),--json $(MEASURE_JSON))

load-test: ## Progressive SIP load test (Phase 8 — not yet built)
	@echo "Phase 8 delivers this. Progression: 10, 25, 50, 100, 250, 500, 750, 1000."
	@echo "For repeated stage measurement (Plan 2b.8) use: make measure-latency STT_URL=..."
	@echo "No capacity claim is valid until the Phase 8 numbers are measured."

# --------------------------------------------------------------------------- #
# Cleanup
# --------------------------------------------------------------------------- #

clean: ## Remove containers, keeping volumes
	$(COMPOSE) down --remove-orphans

nuke: ## Remove containers AND volumes — destroys local data
	@printf 'This deletes the local database, recordings and dashboards. Type yes: ' && read ans && [ "$$ans" = yes ]
	$(COMPOSE) down -v --remove-orphans

# --------------------------------------------------------------------------- #
# Browser test client (Plan 2b.10) — development only
#
# Exercises the real pipeline with no PBX in the path, which is what makes
# endpointing measurable in seconds instead of one phone call at a time. The
# worker gate is off by default and refused outside development.
# --------------------------------------------------------------------------- #
detect-drift: ## Compare PostgreSQL against LiveKit without repairing (spec 46)
	$(API) python -m app.cli detect-drift

test-room: ## Create a room + agent dispatch for a DID, for a client of your own
	@test -n "$(DID)" || (echo "usage: make test-room DID=1001" && exit 2)
	$(API) python -m app.cli create-test-room --did "$(DID)" --room "$(or $(ROOM),browser-test)"

browser-test: ## Turn the worker gate on, restart it, and print what to do next
	@grep -q '^ALLOW_BROWSER_TEST_PARTICIPANT=true' .env \
		|| (echo "set ALLOW_BROWSER_TEST_PARTICIPANT=true in .env first" && exit 2)
	$(COMPOSE) up -d --force-recreate ai-agent-worker
	@echo ""
	@echo "worker restarted with the browser test path on."
	@echo "  make test-room DID=<number>   then connect the playground to that room"
	@echo "remember to set it back to false when finished."

refresh-ip: ## Point SIP_NAT_IP at this machine's current LAN address
	@ip=$$(./scripts/lan-ip.sh) && test -n "$$ip" \
		&& sed -i.bak "s/^SIP_NAT_IP=.*/SIP_NAT_IP=$$ip/" .env && rm -f .env.bak \
		&& echo "SIP_NAT_IP=$$ip" \
		&& echo "restart livekit and livekit-sip for it to take effect:" \
		&& echo "  $(COMPOSE) up -d --force-recreate livekit livekit-sip"
