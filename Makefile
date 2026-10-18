.DEFAULT_GOAL := help

.PHONY: help check test rust-check rust-test go-format-check web-check installer-check shellcheck docs-check

help:
	@printf '%s\n' 'make check          Rust/Go/TS quality, shell syntax and document links' 'make test           Rust, Go/Rust integration, TS and installer tests' 'make web-e2e        Build Web/Go/fixture and run browser tests' 'make shellcheck     Installer shell static analysis (requires ShellCheck)' 'make docs-check     Check local Markdown links' 'make release-build  Build Linux installer on a native Linux build machine'

check: rust-check go-format-check go-vet web-check installer-check docs-check

test: rust-test go-integration web-test installer-test

rust-check:
	cargo fmt --all --check
	cargo clippy --workspace --all-targets --locked -- -D warnings
	cargo check -p agent-api --no-default-features --locked
	cargo check -p agent-api --no-default-features --features client --locked
	cargo check -p agent-api --no-default-features --features server --locked
	cargo check -p ingress --no-default-features --locked
	cargo check -p ingress --no-default-features --features caddy --locked

rust-test:
	cargo test --workspace --locked

go-format-check:
	@files="$$(gofmt -l packages/go/agentapi apps/controlplane)"; if [ -n "$$files" ]; then printf '%s\n' "$$files"; exit 1; fi

web-check:
	npm run typecheck
	npm run format:check

docs-check:
	python3 tests/check_docs.py

.PHONY: go-build go-test go-integration go-vet

go-build:
	CGO_ENABLED=0 go build -trimpath -o target/controlplane/controld ./apps/controlplane/cmd/controld
	CGO_ENABLED=0 go build -trimpath -o target/controlplane/agent-client examples/go-client/main.go

go-test:
	go test ./packages/go/agentapi/... ./apps/controlplane/...

go-vet:
	go vet ./packages/go/agentapi/... ./apps/controlplane/...

go-integration:
	cargo build --locked -p node-agent --example api_fixture
	AGENT_API_FIXTURE="$(CURDIR)/target/debug/examples/api_fixture" go test -race ./packages/go/agentapi/... ./apps/controlplane/...

.PHONY: web-build web-test web-e2e

# Run npm ci first on a development/CI machine, not the small deployment node.
web-build:
	npm run web:build

web-test:
	npm test

web-e2e: web-build go-build
	cargo build --locked -p node-agent --example api_fixture
	npm run test:e2e

.PHONY: installer-test release-build

installer-test:
	python3 -m unittest discover -s tests/installer -v
	$(MAKE) installer-check

installer-check:
	bash -n distribution/installer/bootstrap.sh.in distribution/installer/install.sh distribution/installer/cloudctl distribution/installer/lib/*.sh distribution/release/build.sh distribution/tests/ubuntu-smoke.sh

shellcheck:
	shellcheck -x distribution/installer/bootstrap.sh.in distribution/installer/install.sh distribution/installer/cloudctl distribution/installer/lib/*.sh distribution/release/build.sh distribution/tests/ubuntu-smoke.sh

# Run on a native Linux build machine. Target servers receive only the resulting .run.
release-build:
	bash distribution/release/build.sh
