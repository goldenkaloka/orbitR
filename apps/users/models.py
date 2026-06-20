import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
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
