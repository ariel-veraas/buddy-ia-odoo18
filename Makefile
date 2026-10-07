PYTHON ?= python3
.PHONY: help prepare demo up validate down test
help:
	$(PYTHON) tools/manage.py --help
prepare:
	$(PYTHON) tools/manage.py prepare
demo:
	$(PYTHON) tools/manage.py demo
up:
	$(PYTHON) tools/manage.py up
validate:
	$(PYTHON) tools/manage.py validate
down:
	$(PYTHON) tools/manage.py down
test:
	cd frontend-tests && npm ci --ignore-scripts && npm test
