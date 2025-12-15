from django.core.management.base import BaseCommand
from api.models import Tag


class Command(BaseCommand):
    help = 'Create initial music tags'

    def handle(self, *args, **options):
        # Define tags with Persian names and English slugs
        tags_data = [
            {'name': 'تابستانی', 'slug': 'summer'},
            {'name': 'زمستانی', 'slug': 'winter'},
            {'name': 'بهاری', 'slug': 'spring'},
            {'name': 'پاییزی', 'slug': 'autumn'},
            {'name': 'جدید', 'slug': 'new'},
            {'name': 'کلاسیک', 'slug': 'classic'},
            {'name': 'ویژه', 'slug': 'special'},
            {'name': 'پرفروش', 'slug': 'bestseller'},
            {'name': 'جدیدترین‌ها', 'slug': 'latest'},
            {'name': 'محبوب', 'slug': 'popular'},
            {'name': 'وایرال', 'slug': 'viral'},
            {'name': 'تیتراژ', 'slug': 'soundtrack'},
            {'name': 'تبلیغاتی', 'slug': 'advertisement'},
            {'name': 'فستیوال', 'slug': 'festival'},
            {'name': 'کنسرت', 'slug': 'concert'},
            {'name': 'ریمیکس', 'slug': 'remix'},
            {'name': 'اصل', 'slug': 'original'},
            {'name': 'کاور', 'slug': 'cover'},
            {'name': 'لایو', 'slug': 'live'},
            {'name': 'استودیویی', 'slug': 'studio'},
        ]

        created_count = 0
        updated_count = 0

        for tag_data in tags_data:
            tag, created = Tag.objects.get_or_create(
                slug=tag_data['slug'],
                defaults={'name': tag_data['name']}
            )
            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f'Created tag: {tag.name} ({tag.slug})')
                )
            else:
                # Update name if it changed
                if tag.name != tag_data['name']:
                    tag.name = tag_data['name']
                    tag.save()
                    updated_count += 1
                    self.stdout.write(
                        self.style.WARNING(f'Updated tag: {tag.name} ({tag.slug})')
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f'\nSuccessfully processed {len(tags_data)} tags.'
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'Created: {created_count} tags, Updated: {updated_count} tags'
            )
        )