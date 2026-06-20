# Signal handlers removed — orphaned code.
# These used to delete cache keys (restaurants_list_global, vendor_restaurants_{id},
# restaurant_detail_{id}) that are never read anywhere.
# Actual cache invalidation is handled by invalidate_restaurant_cache() in api/views/core.py,
# which uses a version-number-based approach for restaurant list caches.
