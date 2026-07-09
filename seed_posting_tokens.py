import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.marketplace.models import PostingTokenConfig, PostingTokenBundleOption

# 1. Create default config if not exists
config, created = PostingTokenConfig.objects.get_or_create(
    defaults={
        'free_tokens_for_new_users': 5,
        'token_deduction_per_listing': 1
    }
)
if created:
    print("Created default PostingTokenConfig: 5 free tokens, 1 token per post.")
else:
    print("Default PostingTokenConfig already exists.")

# 2. Create bundle options
bundles = [
    {
        'id': 'starter',
        'name': 'Starter Pack',
        'description': 'Buy 8 posting tokens',
        'price': 1000.00,
        'tokens_granted': 8,
        'is_active': True
    },
    {
        'id': 'pro',
        'name': 'Pro Pack',
        'description': 'Buy 50 posting tokens (10 free!)',
        'price': 5000.00,
        'tokens_granted': 50,
        'is_active': True
    },
    {
        'id': 'business',
        'name': 'Business Pack',
        'description': 'Buy 120 posting tokens (50 free!)',
        'price': 10000.00,
        'tokens_granted': 120,
        'is_active': True
    }
]

for b_data in bundles:
    bundle, created = PostingTokenBundleOption.objects.get_or_create(
        id=b_data['id'],
        defaults=b_data
    )
    if created:
        print(f"Created bundle option: {b_data['name']}")
    else:
        # Update existing bundle prices / tokens in case they changed
        for key, val in b_data.items():
            setattr(bundle, key, val)
        bundle.save()
        print(f"Updated bundle option: {b_data['name']}")

print("Seeding completed successfully!")
