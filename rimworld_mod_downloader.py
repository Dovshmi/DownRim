#!/usr/bin/env python3
"""
RimWorld Steam Workshop Mod Downloader (SteamCMD) - Windows-friendly

Place this file inside your SteamCMD folder (same folder as steamcmd.exe),
then run it from that folder.

Examples:
  # Single mod item
  python rimworld_mod_downloader.py --links "https://steamcommunity.com/sharedfiles/filedetails/?id=818773962"

  # Many items
  python rimworld_mod_downloader.py --links 818773962 123456789 987654321

  # From a list file (one per line)
  python rimworld_mod_downloader.py --in mods.txt

  # Whole collection (112 items, etc.)
  python rimworld_mod_downloader.py --collection "https://steamcommunity.com/sharedfiles/filedetails/?id=1884025115"

  # Mix items + collections
  python rimworld_mod_downloader.py --links 818773962 --collection 1884025115

Verbose output:
  -v   basic progress
  -vv  more details (collections, batches)
  -vvv includes SteamCMD stdout/stderr in console (still logged to files)

Notes:
- Default AppID is RimWorld (294100).
- Default login is anonymous. Some Workshop items may require a Steam account.
- Collection expansion uses Steam Web API endpoint ISteamRemoteStorage/GetCollectionDetails.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import json
import locale
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


RIMWORLD_APPID_DEFAULT = 294100
STEAM_COLLECTION_API = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def default_steamcmd_path() -> Path:
    return script_dir() / "steamcmd.exe"


def content_root(appid: int) -> Path:
    # SteamCMD workshop downloads typically go here relative to the SteamCMD install directory.
    return script_dir() / "steamapps" / "workshop" / "content" / str(appid)


def logs_dir() -> Path:
    return script_dir() / "logs"


def now_stamp() -> str:
    # safe for filenames
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def vlog(verbose: int, level: int, msg: str) -> None:
    if verbose >= level:
        print(msg)


def read_lines_from_file(p: Path) -> List[str]:
    if not p.exists():
        raise FileNotFoundError(f"Input file not found: {p}")
    lines: List[str] = []
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        lines.append(line)
    return lines


_ID_RE = re.compile(r"(?i)\b(?:id=)?(\d{6,})\b")


def extract_workshop_id(s: str) -> Optional[str]:
    """
    Accepts:
      - numeric ID (e.g., 818773962)
      - URL with ?id=NNNNN
      - other strings that contain id=NNNNN
    Returns numeric ID as string, or None if not found.
    """
    s = s.strip()
    if not s:
        return None

    # Pure digits
    if s.isdigit():
        return s

    # URL parse first (more precise)
    try:
        u = urlparse(s)
        if u.scheme in ("http", "https") and u.netloc:
            qs = parse_qs(u.query or "")
            if "id" in qs and qs["id"]:
                cand = qs["id"][0].strip()
                if cand.isdigit():
                    return cand
    except Exception:
        pass

    # Fallback regex scan
    m = _ID_RE.search(s)
    if m:
        cand = m.group(1)
        if cand and cand.isdigit():
            return cand
    return None


def chunked(items: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def sanitize_steamcmd_args(args: List[str]) -> List[str]:
    """
    Mask password in "+login <user> <pass>" so it never appears in logs/console.
    """
    out = list(args)
    try:
        i = out.index("+login")
        # "+login anonymous" OR "+login user pass"
        if i + 1 < len(out) and out[i + 1] != "anonymous":
            if i + 3 < len(out):
                out[i + 3] = "********"
    except ValueError:
        pass
    return out


def build_steamcmd_args(
    steamcmd_exe: Path,
    login_mode: str,
    username: Optional[str],
    password: Optional[str],
    appid: int,
    ids: List[str],
) -> List[str]:
    """
    Build SteamCMD argument list for subprocess.run([...]).
    SteamCMD commands are prefixed with "+" as separate tokens.
    """
    args: List[str] = [str(steamcmd_exe)]

    # Common flags to make SteamCMD less interactive.
    args += ["+@ShutdownOnFailedCommand", "1", "+@NoPromptForPassword", "1"]

    if login_mode == "anonymous":
        args += ["+login", "anonymous"]
    else:
        if not username:
            raise ValueError("username is required for --login user")
        if password is None:
            raise ValueError("password is required for --login user (we prompt for it)")
        args += ["+login", username, password]

    for wid in ids:
        args += ["+workshop_download_item", str(appid), wid]

    args += ["+quit"]
    return args


def run_steamcmd_batch(args: List[str], cwd: Path, log_file: Path, dry_run: bool, verbose: int) -> Dict[str, Any]:
    import subprocess

    safe_args = sanitize_steamcmd_args(args)

    if dry_run:
        log_file.write_text("DRY RUN\n" + " ".join(safe_args) + "\n", encoding="utf-8", errors="replace")
        vlog(verbose, 2, f"[DRY RUN] Would run: {' '.join(safe_args)}")
        return {"returncode": 0, "stdout": "", "stderr": "", "dry_run": True, "sanitized_cmd": " ".join(safe_args)}

    # Use system preferred encoding to avoid decode errors on Windows consoles.
    enc = locale.getpreferredencoding(False) or "utf-8"

    proc = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding=enc,
        errors="replace",
    )

    combined = []
    combined.append("COMMAND:\n" + " ".join(safe_args) + "\n")
    combined.append(f"RETURN CODE: {proc.returncode}\n\n")
    if proc.stdout:
        combined.append("STDOUT:\n" + proc.stdout + "\n")
    if proc.stderr:
        combined.append("STDERR:\n" + proc.stderr + "\n")

    log_file.write_text("".join(combined), encoding="utf-8", errors="replace")

    if verbose >= 3:
        if proc.stdout:
            print(proc.stdout)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)

    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "dry_run": False,
        "sanitized_cmd": " ".join(safe_args),
    }


def looks_like_steam_guard(output: str) -> bool:
    o = output.lower()
    keywords = [
        "steam guard",
        "two-factor",
        "two factor",
        "auth code",
        "email code",
        "phone code",
    ]
    return any(k in o for k in keywords)


def verify_downloaded(appid: int, wid: str) -> Dict[str, Any]:
    mod_dir = content_root(appid) / wid
    ok = mod_dir.exists() and mod_dir.is_dir()
    size = 0
    file_count = 0
    if ok:
        try:
            for p in mod_dir.rglob("*"):
                if p.is_file():
                    file_count += 1
                    try:
                        size += p.stat().st_size
                    except OSError:
                        pass
        except Exception:
            # If we can't scan, still keep "exists" as basic success signal.
            pass
    return {
        "workshop_id": wid,
        "path": str(mod_dir),
        "exists": ok,
        "file_count": file_count,
        "bytes": size,
    }


def steam_api_post(url: str, data: Dict[str, str], timeout: int, verbose: int) -> Dict[str, Any]:
    encoded = urlencode(data).encode("utf-8")
    req = Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
    # A UA helps avoid some simple blocks.
    req.add_header("User-Agent", "rimworld_mod_downloader/1.1")

    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        text = raw.decode("utf-8", errors="replace")
        return json.loads(text)
    except HTTPError as e:
        vlog(verbose, 1, f"[ERROR] Steam API HTTPError {e.code}: {e.reason}")
        raise
    except URLError as e:
        vlog(verbose, 1, f"[ERROR] Steam API URLError: {e}")
        raise
    except json.JSONDecodeError as e:
        vlog(verbose, 1, f"[ERROR] Steam API returned invalid JSON: {e}")
        raise


def get_collection_children(collection_id: str, timeout: int, verbose: int) -> List[str]:
    """
    Returns the immediate children of a workshop collection via ISteamRemoteStorage/GetCollectionDetails.
    """
    payload = {
        "collectioncount": "1",
        "publishedfileids[0]": str(collection_id),
        "format": "json",
    }
    j = steam_api_post(STEAM_COLLECTION_API, payload, timeout=timeout, verbose=verbose)

    details = (j.get("response") or {}).get("collectiondetails") or []
    if not details:
        return []

    children = details[0].get("children") or []
    out: List[str] = []
    for c in children:
        pid = c.get("publishedfileid")
        if pid is None:
            continue
        s = str(pid)
        if s.isdigit():
            out.append(s)
    return out


def expand_collections(
    collection_ids: List[str],
    depth: int,
    timeout: int,
    verbose: int,
) -> Tuple[List[str], Dict[str, Any]]:
    """
    Expand collection IDs to item IDs.
    - depth=1: only the direct items of each collection
    - depth>1: tries to expand nested collections too (best-effort)
    Returns (expanded_item_ids, meta)
    """
    # BFS-style expansion with a depth cap.
    visited_collections = set()
    queue: List[Tuple[str, int]] = [(cid, 1) for cid in collection_ids]

    expanded_items: List[str] = []
    meta: Dict[str, Any] = {"collections": {}, "depth": depth}

    while queue:
        cid, lvl = queue.pop(0)
        if cid in visited_collections:
            continue
        visited_collections.add(cid)

        vlog(verbose, 2, f"[COLLECTION] Expanding {cid} (level {lvl}/{depth}) ...")

        children = get_collection_children(cid, timeout=timeout, verbose=verbose)
        meta["collections"][cid] = {"child_count": len(children), "children": children}

        if not children:
            vlog(verbose, 1, f"[WARN] Collection {cid} returned 0 children (private? invalid? network blocked?)")
            continue

        expanded_items.extend(children)

        # Optional nested expansion: try treating each child as a collection too.
        if lvl < depth:
            # Only enqueue if it *looks* like a collection (cheap heuristic: has children).
            # We'll do a single probe per child.
            for child_id in children:
                if child_id in visited_collections:
                    continue
                try:
                    grandkids = get_collection_children(child_id, timeout=timeout, verbose=0)  # silent probe
                except Exception:
                    grandkids = []
                if grandkids:
                    queue.append((child_id, lvl + 1))

    return expanded_items, meta


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="rimworld_mod_downloader",
        description="Download RimWorld workshop mods with SteamCMD into the SteamCMD folder.",
    )

    ap.add_argument("-v", action="count", default=0, help="Verbose output (-v, -vv, -vvv).")
    ap.add_argument("--appid", type=int, default=RIMWORLD_APPID_DEFAULT, help="Game AppID (default: 294100 for RimWorld).")
    ap.add_argument("--steamcmd", type=str, default=str(default_steamcmd_path()), help="Path to steamcmd.exe (default: ./steamcmd.exe).")

    ap.add_argument("--links", nargs="+", help="Workshop links and/or numeric IDs (individual items).")
    ap.add_argument("--in", dest="infile", type=str, help="Text file containing one workshop link/ID per line.")

    ap.add_argument("--collection", nargs="+", help="Workshop collection link(s) and/or numeric collection ID(s).")
    ap.add_argument("--collection-depth", type=int, default=1, help="Expand nested collections up to this depth (default: 1).")
    ap.add_argument("--api-timeout", type=int, default=20, help="Timeout (seconds) for Steam Web API calls (default: 20).")

    ap.add_argument("--batch-size", type=int, default=25, help="How many items to request per SteamCMD run.")
    ap.add_argument("--retries", type=int, default=1, help="Retries per batch if SteamCMD fails (default: 1).")
    ap.add_argument("--out", type=str, default="download_report.json", help="Where to write the JSON report (default: download_report.json).")
    ap.add_argument("--dry-run", action="store_true", help="Do not run SteamCMD; only print/record what would run.")
    ap.add_argument("--list", action="store_true", help="Only parse and print IDs, then exit (no download).")

    ap.add_argument("--login", choices=["anonymous", "user"], default="anonymous", help="Login method (default: anonymous).")
    ap.add_argument("--username", type=str, default=None, help="Steam username (required if --login user).")

    args = ap.parse_args()
    verbose = int(args.v or 0)

    # Input validation: allow mixing, but require at least one source.
    if not args.links and not args.infile and not args.collection:
        ap.error("You must provide at least one input source: --links, --in, or --collection")

    base = script_dir()
    steamcmd_exe = Path(args.steamcmd)
    if not steamcmd_exe.is_absolute():
        steamcmd_exe = (base / steamcmd_exe).resolve()

    if not steamcmd_exe.exists():
        print(f"[ERROR] steamcmd.exe not found at: {steamcmd_exe}", file=sys.stderr)
        print("Put steamcmd.exe in the same folder as this script, or pass --steamcmd <path>.", file=sys.stderr)
        return 2

    # Collect raw item inputs
    raw_items: List[str] = []
    if args.links:
        raw_items.extend(list(args.links))
    if args.infile:
        raw_items.extend(read_lines_from_file(Path(args.infile)))

    # Collect raw collections
    raw_collections: List[str] = []
    if args.collection:
        raw_collections.extend(list(args.collection))

    # Extract item IDs
    invalid_items: List[str] = []
    item_ids: List[str] = []
    for s in raw_items:
        wid = extract_workshop_id(s)
        if not wid:
            invalid_items.append(s)
            continue
        item_ids.append(wid)

    # Extract collection IDs
    invalid_collections: List[str] = []
    collection_ids: List[str] = []
    for s in raw_collections:
        cid = extract_workshop_id(s)
        if not cid:
            invalid_collections.append(s)
            continue
        collection_ids.append(cid)

    if invalid_items:
        vlog(verbose, 1, "[WARN] Some item inputs did not contain a workshop ID and will be ignored:")
        for s in invalid_items:
            vlog(verbose, 1, "  - " + s)

    if invalid_collections:
        vlog(verbose, 1, "[WARN] Some collection inputs did not contain an ID and will be ignored:")
        for s in invalid_collections:
            vlog(verbose, 1, "  - " + s)

    # Expand collections (into item IDs)
    collection_meta: Dict[str, Any] = {"collections": {}, "depth": args.collection_depth}
    if collection_ids:
        expanded, meta = expand_collections(
            collection_ids=collection_ids,
            depth=max(1, int(args.collection_depth)),
            timeout=max(5, int(args.api_timeout)),
            verbose=verbose,
        )
        collection_meta = meta
        item_ids.extend(expanded)

    # Deduplicate while preserving order
    seen = set()
    deduped: List[str] = []
    for wid in item_ids:
        if wid in seen:
            continue
        seen.add(wid)
        deduped.append(wid)
    item_ids = deduped

    if not item_ids:
        print("[ERROR] No valid workshop IDs were found.", file=sys.stderr)
        return 2

    if args.list:
        print("Resolved workshop item IDs (after expanding collections):")
        for wid in item_ids:
            print(wid)
        return 0

    vlog(verbose, 1, f"[INFO] Total workshop items to download: {len(item_ids)}")
    if collection_ids:
        vlog(verbose, 1, f"[INFO] Collections provided: {len(collection_ids)} (expanded depth={max(1,int(args.collection_depth))})")

    # Login credentials
    username = args.username
    password: Optional[str] = None
    if args.login == "user":
        if not username:
            print("[ERROR] --username is required when --login user.", file=sys.stderr)
            return 2
        # Prompt for password so it isn't stored in history/logs.
        password = getpass.getpass("Steam password (input hidden): ")

    # Prepare logs folder
    ld = logs_dir()
    ld.mkdir(parents=True, exist_ok=True)

    report: Dict[str, Any] = {
        "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
        "steamcmd": str(steamcmd_exe),
        "appid": args.appid,
        "input": {
            "items_raw_count": len(raw_items),
            "collections_raw_count": len(raw_collections),
            "invalid_items": invalid_items,
            "invalid_collections": invalid_collections,
        },
        "collections": {
            "collection_ids": collection_ids,
            "expansion": collection_meta,
        },
        "workshop_item_count": len(item_ids),
        "batches": [],
        "items": {},
        "notes": [],
    }

    # Download in batches
    any_fail = False
    batch_index = 0
    for batch in chunked(item_ids, max(1, args.batch_size)):
        batch_index += 1
        batch_stamp = now_stamp()
        log_file = ld / f"steamcmd_batch_{batch_index:03d}_{batch_stamp}.log"

        vlog(verbose, 2, f"[BATCH {batch_index}] Downloading {len(batch)} items ...")

        # SteamCMD args for this batch
        steamcmd_args = build_steamcmd_args(
            steamcmd_exe=steamcmd_exe,
            login_mode=args.login,
            username=username,
            password=password,
            appid=args.appid,
            ids=batch,
        )

        attempt = 0
        result: Optional[Dict[str, Any]] = None
        last_log_path: Path = log_file

        while attempt <= max(0, args.retries):
            attempt += 1
            # Include attempt number in the log name after first retry
            run_log = log_file if attempt == 1 else ld / f"{log_file.stem}_retry{attempt-1}{log_file.suffix}"
            last_log_path = run_log

            result = run_steamcmd_batch(steamcmd_args, cwd=base, log_file=run_log, dry_run=args.dry_run, verbose=verbose)

            ok_rc = (result["returncode"] == 0)
            combined_out = (result.get("stdout", "") + "\n" + result.get("stderr", ""))

            if args.login == "user" and looks_like_steam_guard(combined_out):
                report["notes"].append(
                    "Steam Guard / 2FA detected in SteamCMD output. "
                    "You may need to run steamcmd.exe manually once to complete login, then rerun this script."
                )
                vlog(verbose, 1, "[WARN] Steam Guard / 2FA detected. Login once manually in steamcmd.exe, then rerun.")
                # No point retrying automatically here.
                break

            if ok_rc:
                break

            vlog(verbose, 1, f"[WARN] SteamCMD returned code {result['returncode']} (attempt {attempt}/{1+max(0,args.retries)}).")

        # Verify each item in batch
        verified = [verify_downloaded(args.appid, wid) for wid in batch]
        for v in verified:
            report["items"][v["workshop_id"]] = v
            if not v["exists"] and not args.dry_run:
                any_fail = True

        batch_entry = {
            "batch_index": batch_index,
            "batch_size": len(batch),
            "workshop_ids": batch,
            "log_file": str(last_log_path),
            "returncode": (result["returncode"] if result else None),
            "dry_run": bool(result.get("dry_run")) if result else False,
            "sanitized_cmd": (result.get("sanitized_cmd") if result else None),
        }
        report["batches"].append(batch_entry)

        # If SteamCMD itself failed, we still continue to next batch, but mark fail.
        if result and result["returncode"] != 0 and not args.dry_run:
            any_fail = True

    # Write report
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = (base / out_path).resolve()
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8", errors="replace")

    # Print summary
    success_count = sum(1 for v in report["items"].values() if v.get("exists"))
    print(f"\nDone. Verified {success_count}/{len(item_ids)} item folders.")
    print(f"Workshop content folder: {content_root(args.appid)}")
    print(f"Report written to: {out_path}")
    print(f"Logs folder: {logs_dir()}")

    if report["notes"]:
        print("\nNotes:")
        for n in report["notes"]:
            print(" -", n)

    return 2 if any_fail and not args.dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
