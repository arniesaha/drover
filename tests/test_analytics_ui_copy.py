from pathlib import Path


def test_analytics_labels_total_as_observed_sessions():
    cockpit = Path("apps/drover/Drover/Screens/Cockpit")
    sources = "\n".join(path.read_text() for path in cockpit.glob("*.swift"))

    assert sources.count('label: "Observed sessions"') == 1
    assert sources.count('"Observed sessions"') == 2
