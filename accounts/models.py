from django.db import models
from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import RegexValidator
from django.conf import settings
from django.utils import timezone

# -------------------------------------------------
# Custom Manager
# -------------------------------------------------
class CustomUserManager(UserManager):
    """
    Ensure superusers are created with ADMIN role and without ChurchMember linkage.
    Also prompt for phone_number via REQUIRED_FIELDS in the model.
    """
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        # Force ADMIN role and unlink any ChurchMember
        extra_fields["user_type"] = "ADMIN"
        extra_fields["church_member"] = None
        return super().create_superuser(username, email=email, password=password, **extra_fields)


# -------------------------------------------------
# Custom User
# -------------------------------------------------
class CustomUser(AbstractUser):
    """
    Custom user model that adds:
    - Phone number (must start with +255)
    - User type (ADMIN, CHURCH_MEMBER)
    - Optional link to a ChurchMember (required only for CHURCH_MEMBER users)
    - Profile picture
    - Date created
    - Agreement to Terms & Conditions
    """

    email = models.EmailField("email address", blank=True, null=True)

    phone_validator = RegexValidator(
        regex=r'^\+255\d{9}$',
        message="Phone number must be in the format: +255XXXXXXXXX (9 digits after +255).",
    )

    phone_number = models.CharField(
        max_length=13,
        unique=True,
        validators=[phone_validator],
        help_text="Format: +255XXXXXXXXX (9 digits after +255).",
    )

    USER_TYPES = [
        ("ADMIN", "Admin (Superuser)"),
        ("CHURCH_MEMBER", "Church Member"),
    ]
    user_type = models.CharField(
        max_length=30,
        choices=USER_TYPES,
        default="CHURCH_MEMBER",
        help_text="Indicates the role this user will have in the system.",
    )

    # Use a string reference to avoid circular imports at import-time
    church_member = models.OneToOneField(
        "members.ChurchMember",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="user_account",
        help_text="Church Member linked to this user (only required for CHURCH_MEMBER users).",
    )

    profile_picture = models.ImageField(
        upload_to="profile_pictures/",
        blank=True,
        null=True,
        help_text="Upload a profile picture.",
    )

    date_created = models.DateTimeField(auto_now_add=True)

    is_agreed_to_terms_and_conditions = models.BooleanField(
        default=False,
        help_text="Indicates whether the user has agreed to the terms and conditions.",
    )

    # Make Django prompt for phone_number during createsuperuser
    REQUIRED_FIELDS = ["email", "phone_number"]

    # Hook in our manager
    objects = CustomUserManager()

    def __str__(self):
        return self.username

    def save(self, *args, **kwargs):
        """
        Rules:
        - Superusers and staff are always ADMIN and must not link to ChurchMember.
        - CHURCH_MEMBER must be linked to a ChurchMember.
        """
        if self.is_superuser or self.is_staff or self.user_type == "ADMIN":
            self.user_type = "ADMIN"
            self.church_member = None
        else:
            # Only enforce link for regular church members
            if self.user_type == "CHURCH_MEMBER" and not getattr(self, "church_member_id", None):
                raise ValueError("CHURCH_MEMBER users must be linked to a valid ChurchMember.")

        super().save(*args, **kwargs)


class LoginHistory(models.Model):
    """
    Stores login history information and last visited path.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="login_history",
    )
    login_time = models.DateTimeField(default=timezone.now)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    last_visited_path = models.CharField(max_length=255, null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} logged in at {self.login_time}"
