"""Settlement tests with the Lenco HTTP API mocked.

bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app payments_lenco

Needs ERPNext with at least one company; the first company with a Lenco
Payment Gateway Account is used, or the default company gets one. Settlement
commits by design (webhooks must be durable), so run on a test site.
"""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from payments_lenco import collections
from payments_lenco.client import Lenco, normalize_phone
from payments_lenco.setup import gateway_account

TOKEN = "test-token-not-real"


def company():
	return frappe.db.get_value("Payment Gateway Account", {"payment_gateway": "Lenco"}, "company") or frappe.defaults.get_global_default("company")


def make_payment_request(amount=500):
	from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request as erpnext_make

	comp = company()
	pga = gateway_account(comp)
	if not frappe.db.exists("Customer", "Lenco Test Customer"):
		frappe.get_doc({"doctype": "Customer", "customer_name": "Lenco Test Customer", "customer_type": "Individual"}).insert()
	if not frappe.db.exists("Item", "LENCO-TEST-SERVICE"):
		frappe.get_doc({"doctype": "Item", "item_code": "LENCO-TEST-SERVICE", "item_group": "All Item Groups", "stock_uom": "Nos", "is_stock_item": 0}).insert()
	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"company": comp,
			"customer": "Lenco Test Customer",
			"currency": "ZMW",
			"delivery_date": frappe.utils.add_days(frappe.utils.nowdate(), 7),
			"items": [{"item_code": "LENCO-TEST-SERVICE", "qty": 1, "rate": amount}],
		}
	)
	so.flags.ignore_permissions = True
	so.insert()
	so.submit()
	return erpnext_make(dt="Sales Order", dn=so.name, submit_doc=1, mute_email=1, payment_gateway_account=pga, return_doc=1)


class TestLenco(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("Lenco Settings")
		settings.update({"enabled": 1, "environment": "sandbox", "api_token": TOKEN, "max_amount": 10000})
		settings.save()

	def start(self, pr, phone="0971234567"):
		with patch.object(Lenco, "_request", return_value={"status": "pay-offline"}) as request:
			result = collections.start(pr.name, phone)
		return result, request

	def settle(self, reference, collection):
		with patch.object(Lenco, "_request", return_value=collection):
			return collections.refresh(reference, "test")

	def payments(self, pr):
		return frappe.get_all("Payment Entry Reference", filters={"reference_name": pr.reference_name, "docstatus": 1})

	def test_phone_numbers(self):
		self.assertEqual(normalize_phone("+260 97 123 4567"), "0971234567")
		self.assertEqual(normalize_phone("0761234567"), "0761234567")
		self.assertEqual(normalize_phone("0211234567"), "")
		self.assertEqual(normalize_phone("12345"), "")

	def test_signature_is_over_the_raw_body(self):
		raw = b'{"event":"collection.successful"}'
		key = hashlib.sha256(TOKEN.encode()).hexdigest().encode()
		good = hmac.new(key, raw, hashlib.sha512).hexdigest()
		client = Lenco(TOKEN)
		self.assertTrue(client.verify_signature(raw, good))
		self.assertFalse(client.verify_signature(raw + b" ", good))
		self.assertFalse(client.verify_signature(raw, ""))

	def test_amount_comes_from_the_request_and_ceiling_applies(self):
		pr = make_payment_request(500)
		result, request = self.start(pr)
		body = request.call_args.args[2]
		self.assertEqual(body["amount"], 500)
		self.assertEqual(body["operator"], "airtel")
		self.assertEqual(body["bearer"], "merchant")
		self.assertEqual(result["state"], "pending")
		big = make_payment_request(10001)
		with self.assertRaises(collections.PaymentNotAllowed):
			self.start(big)

	def test_success_settles_once(self):
		pr = make_payment_request(500)
		reference = self.start(pr)[0]["reference"]
		ok = {"status": "successful", "amount": "500.00", "currency": "ZMW", "reference": reference}
		self.assertEqual(self.settle(reference, ok), "paid")
		self.assertEqual(self.settle(reference, ok), "paid")
		self.assertEqual(frappe.db.get_value("Payment Request", pr.name, "status"), "Paid")
		self.assertEqual(len(self.payments(pr)), 1)

	def test_amount_mismatch_is_held_for_review(self):
		pr = make_payment_request(500)
		reference = self.start(pr)[0]["reference"]
		self.assertEqual(self.settle(reference, {"status": "successful", "amount": "50.00", "currency": "ZMW"}), "review")
		self.assertEqual(collections.state(pr.name), "review")
		self.assertEqual(len(self.payments(pr)), 0)

	def test_failure_then_retry(self):
		pr = make_payment_request(500)
		first = self.start(pr)[0]["reference"]
		self.assertEqual(self.settle(first, {"status": "failed", "reasonForFailure": "insufficient funds"}), "failed")
		second = self.start(pr)[0]["reference"]
		self.assertNotEqual(first, second)
		self.assertEqual(self.settle(second, {"status": "successful", "amount": 500, "currency": "ZMW"}), "paid")

	def test_superseded_attempt_success_is_held_not_dropped(self):
		pr = make_payment_request(500)
		old = self.start(pr)[0]["reference"]
		self.start(pr)
		self.assertEqual(self.settle(old, {"status": "successful", "amount": 500, "currency": "ZMW"}), "review")
		self.assertEqual(len(self.payments(pr)), 0)

	def test_webhook_rejects_bad_signatures(self):
		from payments_lenco import webhook

		raw = json.dumps({"event": "collection.successful", "data": {"reference": "nope"}}).encode()
		for signature, code in (("bad", 401), ("", 400)):
			frappe.local.response = frappe._dict()
			frappe.local.request = MagicMock()
			frappe.local.request.get_data.return_value = raw
			try:
				with patch.object(frappe, "get_request_header", return_value=signature):
					webhook.receive()
			finally:
				del frappe.local.request
			self.assertEqual(frappe.local.response.http_status_code, code)
