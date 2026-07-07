# hermiq-exec ExApp - Build System

REGISTRY ?= codeberg.org/conduction
IMAGE_NAME ?= hermiq-exec
VERSION ?= 0.1.0

.PHONY: build push run clean help lint format format-fix lint-fix mypy test-unit check check-full check-strict

help:
	@echo "hermiq-exec ExApp"
	@echo ""
	@echo "Usage:"
	@echo "  make build         - Build Docker image"
	@echo "  make push          - Push to registry"
	@echo "  make check-strict  - Run the fleet-standard Python ExApp quality gate (ruff + mypy)"
	@echo ""
	@echo "Variables:"
	@echo "  REGISTRY=$(REGISTRY)"
	@echo "  VERSION=$(VERSION)"

build:
	docker build -t $(REGISTRY)/$(IMAGE_NAME):$(VERSION) -t $(REGISTRY)/$(IMAGE_NAME):latest .

push: build
	docker push $(REGISTRY)/$(IMAGE_NAME):$(VERSION)
	docker push $(REGISTRY)/$(IMAGE_NAME):latest

clean:
	-docker rmi $(REGISTRY)/$(IMAGE_NAME):$(VERSION)
	-docker rmi $(REGISTRY)/$(IMAGE_NAME):latest

# ── Code Quality ───────────────────────────────────────────────────────

lint:
	ruff check ex_app/

format:
	ruff format --check ex_app/

format-fix:
	ruff format ex_app/

lint-fix:
	ruff check --fix ex_app/

mypy:
	mypy ex_app/

test-unit:
	@if [ -d tests ]; then pytest tests/; else echo "No tests/ directory yet, skipping."; fi

check:
	@E=0; \
	for CMD in lint mypy; do \
		echo; echo "=== $$CMD ==="; \
		$(MAKE) $$CMD || E=1; \
	done; \
	echo; \
	if [ $$E -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED (see above)"; fi; \
	exit $$E

check-full:
	@E=0; \
	for CMD in lint format mypy test-unit; do \
		echo; echo "=== $$CMD ==="; \
		$(MAKE) $$CMD || E=1; \
	done; \
	echo; \
	if [ $$E -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED (see above)"; fi; \
	exit $$E

check-strict:
	@E=0; \
	for CMD in lint format mypy test-unit; do \
		echo; echo "=== $$CMD ==="; \
		$(MAKE) $$CMD || E=1; \
	done; \
	echo; \
	if [ $$E -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED (see above)"; fi; \
	exit $$E
