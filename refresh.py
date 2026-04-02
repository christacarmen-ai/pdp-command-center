#!/usr/bin/env python3
"""PDP Executive Command Center - Nightly Refresh v2
Parses SPRINT_LOG.md, CHANGELOG.md, git state, and config.json into data.json.
Dashboard reads data.json at load time.
"""

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

COMMAND_CENTER = Path("/Users/christadiaz/Desktop/PDP_Command_Center")
UNIFIED_REPO = Path("/Users/christadiaz/Desktop/UnifiedScoringPY")
PDP_DB = Path.home() / "My Drive" / "PDP Database"
LOG_FILE = COMMAND_CENTER / "refresh.log"
DATA_FILE = COMMAND_CENTER / "data.json"
CONFIG_FILE = COMMAND_CENTER / "config.json"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")


def run(cmd, cwd=None):
    """Run a command as a list, return stdout or empty string on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=120)
        return r.stdout.strip()
    except Exception:
        return ""


def run_shell(cmd_str, cwd=None):
    """Run a command string through the shell, return stdout or empty string."""
    try:
        r = subprocess.run(cmd_str, capture_output=True, text=True, cwd=cwd, timeout=120, shell=True, executable="/bin/zsh")
        return r.stdout.strip()
    except Exception:
        return ""


def gather_git():
    """Read git state from UnifiedScoringPY."""
    git_dir = UNIFIED_REPO / ".git"
    if not git_dir.exists():
        return {"engine_version": "unknown", "current_branch": "unknown",
                "last_commit_date": "unknown", "last_commit_msg": "unknown",
                "uncommitted_files": 0}

    version_file = UNIFIED_REPO / "pdp_scoring" / "VERSION"
    engine_version = version_file.read_text().strip() if version_file.exists() else "unknown"

    branch = run(["git", "branch", "--show-current"], cwd=UNIFIED_REPO)
    commit_date = run(["git", "log", "-1", "--format=%ci"], cwd=UNIFIED_REPO)[:16]
    commit_msg = run(["git", "log", "-1", "--format=%s"], cwd=UNIFIED_REPO)
    porcelain = run(["git", "status", "--porcelain"], cwd=UNIFIED_REPO)
    uncommitted = len([l for l in porcelain.splitlines() if l.strip()])

    return {
        "engine_version": engine_version,
        "current_branch": branch or "unknown",
        "last_commit_date": commit_date or "unknown",
        "last_commit_msg": commit_msg or "unknown",
        "uncommitted_files": uncommitted,
    }


def gather_tests():
    """Run pytest and parse results."""
    result = {"passing": 0, "skipped": 0, "failed": 0, "total": 0}
    if not UNIFIED_REPO.exists():
        return result

    # Try via shell to pick up PATH/uv from profile
    # uv sync first to ensure package is installed
    shell_cmds = [
        "source ~/.zshrc 2>/dev/null; uv sync --group dev 2>/dev/null; uv run pytest tests/ --tb=no -q",
        "python3 -m pytest tests/ --tb=no -q",
    ]
    for cmd_str in shell_cmds:
        output = run_shell(cmd_str, cwd=UNIFIED_REPO)
        if output and ("passed" in output or "failed" in output):
            for key, pattern in [("passing", r"(\d+) passed"),
                                  ("skipped", r"(\d+) skipped"),
                                  ("failed", r"(\d+) failed")]:
                m = re.search(pattern, output)
                if m:
                    result[key] = int(m.group(1))
            result["total"] = result["passing"] + result["skipped"] + result["failed"]
            break

    # Fallback: if tests couldn't run, preserve last known counts from existing data.json
    if result["total"] == 0 and DATA_FILE.exists():
        try:
            prev = json.loads(DATA_FILE.read_text())
            prev_tests = prev.get("tests", {})
            if prev_tests.get("total", 0) > 0:
                result = prev_tests
                result["_fallback"] = True
        except Exception:
            pass

    return result


def parse_sprint_log():
    """Parse SPRINT_LOG.md markdown table into list of sprint dicts."""
    sprint_file = UNIFIED_REPO / "_docs" / "sprints" / "SPRINT_LOG.md"
    if not sprint_file.exists():
        return []

    sprints = []
    for line in sprint_file.read_text().splitlines():
        line = line.strip()
        if not line.startswith("| S"):
            continue
        parts = [p.strip() for p in line.split("|")]
        # parts: ['', 'S002', 'convergence-fixes', 'shipped', '2026-03-07', 'summary...', '']
        if len(parts) >= 6:
            sprints.append({
                "id": parts[1],
                "name": parts[2],
                "status": parts[3],
                "date": parts[4],
                "summary": parts[5],
            })

    return sprints


def parse_changelog():
    """Parse CHANGELOG.md into structured version entries, risks, and pending items."""
    changelog_file = UNIFIED_REPO / "CHANGELOG.md"
    if not changelog_file.exists():
        return [], [], []

    text = changelog_file.read_text()

    # --- Parse version entries ---
    entries = []
    versions = re.split(r"^## ", text, flags=re.MULTILINE)
    for v in versions[1:]:
        lines = v.strip().split("\n")
        header = lines[0]
        ver_match = re.match(r"\[([^\]]+)\](?:\s*-\s*(.+))?", header)
        if not ver_match:
            continue
        version = ver_match.group(1)
        date = ver_match.group(2) or ""

        sections = {}
        current_section = None
        for line in lines[1:]:
            sec_match = re.match(r"^### (.+)", line)
            if sec_match:
                current_section = sec_match.group(1).strip()
                sections[current_section] = []
            elif current_section and line.startswith("- "):
                item = line[2:].strip()
                if len(item) > 140:
                    item = item[:137] + "..."
                sections[current_section].append(item)

        if sections:
            entries.append({"version": version, "date": date, "sections": sections})
        if len(entries) >= 5:
            break

    # --- Parse risks from [Unreleased] ---
    risks = []
    risk_section = re.search(
        r"### Risks to flag\n(.*?)(?=\n###? |\n## |\Z)", text, re.DOTALL
    )
    if risk_section:
        for line in risk_section.group(1).strip().splitlines():
            m = re.match(r"- \*\*(.+?)\*\*:?\s*(.*)", line)
            if m:
                title = m.group(1)
                detail = m.group(2)
                # Auto-assign severity
                severity = "medium"
                if any(kw in detail.lower() for kw in ["stub", "inert", "missing", "no-op", "all 13", "all 8", "every"]):
                    severity = "high"
                if any(kw in detail.lower() for kw in ["unknown whether", "minor", "layout"]):
                    severity = "low"
                risks.append({"severity": severity, "title": title, "detail": detail})

    # --- Parse pending items ---
    pending_section = re.search(
        r"### Pending[^\n]*\n(.*?)(?=\n###? |\n## |\Z)", text, re.DOTALL
    )
    if pending_section:
        for line in pending_section.group(1).strip().splitlines():
            if line.startswith("- "):
                risks.append({
                    "severity": "low",
                    "title": "Pending adjudication",
                    "detail": line[2:].strip()
                })

    return entries, risks, []


def gather_commits():
    """Get last 15 git commits across all branches."""
    if not (UNIFIED_REPO / ".git").exists():
        return []

    output = run(
        ["git", "log", "--all", "-15", "--format=%h||%ci||%s||%D"],
        cwd=UNIFIED_REPO
    )
    commits = []
    for line in output.splitlines():
        parts = line.split("||")
        if len(parts) >= 3:
            commits.append({
                "hash": parts[0],
                "date": parts[1][:16],
                "subject": parts[2],
                "branch": parts[3] if len(parts) > 3 else "",
            })
    return commits


def gather_recent_files():
    """Find recently modified source files in the repo."""
    if not UNIFIED_REPO.exists():
        return []

    files = []
    for ext in ("*.py", "*.tsx", "*.ts", "*.md"):
        for p in UNIFIED_REPO.rglob(ext):
            if any(skip in p.parts for skip in (".git", "node_modules", ".claude", ".venv", ".pytest_cache", "__pycache__")):
                continue
            try:
                mtime = p.stat().st_mtime
                files.append((str(p.relative_to(UNIFIED_REPO)), mtime))
            except OSError:
                pass

    files.sort(key=lambda x: x[1], reverse=True)
    return [
        {"path": f[0], "modified": datetime.fromtimestamp(f[1]).strftime("%Y-%m-%d %H:%M")}
        for f in files[:20]
    ]


def gather_gold_standards():
    """Parse gold standard JSON files from the repo."""
    gs_dir = UNIFIED_REPO / "gold_standards"
    if not gs_dir.exists():
        return None  # None = use config.json fallback

    standards = []
    for f in sorted(gs_dir.glob("gold_*.json")):
        try:
            d = json.loads(f.read_text())
            standards.append({
                "name": d.get("client_id", f.stem),
                "stage": str(d.get("validated_stage", "?")),
                "use": d.get("validated_stage_name", ""),
            })
        except Exception:
            pass

    return standards if standards else None


def gather_exemplar_db():
    """Scan exemplar_db/ for stage and fuzzy directories, count stems per band."""
    db_dir = UNIFIED_REPO / "exemplar_db"
    if not db_dir.exists():
        return None

    bands = []
    for d in sorted(db_dir.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        stems = list(d.glob("stem_*.json"))
        if not stems:
            continue
        band_type = "transition" if d.name.startswith("fuzzy_") else "core"
        label = d.name.replace("fuzzy_", "").replace("stage_", "")
        bands.append({
            "name": d.name,
            "label": label,
            "type": band_type,
            "stem_count": len(stems),
        })

    total_exemplars = sum(b["stem_count"] for b in bands)
    return {
        "bands": bands,
        "total_bands": len(bands),
        "total_exemplars": total_exemplars,
    }


def count_db_files():
    """Count files in PDP Database (shallow)."""
    if not PDP_DB.exists():
        return 0
    count = 0
    for root, dirs, filenames in os.walk(PDP_DB):
        depth = root.replace(str(PDP_DB), "").count(os.sep)
        if depth >= 3:
            dirs.clear()
            continue
        count += len(filenames)
    return count


def load_config():
    """Load the editable config.json with phases, decisions, etc."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def compute_phase_status(phases):
    """Auto-compute progress and status for each phase from task statuses."""
    for phase in phases:
        tasks = phase.get("tasks", [])
        if not tasks:
            phase["progress"] = 0
            phase["status"] = "queued"
            continue

        shipped = sum(1 for t in tasks if t.get("status") == "shipped")
        phase["progress"] = round((shipped / len(tasks)) * 100)

        if all(t.get("status") == "shipped" for t in tasks):
            phase["status"] = "shipped"
        elif any(t.get("status") in ("active", "shipped") for t in tasks):
            phase["status"] = "active"
        else:
            phase["status"] = "queued"

    return phases


def main():
    log("=== Nightly refresh v2 started ===")

    # Gather all data
    git = gather_git()
    log(f"Git: v{git['engine_version']} on {git['current_branch']}, {git['uncommitted_files']} uncommitted")

    tests = gather_tests()
    log(f"Tests: {tests['passing']} pass, {tests['skipped']} skip, {tests['failed']} fail")

    sprints = parse_sprint_log()
    log(f"Sprints parsed: {len(sprints)}")

    changelog_entries, risks, _ = parse_changelog()
    log(f"Changelog versions: {len(changelog_entries)}, Risks: {len(risks)}")

    commits = gather_commits()
    log(f"Git commits: {len(commits)}")

    recent_files = gather_recent_files()
    log(f"Recent files: {len(recent_files)}")

    db_file_count = count_db_files()

    gold_standards = gather_gold_standards()
    log(f"Gold standards: {len(gold_standards) if gold_standards else 'using config.json'}")

    exemplar_db = gather_exemplar_db()
    if exemplar_db:
        log(f"Exemplar DB: {exemplar_db['total_bands']} bands, {exemplar_db['total_exemplars']} exemplars")
    else:
        log("Exemplar DB: not found")

    config = load_config()
    phases = compute_phase_status(config.get("phases", []))

    # Assemble
    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "git": git,
        "tests": tests,
        "system_version": config.get("system_version", "unknown"),
        "phases": phases,
        "sprints": sprints,
        "risks": risks,
        "changelog": changelog_entries,
        "commits": commits,
        "recent_files": recent_files,
        "christa_deliverables": config.get("christa_deliverables", []),
        "jon_decisions": config.get("jon_decisions", []),
        "christa_decisions": config.get("christa_decisions", []),
        "gold_standards": gold_standards or config.get("gold_standards", []),
        "exemplar_db": exemplar_db,
        "roadmap": config.get("roadmap", []),
        "db_file_count": db_file_count,
    }

    DATA_FILE.write_text(json.dumps(data, indent=2))
    log(f"data.json written ({DATA_FILE.stat().st_size} bytes)")

    # Also inject data into index.html as inline JS so it works via file:// protocol
    dashboard = COMMAND_CENTER / "index.html"
    if dashboard.exists():
        html = dashboard.read_text()
        json_str = json.dumps(data)
        # Replace the DATA_INJECT marker
        marker_start = "// __DATA_INJECT_START__"
        marker_end = "// __DATA_INJECT_END__"
        if marker_start in html:
            start_idx = html.index(marker_start)
            end_idx = html.index(marker_end) + len(marker_end)
            injection = f"{marker_start}\nwindow.__PDP_DATA__ = {json_str};\n{marker_end}"
            html = html[:start_idx] + injection + html[end_idx:]
            dashboard.write_text(html)
            log("Injected data into index.html")

    log("=== Nightly refresh v2 complete ===")


if __name__ == "__main__":
    main()
