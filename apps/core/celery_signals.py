import logging

from celery.signals import task_failure, task_retry, task_success

logger = logging.getLogger("perf.celery")


@task_failure.connect
def on_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, traceback=None, einfo=None, **extra):
    logger.error(
        "celery_task_failure task=%s task_id=%s exception=%s args=%s kwargs=%s",
        getattr(sender, "name", "unknown"),
        task_id,
        str(exception),
        args,
        kwargs,
    )


@task_retry.connect
def on_task_retry(sender=None, request=None, reason=None, einfo=None, **extra):
    logger.warning(
        "celery_task_retry task=%s task_id=%s reason=%s retries=%s",
        getattr(sender, "name", "unknown"),
        getattr(request, "id", "unknown"),
        str(reason),
        getattr(request, "retries", "unknown"),
    )


@task_success.connect
def on_task_success(sender=None, result=None, **extra):
    logger.info("celery_task_success task=%s", getattr(sender, "name", "unknown"))

