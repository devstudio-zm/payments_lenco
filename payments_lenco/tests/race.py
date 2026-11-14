"""Duplicate-settlement race: two settlers for one successful collection.

Lenco has delivered one webhook twice a second apart, and the poller can
land at the same moment. Run from the bench directory:

    apps/payments_lenco/payments_lenco/tests/race.sh <site>            # expect: 1 payment entry
    apps/payments_lenco/payments_lenco/tests/race.sh <site> --unlocked # expect: a duplicate

The unlocked mode replays settlement without the row lock and must report a
duplicate, otherwise this test proves nothing. Both racers read the Payment
Request first, then start together from a shared timestamp.
"""

import time
from unittest.mock import patch

import frappe

from payments_lenco import collections
from payments_lenco.client import Lenco


def prepare():
	from payments_lenco.tests.test_collections import make_payment_request

	pr = make_payment_request(500)
	with patch.object(Lenco, "_request", return_value={"status": "pay-offline"}):
		reference = collections.start(pr.name, "0971234567")["reference"]
	frappe.db.commit()
	print(f"RACE {pr.name} {reference}")


def racer(reference: str, at: float, unlocked: int = 0):
	log = frappe.db.get_value("Integration Request", {"request_id": reference}, ["name", "reference_docname"], as_dict=True)
	frappe.get_doc("Payment Request", log.reference_docname)  # read first, like a real handler
	time.sleep(max(0, at - time.time()))
	collection = {"status": "successful", "amount": 500, "currency": "ZMW", "reference": reference}
	if unlocked:
		with patch.object(collections, "_lock", lambda name: None), patch.object(frappe.db, "commit", lambda *a, **k: None):
			result = collections.settle_once(log.name, collection, "race")
	else:
		result = collections.settle_once(log.name, collection, "race")
	frappe.db.commit()
	print(f"RACER {result}")


def count(payment_request: str):
	reference_name = frappe.db.get_value("Payment Request", payment_request, "reference_name")
	entries = frappe.get_all("Payment Entry Reference", filters={"reference_name": reference_name, "docstatus": 1}, pluck="parent")
	print(f"PAYMENT_ENTRIES {len(set(entries))}")
