# ADR-0018: Financial safety gate decisions

- Status: accepted
- Date: 2026-11-04

## Context

[safety-gate.md](../specs/safety-gate.md) left six calculation questions open. They must be
fixed before the gate (#56) is coded, because the same input must always give the same
outcome at the 10% and 15% boundaries. The model output in S2 has no discount field, so a
model-proposed discount has no structured way to reach the gate yet.

## Decision

1. **Discount base: eligible cart line total, before the discount, tax excluded.**
   The percent is compared exactly as a `Decimal`; the percent itself is never rounded.
   Money amounts used for budget accounting are rounded half-up to 2 decimals.
   Why: the discount is a promise on goods the customer picks; tax is not ours to discount
   and differs by region (Gulf VAT rules differ), so a tax-free base keeps one rule across
   markets. Exact comparison removes boundary ambiguity: 15.00% passes, 15.01% is rejected,
   and a rounded percent could silently turn 15.004% into 15%.
2. **Margin, not markup: `(price_after_discount - unit_cost) / price_after_discount`, and the
   result must be strictly greater than the category minimum margin.**
   Why: margin on price is what finance reports and what the vision and spec call "margin";
   markup on cost gives a larger-looking number for the same deal and would let a thin deal
   pass a threshold meant for margin. Strictly greater means a deal exactly at the minimum is
   not auto-approved. The caller passes the unit cost; the Odoo standard cost source is chosen
   in #63. A missing cost or missing category minimum fails closed.
3. **Day boundary: a required IANA timezone in config, with no default; the POC value is
   `Asia/Riyadh`.**
   Why: the target market is the Gulf. Gulf countries are within one hour of each other, so one
   central zone gives a nearly correct local day for all of them; Riyadh is the central one.
   UTC was rejected because a UTC day would start in the middle of the Gulf morning, and the
   daily budget and per-cart limit would reset at a time that does not match Odoo operations
   and customer behaviour. An IANA name (not a fixed offset) stays correct if a country
   changes its rules. No default, so a forgotten setting stops the gate instead of choosing a
   day silently. A multi-timezone design is an MVP question.
4. **Budget exhausted: reject with an explicit reason code; no hold or queue.**
   Why: a held request needs a scheduler, expiry and a re-check of margin and cart state at
   release; that is extra state in the money path for no POC value. A rejection is simple to
   test, and a later queue can be added without weakening the cap.
5. **A model-proposed discount reaches the gate as a structured
   `proposed_discount_percent` field; the existing text matcher stays only as a second,
   defense-in-depth check on `customer_message`.**
   Why: parsing a percentage out of free text is unreliable (Arabic digits, "half price",
   "ثلث", words, code-switching) and a missed parse would mean an unchecked discount. A
   typed number can be validated, compared and tested exactly. The text matcher can still
   block a message that promises a discount the structured field does not contain. Issue #65
   adds the field to the output schema; until then the gate treats a message with a discount
   claim and no structured field as a rejection.
6. **The gate is the only way to coupon creation and message dispatch.** The ports for both
   accept only a `GateApproval` object that only the gate can create and that records the exact
   proposal it approved. Approval is re-checked immediately before execution (AS-5).

## Alternatives

- Tax-inclusive base: rejected, see 1. Rounded percent: rejected, see 1.
- Markup on cost: rejected, see 2.
- UTC day, fixed UTC+3 offset, or Cairo: rejected, see 3.
- Hold budget-exhausted requests: deferred, see 4.
- Parse the discount from text only: rejected, see 5.

## Consequences

- The 10% and 15% thresholds stay accepted starting points (Q13); only their calculation is
  fixed here. Category minimum margins and the daily budget stay configuration values.
- Nothing here applies to real money: all POC data is synthetic.
- Currency rounding beyond 2 decimals (for example currencies with 3 decimals such as KWD,
  BHD, OMR) is not solved; the POC uses 2 decimals and the question moves to MVP planning.
- Supersede this ADR if the base, margin definition or day rule changes.
