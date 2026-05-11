"""
Quick script to generate synthetic joined 835/837 claims for testing.
Mix of paid history + denied claims across the main denial scenarios.
"""

import json
import random
from datetime import datetime, timedelta
from pathlib import Path


random.seed(42)

PAYERS = [
    ("Aetna",                   "AETNA-001",   "Commercial"),
    ("Blue Cross Blue Shield",  "BCBS-IL",     "Commercial"),
    ("United Healthcare",       "UHC-001",     "Commercial"),
    ("Medicare Part B",         "MEDICARE-B",  "Medicare"),
    ("State Medicaid",          "MEDICAID-IL", "Medicaid"),
]

# rough typical billed amounts + which diagnoses make sense per procedure
PROCEDURES = {
    "99213": {"amount": 185.0,   "diagnoses": ["J06.9", "J20.9", "I10"],         "needs_auth": False},
    "99214": {"amount": 250.0,   "diagnoses": ["J06.9", "I10", "E11.9", "M54.5"],"needs_auth": False},
    "27447": {"amount": 12800.0, "diagnoses": ["M17.11", "M17.12"],              "needs_auth": True},
    "72148": {"amount": 8200.0,  "diagnoses": ["M51.16", "M54.16", "M48.06"],    "needs_auth": True},
    "70553": {"amount": 4200.0,  "diagnoses": ["G44.52", "R51.9"],               "needs_auth": True},
}

PROVIDER_NPIS = ["1234567890", "5678901234", "9876543210"]


def fmt_date(d):
    return d.strftime("%Y-%m-%d")


def build_claim(num, *, payer_name, payer_id, ins_type, procedure, diagnosis,
                svc_date, filing_days, status, carc="", rarc="",
                paid_rate=0.8, auth="", freq="1", delay_code="",
                npi="1234567890", subscriber=None, expected=None, scenario=""):

    amount = PROCEDURES[procedure]["amount"] * random.uniform(0.90, 1.15)
    amount = round(amount, 2)
    paid   = round(amount * paid_rate, 2) if status == "paid" else 0.0
    rcv_date = svc_date + timedelta(days=filing_days)
    sub = subscriber or f"SUB{num:05d}"
    cid = f"CLM-2026-{num:05d}"

    return {
        "claim_id": cid,
        "scenario": scenario,
        "edi_835": {
            "cp_PayerName":          payer_name,
            "cp_PayerID":            payer_id,
            "pc_ClaimID":            cid,
            "pc_ClaimStatus":        "4" if status == "denied" else "1",
            "pc_ClaimAmount":        amount,
            "pc_ClaimPaid":          paid,
            "pc_InsuranceType":      ins_type,
            "pc_ReceivedDate":       fmt_date(rcv_date),
            "pc_StatementBegin":     fmt_date(svc_date),
            "pc_StatementEnd":       fmt_date(svc_date),
            "pc_PriorAuthNum":       auth,
            "pcl_ProcedureCode":     procedure,
            "pcl_ChargedAmount":     amount,
            "pcl_PaidAmount":        paid,
            "pcl_ServiceDate":       fmt_date(svc_date),
            "pcl_RemarkCodes":       rarc,
            "pcla_AdjustmentGroup":  "CO" if status == "denied" else "",
            "pcla_AdjustmentReason": carc,
            "pcla_AdjustmentAmount": round(amount - paid, 2),
        },
        "edi_837": {
            "ec_ClaimNo":          cid,
            "ec_Amount":           amount,
            "ec_PlaceOfService":   "11",
            "ec_PayerName":        payer_name,
            "ec_PayerID":          payer_id,
            "ec_InsuranceType":    ins_type,
            "ec_PrincipalDiagnosis": diagnosis,
            # add a secondary diag for lumbar MRI when primary is just back pain
            "ec_Diag2":            "M51.16" if procedure == "72148" and diagnosis == "M54.5" else "",
            "ec_BillProvNPI":      npi,
            "ec_RendProvNPI":      npi,
            "ec_ServiceDateFrom":  fmt_date(svc_date),
            "ec_ServiceDateTo":    fmt_date(svc_date),
            "ec_PriorAuthorization": auth,
            "ec_TypeOfBill":       "131",
            "ec_ClaimFrequency":   freq,
            "ec_DelayReasonCode":  delay_code,
            "ec_SubscriberID":     sub,
            "cd_ProcedureCode":    procedure,
            "cd_Amount":           amount,
            "cd_Quantity":         1,
            "cd_DiagPointer1":     "1",
            "cd_PlaceOfService":   "11",
            "cd_ServiceDateFrom":  fmt_date(svc_date),
            "cd_ServiceDateTo":    fmt_date(svc_date),
            "cd_PriorAuthNo":      auth,
        },
        "expected": {
            "recoverability_verdict": expected,
            "scenario": scenario,
        },
    }


def assignment_claims():
    """The four specific claims given in the assignment PDF, used as regression cases."""
    return [
        build_claim(142,
            payer_name="Blue Cross Blue Shield", payer_id="BCBS-IL",
            ins_type="Commercial", procedure="99214", diagnosis="J06.9",
            svc_date=datetime(2025, 6, 15), filing_days=278,
            status="denied", carc="29",
            npi="1234567890", subscriber="XYZ123456",
            expected="not_recoverable",
            scenario="Assignment Claim A - Timely Filing"),

        build_claim(287,
            payer_name="Medicare Part B", payer_id="MEDICARE-B",
            ins_type="Medicare", procedure="27447", diagnosis="M17.11",
            svc_date=datetime(2026, 1, 8), filing_days=33,
            status="denied", carc="16", rarc="N20",
            auth="AUTH-998877", npi="9876543210",
            expected="recoverable",
            scenario="Assignment Claim B - Missing Information"),

        build_claim(391,
            payer_name="Aetna", payer_id="AETNA-001",
            ins_type="Commercial", procedure="72148", diagnosis="M54.5",
            svc_date=datetime(2026, 2, 20), filing_days=20,
            status="denied", carc="50", rarc="N386",
            npi="5678901234",
            expected="recoverable",
            scenario="Assignment Claim C - Medical Necessity"),

        build_claim(455,
            payer_name="United Healthcare", payer_id="UHC-001",
            ins_type="Commercial", procedure="99213", diagnosis="J20.9",
            svc_date=datetime(2026, 1, 10), filing_days=22,
            status="denied", carc="18",
            npi="1234567890",
            expected="recoverable",
            scenario="Assignment Claim D - Duplicate"),
    ]


def generate():
    claims = assignment_claims()
    base   = datetime(2026, 1, 1)
    n      = 1000

    # Build up a set of paid claims to give the pattern matcher something to work with
    for i in range(24):
        pname, pid, ins = PAYERS[i % len(PAYERS)]
        proc  = list(PROCEDURES.keys())[i % len(PROCEDURES)]
        dx    = random.choice(PROCEDURES[proc]["diagnoses"])
        auth  = f"AUTH-{n}" if PROCEDURES[proc]["needs_auth"] else ""

        claims.append(build_claim(n,
            payer_name=pname, payer_id=pid, ins_type=ins,
            procedure=proc, diagnosis=dx,
            svc_date=base - timedelta(days=10 + i),
            filing_days=random.choice([12, 20, 35, 55]),
            status="paid",
            paid_rate=random.choice([0.65, 0.72, 0.80, 0.90]),
            auth=auth,
            npi=random.choice(PROVIDER_NPIS),
            scenario="Historically paid"))
        n += 1

    # Denied claims covering each denial type
    scenarios = [
        ("Timely filing - clearly late",     "29",  "not_recoverable"),
        ("Timely filing - within window",     "29",  "recoverable"),
        ("Missing info",                      "16",  "recoverable"),
        ("Med necessity - unsupported dx",    "50",  "needs_review"),
        ("Auth required but missing",         "197", "needs_review"),
        ("Duplicate - matched history",       "18",  "not_recoverable"),
        ("Coding modifier issue",             "4",   "recoverable"),
        ("Non-covered service",               "96",  "not_recoverable"),
    ]

    for i in range(32):
        label, carc, expected = scenarios[i % len(scenarios)]
        pname, pid, ins = PAYERS[(i + 1) % len(PAYERS)]
        proc  = random.choice(list(PROCEDURES.keys()))
        dx    = random.choice(PROCEDURES[proc]["diagnoses"])
        svc   = base - timedelta(days=random.randint(5, 220))
        fdays = random.choice([30, 45, 80])
        auth  = f"AUTH-{n}" if PROCEDURES[proc]["needs_auth"] else ""
        npi   = random.choice(PROVIDER_NPIS)
        sub   = None
        rarc  = ""
        delay = ""
        freq  = "1"

        if carc == "29" and expected == "not_recoverable":
            fdays = 410 if ins == "Medicare" else 240
            auth  = ""
        elif carc == "29":
            fdays = 45

        elif carc == "16":
            rarc = "N20"

        elif carc == "50":
            # use a vague/unsupported diagnosis so the system can't match procedure<->dx
            dx   = "R69"
            auth = ""
            rarc = "N386"

        elif carc == "197":
            auth = ""
            proc = random.choice(["27447", "72148", "70553"])
            dx   = random.choice(PROCEDURES[proc]["diagnoses"])

        elif carc == "18":
            # copy signature from an existing claim to create an actual duplicate
            src   = claims[random.randint(4, min(12, len(claims) - 1))]
            pname = src["edi_837"]["ec_PayerName"]
            pid   = src["edi_837"]["ec_PayerID"]
            ins   = src["edi_837"]["ec_InsuranceType"]
            proc  = src["edi_837"]["cd_ProcedureCode"]
            dx    = src["edi_837"]["ec_PrincipalDiagnosis"]
            svc   = datetime.strptime(src["edi_837"]["ec_ServiceDateFrom"], "%Y-%m-%d")
            auth  = src["edi_837"]["ec_PriorAuthorization"]
            npi   = src["edi_837"]["ec_BillProvNPI"]
            sub   = src["edi_837"]["ec_SubscriberID"]

        elif carc == "4":
            proc = random.choice(["99213", "99214"])
            dx   = "J06.9"

        elif carc == "96":
            proc  = "70553"
            dx    = "R51.9"
            auth  = ""

        claims.append(build_claim(n,
            payer_name=pname, payer_id=pid, ins_type=ins,
            procedure=proc, diagnosis=dx,
            svc_date=svc, filing_days=fdays,
            status="denied", carc=carc, rarc=rarc,
            auth=auth, freq=freq, delay_code=delay,
            npi=npi, subscriber=sub,
            expected=expected,
            scenario=label))
        n += 1

    return claims


if __name__ == "__main__":
    claims = generate()

    out = Path("data/synthetic_claims.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(claims, f, indent=2)

    denied = sum(1 for c in claims if c["edi_835"]["pc_ClaimStatus"] == "4")
    print(f"Generated {len(claims)} claims  ({len(claims) - denied} paid, {denied} denied)")
    print(f"Saved to {out}")
