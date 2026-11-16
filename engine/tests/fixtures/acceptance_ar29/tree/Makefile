.PHONY: test run-staging run-prod vet build docs

build:
	go build ./...

docs:
	go run ./cmd/gwlintdocs README.md

vet:
	go vet ./...

test: vet
	go test ./...
	go test -race ./internal/config/...

run-staging:
	docker compose up gateway-staging mock-upstream-staging

run-prod:
	docker compose up gateway-production mock-upstream-production
