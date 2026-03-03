#!/usr/bin/env python3
"""
RimWorld Steam Workshop Mod Downloader (SteamCMD) - Version 2 (CLI + GUI)

✅ CLI mode:
  python rimworld_mod_downloader_v2.py --in mods.txt
  python rimworld_mod_downloader_v2.py --collection 1884025115
  python rimworld_mod_downloader_v2.py -vv --links 818773962 123456789

✅ GUI mode:
  python rimworld_mod_downloader_v2.py --gui

GUI has 2 pages (tabs):
1) Downloads: add items/collections, queue, start/stop, logs, overall progress.
2) Library: downloaded vs loaded-to-RimWorld Mods folder; load/unload actions.

Design assumptions:
- This script lives inside your SteamCMD folder (same folder as steamcmd.exe).
- Workshop downloads land under: ./steamapps/workshop/content/294100/<workshop_id>/
- "Loaded" means copied into your chosen RimWorld Mods folder as <Mods>/<workshop_id>/
  with a marker file: <Mods>/<workshop_id>/.workshop_id
"""

from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import json
import locale
import os
import queue as _queue
import re
import shutil
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen


__version__ = "2.2"

RIMWORLD_APPID_DEFAULT = 294100
STEAM_COLLECTION_API = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"


# ------------------------ Paths / utils ------------------------

def script_dir() -> Path:
    return Path(__file__).resolve().parent


def default_steamcmd_path() -> Path:
    return script_dir() / "steamcmd.exe"


def content_root(appid: int) -> Path:
    return script_dir() / "steamapps" / "workshop" / "content" / str(appid)


def logs_dir() -> Path:
    return script_dir() / "logs"


def config_path() -> Path:
    return script_dir() / "gui_config.json"


def now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def read_lines_from_file(p: Path) -> List[str]:
    if not p.exists():
        raise FileNotFoundError(f"Input file not found: {p}")
    lines: List[str] = []
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


_ID_RE = re.compile(r"(?i)\b(?:id=)?(\d{6,})\b")


def extract_workshop_id(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return s
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
    out = list(args)
    try:
        i = out.index("+login")
        if i + 1 < len(out) and out[i + 1] != "anonymous":
            if i + 3 < len(out):
                out[i + 3] = "********"
    except ValueError:
        pass
    return out


# ------------------------ Steam Web API (collections) ------------------------

def steam_api_post(url: str, data: Dict[str, str], timeout: int) -> Dict[str, Any]:
    encoded = urlencode(data).encode("utf-8")
    req = Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
    req.add_header("User-Agent", f"rimworld_mod_downloader/{__version__}")
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    text = raw.decode("utf-8", errors="replace")
    return json.loads(text)


def get_collection_children(collection_id: str, timeout: int) -> List[str]:
    payload = {
        "collectioncount": "1",
        "publishedfileids[0]": str(collection_id),
        "format": "json",
    }
    j = steam_api_post(STEAM_COLLECTION_API, payload, timeout=timeout)
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
    verbose: int = 0,
) -> Tuple[List[str], Dict[str, Any]]:
    visited_collections = set()
    queue: List[Tuple[str, int]] = [(cid, 1) for cid in collection_ids]
    expanded_items: List[str] = []
    meta: Dict[str, Any] = {"collections": {}, "depth": depth}

    while queue:
        cid, lvl = queue.pop(0)
        if cid in visited_collections:
            continue
        visited_collections.add(cid)

        if verbose >= 2:
            print(f"[COLLECTION] Expanding {cid} (level {lvl}/{depth}) ...")

        try:
            children = get_collection_children(cid, timeout=timeout)
        except Exception as e:
            meta["collections"][cid] = {"child_count": 0, "children": [], "error": str(e)}
            if verbose >= 1:
                print(f"[WARN] Collection {cid} failed to expand: {e}")
            continue

        meta["collections"][cid] = {"child_count": len(children), "children": children}
        expanded_items.extend(children)

        if lvl < depth:
            # Best-effort nested expansion (quiet probe)
            for child_id in children:
                if child_id in visited_collections:
                    continue
                try:
                    grandkids = get_collection_children(child_id, timeout=timeout)
                except Exception:
                    grandkids = []
                if grandkids:
                    queue.append((child_id, lvl + 1))

    return expanded_items, meta


# ------------------------ SteamCMD runner ------------------------

def build_steamcmd_args(
    steamcmd_exe: Path,
    login_mode: str,
    username: Optional[str],
    password: Optional[str],
    appid: int,
    ids: List[str],
) -> List[str]:
    args: List[str] = [str(steamcmd_exe)]
    args += ["+@ShutdownOnFailedCommand", "1", "+@NoPromptForPassword", "1"]

    if login_mode == "anonymous":
        args += ["+login", "anonymous"]
    else:
        if not username:
            raise ValueError("username is required for --login user")
        if password is None:
            raise ValueError("password is required for --login user")
        args += ["+login", username, password]

    for wid in ids:
        args += ["+workshop_download_item", str(appid), wid]

    args += ["+quit"]
    return args


def looks_like_steam_guard(output: str) -> bool:
    o = (output or "").lower()
    keywords = ["steam guard", "two-factor", "two factor", "auth code", "email code", "phone code"]
    return any(k in o for k in keywords)


def run_steamcmd_batch(
    args: List[str],
    cwd: Path,
    log_file: Path,
    dry_run: bool,
    verbose: int,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    import subprocess

    safe_args = sanitize_steamcmd_args(args)

    if dry_run:
        log_file.write_text("DRY RUN\n" + " ".join(safe_args) + "\n", encoding="utf-8", errors="replace")
        if verbose >= 2:
            print(f"[DRY RUN] {' '.join(safe_args)}")
        return {"returncode": 0, "stdout": "", "stderr": "", "dry_run": True, "sanitized_cmd": " ".join(safe_args)}

    enc = locale.getpreferredencoding(False) or "utf-8"

    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=enc,
        errors="replace",
    )

    while proc.poll() is None:
        if stop_event is not None and stop_event.is_set():
            try:
                proc.terminate()
            except Exception:
                pass
            break
        time.sleep(0.2)

    try:
        out, err = proc.communicate(timeout=30)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        out, err = proc.communicate()

    combined = []
    combined.append("COMMAND:\n" + " ".join(safe_args) + "\n")
    combined.append(f"RETURN CODE: {proc.returncode}\n\n")
    if out:
        combined.append("STDOUT:\n" + out + "\n")
    if err:
        combined.append("STDERR:\n" + err + "\n")
    log_file.write_text("".join(combined), encoding="utf-8", errors="replace")

    if verbose >= 3:
        if out:
            print(out)
        if err:
            print(err, file=sys.stderr)

    return {
        "returncode": proc.returncode,
        "stdout": out or "",
        "stderr": err or "",
        "dry_run": False,
        "sanitized_cmd": " ".join(safe_args),
    }


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
            pass
    return {
        "workshop_id": wid,
        "path": str(mod_dir),
        "exists": ok,
        "file_count": file_count,
        "bytes": size,
    }



def is_workshop_item_downloaded(appid: int, wid: str) -> bool:
    """Fast existence check to avoid re-downloading already-present workshop items."""
    p = content_root(appid) / str(wid)
    if not (p.exists() and p.is_dir()):
        return False
    try:
        # avoid treating empty dirs as downloaded
        next(p.iterdir())
        return True
    except StopIteration:
        return False
    except Exception:
        return True


# ------------------------ RimWorld Mods folder integration ------------------------

def try_read_mod_metadata(item_dir: Path) -> Dict[str, str]:
    about = item_dir / "About" / "About.xml"
    if not about.exists():
        return {}
    try:
        tree = ET.parse(str(about))
        root = tree.getroot()
        name = root.findtext("name") or ""
        pkg = root.findtext("packageId") or ""
        return {"name": name.strip(), "packageId": pkg.strip()}
    except Exception:
        return {}


def load_to_rimworld_mods(mods_dir: Path, appid: int, wid: str, overwrite: bool = True) -> Tuple[bool, str]:
    src = content_root(appid) / wid
    if not src.exists():
        return False, "Source workshop folder not found"
    dst = mods_dir / wid

    try:
        mods_dir.mkdir(parents=True, exist_ok=True)
        if dst.exists() and overwrite:
            shutil.rmtree(dst)
        if not dst.exists():
            shutil.copytree(src, dst)
        marker = dst / ".workshop_id"
        marker.write_text(f"{wid}\nloaded_at={_dt.datetime.now().isoformat(timespec='seconds')}\n", encoding="utf-8")
        return True, "Loaded"
    except Exception as e:
        return False, f"Load failed: {e}"


def unload_from_rimworld_mods(mods_dir: Path, wid: str) -> Tuple[bool, str]:
    dst = mods_dir / wid
    try:
        if dst.exists():
            shutil.rmtree(dst)
        return True, "Unloaded"
    except Exception as e:
        return False, f"Unload failed: {e}"


def list_downloaded_workshop_items(appid: int) -> List[str]:
    root = content_root(appid)
    if not root.exists():
        return []
    out: List[str] = []
    for p in root.iterdir():
        if p.is_dir() and p.name.isdigit():
            out.append(p.name)
    out.sort(key=lambda s: int(s))
    return out


def list_loaded_items(mods_dir: Path) -> List[str]:
    if not mods_dir.exists():
        return []
    out: List[str] = []
    for p in mods_dir.iterdir():
        if p.is_dir() and p.name.isdigit() and (p / ".workshop_id").exists():
            out.append(p.name)
    out.sort(key=lambda s: int(s))
    return out


# ------------------------ CLI mode ------------------------

def cli_main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rimworld_mod_downloader_v2",
        description="Download RimWorld workshop mods with SteamCMD into the SteamCMD folder.",
    )
    ap.add_argument("--gui", action="store_true", help="Launch the GUI.")
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

    args = ap.parse_args(argv)
    verbose = int(args.v or 0)

    if args.gui:
        return gui_main(args)

    if not args.links and not args.infile and not args.collection:
        ap.error("You must provide at least one input source: --links, --in, or --collection (or use --gui).")

    base = script_dir()
    steamcmd_exe = Path(args.steamcmd)
    if not steamcmd_exe.is_absolute():
        steamcmd_exe = (base / steamcmd_exe).resolve()
    if not steamcmd_exe.exists():
        print(f"[ERROR] steamcmd.exe not found at: {steamcmd_exe}", file=sys.stderr)
        return 2

    raw_items: List[str] = []
    if args.links:
        raw_items.extend(list(args.links))
    if args.infile:
        raw_items.extend(read_lines_from_file(Path(args.infile)))

    raw_collections: List[str] = list(args.collection or [])

    invalid_items: List[str] = []
    item_ids: List[str] = []
    for s in raw_items:
        wid = extract_workshop_id(s)
        if not wid:
            invalid_items.append(s)
            continue
        item_ids.append(wid)

    invalid_collections: List[str] = []
    collection_ids: List[str] = []
    for s in raw_collections:
        cid = extract_workshop_id(s)
        if not cid:
            invalid_collections.append(s)
            continue
        collection_ids.append(cid)

    if invalid_items and verbose >= 1:
        print("[WARN] Invalid item inputs ignored:")
        for s in invalid_items:
            print("  -", s)

    if invalid_collections and verbose >= 1:
        print("[WARN] Invalid collection inputs ignored:")
        for s in invalid_collections:
            print("  -", s)

    if collection_ids:
        expanded, _meta = expand_collections(
            collection_ids=collection_ids,
            depth=max(1, int(args.collection_depth)),
            timeout=max(5, int(args.api_timeout)),
            verbose=verbose,
        )
        item_ids.extend(expanded)

    # Dedup preserving order
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

        # Skip items already downloaded in the workshop cache
    resolved_ids = list(item_ids)
    already_downloaded = [wid for wid in resolved_ids if is_workshop_item_downloaded(args.appid, wid)]
    _already_set = set(already_downloaded)
    item_ids = [wid for wid in resolved_ids if wid not in _already_set]

    if already_downloaded and verbose >= 1:
        print(f"[INFO] Skipping {len(already_downloaded)} already-downloaded item(s).")
    if verbose >= 1:
        print(f"[INFO] Workshop items resolved: {len(resolved_ids)} | To download: {len(item_ids)}")

    username = args.username
    password: Optional[str] = None
    if args.login == "user":
        if not username:
            print("[ERROR] --username is required when --login user.", file=sys.stderr)
            return 2
        password = getpass.getpass("Steam password (input hidden): ")

    ld = logs_dir()
    ld.mkdir(parents=True, exist_ok=True)

    report: Dict[str, Any] = {
        "version": __version__,
        "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
        "steamcmd": str(steamcmd_exe),
        "appid": args.appid,
        "resolved_item_count": len(resolved_ids),
        "already_downloaded_count": len(already_downloaded),
        "workshop_item_count": len(item_ids),
        "already_downloaded": list(already_downloaded),
        "batches": [],
        "items": {},
        "notes": [],
    }

    # Pre-fill report for items already present on disk
    for wid in already_downloaded:
        report["items"][wid] = verify_downloaded(args.appid, wid)

    any_fail = False

    batch_index = 0
    for batch in chunked(item_ids, max(1, args.batch_size)):
        batch_index += 1
        batch_stamp = now_stamp()
        log_file = ld / f"steamcmd_batch_{batch_index:03d}_{batch_stamp}.log"

        if verbose >= 2:
            print(f"[BATCH {batch_index}] Downloading {len(batch)} items ...")

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
            run_log = log_file if attempt == 1 else ld / f"{log_file.stem}_retry{attempt-1}{log_file.suffix}"
            last_log_path = run_log
            result = run_steamcmd_batch(steamcmd_args, cwd=base, log_file=run_log, dry_run=args.dry_run, verbose=verbose)

            combined_out = (result.get("stdout", "") + "\n" + result.get("stderr", ""))
            if args.login == "user" and looks_like_steam_guard(combined_out):
                report["notes"].append("Steam Guard / 2FA detected. Run steamcmd.exe once manually to complete login, then rerun.")
                if verbose >= 1:
                    print("[WARN] Steam Guard / 2FA detected. Complete login manually, then rerun.")
                break

            if result["returncode"] == 0:
                break

            if verbose >= 1:
                print(f"[WARN] SteamCMD returned code {result['returncode']} (attempt {attempt}/{1+max(0,args.retries)}).")

        verified = [verify_downloaded(args.appid, wid) for wid in batch]
        for v in verified:
            report["items"][v["workshop_id"]] = v
            if not v["exists"] and not args.dry_run:
                any_fail = True

        report["batches"].append({
            "batch_index": batch_index,
            "batch_size": len(batch),
            "workshop_ids": batch,
            "log_file": str(last_log_path),
            "returncode": (result["returncode"] if result else None),
            "dry_run": bool(result.get("dry_run")) if result else False,
            "sanitized_cmd": (result.get("sanitized_cmd") if result else None),
        })

        if result and result["returncode"] != 0 and not args.dry_run:
            any_fail = True

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = (base / out_path).resolve()
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8", errors="replace")

    success_count = sum(1 for v in report["items"].values() if v.get("exists"))
    print(f"\nDone. Verified {success_count}/{len(resolved_ids)} item folders.")
    print(f"Workshop content folder: {content_root(args.appid)}")
    print(f"Report written to: {out_path}")
    print(f"Logs folder: {logs_dir()}")

    if report["notes"]:
        print("\nNotes:")
        for n in report["notes"]:
            print(" -", n)

    return 2 if any_fail and not args.dry_run else 0


# ------------------------ GUI mode ------------------------

@dataclass
class QueueItem:
    workshop_id: str
    source: str = ""
    status: str = "queued"  # queued/downloading/done/failed/stopped
    message: str = ""
    added_at: str = field(default_factory=lambda: _dt.datetime.now().strftime("%H:%M:%S"))


def load_gui_config() -> Dict[str, Any]:
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def save_gui_config(cfg: Dict[str, Any]) -> None:
    p = config_path()
    try:
        p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8", errors="replace")
    except Exception:
        pass


def gui_main(parsed_args: argparse.Namespace) -> int:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog

    base = script_dir()

    # IMPORTANT: create the Tk root BEFORE creating any tk.Variable (StringVar/IntVar),
    # otherwise you'll get: "Too early to create variable: no default root window"
    root = tk.Tk()
    root.withdraw()  # we'll show it after basic validation

    steamcmd_exe = Path(parsed_args.steamcmd)
    if not steamcmd_exe.is_absolute():
        steamcmd_exe = (base / steamcmd_exe).resolve()
    if not steamcmd_exe.exists():
        messagebox.showerror(
            "SteamCMD not found",
            "steamcmd.exe not found at: Put it next to this script or set --steamcmd.",
            parent=root,
        )
        try:
            root.destroy()
        except Exception:
            pass
        return 2

    ld = logs_dir()
    ld.mkdir(parents=True, exist_ok=True)

    cfg = load_gui_config()
    appid = int(getattr(parsed_args, "appid", RIMWORLD_APPID_DEFAULT) or RIMWORLD_APPID_DEFAULT)

    ui_events: "_queue.Queue[Tuple[str, Any]]" = _queue.Queue()
    stop_event = threading.Event()
    worker_thread: Optional[threading.Thread] = None
    queue_items: Dict[str, QueueItem] = {}
    running = {"active": False}

    # tk variables (now safe because root exists)
    rimworld_mods_dir = tk.StringVar(master=root, value=cfg.get("rimworld_mods_dir", ""))

    batch_size_var = tk.IntVar(master=root, value=int(cfg.get("batch_size", 25)))
    retries_var = tk.IntVar(master=root, value=int(cfg.get("retries", 1)))
    api_timeout_var = tk.IntVar(master=root, value=int(cfg.get("api_timeout", 20)))
    collection_depth_var = tk.IntVar(master=root, value=int(cfg.get("collection_depth", 1)))

    verbose_var = tk.IntVar(master=root, value=int(cfg.get("verbose", 1)))
    login_mode_var = tk.StringVar(master=root, value=cfg.get("login_mode", "anonymous"))
    username_var = tk.StringVar(master=root, value=cfg.get("username", ""))

    # Now show the window
    root.deiconify()
    root.title(f"RimWorld Mod Downloader (SteamCMD) v{__version__}")
    root.geometry(cfg.get("window_geometry", "1100x700"))

    # -------- helpers --------
    def ui_log(msg: str, level: int = 1) -> None:
        if verbose_var.get() >= level:
            ui_events.put(("log", msg))

    def add_queue_ids(ids: List[str], source: str) -> None:
        added = 0
        for wid in ids:
            wid = str(wid).strip()
            if not wid or not wid.isdigit():
                continue
            if wid in queue_items:
                continue
            qi = QueueItem(workshop_id=wid, source=source)
            if is_workshop_item_downloaded(appid, wid):
                qi.status = "done"
                qi.message = "Already downloaded"
            queue_items[wid] = qi
            ui_events.put(("queue_add", queue_items[wid]))
            added += 1
        if added:
            ui_log(f"[QUEUE] Added {added} item(s).", 1)

    def parse_textbox_lines(text: str) -> List[str]:
        lines = []
        for raw in (text or "").splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            lines.append(s)
        return lines

    def resolve_item_ids_from_inputs(lines: List[str]) -> Tuple[List[str], List[str]]:
        good: List[str] = []
        bad: List[str] = []
        for s in lines:
            wid = extract_workshop_id(s)
            if wid:
                good.append(wid)
            else:
                bad.append(s)
        seen = set()
        out = []
        for wid in good:
            if wid in seen:
                continue
            seen.add(wid)
            out.append(wid)
        return out, bad

    def expand_collection_gui(collection_id: str) -> None:
        cid = extract_workshop_id(collection_id or "")
        if not cid:
            messagebox.showwarning("Invalid collection", "Could not find a numeric collection ID in that input.")
            return
        ui_log(f"[COLLECTION] Expanding {cid} ...", 1)
        try:
            expanded, _meta = expand_collections(
                [cid],
                depth=max(1, int(collection_depth_var.get())),
                timeout=max(5, int(api_timeout_var.get())),
                verbose=max(0, verbose_var.get() - 1),
            )
        except Exception as e:
            messagebox.showerror("Collection expansion failed", f"Collection {cid} failed:\n{e}")
            return
        add_queue_ids(expanded, source=f"collection:{cid}")
        ui_log(f"[COLLECTION] {cid} expanded to {len(expanded)} item(s).", 1)

    def set_status(wid: str, status: str, message: str = "") -> None:
        if wid not in queue_items:
            return
        queue_items[wid].status = status
        queue_items[wid].message = message
        ui_events.put(("queue_update", queue_items[wid]))

    def pick_rimworld_mods_folder() -> None:
        p = filedialog.askdirectory(title="Choose RimWorld Mods folder")
        if p:
            rimworld_mods_dir.set(p)
            ui_log(f"[SETTINGS] RimWorld Mods folder set to: {p}", 1)

    def validate_rimworld_folder() -> Optional[Path]:
        p = (rimworld_mods_dir.get() or "").strip()
        if not p:
            messagebox.showwarning("Mods folder required", "Please choose your RimWorld Mods folder first.")
            return None
        return Path(p)

    # -------- downloads worker --------
    def downloads_worker() -> None:
        try:
            running["active"] = True
            stop_event.clear()

            pending_all = [wid for wid, qi in queue_items.items() if qi.status in ("queued", "failed", "stopped")]
            if not pending_all:
                ui_events.put(("info", "Nothing to download."))
                running["active"] = False
                return

            # Mark already-downloaded items as done and skip sending them to SteamCMD
            pending: List[str] = []
            done_count = 0
            for wid in pending_all:
                if is_workshop_item_downloaded(appid, wid):
                    set_status(wid, "done", "Already downloaded")
                    done_count += 1
                else:
                    pending.append(wid)

            total = len(pending_all)
            ui_events.put(("progress", (done_count, total)))

            if not pending:
                ui_events.put(("info", "All queued items are already downloaded."))
                running["active"] = False
                return

            for batch in chunked(pending, max(1, int(batch_size_var.get()))):
                if stop_event.is_set():
                    break

                for wid in batch:
                    set_status(wid, "downloading", "Batch running...")

                login_mode = login_mode_var.get()
                username = username_var.get().strip() if login_mode == "user" else None
                password = None

                if login_mode == "user":
                    if not username:
                        ui_events.put(("error", "Login mode is 'user' but username is empty."))
                        for wid in batch:
                            set_status(wid, "failed", "Missing username")
                        continue
                    ui_events.put(("ask_password", None))
                    pw = None
                    while pw is None and not stop_event.is_set():
                        try:
                            typ, payload = ui_events.get(timeout=0.2)
                        except _queue.Empty:
                            continue
                        if typ == "password":
                            pw = payload
                        else:
                            ui_events.put((typ, payload))
                            time.sleep(0.05)
                    if stop_event.is_set():
                        break
                    password = pw

                steamcmd_args = build_steamcmd_args(
                    steamcmd_exe=steamcmd_exe,
                    login_mode=login_mode,
                    username=username,
                    password=password,
                    appid=appid,
                    ids=batch,
                )

                attempt = 0
                while attempt <= max(0, int(retries_var.get())):
                    if stop_event.is_set():
                        break
                    attempt += 1
                    stamp = now_stamp()
                    log_file = ld / f"steamcmd_gui_batch_{stamp}_attempt{attempt}.log"
                    ui_log(f"[BATCH] Running SteamCMD for {len(batch)} item(s) (attempt {attempt})", 2)

                    result = run_steamcmd_batch(
                        steamcmd_args,
                        cwd=base,
                        log_file=log_file,
                        dry_run=False,
                        verbose=0,
                        stop_event=stop_event,
                    )

                    # If log level is high, show SteamCMD output tail in GUI log
                    if verbose_var.get() >= 3:
                        try:
                            txt = log_file.read_text(encoding="utf-8", errors="replace")
                            tail = "\n".join(txt.splitlines()[-80:])
                            ui_log("[STEAMCMD LOG TAIL]\n" + tail, 3)
                        except Exception:
                            pass

                    combined_out = (result.get("stdout", "") + "\n" + result.get("stderr", ""))
                    if login_mode == "user" and looks_like_steam_guard(combined_out):
                        ui_events.put(("error", "Steam Guard / 2FA detected. Run steamcmd.exe once manually to complete login, then retry."))
                        break

                    if result["returncode"] == 0:
                        break

                for wid in batch:
                    if stop_event.is_set():
                        set_status(wid, "stopped", "Stopped")
                        continue
                    v = verify_downloaded(appid, wid)
                    if v["exists"]:
                        set_status(wid, "done", "Downloaded")
                    else:
                        set_status(wid, "failed", "Not found after download")

                done_count += len(batch)
                ui_events.put(("progress", (min(done_count, total), total)))

            if stop_event.is_set():
                ui_events.put(("info", "Download stopped."))
        except Exception as e:
            ui_events.put(("error", f"Worker crashed: {e}\n\n{traceback.format_exc()}"))
        finally:
            running["active"] = False
            ui_events.put(("worker_done", None))

    def start_downloads() -> None:
        nonlocal worker_thread
        if running["active"]:
            messagebox.showinfo("Already running", "Downloads are already running.")
            return
        if not queue_items:
            messagebox.showinfo("Queue is empty", "Add some workshop links/IDs first.")
            return
        worker_thread = threading.Thread(target=downloads_worker, daemon=True)
        worker_thread.start()
        ui_log("[INFO] Downloads started.", 1)

    def stop_downloads() -> None:
        if not running["active"]:
            return
        stop_event.set()
        ui_log("[INFO] Stopping... (may take a moment)", 1)

    # -------- library --------
    def refresh_library() -> None:
        mods_path = validate_rimworld_folder()
        if mods_path is None:
            return
        downloaded = list_downloaded_workshop_items(appid)
        loaded = set(list_loaded_items(mods_path))
        not_loaded = [wid for wid in downloaded if wid not in loaded]
        loaded_list = sorted(list(loaded), key=lambda s: int(s))
        ui_events.put(("library_refresh", (not_loaded, loaded_list)))

    def load_selected(ids: List[str]) -> None:
        mods_path = validate_rimworld_folder()
        if mods_path is None:
            return
        if not ids:
            return

        def _worker():
            ok = 0
            for wid in ids:
                if stop_event.is_set():
                    break
                success, msg = load_to_rimworld_mods(mods_path, appid, wid, overwrite=True)
                ui_log(f"[LOAD] {wid}: {msg}", 1)
                ok += 1 if success else 0
            ui_events.put(("info", f"Load completed. Loaded {ok}/{len(ids)}."))
            ui_events.put(("library_refresh_request", None))

        threading.Thread(target=_worker, daemon=True).start()

    def unload_selected(ids: List[str]) -> None:
        mods_path = validate_rimworld_folder()
        if mods_path is None:
            return
        if not ids:
            return

        def _worker():
            ok = 0
            for wid in ids:
                if stop_event.is_set():
                    break
                success, msg = unload_from_rimworld_mods(mods_path, wid)
                ui_log(f"[UNLOAD] {wid}: {msg}", 1)
                ok += 1 if success else 0
            ui_events.put(("info", f"Unload completed. Unloaded {ok}/{len(ids)}."))
            ui_events.put(("library_refresh_request", None))

        threading.Thread(target=_worker, daemon=True).start()

    # -------- UI layout --------
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=8, pady=8)

    tab_downloads = ttk.Frame(notebook)
    tab_library = ttk.Frame(notebook)
    notebook.add(tab_downloads, text="Downloads")
    notebook.add(tab_library, text="Library")

    top_frame = ttk.Frame(tab_downloads)
    top_frame.pack(fill="x", pady=(0, 8))

    mods_frame = ttk.LabelFrame(top_frame, text="RimWorld Mods Folder (target)")
    mods_frame.pack(fill="x", pady=6)
    ttk.Entry(mods_frame, textvariable=rimworld_mods_dir).pack(side="left", fill="x", expand=True, padx=6, pady=6)
    ttk.Button(mods_frame, text="Choose...", command=pick_rimworld_mods_folder).pack(side="left", padx=6, pady=6)

    inputs_frame = ttk.LabelFrame(top_frame, text="Add workshop items / collections")
    inputs_frame.pack(fill="x", pady=6)

    left_inputs = ttk.Frame(inputs_frame)
    left_inputs.pack(side="left", fill="both", expand=True, padx=6, pady=6)

    ttk.Label(left_inputs, text="Paste item links/IDs (one per line):").pack(anchor="w")
    items_text = tk.Text(left_inputs, height=5)
    items_text.pack(fill="x", expand=True)

    btns_row = ttk.Frame(left_inputs)
    btns_row.pack(fill="x", pady=(6, 0))

    def add_items_from_text():
        lines = parse_textbox_lines(items_text.get("1.0", "end"))
        ids, bad = resolve_item_ids_from_inputs(lines)
        if bad:
            ui_log("[WARN] Some lines had no ID and were ignored:\n  - " + "\n  - ".join(bad), 1)
        add_queue_ids(ids, source="item")

    def add_from_file():
        fp = filedialog.askopenfilename(title="Select list file", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not fp:
            return
        try:
            lines = read_lines_from_file(Path(fp))
        except Exception as e:
            messagebox.showerror("Failed to read file", str(e))
            return
        ids, bad = resolve_item_ids_from_inputs(lines)
        if bad:
            ui_log("[WARN] Some lines had no ID and were ignored:\n  - " + "\n  - ".join(bad), 1)
        add_queue_ids(ids, source=f"file:{Path(fp).name}")

    ttk.Button(btns_row, text="Add Items", command=add_items_from_text).pack(side="left")
    ttk.Button(btns_row, text="Load List File...", command=add_from_file).pack(side="left", padx=6)

    right_inputs = ttk.Frame(inputs_frame)
    right_inputs.pack(side="left", fill="y", padx=6, pady=6)

    ttk.Label(right_inputs, text="Collection link/ID:").pack(anchor="w")
    collection_entry = ttk.Entry(right_inputs)
    collection_entry.pack(fill="x")

    def add_collection():
        s = collection_entry.get().strip()
        if s:
            expand_collection_gui(s)

    ttk.Button(right_inputs, text="Add Collection", command=add_collection).pack(fill="x", pady=(6, 0))

    settings_row = ttk.Frame(top_frame)
    settings_row.pack(fill="x", pady=6)

    def _spin(parent, label, var, frm=1, to=999, width=6):
        w = ttk.Frame(parent)
        ttk.Label(w, text=label).pack(side="left")
        ttk.Spinbox(w, from_=frm, to=to, textvariable=var, width=width).pack(side="left", padx=(6, 0))
        return w

    _spin(settings_row, "Batch size:", batch_size_var, frm=1, to=200).pack(side="left", padx=(0, 12))
    _spin(settings_row, "Retries:", retries_var, frm=0, to=10).pack(side="left", padx=(0, 12))
    _spin(settings_row, "API timeout:", api_timeout_var, frm=5, to=120).pack(side="left", padx=(0, 12))
    _spin(settings_row, "Collection depth:", collection_depth_var, frm=1, to=5).pack(side="left", padx=(0, 12))

    ttk.Label(settings_row, text="Log level:").pack(side="left")
    ttk.Combobox(settings_row, values=[0, 1, 2, 3], width=3, state="readonly", textvariable=verbose_var).pack(side="left", padx=(6, 18))

    ttk.Label(settings_row, text="Login:").pack(side="left")
    ttk.Combobox(settings_row, values=["anonymous", "user"], width=10, state="readonly", textvariable=login_mode_var).pack(side="left", padx=(6, 6))
    ttk.Label(settings_row, text="Username:").pack(side="left")
    ttk.Entry(settings_row, textvariable=username_var, width=20).pack(side="left", padx=(6, 0))

    mid_frame = ttk.Frame(tab_downloads)
    mid_frame.pack(fill="both", expand=True)

    queue_frame = ttk.LabelFrame(mid_frame, text="Download Queue")
    queue_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))

    columns = ("id", "source", "status", "message", "added")
    queue_tree = ttk.Treeview(queue_frame, columns=columns, show="headings", height=14)
    for c, t, w in [
        ("id", "Workshop ID", 110),
        ("source", "Source", 160),
        ("status", "Status", 100),
        ("message", "Message", 420),
        ("added", "Added", 70),
    ]:
        queue_tree.heading(c, text=t)
        queue_tree.column(c, width=w, anchor="w")
    queue_tree.pack(fill="both", expand=True, padx=6, pady=6)

    q_scroll = ttk.Scrollbar(queue_frame, orient="vertical", command=queue_tree.yview)
    queue_tree.configure(yscrollcommand=q_scroll.set)
    q_scroll.pack(side="right", fill="y")

    ctrl_frame = ttk.Frame(mid_frame)
    ctrl_frame.pack(side="left", fill="y")

    ttk.Button(ctrl_frame, text="Start", command=start_downloads).pack(fill="x", pady=(0, 6))
    ttk.Button(ctrl_frame, text="Stop", command=stop_downloads).pack(fill="x", pady=(0, 6))

    def remove_selected():
        sel = queue_tree.selection()
        for iid in sel:
            wid = queue_tree.item(iid, "values")[0]
            if wid in queue_items and queue_items[wid].status not in ("downloading",):
                queue_items.pop(wid, None)
                queue_tree.delete(iid)

    ttk.Button(ctrl_frame, text="Remove selected", command=remove_selected).pack(fill="x", pady=(0, 6))

    def clear_done():
        to_delete = [wid for wid, qi in queue_items.items() if qi.status == "done"]
        for wid in to_delete:
            queue_items.pop(wid, None)
        for iid in list(queue_tree.get_children()):
            wid = queue_tree.item(iid, "values")[0]
            if wid in to_delete:
                queue_tree.delete(iid)

    ttk.Button(ctrl_frame, text="Clear completed", command=clear_done).pack(fill="x", pady=(0, 6))

    def open_workshop_folder():
        p = content_root(appid)
        if not p.exists():
            messagebox.showinfo("Workshop folder", f"Workshop folder does not exist yet:\n{p}")
            return
        try:
            os.startfile(str(p))
        except Exception:
            messagebox.showinfo("Workshop folder", str(p))

    ttk.Button(ctrl_frame, text="Open workshop folder", command=open_workshop_folder).pack(fill="x", pady=(0, 6))

    bottom_frame = ttk.Frame(tab_downloads)
    bottom_frame.pack(fill="both", expand=False, pady=(8, 0))

    progress_var = tk.DoubleVar(value=0.0)
    ttk.Progressbar(bottom_frame, variable=progress_var, maximum=100.0).pack(fill="x", padx=2)

    progress_label = ttk.Label(bottom_frame, text="0 / 0")
    progress_label.pack(anchor="w", padx=2, pady=(2, 6))

    log_frame = ttk.LabelFrame(bottom_frame, text="Log")
    log_frame.pack(fill="both", expand=True)
    log_text = tk.Text(log_frame, height=8, wrap="word")
    log_text.pack(side="left", fill="both", expand=True, padx=6, pady=6)
    log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_scroll.pack(side="right", fill="y")

    def append_log(s: str):
        log_text.insert("end", s + "\n")
        log_text.see("end")

    # Library tab
    lib_top = ttk.Frame(tab_library)
    lib_top.pack(fill="x", pady=8, padx=8)

    ttk.Button(lib_top, text="Refresh", command=refresh_library).pack(side="left")
    ttk.Button(lib_top, text="Open RimWorld Mods folder", command=lambda: os.startfile(rimworld_mods_dir.get()) if rimworld_mods_dir.get() else None).pack(side="left", padx=6)

    lib_mid = ttk.Frame(tab_library)
    lib_mid.pack(fill="both", expand=True, padx=8, pady=8)

    not_loaded_frame = ttk.LabelFrame(lib_mid, text="Downloaded (not loaded to RimWorld Mods folder)")
    loaded_frame = ttk.LabelFrame(lib_mid, text="Loaded to RimWorld Mods folder")
    not_loaded_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
    loaded_frame.pack(side="left", fill="both", expand=True)

    def make_lib_tree(parent):
        cols = ("id", "name", "packageId")
        tv = ttk.Treeview(parent, columns=cols, show="headings", height=18)
        tv.heading("id", text="Workshop ID")
        tv.heading("name", text="Name")
        tv.heading("packageId", text="PackageId")
        tv.column("id", width=110, anchor="w")
        tv.column("name", width=260, anchor="w")
        tv.column("packageId", width=240, anchor="w")
        tv.pack(fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(parent, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        return tv

    not_loaded_tree = make_lib_tree(not_loaded_frame)
    loaded_tree = make_lib_tree(loaded_frame)

    lib_ctrl = ttk.Frame(tab_library)
    lib_ctrl.pack(fill="x", padx=8, pady=(0, 8))

    def get_selected_ids(tv):
        ids = []
        for iid in tv.selection():
            vals = tv.item(iid, "values")
            if vals:
                ids.append(str(vals[0]))
        return ids

    ttk.Button(lib_ctrl, text="Load selected →", command=lambda: load_selected(get_selected_ids(not_loaded_tree))).pack(side="left")
    ttk.Button(lib_ctrl, text="← Unload selected", command=lambda: unload_selected(get_selected_ids(loaded_tree))).pack(side="left", padx=6)

    def open_selected_workshop(tv):
        ids = get_selected_ids(tv)
        if not ids:
            return
        p = content_root(appid) / ids[0]
        if p.exists():
            try:
                os.startfile(str(p))
            except Exception:
                messagebox.showinfo("Path", str(p))
        else:
            messagebox.showinfo("Not found", f"Folder not found:\n{p}")

    ttk.Button(lib_ctrl, text="Open selected workshop folder", command=lambda: open_selected_workshop(not_loaded_tree)).pack(side="left", padx=12)

    def select_all(tv):
        tv.selection_set(tv.get_children())

    def clear_all_selections():
        not_loaded_tree.selection_remove(not_loaded_tree.selection())
        loaded_tree.selection_remove(loaded_tree.selection())

    def delete_selected_downloaded():
        mods_path = validate_rimworld_folder()
        if mods_path is None:
            return
        ids = get_selected_ids(not_loaded_tree)
        if not ids:
            return

        loaded_now = set(list_loaded_items(mods_path))
        safe = [wid for wid in ids if wid not in loaded_now]
        blocked = [wid for wid in ids if wid in loaded_now]

        if blocked:
            messagebox.showwarning(
                "Cannot delete loaded mods",
                "These are loaded into your RimWorld Mods folder and will NOT be deleted:\n" + "\n".join(blocked),
            )

        if not safe:
            return

        if not messagebox.askyesno(
            "Delete downloaded mods",
            f"Delete {len(safe)} downloaded mod folder(s) from the workshop cache?\n\n"
            "This does NOT touch your RimWorld Mods folder.",
        ):
            return

        deleted = 0
        for wid in safe:
            p = content_root(appid) / wid
            try:
                if p.exists() and p.is_dir():
                    shutil.rmtree(p)
                    deleted += 1
                    ui_log(f"[DELETE] {wid}: deleted from workshop cache", 1)
            except Exception as e:
                ui_log(f"[DELETE] {wid}: failed ({e})", 1)

        ui_events.put(("info", f"Deleted {deleted}/{len(safe)} from workshop cache."))
        ui_events.put(("library_refresh_request", None))

    ttk.Separator(lib_ctrl, orient="vertical").pack(side="left", fill="y", padx=10)
    ttk.Button(lib_ctrl, text="Select all (downloaded)", command=lambda: select_all(not_loaded_tree)).pack(side="left", padx=6)
    ttk.Button(lib_ctrl, text="Select all (loaded)", command=lambda: select_all(loaded_tree)).pack(side="left", padx=6)
    ttk.Button(lib_ctrl, text="Clear selection", command=clear_all_selections).pack(side="left", padx=6)
    ttk.Button(lib_ctrl, text="Delete selected (downloaded)", command=delete_selected_downloaded).pack(side="left", padx=12)

    # -------- event processing --------
    def upsert_queue_row(qi: QueueItem):
        iid = qi.workshop_id
        vals = (qi.workshop_id, qi.source, qi.status, qi.message, qi.added_at)
        if queue_tree.exists(iid):
            queue_tree.item(iid, values=vals)
        else:
            queue_tree.insert("", "end", iid=iid, values=vals)

    def refresh_library_tables(not_loaded: List[str], loaded_list: List[str]):
        def fill(tv, ids: List[str]):
            for iid in tv.get_children():
                tv.delete(iid)
            for wid in ids:
                item_dir = content_root(appid) / wid
                meta = try_read_mod_metadata(item_dir)
                name = meta.get("name", "") or ""
                pkg = meta.get("packageId", "") or ""
                tv.insert("", "end", values=(wid, name, pkg))

        fill(not_loaded_tree, not_loaded)
        fill(loaded_tree, loaded_list)

    def set_progress(done: int, total: int):
        if total <= 0:
            progress_var.set(0.0)
            progress_label.configure(text="0 / 0")
            return
        pct = max(0.0, min(100.0, (done / total) * 100.0))
        progress_var.set(pct)
        progress_label.configure(text=f"{done} / {total}")

    def poll_events():
        try:
            while True:
                typ, payload = ui_events.get_nowait()
                if typ == "log":
                    append_log(payload)
                elif typ == "queue_add":
                    upsert_queue_row(payload)
                elif typ == "queue_update":
                    upsert_queue_row(payload)
                elif typ == "progress":
                    done, total = payload
                    set_progress(done, total)
                elif typ == "info":
                    append_log("[INFO] " + str(payload))
                elif typ == "error":
                    append_log("[ERROR] " + str(payload))
                    messagebox.showerror("Error", str(payload))
                elif typ == "ask_password":
                    pw = simpledialog.askstring("Steam Login", "Steam password:", show="*")
                    ui_events.put(("password", pw or ""))
                elif typ == "worker_done":
                    append_log("[INFO] Worker finished.")
                elif typ == "library_refresh":
                    not_loaded, loaded_list = payload
                    refresh_library_tables(not_loaded, loaded_list)
                    append_log(f"[LIBRARY] Refreshed. Not loaded: {len(not_loaded)} | Loaded: {len(loaded_list)}")
                elif typ == "library_refresh_request":
                    refresh_library()
        except _queue.Empty:
            pass
        root.after(150, poll_events)

    root.after(150, poll_events)

    def on_tab_changed(_event):
        if notebook.index("current") == 1 and rimworld_mods_dir.get().strip():
            refresh_library()

    notebook.bind("<<NotebookTabChanged>>", on_tab_changed)

    def on_close():
        cfg2 = {
            "rimworld_mods_dir": rimworld_mods_dir.get(),
            "batch_size": int(batch_size_var.get()),
            "retries": int(retries_var.get()),
            "api_timeout": int(api_timeout_var.get()),
            "collection_depth": int(collection_depth_var.get()),
            "verbose": int(verbose_var.get()),
            "login_mode": login_mode_var.get(),
            "username": username_var.get(),
            "window_geometry": root.geometry(),
        }
        save_gui_config(cfg2)
        try:
            stop_event.set()
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    append_log(f"RimWorld Mod Downloader v{__version__} (GUI)")
    append_log(f"SteamCMD: {steamcmd_exe}")
    append_log(f"Workshop folder: {content_root(appid)}")

    root.mainloop()
    return 0


def main() -> int:
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
