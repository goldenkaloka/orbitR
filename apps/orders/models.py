import uuid

from apps.core.geo_utils import latlng_to_h3
from apps.products.models import Food
from apps.restaurants.models import MealPeriod, Restaurant
from apps.users.models import User
from django.contrib.gis.db import models as gis_models
from django.db import models
from domain.fsm.order_fsm import OrderFSM

# Order lifecycle vocabulary, sourced from OrderFSM (single source of truth).
# Module-level because nested `class Meta` bodies cannot see class attributes.
ORDER_STATUS_CHOICES = tuple(
    (s, s.replace('_', ' ').title()) for s in OrderFSM.STATES
)
ORDER_VALID_STATUSES = list(OrderFSM.STATES)


class Order(models.Model):
    FULFILLMENT_DELIVERY = 'DELIVERY'
    FULFILLMENT_PICKUP = 'PICKUP'
    FULFILLMENT_CHOICES = (
        (FULFILLMENT_DELIVERY, 'Delivery'),
        (FULFILLMENT_PICKUP, 'Pickup'),
    )
    STATUS_CHOICES = ORDER_STATUS_CHOICES
    VALID_STATUSES = ORDER_VALID_STATUSES
    CANCELLED_BY_CHOICES = (
        ('CUSTOMER', 'Customer'),
        ('VENDOR', 'Restaurant'),
        ('DRIVER', 'Driver'),
        ('ADMIN', 'Admin'),
        ('SYSTEM', 'System'),
    )
    CANCELLATION_FAULT_CHOICES = (
        ('CUSTOMER', 'Customer fault'),
        ('VENDOR', 'Restaurant fault'),
        ('DRIVER', 'Driver fault'),
        ('PLATFORM', 'Platform fault'),
        ('SYSTEM', 'System/automatic'),
        ('NONE', 'No fault'),
    )
    CANCELLATION_CATEGORY_CHOICES = (
        ('customer_changed_mind', 'Customer changed mind'),
        ('payment_timeout', 'Payment timeout'),
        ('vendor_rejected', 'Restaurant rejected'),
        ('item_unavailable', 'Item unavailable'),
        ('restaurant_delay', 'Restaurant delay'),
        ('driver_cancelled', 'Driver cancelled'),
        ('driver_no_show', 'Driver no-show'),
        ('system_timeout', 'System timeout'),
        ('admin_cancelled', 'Admin cancelled'),
        ('pool_cancelled', 'Pool cancelled'),
        ('other', 'Other'),
    )
    CONFIRMATION_PENDING = 'PENDING'
    CONFIRMATION_CONFIRMED = 'CONFIRMED'
    CONFIRMATION_UNAVAILABLE = 'UNAVAILABLE'
    CONFIRMATION_STATUS_CHOICES = (
        (CONFIRMATION_PENDING, 'Awaiting restaurant confirmation'),
        (CONFIRMATION_CONFIRMED, 'Confirmed by OrbitR ops'),
        (CONFIRMATION_UNAVAILABLE, 'Restaurant unavailable'),
    )
    RESTAURANT_UNPAID = 'UNPAID'
    RESTAURANT_PAID = 'PAID'
    RESTAURANT_FLOAT = 'FLOAT'
    RESTAURANT_PAYMENT_STATUS_CHOICES = (
        (RESTAURANT_UNPAID, 'Restaurant not paid'),
        (RESTAURANT_PAID, 'Paid by OrbitR'),
        (RESTAURANT_FLOAT, 'Paid from driver float'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='orders', db_index=True)
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='orders', db_index=True)
    pool = models.ForeignKey('OrderPool', on_delete=models.SET_NULL, null=True, blank=True, related_name='final_orders')
    
    # Batching
    batch = models.ForeignKey('DeliveryBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    
    status = models.CharField(max_length=50, default='PENDING_PAYMENT', db_index=True, choices=STATUS_CHOICES)
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

    # Scheduled drop: a shared hand-over point (hostel gate, campus block)
    # instead of a private address. The delivery fee comes from the window,
    # not from distance, because one trip carries many orders.
    drop_point = models.ForeignKey(
        'DropPoint', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='orders', db_index=True,
    )
    drop_window = models.ForeignKey(
        'DropWindow', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='orders', db_index=True,
    )
    
    # PostGIS Location Field
    delivery_location = gis_models.PointField(geography=True, null=True, blank=True, db_index=True)
    
    # H3 Spatial Indexing
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)
    
    notes = models.TextField(null=True, blank=True)

    driver      = models.ForeignKey('delivery.Driver', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders', db_index=True)

    # Real money already sent to the restaurant at pickup, via
    # PickupDisbursement. When non-zero the restaurant is settled and must NOT
    # be paid again at delivery (see ReleasePaymentService).
    pickup_paid_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    ready_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # Pre-order: the eating time the customer picked, and the moment the order
    # must leave the SCHEDULED state (scheduled_for minus prep time).
    scheduled_for = models.DateTimeField(null=True, blank=True, db_index=True)
    release_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Concierge/platform-managed flow: the real restaurant must confirm the
    # items and final bill before the order is dispatched.
    confirmation_status = models.CharField(
        max_length=20,
        choices=CONFIRMATION_STATUS_CHOICES,
        default=CONFIRMATION_CONFIRMED,
        db_index=True,
        help_text="PENDING only for platform-managed orders awaiting an ops call.",
    )
    confirmed_total = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Final amount confirmed with the restaurant; falls back to total_amount.",
    )
    restaurant_payment_status = models.CharField(
        max_length=20,
        choices=RESTAURANT_PAYMENT_STATUS_CHOICES,
        default=RESTAURANT_UNPAID,
        db_index=True,
    )
    restaurant_paid_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0.00,
    )
    restaurant_payment_reference = models.CharField(
        max_length=255, blank=True, default='',
    )
    restaurant_paid_at = models.DateTimeField(null=True, blank=True)
    restaurant_paid_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='restaurant_payments_made',
    )
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
    cancelled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    cancelled_by = models.CharField(max_length=20, choices=CANCELLED_BY_CHOICES, null=True, blank=True, db_index=True)
    cancellation_fault = models.CharField(max_length=20, choices=CANCELLATION_FAULT_CHOICES, null=True, blank=True, db_index=True)
    cancellation_category = models.CharField(max_length=40, choices=CANCELLATION_CATEGORY_CHOICES, null=True, blank=True, db_index=True)
    cancellation_reason = models.TextField(null=True, blank=True)
    affects_restaurant_reputation = models.BooleanField(default=False, db_index=True)

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
            models.Index(fields=['restaurant', 'cancellation_fault', 'cancelled_at'], name='order_cancel_fault_idx'),
        ]
        constraints = [
            # The FSM is advisory Python — this makes the database refuse any
            # status outside the lifecycle vocabulary (DDIA Ch.3: let storage
            # enforce invariants, not call-site convention).
            models.CheckConstraint(
                check=models.Q(status__in=ORDER_VALID_STATUSES),
                name='order_status_valid',
            ),
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


class PreorderSheet(models.Model):
    """One meal service's kitchen list, rendered when bookings close.

    Stored as bytes rather than rebuilt per download: the kitchen prints the
    same list every time, even if an order is declined after the cut-off. It
    also stays out of the public media bucket, where customer names and phones
    would be readable by anyone guessing an object URL.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey(
        Restaurant, on_delete=models.CASCADE, related_name='preorder_sheets',
    )
    service_date = models.DateField(db_index=True)
    meal_period = models.CharField(max_length=16, db_index=True)
    meal_label = models.CharField(max_length=64)
    locked_at = models.DateTimeField(
        help_text='Moment the meal stopped taking pre-orders.',
    )
    order_count = models.PositiveIntegerField(default=0)
    item_count = models.PositiveIntegerField(default=0)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    pdf = models.BinaryField(editable=False)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Newest day first; within a day the meals lock in serving order.
        ordering = ['-service_date', 'locked_at']
        constraints = [
            models.UniqueConstraint(
                fields=['restaurant', 'service_date', 'meal_period'],
                name='unique_preorder_sheet_per_meal',
            ),
        ]

    @property
    def filename(self):
        return f'preorders-{self.service_date}-{self.meal_period.lower()}.pdf'

    def __str__(self):
        return f'{self.service_date} {self.meal_period} @ {self.restaurant_id}'


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

    # A pre-order bucket is cooked and delivered for its booked eating time;
    # an immediate bucket goes out as soon as it is paid and ready. Kept in
    # sync with `scheduled_for` in save() so queries never have to infer it.
    POOL_TYPE_IMMEDIATE = 'IMMEDIATE'
    POOL_TYPE_PREORDER = 'PREORDER'
    POOL_TYPE_CHOICES = (
        (POOL_TYPE_IMMEDIATE, 'Deliver as soon as ready'),
        (POOL_TYPE_PREORDER, 'Deliver at the booked eating time'),
    )
    pool_type = models.CharField(
        max_length=16,
        choices=POOL_TYPE_CHOICES,
        default=POOL_TYPE_IMMEDIATE,
        db_index=True,
    )

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

    # A bucket can be pointed at a scheduled drop, which turns a private group
    # order into a group order for a hostel/campus window.
    drop_window = models.ForeignKey(
        'DropWindow', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pools', db_index=True,
    )
    
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

    # When the group wants to eat. Null means an immediate bucket; a value
    # makes this a group pre-order that reserves one kitchen slot seat.
    scheduled_for = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text='Eating time for a group pre-order; null for immediate buckets.',
    )

    # The meal a pre-order bucket is scoped to. The bucket's menu only offers
    # dishes served at this meal (plus all-day items), which keeps a lunch
    # bucket from collecting dinner-only dishes.
    MEAL_PERIOD_CHOICES = MealPeriod.KIND_CHOICES
    meal_period = models.CharField(
        max_length=16,
        choices=MEAL_PERIOD_CHOICES,
        null=True,
        blank=True,
        db_index=True,
        help_text='Meal this pre-order bucket is booked for; null for immediate buckets.',
    )

    def save(self, *args, **kwargs):
        if self.delivery_location:
            new_h3 = latlng_to_h3(self.delivery_location.y, self.delivery_location.x)
            if self.h3_index != new_h3:
                self.h3_index = new_h3

        self.pool_type = (
            self.POOL_TYPE_PREORDER if self.scheduled_for
            else self.POOL_TYPE_IMMEDIATE
        )
        # An immediate bucket has no meal scope; never leave a stale one behind.
        if not self.scheduled_for:
            self.meal_period = None

        self._original_status = self.status
        super().save(*args, **kwargs)

    class Meta:
        indexes = [
            models.Index(fields=['status', 'expires_at'], name='pool_status_expires_idx'),
            models.Index(fields=['status', 'locked_at'], name='pool_locked_status_idx'),
        ]
        constraints = [
            models.UniqueConstraint(fields=['initiator'], condition=models.Q(status='OPEN'), name='unique_open_pool_per_user'),
            models.CheckConstraint(
                check=(
                    models.Q(pool_type='PREORDER', scheduled_for__isnull=False)
                    | models.Q(pool_type='IMMEDIATE', scheduled_for__isnull=True)
                ),
                name='pool_type_matches_schedule',
            ),
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

class DeliveryBatch(models.Model):
    STATUS_CHOICES = (
        ('ACCUMULATING', 'Collecting orders'),
        ('SOFT_LOCK', 'Closing window — limited joins'),
        ('FINALIZING', 'Closed to new orders, waiting for sections'),
        ('PRE_ASSIGNING', 'Early driver ping — sections nearly ready'),
        ('PENDING_DRIVER', 'Waiting for Admin to Allocate Driver'),
        ('DISPATCHED', 'Driver Assigned'),
        ('DELIVERED', 'All orders delivered'),
        ('PARTIAL_BATCH', 'Some sections cancelled, remainder proceeding'),
        ('CANCELLED', 'Cancelled'),
        ('EXPIRED', 'Expired'),
    )

    BATCH_TYPE_CHOICES = (
        ('SINGLE_RESTAURANT', 'Single Restaurant'),
        ('MULTI_RESTAURANT', 'Multiple Restaurants'),
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
    # A scheduled-drop trip: every order for the same drop window rides in one
    # batch so the rider makes a single trip to a single gate.
    drop_window = models.ForeignKey(
        'DropWindow', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='batches', db_index=True,
    )
    # Pre-orders for one restaurant and one eating-time slot share a batch so
    # the kitchen sees the slot as a single unit of work. Null for live batches.
    preorder_slot_start = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Eating-time slot this batch was pre-grouped for.",
    )

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


class DropPoint(models.Model):
    """A shared hand-over point: a hostel gate, a campus block, an office lobby.

    Not a customer address. Many customers share one pin, which is what makes a
    scheduled drop cheap: the rider makes one trip for the whole group instead
    of one trip per order.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, db_index=True)
    description = models.TextField(blank=True, default='')
    geo_location = gis_models.PointField(geography=True, db_index=True)
    h3_index = models.CharField(max_length=20, blank=True, default='', db_index=True)
    zone = models.ForeignKey(
        'geo.OperationalZone', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='drop_points',
    )

    # A trip pays its flat fee at four orders (4 × 800 = 3,200 against a
    # 3,000 trip), so three would run at a loss. Above `max_orders` a second
    # trip is opened, which pays the flat fee again — keep this at the batch
    # size so one window is always one trip.
    min_orders = models.PositiveIntegerField(
        default=4, help_text="Orders needed for the trip to pay for itself",
    )
    max_orders = models.PositiveIntegerField(
        default=5, help_text="Orders one rider can carry on a single trip",
    )

    contact_name = models.CharField(max_length=120, blank=True, default='')
    contact_phone = models.CharField(max_length=20, blank=True, default='')
    notes = models.TextField(blank=True, default='')

    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['is_active', 'h3_index'], name='drop_point_active_h3_idx'),
        ]

    def save(self, *args, **kwargs):
        if self.geo_location:
            self.h3_index = latlng_to_h3(
                self.geo_location.y, self.geo_location.x,
            ) or ''
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({'active' if self.is_active else 'inactive'})"


class DropWindow(models.Model):
    """A recurring hand-over slot at a :class:`DropPoint`.

    ``cutoff_time`` is the last moment an order may join — it is what lets the
    kitchen cook to a known count instead of guessing. ``end_time`` is when the
    rider is expected to *arrive*, so the kitchen's release time is derived by
    walking back travel time from it.
    """

    WEEKDAY_CHOICES = (
        (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'),
        (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    drop_point = models.ForeignKey(
        DropPoint, on_delete=models.CASCADE, related_name='windows',
    )
    label = models.CharField(
        max_length=60, default='Lunch',
        help_text='Shown to customers, e.g. "Lunch" or "Dinner"',
    )
    # Null weekday means the window runs every day.
    weekday = models.PositiveSmallIntegerField(
        choices=WEEKDAY_CHOICES, null=True, blank=True, db_index=True,
    )
    start_time = models.TimeField(help_text='When the hand-over window opens')
    end_time = models.TimeField(help_text='When the rider is expected to arrive')
    cutoff_time = models.TimeField(
        help_text='Last moment a customer may join this window',
    )

    delivery_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=500.00,
        help_text='Flat fee per order — one trip is shared by many orders',
    )
    # Null falls back to the drop point's capacity.
    max_orders = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Orders per trip; blank uses the drop point default',
    )

    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['drop_point__name', 'start_time']
        constraints = [
            models.UniqueConstraint(
                fields=['drop_point', 'weekday', 'start_time'],
                name='unique_drop_window_per_point_day',
            ),
            models.CheckConstraint(
                check=models.Q(end_time__gt=models.F('start_time')),
                name='drop_window_ends_after_start',
            ),
            models.CheckConstraint(
                check=models.Q(cutoff_time__lte=models.F('start_time')),
                name='drop_window_cutoff_before_start',
            ),
        ]

    def __str__(self):
        day = 'Every day' if self.weekday is None else self.get_weekday_display()
        return f"{self.drop_point.name} · {self.label} · {day} {self.start_time:%H:%M}"

