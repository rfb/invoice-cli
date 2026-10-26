"""Terminal presentation, built from the same invoice values as the PDF."""
import textwrap

from .core import money, price_display


def show(inv):
    out = [f"INVOICE {inv['id']:06d}  [{inv['status'].upper()}]", ""]
    for label, key in [("From", "business"), ("Bill to", "customer")]:
        party = inv[key]
        out += [f"{label}: {party['name']}", party["address"]]
        if party.get("email"):
            out.append(party["email"])
        if party.get("gst_number"):
            out.append(f"GST registration: {party['gst_number']}")
        out.append("")
    out += [f"Issued: {inv['issue_date']}    Due: {inv['due_date']}    Currency: {inv['currency']}", "",
            f"{'#':>3}  {'Description':<34} {'Quantity / unit':<18} {'Unit price':>14} {'Amount':>17}  Tax"]
    for index, line in enumerate(inv["lines"], 1):
        parts = []
        for physical in line["description"].splitlines():
            parts.extend(textwrap.wrap(physical, width=34) or [""])
        qty = f"{line['quantity']} {line['unit']}"
        tax = line["tax"] + (f" {line['tax_rate']}%" if line["tax"] not in {"exempt", "zero-rated", "none"} else "")
        out.append(f"{index:>3}  {parts[0]:<34} {qty:<18} {price_display(line['unit_price']):>14} {money(line['net_cents']):>17}  {tax}")
        out.extend("     " + part for part in parts[1:])
    t = inv["totals"]
    out += ["", f"Subtotal: {money(t['subtotal_cents'])} {inv['currency']}"]
    out += [f"{tax['label']} ({tax['rate']}%): {money(tax['cents'])}" for tax in t["taxes"]]
    out += [f"TOTAL: {money(t['total_cents'])} {inv['currency']}"]
    if inv["note"]:
        out += ["", "Note:", inv["note"]]
    if inv.get("void_reason"):
        out += ["", "VOID: " + inv["void_reason"]]
    return "\n".join(out)
