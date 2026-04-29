from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from unfold.admin import ModelAdmin
from unfold.decorators import display
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    """Full-featured Unfold admin for the custom User model."""

    list_display = (
        'display_header', 'phone', 'show_role',
        'is_active', 'is_staff', 'created_at',
    )
    list_filter = ('role', 'is_active', 'is_staff', 'is_superuser')
    search_fields = ('username', 'email', 'phone', 'first_name', 'last_name')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 25
    list_editable = ('is_active',)

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Personal Info', {'fields': ('first_name', 'last_name', 'email', 'phone')}),
        ('Role & Permissions', {
            'fields': ('role', 'is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'),
        }),
        ('Important dates', {'fields': ('last_login', 'date_joined', 'created_at')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'phone', 'role', 'password1', 'password2'),
        }),
    )
    readonly_fields = ('created_at', 'date_joined', 'last_login')

    @display(header=True, description="User")
    def display_header(self, obj):
        initials = ''
        if obj.first_name and obj.last_name:
            initials = obj.first_name[0] + obj.last_name[0]
        elif obj.username:
            initials = obj.username[:2].upper()
        return obj.username, obj.email or '', initials

    @display(
        description="Role",
        ordering="role",
        label={
            "customer": "info",
            "driver": "warning",
            "vendor": "success",
            "admin": "danger",
        },
    )
    def show_role(self, obj):
        return obj.role

    def get_queryset(self, request):
        return super().get_queryset(request).select_related()
