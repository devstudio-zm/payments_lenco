"""Lenco webhook receiver: POST /api/method/payments_lenco.webhook.receive

Mobile money is asynchronous (the shopper approves on the handset, often
after the browser has gone), so this is the primary confirmation path and the
scheduler poll is only a safety net. The signature is checked over the raw
body before anything is parsed.
"""

import json

import frappe

from payments_lenco.client import Lenco, LencoError
from payments_lenco.collections import refresh

EVENTS = {"collection.successful", "collection.failed", "collection.settled"}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive():
	raw = frappe.request.get_data(cache=True) or b""
	signature = frappe.get_request_header("X-Lenco-Signature") or ""
	if not raw or not signature:
		return _respond(400, "missing payload or signature")
	try:
		client = Lenco.from_settings()
	except LencoError:
		return _respond(503, "not configured")
	if not client.verify_signature(raw, signature):
		return _respond(401, "invalid signature")

	try:
		body = json.loads(raw)
	except ValueError:
		return _respond(400, "malformed payload")
	event = body.get("event") if isinstance(body, dict) else None
	data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else {}
	if not event:
		return _respond(400, "malformed payload")
	if event not in EVENTS:
		return _respond(200, "ignored")
	reference = str(data.get("reference") or "")
	# 200 for an unknown reference: valid event, not ours; a retry will not help.
	result = refresh(reference, "webhook") if reference else "unknown"
	return _respond(200, "unknown reference" if result == "unknown" else "ok")


def _respond(code: int, message: str):
	frappe.local.response.http_status_code = code
	return {"received": code < 400, "message": message}
