"""Tests for bot/discover.py (permit-driven PROJECT discovery). Fake permits; every name is FAKE.

(Not to be confused with tests/test_discover.py: county-WEBSITE screening in collectors/discover.py.)"""
import json

from bot import discover
from collectors.common.files import store_file
from scout_reader_schema import create_scout_reader_tables, upsert


def seed_permit(con, num, desc, work="New", units="24"):
    create_scout_reader_tables(con)
    raw = {"permit_number": num, "description": desc, "work_class": work, "housing_units": units}
    res = store_file(con, json.dumps(raw).encode(), source="permit", doc_category="building_permit",
                     collector_name="test", ext="json")
    upsert(con, "filing_fields", {"file_id": res.file_id, "filing_type": "building_permit",
                                  "permit_number": num, "property_address": "100 FAKE ST",
                                  "property_city": "Fake City", "property_state": "TX", "status": "ok"})
    return res.file_id


def test_new_multifamily_building_becomes_a_project(con, env):
    seed_permit(con, "FAKE-1", "New 3-story apartment building with 24 dwelling units")
    discover.discover(con)
    (row,) = con.execute("SELECT project_id, project_name, city, state, property_type, units_or_beds "
                         "FROM projects").fetchall()
    assert row[0] == "permit:FAKE-1" and "100 FAKE ST" in row[1]
    assert row[2:5] == ("Fake City", "TX", "multifamily") and row[5] == 24
    discover.discover(con)                                  # second run: no duplicates
    assert con.execute("SELECT count(*) FROM projects").fetchone()[0] == 1


def test_a_reroof_is_not_a_project(con, env):
    seed_permit(con, "FAKE-2", "Reroof existing office building", work="Alteration", units="")
    discover.discover(con)
    assert con.execute("SELECT count(*) FROM projects").fetchone()[0] == 0


def test_new_build_without_project_words_is_not_a_project(con, env):
    seed_permit(con, "FAKE-3", "New fence and dumpster enclosure")
    discover.discover(con)
    assert con.execute("SELECT count(*) FROM projects").fetchone()[0] == 0


def test_one_development_with_many_building_permits_becomes_one_project(con, env):
    seed_permit(con, "FAKE-9", "New multifamily building (Building 1)", units="145")
    seed_permit(con, "FAKE-10", "New multifamily building (Building 2)", units="70")
    discover.discover(con)
    rows = con.execute("SELECT project_id, units_or_beds FROM projects").fetchall()
    assert len(rows) == 1 and rows[0][1] == 145        # same address: the biggest permit stands for the project


def test_accessory_permits_are_not_projects(con, env):
    seed_permit(con, "FAKE-11", "New carport for apartments, Building 5", units="0")
    seed_permit(con, "FAKE-12", "New parking garage for new apartments", units="")
    discover.discover(con)
    assert con.execute("SELECT count(*) FROM projects").fetchone()[0] == 0


def test_trade_permits_are_not_projects(con, env):
    seed_permit(con, "FAKE-13", "ETJ New construction multi-family EP Electrical Permit")
    seed_permit(con, "FAKE-14", "New construction clubhouse for apartments", units="")
    discover.discover(con)
    assert con.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
