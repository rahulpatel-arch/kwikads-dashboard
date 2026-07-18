#!/usr/bin/env python3
"""
sync_dashboard.py
Pulls live KwikAds data from Salesforce (filtered to Rahul's team only) and
regenerates index.html for the GitHub Pages dashboard, organized by quarter
with JAS as the focused/default view. Runs on a schedule via GitHub Actions.

Required environment variables (GitHub Secrets):
  SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN
"""

import os
import sys
from datetime import datetime, date
from collections import defaultdict
from simple_salesforce import Salesforce

# ---- TEAM FILTER: only these owners are ever included ----
TEAM_OWNERS = {
    "Rahul Patel": "Rahul",
    "gaurav1 Panchal": "Gaurav",
    "Gaurav Panchal": "Gaurav",
    "Trishun Tripathi": "Trishun",
    "Tushar Joshi": "Tushar",
}

QUARTER_MONTHS = {
    "JFM": [1, 2, 3],
    "AMJ": [4, 5, 6],
    "JAS": [7, 8, 9],
    "OND": [10, 11, 12],
}
FOCUS_QUARTER = "JAS"
FOCUS_YEAR = 2026


def sf_connect():
    username = os.environ.get("SF_USERNAME")
    password = os.environ.get("SF_PASSWORD")
    token = os.environ.get("SF_SECURITY_TOKEN")
    if not all([username, password, token]):
        print("ERROR: Missing SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN environment variables.")
        sys.exit(1)
    return Salesforce(username=username, password=password, security_token=token)


def fmt_currency(n):
    n = int(round(n or 0))
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
    return sf.query_all(soql)["records"]


def owner_short(full_name):
    if not full_name:
        return None
    return TEAM_OWNERS.get(full_name)


def which_quarter(close_date_str):
    if not close_date_str:
        return None, None
    d = datetime.strptime(close_date_str, "%Y-%m-%d").date()
    for q, months in QUARTER_MONTHS.items():
        if d.month in months:
            return q, d.year
    return None, None


def build_dashboard():
    sf = sf_connect()
    owner_names_sql = "','".join(sorted(set(TEAM_OWNERS.keys())))

    golive_q = f"""
        SELECT Account.Name, Owner.Name, CloseDate, Kwik_Ads_Expected_ARR__c
        FROM Opportunity
        WHERE RecordType.Name = 'Kwik Ads'
          AND StageName = 'Go-Live'
          AND Owner.Name IN ('{owner_names_sql}')
        ORDER BY CloseDate DESC
    """
    golive_records = query_all(sf, golive_q)

    till_date_rows = []
    owner_totals_alltime = defaultdict(lambda: [0, 0])
    total_earr_alltime = 0
    quarter_buckets = defaultdict(lambda: {"rows": [], "owner_totals": defaultdict(lambda: [0, 0]), "total": 0})

    for r in golive_records:
        acct = r["Account"]["Name"] if r.get("Account") else "Unknown"
        owner_full = r["Owner"]["Name"] if r.get("Owner") else None
        owner = owner_short(owner_full)
        if owner is None:
            continue
        arr = r.get("Kwik_Ads_Expected_ARR__c") or 0
        close_date = r.get("CloseDate")

        till_date_rows.append((acct, owner, close_date, arr))
        owner_totals_alltime[owner][0] += 1
        owner_totals_alltime[owner][1] += arr
        total_earr_alltime += arr

        q, y = which_quarter(close_date)
        if q:
            key = f"{q}-{y}"
            quarter_buckets[key]["rows"].append((acct, owner, close_date, arr))
            quarter_buckets[key]["owner_totals"][owner][0] += 1
            quarter_buckets[key]["owner_totals"][owner][1] += arr
            quarter_buckets[key]["total"] += arr

    focus_key = f"{FOCUS_QUARTER}-{FOCUS_YEAR}"
    focus_data = quarter_buckets.get(focus_key, {"rows": [], "owner_totals": {}, "total": 0})

    focus_month_buckets = defaultdict(lambda: {"rows": [], "total": 0})
    for acct, owner, close_date, arr in focus_data["rows"]:
        month = datetime.strptime(close_date, "%Y-%m-%d").strftime("%B")
        focus_month_buckets[month]["rows"].append((acct, owner, close_date, arr))
        focus_month_buckets[month]["total"] += arr

    pipeline_q = f"""
        SELECT Owner.Name, StageName, Kwik_Ads_Expected_ARR__c
        FROM Opportunity
        WHERE RecordType.Name = 'Kwik Ads'
          AND StageName IN ('Pre Audit', 'Audit Done')
          AND Owner.Name IN ('{owner_names_sql}')
    """
    pipeline_records = query_all(sf, pipeline_q)
    pipeline_by_owner = defaultdict(lambda: [0, 0])
    pipeline_total_count = 0
    pipeline_total_arr = 0
    for r in pipeline_records:
        owner = owner_short(r["Owner"]["Name"] if r.get("Owner") else None)
        if owner is None:
            continue
        arr = r.get("Kwik_Ads_Expected_ARR__c") or 0
        pipeline_by_owner[owner][0] += 1
        pipeline_by_owner[owner][1] += arr
        pipeline_total_count += 1
        pipeline_total_arr += arr

    today = date.today()
    month_start = today.replace(day=1).isoformat()
    lead_q = f"""
        SELECT Id, LeadSource, Status, IsConverted, Owner.Name, CreatedDate
        FROM Lead
        WHERE CreatedDate >= {month_start}T00:00:00Z
          AND Owner.Name IN ('{owner_names_sql}')
    """
    lead_records = query_all(sf, lead_q)
    total_leads = len(lead_records)
    unqualified = sum(1 for r in lead_records if r.get("Status") == "Unqualified")
    converted = sum(1 for r in lead_records if r.get("IsConverted"))

    generated_at = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")

    def render_brand_rows(rows):
        html = ""
        for acct, owner, close_date, arr in rows:
            html += f"<tr><td>{acct}</td><td>{owner}</td><td>{close_date or '-'}</td><td class='num-cell'>{fmt_currency(arr)}</td></tr>\n"
        return html

    def render_owner_cards(owner_totals):
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

    def render_owner_table(owner_totals, total_count, total_arr):
        html = "<table><tr><th>Owner</th><th class='center-cell'>Brands</th><th style='text-align:right'>EARR</th></tr>\n"
        for owner, (count, arr) in sorted(owner_totals.items(), key=lambda x: -x[1][1]):
            html += f"<tr><td>{owner}</td><td class='center-cell'>{count}</td><td class='num-cell'>{fmt_currency(arr)}</td></tr>\n"
        html += f"<tr class='total-row'><td>Total</td><td class='center-cell'>{total_count}</td><td class='num-cell'>{fmt_currency(total_arr)}</td></tr>\n"
        html += "</table>"
        return html

    def render_month_sections():
        html = ""
        for month in ["July", "August", "September"]:
            data = focus_month_buckets.get(month)
            if not data or not data["rows"]:
                html += f"""
                <div class="empty-state">
                  <div class="icon">🔮</div>
                  <b>{month} {FOCUS_YEAR} — Not started yet</b>
                  <p style="margin:4px 0 0">Data will appear here once brands go live this month</p>
                </div>"""
                continue
            html += f"<h2>{month} {FOCUS_YEAR} Go-Live — {len(data['rows'])} Brands · {fmt_currency(data['total'])}</h2>"
            html += "<table><tr><th>Brand</th><th>Owner</th><th>Date</th><th style='text-align:right'>EARR</th></tr>"
            html += render_brand_rows(data["rows"])
            html += f"<tr class='total-row'><td colspan='3'>{month} Total</td><td class='num-cell'>{fmt_currency(data['total'])}</td></tr></table>"
        return html

    other_quarters_html = ""
    for key, data in sorted(quarter_buckets.items()):
        if key == focus_key or not data["rows"]:
            continue
        q, y = key.split("-")
        other_quarters_html += f"<h2>{q} {y} — {len(data['rows'])} Brands · {fmt_currency(data['total'])}</h2>"
        other_quarters_html += render_owner_table(data["owner_totals"], len(data["rows"]), data["total"])

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kwik Ads Dashboard — Live from Salesforce</title>
<style>
  :root {{ --navy:#1E2761; --navy-light:#2A3480; --gold:#C98A2C; --slate:#3A3F55; --bg:#F7F8FC; --ice:#CADCFC; --border:#E5E8EF; --white:#FFFFFF; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Calibri,sans-serif; background:var(--bg); color:var(--slate); }}
  header {{ background:var(--navy); color:white; padding:24px 32px; }}
  header .brand {{ font-size:13px; letter-spacing:2px; color:var(--gold); font-weight:700; text-transform:uppercase; }}
  header h1 {{ font-family:Georgia,serif; font-size:26px; margin:6px 0; }}
  header .meta {{ font-size:13px; color:var(--ice); }}
  nav {{ display:flex; gap:4px; background:var(--navy-light); padding:0 32px; overflow-x:auto; }}
  nav button {{ background:none; border:none; color:var(--ice); padding:14px 16px; font-size:13.5px; font-weight:600; cursor:pointer; border-bottom:3px solid transparent; white-space:nowrap; }}
  nav button.active {{ color:white; border-bottom-color:var(--gold); }}
  main {{ padding:26px 32px; max-width:1150px; margin:0 auto; }}
  .tab-content {{ display:none; }}
  .tab-content.active {{ display:block; }}
  h2 {{ font-family:Georgia,serif; color:var(--navy); font-size:19px; margin:22px 0 12px; }}
  .stat-row {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:18px; }}
  .stat-card {{ background:white; border-radius:10px; box-shadow:0 2px 8px rgba(30,39,97,0.08); padding:14px 18px; flex:1; min-width:130px; }}
  .stat-card .num {{ font-size:22px; font-weight:700; color:var(--navy); font-family:Georgia,serif; }}
  .stat-card .label {{ font-size:11.5px; color:#8891A3; margin-top:3px; }}
  .owner-row {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:18px; }}
  .owner-card {{ background:white; border-radius:10px; box-shadow:0 2px 8px rgba(30,39,97,0.08); padding:12px 16px; flex:1; min-width:140px; }}
  .owner-card .initials {{ display:inline-flex; align-items:center; justify-content:center; width:26px; height:26px; border-radius:50%; background:var(--navy); color:white; font-size:10.5px; font-weight:700; margin-bottom:5px; }}
  .owner-card .name {{ font-weight:700; color:var(--navy); font-size:12.5px; }}
  .owner-card .count {{ font-size:11.5px; color:var(--slate); margin:3px 0; }}
  .owner-card .arr {{ font-size:13px; font-weight:700; color:var(--gold); }}
  table {{ width:100%; border-collapse:collapse; background:white; border-radius:10px; overflow:hidden; box-shadow:0 2px 8px rgba(30,39,97,0.06); font-size:12.5px; margin-bottom:18px; }}
  th {{ background:var(--navy); color:white; text-align:left; padding:8px 12px; font-size:11.5px; }}
  td {{ padding:7px 12px; border-bottom:1px solid var(--border); }}
  tr.total-row td {{ font-weight:700; background:var(--ice); color:var(--navy); }}
  .num-cell {{ text-align:right; }}
  .center-cell {{ text-align:center; }}
  .empty-state {{ background:white; border-radius:10px; padding:28px; text-align:center; color:#8891A3; margin-bottom:18px; }}
  .empty-state .icon {{ font-size:28px; margin-bottom:6px; }}
  .badge {{ display:inline-block; background:var(--gold); color:white; font-size:10.5px; font-weight:700; padding:2px 8px; border-radius:12px; margin-left:8px; }}
  footer {{ text-align:center; padding:20px; font-size:11.5px; color:#8891A3; }}
</style>
</head>
<body>
<header>
  <div class="brand">GoKwik · Kwik Ads</div>
  <h1>Kwik Ads Dashboard <span class="badge">Live · Team Only</span></h1>
  <div class="meta">Rahul Patel · rahul.patel@gokwik.co · Auto-synced {generated_at}</div>
</header>
<nav>
  <button class="tab-btn active" data-tab="jas">JAS {FOCUS_YEAR} (Focus)</button>
  <button class="tab-btn" data-tab="tilldate">Till Date</button>
  <button class="tab-btn" data-tab="otherq">Other Quarters</button>
  <button class="tab-btn" data-tab="pipeline">Active Pipeline</button>
  <button class="tab-btn" data-tab="leadfunnel">Lead Funnel</button>
</nav>
<main>

  <div class="tab-content active" id="jas">
    <h2>JAS {FOCUS_YEAR} — Total Go-Live (Team Only)</h2>
    <div class="stat-row">
      <div class="stat-card"><div class="num">{len(focus_data['rows'])}</div><div class="label">Total Go-Live (JAS)</div></div>
      <div class="stat-card"><div class="num">{fmt_currency(focus_data['total'])}</div><div class="label">Total EARR (JAS)</div></div>
    </div>
    <div class="owner-row">
      {render_owner_cards(focus_data['owner_totals'])}
    </div>
    {render_month_sections()}
  </div>

  <div class="tab-content" id="tilldate">
    <h2>Total Go-Live — All Time (Team Only)</h2>
    <div class="stat-row">
      <div class="stat-card"><div class="num">{len(till_date_rows)}</div><div class="label">Total Go-Live</div></div>
      <div class="stat-card"><div class="num">{fmt_currency(total_earr_alltime)}</div><div class="label">Total EARR</div></div>
    </div>
    <div class="owner-row">
      {render_owner_cards(owner_totals_alltime)}
    </div>
    <h2>All Go-Live Brands — Till Date</h2>
    <table>
      <tr><th>Brand</th><th>Owner</th><th>Go-Live Date</th><th style="text-align:right">EARR</th></tr>
      {render_brand_rows(till_date_rows)}
      <tr class="total-row"><td colspan="3">Total EARR (Till Date)</td><td class="num-cell">{fmt_currency(total_earr_alltime)}</td></tr>
    </table>
  </div>

  <div class="tab-content" id="otherq">
    <h2>Other Quarters (Team Only)</h2>
    {other_quarters_html if other_quarters_html else '<div class="empty-state"><div class="icon">📭</div><b>No other quarters with go-lives yet</b></div>'}
  </div>

  <div class="tab-content" id="pipeline">
    <h2>Active Pipeline — Pre Audit + Audit Done (Team Only)</h2>
    <div class="stat-row">
      <div class="stat-card"><div class="num">{pipeline_total_count}</div><div class="label">Active Brands</div></div>
      <div class="stat-card"><div class="num">{fmt_currency(pipeline_total_arr)}</div><div class="label">Pipeline EARR</div></div>
    </div>
    {render_owner_table(pipeline_by_owner, pipeline_total_count, pipeline_total_arr)}
  </div>

  <div class="tab-content" id="leadfunnel">
    <h2>MQL Lead Funnel — {today.strftime('%B %Y')} MTD (Team Only)</h2>
    <div class="stat-row">
      <div class="stat-card"><div class="num">{total_leads}</div><div class="label">Total Leads</div></div>
      <div class="stat-card"><div class="num">{converted}</div><div class="label">Converted</div></div>
      <div class="stat-card"><div class="num">{unqualified}</div><div class="label">Unqualified</div></div>
    </div>
  </div>

</main>
<footer>GoKwik · Kwik Ads · Live from Salesforce · Auto-synced {generated_at} · Filtered to team: {', '.join(sorted(set(TEAM_OWNERS.values())))} · Never manually edit this file — it is overwritten on every sync</footer>
<script>
  document.querySelectorAll('.tab-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(btn.dataset.tab).classList.add('active');
    }});
  }});
</script>
</body>
</html>
"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"Dashboard regenerated at {generated_at}")
    print(f"Till Date (team only): {len(till_date_rows)} brands, {fmt_currency(total_earr_alltime)}")
    print(f"JAS {FOCUS_YEAR} focus: {len(focus_data['rows'])} brands, {fmt_currency(focus_data['total'])}")
    print(f"Team filter applied: {sorted(set(TEAM_OWNERS.values()))}")


if __name__ == "__main__":
    build_dashboard()
