"""Lenco mobile-money collections against ERPNext Payment Requests.

One Payment Request is the amount owed; each prompt sent to a phone is one
attempt, logged as an Integration Request (request_id = Lenco reference).
Rules carried over from a production WooCommerce gateway, each learned live:

- The amount always comes from the Payment Request, never from a request.
- The webhook only says something changed; the API says what it changed to.
- A successful collection must match the request's amount and currency, or
  the payment is held for a person to review.
- Settle once, however many webhooks and polls arrive together: the Payment
  Request row is locked, and read again only after the lock is held (in a
  fresh transaction, so REPEATABLE READ cannot hand back a stale snapshot).
- A success on a superseded attempt is never applied silently, and never
  dropped: it is held for review.
"""

import json
import secrets
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, now_datetime

from payments_lenco.client import OPERATOR_LABELS, Lenco, LencoError, detect_operator, mask_phone, normalize_phone, shopper_error

SERVICE = "Lenco"
FAILED = {"failed", "declined", "cancelled"}
PENDING_WINDOW_HOURS = 24


class PaymentNotAllowed(frappe.ValidationError):
	pass


@contextmanager
def elevated():
	"""Settlement creates Payment Entries on behalf of the gateway, not the caller."""
	previous = frappe.session.user
	frappe.set_user("Administrator")
	try:
		yield
	finally:
		frappe.set_user(previous)


def start(payment_request: str, phone: str, operator: str | None = None) -> dict:
	"""Send a mobile-money prompt for a submitted Payment Request."""
	settings = frappe.get_cached_doc("Lenco Settings")
	local = normalize_phone(phone)
	if not local:
		frappe.throw(_("Enter a Zambian MTN, Airtel or Zamtel mobile number."), PaymentNotAllowed)
	detected = detect_operator(local)
	if operator and operator != detected:
		frappe.throw(_("That number is not on {0}. Check the number or choose the right network.").format(OPERATOR_LABELS.get(operator, operator)), PaymentNotAllowed)

	_lock(payment_request)
	pr = frappe.get_doc("Payment Request", payment_request, for_update=True)
	if pr.docstatus != 1 or pr.status in ("Paid", "Cancelled"):
		frappe.throw(_("This payment is no longer open."), PaymentNotAllowed)
	if pr.payment_gateway != SERVICE:
		frappe.throw(_("This payment is not set up for mobile money."), PaymentNotAllowed)
	if not settings.enabled:
		frappe.throw(shopper_error(), PaymentNotAllowed)
	amount = flt(pr.grand_total)
	if amount > flt(settings.max_amount or 10000):
		frappe.throw(_("Mobile money is limited to K{0:,.0f} per payment. Please pay by bank transfer.").format(flt(settings.max_amount or 10000)), PaymentNotAllowed)

	for previous in _attempts(pr.name, statuses=("Queued",)):
		frappe.db.set_value("Integration Request", previous, {"status": "Cancelled", "error": "Superseded by a newer attempt"})
	reference = f"{pr.name}-{secrets.token_hex(3).upper()}"
	log = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": SERVICE,
			"request_id": reference,
			"reference_doctype": "Payment Request",
			"reference_docname": pr.name,
			"status": "Queued",
			"data": json.dumps({"amount": amount, "currency": pr.currency, "phone": mask_phone(local), "operator": detected}),
		}
	).insert(ignore_permissions=True)
	# Commit before calling Lenco so a fast webhook can find this attempt.
	frappe.db.commit()
	try:
		collection = Lenco.from_settings().collect_mobile_money(amount, reference, local, detected)
	except LencoError as error:
		frappe.db.set_value("Integration Request", log.name, {"status": "Failed", "error": str(error)})
		frappe.log_error(f"Lenco start failed for {pr.name}", str(error))
		frappe.db.commit()
		frappe.throw(shopper_error(), PaymentNotAllowed)
	frappe.db.set_value("Integration Request", log.name, "output", json.dumps(collection))
	_comment(pr, _("Mobile money prompt sent via {0} to {1}. Awaiting approval. Lenco reference {2}.").format(OPERATOR_LABELS[detected], mask_phone(local), reference))
	return {"reference": reference, "state": "pending"}


def refresh(reference: str, source: str) -> str:
	"""Ask Lenco for an attempt's state and apply it. Returns the display state."""
	log = frappe.db.get_value("Integration Request", {"integration_request_service": SERVICE, "request_id": reference}, ["name", "reference_docname"], as_dict=True)
	if not log:
		return "unknown"
	try:
		collection = Lenco.from_settings().collection(reference)
	except LencoError as error:
		frappe.log_error(f"Lenco status check failed for {reference}", str(error))
		return state(log.reference_docname)
	return settle_once(log.name, collection, source)


def settle_once(log_name: str, collection: dict, source: str) -> str:
	payment_request = frappe.db.get_value("Integration Request", log_name, "reference_docname")
	frappe.db.commit()  # the lock below must be the first read of a fresh transaction
	_lock(payment_request)
	pr = frappe.get_doc("Payment Request", payment_request)
	log = frappe.get_doc("Integration Request", log_name)
	status = str(collection.get("status") or "").lower()
	reference = log.request_id

	if pr.status == "Paid":
		if status == "successful" and log.status != "Completed":
			_hold(pr, log, collection, _("Lenco reports a second successful collection {0} for an already paid request. Check for a duplicate charge.").format(reference))
		return "paid"
	if status in FAILED:
		reason = collection.get("reasonForFailure") or collection.get("failureReason") or "no reason given"
		log.db_set({"status": "Failed", "error": f"{reason} ({source})", "output": json.dumps(collection)})
		_comment(pr, _("Lenco mobile money payment failed ({0}). Detected by {1}.").format(reason, source))
		return state(pr.name)
	if status != "successful":
		log.db_set("output", json.dumps(collection))
		return "pending"

	expected = json.loads(log.data or "{}")
	if log.status == "Cancelled":
		_hold(pr, log, collection, _("Lenco collected {0} on an attempt that a newer one had replaced. Held for review.").format(reference))
		return "review"
	paid, currency = flt(collection.get("amount")), str(collection.get("currency") or pr.currency).upper()
	if currency != (pr.currency or "").upper() or abs(paid - flt(pr.grand_total)) > 0.01 or abs(paid - flt(expected.get("amount"))) > 0.01:
		_hold(pr, log, collection, _("Lenco reported a successful collection of {0} {1} but the request is for {2} {3}. Held for manual review.").format(f"{paid:,.2f}", currency, f"{flt(pr.grand_total):,.2f}", pr.currency))
		return "review"

	log.db_set({"status": "Completed", "output": json.dumps(collection), "error": None})
	with elevated():
		pr.run_method("on_payment_authorized", "Completed")
	_comment(
		pr,
		_("Mobile money payment received via {0} from {1}. Lenco reference {2} (confirmed by {3}).").format(
			OPERATOR_LABELS.get(expected.get("operator"), expected.get("operator")), expected.get("phone"), reference, source
		),
	)
	return "paid"


def state(payment_request: str) -> str:
	"""paid | pending | not_started | failed | review, for the shopper's screen."""
	if frappe.db.get_value("Payment Request", payment_request, "status") == "Paid":
		return "paid"
	latest = frappe.get_all(
		"Integration Request",
		filters={"integration_request_service": SERVICE, "reference_doctype": "Payment Request", "reference_docname": payment_request},
		fields=["status", "error"],
		order_by="creation desc",
		limit=1,
	)
	if not latest:
		return "not_started"
	if latest[0].status == "Authorized":
		return "review"
	return {"Failed": "failed", "Completed": "paid", "Cancelled": "not_started"}.get(latest[0].status, "pending")


def poll():
	"""Scheduler safety net for webhooks that never arrive."""
	since = add_to_date(now_datetime(), hours=-PENDING_WINDOW_HOURS)
	for reference in frappe.get_all(
		"Integration Request",
		filters={"integration_request_service": SERVICE, "status": "Queued", "creation": [">", since]},
		pluck="request_id",
	):
		refresh(reference, "poll")
		frappe.db.commit()


def _attempts(payment_request, statuses):
	return frappe.get_all(
		"Integration Request",
		filters={"integration_request_service": SERVICE, "reference_docname": payment_request, "status": ["in", statuses]},
		pluck="name",
	)


def _lock(payment_request):
	frappe.db.sql("select name from `tabPayment Request` where name = %s for update", payment_request)


def _hold(pr, log, collection, message):
	log.db_set({"status": "Authorized", "output": json.dumps(collection), "error": message})
	_comment(pr, message)
	frappe.log_error(f"Lenco payment held for review: {pr.name}", message)


def _comment(pr, text):
	frappe.get_doc({"doctype": "Comment", "comment_type": "Info", "reference_doctype": "Payment Request", "reference_name": pr.name, "content": text}).insert(ignore_permissions=True)
