"""Assert the built site and report describe the requested source and release."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True, type=Path)
parser.add_argument("--report", required=True, type=Path)
parser.add_argument("--version", required=True)
parser.add_argument("--edition", default="extended")
args = parser.parse_args()
report = json.loads(args.report.read_text())
assert report["status"] == "succeeded", report
assert report["hugo"]["requestedVersion"] == args.version, report
assert report["hugo"]["edition"] == args.edition, report
assert report["source"]["commitSHA"], report
text = "\n".join(path.read_text() for path in args.output.rglob("*.html"))
assert "SHORTCODE_RENDERED" in text
assert "DRAFT_MUST_NOT_BE_PUBLISHED" not in text
assert "https://example.invalid/frontmatter/" in text
assert "https://wrong-base-url.invalid/" not in text
assert (args.output / "images/marker.svg").is_file()
assert not (args.output / ".frontmatter").exists()
print(f"Verified theme, shortcode, draft exclusion, assets, base URL, and Hugo {args.version} ({args.edition}).")
