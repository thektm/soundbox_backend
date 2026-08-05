from decimal import Decimal

from django.core.management.base import BaseCommand

from api.models import Artist, DepositRequest
from api.views import (
    FINANCE_QUANTUM,
    _finance_allocate_across_songs,
    _finance_decimal,
    _finance_saved_song_allocations,
    _finance_song_totals,
    _finance_string,
)


class Command(BaseCommand):
    help = (
        "Persist deterministic per-song allocations for historical payout requests "
        "that predate song-level finance tracking."
    )

    def handle(self, *args, **options):
        updated = 0
        artists = Artist.objects.filter(
            deposit_requests__status__in=[
                DepositRequest.STATUS_PENDING,
                DepositRequest.STATUS_APPROVED,
                DepositRequest.STATUS_DONE,
            ]
        ).distinct()

        for artist in artists.iterator():
            song_totals = _finance_song_totals(artist)
            reserved = {song_id: Decimal("0") for song_id in song_totals}
            requests = DepositRequest.objects.filter(
                artist=artist,
                status__in=[
                    DepositRequest.STATUS_PENDING,
                    DepositRequest.STATUS_APPROVED,
                    DepositRequest.STATUS_DONE,
                ],
            ).order_by("submission_date", "pk")

            for payout in requests.iterator():
                saved = _finance_saved_song_allocations(payout.summary)
                requested = max(Decimal("0"), _finance_decimal(payout.amount))
                saved_total = sum(saved.values(), Decimal("0"))
                usable_saved = (
                    bool(saved)
                    and abs(saved_total - requested) <= FINANCE_QUANTUM
                    and all(song_id in song_totals for song_id in saved)
                )
                allocation = saved if usable_saved else _finance_allocate_across_songs(
                    song_totals,
                    reserved,
                    requested,
                )

                if not usable_saved:
                    summary = dict(payout.summary or {})
                    summary["allocation_version"] = 1
                    summary["song_allocations"] = [
                        {"song_id": song_id, "amount": _finance_string(amount)}
                        for song_id, amount in sorted(allocation.items())
                    ]
                    payout.summary = summary
                    payout.save(update_fields=["summary"])
                    updated += 1

                for song_id, amount in allocation.items():
                    reserved[song_id] = reserved.get(song_id, Decimal("0")) + amount

        self.stdout.write(self.style.SUCCESS(f"Backfilled {updated} payout request(s)."))
