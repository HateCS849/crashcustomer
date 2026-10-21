"""Build a local source/evidence archive without runtime data, credentials or dependencies."""
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRECTORIES = ("crashcustomer", "static", "scripts", "examples", "tests", "docs")
FILES = ("README.md", "pyproject.toml", "uv.lock", ".env.example", ".gitignore")


def build():
    evidence = ROOT / "docs" / "evidence"
    evidence.mkdir(exist_ok=True)
    screenshot = ROOT / "data" / "upgrade-verified-20261021.png"
    if screenshot.exists():
        (evidence / screenshot.name).write_bytes(screenshot.read_bytes())
    paths = [ROOT / name for name in FILES if (ROOT / name).is_file()]
    for directory in DIRECTORIES:
        paths.extend(p for p in (ROOT / directory).rglob("*") if p.is_file()
                     and not p.is_symlink() and "__pycache__" not in p.parts
                     and p.suffix != ".pyc" and not any(part.startswith(".") for part in p.relative_to(ROOT).parts))
    paths = sorted(set(paths))
    records = [{"path": str(p.relative_to(ROOT)), "bytes": p.stat().st_size,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
    assert all(r["path"] != ".env" and not r["path"].startswith(("data/", ".venv/", ".git/")) for r in records)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "files": records,
                "scope": "Local source and synthetic evaluation evidence. No .env, runtime DB/uploads/Trace, venv, or Hangari source.",
                "langsmith": json.loads((ROOT / "docs" / "verification-langsmith-campaign-20261021.json").read_text()) if (ROOT / "docs" / "verification-langsmith-campaign-20261021.json").exists() else {"status": "not_verified"}}
    destination = ROOT / "output"
    destination.mkdir(exist_ok=True)
    archive = destination / "crashcustomer-submission.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for p in paths:
            bundle.write(p, "crashcustomer/" + str(p.relative_to(ROOT)))
        bundle.writestr("crashcustomer/submission-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        assert "crashcustomer/.env" not in bundle.namelist()
        for record in records:
            content = bundle.read("crashcustomer/" + record["path"])
            assert hashlib.sha256(content).hexdigest() == record["sha256"]
    (destination / "submission-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"archive": str(archive), "files": len(paths), "bytes": archive.stat().st_size,
                      "langsmith": manifest["langsmith"]["status"]}, ensure_ascii=False))


if __name__ == "__main__": build()
