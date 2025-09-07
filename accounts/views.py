from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib import messages
from django.urls import reverse, NoReverseMatch
from django.http import JsonResponse, Http404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.utils.timezone import now
from datetime import datetime, timezone

from .forms import (
    LoginForm, AdminUpdateForm, AccountRequestForm, ForgotPasswordForm
)
from .models import LoginHistory, CustomUser
from .utils import authenticate_with_username_or_email, get_client_ip
from accounts.decorators import church_member_required  # custom decorator
from leaders.models import Leader
from members.models import ChurchMember
from settings.models import Year

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def is_admin_or_superuser(user):
    return user.is_authenticated and (user.is_superuser or user.user_type == "ADMIN")


def get_ignored_paths():
    """
    Paths that must NEVER be used for post-login redirection.
    Wrap reverse() calls to avoid NoReverseMatch if a route is missing.
    """
    paths = {"/"}  # root is your login page now

    def _try(name):
        try:
            paths.add(reverse(name))
        except NoReverseMatch:
            pass

    for name in ("login", "request_account", "forgot_password", "public_news_list", "root_login"):
        _try(name)

    # Also ignore common prefixes that should never be targets
    paths.update({"/accounts/login/", "/accounts/"})
    return paths

def _safe_last_path(request, last_path: str | None) -> str | None:
    """
    Return a safe last_path or None to avoid loops or unsafe redirects.
    """
    if not last_path:
        return None
    if last_path in get_ignored_paths():
        return None
    if last_path == request.path:
        return None
    return last_path


def handle_user_redirection(user):
    """
    Redirect user to the appropriate dashboard based on role.
    """
    if user.is_superuser or user.user_type == "ADMIN":
        return redirect("admin_dashboard")

    if user.user_type == "CHURCH_MEMBER":
        # If the member is also a leader, route by occupation
        if hasattr(user, "church_member") and hasattr(user.church_member, "leader"):
            leader = user.church_member.leader
            if leader.occupation == "Senior Pastor":
                return redirect("pastor_dashboard")
            elif leader.occupation == "Evangelist":
                return redirect("evangelist_dashboard")
            elif leader.occupation == "Parish Council Secretary":
                return redirect("secretary_dashboard")
            elif leader.occupation == "Parish Treasurer":
                return redirect("accountant_dashboard")
        return redirect("member_dashboard")

    return redirect("login")


def time_since(dt):
    """Humanized time since a datetime in UTC."""
    now_utc = datetime.now(timezone.utc)
    delta = now_utc - dt
    if delta.days > 365:
        years = delta.days // 365
        return f"{years} year{'s' if years > 1 else ''} ago"
    if delta.days > 30:
        months = delta.days // 30
        return f"{months} month{'s' if months > 1 else ''} ago"
    if delta.days > 0:
        return f"{delta.days} day{'s' if delta.days > 1 else ''} ago"
    if delta.seconds > 3600:
        hours = delta.seconds // 3600
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    if delta.seconds > 60:
        minutes = delta.seconds // 60
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    return "Just now"


# -------------------------------------------------
# Auth Views
# -------------------------------------------------

def login_view(request):
    # If already logged in, go to a safe remembered page or role dashboard
    if request.user.is_authenticated:
        last_path = _safe_last_path(request, request.session.get("last_visited_path"))
        if last_path:
            return redirect(last_path)
        return handle_user_redirection(request.user)

    form = LoginForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        username_or_email = form.cleaned_data["username"]
        password = form.cleaned_data["password"]

        user = authenticate_with_username_or_email(username_or_email, password)
        if user is not None:
            # Only allow active CHURCH_MEMBER (unless superuser/admin)
            if not user.is_superuser and user.user_type == "CHURCH_MEMBER":
                if not user.church_member or getattr(user.church_member, "status", "Inactive") != "Active":
                    messages.error(request, "❌ Your account is inactive. Contact admin for assistance.")
                    return render(request, "accounts/login.html", {"form": form})

            login(request, user)

            LoginHistory.objects.create(
                user=user,
                ip_address=get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )

            # Use safe last path and clear it so it cannot loop later
            last_path = _safe_last_path(request, request.session.pop("last_visited_path", None))
            if last_path:
                return redirect(last_path)

            return handle_user_redirection(user)

        messages.error(request, "❌ Invalid username/email or password.")

    # Ensure any stale last_visited_path doesn't cause loops next time
    request.session.pop("last_visited_path", None)
    return render(request, "accounts/login.html", {"form": form})



@login_required
def logout_view(request):
    """
    Logs the user out, clears session & login history, then goes to login.
    """
    user = request.user
    LoginHistory.objects.filter(user=user).delete()
    logout(request)
    request.session.flush()
    return redirect("login")


# -------------------------------------------------
# Dashboards
# -------------------------------------------------
@login_required(login_url="login")
@user_passes_test(is_admin_or_superuser, login_url="login")
def admin_dashboard(request):
    """
    Admin dashboard.
    Also ensures the DB has a Year for the current system year marked as current.
    """
    current_system_year = now().year

    current_year_entry = Year.objects.filter(is_current=True).first()
    if current_year_entry and current_year_entry.year != current_system_year:
        current_year_entry.is_current = False
        current_year_entry.save()

        new_year_entry, _ = Year.objects.get_or_create(year=current_system_year)
        new_year_entry.is_current = True
        new_year_entry.save()
    elif not current_year_entry:
        Year.objects.create(year=current_system_year, is_current=True)

    # Lazy import to avoid heavy utils loading on module import
    from .utils import (
        get_general_finance_analysis,
        get_general_sacraments_analysis,
        get_general_properties_analysis,
        get_account_completion_analysis,
        get_leaders_distribution_analysis,
        get_members_distribution_analysis,
        get_general_data_analysis,
    )

    context = {
        "general_finance_data": get_general_finance_analysis(),
        "general_sacraments_data": get_general_sacraments_analysis(),
        "general_properties_data": get_general_properties_analysis(),
        "account_completion_data": get_account_completion_analysis(request.user),
        "leaders_distribution_data": get_leaders_distribution_analysis(),
        "members_distribution_data": get_members_distribution_analysis(),
        "general_data_analysis": get_general_data_analysis(),
    }
    return render(request, "accounts/admin_dashboard.html", context)


@login_required(login_url="login")
def secretary_dashboard(request):
    current_system_year = now().year
    current_year_entry = Year.objects.filter(is_current=True).first()
    if current_year_entry and current_year_entry.year != current_system_year:
        current_year_entry.is_current = False
        current_year_entry.save()
        new_year_entry, _ = Year.objects.get_or_create(year=current_system_year)
        new_year_entry.is_current = True
        new_year_entry.save()
    elif not current_year_entry:
        Year.objects.create(year=current_system_year, is_current=True)

    from .utils import (
        get_general_sacraments_analysis,
        get_general_properties_analysis,
        get_leaders_distribution_analysis,
        get_members_distribution_analysis,
    )

    context = {
        "general_sacraments_data": get_general_sacraments_analysis(),
        "general_properties_data": get_general_properties_analysis(),
        "leaders_distribution_data": get_leaders_distribution_analysis(),
        "members_distribution_data": get_members_distribution_analysis(),
    }
    return render(request, "accounts/secretary_dashboard.html", context)


@login_required(login_url="login")
def accountant_dashboard(request):
    current_system_year = now().year
    current_year_entry = Year.objects.filter(is_current=True).first()
    if current_year_entry and current_year_entry.year != current_system_year:
        current_year_entry.is_current = False
        current_year_entry.save()
        new_year_entry, _ = Year.objects.get_or_create(year=current_system_year)
        new_year_entry.is_current = True
        new_year_entry.save()
    elif not current_year_entry:
        Year.objects.create(year=current_system_year, is_current=True)

    from .utils import (
        get_general_finance_analysis,
        get_general_sacraments_analysis,
        get_general_properties_analysis,
        get_account_completion_analysis,
        get_leaders_distribution_analysis,
        get_members_distribution_analysis,
        get_general_data_analysis,
    )

    context = {
        "general_finance_data": get_general_finance_analysis(),
        "general_sacraments_data": get_general_sacraments_analysis(),
        "general_properties_data": get_general_properties_analysis(),
        "account_completion_data": get_account_completion_analysis(request.user),
        "leaders_distribution_data": get_leaders_distribution_analysis(),
        "members_distribution_data": get_members_distribution_analysis(),
        "general_data_analysis": get_general_data_analysis(),
    }
    return render(request, "accounts/accountant_dashboard.html", context)


@login_required(login_url="login")
def pastor_dashboard(request):
    current_system_year = now().year
    current_year_entry = Year.objects.filter(is_current=True).first()
    if current_year_entry and current_year_entry.year != current_system_year:
        current_year_entry.is_current = False
        current_year_entry.save()
        new_year_entry, _ = Year.objects.get_or_create(year=current_system_year)
        new_year_entry.is_current = True
        new_year_entry.save()
    elif not current_year_entry:
        Year.objects.create(year=current_system_year, is_current=True)

    from .utils import (
        get_general_finance_analysis,
        get_general_sacraments_analysis,
        get_general_properties_analysis,
        get_account_completion_analysis,
        get_leaders_distribution_analysis,
        get_members_distribution_analysis,
        get_general_data_analysis,
    )

    context = {
        "general_finance_data": get_general_finance_analysis(),
        "general_sacraments_data": get_general_sacraments_analysis(),
        "general_properties_data": get_general_properties_analysis(),
        "account_completion_data": get_account_completion_analysis(request.user),
        "leaders_distribution_data": get_leaders_distribution_analysis(),
        "members_distribution_data": get_members_distribution_analysis(),
        "general_data_analysis": get_general_data_analysis(),
    }
    return render(request, "accounts/pastor_dashboard.html", context)


@login_required(login_url="login")
def evangelist_dashboard(request):
    current_system_year = now().year
    current_year_entry = Year.objects.filter(is_current=True).first()
    if current_year_entry and current_year_entry.year != current_system_year:
        current_year_entry.is_current = False
        current_year_entry.save()
        new_year_entry, _ = Year.objects.get_or_create(year=current_system_year)
        new_year_entry.is_current = True
        new_year_entry.save()
    elif not current_year_entry:
        Year.objects.create(year=current_system_year, is_current=True)

    from .utils import (
        get_general_finance_analysis,
        get_general_sacraments_analysis,
        get_general_properties_analysis,
        get_account_completion_analysis,
        get_leaders_distribution_analysis,
        get_members_distribution_analysis,
        get_general_data_analysis,
    )

    context = {
        "general_finance_data": get_general_finance_analysis(),
        "general_sacraments_data": get_general_sacraments_analysis(),
        "general_properties_data": get_general_properties_analysis(),
        "account_completion_data": get_account_completion_analysis(request.user),
        "leaders_distribution_data": get_leaders_distribution_analysis(),
        "members_distribution_data": get_members_distribution_analysis(),
        "general_data_analysis": get_general_data_analysis(),
    }
    return render(request, "accounts/evangelist_dashboard.html", context)


@login_required
def member_dashboard(request):
    return render(request, "accounts/member_dashboard.html")


# -------------------------------------------------
# Profile Picture Upload/Remove
# -------------------------------------------------
@login_required(login_url="login")
@user_passes_test(is_admin_or_superuser, login_url="login")
def upload_profile_picture(request):
    if request.method == "POST":
        uploaded_file = request.FILES.get("cameraInput") or request.FILES.get("fileInput")
        if uploaded_file:
            request.user.profile_picture.save(uploaded_file.name, uploaded_file, save=True)
            messages.success(request, "✅ Profile picture uploaded successfully!")
            return redirect("admin_dashboard")
        messages.error(request, "❌ No file uploaded. Please select a file and try again.")
    return render(request, "accounts/upload_profile_picture.html")


@login_required
def pastor_upload_profile_picture(request):
    if request.method == "POST":
        uploaded_file = request.FILES.get("cameraInput") or request.FILES.get("fileInput")
        if uploaded_file:
            request.user.profile_picture.save(uploaded_file.name, uploaded_file, save=True)
            messages.success(request, "✅ Profile picture uploaded successfully!")
            return redirect("pastor_dashboard")
        messages.error(request, "❌ No file uploaded. Please select a file and try again.")
    return render(request, "accounts/pastor_upload_profile_picture.html")


@login_required
def evangelist_upload_profile_picture(request):
    if request.method == "POST":
        uploaded_file = request.FILES.get("cameraInput") or request.FILES.get("fileInput")
        if uploaded_file:
            request.user.profile_picture.save(uploaded_file.name, uploaded_file, save=True)
            messages.success(request, "✅ Profile picture uploaded successfully!")
            return redirect("evangelist_dashboard")
        messages.error(request, "❌ No file uploaded. Please select a file and try again.")
    return render(request, "accounts/evangelist_upload_profile_picture.html")


@login_required
def secretary_upload_profile_picture(request):
    if request.method == "POST":
        uploaded_file = request.FILES.get("cameraInput") or request.FILES.get("fileInput")
        if uploaded_file:
            request.user.profile_picture.save(uploaded_file.name, uploaded_file, save=True)
            messages.success(request, "✅ Profile picture uploaded successfully!")
            return redirect("secretary_dashboard")
        messages.error(request, "❌ No file uploaded. Please select a file and try again.")
    return render(request, "accounts/secretary_upload_profile_picture.html")


@login_required
def accountant_upload_profile_picture(request):
    if request.method == "POST":
        uploaded_file = request.FILES.get("cameraInput") or request.FILES.get("fileInput")
        if uploaded_file:
            request.user.profile_picture.save(uploaded_file.name, uploaded_file, save=True)
            messages.success(request, "✅ Profile picture uploaded successfully!")
            return redirect("accountant_dashboard")
        messages.error(request, "❌ No file uploaded. Please select a file and try again.")
    return render(request, "accounts/accountant_upload_profile_picture.html")


@login_required
@church_member_required
def member_upload_profile_picture(request):
    if request.method == "POST":
        uploaded_file = request.FILES.get("cameraInput") or request.FILES.get("fileInput")
        if uploaded_file:
            request.user.profile_picture.save(uploaded_file.name, uploaded_file, save=True)
            messages.success(request, "📸 Profile picture uploaded successfully!")
            return redirect("member_dashboard")
        messages.error(request, "⚠️ No file uploaded. Please select a file and try again.")
    return render(request, "accounts/member_upload_profile_picture.html")


@login_required
@church_member_required
def member_remove_profile_picture(request):
    if request.method == "POST":
        user = request.user
        if user.profile_picture:
            user.profile_picture.delete()
            user.profile_picture = None
            user.save()
            return JsonResponse({"success": True})
        return JsonResponse({"error": "⚠️ No profile picture found."}, status=400)
    return JsonResponse({"error": "⚠️ Invalid request method."}, status=400)


@login_required
def remove_profile_picture(request):
    if request.method == "POST":
        user = request.user
        if user.profile_picture:
            user.profile_picture.delete()
            user.profile_picture = None
            user.save()
            return JsonResponse({"success": True})
        return JsonResponse({"error": "No profile picture found"}, status=400)
    return JsonResponse({"error": "Invalid request"}, status=400)


# -------------------------------------------------
# Superuser/Admin Profile & Update
# -------------------------------------------------
@login_required
@user_passes_test(is_admin_or_superuser)
def superuser_detail_view(request):
    user = request.user
    profile_picture_url = user.profile_picture.url if user.profile_picture else None
    superuser_details = [
        ("📛 Username", user.username),
        ("📧 Email", user.email if user.email else "N/A"),
        ("📞 Phone Number", user.phone_number),
        ("🛠 User Type", dict(user.USER_TYPES).get(user.user_type, "Unknown Role")),
        ("📅 Date Created", user.date_created),
    ]
    return render(request, "accounts/superuser_detail.html", {
        "user": user,
        "profile_picture_url": profile_picture_url,
        "superuser_details": superuser_details,
    })


@login_required
@user_passes_test(is_admin_or_superuser)
def admin_update_view(request):
    user = request.user
    if request.method == "POST":
        form = AdminUpdateForm(request.POST, request.FILES, instance=user)
        if form.is_valid():
            user = form.save()
            if form.cleaned_data.get("password"):
                update_session_auth_hash(request, user)
            messages.success(request, "Profile updated successfully!")
            return redirect("superuser_detail")
        messages.error(request, "Error updating profile. Please check the form.")
    else:
        form = AdminUpdateForm(instance=user)
    return render(request, "accounts/admin_update.html", {"form": form})


# -------------------------------------------------
# Account Request / Forgot Password (Member)
# -------------------------------------------------
def request_account(request):
    """
    Account requests for Church Members.
    Two-step flow: validate member_id -> submit full form.
    """
    form = AccountRequestForm()
    member_id_valid = False
    display_message = None
    church_member = None

    if request.method == "POST":
        if "validate_id" in request.POST:
            member_id = request.POST.get("member_id", "").strip()
            try:
                church_member = ChurchMember.objects.get(member_id=member_id)
                leader = Leader.objects.filter(church_member=church_member).first()
                member_id_valid = True
                display_message = f"✅ Well done, we identify you as {church_member.full_name}."
                if leader:
                    display_message = f"✅ You are a leader: {leader.occupation} ({church_member.full_name})."

                form = AccountRequestForm(initial={
                    "member_id": member_id,
                    "email": church_member.email or "",
                })
            except ChurchMember.DoesNotExist:
                messages.error(request, "❌ The system does not identify you. Please contact the admin at +255767972343.")
                return render(request, "accounts/request_account.html", {"form": form})

        elif "submit_account" in request.POST:
            form = AccountRequestForm(request.POST)
            if form.is_valid():
                church_member = form.cleaned_data["member_id"]
                email = form.cleaned_data["email"]
                username = form.cleaned_data["username"]
                password = form.cleaned_data["password"]

                # CHURCH_MEMBER user
                user = CustomUser.objects.create_user(
                    username=username,
                    email=email,
                    phone_number=church_member.phone_number,
                    user_type="CHURCH_MEMBER",
                    church_member=church_member,
                )
                user.set_password(password)
                user.save()

                messages.success(request, f"🎉 Account successfully created for {church_member.full_name}. You can now log in.")
                return redirect("login")
            else:
                messages.error(request, "❌ Please fix the errors below.")

    return render(request, "accounts/request_account.html", {
        "form": form,
        "member_id_valid": member_id_valid,
        "display_message": display_message,
    })


def forgot_password(request):
    """
    Password reset for Church Members.
    """
    form = ForgotPasswordForm()
    member_id_valid = False
    display_message = None
    church_member = None

    if request.method == "POST":
        if "validate_id" in request.POST:
            member_id = request.POST.get("member_id", "").strip()
            try:
                church_member = ChurchMember.objects.get(member_id=member_id)
                user = CustomUser.objects.filter(church_member=church_member).first()

                if user:
                    member_id_valid = True
                    previous_username = user.username
                    previous_password = "(Hidden for security reasons)"  # passwords are hashed
                    display_message = (
                        f"✅ Well done! We identified you as {church_member.full_name}. "
                        f"🔑 Previous Username: {previous_username} "
                        f"🔒 Previous Password: {previous_password} "
                        f"Fill the form below to set new credentials."
                    )
                    form = ForgotPasswordForm(initial={"member_id": member_id})
                else:
                    messages.error(request, "❌ This member does not have an account. Please request an account first.")
                    return render(request, "accounts/forgot_password.html", {"form": form})

            except ChurchMember.DoesNotExist:
                messages.error(request, "❌ The system does not recognize this ID. Please contact admin at +255767972343.")
                return render(request, "accounts/forgot_password.html", {"form": form})

        elif "submit_reset" in request.POST:
            form = ForgotPasswordForm(request.POST)
            if form.is_valid():
                church_member = form.cleaned_data["member_id"]
                new_username = form.cleaned_data["new_username"]
                new_password = form.cleaned_data["new_password"]

                user = CustomUser.objects.get(church_member=church_member)
                user.username = new_username
                user.set_password(new_password)
                user.save()

                messages.success(request, f"🎉 Password reset successfully for {church_member.full_name}. You can now log in.")
                return redirect("login")
            else:
                messages.error(request, "❌ Please fix the errors below.")

    return render(request, "accounts/forgot_password.html", {
        "form": form,
        "member_id_valid": member_id_valid,
        "display_message": display_message,
        "church_member": church_member,
    })


