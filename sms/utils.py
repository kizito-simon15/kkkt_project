# sms/utils.py  — NextSMS (safe: no crashes if creds missing)
import base64
import logging
import requests
from django.conf import settings
from django.utils.timezone import now

# Optional DB logging of outbound SMS
try:
    from sms.models import SentSMS
except Exception:
    SentSMS = None

# ---- Config from settings.py / env ----
NEXTSMS_USERNAME    = getattr(settings, "NEXTSMS_USERNAME", "")
NEXTSMS_PASSWORD    = getattr(settings, "NEXTSMS_PASSWORD", "")
NEXTSMS_SENDER_ID   = getattr(settings, "NEXTSMS_SENDER_ID", "KIZITA SOFT")
NEXTSMS_BASE_URL    = getattr(settings, "NEXTSMS_BASE_URL", "https://messaging-service.co.tz")
NEXTSMS_VERIFY_SSL  = getattr(settings, "NEXTSMS_VERIFY_SSL", True)

NEXTSMS_SEND_URL = f"{NEXTSMS_BASE_URL.rstrip('/')}/api/sms/v1/text/single"

def _creds_ok() -> bool:
    return bool(NEXTSMS_USERNAME and NEXTSMS_PASSWORD)

def _auth_header():
    """Build Basic Auth header for NextSMS."""
    if not _creds_ok():
        # NOTE: do not call this without checking _creds_ok() first.
        raise RuntimeError("NEXTSMS credentials missing. Set NEXTSMS_USERNAME and NEXTSMS_PASSWORD.")
    token = base64.b64encode(f"{NEXTSMS_USERNAME}:{NEXTSMS_PASSWORD}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def send_sms(to: str, message: str, member=None, reference: str = ""):
    """
    Send an SMS via NextSMS.
    SAFE: If credentials are missing, this returns a 'skipped' result instead of raising.
    """
    if not _creds_ok():
        logging.warning("NextSMS credentials missing; skipping SMS send.")
        return {"success": False, "skipped": True, "reason": "missing_credentials"}

    headers = _auth_header()
    payload = {
        "from": NEXTSMS_SENDER_ID,
        "to": str(to),
        "text": message,
        "reference": reference or "church-app",
    }

    try:
        resp = requests.post(NEXTSMS_SEND_URL, json=payload, headers=headers, timeout=30, verify=NEXTSMS_VERIFY_SSL)
    except requests.RequestException as e:
        logging.error("NextSMS send_sms network error: %s", e)
        return {"success": False, "error": str(e)}

    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}

    if resp.ok:
        # Typical NextSMS shape: {"messages":[{"messageId":"...","status":{"name":"PENDING",...}}]}
        msg0 = (data.get("messages") or [{}])[0]
        message_id = msg0.get("messageId") or msg0.get("id") or ""
        status_obj = msg0.get("status")
        if isinstance(status_obj, dict):
            status_name = status_obj.get("name") or status_obj.get("groupName") or "SENT"
        else:
            status_name = status_obj or "SENT"

        if SentSMS:
            try:
                SentSMS.objects.create(
                    recipient=member if member else None,
                    phone_number=str(to),
                    message=message,
                    request_id=str(message_id),
                    status=status_name,
                    sent_at=now(),
                )
            except Exception as e:
                logging.warning("Could not persist SentSMS record: %s", e)

        return {"success": True, "api_response": data, "request_id": message_id}

    return {"success": False, "status_code": resp.status_code, "api_response": data}

def check_sms_balance():
    """
    Best-effort balance check. Returns 'N/A' if creds are missing or endpoint unsupported.
    """
    if not _creds_ok():
        return "N/A"
    headers = _auth_header()
    url = f"{NEXTSMS_BASE_URL.rstrip('/')}/api/account/balance"
    try:
        resp = requests.get(url, headers=headers, timeout=30, verify=NEXTSMS_VERIFY_SSL)
        if resp.ok:
            j = resp.json()
            return j.get("balance") or j.get("credit") or j
        return {"error": f"API {resp.status_code}", "body": resp.text}
    except requests.RequestException as e:
        return {"error": str(e)}

def check_sms_status(*, message_id: str = "", to: str = ""):
    """
    Placeholder for NextSMS status polling (varies by account). Kept non-fatal.
    """
    return {"success": True, "status": "UNKNOWN"}
