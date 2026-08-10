import logging
import os
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from api.models import User
from api.performance import user_affinity_version
from api.recommendation_runtime import (
    enqueue_personal_recommendation_refresh,
    mark_personal_recommendation_refresh,
    pop_personal_recommendation_refresh,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process deduplicated personal recommendation refresh jobs from Redis."

    def handle(self, *args, **options):
        try:
            os.nice(5)
        except (AttributeError, OSError):
            pass
        self.stdout.write("recommendation worker ready")
        while True:
            user_id = pop_personal_recommendation_refresh(timeout=5)
            if not user_id:
                close_old_connections()
                # BRPOP normally blocks, but Redis outages short-circuit to a
                # miss. Back off explicitly so a cache outage cannot turn this
                # low-priority worker into a CPU spin loop.
                time.sleep(1.0)
                continue
            try:
                close_old_connections()
                user = User.objects.filter(pk=user_id, is_active=True).first()
                if not user:
                    continue
                # Capture the version this run is responsible for. If another
                # interaction lands while generation is running, its signal bumps
                # the version and queues another pass; we never mark that newer
                # version complete by accident.
                target_version = user_affinity_version(user.pk)
                from api.views import _generate_personal_recommendations
                _generate_personal_recommendations(user, target=18)
                mark_personal_recommendation_refresh(user.pk, target_version)
                if user_affinity_version(user.pk) > target_version:
                    enqueue_personal_recommendation_refresh(user.pk)
            except Exception:
                logger.exception("Personal recommendation refresh failed for user=%s", user_id)
                # The queue dedupe key is released when a job is claimed. Give
                # transient database/Redis failures a bounded retry instead of
                # forcing Home to keep doing synchronous safety regeneration.
                time.sleep(2.0)
                enqueue_personal_recommendation_refresh(user_id, ttl=30)
            finally:
                close_old_connections()
