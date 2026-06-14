"""
utils/url_safety.py — حماية من SSRF على كل الـ URLs اللي السيرفر بيطلبها بنفسه

سيناريوهات الهجوم اللي بنقفلها:
1. Webhook مسجّل على http://169.254.169.254/... (cloud metadata) أو
   http://localhost:6379 (Redis الداخلي) → السيرفر يسرّب بيانات البنية التحتية.
2. Zoom webhook مزوّر بـ download_url يشاور على خدمة داخلية → السيرفر يحمّلها.

القواعد:
- https فقط (لا http، لا ftp، لا file://)
- الـ hostname لازم يتحلّ لـ IP عام — نرفض private / loopback / link-local /
  reserved / multicast (يشمل IPv6: ::1, fc00::/7, fe80::/10...)
- منفصلة عن منطق الـ business عشان تتعاد في أي fetch صادر جديد.
"""
import ipaddress
import socket
from urllib.parse import urlparse

# نطاقات Zoom الرسمية للتحميل — أي download_url خارجها مرفوض
ZOOM_DOWNLOAD_HOST_SUFFIXES = (".zoom.us", ".zoomgov.com")


def is_public_https_url(url: str) -> tuple[bool, str]:
    """
    يرجع (آمن؟, سبب الرفض).
    آمن = https + hostname يتحلّ كله لـ IPs عامة.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "URL غير قابل للتحليل"

    if parsed.scheme != "https":
        return False, "مسموح https فقط"
    if not parsed.hostname:
        return False, "لا يوجد hostname"

    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443,
                                   proto=socket.IPPROTO_TCP)
    except Exception:
        return False, f"تعذّر تحليل الـ DNS لـ {parsed.hostname}"

    if not infos:
        return False, "لا توجد عناوين IP للـ hostname"

    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, f"الـ hostname يتحلّ لعنوان داخلي/محجوز ({ip})"

    return True, ""


def is_safe_webhook_url(url: str) -> tuple[bool, str]:
    """تحقق كامل لـ webhook URL — يُستدعى عند التسجيل وقبل كل إرسال."""
    return is_public_https_url(url)


def is_zoom_download_url(url: str) -> tuple[bool, str]:
    """
    download_url لازم يكون على نطاق Zoom رسمي + يعدّي فحص الـ IP العام.
    ده بيمنع webhook مزوّر من تخلية السيرفر يحمّل من أي مكان.
    """
    ok, reason = is_public_https_url(url)
    if not ok:
        return False, reason

    host = (urlparse(url).hostname or "").lower()
    if not any(host == s.lstrip(".") or host.endswith(s)
               for s in ZOOM_DOWNLOAD_HOST_SUFFIXES):
        return False, f"الـ host ({host}) ليس نطاق Zoom رسمي"
    return True, ""
