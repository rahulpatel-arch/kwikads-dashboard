#!/usr/bin/env python3
"""
sync_dashboard.py
Pulls live KwikAds data from Salesforce and regenerates index.html for the
GitHub Pages dashboard. Designed to run on a schedule via GitHub Actions.

Required environment variables (set as GitHub Secrets):
  SF_USERNAME         - Salesforce login username
  SF_PASSWORD         - Salesforce login password
  SF_SECURITY_TOKEN   - Salesforce security token (Settings > Reset My Security Token)

No credentials are ever written to the repo or the generated HTML.
"""

import os
import sys
from datetime import datetime, date
from collections import defaultdict
from simple_salesforce import Salesforce

def sf_connect():
    username = os.environ.get("SF_USERNAME")
    password = os.environ.get("SF_PASSWORD")
    token = os.environ.get("SF_SECURITY_TOKEN")
    if not all([username, password, token]):
        print("ERROR: Missing SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN environment variables.")
        sys.exit(1)
    return Salesforce(username=username, password=password, security_token=token)

def fmt_currency(n):
    n = int(round(n))
    s = str(n)
    if len(s) <= 3:
        return f"₹{s}"
    last3 = s[-3:]
    rest = s[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return "₹" + ",".join(parts) + "," + last3

def query_all(sf, soql):
    result = sf.query_all(soql)
    return result["records"]

def get_owner_short(full_name):
    if not full_name:
        return "Unassigned"
    mapping = {
        "Rahul Patel": "Rahul",
        "gaurav1 Panchal": "Gaurav",
        "Gaurav Panchal": "Gaurav",
        "Trishun Tripathi": "Trishun",
        "Tushar Joshi": "Tushar",
    }
    return mapping.get(full_name, full_name)

def build_dashboard():
    sf = sf_connect()

    # ---- 1. ALL-TIME GO-LIVE (Till Date) ----
    golive_q = """
        SELECT Account.Name, Owner.Name, CloseDate, Kwik_Ads_Expected_ARR__c
        FROM Opportunity
        WHERE RecordType.Name = 'Kwik Ads' AND StageName = 'Go-Live'
        ORDER BY CloseDate DESC
    """
    golive_records = query_all(sf, golive_q)

    till_date_rows = []
    owner_totals_alltime = defaultdict(lambda: [0, 0])
    total_earr_alltime = 0
    for r in golive_records:
        acct = r["Account"]["Name"] if r.get("Account") else "Unknown"
        owner_full = r["Owner"]["Name"] if r.get("Owner") else None
        owner = get_owner_short(owner_full)
        arr = r.get("Kwik_Ads_Expected_ARR__c") or 0
        close_date = r.get("CloseDate")
        till_date_rows.append((acct, owner, close_date, arr))
        owner_totals_alltime[owner][0] += 1
        owner_totals_alltime[owner][1] += arr
        total_earr_alltime += arr

    # ---- 2. CURRENT MONTH GO-LIVE (dynamic "this month" tab) ----
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    this_month_rows = [row for row in till_date_rows if row[2] and row[2] >= month_start]
    this_month_total = sum(row[3] for row in this_month_rows)
    this_month_owner_totals = defaultdict(lambda: [0, 0])
    for _, owner, _, arr in this_month_rows:
        this_month_owner_totals[owner][0] += 1
        this_month_owner_totals[owner][1] += arr

    # ---- 3. ACTIVE PIPELINE (Pre Audit + Audit Done) ----
    pipeline_q = """
        SELECT Owner.Name, StageName, Kwik_Ads_Expected_ARR__c
        FROM Opportunity
        WHERE RecordType.Name = 'Kwik Ads' AND StageName IN ('Pre Audit', 'Audit Done')
    """
    pipeline_records = query_all(sf, pipeline_q)
    pipeline_by_owner = defaultdict(lambda: [0, 0])
    pipeline_total_count = 0
    pipeline_total_arr = 0
    for r in pipeline_records:
        owner = get_owner_short(r["Owner"]["Name"] if r.get("Owner") else None)
        arr = r.get("Kwik_Ads_Expected_ARR__c") or 0
        pipeline_by_owner[owner][0] += 1
        pipeline_by_owner[owner][1] += arr
        pipeline_total_count += 1
        pipeline_total_arr += arr

    # ---- 4. MQL LEAD FUNNEL (this month) ----
    lead_q = f"""
        SELECT Id, LeadSource, Status, IsConverted, CreatedDate
        FROM Lead
        WHERE CreatedDate >= {month_start}T00:00:00Z
    """
    lead_records = query_all(sf, lead_q)
    total_leads = len(lead_records)
    unqualified = sum(1 for r in lead_records if r.get("Status") == "Unqualified")
    converted = sum(1 for r in lead_records if r.get("IsConverted"))

    # ---- Build HTML ----
    generated_at = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")

    def render_till_date_rows():
        html = ""
        for acct, owner, close_date, arr in till_date_rows:
            html += f"<tr><td>{acct}</td><td>{owner}</td><td>{close_date or '-'}</td><td class='num-cell'>{fmt_currency(arr)}</td></tr>\n"
        return html

    def render_owner_cards(owner_totals, total_label):
        html = ""
        for owner, (count, arr) in sorted(owner_totals.items(), key=lambda x: -x[1][1]):
            initials = "".join(w[0] for w in owner.split()[:2]).upper()
            html += f"""
            <div class="owner-card">
              <div class="initials">{initials}</div>
              <div class="name">{owner}</div>
              <div class="count">{count} brands live</div>
              <div class="arr">{fmt_currency(arr)}</div>
            </div>"""
        return html

    def render_pipeline_table():
        html = ""
        for owner, (count, arr) in sorted(pipeline_by_owner.items(), key=lambda x: -x[1][1]):
            html += f"<tr><td>{owner}</td><td class='center-cell'>{count}</td><td class='num-cell'>{fmt_currency(arr)}</td></tr>\n"
        return html

    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kwik Ads Dashboard — Live from Salesforce</title>
<style>
  :root {{ --navy:#1E2761; --gold:#C98A2C; --slate:#3A3F55; --bg:#F7F8FC; --ice:#CADCFC; --border:#E5E8EF; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Calibri,sans-serif; background:var(--bg); color:var(--slate); }}
  header {{ background:var(--navy); color:white; padding:24px 32px; }}
  header .brand {{ font-size:13px; letter-spacing:2px; color:var(--gold); font-weight:700; text-transform:uppercase; }}
  header h1 {{ font-family:Georgia,serif; font-size:28px; margin:6px 0; }}
  header .meta {{ font-size:13px; color:var(--ice); }}
  main {{ padding:28px 32px; max-width:1100px; margin:0 auto; }}
  h2 {{ font-family:Georgia,serif; color:var(--navy); font-size:20px; margin:24px 0 14px; }}
  .stat-row {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:20px; }}
  .stat-card {{ background:white; border-radius:10px; box-shadow:0 2px 8px rgba(30,39,97,0.08); padding:16px 20px; flex:1; min-width:140px; }}
  .stat-card .num {{ font-size:24px; font-weight:700; color:var(--navy); font-family:Georgia,serif; }}
  .stat-card .label {{ font-size:12px; color:#8891A3; margin-top:4px; }}
  .owner-row {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:20px; }}
  .owner-card {{ background:white; border-radius:10px; box-shadow:0 2px 8px rgba(30,39,97,0.08); padding:14px 18px; flex:1; min-width:150px; }}
  .owner-card .initials {{ display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; border-radius:50%; background:var(--navy); color:white; font-size:11px; font-weight:700; margin-bottom:6px; }}
  .owner-card .name {{ font-weight:700; color:var(--navy); font-size:13px; }}
  .owner-card .count {{ font-size:12px; color:var(--slate); margin:3px 0; }}
  .owner-card .arr {{ font-size:14px; font-weight:700; color:var(--gold); }}
  table {{ width:100%; border-collapse:collapse; background:white; border-radius:10px; overflow:hidden; box-shadow:0 2px 8px rgba(30,39,97,0.06); font-size:13px; margin-bottom:20px; }}
  th {{ background:var(--navy); color:white; text-align:left; padding:9px 12px; font-size:12px; }}
  td {{ padding:8px 12px; border-bottom:1px solid var(--border); }}
  .num-cell {{ text-align:right; }}
  .center-cell {{ text-align:center; }}
  footer {{ text-align:center; padding:20px; font-size:11.5px; color:#8891A3; }}
</style>
</head>
<body>
<header>
  <div class="brand">GoKwik · Kwik Ads</div>
  <h1>Kwik Ads Dashboard</h1>
  <div class="meta">Live from Salesforce · Auto-synced {generated_at}</div>
</header>
<main>
  <h2>Total Go-Live — All Time</h2>
  <div class="stat-row">
    <div class="stat-card"><div class="num">{len(till_date_rows)}</div><div class="label">Total Go-Live</div></div>
    <div class="stat-card"><div class="num">{fmt_currency(total_earr_alltime)}</div><div class="label">Total EARR</div></div>
  </div>
  <div class="owner-row">
    {render_owner_cards(owner_totals_alltime, "All Time")}
  </div>

  <h2>This Month — Go-Live</h2>
  <div class="stat-row">
    <div class="stat-card"><div class="num">{len(this_month_rows)}</div><div class="label">Go-Live ({today.strftime('%B %Y')})</div></div>
    <div class="stat-card"><div class="num">{fmt_currency(this_month_total)}</div><div class="label">EARR ({today.strftime('%B')})</div></div>
  </div>
  <div class="owner-row">
    {render_owner_cards(this_month_owner_totals, "This Month")}
  </div>

  <h2>Active Pipeline (Pre Audit + Audit Done)</h2>
  <div class="stat-row">
    <div class="stat-card"><div class="num">{pipeline_total_count}</div><div class="label">Active Brands</div></div>
    <div class="stat-card"><div class="num">{fmt_currency(pipeline_total_arr)}</div><div class="label">Pipeline EARR</div></div>
  </div>
  <table>
    <tr><th>Owner</th><th class="center-cell">Brands</th><th style="text-align:right">EARR</th></tr>
    {render_pipeline_table()}
  </table>

  <h2>MQL Lead Funnel — {today.strftime('%B %Y')} MTD</h2>
  <div class="stat-row">
    <div class="stat-card"><div class="num">{total_leads}</div><div class="label">Total Leads</div></div>
    <div class="stat-card"><div class="num">{converted}</div><div class="label">Converted</div></div>
    <div class="stat-card"><div class="num">{unqualified}</div><div class="label">Unqualified</div></div>
  </div>

  <h2>All Go-Live Brands — Till Date</h2>
  <table>
    <tr><th>Brand</th><th>Owner</th><th>Go-Live Date</th><th style="text-align:right">EARR</th></tr>
    {render_till_date_rows()}
  </table>
</main>
<footer>GoKwik · Kwik Ads · Live from Salesforce · Auto-synced {generated_at} · Never manually edit this file — it is overwritten on every sync</footer>
</body>
</html>
"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_template)
    print(f"Dashboard regenerated successfully at {generated_at}")
    print(f"Till Date: {len(till_date_rows)} brands, {fmt_currency(total_earr_alltime)}")
    print(f"This month: {len(this_month_rows)} brands, {fmt_currency(this_month_total)}")

if __name__ == "__main__":
    build_dashboard()
