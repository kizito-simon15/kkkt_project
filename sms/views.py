# sms/views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages as dj_messages

from sms.models import SentSMS
from sms.utils import check_sms_balance, check_sms_status  # ✅ NextSMS-safe imports


# ✅ Allow only Admins & Superusers (for admin pages)
def is_admin_or_superuser(user):
    return user.is_authenticated and (user.is_superuser or getattr(user, "user_type", "") == "ADMIN")


def _normalize_balance(value):
    """
    Render-friendly balance. Accepts string/number/dict from utils.
    """
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, dict):
        # Show something meaningful if we got an error dict
        err = value.get("error")
        if err:
            return f"Error: {err}"
        return value
    return "N/A"


def _extract_status(resp) -> str:
    """
    Extract a human-friendly delivery status from check_sms_status() response.

    Expected shapes:
      - {"success": True, "status": "DELIVERED"}
      - {"success": False, "status_code": 400, "error": "..."}
      - {"success": True, "api_response": {"messages":[{"status":{"name":"DELIVERED"}}]}}
    Fallback: "UNKNOWN"
    """
    # Simple/primary path from our utils
    if isinstance(resp, dict):
        status = resp.get("status")
        if isinstance(status, str) and status:
            return status

        # Try to mine deeper if a raw API response is included
        data = resp.get("api_response")
        if isinstance(data, dict):
            msgs = None
            for key in ("messages", "results", "reports", "data", "items"):
                val = data.get(key)
                if isinstance(val, list) and val:
                    msgs = val
                    break
                if isinstance(val, dict):
                    msgs = [val]
                    break
            if msgs:
                m0 = msgs[0]
                if isinstance(m0, dict):
                    st = m0.get("status")
                    if isinstance(st, dict):
                        return st.get("name") or st.get("groupName") or "UNKNOWN"
                    if st:
                        return str(st)
                    # Other possible vendor fields
                    for f in ("name", "messageStatus", "deliveryStatus"):
                        if m0.get(f):
                            return str(m0[f])

        # If util flagged error explicitly
        if resp.get("success") is False:
            code = resp.get("status_code")
            return f"ERROR:{code}" if code else "ERROR"

    return "UNKNOWN"


def _collect_rows_and_update_status():
    """
    Read all SentSMS, fetch status via NextSMS (best-effort), persist normalized status.
    Returns a list of dicts for templates.
    """
    rows = []
    for sms in SentSMS.objects.all().order_by("-sent_at"):
        phone = sms.phone_number or ""
        req_id = sms.request_id or ""

        if req_id:
            status_resp = check_sms_status(message_id=req_id, to=phone)
            status_str = _extract_status(status_resp)
        else:
            status_str = "UNKNOWN"

        if status_str and status_str != sms.status:
            sms.status = status_str
            sms.save(update_fields=["status"])

        rows.append({
            "id": sms.id,
            "sent_at": sms.sent_at.strftime("%Y-%m-%d %H:%M:%S") if sms.sent_at else "",
            "name": getattr(sms.recipient, "full_name", None) or getattr(sms.recipient, "name", None) or "-",
            "phone": sms.phone_number,
            "message": sms.message,
            "status": status_str,
        })
    return rows


# =========================
# Admin area
# =========================
@login_required
@user_passes_test(is_admin_or_superuser, login_url="login")
def sms_status_view(request):
    balance = _normalize_balance(check_sms_balance())   # "N/A" or number/string/error text
    rows = _collect_rows_and_update_status()
    context = {
        "balance": balance,
        "total_sent_sms": len(rows),
        "messages_info": rows,
    }
    return render(request, "sms/sms_status.html", context)


@login_required
@user_passes_test(is_admin_or_superuser, login_url="login")
def delete_sms(request, sms_id):
    sms = get_object_or_404(SentSMS, id=sms_id)
    if request.method == "POST":
        sms.delete()
        dj_messages.success(request, "📩 SMS deleted successfully!")
        return redirect("sms_status")
    return render(request, "sms/delete_sms_confirm.html", {"sms": sms})


@login_required
@user_passes_test(is_admin_or_superuser, login_url="login")
def delete_all_sms(request):
    if request.method == "POST":
        SentSMS.objects.all().delete()
        dj_messages.success(request, "📩 All SMS messages deleted successfully!")
        return redirect("sms_status")
    return render(request, "sms/delete_all_confirm.html")


# =========================
# Secretary area
# =========================
@login_required
def secretary_sms_status_view(request):
    balance = _normalize_balance(check_sms_balance())
    rows = _collect_rows_and_update_status()
    context = {
        "balance": balance,
        "total_sent_sms": len(rows),
        "messages_info": rows,
    }
    return render(request, "secretary/sms/sms_status.html", context)


@login_required
def secretary_delete_sms(request, sms_id):
    sms = get_object_or_404(SentSMS, id=sms_id)
    if request.method == "POST":
        sms.delete()
        dj_messages.success(request, "📩 SMS deleted successfully!")
        return redirect("secretary_sms_status")
    return render(request, "secretary/sms/delete_sms_confirm.html", {"sms": sms})


@login_required
def secretary_delete_all_sms(request):
    if request.method == "POST":
        SentSMS.objects.all().delete()
        dj_messages.success(request, "📩 All SMS messages deleted successfully!")
        return redirect("secretary_sms_status")
    return render(request, "secretary/sms/delete_all_confirm.html")
