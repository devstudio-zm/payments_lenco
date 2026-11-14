"""One-call setup of a company's Lenco Payment Gateway Account.

bench --site <site> execute payments_lenco.setup.gateway_account --kwargs '{"company": "My Company"}'

Creates (if missing) a "Lenco Mobile Money" bank-type account under the
company's Bank Accounts group, and a ZMW Payment Gateway Account that posts
collections to it. Idempotent.
"""

import frappe

from payments_lenco.payments_lenco.doctype.lenco_settings.lenco_settings import GATEWAY

ACCOUNT_NAME = "Lenco Mobile Money"


def gateway_account(company: str, account: str | None = None) -> str:
	if not frappe.db.exists("Payment Gateway", GATEWAY):
		frappe.get_single("Lenco Settings").save()
	account = account or _clearing_account(company)
	name = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": GATEWAY, "company": company})
	doc = frappe.get_doc("Payment Gateway Account", name) if name else frappe.new_doc("Payment Gateway Account")
	doc.update(
		{
			"payment_gateway": GATEWAY,
			"company": company,
			"payment_account": account,
			"currency": "ZMW",
			# Not "Phone": ERPNext only books a Payment Entry for non-phone channels.
			"payment_channel": "Email",
		}
	)
	doc.save()
	frappe.db.commit()
	return doc.name


def _clearing_account(company: str) -> str:
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"{ACCOUNT_NAME} - {abbr}"
	if frappe.db.exists("Account", name):
		return name
	parent = frappe.db.get_value("Account", {"company": company, "account_type": "Bank", "is_group": 1}) or frappe.db.get_value(
		"Account", {"company": company, "root_type": "Asset", "is_group": 1, "account_name": "Bank Accounts"}
	)
	if not parent:
		frappe.throw(f"No Bank Accounts group in {company}; pass account=")
	frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": ACCOUNT_NAME,
			"company": company,
			"parent_account": parent,
			"account_type": "Bank",
			"account_currency": "ZMW",
			"is_group": 0,
		}
	).insert()
	return name
