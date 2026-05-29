GATEWAY_URL ?= wss://api.localhost/agent/ws
TOKEN       ?= changeme
DATA_ROOT   ?= /tmp/agent-data

.PHONY: run build test lint

run:
	GATEWAY_URL=$(GATEWAY_URL) TOKEN=$(TOKEN) DATA_ROOT_PATH=$(DATA_ROOT) python -m agent

build:
	docker build -t saas-platform-agent:dev .

test:
	python -m pytest tests/ -v

lint:
	ruff check agent/
