import uuid

from apps.core.geo_utils import latlng_to_h3
from apps.products.models import Food
from apps.restaurants.models import Restaurant
from apps.users.models import User
from django.contrib.gis.db import models as gis_models
from django.db import models


class Order(models.Model):
    FULFILLMENT_DELIVERY = 'DELIVERY'
    FULFILLMENT_PICKUP = 'PICKUP'
    FULFILLMENT_CHOICES = (
        (FULFILLMENT_DELIVERY, 'Delivery'),
        (FULFILLMENT_PICKUP, 'Pickup'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='orders', db_index=True)
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='orders', db_index=True)
    pool = models.ForeignKey('OrderPool', on_delete=models.SET_NULL, null=True, blank=True, related_name='final_orders')
    
    # Batching
    batch = models.ForeignKey('DeliveryBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    
    status = models.CharField(max_length=50, default='PENDING_PAYMENT', db_index=True) 
    is_paid = models.BooleanField(default=False, db_index=True)
    is_settled = models.BooleanField(default=False, db_index=True)
    
    # Financials
    items_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=500.00)
    service_fee = models.DecimalField(max_digits=10, decimal_places=2, default=300.00)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    
    delivery_address = models.TextField(null=True, blank=True)
    fulfillment_type = models.CharField(
        max_length=20,
        choices=FULFILLMENT_CHOICES,
        default=FULFILLMENT_DELIVERY,
        db_index=True,
    )
    
    # PostGIS Location Field
    delivery_location = gis_models.PointField(geography=True, null=True, blank=True, db_index=True)
    
    # H3 Spatial Indexing
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)
    
    notes = models.TextField(null=True, blank=True)
    driver = models.ForeignKey('delivery.Driver', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders', db_index=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    preparing_at = models.DateTimeField(null=True, blank=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    
    # OTP Delivery Verification
    delivery_otp = models.CharField(max_length=6, null=True, blank=True)
    
    # Group Ordering Metadata
    group_breakdown = models.JSONField(null=True, blank=True)

    # Pool fast-path flag — set by PoolOrderService.finalize_pool()
    # Tells BatchPipeline to skip H3 re-clustering (pool already shares a delivery point)
    is_pool_order = models.BooleanField(
        default=False,
        db_index=True,
        help_text='True when this order was created by finalising an OrderPool.',
    )

    route_sequence = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    original_delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    discount_applied = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    status_changed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def save(self, *args, **kwargs):
        if self.delivery_location:
            new_h3 = latlng_to_h3(self.delivery_location.y, self.delivery_location.x)
            if self.h3_index != new_h3:
                self.h3_index = new_h3

        original_status = getattr(self, '_original_status', None)
        if original_status is not None and self.status != original_status:
            from django.utils import timezone
            self.status_changed_at = timezone.now()

        self._original_status = self.status
        super().save(*args, **kwargs)

    class Meta:
        indexes = [
            models.Index(fields=['restaurant', 'status'], name='order_vendor_status_idx'),
            models.Index(fields=['driver', 'created_at'], name='order_driver_created_idx'),
        ]

    def __str__(self):
        return f"Order {self.id} - {self.status}"

class OrderItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    food = models.ForeignKey(Food, on_delete=models.SET_NULL, null=True, blank=True)
    section = models.ForeignKey('restaurants.RestaurantSection', on_delete=models.SET_NULL, null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2) # snapshot price at order time
    cancelled_quantity = models.PositiveIntegerField(default=0, help_text='Quantity cancelled by vendor')

    @property
    def active_quantity(self):
        return self.quantity - self.cancelled_quantity

    def __str__(self):
        return f"{self.quantity} x {self.food.name}"

class Dispute(models.Model):
    CATEGORY_CHOICES = [
        ('wrong_items', 'Wrong Items'),
        ('missing_items', 'Missing Items'),
        ('quality', 'Poor Quality'),
        ('late', 'Late Delivery'),
        ('never_arrived', 'Never Arrived'),
        ('charging', 'Incorrect Charge'),
        ('other', 'Other'),
    ]

    STATUS_CHOICES = [
        ('PENDING', 'Pending Review'),
        ('INVESTIGATING', 'Under Investigation'),
        ('RESOLVED_REFUND', 'Resolved — Refund Issued'),
        ('RESOLVED_PARTIAL', 'Resolved — Partial Refund'),
        ('RESOLVED_DISMISSED', 'Resolved — Dismissed'),
        ('ESCALATED', 'Escalated'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name='disputes')
    raised_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='disputes_raised')
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_disputes')
    parent_dispute = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='escalations')

    reason = models.TextField()
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='other')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    resolution_note = models.TextField(null=True, blank=True)
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='resolved_disputes')
    escalated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['order', 'raised_by', 'category'],
                condition=~models.Q(status__in=['RESOLVED_REFUND', 'RESOLVED_PARTIAL', 'RESOLVED_DISMISSED']),
                name='unique_active_dispute_per_category',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'created_at'], name='dispute_status_created_idx'),
        ]

    def __str__(self):
        return f"Dispute for Order {self.order.id} - {self.status} ({self.get_category_display()})"

class OrderPool(models.Model):
    STATUS_CHOICES = (
        ('OPEN', 'Open for additions'),
        ('LOCKED', 'Locked for payment'),
        ('PAID', 'All members paid, restaurant preparing'),
        ('READY', 'Ready for dispatch'),
        ('DISPATCHING', 'Driver search in progress'),
        ('DELIVERING', 'Driver en route'),
        ('COMPLETED', 'All delivered'),
        ('CANCELLED', 'Cancelled'),
        ('EXPIRED', 'Expired'),
        ('REFUNDED', 'Refunded'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    initiator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='initiated_pools')
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='order_pools')
    
    invite_code = models.CharField(max_length=10, unique=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN', db_index=True)

    # Deprecated: master Order created during old finalize_pool path
    # Will be removed once all pools use the new direct path
    final_order = models.OneToOneField('Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='source_pool')

    # Link to the DeliveryBatch created when this pool was dispatched.
    batch = models.ForeignKey(
        'DeliveryBatch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='source_pools',
        help_text='The delivery batch created when this pool was dispatched.',
    )
    
    # Shared delivery details
    delivery_address = models.TextField()
    delivery_location = gis_models.PointField(geography=True, db_index=True)
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)
    
    # Financials — pooled across all members
    total_delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    service_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_items_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    expires_at = models.DateTimeField()
    locked_at = models.DateTimeField(null=True, blank=True)
    lock_expires_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if self.delivery_location:
            new_h3 = latlng_to_h3(self.delivery_location.y, self.delivery_location.x)
            if self.h3_index != new_h3:
                self.h3_index = new_h3

        self._original_status = self.status
        super().save(*args, **kwargs)

    class Meta:
        indexes = [
            models.Index(fields=['status', 'expires_at'], name='pool_status_expires_idx'),
            models.Index(fields=['status', 'locked_at'], name='pool_locked_status_idx'),
        ]
        constraints = [
            models.UniqueConstraint(fields=['initiator'], condition=models.Q(status='OPEN'), name='unique_open_pool_per_user')
        ]

    @property
    def is_expired(self):
        from django.utils import timezone
        return timezone.now() > self.expires_at

    @property
    def all_members_paid(self) -> bool:
        return not self.members.exclude(status='PAID').exists()

    @property
    def member_count(self) -> int:
        return self.members.count()

    def __str__(self):
        return f"Pool {self.invite_code} at {self.restaurant.name}"

class PoolMember(models.Model):
    STATUS_CHOICES = (
        ('ADDING', 'Adding items'),
        ('READY', 'Ready to pay'),
        ('PAID', 'Paid share'),
        ('REFUNDED', 'Refunded'),
    )

    pool = models.ForeignKey(OrderPool, on_delete=models.CASCADE, related_name='members')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pool_memberships')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ADDING')
    
    delivery_fee_share = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    service_fee_share = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    items_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    # Items stored as JSON: [{food_id, name, quantity, price, image_url}]
    # Replaces the separate PoolItem table for reads in the dispatch path.
    items = models.JSONField(default=list, blank=True)
    
    joined_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('pool', 'user')

class PoolItem(models.Model):
    member = models.ForeignKey(PoolMember, on_delete=models.CASCADE, related_name='pool_item_entries')
    food = models.ForeignKey(Food, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    price_at_addition = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.quantity} x {self.food.name} (User: {self.member.user.username})"

class DeliveryBatch(models.Model):
    STATUS_CHOICES = (
        ('ACCUMULATING', 'Collecting orders'),
        ('SOFT_LOCK', 'Closing window — limited joins'),
        ('FINALIZING', 'Closed to new orders, waiting for sections'),
        ('PRE_ASSIGNING', 'Early driver ping — sections nearly ready'),
        ('PENDING_DRIVER', 'Waiting for Restaurant to Assign Driver'),
        ('DISPATCHED', 'Driver Assigned'),
        ('DELIVERED', 'All orders delivered'),
        ('PARTIAL_BATCH', 'Some sections cancelled, remainder proceeding'),
        ('CANCELLED', 'Cancelled'),
        ('EXPIRED', 'Expired'),
    )

    BATCH_TYPE_CHOICES = (
        ('SINGLE_RESTAURANT', 'Single Restaurant'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='batches', null=True, blank=True)
    driver = models.ForeignKey('delivery.Driver', on_delete=models.SET_NULL, null=True, blank=True, related_name='batches')

    batch_type = models.CharField(max_length=20, choices=BATCH_TYPE_CHOICES, default='SINGLE_RESTAURANT', db_index=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACCUMULATING', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    route_optimization = models.JSONField(null=True, blank=True)

    user_paid_total = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Total fees paid by users (delivery + service)")
    original_total_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Original delivery fees before discount")
    discounted_total_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Discounted delivery fees after batch discount")
    driver_payout = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Total payout to driver")
    company_profit = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, help_text="Company profit (user_paid - driver_payout)")
    route_distance_km = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Optimized route distance")
    discount_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, help_text="Discount rate applied to batch (e.g., 0.15 for 15%)")

    offer_attempt = models.PositiveIntegerField(default=0, help_text="Number of times batch has been offered to drivers")
    offer_sent_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when batch was last offered")
    waiting_since = models.DateTimeField(null=True, blank=True, help_text="Timestamp when batch entered PENDING_DRIVER status")
    
    zone_ids = models.JSONField(null=True, blank=True, help_text="List of zone IDs this batch spans")
    is_inter_zone = models.BooleanField(default=False, help_text="True if batch spans multiple zones")

    # ── New lifecycle timing fields ──────────────────────────────────────────
    accumulating_since = models.DateTimeField(null=True, blank=True, help_text="When batch entered ACCUMULATING")
    soft_lock_at = models.DateTimeField(null=True, blank=True, help_text="When batch entered SOFT_LOCK")
    finalized_at = models.DateTimeField(null=True, blank=True, help_text="When batch entered FINALIZING")
    pre_assigned_at = models.DateTimeField(null=True, blank=True, help_text="When batch entered PRE_ASSIGNING")
    delivered_at = models.DateTimeField(null=True, blank=True, help_text="When batch entered DELIVERED")

    max_orders = models.PositiveIntegerField(default=5, help_text="Max orders before batch closes to new joins")
    accumulate_window_minutes = models.PositiveIntegerField(default=10, help_text="Minutes to stay in ACCUMULATING before SOFT_LOCK")

    first_order_confirmed_at = models.DateTimeField(null=True, blank=True, help_text="When first order was confirmed into this batch")
    last_order_confirmed_at = models.DateTimeField(null=True, blank=True, help_text="When last order was confirmed into this batch")

    def __str__(self):
        if self.restaurant:
            return f"Batch {self.id} - {self.restaurant.name} ({self.status})"
        return f"Batch {self.id} - Multi-Restaurant ({self.status})"


class BatchSectionStatus(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('PREPARING', 'Preparing'),
        ('READY', 'Ready'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(DeliveryBatch, on_delete=models.CASCADE, related_name='section_statuses')
    section = models.ForeignKey('restaurants.RestaurantSection', on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    estimated_ready_at = models.DateTimeField(null=True, blank=True, help_text="Estimated completion time for this section")
    started_prepping_at = models.DateTimeField(null=True, blank=True, help_text="When cooking started for this section in this batch")

    class Meta:
        unique_together = ('batch', 'section')
        verbose_name_plural = 'Batch section statuses'
        indexes = [
            models.Index(fields=['status', 'updated_at'], name='bss_status_updated_idx'),
        ]

    def __str__(self):
        return f"{self.section.name} @ {self.batch.id}: {self.status}"


class BatchRestaurant(models.Model):
    batch = models.ForeignKey(DeliveryBatch, on_delete=models.CASCADE, related_name='batch_restaurants')
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='batch_memberships')
    pickup_sequence = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('batch', 'restaurant')
        ordering = ['pickup_sequence']


class BatchingStrategy(models.Model):
    CLUSTER_BY_CHOICES = (
        ('restaurant', 'Same Restaurant'),
        ('delivery_h3', 'Delivery H3 Cluster'),
        ('time_window', 'Time Window'),
        ('hybrid', 'Hybrid (Restaurant + Location)'),
    )

    name = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    priority = models.PositiveIntegerField(default=10)

    cluster_by = models.CharField(max_length=30, choices=CLUSTER_BY_CHOICES, default='delivery_h3')
    h3_resolution = models.PositiveIntegerField(default=10)
    neighbor_ring_k = models.PositiveIntegerField(default=2)

    constraints = models.JSONField(default=dict)
    pricing = models.JSONField(default=dict)
    route_optimization = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-priority', 'name']
        verbose_name_plural = 'Batching Strategies'

    def __str__(self):
        return f"{self.name} (priority={self.priority}, active={self.is_active})"

    def get_constraint(self, key, default=None):
        return self.constraints.get(key, default)

    def get_pricing(self, key, default=None):
        return self.pricing.get(key, default)
