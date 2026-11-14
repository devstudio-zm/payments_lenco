# Payments Lenco

[Lenco](https://lenco.co) mobile money (MTN, Airtel and Zamtel in Zambia) for ERPNext, as a gateway for the [Frappe Payments](https://github.com/frappe/payments) app.

- Sends a payment prompt to the payer's phone for an ERPNext **Payment Request** (against a Sales Order or Sales Invoice).
- Confirms by **signed webhook**, with a scheduler poll as the safety net.
- Books exactly one **Payment Entry** per paid request, however many webhooks and polls arrive at once.

Tested on Frappe/ERPNext v16 with Payments `version-16`.

## Install

```bash
bench get-app payments --branch version-16        # if not installed
bench get-app https://github.com/devstudio-zm/payments_lenco
bench --site <site> install-app payments_lenco
```

## Configure

1. **Desk → Lenco Settings:**
   - tick *Enabled*;
   - choose *sandbox* or *live*;
   - paste the Lenco API v2 token;
   - keep *Maximum per transaction* at Lenco's mobile-money ceiling (default K10,000).

   Saving creates the **Lenco** Payment Gateway.
2. **Register the webhook URL** in the Lenco dashboard. It is shown on the settings page: `https://<site>/api/method/payments_lenco.webhook.receive`.
3. **Create the company's gateway account:**
   ```bash
   bench --site <site> execute payments_lenco.setup.gateway_account --kwargs '{"company": "My Company"}'
   ```
   This adds a *Lenco Mobile Money* bank account and a ZMW Payment Gateway Account that posts to it.
4. **Allowlist the outbound IP.** Lenco allowlists your server's outbound IP address, so ask Lenco to add it. Calls fail until it is allowlisted.

## Use it from your checkout

Accepted Zambian mobile prefixes are MTN **096/076/056**, Airtel **097/077/057**, and Zamtel **095/075/055**.

Lenco is a push flow, so there is no hosted payment page. Your app creates the Payment Request and starts the collection:

```python
from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request
from payments_lenco import collections

pr = make_payment_request(
    dt="Sales Order", dn=order.name, submit_doc=1, mute_email=1, return_doc=1,
    payment_gateway_account=frappe.db.get_value(
        "Payment Gateway Account", {"payment_gateway": "Lenco", "company": order.company}
    ),
)
collections.start(pr.name, phone="0971234567")   # prompt sent; returns {"reference", "state"}
collections.state(pr.name)                         # paid | pending | not_started | failed | review
collections.refresh(reference, "manual")           # ask Lenco now instead of waiting
```

**Validation.** `start()` raises `PaymentNotAllowed` with a message you can show the payer when:

- the number is not a Zambian mobile number (the operator is detected from the prefix);
- the operator does not match the number;
- the amount is over the ceiling.

**Errors.** Provider error details never reach the payer; they go to the Error Log and to the attempt's Integration Request.

**Access.** Put these calls behind your own authorization, for example an order key or the logged-in customer.

## How settlement works

Each rule below was learned from a live store.

- **Amounts come from ERPNext.** The amount is the Payment Request's `grand_total`, never a value from a request.
- **Webhooks are authenticated before parsing.** The signature is an HMAC-SHA512 of the raw body, keyed with the SHA-256 hex digest of the API token.
- **Lenco's API is the source of truth.** A signed webhook only says that something changed, so the collection is re-fetched from Lenco before anything is booked.
- **Mismatches are held for review.** If a successful collection does not match the request's amount and currency, nothing is booked. The Integration Request status becomes *Authorized* and a comment is added to the Payment Request.
- **Each payment settles once.** The Payment Request row is locked, and the row is read again only after the lock is held, in a fresh transaction so REPEATABLE READ cannot return a stale snapshot. Duplicate webhooks, or a webhook racing the poller, settle once and both return cleanly.
- **One attempt per prompt.** Each prompt is an Integration Request whose `request_id` is the Lenco reference. Starting a new prompt supersedes the open one. These cases are held for review, neither applied nor dropped:
  - a success on a superseded attempt;
  - a second success on a request that is already paid.
- **Gateway accounts use the *Email* channel.** ERPNext books a Payment Entry only for non-Phone channels.

## Tests

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app payments_lenco
apps/payments_lenco/payments_lenco/tests/race.sh <test-site> --unlocked   # must go wrong
apps/payments_lenco/payments_lenco/tests/race.sh <test-site>              # settles exactly once
```

**Unit tests** mock the Lenco HTTP API.

**The race test** starts two real settlers against one collection. With `--unlocked`, it replays settlement without the lock and must go wrong, which proves the test can catch the bug. On MariaDB with `innodb_snapshot_isolation` (11.x), "going wrong" shows up as one settler crashing with a write conflict, which Lenco would see as a failed webhook.

**Use a test site.** The tests commit the records they create (a customer, sales orders and payment requests).

## Licence

MIT
