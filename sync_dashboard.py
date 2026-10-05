#!/usr/bin/env python3
"""
sync_dashboard.py
Pulls live KwikAds data from Salesforce (filtered to Rahul's team only) and
regenerates index.html for the GitHub Pages dashboard: OND quarter focus (+ frozen JAS tab),
Target vs Achievement (7.2 Cr goal), Agreement Signed pipeline, weighted
active pipeline (Pitch 5% / Pre Audit 15% / Audit Done 30%), average ticket
size, per-rep MQL lead funnel, and Till Date with quarterly segregation.
Runs on a schedule via GitHub Actions.

Required environment variables (GitHub Secrets):
  SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN
"""

import os
import sys
import json
import math
import base64
from datetime import datetime, date, timedelta
from collections import defaultdict
from simple_salesforce import Salesforce

TEAM_OWNERS = {
    "Rahul Patel": "Rahul",
    "gaurav1 Panchal": "Gaurav",
    "Gaurav Panchal": "Gaurav",
    "Trishun Tripathi": "Trishun",
    "Tushar Joshi": "Tushar",
    "Vaishak Mohan": "Vaishak",
}

QUARTER_MONTHS = {
    "JFM": [1, 2, 3],
    "AMJ": [4, 5, 6],
    "JAS": [7, 8, 9],
    "OND": [10, 11, 12],
}
FOCUS_YEAR = 2026
JAS_TARGET = 7_20_00_000  # Rs 7.2 Cr
INDIVIDUAL_TARGET = 1_80_00_000  # Rs 1.8 Cr per rep, JAS quarter
# OND 2026 targets: Rs 9 Cr team target; the
# individual target per POC is Rs 1.8 Cr. Edit these two lines to change.
OND_TARGET = 9_00_00_000
OND_INDIVIDUAL_TARGET = 1_80_00_000

# --- POC Focus Areas tab: plan assumptions to reach the individual target ---
PLAN_AOV = 7_00_000          # target average order value (Rs 7L)
PLAN_MIN_CONV = 0.32         # Audit -> Go-Live ratio: plan uses max(rep's last-quarter ratio, this)
BASELINE_QUARTER = "JAS"     # last quarter, used as each rep's baseline
EARLY_DAYS = 10              # before this many days into the quarter, focus uses the baseline, not live pace
MIN_AUDITS_FOR_RATIO = 5     # need at least this many audits before the live Audit->Go-Live ratio is trusted

# POC Focus Areas tab is password protected. The password is NEVER stored in this (public) repo:
# it is read from the FOCUS_TAB_PASSWORD GitHub secret. If the secret is missing the tab is left out.
# Set PROTECT_FOCUS_TAB = True to password-lock the tab (needs the FOCUS_TAB_PASSWORD secret in GitHub).
# False = the tab is visible to everyone who opens the dashboard.
PROTECT_FOCUS_TAB = False
FOCUS_TAB_PASSWORD = os.environ.get("FOCUS_TAB_PASSWORD", "")
PBKDF2_ITERATIONS = 600_000


def encrypt_for_page(plaintext, password):
    """AES-256-GCM with a PBKDF2-SHA256 key. Returns base64(salt[16] + iv[12] + ciphertext+tag).
    The page decrypts it in the browser with the Web Crypto API."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS).derive(password.encode("utf-8"))
    return base64.b64encode(salt + iv + AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)).decode("ascii")


# Individual contributors shown in "Achievement by Owner" (Rahul is excluded there on request).
# Add Utkrist here (and to TEAM_OWNERS, with his exact Salesforce Owner.Name) once he starts.
IC_NAMES = ["Gaurav", "Trishun", "Tushar", "Vaishak"]

# Focus tabs. JAS stays as a frozen quarter; OND is the live "Focus" tab.
QUARTER_CFG = {
    "OND": {
        "label": "OND", "tab_id": "ond", "suffix": "OND",
        "start": date(2026, 10, 1), "end": date(2027, 1, 1),
        "months": ["October", "November", "December"],
        "target": OND_TARGET, "individual_target": OND_INDIVIDUAL_TARGET,
    },
    "JAS": {
        "label": "JAS", "tab_id": "jas", "suffix": "",
        "start": date(2026, 7, 1), "end": date(2026, 10, 1),
        "months": ["July", "August", "September"],
        "target": JAS_TARGET, "individual_target": INDIVIDUAL_TARGET,
    },
}
LEAD_FUNNEL_QUARTER = "OND"   # Lead Funnel tab talks about OND leads only
PIPELINE_FROM = date(2026, 10, 1)  # Active Pipeline tab shows opportunities created from this date (OND)

# Salesforce Reports to surface on the "SF Reports" tab.
# id = the 18-char Report Id from the Lightning URL; url = the full link to open it in SF.
SF_REPORTS = [
    {
        "name": "Opp Funnel OND'26",
        "id": "00Ofu00000AJ8qXEAT",
        "url": "https://gokwikcommercesolutionsprivatelimi.lightning.force.com/lightning/r/sObject/00Ofu00000AJ8qXEAT/view?queryScope=userFolders",
        "use_count": False,
        "exclude_owners": ["Rahul Patel"],
    },
    {
        "name": "WoW Pitch OND'26",
        "id": "00Ofu00000AJ8nJEAT",
        "url": "https://gokwikcommercesolutionsprivatelimi.lightning.force.com/lightning/r/sObject/00Ofu00000AJ8nJEAT/view?queryScope=userFolders",
        "use_count": True,  # show count of pitches, not Sum of Amount
    },
    {
        "name": "OND'26 Lead Funnel",
        "id": "00Ofu00000AJ8ddEAD",
        "url": "https://gokwikcommercesolutionsprivatelimi.lightning.force.com/lightning/r/sObject/00Ofu00000AJ8ddEAD/view?queryScope=userFolders",
        "use_count": False,
    },
]

# Weighted active pipeline conversion assumptions
STAGE_WEIGHTS = {
    "Pitch": 0.05,
    "Pre Audit": 0.15,
    "Audit Done": 0.30,
    "Agreement Signed": 0.70,  # ASSUMPTION: not specified by Rahul — adjust if a different rate applies
}


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


def _parse_numeric(label):
    """Extracts a float from a formatted label like 'INR 12,828,000.00' or '45'."""
    if label is None:
        return None
    cleaned = "".join(ch for ch in str(label) if ch.isdigit() or ch in ".-")
    try:
        return float(cleaned) if cleaned not in ("", "-", ".") else None
    except ValueError:
        return None


def _format_like(original_label, new_value):
    """Reformats new_value using the same style (currency prefix, decimals) as original_label."""
    if original_label is None:
        return str(new_value)
    s = str(original_label)
    has_decimals = "." in s
    is_currency = any(c.isalpha() for c in s)
    if is_currency:
        currency_prefix = "".join(ch for ch in s if ch.isalpha() or ch == " ").strip()
        return f"{currency_prefix} {new_value:,.2f}".strip()
    if has_decimals:
        return f"{new_value:,.2f}"
    return f"{int(round(new_value))}"


def fetch_sf_report_summary(sf, report_id, use_count=False, exclude_owners=None):
    exclude_owners = set(exclude_owners or [])
    """
    Pulls a Salesforce Report's results via the Analytics REST API and tries to
    extract a display-ready breakdown. Handles three shapes:
      - Matrix reports (groupingsDown AND groupingsAcross, e.g. Owner x Week) ->
        a full 2D table, since collapsing either axis away loses the point of
        a "week-on-week" report.
      - Single-grouped reports (groupingsDown only) -> a simple label/value list.
      - Ungrouped tabular reports -> just the grand total.

    use_count: when True, shows each cell's rowCount (the number of underlying
    records) instead of its configured summary aggregate (e.g. Sum of Amount).
    rowCount is tracked by Salesforce independently of which summary field the
    report happens to have configured, so this works regardless of report setup.

    Intentionally defensive: on ANY unexpected shape, returns None so the caller
    falls back to a plain link card instead of breaking the whole sync.
    """
    def cell_value(cell):
        agg = cell.get("aggregates", [])
        if use_count:
            # The report has TWO summary aggregates per cell (e.g. Sum of Booked ARR,
            # then Record Count) — count is the second one, not a separate rowCount field.
            if len(agg) > 1:
                return agg[1].get("label")
            return cell.get("rowCount")  # fallback for reports that only expose rowCount
        return agg[0].get("label") if agg else None

    try:
        data = sf.restful(f"analytics/reports/{report_id}")
        report_name = data.get("reportMetadata", {}).get("name", report_id)
        fact_map = data.get("factMap", {})
        groupings_down = data.get("groupingsDown", {}).get("groupings", [])
        groupings_across = data.get("groupingsAcross", {}).get("groupings", [])

        grand_total = cell_value(fact_map.get("T!T", {}))

        # ---- Matrix report: e.g. Owner (down) x Week (across) ----
        if groupings_down and groupings_across:
            across_labels = [g.get("label", f"Col {j}") for j, g in enumerate(groupings_across)]
            matrix_rows = []
            for i, g in enumerate(groupings_down):
                row_label = g.get("label", f"Row {i}")
                if row_label in exclude_owners:
                    continue  # hide this row from view; totals below are left exactly as Salesforce returns them
                row_values = []
                for j in range(len(groupings_across)):
                    cell = fact_map.get(f"{i}!{j}", {})
                    v = cell_value(cell)
                    row_values.append(v if v is not None else "-")
                row_total = cell_value(fact_map.get(f"{i}!T", {}))
                matrix_rows.append((row_label, row_values, row_total if row_total is not None else "-"))
            if not matrix_rows:
                return None

            col_totals = []
            for j in range(len(groupings_across)):
                ct = cell_value(fact_map.get(f"T!{j}", {}))
                col_totals.append(ct if ct is not None else "-")

            return {
                "name": report_name,
                "is_matrix": True,
                "across_labels": across_labels[:12],  # cap columns so the card stays readable
                "matrix_rows": [(label, vals[:12], total) for label, vals, total in matrix_rows],
                "col_totals": col_totals[:12],
                "grand_total": grand_total,
            }

        # ---- Single-grouped report (one axis only) ----
        rows = []
        if groupings_down:
            for i, g in enumerate(groupings_down):
                label = g.get("label", f"Group {i}")
                if label in exclude_owners:
                    continue  # hide from view; grand_total below is left exactly as Salesforce returns it
                value = cell_value(fact_map.get(f"{i}!T", {}))
                if value is not None:
                    rows.append((label, value))
        elif groupings_across:
            for j, g in enumerate(groupings_across):
                label = g.get("label", f"Group {j}")
                if label in exclude_owners:
                    continue
                value = cell_value(fact_map.get(f"T!{j}", {}))
                if value is not None:
                    rows.append((label, value))
        elif grand_total is not None:
            rows.append(("Grand Total", grand_total))

        if not rows and grand_total is None:
            return None

        return {"name": report_name, "is_matrix": False, "rows": rows[:12], "grand_total": grand_total}
    except Exception as e:
        print(f"  (Report {report_id} preview unavailable: {e})")
        return None


def build_dashboard():
    sf = sf_connect()
    owner_names_sql = "','".join(sorted(set(TEAM_OWNERS.keys())))

    # ================= ALL-TIME GO-LIVE (Till Date), team only =================
    golive_q = f"""
        SELECT Account.Name, Owner.Name, CloseDate, Booked_ARR_Cr__c
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
        # Booked_ARR_Cr__c is stored in Crores (e.g. 0.048 = Rs 4,80,000) — convert to rupees.
        arr = (r.get("Booked_ARR_Cr__c") or 0) * 1_00_00_000
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

    focus_key = f"OND-{FOCUS_YEAR}"  # highlighted as "(current)" in the Till Date quarterly table

    # ================= AGREEMENT SIGNED (ready to go live soon) =================
    agreement_q = f"""
        SELECT Account.Name, Owner.Name, Kwik_Ads_Expected_ARR__c
        FROM Opportunity
        WHERE RecordType.Name = 'Kwik Ads'
          AND StageName = 'Agreement Signed'
          AND Owner.Name IN ('{owner_names_sql}')
    """
    agreement_records = query_all(sf, agreement_q)
    agreement_rows = []
    agreement_total = 0
    agreement_by_owner = defaultdict(lambda: [0, 0])
    for r in agreement_records:
        acct = r["Account"]["Name"] if r.get("Account") else "Unknown"
        owner = owner_short(r["Owner"]["Name"] if r.get("Owner") else None)
        if owner is None:
            continue
        arr = r.get("Kwik_Ads_Expected_ARR__c") or 0
        agreement_rows.append((acct, owner, arr))
        agreement_total += arr
        agreement_by_owner[owner][0] += 1
        agreement_by_owner[owner][1] += arr

    # ================= WEIGHTED ACTIVE PIPELINE: Pitch / Pre Audit / Audit Done =================
    pipeline_q = f"""
        SELECT Owner.Name, StageName, Kwik_Ads_Expected_ARR__c, CreatedDate
        FROM Opportunity
        WHERE RecordType.Name = 'Kwik Ads'
          AND StageName IN ('Pitch', 'Pre Audit', 'Audit Done', 'Agreement Signed')
          AND Owner.Name IN ('{owner_names_sql}')
    """
    pipeline_records = query_all(sf, pipeline_q)
    # stage -> {count, earr}
    stage_summary = {s: {"count": 0, "earr": 0} for s in STAGE_WEIGHTS}
    # owner -> stage -> {count, earr}
    owner_stage_matrix = defaultdict(lambda: {s: {"count": 0, "earr": 0} for s in STAGE_WEIGHTS})
    pipeline_total_count = 0
    pipeline_total_arr = 0
    carry_count = 0   # active opportunities created before OND (excluded from this tab)
    carry_arr = 0
    pipeline_from_str = PIPELINE_FROM.isoformat()
    for r in pipeline_records:
        owner = owner_short(r["Owner"]["Name"] if r.get("Owner") else None)
        stage = r.get("StageName")
        if owner is None or stage not in STAGE_WEIGHTS:
            continue
        arr = r.get("Kwik_Ads_Expected_ARR__c") or 0
        if (r.get("CreatedDate") or "")[:10] < pipeline_from_str:
            carry_count += 1
            carry_arr += arr
            continue
        stage_summary[stage]["count"] += 1
        stage_summary[stage]["earr"] += arr
        owner_stage_matrix[owner][stage]["count"] += 1
        owner_stage_matrix[owner][stage]["earr"] += arr
        pipeline_total_count += 1
        pipeline_total_arr += arr

    weighted_total = sum(stage_summary[s]["earr"] * w for s, w in STAGE_WEIGHTS.items())
    for s in stage_summary:
        stage_summary[s]["weighted"] = stage_summary[s]["earr"] * STAGE_WEIGHTS[s]

    today = date.today()

    # ================= PER-QUARTER FOCUS DATA (OND live, JAS frozen) =================
    def _stage_entries_by_owner(start_s, end_s):
        """Real stage moves from Salesforce field history (OldValue -> NewValue), per owner.

        Counts distinct opportunities that MOVED INTO 'Pitch' / 'Audit Done' within [start, end).
        Why not OpportunityHistory: it also logs a row when Amount / Close Date change while an
        opportunity sits in a stage, so bulk updates inflate the counts. Moves out of Closed Lost
        (restores / re-opens) are not counted as new pitches or audits.
        """
        q = f"""
            SELECT OpportunityId, OldValue, NewValue, Opportunity.Owner.Name
            FROM OpportunityFieldHistory
            WHERE Field = 'StageName'
              AND CreatedDate >= {start_s}T00:00:00Z
              AND CreatedDate < {end_s}T00:00:00Z
              AND Opportunity.RecordType.Name = 'Kwik Ads'
              AND Opportunity.Owner.Name IN ('{owner_names_sql}')
        """
        seen = {"Pitch": defaultdict(set), "Audit Done": defaultdict(set)}
        for r in query_all(sf, q):
            new_stage = r.get("NewValue")
            if new_stage not in seen or r.get("OldValue") == "Closed Lost":
                continue
            owner = owner_short(((r.get("Opportunity") or {}).get("Owner") or {}).get("Name"))
            if owner:
                seen[new_stage][owner].add(r["OpportunityId"])
        return {stage: {o: len(ids) for o, ids in by_owner.items()} for stage, by_owner in seen.items()}

    def _empty_lead_agg():
        return {"buckets": defaultdict(int), "by_owner": defaultdict(lambda: defaultdict(int)), "total": 0}

    def build_lead_agg(start_s, end_s, month_names):
        q = f"""
            SELECT Id, Status, IsConverted, Owner.Name, CreatedDate
            FROM Lead
            WHERE CreatedDate >= {start_s}T00:00:00Z
              AND CreatedDate < {end_s}T00:00:00Z
              AND Owner.Name IN ('{owner_names_sql}')
        """
        agg = {"QDR": _empty_lead_agg()}
        for m in month_names:
            agg[m] = _empty_lead_agg()
        for r in query_all(sf, q):
            owner = owner_short(r["Owner"]["Name"] if r.get("Owner") else None)
            b = bucket_lead_status(r.get("Status"), r.get("IsConverted"))
            created = r.get("CreatedDate")
            month_name = datetime.strptime(created[:10], "%Y-%m-%d").strftime("%B") if created else None
            for key in (["QDR"] + ([month_name] if month_name in agg else [])):
                agg[key]["buckets"][b] += 1
                agg[key]["total"] += 1
                if owner:
                    agg[key]["by_owner"][owner]["Total"] += 1
                    agg[key]["by_owner"][owner][b] += 1
        return agg

    qdata = {}
    lead_agg_by_q = {}
    for qk, cfg in QUARTER_CFG.items():
        start_s, end_s = cfg["start"].isoformat(), cfg["end"].isoformat()
        bucket = quarter_buckets.get(f"{qk}-{FOCUS_YEAR}", {"rows": [], "owner_totals": {}, "total": 0})
        month_buckets = defaultdict(lambda: {"rows": [], "total": 0})
        for acct, owner, close_date, arr in bucket["rows"]:
            m = datetime.strptime(close_date, "%Y-%m-%d").strftime("%B")
            month_buckets[m]["rows"].append((acct, owner, close_date, arr))
            month_buckets[m]["total"] += arr
        deal_count = len(bucket["rows"])
        # Achievement by owner: all ICs always listed (zero until their first Go-Live); Rahul excluded.
        ach = {o: [0, 0] for o in IC_NAMES}
        for o, (c, a) in bucket["owner_totals"].items():
            if o != "Rahul":
                ach[o] = [c, a]
        _entries = _stage_entries_by_owner(start_s, end_s)
        pitches_by = _entries["Pitch"]
        audits_by = _entries["Audit Done"]
        pitches = sum(pitches_by.values())
        audits = sum(audits_by.values())
        qdata[qk] = {
            "rows": bucket["rows"], "total": bucket["total"], "deal_count": deal_count,
            "months": month_buckets, "avg_ticket": (bucket["total"] / deal_count) if deal_count else 0,
            "ach": ach, "pitches": pitches, "audits": audits, "pitches_by": pitches_by, "audits_by": audits_by,
            "conv": (deal_count / audits * 100) if audits else 0,
            "progress_pct": round(min(bucket["total"] / cfg["target"] * 100, 100), 1) if cfg["target"] else 0,
        }
        if qk == LEAD_FUNNEL_QUARTER:
            lead_agg_by_q[qk] = build_lead_agg(start_s, end_s, cfg["months"])

    lead_agg = lead_agg_by_q[LEAD_FUNNEL_QUARTER]
    lead_months = QUARTER_CFG[LEAD_FUNNEL_QUARTER]["months"]
    lead_periods = ["QDR"] + lead_months

    # ================= POC FOCUS AREAS: live quarter vs the plan to hit each POC's target =================
    live_cfg = QUARTER_CFG[LEAD_FUNNEL_QUARTER]
    q_days = (live_cfg["end"] - live_cfg["start"]).days
    elapsed_days = min(max((today - live_cfg["start"]).days + 1, 1), q_days)
    frac = elapsed_days / q_days
    use_live = elapsed_days >= EARLY_DAYS

    def _lakh(x):
        return f"₹{x/100000:.1f}L"

    def _pctv(x):
        return f"{x*100:.1f}%"

    def _chip_class(ratio):
        if ratio is None:
            return "na"
        return "ok" if ratio >= 1.0 else ("warn" if ratio >= 0.85 else "bad")

    focus_reps = []
    base_q, live_q = qdata[BASELINE_QUARTER], qdata[LEAD_FUNNEL_QUARTER]
    target = live_cfg["individual_target"]
    for rep in IC_NAMES:
        b_pit, b_aud = base_q["pitches_by"].get(rep, 0), base_q["audits_by"].get(rep, 0)
        b_gl, b_arr = base_q["ach"][rep]
        c_pit, c_aud = live_q["pitches_by"].get(rep, 0), live_q["audits_by"].get(rep, 0)
        c_gl, c_arr = live_q["ach"][rep]

        b_conv = (b_gl / b_aud) if b_aud else None
        b_aov = (b_arr / b_gl) if b_gl else None
        b_p2a = (b_aud / b_pit) if b_pit else None
        conv_t = max(b_conv or 0, PLAN_MIN_CONV)
        aud_t = math.ceil(target / (PLAN_AOV * conv_t))
        pit_t = math.ceil(aud_t / b_p2a) if b_p2a else None

        c_conv = (c_gl / c_aud) if c_aud >= MIN_AUDITS_FOR_RATIO else None
        c_aov = (c_arr / c_gl) if c_gl else None
        exp_pit = pit_t * frac if pit_t else None
        exp_aud = aud_t * frac
        exp_arr = target * frac

        def pick(base_ratio, live_ratio):
            return (live_ratio, "live") if (use_live and live_ratio is not None) else (base_ratio, "baseline")

        drivers = []
        # --- Pitches ---
        r, basis = pick((b_pit / pit_t) if pit_t else None, (c_pit / exp_pit) if exp_pit else None)
        if basis == "live":
            cell, msg = f"{c_pit} vs {exp_pit:.0f} by today", f"Pitches behind pace: {c_pit} done vs {exp_pit:.0f} expected by today (quarter needs ~{pit_t})."
        else:
            cell = f"{b_pit} in JAS → ~{pit_t} needed" if pit_t else f"{b_pit} in JAS"
            msg = f"Pitch volume: needs ~{pit_t} pitches this quarter vs {b_pit} in JAS." if pit_t else "Pitch volume"
        drivers.append({"name": "Pitches", "ratio": r, "basis": basis, "cell": cell, "msg": msg})
        # --- Audit -> Go-Live ratio ---
        r, basis = pick((b_conv / conv_t) if b_conv is not None else None, (c_conv / conv_t) if c_conv is not None else None)
        if basis == "live":
            cell, msg = f"{_pctv(c_conv)} vs {_pctv(conv_t)} needed", f"Audit → Go-Live ratio is {_pctv(c_conv)} vs {_pctv(conv_t)} needed."
        else:
            cell = f"{_pctv(b_conv)} in JAS → {_pctv(conv_t)} needed" if b_conv is not None else "n/a"
            msg = f"Audit → Go-Live ratio: {_pctv(b_conv)} in JAS vs {_pctv(conv_t)} needed." if b_conv is not None else "Audit → Go-Live ratio"
        drivers.append({"name": "Audit → Go-Live", "ratio": r, "basis": basis, "cell": cell, "msg": msg})
        # --- AOV ---
        r, basis = pick((b_aov / PLAN_AOV) if b_aov else None, (c_aov / PLAN_AOV) if c_aov else None)
        if basis == "live":
            cell, msg = f"{_lakh(c_aov)} vs {_lakh(PLAN_AOV)}", f"AOV is {_lakh(c_aov)} vs {_lakh(PLAN_AOV)} needed."
        else:
            cell = f"{_lakh(b_aov)} in JAS → {_lakh(PLAN_AOV)}" if b_aov else "n/a"
            msg = f"AOV: {_lakh(b_aov)} in JAS vs {_lakh(PLAN_AOV)} needed." if b_aov else "AOV"
        drivers.append({"name": "AOV", "ratio": r, "basis": basis, "cell": cell, "msg": msg})

        ranked = [d for d in drivers if d["ratio"] is not None]
        worst = min(ranked, key=lambda d: d["ratio"]) if ranked else None
        if worst is None:
            focus_name, focus_msg, sev = "—", "Not enough data yet.", "na"
        elif worst["ratio"] >= 0.95:
            focus_name, focus_msg, sev = "On track", "All three drivers are at or near plan. Keep the pace.", "ok"
        else:
            focus_name, focus_msg, sev = worst["name"], worst["msg"], _chip_class(worst["ratio"])

        live_conv_ratio = (c_conv / conv_t) if c_conv is not None else None
        live_aov_ratio = (c_aov / PLAN_AOV) if c_aov else None
        base_conv_ratio = (b_conv / conv_t) if b_conv is not None else None
        base_aov_ratio = (b_aov / PLAN_AOV) if b_aov else None
        focus_reps.append({
            "rep": rep, "drivers": drivers, "focus_name": focus_name, "focus_msg": focus_msg, "sev": sev,
            "arr": c_arr, "arr_pct": (c_arr / target * 100) if target else 0, "exp_arr": exp_arr,
            "proj_arr": (c_arr / frac) if frac else 0,
            "rows": [
                ("Pitches", str(b_pit), str(c_pit), f"{exp_pit:.0f}" if exp_pit is not None else "–", f"~{pit_t}" if pit_t else "–",
                 _chip_class((c_pit / exp_pit) if (use_live and exp_pit) else ((b_pit / pit_t) if pit_t else None))),
                ("Audits done", str(b_aud), str(c_aud), f"{exp_aud:.0f}", str(aud_t),
                 _chip_class((c_aud / exp_aud) if (use_live and exp_aud) else (b_aud / aud_t))),
                ("Audit → Go-Live", _pctv(b_conv) if b_conv is not None else "–", _pctv(c_conv) if c_conv is not None else "–", "–", f"≥ {_pctv(conv_t)}",
                 _chip_class(live_conv_ratio if (use_live and live_conv_ratio is not None) else base_conv_ratio)),
                ("AOV", _lakh(b_aov) if b_aov else "–", _lakh(c_aov) if c_aov else "–", "–", _lakh(PLAN_AOV),
                 _chip_class(live_aov_ratio if (use_live and live_aov_ratio is not None) else base_aov_ratio)),
                ("Booked ARR", fmt_currency(b_arr), fmt_currency(c_arr), fmt_currency(exp_arr), fmt_currency(target),
                 _chip_class((c_arr / exp_arr) if (use_live and exp_arr) else ((b_arr / target) if target else None))),
            ],
        })

    def pct_of(n, total):
        return f"{(n/total*100):.1f}%" if total else "0%"

    # ================= SF Reports tab: pull live previews where possible =================
    report_previews = []
    for rpt in SF_REPORTS:
        summary = fetch_sf_report_summary(sf, rpt["id"], use_count=rpt.get("use_count", False), exclude_owners=rpt.get("exclude_owners"))
        report_previews.append({**rpt, "summary": summary})

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
            avg = arr / count if count else 0
            html += f"""
            <div class="owner-card">
              <div class="initials">{initials}</div>
              <div class="name">{owner}</div>
              <div class="count">{count} brands live</div>
              <div class="arr">{fmt_currency(arr)}</div>
              <div class="avg">Avg: {fmt_currency(avg)}</div>
            </div>"""
        return html

    def render_achievement_target_cards(owner_totals, target):
        html = ""
        for owner, (count, arr) in sorted(owner_totals.items(), key=lambda x: -x[1][1]):
            initials = "".join(w[0] for w in owner.split()[:2]).upper()
            pct_of_target = min(arr / target * 100, 100) if target else 0
            gap = max(target - arr, 0)
            bar_color = "var(--green)" if pct_of_target >= 100 else ("var(--gold)" if pct_of_target >= 50 else "var(--red)")
            html += f"""
            <div class="owner-card">
              <div class="initials">{initials}</div>
              <div class="name">{owner}</div>
              <div class="arr">{fmt_currency(arr)} <span style="font-weight:400;color:#8891A3;font-size:11px">/ {fmt_currency(target)}</span></div>
              <div class="progress-wrap" style="height:16px;margin:6px 0 4px;">
                <div class="progress-fill" style="width:{pct_of_target:.1f}%;background:{bar_color};font-size:9.5px;padding-right:6px;">{pct_of_target:.0f}%</div>
              </div>
              <div class="avg">Gap: {fmt_currency(gap)}</div>
            </div>"""
        return html

    def render_owner_table(owner_totals, total_count, total_arr):
        html = "<table><tr><th>Owner</th><th class='center-cell'>Brands</th><th style='text-align:right'>EARR</th></tr>\n"
        for owner, (count, arr) in sorted(owner_totals.items(), key=lambda x: -x[1][1]):
            html += f"<tr><td>{owner}</td><td class='center-cell'>{count}</td><td class='num-cell'>{fmt_currency(arr)}</td></tr>\n"
        html += f"<tr class='total-row'><td>Total</td><td class='center-cell'>{total_count}</td><td class='num-cell'>{fmt_currency(total_arr)}</td></tr>\n"
        html += "</table>"
        return html

    def render_quarterly_breakdown():
        html = "<table><tr><th>Quarter</th><th class='center-cell'>Brands</th><th style='text-align:right'>Booked ARR</th></tr>"
        for key, data in sorted(quarter_buckets.items()):
            if not data["rows"]:
                continue
            q, y = key.split("-")
            is_focus = key == focus_key
            style = "style='background:#FBF1E0'" if is_focus else ""
            label = f"{q} {y}" + (" (current)" if is_focus else "")
            html += f"<tr {style}><td>{label}</td><td class='center-cell'>{len(data['rows'])}</td><td class='num-cell'>{fmt_currency(data['total'])}</td></tr>"
        html += f"<tr class='total-row'><td>Grand Total</td><td class='center-cell'>{len(till_date_rows)}</td><td class='num-cell'>{fmt_currency(total_earr_alltime)}</td></tr>"
        html += "</table>"
        return html

    def render_lead_stat_cards(agg):
        b, t = agg["buckets"], agg["total"]
        return f"""
        <div class="stat-row">
          <div class="stat-card"><div class="num">{t}</div><div class="label">Total Leads</div></div>
          <div class="stat-card red"><div class="num">{b.get('Unqualified', 0)}</div><div class="label">Unqualified · {pct_of(b.get('Unqualified', 0), t)}</div></div>
          <div class="stat-card"><div class="num">{b.get('Open', 0)}</div><div class="label">Open · {pct_of(b.get('Open', 0), t)}</div></div>
          <div class="stat-card purple"><div class="num">{b.get('Contacted', 0)}</div><div class="label">Contacted · {pct_of(b.get('Contacted', 0), t)}</div></div>
          <div class="stat-card"><div class="num">{b.get('Could Not Connect', 0)}</div><div class="label">Could Not Connect · {pct_of(b.get('Could Not Connect', 0), t)}</div></div>
          <div class="stat-card green"><div class="num">{b.get('Converted', 0)}</div><div class="label">Converted · {pct_of(b.get('Converted', 0), t)}</div></div>
        </div>"""

    def render_lead_by_rep_table(agg):
        cols = ["Total", "Unqualified", "Open", "Contacted", "Could Not Connect", "Converted"]
        by_owner, buckets, total = agg["by_owner"], agg["buckets"], agg["total"]
        html = "<table><tr><th>Owner</th>" + "".join(f"<th class='center-cell'>{c}</th>" for c in cols) + "</tr>\n"
        for owner in sorted(by_owner.keys()):
            html += f"<tr><td>{owner}</td>" + "".join(f"<td class='center-cell'>{by_owner[owner].get(c, 0)}</td>" for c in cols) + "</tr>\n"
        html += "<tr class='total-row'><td>Team Total</td>" + f"<td class='center-cell'>{total}</td>" + "".join(f"<td class='center-cell'>{buckets.get(c,0)}</td>" for c in cols[1:]) + "</tr>"
        html += "<tr><td><i>% of Total</i></td><td class='center-cell'>100%</td>" + "".join(f"<td class='center-cell'>{pct_of(buckets.get(c,0), total)}</td>" for c in cols[1:]) + "</tr>"
        html += "</table>"
        return html

    def render_sf_report_cards(previews):
        html = ""
        for rpt in previews:
            summary = rpt["summary"]
            html += '<div class="chart-card" style="margin-bottom:20px;overflow-x:auto;">'
            html += f'<h2 style="margin-top:0;font-size:16px">{rpt["name"]}</h2>'
            if summary:
                if summary.get("grand_total") is not None:
                    html += f'<div class="stat-card" style="margin-bottom:14px;max-width:260px"><div class="num">{summary["grand_total"]}</div><div class="label">Grand Total</div></div>'
                if summary.get("is_matrix"):
                    across = summary["across_labels"]
                    html += "<table><tr><th>Owner</th>" + "".join(f"<th class='center-cell'>{c}</th>" for c in across) + "<th class='center-cell'>Total</th></tr>"
                    for row_label, values, row_total in summary["matrix_rows"]:
                        html += f"<tr><td>{row_label}</td>" + "".join(f"<td class='center-cell'>{v}</td>" for v in values) + f"<td class='center-cell' style='font-weight:700'>{row_total}</td></tr>"
                    if summary.get("col_totals"):
                        html += "<tr class='total-row'><td>Total</td>" + "".join(f"<td class='center-cell'>{v}</td>" for v in summary["col_totals"]) + f"<td class='center-cell'>{summary.get('grand_total','-')}</td></tr>"
                    html += "</table>"
                elif summary.get("rows"):
                    html += "<table><tr><th>Group</th><th style='text-align:right'>Value</th></tr>"
                    for label, value in summary["rows"]:
                        html += f"<tr><td>{label}</td><td class='num-cell'>{value}</td></tr>"
                    html += "</table>"
            else:
                html += '<div class="empty-state" style="padding:16px;margin-bottom:12px"><div class="icon">🔗</div><b>Live preview unavailable</b><p style="margin:4px 0 0;font-size:11px">This report\'s structure couldn\'t be auto-parsed — open it directly in Salesforce.</p></div>'
            html += f'<a href="{rpt["url"]}" target="_blank" rel="noopener" style="display:inline-block;background:var(--navy);color:white;text-decoration:none;padding:8px 16px;border-radius:8px;font-size:12.5px;font-weight:600">Open in Salesforce →</a>'
            html += '</div>'
        return html

    def render_pipeline_stage_table():
        html = "<table><tr><th>Stage</th><th class='center-cell'>Brands</th><th style='text-align:right'>EARR</th><th class='center-cell'>Conv. Weight</th><th style='text-align:right'>Weighted Value</th></tr>\n"
        for s in ["Pitch", "Pre Audit", "Audit Done", "Agreement Signed"]:
            d = stage_summary[s]
            html += f"<tr><td>{s}</td><td class='center-cell'>{d['count']}</td><td class='num-cell'>{fmt_currency(d['earr'])}</td><td class='center-cell'>{int(STAGE_WEIGHTS[s]*100)}%</td><td class='num-cell'>{fmt_currency(d['weighted'])}</td></tr>\n"
        html += f"<tr class='total-row'><td colspan='2'>Total ({pipeline_total_count} brands)</td><td class='num-cell'>{fmt_currency(pipeline_total_arr)}</td><td></td><td class='num-cell'>{fmt_currency(weighted_total)}</td></tr>\n"
        html += "</table>"
        return html

    def render_pipeline_owner_matrix():
        html = "<table><tr><th>Owner</th><th class='center-cell'>Pitch</th><th class='center-cell'>Pre Audit</th><th class='center-cell'>Audit Done</th><th class='center-cell'>Agreement Signed</th><th style='text-align:right'>Weighted Value</th></tr>\n"
        for owner in sorted(owner_stage_matrix.keys()):
            m = owner_stage_matrix[owner]
            weighted = sum(m[s]["earr"] * STAGE_WEIGHTS[s] for s in STAGE_WEIGHTS)
            html += f"<tr><td>{owner}</td>"
            for s in ["Pitch", "Pre Audit", "Audit Done", "Agreement Signed"]:
                html += f"<td class='center-cell'>{m[s]['count']} · {fmt_currency(m[s]['earr'])}</td>"
            html += f"<td class='num-cell'>{fmt_currency(weighted)}</td></tr>\n"
        html += "</table>"
        return html

    # ---- Per-quarter focus tab + lead funnel tab renderers ----
    def render_month_sections(qk):
        cfg, d = QUARTER_CFG[qk], qdata[qk]
        html = ""
        for month in cfg["months"]:
            data = d["months"].get(month)
            if not data or not data["rows"]:
                html += f"""
                <div class="empty-state">
                  <div class="icon">🔮</div>
                  <b>{month} {FOCUS_YEAR} — Not started yet</b>
                  <p style="margin:4px 0 0">Data will appear here once brands go live this month</p>
                </div>"""
                continue
            html += f"<h2>{month} {FOCUS_YEAR} Go-Live — {len(data['rows'])} Brands · {fmt_currency(data['total'])}</h2>"
            html += "<table><tr><th>Brand</th><th>Owner</th><th>Date</th><th style='text-align:right'>Booked ARR</th></tr>"
            html += render_brand_rows(data["rows"])
            html += f"<tr class='total-row'><td colspan='3'>{month} Total</td><td class='num-cell'>{fmt_currency(data['total'])}</td></tr></table>"
        return html

    def render_focus_tab(qk, active=False):
        cfg, d = QUARTER_CFG[qk], qdata[qk]
        ql, sfx = cfg["label"], cfg["suffix"]
        return f"""
  <div class="tab-content{' active' if active else ''}" id="{cfg['tab_id']}">

    <div class="chart-card">
      <h2 style="margin-top:0">🎯 {ql} {FOCUS_YEAR} — Target vs Achievement</h2>
      <div class="progress-wrap">
        <div class="progress-fill" style="width:{d['progress_pct']}%">{d['progress_pct']}%</div>
      </div>
      <div class="target-summary">
        <div><div class="big">{fmt_currency(cfg['target'])}</div><div class="lbl">Target ({ql})</div></div>
        <div><div class="big" style="color:var(--gold)">{fmt_currency(d['total'])}</div><div class="lbl">Achieved So Far</div></div>
        <div><div class="big" style="color:var(--red)">{fmt_currency(max(cfg['target'] - d['total'], 0))}</div><div class="lbl">Gap Remaining</div></div>
        <div><div class="big" style="color:var(--purple)">{fmt_currency(agreement_total)}</div><div class="lbl">Agreement Signed (soon)</div></div>
      </div>
      <canvas id="targetChart{sfx}" height="140"></canvas>
    </div>

    <div class="stat-row">
      <div class="stat-card"><div class="icon-tag">🏆</div><div class="num">{d['deal_count']}</div><div class="label">Total Go-Live ({ql})</div></div>
      <div class="stat-card purple"><div class="icon-tag">📝</div><div class="num">{d['pitches']}</div><div class="label">Pitches ({ql})</div></div>
      <div class="stat-card"><div class="icon-tag">🔍</div><div class="num">{d['audits']}</div><div class="label">Audits Done ({ql})</div></div>
      <div class="stat-card green"><div class="icon-tag">📈</div><div class="num">{d['conv']:.1f}%</div><div class="label">Audit → Go-Live Conv.</div></div>
      <div class="stat-card money"><div class="icon-tag">💰</div><div class="num">{fmt_currency(d['avg_ticket'])}</div><div class="label">Avg Ticket Size ({ql})</div></div>
    </div>

    <div class="chart-row">
      <div class="chart-card">
        <h2 style="margin-top:0">Achievement by Owner — over time</h2>
        <canvas id="ownerChart{sfx}"></canvas>
      </div>
      <div class="chart-card">
        <h2 style="margin-top:0">MQL Lead Funnel — {ql}</h2>
        <canvas id="leadChart{sfx}"></canvas>
      </div>
    </div>

    <div class="chart-card">
      <h2 style="margin-top:0">Achievement by Owner — progress to {fmt_currency(cfg['individual_target'])} target</h2>
      <canvas id="ownerBarChart{sfx}" height="90"></canvas>
    </div>

    <div class="owner-row">
      {render_achievement_target_cards(d['ach'], cfg['individual_target'])}
    </div>

    <h2>📋 Agreement Signed — Ready to Go Live Soon</h2>
    <div class="stat-row">
      <div class="stat-card purple"><div class="num">{len(agreement_rows)}</div><div class="label">Brands Signed</div></div>
      <div class="stat-card money"><div class="num">{fmt_currency(agreement_total)}</div><div class="label">EARR (Signed, Pending Go-Live)</div></div>
    </div>
    {render_owner_table(agreement_by_owner, len(agreement_rows), agreement_total) if agreement_rows else '<div class="empty-state"><div class="icon">📭</div><b>No brands currently at Agreement Signed</b></div>'}

    {render_month_sections(qk)}
  </div>
"""

    def render_lead_tab_panels():
        btns = "".join(
            f'<button class="leadtab-btn{" active" if pd == "QDR" else ""}" data-leadtab="{pd}">{"QDR (Quarter)" if pd == "QDR" else pd}</button>'
            for pd in lead_periods
        )
        panels = ""
        for pd in lead_periods:
            title = "QDR (Quarter-to-Date)" if pd == "QDR" else pd
            panels += f"""
    <div class="leadtab-panel{' active' if pd == 'QDR' else ''}" id="leadtab-{pd}">
      {render_lead_stat_cards(lead_agg[pd])}
      <div class="chart-card">
        <h2 style="margin-top:0">Lead Status Breakdown — {title}</h2>
        <canvas id="leadChart{pd}"></canvas>
      </div>
      <h2>By Rep</h2>
      {render_lead_by_rep_table(lead_agg[pd])}
    </div>
"""
        return f'<div class="month-selector">{btns}</div>' + panels

    def render_focus_inner():
        chip_txt = {"ok": "On track", "warn": "Watch", "bad": "Behind", "na": "—"}
        banner = (f"Day {elapsed_days} of {q_days} ({frac*100:.0f}% of OND elapsed). " +
                  ("Focus areas use live pace against the plan." if use_live else
                   f"Early in the quarter: focus areas are based on JAS baselines and switch to live pace after day {EARLY_DAYS}."))
        summary = "<table><tr><th>POC</th><th class='center-cell'>Booked ARR vs target</th><th>Pitches</th><th>Audit → Go-Live</th><th>AOV</th><th>🔎 Focus area</th></tr>"
        for fr in focus_reps:
            cells = ""
            for d in fr["drivers"]:
                cc = _chip_class(d["ratio"])
                cells += f"<td><span class='chip {cc}'>{chip_txt[cc]}</span><div class='cell-sub'>{d['cell']}</div></td>"
            summary += (f"<tr><td><b>{fr['rep']}</b></td><td class='center-cell'>{fmt_currency(fr['arr'])}<div class='cell-sub'>{fr['arr_pct']:.0f}% of {fmt_currency(target)}</div></td>"
                        f"{cells}<td><span class='chip {fr['sev']}'>{fr['focus_name']}</span></td></tr>")
        summary += "</table>"

        cards = ""
        for fr in focus_reps:
            initials = fr["rep"][:2].upper()
            bar = min(fr["arr_pct"], 100)
            exp_pos = min(fr["exp_arr"] / target * 100, 100) if target else 0
            rows_html = ""
            for name, base, cur, exp, need, chip in fr["rows"]:
                rows_html += (f"<tr><td>{name}</td><td class='center-cell'>{base}</td><td class='center-cell'><b>{cur}</b></td>"
                              f"<td class='center-cell'>{exp}</td><td class='center-cell'>{need}</td><td class='center-cell'><span class='chip {chip}'>{chip_txt[chip]}</span></td></tr>")
            proj = fmt_currency(fr["proj_arr"]) if use_live else f"available after day {EARLY_DAYS}"
            cards += f"""
    <div class="chart-card focus-card">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <div class="owner-card" style="padding:0;box-shadow:none;flex:none;min-width:0"><div class="initials" style="margin:0">{initials}</div></div>
        <div><b style="font-size:15px;color:var(--heading)">{fr['rep']}</b>
        <div class="cell-sub">{fmt_currency(fr['arr'])} of {fmt_currency(target)} · expected by today {fmt_currency(fr['exp_arr'])} · projected at current pace: {proj}</div></div>
      </div>
      <div class="progress-wrap" style="height:14px;margin:4px 0 12px;position:relative">
        <div class="progress-fill" style="width:{bar:.1f}%"></div>
        <div style="position:absolute;top:0;bottom:0;left:{exp_pos:.1f}%;width:2px;background:var(--navy)" title="Expected by today"></div>
      </div>
      <table>
        <tr><th>Driver</th><th class='center-cell'>{BASELINE_QUARTER} (last qtr)</th><th class='center-cell'>OND so far (from 1 Oct)</th><th class='center-cell'>Needed by today</th><th class='center-cell'>OND quarter need</th><th class='center-cell'>Status</th></tr>
        {rows_html}
      </table>
      <div class="focus-callout {fr['sev']}">🔎 Focus: <b>{fr['focus_name']}</b> — {fr['focus_msg']}</div>
    </div>"""
        return f"""
    <h2>🧭 POC Focus Areas — OND {FOCUS_YEAR} · target {fmt_currency(target)} per POC</h2>
    <p class="section-note">{banner}</p>
    {summary}
    {cards}
    <p class="section-note">How the plan is set: AOV {_lakh(PLAN_AOV)}; Audit → Go-Live at least the higher of the POC's {BASELINE_QUARTER} ratio and {int(PLAN_MIN_CONV*100)}%; audits needed = target ÷ (AOV × ratio); pitches needed = audits ÷ the POC's {BASELINE_QUARTER} Pitch → Audit rate. Status: On track is 100% or more of what is needed, Watch is 85–100%, Behind is below 85%. Pitches and audits come from Salesforce only: an opportunity counts when it actually moves into the Pitch or Audit Done stage on or after the period start (OND = 1 Oct). Amount or close-date edits and re-opens from Closed Lost are not counted. They can differ from the manual tracker.</p>
"""

    if not PROTECT_FOCUS_TAB:
        poc_mode = "open"
    elif FOCUS_TAB_PASSWORD:
        poc_mode = "locked"
    else:
        poc_mode = "off"
        print("POC Focus Areas tab skipped: protection is on but FOCUS_TAB_PASSWORD is not set.")
    poc_nav_button = {
        "open": '<button class="tab-btn" data-tab="pocfocus">🧭 POC Focus Areas</button>\n',
        "locked": '<button class="tab-btn" data-tab="pocfocus">🔒 POC Focus Areas</button>\n',
        "off": "",
    }[poc_mode]

    def render_focus_areas_tab():
        if poc_mode == "off":
            return ""
        if poc_mode == "open":
            return f"""
  <div class="tab-content" id="pocfocus">{render_focus_inner()}
  </div>
"""
        blob = encrypt_for_page(render_focus_inner(), FOCUS_TAB_PASSWORD)
        return f"""
  <div class="tab-content" id="pocfocus" data-blob="{blob}">
    <div id="pocfocus-body">
      <div class="chart-card" style="max-width:420px;margin:40px auto;text-align:center">
        <div style="font-size:34px">🔒</div>
        <h2 style="margin:6px 0 4px">POC Focus Areas</h2>
        <p class="section-note">This tab is password protected.</p>
        <input type="password" id="pocpw" placeholder="Password" autocomplete="off" style="padding:9px 12px;border:1px solid #D5DAE8;border-radius:8px;font-size:14px;width:62%">
        <button id="pocunlock" class="unlock-btn active" style="margin-left:6px">Unlock</button>
        <div id="pocmsg" style="margin-top:10px;font-size:12px;color:#B33A3A;min-height:16px"></div>
      </div>
    </div>
  </div>
"""

    # ---- Chart data (JS-side Chart.js) ----
    lead_labels = ["Unqualified", "Open", "Contacted", "Could Not Connect", "Converted"]

    def focus_payload(qk):
        cfg, d = QUARTER_CFG[qk], qdata[qk]
        ind = cfg["individual_target"]
        # Daily cumulative ARR over the quarter (line charts): team vs target pace, and per POC vs pace.
        q_len = (cfg["end"] - cfg["start"]).days
        today_i = min(max((today - cfg["start"]).days, 0), q_len - 1)
        day_dates = [cfg["start"] + timedelta(days=i) for i in range(q_len)]
        day_labels = [f"{dd.day} {dd.strftime('%b')}" for dd in day_dates]

        def cum_series(owners=None):
            per_day = [0] * q_len
            for acct, owner, close_date, arr in d["rows"]:
                if owners is not None and owner not in owners:
                    continue
                idx = (datetime.strptime(close_date, "%Y-%m-%d").date() - cfg["start"]).days
                if 0 <= idx < q_len:
                    per_day[idx] += arr
            out, run = [], 0
            for i in range(q_len):
                run += per_day[i]
                out.append(round(run) if i <= today_i else None)
            return out

        ach_sorted = sorted(d["ach"].items(), key=lambda x: -x[1][1])
        bar_vals = [v[1] for _, v in ach_sorted]
        timeline = {
            "labels": day_labels,
            "todayIndex": today_i,
            "target": [round(cfg["target"] * (i + 1) / q_len) for i in range(q_len)],
            "achieved": cum_series(None),
            "owners": [{"name": o, "data": cum_series({o})} for o in IC_NAMES],
            "ownerPace": [round(ind * (i + 1) / q_len) for i in range(q_len)],
            "individualTarget": ind,
        }
        agg = lead_agg_by_q[qk]["QDR"]
        lv = [agg["buckets"].get(l, 0) for l in lead_labels]
        tot = agg["total"]
        return {
            "label": cfg["label"],
            "timeline": timeline,
            "byOwner": {
                "labels": [o for o, _ in ach_sorted], "values": bar_vals,
                "remaining": [max(ind - v, 0) for v in bar_vals],
                "pcts": [round(v / ind * 100, 1) if ind else 0 for v in bar_vals], "target": ind,
            },
            "leadFunnel": {"labels": lead_labels, "values": lv, "pcts": [round(v / tot * 100, 1) if tot else 0 for v in lv]},
        }

    pipeline_stage_labels = ["Pitch", "Pre Audit", "Audit Done", "Agreement Signed"]
    pipeline_stage_values = [stage_summary[s]["earr"] for s in pipeline_stage_labels]
    pipeline_weighted_values = [stage_summary[s]["weighted"] for s in pipeline_stage_labels]

    chart_data_json = json.dumps({
        "focus": {LEAD_FUNNEL_QUARTER: focus_payload(LEAD_FUNNEL_QUARTER)},
        "leadPeriods": lead_periods,
        "leadFunnelByPeriod": {
            pd: {"labels": lead_labels, "values": [lead_agg[pd]["buckets"].get(l, 0) for l in lead_labels]}
            for pd in lead_periods
        },
        "pipelineStages": {"labels": pipeline_stage_labels, "values": pipeline_stage_values, "weighted": pipeline_weighted_values},
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
  :root {{ --navy:#1E2761; --navy-light:#2A3480; --gold:#C98A2C; --slate:#3A3F55; --bg:#EEF1F8; --ice:#D8E4FB; --border:#E5E8EF; --white:#FFFFFF; --green:#1F7A1F; --red:#B33A3A; --purple:#6C4FB6; --card:#FFFFFF; --heading:#1E2761; --muted:#8891A3; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Calibri,sans-serif; background:linear-gradient(180deg,#EEF1F8 0%,#E7ECF7 100%); color:var(--slate); min-height:100vh; }}
  header {{ background:linear-gradient(120deg, var(--navy) 0%, #2E3A94 55%, #4A3690 100%); color:white; padding:30px 32px; box-shadow:0 6px 24px rgba(30,39,97,0.28); position:relative; overflow:hidden; }}
  header::after {{ content:''; position:absolute; top:-60%; right:-8%; width:340px; height:340px; background:radial-gradient(circle, rgba(201,138,44,0.22) 0%, transparent 70%); border-radius:50%; }}
  header .brand {{ font-size:13px; letter-spacing:3px; color:var(--gold); font-weight:700; text-transform:uppercase; position:relative; }}
  header h1 {{ font-family:Georgia,serif; font-size:29px; margin:7px 0; position:relative; }}
  header .meta {{ font-size:13px; color:#C9D3F5; position:relative; }}
  nav {{ display:flex; gap:4px; background:var(--navy-light); padding:0 32px; overflow-x:auto; box-shadow:0 3px 10px rgba(0,0,0,0.15); }}
  nav button {{ background:none; border:none; color:#C3CBEF; padding:15px 18px; font-size:13.5px; font-weight:600; cursor:pointer; border-bottom:3px solid transparent; white-space:nowrap; transition:all 0.2s; }}
  nav button:hover {{ color:white; background:rgba(255,255,255,0.08); }}
  nav button.active {{ color:white; border-bottom-color:var(--gold); }}
  main {{ padding:26px 32px 40px; max-width:1200px; margin:0 auto; }}
  .tab-content {{ display:none; }}
  .leadtab-panel {{ display:none; }}
  .leadtab-panel.active {{ display:block; animation:fadeIn 0.3s ease; }}
  .month-selector {{ display:flex; gap:8px; margin-bottom:18px; flex-wrap:wrap; }}
  .month-selector button, .leadtab-btn, .unlock-btn {{ background:var(--card); border:1px solid var(--border); border-radius:8px; padding:8px 16px; font-size:12.5px; font-weight:600; color:var(--slate); cursor:pointer; transition:all 0.2s; }}
  .month-selector button.active, .leadtab-btn.active, .unlock-btn.active {{ background:var(--navy); color:white; border-color:var(--navy); }}
  .month-selector button:hover, .leadtab-btn:hover {{ box-shadow:0 2px 8px rgba(30,39,97,0.12); }}
  .tab-content.active {{ display:block; animation:fadeIn 0.35s ease; }}
  @keyframes fadeIn {{ from {{ opacity:0; transform:translateY(6px); }} to {{ opacity:1; transform:translateY(0); }} }}
  h2 {{ font-family:Georgia,serif; color:var(--heading); font-size:19px; margin:24px 0 13px; display:flex; align-items:center; gap:8px; }}
  .stat-row {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:18px; }}
  .stat-card {{ background:var(--card); border-radius:12px; box-shadow:0 4px 16px rgba(30,39,97,0.10), 0 1px 3px rgba(30,39,97,0.06); padding:15px 18px; flex:1; min-width:130px; border-left:4px solid var(--navy); transition:transform 0.2s, box-shadow 0.2s; }}
  .stat-card:hover {{ transform:translateY(-3px); box-shadow:0 8px 22px rgba(30,39,97,0.16); }}
  .stat-card.money {{ border-left-color:var(--gold); }}
  .stat-card.green {{ border-left-color:var(--green); }}
  .stat-card.red {{ border-left-color:var(--red); }}
  .stat-card.purple {{ border-left-color:var(--purple); }}
  .stat-card .icon-tag {{ font-size:15px; margin-bottom:2px; }}
  .stat-card .num {{ font-size:23px; font-weight:700; color:var(--heading); font-family:Georgia,serif; }}
  .stat-card .label {{ font-size:11.5px; color:var(--muted); margin-top:3px; }}
  .owner-row {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:18px; }}
  .owner-card {{ background:var(--card); border-radius:12px; box-shadow:0 4px 16px rgba(30,39,97,0.10); padding:13px 17px; flex:1; min-width:150px; transition:transform 0.2s; }}
  .owner-card:hover {{ transform:translateY(-2px); }}
  .owner-card .initials {{ display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; border-radius:50%; background:linear-gradient(135deg,var(--navy),var(--purple)); color:white; font-size:11px; font-weight:700; margin-bottom:6px; box-shadow:0 2px 6px rgba(30,39,97,0.3); }}
  .owner-card .name {{ font-weight:700; color:var(--heading); font-size:12.5px; }}
  .owner-card .count {{ font-size:11.5px; color:var(--slate); margin:3px 0; }}
  .owner-card .arr {{ font-size:14px; font-weight:700; color:var(--gold); }}
  .owner-card .avg {{ font-size:10.5px; color:var(--muted); margin-top:2px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border-radius:12px; overflow:hidden; box-shadow:0 4px 16px rgba(30,39,97,0.08); font-size:12.5px; margin-bottom:18px; }}
  th {{ background:linear-gradient(90deg,var(--navy),#2E3A94); color:white; text-align:left; padding:10px 12px; font-size:11.5px; }}
  td {{ padding:8px 12px; border-bottom:1px solid var(--border); color:var(--slate); }}
  tbody tr:nth-child(even) td {{ background:#F7F9FD; }}
  tr:hover td {{ background:#EFF3FC !important; }}
  tr.total-row td {{ font-weight:700; background:var(--ice) !important; color:var(--navy); }}
  .num-cell {{ text-align:right; }}
  .center-cell {{ text-align:center; }}
  .empty-state {{ background:var(--card); border-radius:12px; padding:28px; text-align:center; color:var(--muted); margin-bottom:18px; box-shadow:0 4px 16px rgba(30,39,97,0.08); }}
  .empty-state .icon {{ font-size:28px; margin-bottom:6px; }}
  .badge {{ display:inline-block; background:var(--gold); color:white; font-size:10.5px; font-weight:700; padding:2px 8px; border-radius:12px; margin-left:8px; }}
  .chart-card {{ background:var(--card); border-radius:14px; box-shadow:0 6px 22px rgba(30,39,97,0.11); padding:20px 22px; margin-bottom:20px; position:relative; overflow:hidden; }}
  .chart-card::before {{ content:''; position:absolute; top:0; left:0; right:0; height:4px; background:linear-gradient(90deg,var(--gold),var(--navy),var(--purple)); }}
  .chart-row {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-bottom:18px; }}
  @media (max-width:800px) {{ .chart-row {{ grid-template-columns:1fr; }} }}
  .progress-wrap {{ background:#E3E8F5; border-radius:20px; height:28px; overflow:hidden; margin:10px 0; box-shadow:inset 0 1px 3px rgba(30,39,97,0.12); }}
  .progress-fill {{ background:linear-gradient(90deg, var(--gold), #E8B85C); height:100%; display:flex; align-items:center; justify-content:flex-end; padding-right:10px; color:white; font-size:11.5px; font-weight:700; transition:width 0.8s ease; }}
  .target-summary {{ display:flex; gap:20px; flex-wrap:wrap; margin-top:14px; }}
  .target-summary div {{ flex:1; min-width:120px; }}
  .target-summary .big {{ font-size:21px; font-weight:700; color:var(--heading); font-family:Georgia,serif; }}
  .target-summary .lbl {{ font-size:11px; color:var(--muted); }}
  .section-note {{ font-size:11.5px; color:var(--muted); font-style:italic; margin:-8px 0 16px; }}
  .chip {{ display:inline-block; padding:2px 9px; border-radius:10px; font-size:11px; font-weight:700; }}
  .chip.ok {{ background:#DCF1DC; color:#1F7A1F; }}
  .chip.warn {{ background:#FBF1D6; color:#8A6410; }}
  .chip.bad {{ background:#F7DEDE; color:#B33A3A; }}
  .chip.na {{ background:#ECEEF4; color:#6B7390; }}
  .cell-sub {{ font-size:10.5px; color:var(--muted); margin-top:3px; font-weight:400; }}
  .focus-callout {{ border-radius:10px; padding:10px 14px; font-size:12.5px; margin-top:12px; background:#ECEEF4; color:var(--slate); border-left:4px solid #8891A3; }}
  .focus-callout.bad {{ background:#FBEAEA; color:#8F2B2B; border-left-color:#B33A3A; }}
  .focus-callout.warn {{ background:#FDF6E3; color:#7A5A12; border-left-color:#C98A2C; }}
  .focus-callout.ok {{ background:#E7F4E7; color:#1B5E1B; border-left-color:#1F7A1F; }}
  footer {{ text-align:center; padding:24px; font-size:11.5px; color:var(--muted); }}
</style>
</head>
<body>
<header>
  <div class="brand">GoKwik · Kwik Ads</div>
  <h1>Kwik Ads Dashboard <span class="badge">Live · Team Only</span></h1>
  <div class="meta">Rahul Patel · rahul.patel@gokwik.co · Auto-synced {generated_at}</div>
</header>
<nav>
  <button class="tab-btn active" data-tab="ond">🎯 OND {FOCUS_YEAR} (Focus)</button>
  {poc_nav_button}  <button class="tab-btn" data-tab="tilldate">⭐ Till Date</button>
  <button class="tab-btn" data-tab="pipeline">🚦 Active Pipeline</button>
  <button class="tab-btn" data-tab="leadfunnel">📊 Lead Funnel</button>
  <button class="tab-btn" data-tab="sfreports">📁 SF Reports</button>
</nav>
<main>

  {render_focus_tab('OND', True)}

  {render_focus_areas_tab()}

  <div class="tab-content" id="tilldate">
    <h2>⭐ Total Go-Live — All Time (Team Only)</h2>
    <div class="stat-row">
      <div class="stat-card"><div class="num">{len(till_date_rows)}</div><div class="label">Total Go-Live</div></div>
      <div class="stat-card money"><div class="num">{fmt_currency(total_earr_alltime)}</div><div class="label">Total Booked ARR</div></div>
    </div>
    <div class="owner-row">
      {render_owner_cards(owner_totals_alltime)}
    </div>

    <h2>📅 Quarterly Segregation</h2>
    {render_quarterly_breakdown()}

    <h2>All Go-Live Brands — Till Date</h2>
    <table>
      <tr><th>Brand</th><th>Owner</th><th>Go-Live Date</th><th style="text-align:right">Booked ARR</th></tr>
      {render_brand_rows(till_date_rows)}
      <tr class="total-row"><td colspan="3">Total Booked ARR (Till Date)</td><td class="num-cell">{fmt_currency(total_earr_alltime)}</td></tr>
    </table>
  </div>

  <div class="tab-content" id="pipeline">
    <h2>🚦 Weighted Active Pipeline — OND {FOCUS_YEAR} (Team Only)</h2>
    <p class="section-note">Weighted using stage-conversion assumptions: Pitch 5%, Pre Audit 15%, Audit Done 30%, Agreement Signed 70% (this last figure wasn't specified — adjust in the script if a different rate applies).</p>
    <p class="section-note">Showing only opportunities created in OND {FOCUS_YEAR} (from {PIPELINE_FROM.strftime('%d %b %Y')}). {carry_count} active opportunities worth {fmt_currency(carry_arr)} created before OND are not included here.</p>
    <div class="stat-row">
      <div class="stat-card"><div class="num">{pipeline_total_count}</div><div class="label">Active Brands</div></div>
      <div class="stat-card money"><div class="num">{fmt_currency(pipeline_total_arr)}</div><div class="label">Raw Pipeline EARR</div></div>
      <div class="stat-card purple"><div class="num">{fmt_currency(weighted_total)}</div><div class="label">Weighted Expected Value</div></div>
    </div>
    <div class="chart-card">
      <h2 style="margin-top:0">Pipeline by Stage — Raw vs Weighted</h2>
      <canvas id="pipelineChart"></canvas>
    </div>
    <h2>Stage Summary</h2>
    {render_pipeline_stage_table()}
    <h2>By Owner × Stage</h2>
    {render_pipeline_owner_matrix()}
  </div>

  <div class="tab-content" id="leadfunnel">
    <h2>📊 MQL Lead Funnel — OND {FOCUS_YEAR} (Team Only)</h2>
    {render_lead_tab_panels()}

    <p class="section-note">Bucketing is inferred from the Lead.Status text field — verify these categories match your org's actual picklist values if numbers look off. Months with no leads yet will show all zeros.</p>
  </div>

  <div class="tab-content" id="sfreports">
    <h2>📁 Salesforce Reports</h2>
    <p class="section-note">Live previews pulled via the Salesforce Reports API where the report structure allows it. If a preview isn't available, use the link to open the report directly in Salesforce.</p>
    {render_sf_report_cards(report_previews)}
  </div>

</main>
<footer>GoKwik · Kwik Ads · Live from Salesforce · Auto-synced {generated_at} · Filtered to team: {', '.join(sorted(set(TEAM_OWNERS.values())))} · Never manually edit this file — it is overwritten on every sync</footer>
<script>
  document.querySelectorAll('.leadtab-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.leadtab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.leadtab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('leadtab-' + btn.dataset.leadtab).classList.add('active');
    }});
  }});
  document.querySelectorAll('.tab-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(btn.dataset.tab).classList.add('active');
    }});
  }});

  Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Calibri, sans-serif";

  const CHART_DATA = {chart_data_json};
  const NAVY = '#1E2761', GOLD = '#C98A2C', ICE = '#CADCFC', SLATE = '#3A3F55', GREEN='#1F7A1F', RED='#B33A3A', PURPLE='#6C4FB6';

  const leadColors = [RED, '#AAB2C5', GOLD, '#8891A3', GREEN];
  const ownerColors = [GOLD, NAVY, PURPLE, '#5B6EAE', '#2E7D6B'];

  function initFocusCharts(sfx, D) {{
    const T = D.timeline;
    const tgtLabel = '₹' + (T.individualTarget / 10000000).toFixed(1) + 'Cr';
    const crTick = v => '₹' + (v/10000000).toFixed(1) + 'Cr';
    const lakhTip = ctx => ctx.dataset.label + ': ₹' + (ctx.raw/100000).toFixed(1) + 'L';
    const todayDot = ctx => ctx.dataIndex === T.todayIndex ? 4 : 0;
    const lineOpts = {{
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{ legend: {{ position: 'bottom', labels: {{ font: {{ size: 10.5 }} }} }}, tooltip: {{ callbacks: {{ label: lakhTip }} }} }},
      scales: {{ y: {{ beginAtZero: true, ticks: {{ callback: crTick }} }}, x: {{ ticks: {{ autoSkip: true, maxTicksLimit: 12 }} }} }}
    }};

    new Chart(document.getElementById('targetChart' + sfx), {{
      type: 'line',
      data: {{
        labels: T.labels,
        datasets: [
          {{ label: 'Target pace', data: T.target, borderColor: '#8891A3', borderDash: [6, 5], borderWidth: 2, pointRadius: 0, fill: false }},
          {{ label: 'Achieved (cumulative)', data: T.achieved, borderColor: GOLD, backgroundColor: 'rgba(201,138,44,0.18)', borderWidth: 3, pointRadius: todayDot, pointBackgroundColor: GOLD, fill: true, tension: 0, spanGaps: false }}
        ]
      }},
      options: lineOpts
    }});

    new Chart(document.getElementById('ownerChart' + sfx), {{
      type: 'line',
      data: {{
        labels: T.labels,
        datasets: T.owners.map((o, i) => ({{
          label: o.name, data: o.data, borderColor: ownerColors[i % ownerColors.length], backgroundColor: ownerColors[i % ownerColors.length],
          borderWidth: 2.5, pointRadius: todayDot, fill: false, tension: 0, spanGaps: false
        }})).concat([{{ label: 'Pace to ' + tgtLabel, data: T.ownerPace, borderColor: '#B8BFD4', borderDash: [6, 5], borderWidth: 2, pointRadius: 0, fill: false }}])
      }},
      options: lineOpts
    }});

    new Chart(document.getElementById('ownerBarChart' + sfx), {{
      type: 'bar',
      data: {{
        labels: D.byOwner.labels,
        datasets: [
          {{ label: 'Achieved', data: D.byOwner.values, backgroundColor: ownerColors, borderRadius: {{topLeft:8,bottomLeft:8,topRight:0,bottomRight:0}}, stack: 's' }},
          {{ label: 'Remaining to ' + tgtLabel, data: D.byOwner.remaining, backgroundColor: '#E8EAF2', borderRadius: {{topLeft:0,bottomLeft:0,topRight:8,bottomRight:8}}, stack: 's' }}
        ]
      }},
      options: {{
        indexAxis: 'y',
        plugins: {{
          legend: {{ display: true, position: 'bottom', labels: {{ font: {{ size: 10.5 }} }} }},
          tooltip: {{ callbacks: {{ label: (ctx) => {{
            if (ctx.dataset.label === 'Achieved') return 'Achieved: ₹' + (ctx.raw/100000).toFixed(1) + 'L (' + D.byOwner.pcts[ctx.dataIndex] + '% of ' + tgtLabel + ')';
            return 'Remaining: ₹' + (ctx.raw/100000).toFixed(1) + 'L';
          }} }} }}
        }},
        scales: {{ x: {{ stacked: true, max: D.byOwner.target, ticks: {{ callback: crTick }} }}, y: {{ stacked: true }} }}
      }},
      plugins: [{{
        id: 'pctLabel',
        afterDatasetsDraw(chart) {{
          const {{ ctx }} = chart;
          chart.data.labels.forEach((label, i) => {{
            const bar = chart.getDatasetMeta(0).data[i];
            if (!bar) return;
            ctx.save();
            ctx.fillStyle = '#1E2761';
            ctx.font = 'bold 11px -apple-system, sans-serif';
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';
            ctx.fillText(D.byOwner.pcts[i] + '%', bar.x + 8, bar.y);
            ctx.restore();
          }});
        }}
      }}]
    }});

    new Chart(document.getElementById('leadChart' + sfx), {{
      type: 'doughnut',
      data: {{ labels: D.leadFunnel.labels, datasets: [{{ data: D.leadFunnel.values, backgroundColor: leadColors, borderWidth:2, borderColor:'#fff' }}] }},
      options: {{ plugins: {{ legend: {{ position: 'bottom', labels: {{ font: {{ size: 10.5 }} }} }}, tooltip: {{ callbacks: {{ label: (ctx) => {{
        const pct = D.leadFunnel.pcts[ctx.dataIndex];
        return `${{ctx.label}}: ${{ctx.raw}} (${{pct}}%)`;
      }} }} }} }} }}
    }});
  }}
  initFocusCharts('OND', CHART_DATA.focus.OND);

  CHART_DATA.leadPeriods.forEach(period => {{
    const canvasId = 'leadChart' + period;
    const el = document.getElementById(canvasId);
    if (!el) return;
    const data = CHART_DATA.leadFunnelByPeriod[period];
    const total = data.values.reduce((a,b) => a+b, 0);
    new Chart(el, {{
      type: 'doughnut',
      data: {{ labels: data.labels, datasets: [{{ data: data.values, backgroundColor: leadColors, borderWidth:2, borderColor:'#fff' }}] }},
      options: {{
        plugins: {{
          legend: {{ position: 'bottom', labels: {{ font: {{ size: 10.5 }} }} }},
          tooltip: {{ callbacks: {{ label: (ctx) => {{
            const pct = total ? (ctx.raw/total*100).toFixed(1) : 0;
            return `${{ctx.label}}: ${{ctx.raw}} (${{pct}}%)`;
          }} }} }}
        }}
      }}
    }});
  }});

  // ---- Password gate for the POC Focus Areas tab (content is AES-GCM encrypted inside the page) ----
  (function () {{
    const tab = document.getElementById('pocfocus');
    if (!tab || !tab.dataset.blob) return;
    const blob = tab.dataset.blob;
    async function decryptBlob(pw) {{
      const raw = Uint8Array.from(atob(blob), c => c.charCodeAt(0));
      const salt = raw.slice(0, 16), iv = raw.slice(16, 28), ct = raw.slice(28);
      const km = await crypto.subtle.importKey('raw', new TextEncoder().encode(pw), 'PBKDF2', false, ['deriveKey']);
      const key = await crypto.subtle.deriveKey({{ name: 'PBKDF2', salt: salt, iterations: {PBKDF2_ITERATIONS}, hash: 'SHA-256' }}, km, {{ name: 'AES-GCM', length: 256 }}, false, ['decrypt']);
      const buf = await crypto.subtle.decrypt({{ name: 'AES-GCM', iv: iv }}, key, ct);
      return new TextDecoder().decode(buf);
    }}
    async function tryUnlock(pw, silent) {{
      const msg = document.getElementById('pocmsg');
      if (!window.crypto || !crypto.subtle) {{ if (msg) msg.textContent = 'Open the dashboard over https to unlock.'; return; }}
      try {{
        const html = await decryptBlob(pw);
        document.getElementById('pocfocus-body').innerHTML = html;
        try {{ sessionStorage.setItem('pocpw', pw); }} catch (e) {{}}
        const btn = document.querySelector('[data-tab="pocfocus"]');
        if (btn) btn.textContent = '🔓 POC Focus Areas';
      }} catch (e) {{
        if (!silent && msg) msg.textContent = 'Incorrect password.';
      }}
    }}
    document.getElementById('pocunlock').addEventListener('click', () => tryUnlock(document.getElementById('pocpw').value, false));
    document.getElementById('pocpw').addEventListener('keydown', e => {{ if (e.key === 'Enter') tryUnlock(e.target.value, false); }});
    try {{ const saved = sessionStorage.getItem('pocpw'); if (saved) tryUnlock(saved, true); }} catch (e) {{}}
  }})();

  new Chart(document.getElementById('pipelineChart'), {{
    type: 'line',
    data: {{
      labels: CHART_DATA.pipelineStages.labels,
      datasets: [
        {{ label: 'Raw EARR', data: CHART_DATA.pipelineStages.values, borderColor: NAVY, backgroundColor: 'rgba(202,220,252,0.55)', borderWidth: 3, pointRadius: 5, fill: true, tension: 0.25 }},
        {{ label: 'Weighted Value', data: CHART_DATA.pipelineStages.weighted, borderColor: PURPLE, backgroundColor: PURPLE, borderWidth: 3, pointRadius: 5, fill: false, tension: 0.25 }}
      ]
    }},
    options: {{
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{ legend: {{ position: 'bottom' }}, tooltip: {{ callbacks: {{ label: ctx => ctx.dataset.label + ': ₹' + (ctx.raw/100000).toFixed(1) + 'L' }} }} }},
      scales: {{ y: {{ beginAtZero: true, ticks: {{ callback: v => '₹' + (v/100000).toFixed(0) + 'L' }} }} }}
    }}
  }});
</script>
</body>
</html>
"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"Dashboard regenerated at {generated_at}")
    print(f"Till Date (team only): {len(till_date_rows)} brands, {fmt_currency(total_earr_alltime)}")
    for qk, cfg in QUARTER_CFG.items():
        d = qdata[qk]
        print(f"{qk} {FOCUS_YEAR}: {d['deal_count']} Go-Lives, {fmt_currency(d['total'])} ({d['progress_pct']}% of {fmt_currency(cfg['target'])}); pitches={d['pitches']}, audits={d['audits']}, conv={d['conv']:.1f}%, avg ticket={fmt_currency(d['avg_ticket'])}")
    print(f"Agreement Signed: {len(agreement_rows)} brands, {fmt_currency(agreement_total)}")
    print(f"Active pipeline (OND-created only): {pipeline_total_count} brands raw={fmt_currency(pipeline_total_arr)} weighted={fmt_currency(weighted_total)}; carry-forward excluded: {carry_count} brands {fmt_currency(carry_arr)}")
    print(f"OND leads: {lead_agg['QDR']['total']} {dict(lead_agg['QDR']['buckets'])}")

if __name__ == "__main__":
    build_dashboard()
