#!/usr/bin/env python3
############################################################################################################
# Copyright (c) 2025 AXP.OS <project |AT| axpos #dot# org>
############################################################################################################

"""
deblob-bp.py -- disable modules in Android.bp files based on strict regex matches using bpflatten.

Usage:
  ./deblob-bp.py --regex "bad_mod|evil_dep" --path vendor/yourvendor --bpmodify /path/to/bpmodify

Notes:
 - regex is a pipe-separated list of regexes. Empty entries are ignored.
 - Fields checked: name, and any property key whose last segment is in:
   shared_libs, src, srcs, deps, imports, jars, apk (including target.* nested keys).
 - Whitelisted module types are skipped entirely.
 - Modules already containing `enabled: false` are skipped.
 - Matches are logged to --log (default: out/deblobbing/<path_with_slashes_replaced>.log).
 - After regex match, also disables any module depending (directly or indirectly) on disabled ones.
"""

import argparse
import subprocess
import sys
import re
import json
from pathlib import Path
from collections import defaultdict, deque

WHITELIST_TYPES = {"hidl_interface", "soong_namespace", "hidl_package_root"}
MATCH_FIELDS = {"shared_libs", "apk", "src", "srcs", "deps", "imports", "jars"}

def parse_args():
    p = argparse.ArgumentParser(description="Disable Android.bp modules matching given regex patterns using bpflatten.")
    p.add_argument("--regex", required=True,
                   help="Pipe-separated list of regex patterns (matches name and dependency-like fields).")
    p.add_argument("--path", required=True, help="Path to a .bp file or directory containing .bp files.")
    p.add_argument("--bpmodify", default="bpmodify", help="Path to bpmodify binary (default: in $PATH).")
    p.add_argument("--bpflatten", default="bpflatten", help="Path to bpflatten binary (default: in $PATH).")
    p.add_argument("--log", help="Path to log file. Default: out/deblobbing/<path_with_slashes_replaced>.log")
    return p.parse_args()

def get_default_log_path(bp_path: Path) -> Path:
    """
    Return a clean log file path under out/deblobbing/
    for either a single .bp file or a directory.
    """
    # For directories, just use the directory name
    if bp_path.is_dir():
        safe = str(bp_path).strip("/").replace("/", "_").replace("\\", "_")
        if safe.endswith("_"):
            safe = safe[:-1]
        return Path("out/deblobbing") / f"{safe}.log"

    # For individual files
    if bp_path.is_file():
        safe = str(bp_path).strip("/").replace("/", "_").replace("\\", "_")
        return Path("out/deblobbing") / f"{safe}.log"

    # If path doesn't exist yet, just sanitize string
    safe = str(bp_path).strip("/").replace("/", "_").replace("\\", "_")
    return Path("out/deblobbing") / f"{safe}.log"

def compile_patterns(regex_param: str):
    parts = [p for p in regex_param.split("|") if p and p.strip()]
    if not parts:
        print(f"Error: No valid regex patterns provided (all parts empty after splitting): '{regex_param}'", file=sys.stderr)
        raise SystemExit(1)
    try:
        return [re.compile(p) for p in parts]
    except re.error as e:
        print(f"Error: Invalid regex in patterns '{regex_param}': {e}", file=sys.stderr)
        raise SystemExit(1)

def log_write(log_f, line: str):
    log_f.write(line.rstrip("\n") + "\n")

def matches_any_pattern(value: str, patterns):
    return any(p.search(value) for p in patterns)

def scan_props(props, patterns, mod_name, bp_file, log_f, prefix=""):
    """Recursively scan PropertyMap for keys ending with MATCH_FIELDS and check string values."""
    if not isinstance(props, dict):
        return False

    for key, val in props.items():
        sep = "." if prefix else ""
        full_key = f"{prefix}{sep}{key}" if prefix or key else key

        if any(full_key.endswith(field) for field in MATCH_FIELDS):
            values = []
            if isinstance(val, list):
                values = val
            elif isinstance(val, str):
                values = [val]
            else:
                values = []
            for item in values:
                if isinstance(item, str):
                    if matches_any_pattern(item, patterns):
                        log_write(log_f, f"[MATCH] regex-match in '{full_key}' value='{item}' module='{mod_name}' file='{bp_file}'")
                        return True
                elif isinstance(item, dict):
                    if scan_props(item, patterns, mod_name, bp_file, log_f, prefix=full_key):
                        return True

        if isinstance(val, dict):
            if scan_props(val, patterns, mod_name, bp_file, log_f, prefix=full_key):
                return True
        if isinstance(val, list):
            for element in val:
                if isinstance(element, dict):
                    if scan_props(element, patterns, mod_name, bp_file, log_f, prefix=full_key):
                        return True
    return False

def extract_dependencies(props):
    """Return a list of dependency module names from MATCH_FIELDS in props."""
    deps = []
    if not isinstance(props, dict):
        return deps
    for key, val in props.items():
        if any(key.endswith(field) for field in MATCH_FIELDS):
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        deps.append(item)
            elif isinstance(val, str):
                deps.append(val)
        if isinstance(val, dict):
            deps.extend(extract_dependencies(val))
        if isinstance(val, list):
            for element in val:
                if isinstance(element, dict):
                    deps.extend(extract_dependencies(element))
    return deps

def process_module(mod, patterns, bp_file, log_f):
    """Check if a module matches regex patterns directly."""
    mod_type = mod.get("Type")
    mod_name = mod.get("Name")
    props = mod.get("PropertyMap", {})

    if mod_type in WHITELIST_TYPES:
        log_write(log_f, f"[SKIP-WHITELIST] type='{mod_type}' module='{mod_name or '?'}' file='{bp_file}'")
        return None

    if str(props.get("enabled", "")).strip().lower() == "false":
        log_write(log_f, f"[SKIP-ALREADY-DISABLED] module='{mod_name or '?'}' file='{bp_file}'")
        return None

    if mod_name and matches_any_pattern(mod_name, patterns):
        log_write(log_f, f"[MATCH] regex-match in 'name' value='{mod_name}' module='{mod_name}' file='{bp_file}'")
        return mod_name

    if scan_props(props, patterns, mod_name, bp_file, log_f):
        return mod_name

    return None

def flatten_bp_file(bp_file, bpflatten_bin):
    try:
        result = subprocess.run([bpflatten_bin, "-json", str(bp_file)],
                                check=True, capture_output=True, text=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Error: bpflatten failed for {bp_file}: {e}", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse bpflatten output for {bp_file}: {e}", file=sys.stderr)
        return []

def main():
    args = parse_args()
    try:
        patterns = compile_patterns(args.regex)
    except SystemExit:
        sys.exit(1)

    bp_path = Path(args.path)
    if not bp_path.exists():
        print(f"Error: path '{bp_path}' does not exist", file=sys.stderr)
        sys.exit(1)

    log_path = Path(args.log) if args.log else get_default_log_path(bp_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    bp_files = [bp_path] if bp_path.is_file() else list(bp_path.rglob("*.bp"))
    if not bp_files:
        print(f"Error: No .bp files found under {bp_path}", file=sys.stderr)
        sys.exit(1)

    all_modules = {}         # module_name -> (file_path, props)
    dep_graph = defaultdict(set)  # module_name -> set(dependencies)
    disabled = set()         # modules to disable

    with log_path.open("w") as log_f:
        for f in bp_files:
            log_write(log_f, f"[INFO] scanning file '{f}'")
            modules = flatten_bp_file(f, args.bpflatten)
            for mod in modules:
                name = mod.get("Name")
                props = mod.get("PropertyMap", {})
                if not name:
                    continue
                all_modules[name] = (f, props)

                deps = extract_dependencies(props)
                for d in deps:
                    dep_graph[name].add(d)

                mod_name = process_module(mod, patterns, f, log_f)
                if mod_name:
                    disabled.add(mod_name)

        # Cascade disable: BFS until no new modules are found
        queue = deque(disabled)
        while queue:
            current = queue.popleft()
            for mod_name, deps in dep_graph.items():
                if mod_name not in disabled and any(dep in disabled for dep in deps):
                    disabled.add(mod_name)
                    queue.append(mod_name)
                    file_path, _ = all_modules[mod_name]
                    log_write(log_f, f"[MATCH-DEPENDENCY] module='{mod_name}' depends on disabled module(s) -> file='{file_path}'")

    # Apply disabling via bpmodify
    for module in sorted(disabled):
        file_path, _ = all_modules[module]
        try:
            subprocess.run([
                args.bpmodify,
                "-m", module,
                "-property", "enabled",
                "-set-bool", "false",
                "-w", str(file_path)
            ], check=True)
        except subprocess.CalledProcessError as e:
            with log_path.open("a") as log_f:
                log_write(log_f, f"[ERROR] bpmodify failed for module='{module}' file='{file_path}' returncode={e.returncode}")
            print(f"Error: bpmodify failed for {module} in {file_path} (see log).", file=sys.stderr)

if __name__ == "__main__":
    main()

