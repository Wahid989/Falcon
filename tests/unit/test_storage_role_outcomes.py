"""Every role outcome the agents log is one the storage service accepts.

The Role Manager writes role-change records with an ``outcome``; the .NET
ingester and the SQL schema each keep their own list of known outcomes and
reject anything else. A new outcome on the Python side must be added to both,
or those records end up as ingestion rejects instead of in RoleHandovers.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PARSER = REPO / "storage/dotnet/src/FalconStorage/Ingestion/LogRecordParser.cs"
SCHEMA = REPO / "storage/sql/init/002_create_exchange_schema.sql"


def logged_outcomes():
    """The outcome passed to every _record_change call in src/roles."""
    found = set()
    for path in (REPO / "src/roles").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        found |= set(re.findall(r'_record_change\(\s*[^,()]+,\s*"([a-z_]+)"', source))
    return found


def parser_set(name):
    source = PARSER.read_text(encoding="utf-8")
    body = re.search(name + r"\s*=\s*new\([^)]*\)\s*\{(.*?)\};", source, re.S)
    assert body, f"{name} not found in {PARSER.name}"
    return set(re.findall(r'"([A-Z_]+)"', body.group(1)))


def schema_check(name):
    source = SCHEMA.read_text(encoding="utf-8")
    body = re.search(r"CONSTRAINT " + name + r" CHECK \((.*?)\),\n", source, re.S)
    assert body, f"{name} not found in {SCHEMA.name}"
    return body.group(1)


def test_the_scan_finds_the_role_outcomes():
    assert {"granted", "denied", "self_promoted", "successor_accepted"} <= (
        logged_outcomes()
    )


def test_the_ingester_accepts_every_logged_outcome():
    known = parser_set("RoleOutcomes")
    assert {o.upper() for o in logged_outcomes()} <= known


def test_the_schema_accepts_every_logged_outcome():
    allowed = set(re.findall(r"'([A-Z_]+)'", schema_check("CK_RoleHandovers_Outcome")))
    assert {o.upper() for o in logged_outcomes()} <= allowed


def test_every_outcome_has_an_epoch_rule_in_the_schema():
    # The epoch check names each outcome as either moving the epoch or not.
    rule = schema_check("CK_RoleHandovers_EpochChange")
    for outcome in logged_outcomes():
        assert f"'{outcome.upper()}'" in rule


def test_the_ingester_and_schema_agree_on_which_outcomes_raise_the_epoch():
    advancing = parser_set("EpochAdvancingOutcomes")
    rule = schema_check("CK_RoleHandovers_EpochChange")
    raising = re.split(r"\s+OR\s+\(", rule, maxsplit=1)[0]
    assert advancing == set(re.findall(r"'([A-Z_]+)'", raising))
