# members/views.py
import logging
import random
import string
from datetime import date

import pytz
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.timezone import localtime, now

from .forms import (
    ChurchMemberForm,
    UpdateChurchMemberForm,
    ChurchMemberPassportForm,
)
from .models import ChurchMember
from leaders.forms import LeaderForm
from leaders.models import Leader
from sms.utils import send_sms  # NextSMS integration
from .utils import get_membership_distribution_analysis  # your analysis helper
from settings.models import Cell, OutStation

log = logging.getLogger(__name__)

# =========================
# Access control
# =========================
def is_admin_or_superuser(user):
    return user.is_authenticated and (user.is_superuser or getattr(user, "user_type", "") == "ADMIN")


# =========================
# Member-ID generator (exactly 5 chars, A–Z + 0–9, must contain at least one letter & one digit)
# =========================
def _generate_member_id(length: int = 5) -> str:
    """
    Returns a unique 5-character code containing at least:
      • 1 uppercase letter, and
      • 1 digit.
    Only A–Z and 0–9 used. Ensures uniqueness against ChurchMember.member_id.
    """
    if length < 2:
        length = 2  # need room for 1 letter + 1 digit

    letters = string.ascii_uppercase
    digits = string.digits
    pool = letters + digits

    while True:
        # guarantee mixture
        core = [random.choice(letters), random.choice(digits)]
        core += random.choices(pool, k=length - 2)
        random.shuffle(core)
        code = "".join(core)[:length]  # never exceed length

        # unique check
        if not ChurchMember.objects.filter(member_id=code).exists():
            return code


# =========================
# NextSMS safety wrappers
# =========================
def _sms_enabled() -> bool:
    """True if NEXTSMS credentials exist (avoid crashing in dev)."""
    return bool(getattr(settings, "NEXTSMS_USERNAME", "") and getattr(settings, "NEXTSMS_PASSWORD", ""))


def safe_send_sms(*, to: str, message: str, member=None, reference: str = "") -> dict:
    """
    Calls sms.utils.send_sms but never raises.
    Skips if NEXTSMS creds are missing; logs any error and returns a dict.
    """
    if not _sms_enabled():
        note = "NextSMS credentials missing; SMS skipped."
        log.warning(note)
        return {"success": False, "skipped": True, "reason": "missing_credentials", "note": note}

    try:
        resp = send_sms(to=to, message=message, member=member, reference=reference)
        if not resp.get("success"):
            log.warning("NextSMS send failed: %s", resp)
        return resp
    except Exception as e:
        log.exception("NextSMS send error")
        return {"success": False, "error": str(e)}


# =========================
# Create / Update Church Member
# =========================
@login_required
@user_passes_test(is_admin_or_superuser, login_url="login")
def create_or_update_church_member(request, member_id=None):
    """
    Create or update a church member.

    On create:
      • Ensure member_id exists and is 5-char alnum (A–Z/0–9) with a letter+digit mix.
      • Send welcome SMS via NextSMS (skips silently if creds missing).
      • If marked as a leader, redirect to create leader details; else to member list.

    On update:
      • Never change the member_id.
      • Redirect to the list.
    """
    # Fetch or init instance
    obj = None
    is_update = False
    if member_id:
        obj = get_object_or_404(ChurchMember, id=member_id)
        is_update = True

    if request.method == "POST":
        form = ChurchMemberForm(request.POST, request.FILES, instance=obj)
        if form.is_valid():
            try:
                member = form.save(commit=False)

                # Assign member_id on create only
                if not is_update and not member.member_id:
                    member.member_id = _generate_member_id(5)

                member.save()
                form.save_m2m()

                if not is_update:
                    # Prepare message + link (use absolute URI for current host)
                    request_account_url = request.build_absolute_uri("/accounts/request-account/")
                    sms_message = (
                        f"Habari {member.full_name}, karibu KKKT Mkwawa! "
                        f"Unaweza kuomba akaunti kwa kutumia utambulisho wako (USIMPE YEYOTE): {member.member_id}. "
                        f"Tembelea: {request_account_url}"
                    )
                    resp = safe_send_sms(
                        to=member.phone_number or "",
                        message=sms_message,
                        member=member,
                        reference=f"member-create-{member.id}",
                    )
                    if resp.get("skipped"):
                        messages.warning(request, "Member saved, but SMS not sent (NextSMS credentials missing).")
                    elif not resp.get("success"):
                        messages.warning(request, "Member saved, but SMS sending failed.")
                    else:
                        messages.success(request, "✅ Member saved and SMS notification sent.")

                    # If marked as leader, continue to leader details
                    if getattr(member, "is_this_church_member_a_leader", False):
                        return redirect("create_leader_from_member", member_id=member.id)
                else:
                    messages.success(request, "✅ Church member updated successfully!")

                return redirect("church_member_list")

            except ValidationError as e:
                for msg in e.messages:
                    messages.error(request, f"❌ {msg}")
        else:
            messages.error(request, "❌ Failed to save church member. Please correct the errors below.")
    else:
        form = ChurchMemberForm(instance=obj)

    return render(request, "members/church_member_form.html", {"form": form, "is_update": is_update})


# =========================
# Approve Member (Pending -> Active) + SMS
# =========================
@login_required
@user_passes_test(is_admin_or_superuser, login_url="login")
def approve_church_member(request, member_id):
    """
    Approves a pending church member, changes their status to 'Active', and sends an SMS.
    """
    member = get_object_or_404(ChurchMember, id=member_id, status="Pending")

    if request.method == "POST":
        member.status = "Active"
        # Ensure member_id exists
        if not member.member_id:
            member.member_id = _generate_member_id(5)
        member.save()

        request_account_url = request.build_absolute_uri("/accounts/request-account/")
        sms_message = (
            "Hongera! Umeidhinishwa kuwa mwanachama wa KKKT Mkwawa. "
            f"Tumia ID yako {member.member_id} kuomba akaunti hapa: {request_account_url}"
        )
        resp = safe_send_sms(
            to=member.phone_number or "",
            message=sms_message,
            member=member,
            reference=f"member-approve-{member.id}",
        )
        if resp.get("skipped"):
            messages.warning(request, "Approved, but SMS not sent (NextSMS credentials missing).")
        elif not resp.get("success"):
            messages.warning(request, "Approved, but SMS sending failed.")
        else:
            messages.success(request, f"✅ {member.full_name} approved and notified via SMS!")

        return redirect("church_member_list")

    return render(request, "members/approve_church_member.html", {"member": member})


# =========================
# Create Leader from Member
# =========================
@login_required
@user_passes_test(is_admin_or_superuser, login_url="login")
def create_leader_from_member(request, member_id):
    """
    Create leader details for a member. Prevent duplicates.
    """
    church_member = get_object_or_404(ChurchMember, id=member_id)

    if Leader.objects.filter(church_member=church_member).exists():
        messages.error(request, "❌ This church member already has leader details.")
        return redirect("church_member_list")

    if request.method == "POST":
        form = LeaderForm(request.POST)
        if form.is_valid():
            leader = form.save(commit=False)
            leader.church_member = church_member
            leader.save()
            messages.success(request, f"✅ Leader details for {church_member.full_name} saved successfully!")
            return redirect("church_member_list")
        messages.error(request, "❌ Failed to save leader details. Please correct the errors below.")
    else:
        form = LeaderForm(initial={"church_member": church_member})

    return render(request, "members/leader_from_member_form.html", {"form": form, "church_member": church_member})


# =========================
# List / filtering helpers
# =========================
TZ_TZ = pytz.timezone("Africa/Dar_es_Salaam")

def format_time_since(created_date):
    if not created_date:
        return "N/A"
    created_date = localtime(created_date, timezone=TZ_TZ)
    current_time = localtime(now(), timezone=TZ_TZ)
    delta = current_time - created_date
    s = delta.total_seconds()
    if s < 60:
        return "Just now"
    if s < 3600:
        m = int(s // 60)
        return f"Since {m} minute{'s' if m > 1 else ''} ago"
    if s < 86400:
        h = int(s // 3600)
        return f"Since {h} hour{'s' if h > 1 else ''} ago"
    if s < 604800:
        d = int(s // 86400)
        return f"Since {d} day{'s' if d > 1 else ''} ago"
    if s < 2419200:
        w = int(s // 604800)
        return f"Since {w} week{'s' if w > 1 else ''} ago"
    if s < 29030400:
        mo = int(s // 2419200)
        return f"Since {mo} month{'s' if mo > 1 else ''} ago"
    y = int(s // 29030400)
    return f"Since {y} year{'s' if y > 1 else ''} ago"


@login_required
@user_passes_test(is_admin_or_superuser, login_url="login")
def church_member_list(request):
    name_query = request.GET.get("name", "").strip()
    gender_query = request.GET.get("gender", "").strip()
    cell_query = request.GET.get("cell", "").strip()
    outstation_query = request.GET.get("outstation", "").strip()

    members = (
        ChurchMember.objects.select_related("cell__outstation")
        .filter(status__in=["Active", "Pending"])
        .order_by("-status", "full_name")
    )

    if name_query:
        members = members.filter(Q(full_name__icontains=name_query) | Q(member_id__icontains=name_query))
    if gender_query:
        members = members.filter(gender=gender_query)
    if cell_query:
        members = members.filter(cell_id=cell_query)
    if outstation_query:
        members = members.filter(cell__outstation_id=outstation_query)

    for m in members:
        m.time_since_created = format_time_since(m.date_created)

    context = {
        "church_members": members,
        "total_members": members.count(),
        "total_males": members.filter(gender="Male").count(),
        "total_females": members.filter(gender="Female").count(),
        "cells": Cell.objects.all(),
        "outstations": OutStation.objects.all(),
        "name_query": name_query,
        "gender_query": gender_query,
        "cell_query": cell_query,
        "outstation_query": outstation_query,
    }
    return render(request, "members/church_member_list.html", context)


@login_required
@user_passes_test(is_admin_or_superuser, login_url="login")
def inactive_church_member_list(request):
    name_query = request.GET.get("name", "").strip()
    gender_query = request.GET.get("gender", "").strip()
    cell_query = request.GET.get("cell", "").strip()
    outstation_query = request.GET.get("outstation", "").strip()

    members = (
        ChurchMember.objects.select_related("cell__outstation")
        .filter(status="Inactive")
        .order_by("full_name")
    )

    if name_query:
        members = members.filter(Q(full_name__icontains=name_query) | Q(member_id__icontains=name_query))
    if gender_query:
        members = members.filter(gender=gender_query)
    if cell_query:
        members = members.filter(cell_id=cell_query)
    if outstation_query:
        members = members.filter(cell__outstation_id=outstation_query)

    for m in members:
        m.time_since_created = format_time_since(m.date_created)

    context = {
        "church_members": members,
        "total_members": members.count(),
        "total_males": members.filter(gender="Male").count(),
        "total_females": members.filter(gender="Female").count(),
        "cells": Cell.objects.all(),
        "outstations": OutStation.objects.all(),
        "name_query": name_query,
        "gender_query": gender_query,
        "cell_query": cell_query,
        "outstation_query": outstation_query,
    }
    return render(request, "members/inactive_church_member_list.html", context)


# =========================
# Members Home (summary + charts)
# =========================
@login_required(login_url="login")
@user_passes_test(is_admin_or_superuser, login_url="login")
def members_home(request):
    total_active_members = ChurchMember.objects.filter(status="Active").count()
    total_inactive_members = ChurchMember.objects.filter(status="Inactive").count()
    membership_distribution_data = get_membership_distribution_analysis()
    return render(
        request,
        "members/members_home.html",
        {
            "total_active_members": total_active_members,
            "total_inactive_members": total_inactive_members,
            "membership_distribution_data": membership_distribution_data,
        },
    )


# =========================
# Delete Member (confirm)
# =========================
@login_required
@user_passes_test(is_admin_or_superuser, login_url="login")
def delete_church_member(request, pk):
    church_member = get_object_or_404(ChurchMember, pk=pk)
    if request.method == "POST":
        name = church_member.full_name
        church_member.delete()
        messages.success(request, f"✅ Church member '{name}' deleted successfully!")
        return redirect("church_member_list")
    return render(request, "members/confirm_delete_church_member.html", {"church_member": church_member})


# =========================
# Update Member
# =========================
@login_required
@user_passes_test(is_admin_or_superuser, login_url="login")
def update_church_member(request, pk):
    church_member = get_object_or_404(ChurchMember, pk=pk)
    if request.method == "POST":
        form = UpdateChurchMemberForm(request.POST, request.FILES, instance=church_member)
        if form.is_valid():
            try:
                form.save()
                messages.success(request, "✅ Church member updated successfully!")
                return redirect("church_member_detail", pk=church_member.pk)
            except ValidationError as e:
                for msg in e.messages:
                    messages.error(request, msg)
        else:
            messages.error(request, "❌ Failed to update the church member. Please correct the errors below.")
    else:
        form = UpdateChurchMemberForm(instance=church_member)

    return render(request, "members/update_church_member.html", {"form": form, "church_member": church_member})


# =========================
# Upload Passport
# =========================
@login_required
@user_passes_test(is_admin_or_superuser, login_url="login")
def upload_passport(request, pk):
    member = get_object_or_404(ChurchMember, pk=pk)
    if request.method == "POST":
        form = ChurchMemberPassportForm(request.POST, request.FILES, instance=member)
        if form.is_valid():
            form.save()
            messages.success(request, "✅ Passport uploaded successfully!")
            return redirect("church_member_list")
        messages.error(request, "❌ Failed to upload passport. Please try again.")
    else:
        form = ChurchMemberPassportForm(instance=member)

    return render(request, "members/upload_passport.html", {"form": form, "member": member})


# =========================
# Member Detail
# =========================
def calculate_age(date_of_birth):
    if date_of_birth:
        today = date.today()
        return f"{today.year - date_of_birth.year - ((today.month, today.day) < (date_of_birth.month, date_of_birth.day))} years old"
    return "----"


def calculate_since_created(date_created):
    delta = now() - date_created
    if delta.days < 1:
        if delta.seconds < 60:
            return "Just now"
        if delta.seconds < 3600:
            return f"{delta.seconds // 60} minute(s) ago"
        return f"{delta.seconds // 3600} hour(s) ago"
    if delta.days == 1:
        return "1 day ago"
    if delta.days < 7:
        return f"{delta.days} day(s) ago"
    if delta.days < 30:
        return f"{delta.days // 7} week(s) ago"
    if delta.days < 365:
        return f"{delta.days // 30} month(s) ago"
    return f"{delta.days // 365} year(s) ago"


@login_required(login_url="/accounts/login/")
@user_passes_test(is_admin_or_superuser, login_url="/accounts/login/")
def church_member_detail(request, pk):
    church_member = get_object_or_404(ChurchMember, pk=pk)
    since_created = calculate_since_created(church_member.date_created)

    def yes_no(v): return "✅" if v else "❌"

    documents = {
        "📜 Baptism Certificate": church_member.baptism_certificate.url if church_member.baptism_certificate else None,
        "🕊️ Confirmation Certificate": church_member.confirmation_certificate.url if church_member.confirmation_certificate else None,
    }

    details = {
        "👤 Full Name": church_member.full_name,
        "🆔 Member ID": church_member.member_id,
        "🎂 Date of Birth": church_member.date_of_birth.strftime("%d %B, %Y") if church_member.date_of_birth else "----",
        "🔢 Age": calculate_age(church_member.date_of_birth),
        "⚥ Gender": church_member.gender,
        "📞 Phone Number": church_member.phone_number,
        "📧 Email": church_member.email or "----",
        "🏠 Address": church_member.address or "----",
        "🏘️ Cell": f"{church_member.cell.name} ({church_member.cell.outstation.name})" if church_member.cell else "----",
        "🔘 Status": {"Active": "✅ Active", "Pending": "⏳ Pending", "Inactive": "❌ Inactive"}.get(church_member.status, "❓ Unknown"),
        "📅 Date Created": f"{localtime(church_member.date_created).strftime('%d %B, %Y %I:%M %p')} ({since_created})",
        # Sacramental
        "🌊 Baptized": yes_no(church_member.is_baptised),
        "🗓️ Date of Baptism": church_member.date_of_baptism.strftime("%d %B, %Y") if church_member.date_of_baptism else "----",
        "🕊️ Confirmed": yes_no(bool(church_member.date_confirmed)),
        "🗓️ Date of Confirmation": church_member.date_confirmed.strftime("%d %B, %Y") if church_member.date_confirmed else "----",
        # Marriage
        "💍 Marital Status": church_member.marital_status or "----",
        "🗓️ Date of Marriage": church_member.date_of_marriage.strftime("%d %B, %Y") if church_member.date_of_marriage else "----",
        # Emergency
        "📛 Emergency Contact Name": church_member.emergency_contact_name or "----",
        "📞 Emergency Contact Phone": church_member.emergency_contact_phone or "----",
        # Files
        "📸 Passport": church_member.passport.url if church_member.passport else "----",
    }

    return render(
        request,
        "members/church_member_detail.html",
        {"church_member": church_member, "details": details, "documents": documents},
    )


# =========================
# Members Report
# =========================
@login_required
@user_passes_test(is_admin_or_superuser, login_url="/accounts/login/")
def church_members_report(request):
    total_members = ChurchMember.objects.count()
    total_active = ChurchMember.objects.filter(status="Active").count()
    total_inactive = ChurchMember.objects.filter(status="Inactive").count()

    active_male = ChurchMember.objects.filter(status="Active", gender="Male").count()
    active_female = ChurchMember.objects.filter(status="Active", gender="Female").count()

    inactive_male = ChurchMember.objects.filter(status="Inactive", gender="Male").count()
    inactive_female = ChurchMember.objects.filter(status="Inactive", gender="Female").count()

    total_outstations = OutStation.objects.count()
    total_cells = Cell.objects.count()

    cells = Cell.objects.select_related("outstation").all()
    cell_stats_list = []
    for cell in cells:
        all_members = cell.members.all()
        total_cell_members = all_members.count()

        active_cell = all_members.filter(status="Active").count()
        inactive_cell = all_members.filter(status="Inactive").count()

        active_male_cell = all_members.filter(status="Active", gender="Male").count()
        active_female_cell = all_members.filter(status="Active", gender="Female").count()

        inactive_male_cell = all_members.filter(status="Inactive", gender="Male").count()
        inactive_female_cell = all_members.filter(status="Inactive", gender="Female").count()

        active_baptized_cell = all_members.filter(status="Active", is_baptised=True).count()
        active_unbaptized_cell = all_members.filter(status="Active", is_baptised=False).count()
        active_confirmed_cell = all_members.filter(status="Active", date_confirmed__isnull=False).count()
        active_unconfirmed_cell = all_members.filter(status="Active", date_confirmed__isnull=True).count()

        married_males_cell = all_members.filter(status="Active", gender="Male", marital_status="Married").count()
        unmarried_males_cell = all_members.filter(
            status="Active", gender="Male", marital_status__in=["Single", "Divorced", "Widowed"]
        ).count()
        married_females_cell = all_members.filter(status="Active", gender="Female", marital_status="Married").count()
        unmarried_females_cell = all_members.filter(
            status="Active", gender="Female", marital_status__in=["Single", "Divorced", "Widowed"]
        ).count()

        cell_stats_list.append(
            {
                "cell": cell,
                "cell_display": f"{cell.name} ({cell.outstation.name})",
                "total_members": total_cell_members,
                "active_members": active_cell,
                "inactive_members": inactive_cell,
                "active_male": active_male_cell,
                "active_female": active_female_cell,
                "inactive_male": inactive_male_cell,
                "inactive_female": inactive_female_cell,
                "active_baptized": active_baptized_cell,
                "active_unbaptized": active_unbaptized_cell,
                "active_confirmed": active_confirmed_cell,
                "active_unconfirmed": active_unconfirmed_cell,
                "married_males": married_males_cell,
                "unmarried_males": unmarried_males_cell,
                "married_females": married_females_cell,
                "unmarried_females": unmarried_females_cell,
            }
        )

    largest_cell = max(cell_stats_list, key=lambda c: c["total_members"]) if cell_stats_list else None
    smallest_cell = min(cell_stats_list, key=lambda c: c["total_members"]) if cell_stats_list else None

    context = {
        "total_members": total_members,
        "total_active": total_active,
        "total_inactive": total_inactive,
        "active_male": active_male,
        "active_female": active_female,
        "inactive_male": inactive_male,
        "inactive_female": inactive_female,
        "total_outstations": total_outstations,
        "total_cells": total_cells,
        "cell_stats_list": cell_stats_list,
        "largest_cell": largest_cell,
        "smallest_cell": smallest_cell,
        "active_baptized": ChurchMember.objects.filter(status="Active", is_baptised=True).count(),
        "active_unbaptized": ChurchMember.objects.filter(status="Active", is_baptised=False).count(),
        "active_confirmed": ChurchMember.objects.filter(status="Active", date_confirmed__isnull=False).count(),
        "active_unconfirmed": ChurchMember.objects.filter(status="Active", date_confirmed__isnull=True).count(),
        "married_males": ChurchMember.objects.filter(status="Active", gender="Male", marital_status="Married").count(),
        "unmarried_males": ChurchMember.objects.filter(
            status="Active", gender="Male", marital_status__in=["Single", "Divorced", "Widowed"]
        ).count(),
        "married_females": ChurchMember.objects.filter(status="Active", gender="Female", marital_status="Married").count(),
        "unmarried_females": ChurchMember.objects.filter(
            status="Active", gender="Female", marital_status__in=["Single", "Divorced", "Widowed"]
        ).count(),
        "comments_explanations_advice": (
            "These statistics provide insights into membership trends, gender distribution, and sacramental participation "
            "across outstations and cells. Cells with lower membership may indicate areas needing outreach or support. "
            "Sacramental and marital data can guide pastoral planning and highlight opportunities for spiritual growth."
        ),
    }
    return render(request, "members/church_members_report.html", context)


# =========================
# Public signup (kept) + SMS
# =========================
def church_member_signup(request):
    from .forms import ChurchMemberSignupForm

    if request.method == "POST":
        form = ChurchMemberSignupForm(request.POST, request.FILES)
        if form.is_valid():
            member = form.save()

            # Ensure ID exists even for public signup
            if not member.member_id:
                member.member_id = _generate_member_id(5)
                member.save(update_fields=["member_id"])

            signup_message = (
                f"Hongera {member.full_name}! Umefanikiwa kujisajili KKKT Mkwawa. "
                "Hali yako kwa sasa ni *Pending*. Utapokea SMS ukishathibitishwa."
            )
            resp = safe_send_sms(
                to=member.phone_number or "",
                message=signup_message,
                member=member,
                reference=f"member-signup-{member.id}",
            )
            if resp.get("skipped"):
                messages.warning(request, "Usajili umefanikiwa, lakini SMS haikutumwa (missing NextSMS credentials).")
            elif not resp.get("success"):
                messages.warning(request, "Usajili umefanikiwa, lakini kutuma SMS kume-fail.")
            else:
                log.info("Signup SMS sent: %s", resp)

            messages.success(
                request,
                "Maombi yako ya usajili yamewasilishwa kikamilifu! Utapokea SMS yenye maelezo zaidi."
            )
            return redirect("signup_success")
        else:
            messages.error(request, "Tafadhali rekebisha makosa yaliyo hapa chini.")
    else:
        form = ChurchMemberSignupForm()

    return render(request, "members/signup.html", {"form": form})


def signup_success(request):
    return render(request, "members/signup_success.html")
