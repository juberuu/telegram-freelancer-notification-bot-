"""Read client states embedded in a public project page; no login or token."""
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .api import APIError
from .core import client_avatar, avatar_url, client_id

FIELDS = {
    "paymentVerified": "payment_verified", "identityVerified": "identity_verified",
    "emailVerified": "email_verified", "phoneVerified": "phone_verified",
    "profileComplete": "profile_complete", "depositMade": "deposit_made",
}


class PageState(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.inside = attrs.get("id") == "webapp-state" and attrs.get("type") == "application/json"

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def parse_client(page, project_id):
    parser = PageState()
    parser.feed(page)
    try:
        state = json.loads("".join(parser.parts))
    except (ValueError, RecursionError):
        return {}
    stack = [state]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            # Only the exact project's raw document, never bidder records or text labels.
            raw = item.get("rawDocument")
            if isinstance(raw, dict) and str(raw.get("projectId")) == str(project_id):
                client = raw.get("client") or {}
                if not isinstance(client, dict):
                    continue
                values = client.get("verification") or {}
                if not isinstance(values, dict):
                    values = {}
                result = {"verifications": {target: values[source] for source, target in FIELDS.items()
                                            if type(values.get(source)) is bool}}
                avatar = client_avatar(client)
                if avatar:
                    result["client_avatar_url"] = avatar
                owner_id = client_id(client.get("id")) or client_id(client.get("userId"))
                if owner_id:
                    result["client_id"] = owner_id
                address = client.get("address") or {}
                if isinstance(address, dict):
                    for source, target in (("country", "country"), ("countryCode", "country_code")):
                        if isinstance(address.get(source), str) and address[source].strip():
                            result[target] = address[source].strip()
                return result
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return {}


def fetch_client(project, timeout=15):
    url = project["url"]
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "www.freelancer.com" or not parsed.path.startswith("/projects/"):
        return {}
    req = urllib.request.Request(url, headers={"User-Agent": "PersonalProjectAlerts/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if urllib.parse.urlsplit(response.geturl()).netloc != "www.freelancer.com":
                return {}
            page = response.read(3 * 1024 * 1024 + 1)
        if len(page) > 3 * 1024 * 1024:
            return {}
        return parse_client(page.decode("utf-8", errors="replace"), project["id"])
    except urllib.error.HTTPError as e:
        try:
            retry = int(e.headers.get("Retry-After", 60))
        except (ValueError, TypeError):
            retry = 60
        raise APIError("Freelancer public page", e.code, retry) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError, OSError):
        raise APIError("Freelancer public page") from None


def enrich(project, details, prefer_new=False):
    current_id = client_id(project.get("client_id"))
    incoming_id = client_id(details.get("client_id"))
    if current_id and incoming_id and current_id != incoming_id:
        return dict(project)
    result = dict(project)
    known = {"payment_verified": project.get("verified"), **(project.get("verifications") or {})}
    known = {k: v for k, v in known.items() if type(v) is bool}
    incoming = {k: v for k, v in details.get("verifications", {}).items() if type(v) is bool}
    merged = {**known, **incoming} if prefer_new else {**incoming, **known}
    result["verifications"] = merged
    result["verified"] = merged.get("payment_verified")
    old_avatar, new_avatar = avatar_url(project.get("client_avatar_url")), avatar_url(details.get("client_avatar_url"))
    result["client_avatar_url"] = (new_avatar or old_avatar) if prefer_new else (old_avatar or new_avatar)
    result["client_id"] = client_id(project.get("client_id")) or client_id(details.get("client_id"))
    new_country = details.get("country")
    if (prefer_new or project.get("country") in (None, "", "Unknown")) and new_country and str(new_country).casefold() not in ("unknown", "?", "n/a"):
        result["country"] = details["country"]
        if prefer_new and not details.get("country_code") and new_country != project.get("country"):
            result["country_code"] = ""
    if (prefer_new or not project.get("country_code")) and details.get("country_code"):
        result["country_code"] = details["country_code"].upper()
    return result
