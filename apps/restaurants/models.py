import uuid
from decimal import Decimal

from apps.core.geo_utils import latlng_to_h3
from apps.users.models import User
from django.contrib.gis.db import models as gis_models
from django.db import models


class RestaurantSection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey('Restaurant', on_delete=models.CASCADE, related_name='sections')
    name = models.CharField(max_length=255)
    specialization = models.CharField(max_length=100, blank=True, db_index=True)
    description = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='sections/', blank=True, null=True)
    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveIntegerField(default=0)
    prep_time = models.PositiveIntegerField(default=20, help_text='Preparation time in minutes')
    max_concurrent_orders = models.PositiveIntegerField(
        default=5,
        help_text='Max concurrent un-cooked orders before this section is busy'
    )

    class Meta:
        ordering = ['sort_order', 'name']

    def save(self, *args, **kwargs):
        if self._state.adding and not kwargs.get('force_insert') and self.prep_time:
            self.max_concurrent_orders = max(3, int(round(100 / self.prep_time)))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} @ {self.restaurant.name}"


class MealPeriod(models.Model):
    """
    A restaurant's serving window for breakfast, lunch or dinner.

    Pre-orders are only accepted inside one of these windows, and each meal
    can cap its own pre-order slots. A restaurant with no rows here serves the
    platform defaults; an existing row set replaces them entirely, so a
    restaurant can disable a meal by deactivating its row.
    """

    BREAKFAST = 'BREAKFAST'
    LUNCH = 'LUNCH'
    DINNER = 'DINNER'
    KIND_CHOICES = (
        (BREAKFAST, 'Breakfast'),
        (LUNCH, 'Lunch'),
        (DINNER, 'Dinner'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey(
        'Restaurant', on_delete=models.CASCADE, related_name='meal_periods',
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, db_index=True)
    label = models.CharField(
        max_length=64,
        blank=True,
        help_text="Customer-facing name; defaults to the meal kind.",
    )
    start_time = models.TimeField()
    end_time = models.TimeField()
    preorder_cutoff_time = models.TimeField(
        null=True,
        blank=True,
        help_text=(
            "Latest time of day a pre-order for this meal can be placed "
            "(e.g. 11:00 for lunch). Blank accepts pre-orders until the "
            "meal window closes."
        ),
    )
    slot_capacity = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Pre-orders accepted per slot during this meal; blank uses the restaurant capacity, 0 disables the meal.",
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['start_time']
        constraints = [
            models.UniqueConstraint(
                fields=['restaurant', 'kind'],
                name='unique_meal_period_per_kind',
            ),
            models.CheckConstraint(
                check=models.Q(end_time__gt=models.F('start_time')),
                name='meal_period_ends_after_start',
            ),
        ]

    def __str__(self):
        return f"{self.label or self.get_kind_display()} @ {self.restaurant.name}"


class SectionStaff(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='section_staff_assignments')
    section = models.ForeignKey(RestaurantSection, on_delete=models.CASCADE, related_name='staff')

    class Meta:
        unique_together = ('user', 'section')

    def __str__(self):
        return f"{self.user.username} → {self.section.name}"


class Restaurant(models.Model):
    SOURCE_PARTNER = 'PARTNER'
    SOURCE_PLATFORM_MANAGED = 'PLATFORM_MANAGED'
    SOURCE_TYPE_CHOICES = (
        (SOURCE_PARTNER, 'Verified partner'),
        (SOURCE_PLATFORM_MANAGED, 'Platform-managed concierge'),
    )

    # How OrbitR settles the actual restaurant for an order. This is separate
    # from `owner`: an admin-created/platform-managed listing is often owned by
    # the admin user in the database but is still a third-party restaurant that
    # must be paid.
    SETTLEMENT_PLATFORM_PREPAID = 'PLATFORM_PREPAID'
    SETTLEMENT_DRIVER_FLOAT = 'DRIVER_FLOAT'
    SETTLEMENT_COMPANY_OWNED = 'COMPANY_OWNED'
    SETTLEMENT_VENDOR_PAYOUT = 'VENDOR_PAYOUT'
    SETTLEMENT_CHOICES = (
        (SETTLEMENT_PLATFORM_PREPAID, 'OrbitR pays restaurant before pickup'),
        (SETTLEMENT_DRIVER_FLOAT, 'Driver pays from company float'),
        (SETTLEMENT_COMPANY_OWNED, 'Company-owned — internal settlement'),
        (SETTLEMENT_VENDOR_PAYOUT, 'Vendor payout (existing partner flow)'),
    )

    # ── Dispatch authority ────────────────────────────────────────────────────
    # Who allocates a driver for this restaurant's batches. Distinct from
    # source_type, which controls money/SMS routing and customer-facing
    # disclosure — not who dispatches.
    DISPATCH_ADMIN = 'admin'
    DISPATCH_RESTRANT = 'restaurant'
    DISPATCH_MODE_CHOICES = (
        (DISPATCH_ADMIN, 'Admin dispatch'),
        (DISPATCH_RESTRANT, 'Restaurant dispatch'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='restaurants', db_index=True)
    source_type = models.CharField(
        max_length=24,
        choices=SOURCE_TYPE_CHOICES,
        default=SOURCE_PARTNER,
        db_index=True,
        help_text="Partner restaurants are operated by the vendor; platform-managed listings are fulfilled by OrbitR ops.",
    )
    is_verified_partner = models.BooleanField(
        default=True,
        db_index=True,
        help_text="True only when the restaurant directly manages and confirms its own listing.",
    )
    public_disclaimer = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="Optional customer-facing note for platform-managed listings.",
    )

    # Real settlement configuration. For platform-managed listings the admin
    # user is only the listing operator; these fields identify the actual
    # restaurant/payee and how OrbitR pays it.
    settlement_mode = models.CharField(
        max_length=24,
        choices=SETTLEMENT_CHOICES,
        default=SETTLEMENT_VENDOR_PAYOUT,
        db_index=True,
        help_text="How the restaurant is paid for an order.",
    )
    payout_phone = models.CharField(
        max_length=32,
        blank=True,
        default='',
        help_text="Mobile-money number / Lipa Namba that receives the restaurant payout.",
    )
    payout_network = models.CharField(
        max_length=32,
        blank=True,
        default='',
        help_text="M_PESA, TIGO_PESA, AIRTEL_MONEY, HALOPESA.",
    )
    payout_account_name = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text="Name registered on the payout account.",
    )

    # Who allocates a driver when this restaurant's batches need one.
    # admin      — admin dispatch queue owns allocation (default)
    # restaurant — the restaurant allocates its own driver
    # Admins may always allocate as an escalation, regardless of this value.
    dispatch_mode = models.CharField(
        max_length=20,
        choices=DISPATCH_MODE_CHOICES,
        default=DISPATCH_ADMIN,
        db_index=True,
        help_text="Who allocates drivers for this restaurant's batches.",
    )

    @property
    def allows_multi_restaurant_batching(self):
        """
        Whether this restaurant's orders may share a batch with other
        restaurants.

        Restricted to platform-managed listings. Partner restaurants keep their
        own driver pool and their own order data, so their batches must stay
        single-restaurant — pooling them would expose one restaurant's orders to
        another and force the partner's own drivers to serve it.
        """
        return self.source_type == self.SOURCE_PLATFORM_MANAGED
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='restaurants/', blank=True, null=True)
    logo = models.ImageField(upload_to='restaurants/logos/', blank=True, null=True)
    location = models.CharField(max_length=255) # Text address
    
    # PostGIS Location Field for the restaurant itself
    geo_location = gis_models.PointField(geography=True, null=True, blank=True, db_index=True)
    
    # H3 Spatial Indexing
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)

    # Maximum delivery radius — replaces DeliveryZone model entirely.
    # "Does this restaurant deliver here?" = distance <= max_delivery_radius_km
    max_delivery_radius_km = models.FloatField(
        default=5.0,
        help_text="Maximum delivery distance in km from the restaurant"
    )

    # Average food preparation time in minutes — used for ETA calculation
    average_prep_time = models.PositiveIntegerField(
        default=20,
        help_text="Average preparation time in minutes"
    )

    # Pre-order capacity per eating-time slot. Blank derives it from the
    # sections' prep throughput; 0 disables pre-orders for this kitchen.
    preorder_slot_capacity = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Pre-orders accepted per slot; blank derives it from kitchen throughput.",
    )

    # Commission rate for platform monetization
    commission_rate = models.DecimalField(
        max_digits=4, 
        decimal_places=3, 
        default=Decimal('0.050'),
        help_text="Percentage of food subtotal taken by the platform (e.g., 0.050 for 5%)"
    )

    category = models.CharField(max_length=100, default='General', db_index=True)
    price_range = models.CharField(max_length=50, default='TSH 2k–5k')
    rating = models.DecimalField(max_digits=3, decimal_places=1, default=0.0)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    def save(self, *args, **kwargs):
        # The flag only restates source_type ("Verified partner" is the partner
        # source label). Deriving it here keeps the two from drifting when ops
        # edits one without the other.
        self.is_verified_partner = self.source_type == self.SOURCE_PARTNER
        if self.geo_location:
            new_h3 = latlng_to_h3(self.geo_location.y, self.geo_location.x)
            if self.h3_index != new_h3:
                self.h3_index = new_h3
        super().save(*args, **kwargs)


    @property
    def foods(self):
        from apps.products.models import Food
        return Food.objects.filter(section__restaurant=self)

    def __str__(self):
        return self.name
