# Copyright (c) 2026, DevStudio ZM
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

GATEWAY = "Lenco"
SUPPORTED_CURRENCIES = ("ZMW",)


class LencoSettings(Document):
	"""Payments-app gateway controller for Lenco mobile money (Zambia)."""

	@property
	def webhook_url(self):
		return get_url("/api/method/payments_lenco.webhook.receive")

	def on_update(self):
		from payments.utils import create_payment_gateway

		create_payment_gateway(GATEWAY, settings="Lenco Settings", controller="Lenco Settings")

	def validate_transaction_currency(self, currency):
		if currency not in SUPPORTED_CURRENCIES:
			frappe.throw(_("Lenco mobile money only accepts Zambian kwacha (ZMW)."))

	def get_payment_url(self, **kwargs):
		"""Lenco is a push flow (a prompt on the payer's phone), not a hosted page.

		Payment Requests still ask the gateway for a link; there is none, so the
		app integrating checkout calls payments_lenco.collections.start().
		"""
		return ""

	def request_for_payment(self, **kwargs):
		"""Payments-app entry point for phone-channel Payment Requests."""
		from payments_lenco.collections import start

		start(kwargs["reference_docname"], kwargs.get("phone_number"))
