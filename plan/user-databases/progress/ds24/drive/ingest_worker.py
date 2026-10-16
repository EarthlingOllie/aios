"""The real ingest worker (default queue, two processes as on AWS), with Docling and Voyage
faked (`fakes.py`)."""

import sys

import fakes

fakes.install()

from aios.worker import celery_app  # noqa: E402

celery_app.worker_main(["worker", "--loglevel=INFO", "--concurrency=2", *sys.argv[1:]])
