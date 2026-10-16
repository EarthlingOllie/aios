from aios.worker import celery_app
celery_app.conf.beat_schedule["retry-unfinished-data-stores"]["schedule"] = 5
