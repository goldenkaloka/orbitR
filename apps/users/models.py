import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.contrib.gis.db import models as gis_models
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, phone=None, username=None, password=None, **extra_fields):
        if not phone:
            import random
            phone = f"+255{random.randint(100000000, 999999999)}"
        user = self.model(phone=phone, username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone=None, username=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', 'admin')

        return self.create_user(phone, username, password, **extra_fields)

def get_default_free_tokens():
    try:
        from apps.marketplace.models import PostingTokenConfig
        config = PostingTokenConfig.objects.first()
        return config.free_tokens_for_new_users if config else 5
    except Exception:
        return 5


class User(AbstractUser):
    ROLE_CHOICES = (
        ('customer', 'customer'),
        ('driver', 'driver'),
        ('vendor', 'vendor'),
        ('admin', 'admin'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=150, unique=False, blank=True, null=True)
    phone = models.CharField(max_length=20, unique=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='customer', db_index=True)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    avatar_url = models.URLField(max_length=500, blank=True, null=True)
    onesignal_id = models.CharField(max_length=255, blank=True, null=True, help_text="OneSignal Player/Subscription ID for push notifications")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    posting_tokens_balance = models.PositiveIntegerField(default=get_default_free_tokens)

    objects = UserManager()

    USERNAME_FIELD = 'phone'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return f"{self.username or self.phone} ({self.role})"

    _cached_serialized_role = None

    @property
    def serialized_role(self):
        if self._cached_serialized_role is not None:
            return self._cached_serialized_role
        from apps.restaurants.models import SectionStaff
        if self.role == 'customer' and SectionStaff.objects.filter(user=self).exists():
            self._cached_serialized_role = 'vendor_staff'
        else:
            self._cached_serialized_role = self.role
        return self._cached_serialized_role

class OTPCode(models.Model):
    MAX_FAILED_ATTEMPTS = 5

    phone = models.CharField(max_length=20, unique=True)
    code = models.CharField(max_length=6)
    failed_attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def is_valid(self):
        import datetime

        from django.utils import timezone
        return timezone.now() < self.created_at + datetime.timedelta(minutes=5)


class UserAddress(models.Model):
    """A customer's saved delivery address.

    ``place_name`` is deliberately free text: street addressing in Tanzania is
    unreliable, so the words customers actually use ("Kwa Mangi", "nyuma ya
    shule") are the most useful part of the record.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='addresses', db_index=True
    )
    label = models.CharField(max_length=40)
    place_name = models.CharField(max_length=120)
    address_line = models.CharField(max_length=255, blank=True, default='')
    landmark = models.CharField(max_length=160, blank=True, default='')

    delivery_location = gis_models.PointField(geography=True)
    h3_index = models.CharField(max_length=20, blank=True, default='', db_index=True)
    zone = models.ForeignKey(
        'geo.OperationalZone',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='saved_addresses',
    )

    is_default = models.BooleanField(default=False)
    use_count = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', '-last_used_at', '-created_at']
        indexes = [
            models.Index(
                fields=['user', 'is_default'],
                name='users_addr_user_default_idx',
            )
        ]

    def __str__(self):
        return f"{self.label}: {self.place_name}"

    def save(self, *args, **kwargs):
        if self.delivery_location:
            from apps.core.geo_utils import latlng_to_h3

            self.h3_index = latlng_to_h3(
                self.delivery_location.y, self.delivery_location.x
            )

        super().save(*args, **kwargs)

        if self.h3_index and self.zone_id is None:
            from apps.core.geo_utils import get_zone_from_h3

            zone = get_zone_from_h3(self.h3_index)
            if zone is None and self.delivery_location:
                try:
                    from domain.services.geo_services import GeoDomainService

                    zone = GeoDomainService.find_zone_for_point(
                        self.delivery_location.x, self.delivery_location.y
                    )
                except Exception:
                    zone = None
            if zone is not None:
                UserAddress.objects.filter(pk=self.pk).update(zone=zone)
                self.zone = zone

