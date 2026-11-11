# Development tasks. `make seed` rebuilds development data from scratch: it migrates the platform
# database to head, wipes every organisation (with its store) and user, and makes one working
# organisation (see backend/src/aios/seed.py). Destructive: development databases only.

.PHONY: seed

seed:
	cd backend && uv run python -m aios.seed
