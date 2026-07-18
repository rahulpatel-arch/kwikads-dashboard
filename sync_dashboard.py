#!/usr/bin/env python3
"""
sync_dashboard.py
Pulls live KwikAds data from Salesforce (filtered to Rahul's team only) and
regenerates index.html for the GitHub Pages dashboard: JAS quarter focus,
Target vs Achievement (8 Cr goal), monthly audit/pitch counts, conversion
rate, and MQL lead funnel breakdown. Runs on a schedule via GitHub Actions.

Required environment variables (GitHub Secrets):
  SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN
"""

import os
import sys
import json
from datetime import datetime, date
from collections import defaultdict
from simple_salesforce import Salesforce

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
JAS_TARGET = 8_00_00_000  # Rs 8 Cr


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


def bucket_lead_status(status, is_converted):
    if is_converted:
        return "Converted"
    if not status:
        return "Open"
    s = status.lower()
    if "unqualified" in s:
        return "Unqualified"
    if "could not connect" in s or "no connect" in s or "not reachable" in s:
        return "Could Not Connect"
    if "contact" in s or "pitch" in s:
        return "Contacted"
    return "Open"


def build_dashboard():
    sf = sf_connect()
    owner_names_sql = "','".join(sorted(set(TEAM_OWNERS.keys())))

    # ---- ALL-TIME GO-LIVE (Till Date), team only ----
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
        owner = owner_short(r["Owner"]["Name"] if r.get("Owner") else None)
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

    # ---- ACTIVE PIPELINE (Pre Audit + Audit Done), team only ----
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

    # ---- PITCHES THIS MONTH: opportunities created this month that reached Pitch or beyond ----
    pitch_q = f"""
        SELECT Id
        FROM Opportunity
        WHERE RecordType.Name = 'Kwik Ads'
          AND Owner.Name IN ('{owner_names_sql}')
          AND CreatedDate >= {month_start}T00:00:00Z
          AND StageName != 'Prospecting'
    """
    pitches_this_month = len(query_all(sf, pitch_q))

    # ---- AUDITS THIS MONTH: opportunities created this month that reached Audit Done or beyond ----
    audit_q = f"""
        SELECT Id
        FROM Opportunity
        WHERE RecordType.Name = 'Kwik Ads'
          AND Owner.Name IN ('{owner_names_sql}')
          AND CreatedDate >= {month_start}T00:00:00Z
          AND StageName IN ('Audit Done', 'Agreement Signed', 'Go-Live')
    """
    audits_this_month = len(query_all(sf, audit_q))

    golives_this_month = sum(1 for _, _, cd, _ in till_date_rows if cd and cd >= month_start)
    conversion_rate = (golives_this_month / audits_this_month * 100) if audits_this_month else 0

    # ---- MQL LEAD FUNNEL (this month), team only ----
    lead_q = f"""
        SELECT Id, Status, IsConverted
        FROM Lead
        WHERE CreatedDate >= {month_start}T00:00:00Z
          AND Owner.Name IN ('{owner_names_sql}')
    """
    lead_records = query_all(sf, lead_q)
    lead_buckets = defaultdict(int)
    for r in lead_records:
        b = bucket_lead_status(r.get("Status"), r.get("IsConverted"))
        lead_buckets[b] += 1
    total_leads = len(lead_records)

    generated_at = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")

    # Chart.js is embedded inline (not loaded from a CDN) so the dashboard
    # never depends on an external script load succeeding at view-time.
    chartjs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chartjs.min.js")
    with open(chartjs_path, "r", encoding="utf-8") as f:
        chartjs_source = f.read()

    # ---------------- HTML RENDER HELPERS ----------------
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

    # ---- Chart data (JS-side Chart.js) ----
    owner_labels = [o for o, _ in sorted(focus_data["owner_totals"].items(), key=lambda x: -x[1][1])] if focus_data["owner_totals"] else []
    owner_values = [focus_data["owner_totals"][o][1] for o in owner_labels]

    lead_labels = ["Unqualified", "Open", "Contacted", "Could Not Connect", "Converted"]
    lead_values = [lead_buckets.get(l, 0) for l in lead_labels]

    target_progress_pct = round(min(focus_data["total"] / JAS_TARGET * 100, 100), 1) if JAS_TARGET else 0

    chart_data_json = json.dumps({
        "targetVsAchieved": {"labels": ["JAS Target", "Achieved So Far"], "values": [JAS_TARGET, focus_data["total"]]},
        "byOwner": {"labels": owner_labels, "values": owner_values},
        "leadFunnel": {"labels": lead_labels, "values": lead_values},
    })

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kwik Ads Dashboard — Live from Salesforce</title>
<script>
{chartjs_source}
</script>
<style>
  :root {{ --navy:#1E2761; --navy-light:#2A3480; --gold:#C98A2C; --slate:#3A3F55; --bg:#F7F8FC; --ice:#CADCFC; --border:#E5E8EF; --white:#FFFFFF; --green:#1F7A1F; --red:#B33A3A; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Calibri,sans-serif; background:var(--bg); color:var(--slate); }}
  header {{ background:linear-gradient(135deg, var(--navy) 0%, #263480 100%); color:white; padding:26px 32px; }}
  header .brand {{ font-size:13px; letter-spacing:2px; color:var(--gold); font-weight:700; text-transform:uppercase; }}
  header h1 {{ font-family:Georgia,serif; font-size:27px; margin:6px 0; }}
  header .meta {{ font-size:13px; color:var(--ice); }}
  nav {{ display:flex; gap:4px; background:var(--navy-light); padding:0 32px; overflow-x:auto; }}
  nav button {{ background:none; border:none; color:var(--ice); padding:14px 16px; font-size:13.5px; font-weight:600; cursor:pointer; border-bottom:3px solid transparent; white-space:nowrap; }}
  nav button.active {{ color:white; border-bottom-color:var(--gold); }}
  main {{ padding:26px 32px; max-width:1180px; margin:0 auto; }}
  .tab-content {{ display:none; }}
  .tab-content.active {{ display:block; }}
  h2 {{ font-family:Georgia,serif; color:var(--navy); font-size:19px; margin:22px 0 12px; }}
  .stat-row {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:18px; }}
  .stat-card {{ background:white; border-radius:10px; box-shadow:0 2px 10px rgba(30,39,97,0.09); padding:14px 18px; flex:1; min-width:125px; }}
  .stat-card .num {{ font-size:22px; font-weight:700; color:var(--navy); font-family:Georgia,serif; }}
  .stat-card .label {{ font-size:11.5px; color:#8891A3; margin-top:3px; }}
  .owner-row {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:18px; }}
  .owner-card {{ background:white; border-radius:10px; box-shadow:0 2px 10px rgba(30,39,97,0.09); padding:12px 16px; flex:1; min-width:140px; }}
  .owner-card .initials {{ display:inline-flex; align-items:center; justify-content:center; width:26px; height:26px; border-radius:50%; background:var(--navy); color:white; font-size:10.5px; font-weight:700; margin-bottom:5px; }}
  .owner-card .name {{ font-weight:700; color:var(--navy); font-size:12.5px; }}
  .owner-card .count {{ font-size:11.5px; color:var(--slate); margin:3px 0; }}
  .owner-card .arr {{ font-size:13px; font-weight:700; color:var(--gold); }}
  table {{ width:100%; border-collapse:collapse; background:white; border-radius:10px; overflow:hidden; box-shadow:0 2px 10px rgba(30,39,97,0.07); font-size:12.5px; margin-bottom:18px; }}
  th {{ background:var(--navy); color:white; text-align:left; padding:8px 12px; font-size:11.5px; }}
  td {{ padding:7px 12px; border-bottom:1px solid var(--border); }}
  tr.total-row td {{ font-weight:700; background:var(--ice); color:var(--navy); }}
  .num-cell {{ text-align:right; }}
  .center-cell {{ text-align:center; }}
  .empty-state {{ background:white; border-radius:10px; padding:28px; text-align:center; color:#8891A3; margin-bottom:18px; }}
  .empty-state .icon {{ font-size:28px; margin-bottom:6px; }}
  .badge {{ display:inline-block; background:var(--gold); color:white; font-size:10.5px; font-weight:700; padding:2px 8px; border-radius:12px; margin-left:8px; }}
  .chart-card {{ background:white; border-radius:12px; box-shadow:0 2px 10px rgba(30,39,97,0.09); padding:18px 20px; margin-bottom:20px; }}
  .chart-row {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-bottom:18px; }}
  @media (max-width:800px) {{ .chart-row {{ grid-template-columns:1fr; }} }}
  .progress-wrap {{ background:#EDEFF5; border-radius:20px; height:26px; overflow:hidden; margin:10px 0; position:relative; }}
  .progress-fill {{ background:linear-gradient(90deg, var(--gold), #E0A84A); height:100%; display:flex; align-items:center; justify-content:flex-end; padding-right:10px; color:white; font-size:11.5px; font-weight:700; transition:width 0.6s ease; }}
  .target-summary {{ display:flex; gap:20px; flex-wrap:wrap; margin-top:12px; }}
  .target-summary div {{ flex:1; min-width:120px; }}
  .target-summary .big {{ font-size:20px; font-weight:700; color:var(--navy); font-family:Georgia,serif; }}
  .target-summary .lbl {{ font-size:11px; color:#8891A3; }}
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

    <div class="chart-card">
      <h2 style="margin-top:0">🎯 JAS {FOCUS_YEAR} — Target vs Achievement</h2>
      <div class="progress-wrap">
        <div class="progress-fill" style="width:{target_progress_pct}%">{target_progress_pct}%</div>
      </div>
      <div class="target-summary">
        <div><div class="big">{fmt_currency(JAS_TARGET)}</div><div class="lbl">Target (JAS)</div></div>
        <div><div class="big" style="color:var(--gold)">{fmt_currency(focus_data['total'])}</div><div class="lbl">Achieved So Far</div></div>
        <div><div class="big" style="color:var(--red)">{fmt_currency(max(JAS_TARGET - focus_data['total'], 0))}</div><div class="lbl">Gap Remaining</div></div>
      </div>
      <canvas id="targetChart" height="90"></canvas>
    </div>

    <div class="stat-row">
      <div class="stat-card"><div class="num">{len(focus_data['rows'])}</div><div class="label">Total Go-Live (JAS)</div></div>
      <div class="stat-card"><div class="num">{pitches_this_month}</div><div class="label">Pitches ({today.strftime('%B')})</div></div>
      <div class="stat-card"><div class="num">{audits_this_month}</div><div class="label">Audits ({today.strftime('%B')})</div></div>
      <div class="stat-card"><div class="num">{conversion_rate:.1f}%</div><div class="label">Audit → Go-Live Conv. ({today.strftime('%B')})</div></div>
    </div>

    <div class="chart-row">
      <div class="chart-card">
        <h2 style="margin-top:0">Achievement by Owner</h2>
        <canvas id="ownerChart"></canvas>
      </div>
      <div class="chart-card">
        <h2 style="margin-top:0">MQL Lead Funnel — {today.strftime('%B')}</h2>
        <canvas id="leadChart"></canvas>
      </div>
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
      <div class="stat-card"><div class="num">{lead_buckets.get('Unqualified', 0)}</div><div class="label">Unqualified</div></div>
      <div class="stat-card"><div class="num">{lead_buckets.get('Open', 0)}</div><div class="label">Open</div></div>
      <div class="stat-card"><div class="num">{lead_buckets.get('Contacted', 0)}</div><div class="label">Contacted</div></div>
      <div class="stat-card"><div class="num">{lead_buckets.get('Could Not Connect', 0)}</div><div class="label">Could Not Connect</div></div>
      <div class="stat-card"><div class="num">{lead_buckets.get('Converted', 0)}</div><div class="label">Converted</div></div>
    </div>
    <div class="chart-card">
      <h2 style="margin-top:0">Lead Status Breakdown</h2>
      <canvas id="leadChart2"></canvas>
    </div>
    <p style="font-size:11.5px;color:#8891A3;font-style:italic;">Bucketing is inferred from the Lead.Status text field — verify these categories match your org's actual picklist values if numbers look off.</p>
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

  const CHART_DATA = {chart_data_json};
  const NAVY = '#1E2761', GOLD = '#C98A2C', ICE = '#CADCFC', SLATE = '#3A3F55', GREEN='#1F7A1F', RED='#B33A3A';

  new Chart(document.getElementById('targetChart'), {{
    type: 'bar',
    data: {{
      labels: CHART_DATA.targetVsAchieved.labels,
      datasets: [{{ label: 'INR', data: CHART_DATA.targetVsAchieved.values, backgroundColor: [ICE, GOLD], borderRadius: 6 }}]
    }},
    options: {{ indexAxis: 'y', plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ ticks: {{ callback: v => '₹' + (v/10000000).toFixed(1) + 'Cr' }} }} }} }}
  }});

  new Chart(document.getElementById('ownerChart'), {{
    type: 'bar',
    data: {{
      labels: CHART_DATA.byOwner.labels,
      datasets: [{{ label: 'EARR', data: CHART_DATA.byOwner.values, backgroundColor: NAVY, borderRadius: 6 }}]
    }},
    options: {{ plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ ticks: {{ callback: v => '₹' + (v/100000).toFixed(0) + 'L' }} }} }} }}
  }});

  const leadColors = [RED, '#AAB2C5', GOLD, '#8891A3', GREEN];
  new Chart(document.getElementById('leadChart'), {{
    type: 'doughnut',
    data: {{ labels: CHART_DATA.leadFunnel.labels, datasets: [{{ data: CHART_DATA.leadFunnel.values, backgroundColor: leadColors }}] }},
    options: {{ plugins: {{ legend: {{ position: 'bottom', labels: {{ font: {{ size: 10.5 }} }} }} }} }}
  }});
  new Chart(document.getElementById('leadChart2'), {{
    type: 'bar',
    data: {{ labels: CHART_DATA.leadFunnel.labels, datasets: [{{ data: CHART_DATA.leadFunnel.values, backgroundColor: leadColors, borderRadius: 6 }}] }},
    options: {{ plugins: {{ legend: {{ display: false }} }} }}
  }});
</script>
</body>
</html>
"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"Dashboard regenerated at {generated_at}")
    print(f"Till Date (team only): {len(till_date_rows)} brands, {fmt_currency(total_earr_alltime)}")
    print(f"JAS {FOCUS_YEAR} focus: {len(focus_data['rows'])} brands, {fmt_currency(focus_data['total'])} ({target_progress_pct}% of {fmt_currency(JAS_TARGET)} target)")
    print(f"Pitches this month: {pitches_this_month}, Audits this month: {audits_this_month}, Conversion: {conversion_rate:.1f}%")
    print(f"Lead buckets: {dict(lead_buckets)}")


if __name__ == "__main__":
    build_dashboard()
