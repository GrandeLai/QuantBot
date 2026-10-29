import io
import json

import pytest

from quantbot.intraday import attribution, research
from tests.test_intraday_policy_snapshots import archived_protocol  # noqa: F401
from tests.test_intraday_execution import model, section  # noqa: F401


def test_diagnostic_cli_uses_archived_evidence_without_new_protocol(archived_protocol, tmp_path):  # noqa: F811
    protocol, mapping, _ = archived_protocol
    research.freeze(protocol, tmp_path / "manifest.json", tmp_path / "frozen", policy_snapshots=mapping)
    study = tmp_path / "study"
    research.run(protocol, tmp_path / "frozen/execution-freeze.json", study)
    attribution.attribute(protocol, study / "report.json", tmp_path / "attribution.json")
    out = io.StringIO()
    assert research.main(["diagnose", "--attribution", str(tmp_path / "attribution.json"),
                          "--output", str(tmp_path / "diagnostics.json")], stdout=out) == 0
    summary = json.loads(out.getvalue())
    assert summary["replays"] == 0
    assert summary["market_coverage"]["stocks"]["status"] == "blocked_data"
    assert not summary["trading_enabled"]


@pytest.mark.parametrize("args", [
    ["check-evidence", "--data-manifest", "missing.json", "--output", "out.json"],
    ["validate-prepare", "--protocol", "missing.json", "--study", "report.json"],
    ["validate-run", "--output", "out"],
    ["validate-assess", "--freeze", "missing.json"],
    ["diagnose", "--attribution", "missing.json"],
    ["validate-run", "--freeze", "missing.json", "--output", "out", "--policy-snapshots", "mapping.json"],
])
def test_missing_arguments_or_invalid_policy_override_stop_before_io(args):
    with pytest.raises(SystemExit) as exc:
        research.main(args, stdout=io.StringIO())
    assert exc.value.code == 2
