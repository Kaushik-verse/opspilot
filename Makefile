.PHONY: install test lint oracle bench bench-kind kind-up kind-down demo

install:
	uv sync

test:
	uv run pytest

lint:
	uvx ruff check src tests

oracle:            ## sanity-check the environment & scorer (no API key)
	uv run opspilot bench --configs oracle --run-id oracle

bench:             ## the headline experiment on the simulator
	uv run opspilot bench --configs small,cascade,large --run-id main --pause 2

bench-kind:        ## the cascade agent against a real kind cluster
	uv run opspilot bench --backend kind --configs cascade --run-id kind-cascade --pause 2

kind-up:
	kind create cluster --name opspilot --wait 180s

kind-down:
	kind delete cluster --name opspilot

demo:              ## watch the agent fix a broken Service live
	uv run opspilot run --scenario target_port --task mitigation --config cascade
