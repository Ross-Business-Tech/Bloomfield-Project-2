"""Tests for the rents reader. Fake pages; every name is FAKE."""
import json

from bot import rents
from collectors.common import db
from collectors.common.files import store_file
from scout_reader_schema import create_scout_reader_tables, upsert

PAGE = """<html><body><h1>FAKE OAKS APARTMENTS</h1>
<p>Now pre-leasing! 1 bedrooms from $1,450/mo, 2 bedrooms from $1,950/mo.</p>
<p>Limited time: 6 weeks free rent on select homes. $99 application fee. Open late 2027.</p></body></html>"""


def seed(con, page=PAGE):
    create_scout_reader_tables(con)
    upsert(con, "projects", {"project_id": "p1", "project_name": "FAKE OAKS", "city": "Fake City",
                             "state": "MI", "main_url": "https://fake-oaks.example.com/"})
    res = store_file(con, page.encode(), source="project_site", doc_category="other", collector_name="test",
                     ext="html", url="https://fake-oaks.example.com/")
    return res.file_id


def observations(con):
    return con.execute("SELECT project_id, asking_rent, concession_months, source_url, file_id "
                       "FROM rent_observations").fetchall()


def test_lowest_rent_and_concession_are_read(con, env):
    fid = seed(con)
    rents.collect(con)
    (row,) = observations(con)
    assert row[0] == "p1" and row[1] == 1450.0            # lowest advertised rent, not the average
    assert row[2] == 6 * 7 / 30.44 and row[3] == "https://fake-oaks.example.com/" and row[4] == fid
    rents.collect(con)                                    # same page twice: no duplicate reading
    assert len(observations(con)) == 1


def test_no_rent_language_means_no_row(con, env):
    seed(con, "<html><body><p>FAKE page about floor plans and parking. Contact us for pricing.</p></body></html>")
    rents.collect(con)
    assert observations(con) == []


def test_prices_without_rent_context_are_ignored(con, env):
    seed(con, "<html><body><p>FAKE insurance ad: save $500 on car insurance. Deposit: $500.</p></body></html>")
    rents.collect(con)
    assert observations(con) == []
