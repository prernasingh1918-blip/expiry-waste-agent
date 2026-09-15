"""
Daily Kitchen Waste Report — standalone script (no notebook needed).

What this does, every time it runs:
1. Loads the restaurant's current stock data
2. Runs each batch/prep item through the same deterministic decision rules
   we built and tested in Colab (fast, free, no AI API calls needed for this
   automated version — reliable and won't hit rate limits)
3. Builds a plain-English daily summary
4. Emails it to whoever should see it (e.g. the kitchen manager)

Designed to be run automatically on a schedule (see the GitHub Actions
workflow file), not manually.
"""

import pandas as pd
import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

# ---------------------------------------------------------------
# In production this would read TODAY as the real current date.
# For this demo, we keep the same fixed reference date as the rest
# of the project so results stay consistent with everything we tested.
# ---------------------------------------------------------------
TODAY = pd.Timestamp('2026-09-11')


def load_data():
    raw_df = pd.read_csv('raw_ingredient_batches.csv')
    prep_df = pd.read_csv('prepped_items.csv')
    raw_df['Received_Date'] = pd.to_datetime(raw_df['Received_Date'])
    raw_df['Use_By_Date'] = pd.to_datetime(raw_df['Use_By_Date'])
    prep_df['Prep_DateTime'] = pd.to_datetime(prep_df['Prep_DateTime'])
    prep_df['Use_By_DateTime'] = pd.to_datetime(prep_df['Use_By_DateTime'])
    return raw_df, prep_df


def load_policy_sections(filepath):
    with open(filepath, 'r') as f:
        text = f.read()
    sections = {}
    parts = text.split('\n## ')
    for part in parts[1:]:
        lines = part.split('\n', 1)
        title = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        sections[title] = body
    return sections


def usage_pace_status(row, today):
    total_shelf_days = (row['Use_By_Date'] - row['Received_Date']).days
    days_elapsed = (today - row['Received_Date']).days
    days_remaining = (row['Use_By_Date'] - today).days
    if total_shelf_days <= 0 or row['Quantity_Received'] <= 0:
        return None
    pct_time_elapsed = days_elapsed / total_shelf_days
    pct_qty_used = float(row['Quantity_Used_So_Far']) / float(row['Quantity_Received'])
    pace_ratio = (pct_qty_used / pct_time_elapsed) if pct_time_elapsed > 0 else None
    return {
        "days_remaining": int(days_remaining),
        "pct_time_elapsed": float(round(pct_time_elapsed * 100, 1)),
        "pct_qty_used": float(round(pct_qty_used * 100, 1)),
        "pace_ratio": float(round(pace_ratio, 2)) if pace_ratio is not None else None,
    }


def classify_raw_batch(row, today):
    pace = usage_pace_status(row, today)
    if pace is None:
        return {"action": "ASK", "reason": "Invalid or missing data."}

    category = row['Category']
    days_remaining = pace['days_remaining']
    pace_ratio = pace['pace_ratio']
    days_elapsed = (today - row['Received_Date']).days

    if row['Quantity_Used_So_Far'] == 0 and days_elapsed >= 1:
        return {"action": "ASK", "reason": "No usage logged despite at least a day elapsed."}

    if category != "Dry Goods & Spices":
        if pace_ratio is not None and pace_ratio > 3.5 and pace['pct_time_elapsed'] < 30:
            return {"action": "ASK", "reason": "Usage pace spiked sharply early in shelf life."}

    if days_remaining < 0:
        if category == "Dry Goods & Spices":
            return {"action": "FLAG_FOR_REVIEW", "reason": "Past use-by, dry goods need human review."}
        return {"action": "WRITE_OFF", "reason": f"Past use-by date ({abs(days_remaining)} days expired)."}

    if category == "Seafood" and days_remaining <= 2:
        return {"action": "CONVERT_TO_PREP", "reason": "Seafood within 2-day at-risk window."}

    elif category == "Meat & Poultry" and pace_ratio is not None and pace_ratio < 0.75:
        return {"action": "AT_RISK", "reason": f"Usage pace ({pace_ratio}) suggests <75% will be used in time."}

    elif category == "Dairy":
        total_shelf = (row['Use_By_Date'] - row['Received_Date']).days
        pct_shelf_remaining = (days_remaining / total_shelf * 100) if total_shelf > 0 else 0
        if pct_shelf_remaining <= 20:
            return {"action": "AT_RISK", "reason": f"Only {round(pct_shelf_remaining,1)}% shelf life remaining."}
        if pace_ratio is not None and pace_ratio < 0.5:
            return {"action": "WATCH", "reason": f"Usage pace ({pace_ratio}) behind schedule, early warning."}

    elif category == "Produce" and pace_ratio is not None and pace_ratio < 0.70:
        return {"action": "AT_RISK", "reason": f"Usage pace ({pace_ratio}) suggests <70% will be used in time."}

    elif category == "Dry Goods & Spices" and days_remaining <= 7:
        return {"action": "AT_RISK", "reason": "Within 7 days of use-by."}

    return {"action": "ON_TRACK", "reason": "No risk threshold triggered."}


def classify_prepped_item(prep_row, raw_df, today):
    total_shelf_hours = (prep_row['Use_By_DateTime'] - prep_row['Prep_DateTime']).total_seconds() / 3600
    hours_elapsed = (today - prep_row['Prep_DateTime']).total_seconds() / 3600
    hours_remaining = (prep_row['Use_By_DateTime'] - today).total_seconds() / 3600

    if total_shelf_hours <= 0 or prep_row['Quantity_Prepped'] <= 0:
        return {"action": "ASK", "reason": "Invalid data."}

    pct_time_elapsed = hours_elapsed / total_shelf_hours
    pct_qty_used = prep_row['Quantity_Used_So_Far'] / prep_row['Quantity_Prepped']
    pace_ratio = (pct_qty_used / pct_time_elapsed) if pct_time_elapsed > 0 else None

    source_was_written_off = False
    source_ids = str(prep_row['Made_From_Batch_IDs']).split(';')
    for bid in source_ids:
        source_rows = raw_df[raw_df['Batch_ID'] == bid]
        if len(source_rows) > 0:
            source_row = source_rows.iloc[0]
            source_result = classify_raw_batch(source_row, prep_row['Prep_DateTime'])
            if source_result['action'] == 'WRITE_OFF':
                source_was_written_off = True
                break

    if hours_remaining < 0:
        action = "FLAG_FOR_REVIEW" if prep_row['Category'] == 'Bakery' else "WRITE_OFF"
        reason = "Past use-by."
    elif prep_row['Is_Dairy_Based'] and hours_remaining <= 4:
        action, reason = "AT_RISK", "Dairy-based prep near 24-hour limit."
    elif prep_row['Category'] == 'Bakery' and pace_ratio is not None and pace_ratio < 0.60:
        action, reason = "AT_RISK", "Bakery item below usage threshold."
    elif pace_ratio is not None and pace_ratio < 0.5 and pct_time_elapsed > 0.5:
        action, reason = "AT_RISK", "Usage pace behind schedule, over halfway through shelf life."
    else:
        action, reason = "ON_TRACK", "No risk threshold triggered."

    if source_was_written_off:
        action = "URGENT_REVIEW"
        reason = "Made from an already-expired source ingredient — safety override."

    return {"action": action, "reason": reason}


def build_report(raw_df, prep_df, policy_sections, today):
    raw_df = raw_df.copy()
    prep_df = prep_df.copy()

    raw_df['Result'] = raw_df.apply(lambda r: classify_raw_batch(r, today), axis=1)
    raw_df['Action'] = raw_df['Result'].apply(lambda r: r['action'])
    raw_df['Reason'] = raw_df['Result'].apply(lambda r: r['reason'])

    prep_df['Result'] = prep_df.apply(lambda r: classify_prepped_item(r, raw_df, today), axis=1)
    prep_df['Action'] = prep_df['Result'].apply(lambda r: r['action'])
    prep_df['Reason'] = prep_df['Result'].apply(lambda r: r['reason'])

    needs_attention_raw = raw_df[~raw_df['Action'].isin(['ON_TRACK'])]
    needs_attention_prep = prep_df[~prep_df['Action'].isin(['ON_TRACK'])]

    urgent = pd.concat([
        raw_df[raw_df['Action'].isin(['WRITE_OFF', 'CONVERT_TO_PREP'])],
        prep_df[prep_df['Action'] == 'URGENT_REVIEW']
    ])

    at_risk_value = raw_df[raw_df['Action'].isin(['AT_RISK', 'WRITE_OFF'])].apply(
        lambda r: r['Unit_Cost'] * (r['Quantity_Received'] - r['Quantity_Used_So_Far']), axis=1
    ).sum()

    lines = []
    lines.append(f"KITCHEN DAILY STOCK REPORT — {today.strftime('%d %b %Y')}")
    lines.append("=" * 50)
    lines.append("")
    lines.append(f"Reviewed: {len(raw_df)} raw ingredient batches, {len(prep_df)} prepped items")
    lines.append(f"Need attention today: {len(needs_attention_raw) + len(needs_attention_prep)}")
    lines.append(f"Estimated value at risk: Rs {at_risk_value:,.0f}")
    lines.append("")

    if len(urgent) > 0:
        lines.append("URGENT — ACT TODAY")
        lines.append("-" * 30)
        for _, r in raw_df[raw_df['Action'].isin(['WRITE_OFF', 'CONVERT_TO_PREP'])].iterrows():
            lines.append(f"  [{r['Action']}] {r['Ingredient_Name']} ({r['Batch_ID']}) — {r['Reason']}")
        for _, r in prep_df[prep_df['Action'] == 'URGENT_REVIEW'].iterrows():
            lines.append(f"  [{r['Action']}] {r['Item_Name']} ({r['Prep_ID']}) — {r['Reason']}")
        lines.append("")

    at_risk_rows = raw_df[raw_df['Action'] == 'AT_RISK']
    if len(at_risk_rows) > 0:
        lines.append("AT RISK — WORTH CHECKING")
        lines.append("-" * 30)
        for _, r in at_risk_rows.iterrows():
            lines.append(f"  {r['Ingredient_Name']} ({r['Batch_ID']}) — {r['Reason']}")
        lines.append("")

    watch_rows = raw_df[raw_df['Action'] == 'WATCH']
    if len(watch_rows) > 0:
        lines.append("EARLY WARNING")
        lines.append("-" * 30)
        for _, r in watch_rows.iterrows():
            lines.append(f"  {r['Ingredient_Name']} ({r['Batch_ID']}) — {r['Reason']}")
        lines.append("")

    ask_rows = raw_df[raw_df['Action'] == 'ASK']
    if len(ask_rows) > 0:
        lines.append("NEEDS A HUMAN LOOK — DATA UNCLEAR")
        lines.append("-" * 30)
        for _, r in ask_rows.iterrows():
            lines.append(f"  {r['Ingredient_Name']} ({r['Batch_ID']}) — {r['Reason']}")
        lines.append("")

    lines.append("Everything else is on track — no action needed.")
    return "\n".join(lines)


def send_email(report_text, today):
    sender = os.environ.get('REPORT_SENDER_EMAIL')
    password = os.environ.get('REPORT_SENDER_APP_PASSWORD')
    recipient = os.environ.get('REPORT_RECIPIENT_EMAIL')

    if not all([sender, password, recipient]):
        print("Email credentials not set — skipping send, printing report instead:\n")
        print(report_text)
        return

    msg = MIMEText(report_text)
    msg['Subject'] = f"Kitchen Daily Stock Report — {today.strftime('%d %b %Y')}"
    msg['From'] = sender
    msg['To'] = recipient

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
    print(f"Report emailed to {recipient}")


if __name__ == "__main__":
    raw_df, prep_df = load_data()
    policy_sections = load_policy_sections('kitchen_waste_policy.md')
    report = build_report(raw_df, prep_df, policy_sections, TODAY)
    send_email(report, TODAY)
