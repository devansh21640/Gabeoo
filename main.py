import argparse
import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple


# CARC = Claim Adjustment Reason Code, these are the standard denial codes
# used by every payer in the US. Only mapping the ones I actually see in practice.
CARC_CODES: Dict[str, Tuple[str, str]] = {
    "4":   ("Coding",            "Procedure code is inconsistent with the modifier, or a required modifier is missing"),
    "16":  ("Information",       "Claim/service lacks info needed for adjudication"),
    "18":  ("Duplicate",         "Exact duplicate claim or service"),
    "29":  ("Timely Filing",     "Time limit for filing has expired"),
    "50":  ("Medical Necessity", "Service not deemed medically necessary by payer"),
    "96":  ("Non-Covered",       "Non-covered charge"),
    "97":  ("Bundling",          "Benefit for this service is included in payment/allowance for another service"),
    "197": ("Precertification",  "Precertification/authorization/notification absent"),
    "252": ("Documentation",     "Attachment or documentation required to adjudicate"),
}

# RARC = Remittance Advice Remark Code, give extra context on top of CARC
RARC_CODES: Dict[str, str] = {
    "N20":  "Service not payable with the info submitted",
    "N386": "Decision based on a National Coverage Determination (NCD)",
    "M127": "Missing patient medical record for this service",
}

# Filing limits by payer — Medicare is set by CMS (365 days), commercial varies.
# For unknown payers we default to 180 days which is a safe middle ground.
FILING_LIMITS: Dict[str, int] = {
    "Aetna":                  120,
    "Blue Cross Blue Shield": 180,
    "United Healthcare":      90,
    "Cigna":                  180,
}

# CPT codes that typically support certain diagnoses for medical necessity.
# Simplified — in production you'd pull these from payer LCDs.
MED_NECESSITY_MAP: Dict[str, set] = {
    "27447": {"M17", "M17.0", "M17.1", "M17.11", "M17.12"},  # knee replacement
    "72148": {"M51.16", "M54.16", "M48.06", "M47.26"},        # MRI lumbar
    "99213": {"J06.9", "J20.9", "I10", "E11.9"},
    "99214": {"J06.9", "J20.9", "I10", "E11.9", "M54.5"},
}


@dataclass
class MatchedClaim:
    """A historical claim that's similar to the one being analyzed."""
    claim_id: str
    score: float
    payer: str
    procedure: str
    diagnosis: str
    status: str
    paid: float
    match_reasons: List[str]


@dataclass
class Analysis:
    """The structured output per denied claim."""
    claim_id: str
    root_cause: str
    carc_interpretation: str
    recoverability_verdict: str  # "recoverable" | "not_recoverable" | "needs_review"
    confidence_score: float
    supporting_evidence: List[str]
    recommended_actions: List[str]
    historical_context: Dict[str, Any]
    similar_paid_claims: List[MatchedClaim]


# ─── helpers to read fields from both flat and nested 835/837 structures ─────

def _get_835(claim: dict) -> dict:
    return claim.get("edi_835", claim)

def _get_837(claim: dict) -> dict:
    return claim.get("edi_837", claim)

def _field(claim: dict, *keys: str, default: Any = "") -> Any:
    """Try a list of field names across the claim root, 835 block, and 837 block."""
    s835 = _get_835(claim)
    s837 = _get_837(claim)
    for k in keys:
        for src in (claim, s835, s837):
            if k in src and src[k] not in (None, ""):
                return src[k]
    return default

def get_claim_id(claim: dict) -> str:
    return str(_field(claim, "claim_id", "pc_ClaimID", "ec_ClaimNo"))

def get_payer(claim: dict) -> str:
    return str(_field(claim, "payer_name", "ec_PayerName", "cp_PayerName", default="Unknown"))

def get_procedure(claim: dict) -> str:
    return str(_field(claim, "procedure_code", "pcl_ProcedureCode", "cd_ProcedureCode"))

def get_diagnoses(claim: dict) -> List[str]:
    vals = [_field(claim, "principal_diagnosis", "ec_PrincipalDiagnosis")]
    vals += [_field(claim, f"ec_Diag{i}") for i in range(2, 26)]
    return [str(v) for v in vals if v not in (None, "")]

def get_billed(claim: dict) -> float:
    return float(_field(claim, "billed_amount", "pc_ClaimAmount", "ec_Amount", default=0.0))

def get_paid(claim: dict) -> float:
    return float(_field(claim, "paid_amount", "pc_ClaimPaid", default=0.0))

def get_status(claim: dict) -> str:
    raw = str(_field(claim, "status", "pc_ClaimStatus", default=""))
    if raw.lower() in ("paid", "denied"):
        return raw.lower()
    if raw == "4":
        return "denied"
    return "paid" if get_paid(claim) > 0 else "unknown"

def get_carc(claim: dict) -> str:
    return str(_field(claim, "denial_reason_code", "pcla_AdjustmentReason"))

def _parse_date(val: Any) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.strptime(str(val), "%Y-%m-%d")
    except ValueError:
        return None

def get_filing_limit(claim: dict) -> int:
    ins = str(_field(claim, "insurance_type", "ec_InsuranceType", "pc_InsuranceType")).lower()
    if ins == "medicare":
        return 365
    if ins == "medicaid":
        return 180
    return FILING_LIMITS.get(get_payer(claim), 180)

def days_filed(claim: dict) -> Optional[int]:
    svc = _parse_date(_field(claim, "service_date", "ec_ServiceDateFrom", "pc_StatementBegin"))
    rcv = _parse_date(_field(claim, "received_date", "pc_ReceivedDate"))
    if not svc or not rcv:
        return None
    return (rcv - svc).days

def claim_signature(claim: dict) -> Tuple[Any, ...]:
    """Unique key for duplicate detection: payer + procedure + date + NPI + member ID."""
    return (
        get_payer(claim),
        get_procedure(claim),
        _field(claim, "ec_ServiceDateFrom", "service_date"),
        _field(claim, "ec_BillProvNPI", "billing_provider_npi"),
        _field(claim, "ec_SubscriberID", "subscriber_id"),
    )


# ─── historical pattern matching ─────────────────────────────────────────────

def calc_similarity(target: dict, candidate: dict) -> Tuple[float, List[str]]:
    """
    Score how similar two claims are (0.0–1.0). Higher = more alike.
    Weights reflect what billing teams actually care about: payer and procedure
    dominate, then provider, then diagnosis overlap.
    """
    # tuned these by hand — payer+procedure are the strongest signals for pattern matching
    weights = {
        "payer":     0.25,
        "procedure": 0.25,
        "provider":  0.15,
        "diagnosis": 0.15,
        "ins_type":  0.10,
        "place":     0.05,
        "auth":      0.05,
    }

    score = 0.0
    reasons: List[str] = []

    checks = [
        ("payer",     get_payer(target),      get_payer(candidate),      "same payer"),
        ("procedure", get_procedure(target),  get_procedure(candidate),  "same procedure code"),
        ("provider",  _field(target, "ec_BillProvNPI"),
                      _field(candidate, "ec_BillProvNPI"),               "same billing NPI"),
        ("ins_type",  _field(target, "insurance_type", "ec_InsuranceType"),
                      _field(candidate, "insurance_type", "ec_InsuranceType"), "same insurance type"),
        ("place",     _field(target, "ec_PlaceOfService"),
                      _field(candidate, "ec_PlaceOfService"),            "same place of service"),
        ("auth",      bool(_field(target, "ec_PriorAuthorization", "cd_PriorAuthNo")),
                      bool(_field(candidate, "ec_PriorAuthorization", "cd_PriorAuthNo")),
                      "both have/lack auth"),
    ]

    for key, tv, cv, reason in checks:
        if tv and tv == cv:
            score += weights[key]
            reasons.append(reason)

    # Jaccard overlap for diagnosis codes since one claim can have many
    t_dx = set(get_diagnoses(target))
    c_dx = set(get_diagnoses(candidate))
    if t_dx and c_dx:
        overlap = len(t_dx & c_dx) / len(t_dx | c_dx)
        score += weights["diagnosis"] * overlap
        if overlap:
            reasons.append(f"shared diagnoses: {sorted(t_dx & c_dx)}")

    return round(score, 3), reasons


class HistoricalMatcher:
    """Looks up paid analogs and computes payer/procedure denial rates."""

    def __init__(self, all_claims: List[dict]) -> None:
        self.claims = all_claims

    def find_paid_analogs(self, target: dict, top_n: int = 3) -> List[MatchedClaim]:
        """Return top N paid claims most similar to the denied target claim."""
        results: List[MatchedClaim] = []
        for c in self.claims:
            if get_claim_id(c) == get_claim_id(target):
                continue
            if get_status(c) != "paid":
                continue
            score, reasons = calc_similarity(target, c)
            if score >= 0.35:
                results.append(MatchedClaim(
                    claim_id=get_claim_id(c),
                    score=score,
                    payer=get_payer(c),
                    procedure=get_procedure(c),
                    diagnosis=_field(c, "principal_diagnosis", "ec_PrincipalDiagnosis"),
                    status="paid",
                    paid=get_paid(c),
                    match_reasons=reasons,
                ))
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_n]

    def payer_procedure_stats(self, target: dict) -> Dict[str, Any]:
        """Denial/payment rates for same payer, same procedure, and their intersection."""
        payer = get_payer(target)
        proc  = get_procedure(target)

        same_pp = [c for c in self.claims if get_payer(c) == payer and get_procedure(c) == proc]
        same_p  = [c for c in self.claims if get_payer(c) == payer]
        same_pr = [c for c in self.claims if get_procedure(c) == proc]

        return {
            "same_payer_and_procedure":     _summarize(same_pp),
            "same_payer_all_procedures":    _summarize(same_p),
            "same_procedure_all_payers":    _summarize(same_pr),
        }


def _summarize(claims: List[dict]) -> Dict[str, Any]:
    if not claims:
        return {"count": 0, "denial_rate": None, "paid_rate": None, "common_denial_reasons": []}
    statuses = Counter(get_status(c) for c in claims)
    reasons  = Counter(get_carc(c) for c in claims if get_status(c) == "denied")
    n = len(claims)
    return {
        "count": n,
        "denial_rate": round(statuses["denied"] / n, 3),
        "paid_rate":   round(statuses["paid"] / n, 3),
        "common_denial_reasons": reasons.most_common(3),
    }


# ─── denial analysis rules ───────────────────────────────────────────────────

# Internal return type for the rule methods
_RuleResult = Tuple[str, str, float, List[str], List[str]]


class DenialAnalyzer:
    """Applies denial-type-specific rules to produce a structured analysis."""

    def __init__(self, matcher: HistoricalMatcher) -> None:
        self.matcher = matcher

    def analyze(self, claim: dict) -> Analysis:
        carc = get_carc(claim)
        root_cause, verdict, conf, evidence, actions = self._run_rules(claim, carc)

        paid_analogs = self.matcher.find_paid_analogs(claim)
        history      = self.matcher.payer_procedure_stats(claim)

        # If we found paid claims very similar to this denied one, it weakens
        # a firm "not_recoverable" verdict and strengthens the case for appeal.
        if paid_analogs:
            if verdict == "not_recoverable":
                conf = max(0.55, conf - 0.15)
                evidence.append(
                    f"Found {len(paid_analogs)} historically paid claim(s) with similar payer/procedure — "
                    "consider appeal before writing off."
                )
            elif verdict == "needs_review":
                evidence.append(f"{len(paid_analogs)} paid analog(s) found — supports appeal potential.")

        ref = CARC_CODES.get(carc, ("Unknown", "No description available"))
        interp = f"CARC {carc} ({ref[0]}): {ref[1]}"

        return Analysis(
            claim_id=get_claim_id(claim),
            root_cause=root_cause,
            carc_interpretation=interp,
            recoverability_verdict=verdict,
            confidence_score=round(conf, 2),
            supporting_evidence=evidence,
            recommended_actions=actions,
            historical_context=history,
            similar_paid_claims=paid_analogs,
        )

    def _run_rules(self, claim: dict, carc: str) -> _RuleResult:
        if carc == "29":
            return self._timely_filing(claim)
        if carc in ("16", "252"):
            return self._missing_info(claim)
        if carc == "50":
            return self._med_necessity(claim)
        if carc in ("18", "97"):
            return self._duplicate(claim)
        if carc == "197":
            return self._auth_missing(claim)
        if carc == "4":
            return self._coding_error(claim)
        if carc == "96":
            proc = get_procedure(claim)
            return (
                "Payer denied as non-covered service.",
                "not_recoverable",
                0.72,
                [f"CARC 96 on {proc}", "No covered benefit evidence in claim data."],
                ["Verify benefit plan and payer policy before writing off — some plans have exceptions."],
            )
        return (
            f"Denial reason CARC {carc or '(missing)'} not handled by current ruleset.",
            "needs_review",
            0.45,
            [f"CARC {carc} needs manual review."],
            ["Send to senior billing analyst with full 835/837 packet."],
        )

    def _timely_filing(self, claim: dict) -> _RuleResult:
        days  = days_filed(claim)
        limit = get_filing_limit(claim)
        delay_code = _field(claim, "ec_DelayReasonCode")
        freq       = _field(claim, "ec_ClaimFrequency")

        svc_date = _field(claim, "service_date", "ec_ServiceDateFrom", "pc_StatementBegin")
        rcv_date = _field(claim, "received_date", "pc_ReceivedDate")

        ev = [
            f"Service date: {svc_date}",
            f"Payer received: {rcv_date}",
            f"Days to file: {days}" if days is not None else "Cannot calculate filing age (missing dates)",
            f"Filing limit for {get_payer(claim)}: {limit} days",
        ]

        if days is None:
            return (
                "Cannot verify timely filing — key dates are missing from the claim.",
                "needs_review", 0.55, ev,
                ["Pull service date and payer receipt date from clearinghouse before deciding."],
            )

        if days > limit and not delay_code and str(freq) != "7":
            ev.append("No delay reason code and not a replacement claim (frequency=7).")
            return (
                f"Claim was filed {days} days after service, exceeding the {limit}-day limit for this payer.",
                "not_recoverable", 0.90, ev,
                ["Write off unless you have proof of earlier submission or a payer-specific waiver."],
            )

        if days <= limit:
            ev.append(f"Filed {limit - days} days inside the deadline — denial appears incorrect.")
            return (
                "Timely filing denial does not hold up — claim was filed within the allowed window.",
                "recoverable", 0.88, ev,
                ["Appeal with clearinghouse acceptance timestamp, service date, and payer receipt confirmation."],
            )

        # filed late but there might be an exception
        ev += [
            f"Delay reason code: {delay_code or 'none'}",
            f"Claim frequency: {freq or 'missing'}",
        ]
        return (
            "Claim was filed late but may qualify for an exception.",
            "needs_review", 0.70, ev,
            ["Check for delay reason, replacement claim, or secondary payer EOB timing before writing off."],
        )

    def _missing_info(self, claim: dict) -> _RuleResult:
        rarc = str(_field(claim, "remark_codes", "pcl_RemarkCodes"))
        rarc_codes = [r.strip() for r in rarc.split(",") if r.strip()]

        missing_fields = []
        for f in ("ec_SubscriberID", "ec_PrincipalDiagnosis", "ec_BillProvNPI"):
            if not _field(claim, f):
                missing_fields.append(f)

        ev = [
            f"Remark codes: {', '.join(rarc_codes) or 'none'}",
            f"Empty required fields: {missing_fields or 'none found in 837'}",
        ]

        if any(r in ("N20", "M127") for r in rarc_codes) or missing_fields:
            return (
                "Payer returned claim because required information was missing or incorrect.",
                "recoverable", 0.82, ev,
                ["Correct and resubmit with demographics, diagnosis, NPI, and any supporting records."],
            )

        return (
            "Missing info denial but no obvious gap found in the 837 data — may need full EDI review.",
            "needs_review", 0.62, ev,
            ["Compare against payer's companion guide and attach clinical documentation."],
        )

    def _med_necessity(self, claim: dict) -> _RuleResult:
        proc = get_procedure(claim)
        dx   = get_diagnoses(claim)
        auth = _field(claim, "ec_PriorAuthorization", "cd_PriorAuthNo", "pc_PriorAuthNum")

        supported_dx = MED_NECESSITY_MAP.get(proc, set())
        # check exact match or ICD-10 prefix match (e.g. "M17" covers "M17.11")
        match = bool(set(dx) & supported_dx)
        if not match:
            match = any(
                d.startswith(s.rstrip("0123456789."))
                for d in dx for s in supported_dx
            )

        ev = [
            f"Procedure billed: {proc}",
            f"Diagnoses on claim: {dx or 'none'}",
            f"Prior auth: {auth or 'not present'}",
        ]

        if match:
            ev.append("Diagnosis codes are clinically consistent with the procedure.")
            return (
                "Claim has diagnosis support for the denied procedure — good appeal candidate.",
                "recoverable", 0.80, ev,
                ["Appeal with clinical notes, imaging, diagnosis rationale, and payer's medical policy."],
            )

        if auth:
            return (
                "Payer denied medical necessity despite an existing prior authorization — clear conflict.",
                "recoverable", 0.78, ev,
                ["Appeal referencing the prior auth approval and documentation showing the service matched the auth."],
            )

        return (
            "Diagnosis/procedure pairing doesn't clearly support medical necessity and no auth was obtained.",
            "needs_review", 0.68, ev,
            ["Get clinical documentation from treating provider and check payer LCD before appealing."],
        )

    def _duplicate(self, claim: dict) -> _RuleResult:
        dupes = [
            c for c in self.matcher.claims
            if get_claim_id(c) != get_claim_id(claim)
            and claim_signature(c) == claim_signature(claim)
        ]
        freq = _field(claim, "ec_ClaimFrequency")

        ev = [
            f"Claim signature (payer/proc/date/NPI/member): {claim_signature(claim)}",
            f"Matching claims in dataset: {[get_claim_id(c) for c in dupes] or 'none'}",
            f"Claim frequency code: {freq or 'missing'}",
        ]

        if dupes and str(freq) != "7":
            paid_dupes = [c for c in dupes if get_status(c) == "paid"]
            conf = 0.86 if paid_dupes else 0.74
            return (
                "Duplicate denial is backed by a matching previously-adjudicated claim.",
                "not_recoverable", conf, ev,
                ["Verify this wasn't meant as a corrected claim. If duplicate, write off."],
            )

        return (
            "Duplicate denial but no matching claim found in history — payer may have made an error.",
            "recoverable", 0.76, ev,
            ["Appeal with corrected-claim indicator (freq=7), original control number, and line-item differences."],
        )

    def _auth_missing(self, claim: dict) -> _RuleResult:
        auth = _field(claim, "ec_PriorAuthorization", "cd_PriorAuthNo", "pc_PriorAuthNum")
        proc = get_procedure(claim)
        ev   = [f"Procedure: {proc}", f"Auth on claim: {auth or 'none'}"]

        if auth:
            return (
                "Authorization denial doesn't match — claim already has an auth number.",
                "recoverable", 0.84, ev,
                ["Appeal with the auth number, approval date range, and matching service documentation."],
            )

        return (
            "High-auth procedure submitted without prior authorization.",
            "needs_review", 0.72, ev,
            [
                "Check scheduling system for a missed auth. "
                "If auth exists, resubmit corrected. If not, escalate for retroactive review."
            ],
        )

    def _coding_error(self, claim: dict) -> _RuleResult:
        modifiers: List[str] = []
        for i in range(1, 5):
            m = _field(claim, f"pcl_ProcedureModifier{i}", f"cd_Modifier{i}")
            if m:
                modifiers.append(str(m))

        proc = get_procedure(claim)
        ev   = [f"Procedure: {proc}", f"Modifiers submitted: {modifiers or 'none'}"]

        # most common E&M coding denial — missing mod 25 for same-day procedure
        if proc in ("99213", "99214") and "25" not in modifiers:
            return (
                "E&M service likely needs modifier 25 (significant, separately identifiable service).",
                "recoverable", 0.74, ev,
                ["Review encounter notes — if a separate problem was addressed, add mod 25 and resubmit."],
            )

        return (
            "Coding/modifier mismatch detected — needs review by a certified coder.",
            "needs_review", 0.60, ev,
            ["Send to certified coder for CPT/modifier validation before resubmission."],
        )


# ─── batch clustering ─────────────────────────────────────────────────────────

def build_clusters(claims: List[dict], analyses: List[Analysis]) -> List[Dict[str, Any]]:
    """Group denied claims by payer + CARC + procedure, ranked by estimated recovery."""
    by_id = {a.claim_id: a for a in analyses}

    groups: Dict[Tuple[str, str, str], List[dict]] = defaultdict(list)
    for c in claims:
        if get_status(c) != "denied":
            continue
        key = (get_payer(c), get_carc(c), get_procedure(c))
        groups[key].append(c)

    clusters = []
    for (payer, carc, proc), members in groups.items():
        total_at_risk = sum(get_billed(m) - get_paid(m) for m in members)

        recoverable_est = sum(
            (get_billed(m) - get_paid(m)) * by_id[get_claim_id(m)].confidence_score
            for m in members
            if get_claim_id(m) in by_id
            and by_id[get_claim_id(m)].recoverability_verdict in ("recoverable", "needs_review")
        )

        verdicts = Counter(
            by_id[get_claim_id(m)].recoverability_verdict
            for m in members if get_claim_id(m) in by_id
        )

        cat = CARC_CODES.get(carc, ("Unknown",))[0]

        clusters.append({
            "payer":               payer,
            "carc":                carc,
            "denial_type":         cat,
            "procedure":           proc,
            "claim_count":         len(members),
            "total_denied_dollars": round(total_at_risk, 2),
            "estimated_recovery":  round(recoverable_est, 2),
            "verdict_breakdown":   dict(verdicts),
            "claim_ids":           [get_claim_id(m) for m in members],
            "summary": (
                f"{len(members)} {payer} claim(s) | CPT {proc} | "
                f"{cat} denial | ${total_at_risk:,.2f} at risk"
            ),
        })

    clusters.sort(key=lambda x: x["estimated_recovery"], reverse=True)
    return clusters


# ─── evaluation ──────────────────────────────────────────────────────────────

def run_evaluation(analyses: List[Analysis], claims: List[dict]) -> Dict[str, Any]:
    """Check predicted verdicts against expected labels in the synthetic dataset."""
    labels: Dict[str, str] = {}
    for c in claims:
        expected = c.get("expected", {}).get("recoverability_verdict")
        if expected:
            labels[get_claim_id(c)] = expected

    labeled = [a for a in analyses if a.claim_id in labels]
    correct  = [a for a in labeled if a.recoverability_verdict == labels[a.claim_id]]

    acc      = round(len(correct) / len(labeled), 3) if labeled else None
    avg_conf = round(mean(a.confidence_score for a in analyses), 3) if analyses else None

    confusion = Counter(
        f"{labels[a.claim_id]} → {a.recoverability_verdict}"
        for a in labeled
    )

    return {
        "labeled_claims":   len(labeled),
        "verdict_accuracy": acc,
        "avg_confidence":   avg_conf,
        "breakdown":        dict(confusion),
    }


# ─── serialization ────────────────────────────────────────────────────────────

def to_dict(obj: Any) -> Any:
    if isinstance(obj, list):
        return [to_dict(x) for x in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    return obj


def save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ─── entrypoint ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="RCM claim denial analysis pipeline")
    parser.add_argument("--input",      default="data/synthetic_claims.json")
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--claim-id",   help="Only analyze this one claim ID")
    args = parser.parse_args()

    with open(args.input) as f:
        all_claims: List[dict] = json.load(f)

    denied = [c for c in all_claims if get_status(c) == "denied"]
    if args.claim_id:
        denied = [c for c in denied if get_claim_id(c) == args.claim_id]
        if not denied:
            print(f"No denied claim found with ID {args.claim_id}")
            return

    print(f"Loaded {len(all_claims)} total claims, {len(denied)} denied.")

    matcher  = HistoricalMatcher(all_claims)
    analyzer = DenialAnalyzer(matcher)

    analyses: List[Analysis] = []
    for claim in denied:
        result = analyzer.analyze(claim)
        analyses.append(result)
        print(f"  {result.claim_id} → {result.recoverability_verdict} (conf: {result.confidence_score})")

    clusters   = build_clusters(all_claims, analyses)
    evaluation = run_evaluation(analyses, all_claims)

    save_json(f"{args.output_dir}/claim_analyses.json",  to_dict(analyses))
    save_json(f"{args.output_dir}/denial_clusters.json", clusters)
    save_json(f"{args.output_dir}/evaluation.json",      evaluation)

    print(f"\nTop cluster: {clusters[0]['summary'] if clusters else 'n/a'}")
    print(f"Accuracy: {evaluation['verdict_accuracy']} | Avg confidence: {evaluation['avg_confidence']}")
    print(f"Results written to {args.output_dir}/")


if __name__ == "__main__":
    main()
