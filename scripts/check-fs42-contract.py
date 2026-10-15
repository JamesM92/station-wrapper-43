#!/usr/bin/env python3
"""Check that a vendored FieldStation42 still offers what this wrapper uses.

Two consumers, both checked against FS42's source text:
  - fs42-tuner/resolver.py reads FS42's sqlite db directly, so the contract is
    the table definition and the plan JSON it stores.
  - scripts/build_channels.py drives the web console (station and catalog
    routes, the shape of their answers) and the station_42.py CLI.
Whether the station configs it writes still match FS42's own schema is checked
by scripts/test_build_channels.py, which sync-fs42.sh also runs.

This asserts only what we depend on. No third-party imports: it reads source
text, so it runs anywhere.

Usage: check-fs42-contract.py <path to FieldStation42 root>
"""
import re
import sys
from pathlib import Path

REQUIRED_COLUMNS = ["station", "start_time", "end_time", "title", "plan_json"]
REQUIRED_PLAN_KEYS = ["path", "skip", "duration", "is_stream", "content_type"]


# (file, what build_channels.py relies on, regex that must match). Routes are
# the console's own decorators; the prefix is declared once per router.
GENERATOR_EXPECTATIONS = [
    ("fs42/fs42_server/api/stations.py", 'router prefix "/stations"', r'APIRouter\(prefix="/stations"'),
    ("fs42/fs42_server/api/stations.py", "GET /stations (list)", r'@router\.get\(""'),
    ("fs42/fs42_server/api/stations.py", "POST /stations (create)", r'@router\.post\(""'),
    ("fs42/fs42_server/api/stations.py", "PUT /stations/{network_name} (update)", r'@router\.put\("/\{network_name\}"'),
    ("fs42/fs42_server/api/stations.py", 'list answer {"stations": [{"station_conf": ...}]}',
     r'"stations":\s*raw_stations'),
    ("fs42/fs42_server/api/stations.py", "list entries are keyed by station_conf", r'raw\.get\("station_conf"\)'),
    ("fs42/fs42_server/api/build.py", 'router prefix "/build"', r'APIRouter\(prefix="/build"'),
    ("fs42/fs42_server/api/build.py", "POST /build/catalog/{network_name}", r'@router\.post\("/catalog/\{network_name\}"'),
    ("fs42/fs42_server/api/build.py", "GET /build/catalog/status/{task_id}", r'@router\.get\("/catalog/status/\{task_id\}"'),
    ("fs42/fs42_server/api/build.py", 'catalog start answers {"task_id": ...}', r'return \{"task_id": task_id\}'),
    ("fs42/fs42_server/api/build.py", 'status value "done"', r'\["status"\]\s*=\s*"done"'),
    ("fs42/fs42_server/api/build.py", 'status value "error"', r'\["status"\]\s*=\s*"error"'),
    ("station_42.py", "the -d / --add_day option", r'"-d",\s*"--add_day"'),
    ("station_42.py", 'the "I added a day to" confirmation', r'I added a day to'),
    ("fs42/station_config_schema.json", "the station config schema", r'"station_conf"'),
    ("fs42/liquid_schedule.py", "LiquidSchedule._calc_target_duration (console_launch.py wraps it)",
     r'def _calc_target_duration\(self, duration, increment=None, break_info=None, break_strategy=None\)'),
    ("fs42/catalog.py", "ShowCatalog.make_reel_fill (console_launch.py wraps it for the mid card option)",
     r'def make_reel_fill\(self, when, length, use_bumpers=True'),
    ("fs42/liquid_schedule.py", 'a slot\'s own "bump_dir" (the schedule editor\'s mid card blocks)', r'slot_config.get\("bump_dir"'),
    ("fs42/liquid_manager.py", "LiquidManager.reset_sequences (console_launch.py guards it)", r'def reset_sequences\(self, station_config\)'),
    ("fs42/media_processor.py", "MediaProcessor.rich_find_media and _rfind_media (console_launch.py skips dead links)",
     r'def rich_find_media\(path: str, media_filter="video"\)[\s\S]*def _rfind_media\('),
    ("fs42/sequence.py", "NamedSequence.populate sorts the episodes (console_launch.py puts the natural order in its place)",
     r'def populate\(self, file_list\)[\s\S]*sorted\(self\.episodes, key=lambda entry: entry\.fpath\)'),
    ("fs42/sequence_io.py", "SequenceIO.update_sequence_entries (console_launch.py replaces its sort)",
     r'def update_sequence_entries\(self, station_name: str, sequence_name: str, tag_path: str, file_list: list, current_file: str, fallback_index: int\)'),
    ("station_42.py", "the -s / --server option the console is started with", r'"-s",\s*"--server"'),
]


def check_generator(root, failures):
    for rel, what, pattern in GENERATOR_EXPECTATIONS:
        path = root / rel
        if not path.is_file():
            failures.append(f"{rel} not found (build_channels.py relies on: {what})")
        elif not re.search(pattern, path.read_text()):
            failures.append(f"{rel}: no longer has {what} (build_channels.py relies on it)")


def main(root):
    root = Path(root)
    failures = []
    check_generator(root, failures)

    io_src = (root / "fs42" / "liquid_io.py").read_text()
    m = re.search(r"CREATE TABLE IF NOT EXISTS liquid_blocks \((.*?)\)\"\"\"", io_src, re.S)
    if not m:
        failures.append("liquid_io.py: no 'CREATE TABLE IF NOT EXISTS liquid_blocks' found")
    else:
        body = m.group(1)
        columns = {
            line.strip().split()[0]
            for line in body.splitlines()
            if line.strip() and not line.strip().upper().startswith(("PRIMARY", "UNIQUE"))
        }
        for col in REQUIRED_COLUMNS:
            if col not in columns:
                failures.append(f"liquid_blocks is missing column '{col}' (resolver.py reads it)")

    if not re.search(r"plan_json\s*=\s*json\.dumps\(\[p\.toJSON\(\)\s+for p in block\.plan\]\)", io_src):
        failures.append("liquid_io.py: plan_json is no longer json.dumps([p.toJSON() for p in block.plan])")

    plan_src = (root / "fs42" / "block_plan.py").read_text()
    tojson = re.search(r"def toJSON\(self\):\s*return\s*(\{.*?\})", plan_src, re.S)
    if not tojson:
        failures.append("block_plan.py: BlockPlanEntry.toJSON not found")
    else:
        for key in REQUIRED_PLAN_KEYS:
            if f'"{key}"' not in tojson.group(1):
                failures.append(f"BlockPlanEntry.toJSON has no '{key}' key (resolver.py reads it)")

    if failures:
        print("FieldStation42 contract check FAILED:")
        for f in failures:
            print(f"  - {f}")
        print("Update fs42-tuner/resolver.py or scripts/build_channels.py to match before relying on this sync.")
        return 1

    print("FieldStation42 contract check passed:")
    print(f"  liquid_blocks columns: {', '.join(REQUIRED_COLUMNS)}")
    print(f"  plan keys: {', '.join(REQUIRED_PLAN_KEYS)}")
    print(f"  generator: {len(GENERATOR_EXPECTATIONS)} console routes, answers and CLI details")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
