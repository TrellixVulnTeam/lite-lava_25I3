from celery import shared_task

from django.core.exceptions import ObjectDoesNotExist

from lava_common.compat import yaml_safe_load
from lava_scheduler_app.models import TestJob
from lava_scheduler_app.notifications import (
    create_notification,
    notification_criteria,
    send_notifications,
)


# TODO: pass state, health and old_health
@shared_task(ignore_result=True)
def async_send_notifications(
    job_id: int, state: int, health: int, old_health: int
) -> None:
    print(f"Processing {job_id}: state={state} health={health} old_health={old_health}")
    try:
        job = TestJob.objects.get(id=job_id)
    except TestJob.DoesNotExist:
        print("-> does not exists")
        return

    job_def = yaml_safe_load(job.definition)
    if "notify" in job_def:
        print("-> notify block")
        if notification_criteria(
            job_def["notify"]["criteria"], state, health, old_health
        ):
            # Set state and health as the task can run later while the job
            # state and health already changed.
            # The code is *not* saving the job so this won't have any effect on the db.
            job.state = state
            job.health = health
            try:
                job.notification
            except ObjectDoesNotExist:
                create_notification(job, job_def["notify"])
            print("--> sending notification")
            send_notifications(job)
    print("-> done")