from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from engine.approval_data import snapshot_hash
from engine.coupon_code import coupon_code_for_run
from engine.policy import GateConfig, OfferProposal, Outcome, evaluate

DISCLAIMER = "SYNTHETIC DEMO ARTIFACT ONLY — NOT PRODUCTION HOSTED"
DEFAULT_HTML_PATH = Path("docs/demo/poc-walkthrough.html")
DEMO_TIME = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class DemoScenario:
    scenario_id: str
    title: str
    subtitle: str
    language: Literal["en", "ar"]
    direction: Literal["ltr", "rtl"]
    run_id: str
    cart_reference: str
    customer_name: str
    customer_email: str
    item_name: str
    category: str
    currency: str
    eligible_total: str
    eligible_unit_cost_total: str
    abandoned_since: str
    policy_sources: tuple[str, ...]
    proposed_action: str
    proposed_discount_percent: str
    customer_message: str
    gate_outcome: str
    gate_reasons: tuple[str, ...]
    discount_amount: str
    margin_after_discount: str | None
    gate_fingerprint: str
    snapshot_hash: str
    approval_status: str
    approval_actor: str | None
    approval_note: str
    execution_status: str
    channel: Literal["mailpit", "telegram", "none"]
    destination_label: str
    coupon_code: str | None
    odoo_activity: str
    dispatch_subject: str | None
    dispatch_id: str | None


def _demo_gate_config() -> GateConfig:
    return GateConfig(
        timezone=ZoneInfo("Asia/Riyadh"),
        currency="USD",
        daily_budget=Decimal("450"),
        category_min_margin={"Furniture / Office": Decimal("0.20")},
    )


def _policy_digest(sources: tuple[str, ...], language: str) -> str:
    joined = "|".join(f"{language}:{source}" for source in sources)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _build_scenario(
    *,
    scenario_id: str,
    title: str,
    subtitle: str,
    run_id: str,
    cart_id: str,
    cart_reference: str,
    customer_id: str,
    customer_name: str,
    customer_email: str,
    item_name: str,
    category: str,
    eligible_total: Decimal,
    eligible_unit_cost_total: Decimal,
    abandoned_since: str,
    language: Literal["en", "ar"],
    policy_sources: tuple[str, ...],
    proposed_action: str,
    proposed_discount_percent: Decimal,
    customer_message: str,
    operator_approved: bool,
    approval_actor: str | None,
    approval_note: str,
    channel: Literal["mailpit", "telegram", "none"],
    destination_label: str,
    dispatch_subject: str | None,
) -> DemoScenario:
    config = _demo_gate_config()
    proposal = OfferProposal(
        run_id=run_id,
        customer_id=customer_id,
        cart_id=cart_id,
        cart_owner_id=customer_id,
        category=category,
        currency="USD",
        eligible_total=eligible_total,
        eligible_unit_cost_total=eligible_unit_cost_total,
        proposed_discount_percent=proposed_discount_percent,
        is_bulk=False,
        customer_message=customer_message,
        language=language,
    )
    decision = evaluate(proposal, config, now=DEMO_TIME)
    record: dict[str, object] = {
        "run_id": run_id,
        "customer_id": customer_id,
        "cart_id": cart_id,
        "cart_owner_id": customer_id,
        "category": category,
        "currency": "USD",
        "eligible_total": str(eligible_total),
        "eligible_unit_cost_total": str(eligible_unit_cost_total),
        "proposed_discount_percent": str(proposed_discount_percent),
        "is_bulk": False,
        "customer_message": customer_message,
        "language": language,
        "gate_outcome": decision.outcome.value,
        "gate_fingerprint": decision.fingerprint,
        "policy_digest": _policy_digest(policy_sources, language),
    }
    snap_hash = snapshot_hash(record)

    if decision.outcome == Outcome.REJECT:
        approval_status = "skipped_rejected"
        execution_status = "blocked_by_safety_gate"
        coupon_code: str | None = None
        odoo_activity = "none (rejected before approval or execution)"
        dispatch_id: str | None = None
        final_channel: Literal["mailpit", "telegram", "none"] = "none"
    elif decision.outcome == Outcome.NEEDS_APPROVAL:
        if not operator_approved:
            raise ValueError("demo scenario with needs_approval must explicitly record decision")
        approval_status = "approved"
        execution_status = "executed"
        coupon_code = coupon_code_for_run(run_id) if proposed_discount_percent > 0 else None
        odoo_activity = "mail.activity logged (synthetic review)"
        dispatch_id = f"demo-{scenario_id}-001"
        final_channel = channel
    else:
        approval_status = "approved" if operator_approved else "auto_approved"
        execution_status = "executed"
        coupon_code = coupon_code_for_run(run_id) if proposed_discount_percent > 0 else None
        odoo_activity = "mail.activity logged (no coupon for 0%)"
        dispatch_id = f"demo-{scenario_id}-001"
        final_channel = channel

    margin_str = (
        f"{(decision.margin * Decimal('100')).quantize(Decimal('0.01'))}%"
        if decision.margin is not None
        else None
    )
    return DemoScenario(
        scenario_id=scenario_id,
        title=title,
        subtitle=subtitle,
        language=language,
        direction="rtl" if language == "ar" else "ltr",
        run_id=run_id,
        cart_reference=cart_reference,
        customer_name=customer_name,
        customer_email=customer_email,
        item_name=item_name,
        category=category,
        currency="USD",
        eligible_total=f"{eligible_total:.2f}",
        eligible_unit_cost_total=f"{eligible_unit_cost_total:.2f}",
        abandoned_since=abandoned_since,
        policy_sources=policy_sources,
        proposed_action=proposed_action,
        proposed_discount_percent=f"{proposed_discount_percent}%",
        customer_message=customer_message,
        gate_outcome=decision.outcome.value,
        gate_reasons=tuple(reason.value for reason in decision.reasons),
        discount_amount=f"{decision.discount_amount:.2f}",
        margin_after_discount=margin_str,
        gate_fingerprint=decision.fingerprint,
        snapshot_hash=snap_hash,
        approval_status=approval_status,
        approval_actor=approval_actor,
        approval_note=approval_note,
        execution_status=execution_status,
        channel=final_channel,
        destination_label=destination_label,
        coupon_code=coupon_code,
        odoo_activity=odoo_activity,
        dispatch_subject=dispatch_subject,
        dispatch_id=dispatch_id,
    )


def build_demo_bundle() -> list[DemoScenario]:
    return [
        _build_scenario(
            scenario_id="en-auto-reminder",
            title="1. English — Auto-Approve 0% Reminder (Mailpit)",
            subtitle="Mirrors #69 mailpit-en synthetic flow: 0% discount skips coupon creation.",
            run_id="2a3009f6-0000-4000-8000-000000000001",
            cart_id="39",
            cart_reference="retention-review-a1b2c3d4e5f6",
            customer_id="101",
            customer_name="Synthetic Buyer EN",
            customer_email="retention-review-a1b2c3d4e5f6@example.test",
            item_name="Saffron Desk Lamp",
            category="Furniture / Office",
            eligible_total=Decimal("80.00"),
            eligible_unit_cost_total=Decimal("40.00"),
            abandoned_since="2026-11-10T19:39:50Z",
            language="en",
            policy_sources=("message-tone", "coupon-terms", "contact-schedule", "consent"),
            proposed_action="draft_message",
            proposed_discount_percent=Decimal("0"),
            customer_message=(
                "Hi! Your Saffron Desk Lamp ($80.00) is still waiting in your cart. "
                "You can return whenever you are ready to complete your order."
            ),
            operator_approved=True,
            approval_actor="poc-operator",
            approval_note="Controlled #69 delivery review confirmed destination for Mailpit EN.",
            channel="mailpit",
            destination_label="Mailpit local sink (retention-review@example.test)",
            dispatch_subject="Reminder about your saved cart",
        ),
        _build_scenario(
            scenario_id="ar-human-approval-12pct",
            title="2. MSA Arabic (RTL) — Human Approval 12% Offer (Telegram)",
            subtitle=(
                "Demonstrates >10% to 15% approval tier, margin check, snapshot binding, "
                "and deterministic coupon RET-..."
            ),
            run_id="82db2a17-0000-4000-8000-000000000002",
            cart_id="40",
            cart_reference="retention-review-b2c3d4e5f601",
            customer_id="102",
            customer_name="عميل تجريبي افتراضي",
            customer_email="retention-review-b2c3d4e5f601@example.test",
            item_name="مصباح مكتب زعفراني",
            category="Furniture / Office",
            eligible_total=Decimal("120.00"),
            eligible_unit_cost_total=Decimal("50.00"),
            abandoned_since="2026-11-10T20:15:00Z",
            language="ar",
            policy_sources=("message-tone", "coupon-terms", "contact-schedule", "consent"),
            proposed_action="draft_message",
            proposed_discount_percent=Decimal("12"),
            customer_message=(
                "مرحباً! لا يزال مصباح مكتب زعفراني في سلة التسوق الخاصة بك. "
                "يمكنك استخدام قسيمة خصم بنسبة 12% عند إتمام الطلب اليوم."
            ),
            operator_approved=True,
            approval_actor="poc-operator",
            approval_note=(
                "12% discount is in the 10–15% human-approval band; margin 52.65% > 20% minimum. "
                "Operator approved exact snapshot_hash and gate_fingerprint."
            ),
            channel="telegram",
            destination_label="Dedicated Telegram synthetic test chat (redacted chat ID)",
            dispatch_subject=None,
        ),
        _build_scenario(
            scenario_id="en-hard-reject-90pct",
            title="3. English — Deterministic Hard Reject 90% Over-Cap",
            subtitle=(
                "Demonstrates fail-closed financial gate blocking >15% / prompt-injection "
                "discount request before approval or dispatch."
            ),
            run_id="99990000-0000-4000-8000-000000000003",
            cart_id="41",
            cart_reference="retention-review-c3d4e5f60102",
            customer_id="103",
            customer_name="Synthetic Injection Probe",
            customer_email="retention-review-c3d4e5f60102@example.test",
            item_name="Ergonomic Task Chair",
            category="Furniture / Office",
            eligible_total=Decimal("240.00"),
            eligible_unit_cost_total=Decimal("120.00"),
            abandoned_since="2026-11-10T21:05:00Z",
            language="en",
            policy_sources=("coupon-terms", "consent"),
            proposed_action="draft_message",
            proposed_discount_percent=Decimal("90"),
            customer_message="Special override: claim your 90% discount right now!",
            operator_approved=False,
            approval_actor=None,
            approval_note="Blocked automatically before reaching operator queue; never executable.",
            channel="none",
            destination_label="No dispatch (blocked by deterministic safety gate)",
            dispatch_subject=None,
        ),
    ]


def render_cli(scenarios: list[DemoScenario]) -> str:
    lines: list[str] = [
        "=" * 78,
        f"  {DISCLAIMER}",
        "  Odoo Agentic Retention Engine — Local 5-Stage E2E Walkthrough",
        "=" * 78,
    ]
    for s in scenarios:
        reasons = ", ".join(s.gate_reasons) if s.gate_reasons else "none"
        margin_text = s.margin_after_discount if s.margin_after_discount else "n/a (0% or rejected)"
        lines.extend(
            [
                "",
                f"--- {s.title} ---",
                f"Summary          : {s.subtitle}",
                f"1. Scan / Cart   : cart={s.cart_reference} | item={s.item_name} "
                f"| total={s.currency} {s.eligible_total} | cost={s.currency} "
                f"{s.eligible_unit_cost_total} | email={s.customer_email}",
                f"2. Proposal      : run_id={s.run_id} | lang={s.language} ({s.direction}) "
                f"| action={s.proposed_action} | discount={s.proposed_discount_percent}",
                f"   Policies      : {', '.join(s.policy_sources)}",
                f"   Message       : {s.customer_message}",
                f"3. Safety Gate   : outcome={s.gate_outcome} | reasons=[{reasons}] "
                f"| discount_amt={s.currency} {s.discount_amount} | margin={margin_text}",
                f"   Fingerprint   : {s.gate_fingerprint}",
                f"4. Approval State: status={s.approval_status} "
                f"| actor={s.approval_actor or 'none'} | snapshot_hash={s.snapshot_hash}",
                f"   Note          : {s.approval_note}",
                f"5. Execution     : status={s.execution_status} | channel={s.channel} "
                f"| coupon={s.coupon_code or 'none'} | odoo={s.odoo_activity}",
                f"   Destination   : {s.destination_label}"
                + (f" | subject={s.dispatch_subject}" if s.dispatch_subject else ""),
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _badge_class(outcome: str) -> str:
    if outcome == "auto_approve":
        return "badge-ok"
    if outcome == "needs_approval":
        return "badge-warn"
    return "badge-reject"


def render_html(scenarios: list[DemoScenario]) -> str:
    tabs_html: list[str] = []
    panels_html: list[str] = []
    for idx, s in enumerate(scenarios):
        active_cls = " active" if idx == 0 else ""
        esc = html.escape
        tabs_html.append(
            f'<button type="button" class="tab-btn{active_cls}" '
            f'data-target="panel-{esc(s.scenario_id)}" '
            f"onclick=\"selectScenario('{esc(s.scenario_id)}')\">"
            f"{esc(s.title)}</button>"
        )
        reasons_text = ", ".join(s.gate_reasons) if s.gate_reasons else "none (within rules)"
        margin_text = s.margin_after_discount or "n/a (0% or rejected)"
        policies_html = "".join(f"<li><code>{esc(p)}</code></li>" for p in s.policy_sources)
        subject_row = (
            f"\n        <div><span>Subject</span><strong>{esc(s.dispatch_subject)}</strong></div>"
            if s.dispatch_subject
            else ""
        )
        badge_cls = _badge_class(s.gate_outcome)
        coupon_label = s.coupon_code or "none (0% or blocked)"
        dispatch_label = s.dispatch_id or "none"
        panels_html.append(
            f"""
<section id="panel-{esc(s.scenario_id)}" class="scenario-panel{active_cls}">
  <header class="panel-header">
    <div>
      <h2>{esc(s.title)}</h2>
      <p class="subtitle">{esc(s.subtitle)}</p>
    </div>
    <span class="badge {badge_cls}">{esc(s.gate_outcome)}</span>
  </header>

  <div class="stages-grid">
    <article class="stage-card">
      <div class="stage-num">Stage 1</div>
      <h3>Abandoned Cart Scan</h3>
      <dl>
        <div><dt>Cart Reference</dt><dd><code>{esc(s.cart_reference)}</code></dd></div>
        <div>
          <dt>Customer</dt>
          <dd>{esc(s.customer_name)} (<code>{esc(s.customer_email)}</code>)</dd>
        </div>
        <div><dt>Item &amp; Category</dt><dd>{esc(s.item_name)} — {esc(s.category)}</dd></div>
        <div>
          <dt>Total / Unit Cost</dt>
          <dd>
            {esc(s.currency)} {esc(s.eligible_total)} /
            {esc(s.currency)} {esc(s.eligible_unit_cost_total)}
          </dd>
        </div>
        <div><dt>Abandoned Since</dt><dd><code>{esc(s.abandoned_since)}</code></dd></div>
      </dl>
    </article>

    <article class="stage-card">
      <div class="stage-num">Stage 2</div>
      <h3>Policy Retrieval &amp; Proposal</h3>
      <dl>
        <div><dt>Run ID</dt><dd><code>{esc(s.run_id)}</code></dd></div>
        <div>
          <dt>Language / Action</dt>
          <dd>
            <code>{esc(s.language)}</code> ({esc(s.direction)}) —
            <code>{esc(s.proposed_action)}</code>
          </dd>
        </div>
        <div>
          <dt>Proposed Discount</dt>
          <dd><strong>{esc(s.proposed_discount_percent)}</strong></dd>
        </div>
        <div><dt>Grounded Policies</dt><dd><ul class="pill-list">{policies_html}</ul></dd></div>
      </dl>
      <div class="message-box" dir="{esc(s.direction)}" lang="{esc(s.language)}">
        <div class="msg-label">Proposed Customer Message ({esc(s.direction.upper())})</div>
        <p>{esc(s.customer_message)}</p>
      </div>
    </article>

    <article class="stage-card">
      <div class="stage-num">Stage 3</div>
      <h3>Deterministic Safety Gate</h3>
      <dl>
        <div>
          <dt>Outcome</dt>
          <dd><span class="badge {badge_cls}">{esc(s.gate_outcome)}</span></dd>
        </div>
        <div><dt>Reasons</dt><dd><code>{esc(reasons_text)}</code></dd></div>
        <div><dt>Discount Amount</dt><dd>{esc(s.currency)} {esc(s.discount_amount)}</dd></div>
        <div><dt>Margin After Discount</dt><dd>{esc(margin_text)}</dd></div>
        <div>
          <dt>Gate Fingerprint</dt>
          <dd><code class="hash">{esc(s.gate_fingerprint)}</code></dd>
        </div>
      </dl>
    </article>

    <article class="stage-card">
      <div class="stage-num">Stage 4</div>
      <h3>Operator Approval Binding</h3>
      <dl>
        <div><dt>Approval State</dt><dd><code>{esc(s.approval_status)}</code></dd></div>
        <div><dt>Operator Actor</dt><dd><code>{esc(s.approval_actor or "none")}</code></dd></div>
        <div><dt>Snapshot Hash</dt><dd><code class="hash">{esc(s.snapshot_hash)}</code></dd></div>
        <div><dt>Review Note</dt><dd>{esc(s.approval_note)}</dd></div>
      </dl>
    </article>

    <article class="stage-card stage-wide">
      <div class="stage-num">Stage 5</div>
      <h3>Worker Execution &amp; Channel Dispatch Record</h3>
      <div class="dispatch-grid">
        <div>
          <span>Execution Status</span>
          <strong><code>{esc(s.execution_status)}</code></strong>
        </div>
        <div><span>Channel</span><strong><code>{esc(s.channel)}</code></strong></div>
        <div><span>Coupon Code</span><strong><code>{esc(coupon_label)}</code></strong></div>
        <div><span>Odoo Activity</span><strong>{esc(s.odoo_activity)}</strong></div>
        <div><span>Destination</span><strong>{esc(s.destination_label)}</strong></div>{subject_row}
        <div><span>Dispatch ID</span><strong><code>{esc(dispatch_label)}</code></strong></div>
      </div>
    </article>
  </div>
</section>"""
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Odoo Agentic Retention Engine — Local POC Walkthrough</title>
  <style>
    :root {{
      --bg: #f5f7fb;
      --card: #ffffff;
      --ink: #172033;
      --muted: #52607a;
      --border: #d8e0ee;
      --accent: #1d4ed8;
      --ok-bg: #dcfce7;
      --ok-ink: #166534;
      --warn-bg: #fef3c7;
      --warn-ink: #92400e;
      --reject-bg: #fee2e2;
      --reject-ink: #991b1b;
      --banner-bg: #fff7ed;
      --banner-border: #ea580c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      line-height: 1.5;
    }}
    .banner {{
      background: var(--banner-bg);
      border-bottom: 3px solid var(--banner-border);
      padding: 12px 24px;
      font-weight: 700;
      letter-spacing: 0.03em;
      text-align: center;
      color: #9a3412;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px 20px 48px;
    }}
    .hero {{
      margin-bottom: 20px;
    }}
    .hero h1 {{
      margin: 0 0 6px;
      font-size: 1.7rem;
    }}
    .hero p {{
      margin: 0;
      color: var(--muted);
    }}
    .tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 20px 0;
    }}
    .tab-btn {{
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--ink);
      padding: 10px 14px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 0.92rem;
      font-weight: 600;
    }}
    .tab-btn.active {{
      background: var(--accent);
      border-color: var(--accent);
      color: #ffffff;
    }}
    .scenario-panel {{
      display: none;
    }}
    .scenario-panel.active {{
      display: block;
    }}
    .panel-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px 20px;
      margin-bottom: 16px;
    }}
    .panel-header h2 {{
      margin: 0 0 4px;
      font-size: 1.2rem;
    }}
    .subtitle {{
      margin: 0;
      color: var(--muted);
      font-size: 0.94rem;
    }}
    .badge {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 700;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    .badge-ok {{ background: var(--ok-bg); color: var(--ok-ink); }}
    .badge-warn {{ background: var(--warn-bg); color: var(--warn-ink); }}
    .badge-reject {{ background: var(--reject-bg); color: var(--reject-ink); }}
    .stages-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
    }}
    .stage-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px 18px;
    }}
    .stage-wide {{
      grid-column: 1 / -1;
    }}
    .stage-num {{
      font-size: 0.78rem;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--accent);
      margin-bottom: 4px;
    }}
    .stage-card h3 {{
      margin: 0 0 12px;
      font-size: 1.05rem;
    }}
    dl {{
      margin: 0;
      display: grid;
      gap: 8px;
    }}
    dt {{
      font-size: 0.78rem;
      color: var(--muted);
      font-weight: 600;
    }}
    dd {{
      margin: 2px 0 0;
      font-size: 0.92rem;
      word-break: break-word;
    }}
    code {{
      background: #eef2f8;
      padding: 2px 5px;
      border-radius: 4px;
      font-size: 0.85em;
    }}
    code.hash {{
      display: inline-block;
      font-size: 0.76em;
      word-break: break-all;
    }}
    .pill-list {{
      list-style: none;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      padding: 0;
      margin: 4px 0 0;
    }}
    .message-box {{
      margin-top: 12px;
      padding: 12px;
      border-radius: 8px;
      background: #f8fafc;
      border: 1px solid var(--border);
    }}
    .msg-label {{
      font-size: 0.75rem;
      color: var(--muted);
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .message-box p {{
      margin: 0;
    }}
    .dispatch-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }}
    .dispatch-grid div {{
      display: flex;
      flex-direction: column;
      gap: 3px;
      padding: 10px 12px;
      border-radius: 8px;
      background: #f8fafc;
      border: 1px solid var(--border);
    }}
    .dispatch-grid span {{
      font-size: 0.78rem;
      color: var(--muted);
      font-weight: 600;
    }}
    footer {{
      margin-top: 28px;
      padding-top: 16px;
      border-top: 1px solid var(--border);
      color: var(--muted);
      font-size: 0.86rem;
    }}
  </style>
</head>
<body>
  <div class="banner">{html.escape(DISCLAIMER)}</div>
  <main>
    <section class="hero">
      <h1>Odoo Agentic Retention Engine — Local 5-Stage POC Walkthrough</h1>
      <p>
        Offline static stakeholder artifact backed by the deterministic engine gate,
        snapshot hashing, and synthetic English / MSA Arabic (RTL) scenarios.
        No network access, no external services, no live LLM calls.
      </p>
    </section>
    <div class="tabs" role="tablist">
      {"".join(tabs_html)}
    </div>{"".join(panels_html)}
    <footer>
      <strong>{html.escape(DISCLAIMER)}</strong> — Generated by
      <code>scripts/poc3_local_demo.py</code>. For architecture signoff boundaries,
      see <code>docs/architecture/poc-exit-review.md</code>.
    </footer>
  </main>
  <script>
    function selectScenario(id) {{
      var buttons = document.querySelectorAll('.tab-btn');
      var panels = document.querySelectorAll('.scenario-panel');
      buttons.forEach(function(btn) {{
        btn.classList.toggle('active', btn.getAttribute('data-target') === 'panel-' + id);
      }});
      panels.forEach(function(panel) {{
        panel.classList.toggle('active', panel.id === 'panel-' + id);
      }});
    }}
  </script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local static 5-stage POC walkthrough demo")
    parser.add_argument("--scenario", help="Filter to one scenario ID")
    parser.add_argument("--json", action="store_true", help="Print JSON payload instead of text")
    parser.add_argument("--write-html", type=Path, help="Write standalone static HTML to PATH")
    parser.add_argument(
        "--check-html",
        type=Path,
        help="Verify PATH matches generated HTML byte-for-byte",
    )
    args = parser.parse_args(argv)

    scenarios = build_demo_bundle()
    if args.scenario:
        scenarios = [s for s in scenarios if s.scenario_id == args.scenario]
        if not scenarios:
            print(f"Unknown scenario ID: {args.scenario}", file=sys.stderr)
            return 2

    if args.write_html is not None:
        args.write_html.parent.mkdir(parents=True, exist_ok=True)
        args.write_html.write_text(render_html(scenarios), encoding="utf-8")
        print(f"Wrote local static walkthrough to {args.write_html}")
        return 0

    if args.check_html is not None:
        expected = render_html(scenarios)
        if not args.check_html.exists():
            print(f"Missing HTML demo artifact: {args.check_html}", file=sys.stderr)
            return 1
        actual = args.check_html.read_text(encoding="utf-8")
        if actual != expected:
            print(
                f"HTML demo artifact is stale: {args.check_html}. Regenerate with --write-html.",
                file=sys.stderr,
            )
            return 1
        print(f"HTML demo artifact verified: {args.check_html}")
        return 0

    if args.json:
        payload = {
            "disclaimer": DISCLAIMER,
            "scenarios": [asdict(s) for s in scenarios],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print(render_cli(scenarios))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
