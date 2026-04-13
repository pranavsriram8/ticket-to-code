# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TicketToCode — Makefile
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REGISTRY       := ghcr.io
REPO           := pranavsriram8/ticket-to-code
COORDINATOR    := $(REGISTRY)/$(REPO)/coordinator
EXECUTOR       := $(REGISTRY)/$(REPO)/executor
IMAGE_TAG      := latest
GIT_SHA        := $(shell git rev-parse --short HEAD)

.PHONY: help build-coordinator build-executor build-all push-coordinator push-executor push-all \
        run-local release clean lock

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Docker: Coordinator (Agent 1 — webhook server) ──────────────────

build-coordinator: ## Build the coordinator image locally
	docker build \
		-t $(COORDINATOR):$(IMAGE_TAG) \
		-t $(COORDINATOR):sha-$(GIT_SHA) \
		-f Dockerfile .

push-coordinator: ## Push coordinator image to GHCR
	docker push $(COORDINATOR):$(IMAGE_TAG)
	docker push $(COORDINATOR):sha-$(GIT_SHA)

# ── Docker: Executor (Agent 2 — GitHub Action) ──────────────────────

build-executor: ## Build the executor image locally
	docker build \
		-t $(EXECUTOR):$(IMAGE_TAG) \
		-t $(EXECUTOR):sha-$(GIT_SHA) \
		-f Dockerfile.action .

push-executor: ## Push executor image to GHCR
	docker push $(EXECUTOR):$(IMAGE_TAG)
	docker push $(EXECUTOR):sha-$(GIT_SHA)

# ── Build All ────────────────────────────────────────────────────────

build-all: build-coordinator build-executor ## Build both images

push-all: push-coordinator push-executor ## Push both images to GHCR

# ── Local Development ────────────────────────────────────────────────

run-local: ## Run the coordinator locally with docker-compose
	docker-compose up --build

# ── Release (tag → CI builds & pushes both images) ───────────────────

release: ## Create a release tag (usage: make release V=0.2.0)
ifndef V
	$(error Usage: make release V=0.2.0)
endif
	git tag v$(V)
	git push origin v$(V)
	@echo "✅ Tagged v$(V) — CI will build and push both images"

# ── Dependencies ─────────────────────────────────────────────────────

lock: ## Regenerate both lockfiles with pinned versions + hashes (inside Linux container for correct platform deps)
	docker run --rm -v "$(CURDIR)":/work -w /work python:3.12-slim \
		sh -c "pip install -q pip-tools && \
			pip-compile --generate-hashes --output-file=requirements.lock requirements.txt && \
			pip-compile --generate-hashes --output-file=requirements.action.lock requirements.action.txt"

# ── Cleanup ──────────────────────────────────────────────────────────

clean: ## Remove local Docker images
	-docker rmi $(COORDINATOR):$(IMAGE_TAG) $(COORDINATOR):sha-$(GIT_SHA) 2>/dev/null
	-docker rmi $(EXECUTOR):$(IMAGE_TAG) $(EXECUTOR):sha-$(GIT_SHA) 2>/dev/null
