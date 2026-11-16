"""
cost_reports.py - the SNF cost-report lane (CMS Skilled Nursing Facility Cost Report PUF).

WHAT THIS DATA IS - read before trusting it:
  * The files are the public "CostReportsnf_Final_YY.csv" downloads. They reach us through
    the manual inbox (data/inbox/cms/ with a sidecar naming source_record_id
    "cms_snf_cost_report_fyYYYY") until a screened download path is added; this module
    reads stored copies only and never downloads.
  * Every figure is AS REPORTED, with the source's worksheet line where published. Values
    are never adjusted, summed across years, or annualized. An 11-month period stays an
    11-month period.
  * Currency units are UNCONFIRMED (the dictionary's units note has not been verified here).
  * A facility can appear more than once in a file and the file has no final/amended status
    field, so keeping one row per facility per file is a RECORDED CHECK, not proof the row
    is the final report. The source's row id (rpt_rec_num) is always stored.
  * Negative or odd balance-sheet components are preserved exactly as reported and flagged,
    with NO inference of insolvency. A reported loss is not cash distress, and a historical
    report is not evidence of current distress (the caveat travels with every row).

Run it with:   python -m research ingest     (or import and call collect(con))
"""
from __future__ import annotations

from datetime import date

from collectors import state as state_collector
from collectors.common import config
from collectors.common.log import RunTracker, get_logger
from collectors.common.parse import clean_str, to_date, to_float

from . import evidence as ev
from .cms_pool import _read_csv
from .identity import key_for_ccn, normalize_ccn
from .schema import create_research_tables

log = get_logger("research.cost_reports")
COLLECTOR = "research:cost_reports"
SOURCE = "cms_snf_cost_report"

# field_name -> (possible CSV headers, the worksheet line the source publishes for it, if known).
# Only lines stated in the source dictionary are recorded; unknown lines stay None.
COST_REPORT_FIELDS = {
    "net_income": (["Net Income"], "G3 L31"),
    "net_income_patient_service": (["Net income from service to patients",
                                    "Net Income from Service to Patients"], "G3 L5"),
    "fund_balances": (["Total fund balances"], "G L59"),
    "current_assets": (["Total Current Assets"], "G L11"),
    "current_liabilities": (["Total current liabilities"], "G L43"),
    "total_liabilities": (["Total Liabilities"], "G L51"),
    "net_patient_revenue": (["Net Patient Revenue"], None),
    "operating_expense": (["Less Total Operating Expense"], None),
}

# Balance-sheet totals that are not expected to be negative. A negative value is preserved
# exactly as reported and flagged - with no inference of insolvency.
ODD_COMPONENT_FIELDS = {"fund_balances", "current_assets", "current_liabilities", "total_liabilities"}

PARTIAL_YEAR_MONTHS = 12


def parse_cost_report(text: str, *, file_id: str, source_url: str | None, vintage: str) -> list[ev.Evidence]:
    """One cost-report file -> financial_fact evidence rows (see the module docstring)."""
    idx, rows = _read_csv(text)
    ccn_i = state_collector._col(idx, ["Provider CCN", "CMS Certification Number (CCN)",
                                       "Federal Provider Number"])
    if ccn_i is None:
        raise ValueError(f"[{SOURCE}] no CCN column found; the file's headers changed?")
    rpt_i = state_collector._col(idx, ["rpt_rec_num", "Rpt Rec Num", "Report Record Number"])
    name_i = state_collector._col(idx, ["Facility Name"])
    beg_i = state_collector._col(idx, ["Fiscal Year Begin Date"])
    end_i = state_collector._col(idx, ["Fiscal Year End Date"])
    pos = {k: state_collector._col(idx, aliases) for k, (aliases, _line) in COST_REPORT_FIELDS.items()}

    by_ccn: dict[str, list[dict]] = {}
    for parts in rows:
        def cell(p):
            return clean_str(parts[p]) if p is not None and p < len(parts) else None
        ccn = normalize_ccn(cell(ccn_i))
        if not ccn:
            continue
        by_ccn.setdefault(ccn, []).append({
            "rpt_rec_num": cell(rpt_i), "facility_name": cell(name_i),
            "begin": to_date(cell(beg_i)), "end": to_date(cell(end_i)),
            "values": {k: to_float(cell(p)) for k, p in pos.items()},
        })

    out: list[ev.Evidence] = []
    for ccn, candidates in by_ccn.items():
        row, check_note = select_one_row_per_ccn(candidates)
        begin, end = row["begin"], row["end"]
        months = ev.months_between(begin, end) if begin and end else None
        caveats = [ev.COST_REPORT_CAVEAT]
        if months is not None and months < PARTIAL_YEAR_MONTHS:
            caveats.append(f"{ev.PARTIAL_YEAR_CAVEAT} ({months} months stated)")
        if check_note:
            caveats.append(check_note)
        base = dict(claim_type="financial_fact", source_name=SOURCE, identity_method="ccn",
                    identity_key=key_for_ccn(ccn), raw_record_id=row["rpt_rec_num"],
                    period_begin=begin, period_end=end, period_months=months,
                    units="usd_unconfirmed", unit_confidence="unconfirmed",
                    source_url=source_url, file_id=file_id, dataset_vintage=vintage,
                    coverage_limit="one facility, one stated fiscal period, as reported")
        for field, value in row["values"].items():
            if value is None:
                continue                    # missing data stays unknown - never stored as healthy
            field_caveats = list(caveats)
            if field in ODD_COMPONENT_FIELDS and value < 0:
                field_caveats.append(f"negative {field.replace('_', ' ')} preserved exactly as "
                                     f"reported ({value:,.0f}); no inference of insolvency")
            out.append(ev.Evidence(
                f"{SOURCE}:{ccn}:{field}:{row['rpt_rec_num'] or 'unknown-row'}",
                field_name=field, worksheet_ref=COST_REPORT_FIELDS[field][1],
                value_number=value, caveat=" ".join(field_caveats), **base))
    return out


def select_one_row_per_ccn(candidates: list[dict]) -> tuple[dict, str | None]:
    """
    Keep one row per facility per file. Returns (row, note): the note records the check.
    The file has no final/amended status field, so this is never proof of final status.
    """
    if len(candidates) == 1:
        return candidates[0], None
    def rpt_num(r):
        try:
            return int(r["rpt_rec_num"])
        except (TypeError, ValueError):
            return -1
    chosen = max(candidates, key=rpt_num)
    return chosen, (f"{ev.ONE_ROW_CHECK_CAVEAT} ({len(candidates)} rows listed for this facility; "
                    f"kept rpt_rec_num {chosen['rpt_rec_num']})")


def _stored_files(con) -> list[tuple[str, str, str | None, str]]:
    """Every stored cost-report file: (file_id, text, url, source_record_id)."""
    out = []
    for file_id, file_path, url, srid in con.execute(
            "SELECT file_id, file_path, url, source_record_id FROM raw_files WHERE source = 'cms' "
            "AND source_record_id LIKE ? ORDER BY source_record_id, collected_at DESC",
            [SOURCE + "%"]).fetchall():
        out.append((file_id, config.from_stored_path(file_path).read_text(errors="ignore"), url, srid))
    return out


def collect(con) -> RunTracker:
    """Rebuild cost-report evidence from every stored cost-report file. Reruns replace."""
    create_research_tables(con)
    with RunTracker(con, COLLECTOR) as run:
        files = _stored_files(con)
        if not files:
            run.manual_inbox(SOURCE, "no stored cost-report files; drop the CMS SNF Cost Report PUF "
                                     "CSVs (CostReportsnf_Final_YY.csv) in data/inbox/cms/ with a "
                                     "sidecar naming source_record_id cms_snf_cost_report_fyYYYY")
            return run
        for file_id, text, url, srid in files:
            try:
                rows = parse_cost_report(text, file_id=file_id, source_url=url, vintage=str(srid))
            except ValueError as e:
                run.error(str(e))
                continue
            con.execute("DELETE FROM research_evidence WHERE file_id = ?", [file_id])
            for e in rows:
                ev.insert_evidence(con, e)
            run.new_file()
            log.info("%s: %d evidence rows from %s", COLLECTOR, len(rows), srid)
    return run
