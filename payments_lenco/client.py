"""Thin HTTP client for the Lenco API v2: the only code that talks to Lenco.

Ported from a production WooCommerce gateway. Verified against the live service:
both environments answer {"status": false, "message": "Unauthorized"} with
HTTP 401 without a token, and Lenco can return HTTP 200 with "status": false,
so the envelope is authoritative, not the status code.
"""

import hashlib
import hmac
import re

import frappe
import requests
from frappe import _

BASE = {"live": "https://api.lenco.co/access/v2", "sandbox": "https://sandbox.lenco.co/access/v2"}
OPERATOR_PREFIXES = {"mtn": ("096", "076"), "airtel": ("097", "077"), "zamtel": ("095", "075")}
OPERATOR_LABELS = {"mtn": "MTN Mobile Money", "airtel": "Airtel Money", "zamtel": "Zamtel Kwacha"}


class LencoError(Exception):
	"""A failed call. `str(error)` is for logs only, never for shoppers."""


class Lenco:
	def __init__(self, token: str, environment: str = "sandbox", bearer: str = "merchant"):
		self.token = (token or "").strip()
		self.base = BASE["live" if environment == "live" else "sandbox"]
		self.bearer = bearer or "merchant"

	@classmethod
	def from_settings(cls):
		settings = frappe.get_cached_doc("Lenco Settings")
		if not settings.enabled:
			raise LencoError("Lenco is not enabled")
		return cls(settings.get_password("api_token", raise_exception=False), settings.environment, settings.bearer)

	def collect_mobile_money(self, amount: float, reference: str, phone: str, operator: str) -> dict:
		"""Ask the shopper's wallet to pay. The amount comes from the document, never the request."""
		return self._request(
			"POST",
			"/collections/mobile-money",
			{
				"amount": round(float(amount), 2),
				"reference": reference,
				"phone": phone,
				"operator": operator,
				"country": "zm",
				"bearer": self.bearer,
			},
		)

	def collection(self, reference: str) -> dict:
		"""Authoritative status of a collection we started."""
		return self._request("GET", f"/collections/status/{requests.utils.quote(reference, safe='')}")

	def verify_signature(self, raw_body: bytes, signature: str) -> bool:
		"""HMAC-SHA512 over the raw body, keyed with the token's SHA-256 hex digest."""
		if not self.token or not signature:
			return False
		key = hashlib.sha256(self.token.encode()).hexdigest().encode()
		expected = hmac.new(key, raw_body, hashlib.sha512).hexdigest()
		return hmac.compare_digest(expected, signature.strip())

	def _request(self, method: str, path: str, body: dict | None = None) -> dict:
		if not self.token:
			raise LencoError("Lenco API token is not configured")
		try:
			response = requests.request(
				method,
				self.base + path,
				json=body,
				headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
				timeout=30,
			)
		except requests.RequestException as error:
			raise LencoError(f"HTTP failure {method} {path}: {error}") from error
		try:
			payload = response.json()
		except ValueError as error:
			raise LencoError(f"Unreadable response {method} {path} (HTTP {response.status_code}): {response.text[:300]}") from error
		ok = 200 <= response.status_code < 300 and payload.get("status", True) is not False
		if not ok:
			raise LencoError(f"API error {method} {path} (HTTP {response.status_code}): {payload.get('message')} {payload.get('errorCode') or ''}".strip())
		return payload.get("data") if isinstance(payload.get("data"), dict) else {}


def normalize_phone(phone: str) -> str:
	"""Local 0XXXXXXXXX form Lenco expects, or "" if it is not a Zambian mobile number."""
	digits = re.sub(r"\D+", "", phone or "")
	if digits.startswith("260"):
		digits = digits[3:]
	digits = digits.lstrip("0")
	if len(digits) != 9:
		return ""
	local = "0" + digits
	return local if detect_operator(local) else ""


def detect_operator(local_phone: str) -> str:
	prefix = local_phone[:3]
	return next((operator for operator, prefixes in OPERATOR_PREFIXES.items() if prefix in prefixes), "")


def mask_phone(local_phone: str) -> str:
	return local_phone if len(local_phone) < 10 else f"{local_phone[:3]}****{local_phone[-3:]}"


def shopper_error() -> str:
	return _("We could not start the mobile money payment. Please check the number and try again, or pay by bank transfer.")
