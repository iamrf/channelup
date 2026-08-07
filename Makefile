PYTHON ?= .venv/bin/python

.PHONY: test
test:  ## run the test suite
	$(PYTHON) -m pytest -q