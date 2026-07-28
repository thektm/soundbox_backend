from django.core.management.base import BaseCommand
from api.models import Tag


class Command(BaseCommand):
    help = 'Create initial music tags'

    def handle(self, *args, **options):
        # Define tags with Persian names and English slugs
        tags_data = [
            {'name': 'تابستانی', 'name_en': 'Summer', 'slug': 'summer'},
            {'name': 'زمستانی', 'name_en': 'Winter', 'slug': 'winter'},
            {'name': 'بهاری', 'name_en': 'Spring', 'slug': 'spring'},
            {'name': 'پاییزی', 'name_en': 'Autumn', 'slug': 'autumn'},
            {'name': 'جدید', 'name_en': 'New', 'slug': 'new'},
            {'name': 'کلاسیک', 'name_en': 'Classic', 'slug': 'classic'},
            {'name': 'ویژه', 'name_en': 'Special', 'slug': 'special'},
            {'name': 'پرفروش', 'name_en': 'Bestseller', 'slug': 'bestseller'},
            {'name': 'جدیدترین‌ها', 'name_en': 'Latest', 'slug': 'latest'},
            {'name': 'محبوب', 'name_en': 'Popular', 'slug': 'popular'},
            {'name': 'وایرال', 'name_en': 'Viral', 'slug': 'viral'},
            {'name': 'تیتراژ', 'name_en': 'Soundtrack', 'slug': 'soundtrack'},
            {'name': 'تبلیغاتی', 'name_en': 'Advertisement', 'slug': 'advertisement'},
            {'name': 'فستیوال', 'name_en': 'Festival', 'slug': 'festival'},
            {'name': 'کنسرت', 'name_en': 'Concert', 'slug': 'concert'},
            {'name': 'ریمیکس', 'name_en': 'Remix', 'slug': 'remix'},
            {'name': 'اصل', 'name_en': 'Original', 'slug': 'original'},
            {'name': 'کاور', 'name_en': 'Cover', 'slug': 'cover'},
            {'name': 'لایو', 'name_en': 'Live', 'slug': 'live'},
            {'name': 'استودیویی', 'name_en': 'Studio', 'slug': 'studio'},
        ]

        created_count = 0
        updated_count = 0

        for tag_data in tags_data:
            tag, created = Tag.objects.get_or_create(
                slug=tag_data['slug'],
                defaults={'name': tag_data['name'], 'name_en': tag_data['name_en']}
            )
            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f'Created tag: {tag.name} ({tag.slug})')
                )
            else:
                # Update name if it changed
                if tag.name != tag_data['name'] or tag.name_en != tag_data['name_en']:
                    tag.name = tag_data['name']
                    tag.name_en = tag_data['name_en']
                    tag.save(update_fields=['name', 'name_en'])
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