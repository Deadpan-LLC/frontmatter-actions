"""Exercise real configuration/template failures; no partial output is a success."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
site = root / "tests/fixtures/site"
config = json.loads((root / "tests/fixtures/reusable.json").read_text())
configuration_path = root / ".test-results/failure.json"
configuration_path.parent.mkdir(exist_ok=True)


def expect_failure(temporary, name, phase, detail):
    directory = Path(temporary) / name
    result = subprocess.run([
        sys.executable, str(root / "scripts/frontmatter.py"), "build", "--workspace", str(root),
        "--config", ".test-results/failure.json", "--output", str(directory / "public"),
        "--tools", str(directory / "tools"), "--report", str(directory / "report.json"),
    ], text=True, capture_output=True)
    assert result.returncode != 0, result.stdout
    report = json.loads((directory / "report.json").read_text())
    assert report["status"] == "failed", report
    assert report["phase"] == phase, report
    assert detail in report["error"], report
    print(f"Verified {name}: {report['error']}")


with tempfile.TemporaryDirectory(prefix="frontmatter-failures-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
    config["schemaVersion"] = 99
    configuration_path.write_text(json.dumps(config))
    expect_failure(temporary, "invalid-contract", "configuration", "schemaVersion")
    config["schemaVersion"] = 1
    configuration_path.write_text(json.dumps(config))
    broken = site / "layouts/index.html"
    assert not broken.exists(), "Fixture test must not replace an existing layout"
    try:
        broken.write_text('{{ definitelyMissingFrontmatterFunction }}')
        expect_failure(temporary, "broken-template", "build", "definitelyMissingFrontmatterFunction")
    finally:
        broken.unlink()
        configuration_path.unlink()
