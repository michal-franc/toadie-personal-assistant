.PHONY: check lint test test-python test-watch test-phone lint-python lint-watch lint-phone daemon-install

# Run all lints and tests (mirrors CI)
check: lint test

# All lints
lint: lint-python lint-watch lint-phone

# All tests
test: test-python test-watch test-phone

# Python
lint-python:
	ruff check *.py
	ruff format --check *.py

test-python:
	DEEPGRAM_API_KEY=dummy pytest

# Watch app
lint-watch:
	cd watch-app && ./gradlew lint

test-watch:
	cd watch-app && ./gradlew test

# Phone app
lint-phone:
	cd phone-app && ./gradlew lint

test-phone:
	cd phone-app && ./gradlew test

daemon-install:
	sudo systemctl daemon-reload
	sudo systemctl restart claude-watch
