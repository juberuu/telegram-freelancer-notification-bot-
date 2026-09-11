import json
import logging
import os
import socket
import threading
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from .core import normalize, VERIFICATION_FIELDS, client_id, avatar_url as normalize_avatar_url


class APIError(Exception):
    def __init__(self, service, code=None, retry_after=None, reason=None):
        try:
            code = int(code) if code is not None else None
        except (ValueError, TypeError):
            code = None
        self.code = code
        default = 5 if code is None or code >= 500 else 30
        try:
            self.retry_after = max(1, int(retry_after if retry_after is not None else default))
        except (ValueError, TypeError):
            self.retry_after = default
        super().__init__(f"{service}: HTTP/API {code or reason or 'connection or invalid response'}; retry in {self.retry_after}s")


def request(url, service, payload=None, headers=None, timeout=35):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "PersonalProjectAlerts/1.0", "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        retry = None
        try:
            if e.headers.get("Retry-After"):
                retry = int(e.headers["Retry-After"])
            body = json.load(e)
            if isinstance(body, dict) and isinstance(body.get("parameters"), dict):
                retry = body["parameters"].get("retry_after", retry)
        except (ValueError, TypeError):
            pass
        raise APIError(service, e.code, retry) from None
    except (TimeoutError, socket.timeout):
        raise APIError(service, reason="request timed out") from None
    except ValueError:
        raise APIError(service, reason="invalid JSON response") from None
    except (urllib.error.URLError, OSError):
        # Never expose exception URLs: Telegram URLs contain the bot token.
        raise APIError(service, reason="connection failed") from None


class Telegram:
    def __init__(self, token, chat_id=None, store=None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = chat_id
        self.store = store
        self.cooldown_until = store.get("telegram_cooldown_until", 0) if store else 0
        self.rate_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.next_send = 0
        self.chat_next_send = {}
        self.chat_locks = {}
        self.rich_supported = True

    def call(self, method, payload=None):
        service = f"Telegram {method}"
        with self.rate_lock:
            remaining = self.cooldown_until - time.time()
        if remaining > 0:
            raise APIError(service, 429, math.ceil(remaining))
        try:
            response = request(self.base + method, service, payload or {})
            if not isinstance(response, dict) or not response.get("ok") or "result" not in response:
                response = response if isinstance(response, dict) else {}
                parameters = response.get("parameters")
                parameters = parameters if isinstance(parameters, dict) else {}
                raise APIError(service, response.get("error_code"), parameters.get("retry_after"), reason="invalid API response")
            return response["result"]
        except APIError as e:
            if e.code == 429:
                with self.rate_lock:
                    self.cooldown_until = max(self.cooldown_until, time.time() + e.retry_after + 1)
                    if self.store:
                        self.store.set("telegram_cooldown_until", self.cooldown_until)
            raise

    @staticmethod
    def preview_options(avatar_url=None):
        avatar = normalize_avatar_url(avatar_url)
        return ({"is_disabled": False, "url": avatar, "prefer_large_media": True, "show_above_text": True}
                if avatar else {"is_disabled": True})

    def send(self, text, markup=None, avatar_url=None, rich_message=None):
        if rich_message and self.rich_supported:
            try:
                result = self.paced_call("sendRichMessage", {"chat_id": self.chat_id,
                    "rich_message": rich_message, "reply_markup": markup or {"inline_keyboard": []}})
                return {**result, "_render_mode": "rich"}
            except APIError as e:
                # Definite rejections can safely fall back; never duplicate a timeout or 429.
                if e.code not in (400, 404):
                    raise
                if e.code == 404:
                    self.rich_supported = False
                logging.warning("%s; using the standard Telegram layout", e)
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": self.preview_options(avatar_url)}
        if markup:
            payload["reply_markup"] = markup
        return self.paced_call("sendMessage", payload)

    def edit(self, message_id, text, markup, avatar_url=None, rich_message=None):
        if rich_message and self.rich_supported:
            try:
                return self.paced_call("editMessageText", {"chat_id": self.chat_id,
                    "message_id": message_id, "rich_message": rich_message, "reply_markup": markup})
            except APIError as e:
                if e.code not in (400, 404):
                    raise
                logging.warning("%s; using the standard Telegram layout", e)
        return self.paced_call("editMessageText", {"chat_id": self.chat_id, "message_id": message_id,
            "text": text, "parse_mode": "HTML", "reply_markup": markup,
            "link_preview_options": self.preview_options(avatar_url)})

    def paced_call(self, method, payload):
        chat_id = payload.get("chat_id", self.chat_id)
        with self.send_lock:
            chat_lock = self.chat_locks.setdefault(chat_id, threading.Lock())
        # A slow request in one private chat must not block other recipients.
        with chat_lock:
            delay = self.chat_next_send.get(chat_id, 0) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            with self.send_lock:
                delay = self.next_send - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self.next_send = time.monotonic() + 0.1
            try:
                return self.call(method, payload)
            finally:
                self.chat_next_send[chat_id] = time.monotonic() + 1.5

    def for_chat(self, chat_id):
        return TelegramChat(self, chat_id)


class TelegramChat(Telegram):
    """A destination sharing the token, pacing and cooldown of one transport."""
    def __init__(self, parent, chat_id):
        self.parent, self.chat_id = parent, chat_id

    @property
    def rich_supported(self):
        return self.parent.rich_supported

    @rich_supported.setter
    def rich_supported(self, value):
        self.parent.rich_supported = value

    def call(self, method, payload=None):
        return self.parent.call(method, payload)

    def paced_call(self, method, payload):
        return self.parent.paced_call(method, payload)


class Freelancer:
    def __init__(self, token=None):
        self.token = token or os.getenv("FREELANCER_OAUTH_TOKEN", "")
        self.profile_cache = {}
        self.client_retry_until = 0

    def client_details(self, project, timeout=6):
        """Resolve the exact project owner; called outside the delivery/scan workers."""
        key = (project["id"], client_id(project.get("client_id")))
        cached = self.profile_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if time.monotonic() < self.client_retry_until:
            raise APIError("Freelancer client lookup cooldown", 429,
                           math.ceil(self.client_retry_until - time.monotonic()))
        headers = {"freelancer-oauth-v1": self.token} if self.token else {}

        def get(path):
            data = request("https://www.freelancer.com/api/" + path,
                           "Freelancer client lookup", headers=headers, timeout=timeout)
            if not isinstance(data, dict) or data.get("status") != "success" or not isinstance(data.get("result"), dict):
                raise APIError("Freelancer client lookup")
            return data["result"]

        try:
            details = {}
            owner_id = key[1]
            if not owner_id:
                result = get(f"projects/0.1/projects/{int(project['id'])}/?user_details=true&user_avatar=true&user_status=true")
                raw = result.get("project") or result
                if str(raw.get("id")) == str(project["id"]):
                    details = normalize(raw, result.get("users") or {})
                    owner_id = details.get("client_id")
            if owner_id and (not details.get("client_avatar_url") or any(
                    type(details.get("verifications", {}).get(field)) is not bool for field in VERIFICATION_FIELDS)):
                owner = get(f"users/0.1/users/{owner_id}/?avatar=true&status=true&country_details=true")
                if client_id(owner.get("id")) == owner_id:
                    details = normalize({"id": project["id"], "owner_id": owner_id}, {str(owner_id): owner})
            details = {k: v for k, v in details.items() if k in (
                "client_id", "client_avatar_url", "verifications", "country", "country_code")}
        except APIError as e:
            if e.code == 429:
                self.client_retry_until = time.monotonic() + e.retry_after
            raise
        if len(self.profile_cache) >= 200:
            self.profile_cache.pop(next(iter(self.profile_cache)), None)
        self.profile_cache[key] = (time.monotonic() + 60, details)
        return details

    def fetch(self, minutes=60, max_pages=20):
        now = int(time.time())
        found = {}
        warnings = []
        for page in range(max_pages):
            params = {
                "limit": 100, "offset": page * 100,
                "from_time": now - minutes * 60, "to_time": now,
                "sort_field": "time_submitted", "full_description": "true",
                "job_details": "true", "user_details": "true",
                "user_country_details": "true", "user_status": "true",
                "user_avatar": "true",
            }
            url = "https://www.freelancer.com/api/projects/0.1/projects/active/?" + urllib.parse.urlencode(params)
            headers = {"freelancer-oauth-v1": self.token} if self.token else {}
            data = request(url, "Freelancer", headers=headers, timeout=25)
            if not isinstance(data, dict) or data.get("status") != "success":
                raise APIError("Freelancer")
            result = data.get("result") or {}
            if not isinstance(result, dict) or not isinstance(result.get("projects"), list):
                raise APIError("Freelancer: missing projects list")
            rows = result["projects"]
            for row in rows:
                try:
                    p = normalize(row, result.get("users") or {})
                    found[p["id"]] = p
                except (ValueError, TypeError, KeyError, AttributeError):
                    warnings.append("Skipped an unrecognized project record.")
            total = result.get("total_count")
            if len(rows) < 100 or (isinstance(total, int) and (page + 1) * 100 >= total):
                break
        else:
            warnings.append("Scan reached 2,000 projects; reduce the age window. Some projects may be missing.")
        missing_country = sum(p["country"] == "Unknown" for p in found.values())
        missing_payment = sum(p["verified"] is None for p in found.values())
        if found:
            coverage = {label: sum(key in p.get("verifications", {}) for p in found.values())
                        for key, label in VERIFICATION_FIELDS.items()}
            if any(count < len(found) for count in coverage.values()):
                warnings.append("Client states supplied by API: " + ", ".join(
                    f"{label} {count}/{len(found)}" for label, count in coverage.items()) + ". Unknown states are hidden in posts.")
        if missing_country or missing_payment:
            warnings.append(
                f"Freelancer API omitted client country for {missing_country}/{len(found)} projects "
                f"and payment status for {missing_payment}/{len(found)}. "
                "These fields may still appear on the website. Country/payment filters exclude unknown clients. "
                + ("The configured OAuth token did not provide all client details."
                   if self.token else "FREELANCER_OAUTH_TOKEN is not configured; authorized API access may be required.")
            )
        return sorted(found.values(), key=lambda p: p["created"] or 0, reverse=True), list(dict.fromkeys(warnings))
