#!/usr/bin/env python3
"""toledo-dl: Download KU Leuven Toledo lecture recordings, course files, announcements & transcribe locally with Whisper.

Commands:
  toledo-dl login                  Open a browser to log in to Toledo (saves session)
  toledo-dl check                  Exit 0 if logged in, 2 if login required
  toledo-dl doctor                 Check dependencies, login session, GPU/CPU & Ollama status
  toledo-dl calendar [--course X] [--semester 1|2|all] [--upcoming] [--refresh]
                                   Check class schedule, total semester lectures, frequency & classrooms
  toledo-dl list   [--course X]     Show recordings across courses (marks NEW vs done)
  toledo-dl sync   [--course X] [--limit N] [--video] [--audio-only] [--no-transcribe] [--dry-run]
                                   Complete sync: downloads files, recordings, announcements, transcripts & index
  toledo-dl files  [--course X] [--ext EXT] [--dry-run]
                                   Download ALL course materials, data files, code & documents
  toledo-dl pdfs   [--course X] [--dry-run]
                                   Download all course PDFs to ~/Desktop/University/<course>/
  toledo-dl announcements [--course X] [--all]
                                   Check newly added professor announcements
  toledo-dl transcribe [--course X] [--model M] [--lang L] [--dry-run]
                                   Transcribe existing downloaded lecture audios locally with Whisper
  toledo-dl match-slides [--course X] [--force]
                                   Pair lecture transcripts with matching PDF slide decks
  toledo-dl index  [--course X]     Rebuild LLM_Knowledge/ markdown files, Chapters/ & LLM_INDEX.md

Exit codes: 0 = ok, 1 = download errors, 2 = login required.
"""
import argparse
try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
import base64
import getpass
import hashlib
import html
import inspect
import io
import json
import logging
import math
import ntpath
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path

_WIN_DLL_HANDLES: list = []

try:
    import pypdf
    logging.getLogger("pypdf").setLevel(logging.ERROR)
except ImportError:
    pypdf = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

DATA = Path(
    os.environ.get("UANTWERPEN_DATA_DIR", os.environ.get("TOLEDO_DATA_DIR", str(Path.home() / ".local/share/uantwerpen-dl")))
).expanduser()
PROFILE = DATA / "profile"
STATE = DATA / "state.json"
LOCK = DATA / "lock"
_default_out = (
    Path.home() / "Desktop" / "University"
    if (Path.home() / "Desktop").exists()
    else Path.home() / "University"
)
OUT = Path(
    os.environ.get("UANTWERPEN_LECTURES_DIR", os.environ.get("TOLEDO_LECTURES_DIR", str(_default_out)))
).expanduser()
DB = OUT / ".downloaded.json"
PDF_DB = OUT / ".downloaded_pdfs.json"
FILES_DB = OUT / ".downloaded_files.json"
ANNOUNCEMENTS_DB = OUT / ".checked_announcements.json"
CALENDAR_DB = OUT / ".calendar_schedule.json"

DAY_MAP = {
    "0": "Monday",
    "1": "Tuesday",
    "2": "Wednesday",
    "3": "Thursday",
    "4": "Friday",
    "5": "Saturday",
    "6": "Sunday",
}
BASE = os.environ.get("UANTWERPEN_BASE_URL", "https://lms.uantwerpen.be").rstrip("/")
PARTNER = os.environ.get("UANTWERPEN_KALTURA_PARTNER_ID", "")
ENTRY_RE = re.compile(r"entryid/(\d_[a-z0-9]{8})", re.I)

_venv_ytdlp = Path(sys.executable).parent / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
YTDLP = (
    str(_venv_ytdlp)
    if _venv_ytdlp.exists()
    else (shutil.which("yt-dlp") or str(Path.home() / ".local/bin/yt-dlp") or "yt-dlp")
)

if not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi
        _ca_bundle = certifi.where()
        if _ca_bundle and Path(_ca_bundle).is_file():
            os.environ["SSL_CERT_FILE"] = _ca_bundle
    except ImportError:
        pass


_SECRET_QUERY_RE = re.compile(
    r"([?&](?:key|token|ks|sig|signature|auth|access_token|id_token|session|cookie|jwt)=)[^&\s\"']+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._\-~+/]+=*", re.IGNORECASE)
_API_KEY_HEADER_RE = re.compile(r"\b((?:x-goog-api-key|x-api-key)\s*[:=]\s*)[^\s\"',;]+", re.IGNORECASE)


def _redact_secrets(text: str) -> str:
    """Scrub signed URL query tokens and API/Bearer credentials before logging."""
    s = _SECRET_QUERY_RE.sub(r"\1[REDACTED]", str(text))
    s = _BEARER_RE.sub(r"\1[REDACTED]", s)
    return _API_KEY_HEADER_RE.sub(r"\1[REDACTED]", s)


def _harden_private_path(path: Path, is_dir: bool = False) -> None:
    """Restrict permissions on session/profile/state files to owner-only on POSIX."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o700 if is_dir else 0o600)
    except OSError:
        pass


def _ensure_within_dir(target: Path, root: Path) -> Path:
    """Verify target stays strictly inside root without traversing '..' or escaping via symlinks."""
    if ".." in target.parts:
        raise ValueError(f"Unsafe path traversal rejected: {target}")
    root_resolved = root.resolve()
    # Walk from target upward to root and reject any symlink pointing outside root
    cur = target
    checked = 0
    while checked < 64:
        if cur.is_symlink():
            if not cur.resolve().is_relative_to(root_resolved):
                raise ValueError(f"Unsafe symlink outside destination folder rejected: {cur}")
        if cur == root or cur.parent == cur:
            break
        cur = cur.parent
        checked += 1
    target_resolved = target.resolve()
    if not target_resolved.is_relative_to(root_resolved):
        raise ValueError(f"Destination escapes root folder: {target}")
    return target


def _is_path_within_dir(path: Path, root: Path) -> bool:
    """Return True if path (after resolving symlinks) stays strictly inside root."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _safe_write_text_within(path: Path, root: Path, text: str) -> bool:
    """Atomically write UTF-8 text only if path is not a symlink and stays strictly inside root."""
    if path.is_symlink() or not _is_path_within_dir(path, root) or not _is_path_within_dir(path.parent, root):
        log(f"  ✗ Refused unsafe write outside root: {path}")
        return False
    _replace_bytes(path, text.encode("utf-8"))
    return True


def _safe_rmtree(path: Path, root: Path) -> None:
    """Delete a directory only if it is not a symlink and resolves strictly inside root."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        return
    root_resolved = root.resolve()
    if not path.resolve().is_relative_to(root_resolved) or path.resolve() == root_resolved:
        return
    shutil.rmtree(path, ignore_errors=True)


def log(*args):
    print(*args, flush=True)


def _replace_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        temp_path = Path(tmp.name)
        try:
            tmp.write(data)
            tmp.close()
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)


def _write_json(path: Path, data) -> None:
    _replace_bytes(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _require_playwright():
    if sync_playwright is None:
        raise RuntimeError(
            "Playwright is not installed. Run the installer or 'pip install playwright && playwright install chromium'."
        )
    return sync_playwright()


# ---------------------------------------------------------------- Browser / Session
def clean_stale_singleton_locks():
    """Removes stale Chromium Singleton locks if the owning process is no longer running."""
    lock_file = PROFILE / "SingletonLock"
    if lock_file.exists() or lock_file.is_symlink():
        try:
            target = os.readlink(str(lock_file))
            m = re.search(r"-(\d+)$", target)
            pid = int(m.group(1)) if m else None
            is_running = False
            if pid:
                try:
                    os.kill(pid, 0)
                    is_running = True
                except OSError:
                    is_running = False
            if not is_running:
                for f in PROFILE.glob("Singleton*"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
        except Exception:
            for f in PROFILE.glob("Singleton*"):
                try:
                    f.unlink()
                except Exception:
                    pass


def _browser_channel():
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        return "chrome"
    mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    win_chrome = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    if mac_chrome.exists() or any(p.exists() for p in win_chrome):
        return "chrome"
    return None


def open_ctx(p, headless=True):
    DATA.mkdir(parents=True, exist_ok=True)
    _harden_private_path(DATA, is_dir=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    _harden_private_path(PROFILE, is_dir=True)
    clean_stale_singleton_locks()
    launch_kwargs = {
        "headless": headless,
        "args": ["--autoplay-policy=no-user-gesture-required"],
    }
    ch = _browser_channel()
    if ch:
        launch_kwargs["channel"] = ch
    try:
        c = p.chromium.launch_persistent_context(str(PROFILE), **launch_kwargs)
    except Exception as e:
        if "ProcessSingleton" in str(e) or "SingletonLock" in str(e):
            # Stale lock from an abruptly terminated session; clear and retry once
            for f in PROFILE.glob("Singleton*"):
                try:
                    f.unlink()
                except Exception:
                    pass
            c = p.chromium.launch_persistent_context(str(PROFILE), **launch_kwargs)
        else:
            raise
    if STATE.exists():
        try:
            c.add_cookies(json.loads(STATE.read_text(encoding="utf-8"))["cookies"])
        except Exception:
            pass
    return c


def logged_in(c):
    try:
        return c.request.get(BASE + "/learn/api/public/v1/users/me").status == 200
    except Exception:
        return False


def save_state(c):
    DATA.mkdir(parents=True, exist_ok=True)
    _harden_private_path(DATA, is_dir=True)
    c.storage_state(path=str(STATE))
    _harden_private_path(STATE, is_dir=False)


def api(c, path):
    r = c.request.get(BASE + path)
    if r.status == 401:
        raise LoginRequired()
    return r.json()


class LoginRequired(Exception):
    pass


_LOGIN_MSG = "LOGIN_REQUIRED: Not logged in to UAntwerpen Blackboard yet. Run 'uantwerpen-login' (or 'uantwerpen-dl login') first."


def cmd_login(_args):
    with _require_playwright() as p:
        c = open_ctx(p, headless=False)
        page = c.pages[0] if c.pages else c.new_page()
        page.goto(BASE + "/ultra/course")
        log("Log in via the Chrome window (tick 'Remember my device'). It closes automatically once authenticated.")
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(2)
            if logged_in(c):
                time.sleep(2)
                save_state(c)
                c.close()
                log("✓ Login successful and saved!")
                return 0
        c.close()
        log("Timed out waiting for login.")
        return 2


def cmd_check(_args):
    with _require_playwright() as p:
        c = open_ctx(p)
        try:
            response = c.request.get(BASE + "/learn/api/public/v1/users/me", timeout=10000)
            if response.status == 200:
                save_state(c)
                log("LOGGED_IN")
                return 0
            if response.status in (401, 403):
                log(_LOGIN_MSG)
                return 2
            log(f"Could not check the Toledo session (HTTP {response.status}).")
            return 1
        except Exception:
            log("Could not check the Toledo session. Check your connection and try again.")
            return 1
        finally:
            c.close()


KNOWN_SUBJECT_MAPS = {
    "multivariate": {
        1: "Introduction & Course Overview",
        2: "Basic Concepts & Data Representation",
        3: "Geometric Interpretation & Matrix Operations",
        4: "Spectral Decomposition & Covariance Properties",
        5: "Principal Components Analysis - Population PCA",
        6: "Principal Components Analysis - Eigenvalue Decomposition",
        7: "Principal Components Analysis - Variable Correlations & Scree Plots",
        8: "Principal Components Analysis - Biplots & Diagnostic Plots",
        9: "Exploratory Factor Analysis - Orthogonal Factor Model",
        10: "Exploratory Factor Analysis - Estimation & Principal Factor Method",
        11: "Exploratory Factor Analysis - Factor Rotation & Varimax",
        12: "Exploratory Factor Analysis - Car Perception Example",
        13: "Confirmatory Factor Analysis - Model Specification",
        14: "Confirmatory Factor Analysis - Estimation & Fit Indices",
        15: "Confirmatory Factor Analysis - Convergent & Discriminant Validity",
        16: "Confirmatory Factor Analysis - Multi-Group CFA & Invariance",
        17: "Structural Equation Models - Path Analysis & Latent Variables",
        18: "Structural Equation Models - Model Estimation & Identification",
        19: "Structural Equation Models - Mediation & Direct and Indirect Effects",
        20: "Factor Analysis & SEM - Exercise Session",
        21: "Classification & Discriminant Analysis - Two-Group Separation & Fisher LDA",
        22: "Classification & Discriminant Analysis - Decision Boundaries & Examples",
        23: "Classification & Discriminant Analysis - Misclassification Costs & Priors",
        24: "Classification & Discriminant Analysis - Multi-Group Classification",
    },
    "regression": {
        1: "Introduction to Linear Models & Least Squares",
        2: "The General Linear Model & Matrix Formulation",
        3: "Statistical Inference & Hypothesis Testing in Regression",
        4: "Polynomial Regression & Model Selection",
        5: "Categorical Predictors & Dummy Variables in Regression",
    },
    "anova": {
        1: "One-Way ANOVA & Model Assumptions",
        2: "The F-Test & Multiple Comparisons",
        3: "Contrasts & Multiple Testing Corrections",
        4: "ANOVA Formulated as a Linear Regression Model",
        5: "Two-Way ANOVA with Balanced Design",
        6: "Two-Way ANOVA with Unequal Sample Sizes",
        7: "Overparameterized ANOVA Models & Estimability",
        8: "Type I, II, and III Sum of Squares in ANOVA",
    }
}


# ---------------------------------------------------------------- Course Scraper
def format_clean_filename(it, index):
    """Formats raw recording titles into clean 'Lecture 01 - Subject'."""
    raw = it.get("title", "").strip()
    cid = it.get("course", "").lower()

    # 1. Match ANOVA / OneDrive lecture
    if it.get("type") == "onedrive" or "anova" in raw.lower():
        m_lec = re.search(r"lecture[_\s]*(\d+)", raw, re.I)
        sub = re.sub(r"^.*?lecture[_\s]*\d+\s*[-:]?\s*", "", raw, flags=re.I).strip(" -:")
        sub = re.sub(r"\[.*?\]", "", sub).strip(" -:")
        if m_lec:
            num = int(m_lec.group(1))
            if not sub and num in KNOWN_SUBJECT_MAPS["anova"]:
                sub = KNOWN_SUBJECT_MAPS["anova"][num]
            if sub:
                return f"ANOVA Lecture {num:02d} - {clean(sub)}"
            return f"ANOVA Lecture {num:02d}"
        if "extra" in raw.lower() or "two-way" in raw.lower():
            return f"ANOVA {clean(raw)}"

    # 2. Match date and time like "Aula AV 91.12 2026-09-23 09:02"
    m_dt = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2})[:.-](\d{2})", raw)
    if m_dt:
        date_str = f"{m_dt.group(2)}-{m_dt.group(3)}"
        sub = re.sub(r"Aula\s+.*?(\d{4}-\d{2}-\d{2}\s+\d{2}[:.-]\d{2})", "", raw, flags=re.I).strip(" -:")
        if not sub and "multivariate" in cid and index in KNOWN_SUBJECT_MAPS["multivariate"]:
            sub = KNOWN_SUBJECT_MAPS["multivariate"][index]
        if sub:
            return f"Lecture {index:02d} - {clean(sub)}"
        return f"Lecture {index:02d} - Session ({date_str})"

    # 3. Match recording_N or session N
    m_rec = re.search(r"(?:recording|session)[_\s]*(\d+)", raw, re.I)
    if m_rec:
        rec_num = int(m_rec.group(1))
        clean_sub = re.sub(r"^.*?recording[_\s]*\d+\s*[-:]?\s*", "", raw, flags=re.I).strip(" -:")
        clean_sub = re.sub(r"^.*?session[_\s]*\d+\s*[-:]?\s*", "", clean_sub, flags=re.I).strip(" -:")
        clean_sub = re.sub(r"^Linear Models\s*\[[^\]]+\]\s*[-:]?\s*", "", clean_sub, flags=re.I).strip(" -:")
        if not clean_sub and "linear models" in cid and rec_num in KNOWN_SUBJECT_MAPS["regression"]:
            clean_sub = KNOWN_SUBJECT_MAPS["regression"][rec_num]
        if clean_sub:
            return f"Lecture {rec_num:02d} - {clean(clean_sub)}"
        return f"Lecture {rec_num:02d}"

    # 4. Clean course code prefixes like "FCS 2026-27: "
    clean_t = re.sub(r"^[A-Z0-9\s-]+[-:]\s*", "", raw)
    clean_t = re.sub(r"\s*\[Recording\]\s*$", "", clean_t, flags=re.I).strip()
    return f"Lecture {index:02d} - {clean(clean_t)}"


_WIN_RESERVED_RE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)",
    re.IGNORECASE,
)


def _is_win_reserved(name: str) -> bool:
    if _WIN_RESERVED_RE.match(name):
        return True
    is_res = getattr(ntpath, "isreserved", None)
    return bool(is_res(name)) if callable(is_res) else False


def _sanitize_reserved_name(name: str) -> str:
    if not name:
        return "untitled"
    if _is_win_reserved(name):
        return f"_{name}"
    return name


def clean(s):
    s = re.sub(r"\s*\[[A-Z0-9]+\]\s*$", "", s or "").strip()
    s = re.sub(r'[\x00-\x1f\\/:*?"<>|]+', "-", s)
    cleaned = re.sub(r"\s+", " ", s)[:150].strip(" .") or "untitled"
    return _sanitize_reserved_name(cleaned)


def clean_filename(name):
    safe = re.sub(r'[\\/]+', "-", name or "file")
    p = Path(safe)
    stem = clean(p.stem)[:120].strip(" .") or "untitled"
    ext = re.sub(r'[\x00-\x1f\\/:*?"<>|\s]+', "", p.suffix)
    stem = _sanitize_reserved_name(stem)
    candidate = f"{stem}{ext}" if ext else stem
    if _is_win_reserved(candidate):
        candidate = f"_{candidate}"
    return candidate


def courses(c):
    d = api(c, "/learn/api/public/v1/users/me/courses?limit=200&expand=course")
    return [(m["courseId"], m.get("course", {}).get("name", m["courseId"])) for m in d.get("results", [])]


def recordings_kaltura(c, cid):
    out = []

    def walk(parent, parent_title=None):
        path = f"/learn/api/public/v1/courses/{cid}/contents" + (f"/{parent}/children" if parent else "") + "?limit=200"
        for it in _fetch_all_contents(c, path):
            h = it.get("contentHandler", {})
            if h.get("id") == "resource/x-bb-blti-link":
                m = ENTRY_RE.search(h.get("url", ""))
                if m and "kaltura" in h.get("url", ""):
                    out.append({
                        "type": "kaltura",
                        "entry": m.group(1),
                        "content_id": it["id"],
                        "title": it.get("title", ""),
                        "partner_id": PARTNER,
                        "is_embedded": False,
                    })
            elif h.get("id") == "resource/x-bb-document" or it.get("body"):
                body = it.get("body", "")
                raw_html = body
                html_match = re.search(r'data-bbfile="([^"]+)"', body)
                if html_match:
                    try:
                        bbfile = json.loads(html_match.group(1).replace("&quot;", '"'))
                        res_url = bbfile.get("resourceUrl")
                        if res_url and ("html" in res_url.lower() or "unsafe" in res_url.lower()):
                            resp = c.request.get(res_url)
                            if resp.status == 200:
                                raw_html += " " + resp.text()
                    except Exception:
                        pass
                m_entry = re.search(r'(?:entry_id|entryid)[=/](\d_[a-z0-9]{8})', raw_html, re.I)
                if m_entry:
                    entry_id = m_entry.group(1)
                    m_partner = re.search(r'partner_id[=/](\d+)|p/(\d+)', raw_html, re.I)
                    partner_id = (m_partner.group(1) or m_partner.group(2)) if m_partner else PARTNER
                    title = it.get("title", "")
                    if not title or title.lower() in ("ultradocumentbody", "document"):
                        title = parent_title or "Lecture Recording"
                    out.append({
                        "type": "kaltura",
                        "entry": entry_id,
                        "content_id": it["id"],
                        "title": title,
                        "partner_id": partner_id,
                        "is_embedded": True,
                    })
            if it.get("hasChildren"):
                walk(it["id"], parent_title=it.get("title"))

    walk(None)
    return out


def _is_collab_tool(t: dict) -> bool:
    """Detect enabled Blackboard Collaborate Ultra tool entries across Ultra/Classic API shapes."""
    if not isinstance(t, dict):
        return False
    ident = str(t.get("identifier") or t.get("toolId") or "")
    handle = str((t.get("applicationLabelAsMessage") or {}).get("handle") or "")
    if "collab" not in ident.lower() and "collab" not in handle.lower():
        return False
    enabled = (t.get("toolEnabledSetting") or {}).get("isAvailable", True)
    return bool(enabled and t.get("isVisible", True))


def recordings_collaborate(c, cid):
    """Checks Blackboard Collaborate Ultra recordings for the course."""
    out = []
    # Test if collabultra tool is available in course
    res = c.request.get(f"{BASE}/learn/api/v1/courses/{cid}/tools")
    if res.status != 200:
        return out

    # Check if collab is enabled in tools
    tools = res.json().get("results", [])
    has_collab = any(_is_collab_tool(t) for t in tools)
    if not has_collab:
        return out

    page = c.new_page()
    auth_headers = {}
    api_origin = ["https://eu-lti.bbcollab.com"]

    def on_req(r):
        if "bbcollab.com/collab/api/csa" in r.url:
            auth_headers.update(r.headers)
            parsed_u = urllib.parse.urlparse(r.url)
            if parsed_u.scheme and parsed_u.netloc:
                api_origin[0] = f"{parsed_u.scheme}://{parsed_u.netloc}"

    page.on("request", on_req)
    try:
        page.goto(f"{BASE}/ultra/courses/{cid}/outline/collab/launchRecordings")
        for _ in range(25):
            if auth_headers:
                break
            time.sleep(0.5)

        if auth_headers:
            offset = 0
            while True:
                r = page.request.get(
                    f"{api_origin[0]}/collab/api/csa/recordings?limit=100&offset={offset}",
                    headers=auth_headers,
                )
                if r.status != 200:
                    break
                results = r.json().get("results", [])
                for rec in results:
                    rec_id = rec.get("id")
                    title = rec.get("name") or rec.get("mediaName") or "Collab Recording"
                    out.append({
                        "type": "collab",
                        "entry": rec_id,
                        "content_id": rec_id,
                        "title": title,
                        "auth_headers": auth_headers,
                    })
                if len(results) < 100:
                    break
                offset += len(results)
    except Exception:
        pass
    finally:
        page.close()

    return out


def recordings_onedrive(c, cid):
    """Checks for lectures posted as OneDrive/SharePoint video links in course contents."""
    out = []

    def walk(parent):
        path = f"/learn/api/public/v1/courses/{cid}/contents" + (f"/{parent}/children" if parent else "") + "?limit=200"
        for it in _fetch_all_contents(c, path):
            h = it.get("contentHandler", {})
            url = h.get("url", "")
            desc = it.get("description", "") or ""
            links = re.findall(r'https?://[^\s<>"]+', desc)
            if url:
                links.append(url)

            od_links = [lnk for lnk in links if ("1drv.ms" in lnk.lower() or "onedrive.live.com" in lnk.lower() or "sharepoint.com" in lnk.lower())]
            for lnk in od_links:
                m = re.search(r"s!([a-zA-Z0-9_-]+)", lnk)
                entry_id = f"od_{m.group(1)}" if m else f"od_{hashlib.sha256(lnk.encode('utf-8')).hexdigest()[:24]}"
                out.append({
                    "type": "onedrive",
                    "entry": entry_id,
                    "content_id": it["id"],
                    "title": it.get("title", "OneDrive Lecture"),
                    "url": lnk
                })
            if it.get("hasChildren"):
                walk(it["id"])

    walk(None)
    return out


def recordings_all(c, cid):
    recs = recordings_kaltura(c, cid)
    collab_recs = recordings_collaborate(c, cid)
    onedrive_recs = recordings_onedrive(c, cid)
    recs.extend(collab_recs)
    recs.extend(onedrive_recs)
    return recs


def load_db():
    return json.loads(DB.read_text(encoding="utf-8")) if DB.exists() else {}


def save_db(db):
    _write_json(DB, db)


def gather(c, course_filter):
    items = []
    for cid, name in courses(c):
        if course_filter and course_filter.lower() not in name.lower():
            continue
        for r in recordings_all(c, cid):
            r.update(course_id=cid, course=clean(name))
            items.append(r)
    return items


# ---------------------------------------------------------------- Downloader
def get_kaltura_embedded_ks(c, entry_id, partner_id):
    """Captures KS session token for an embedded Kaltura iframe."""
    found = []

    def onreq(r):
        m = re.search(r"ks=([^&]+)", r.url)
        if m:
            val = urllib.parse.unquote(m.group(1))
            if not val.startswith("{") and len(val) > 20:
                found.append(val)

    page = c.new_page()
    page.on("request", onreq)
    try:
        iframe_url = f"https://cdnapisec.kaltura.com/p/{partner_id}/sp/{partner_id}00/embedIframeJs/uiconf_id/45511891/partner_id/{partner_id}?iframeembed=true&playerId=kaltura_player&entry_id={entry_id}"
        page.goto(iframe_url)
        for _ in range(25):
            if found:
                break
            time.sleep(0.4)
    finally:
        page.close()
    return found[-1] if found else None


_KS_CACHE = {}


def get_kaltura_ks(c, course_id, content_id, entry_id=None, partner_id=PARTNER, is_embedded=False):
    """Opens the Toledo item in a background page to capture Kaltura session token."""
    if is_embedded and entry_id:
        cached = _KS_CACHE.get((course_id, partner_id))
        if cached:
            return cached
        # Embedded Kaltura items live inside Ultra documents; grab a valid KS from any BLTI link in the same course.
        for rec in recordings_kaltura(c, course_id):
            if not rec.get("is_embedded") and (rec.get("partner_id") or PARTNER) == partner_id:
                ks = get_kaltura_ks(c, course_id, rec["content_id"], entry_id=rec["entry"], partner_id=partner_id, is_embedded=False)
                if ks:
                    _KS_CACHE[(course_id, partner_id)] = ks
                    return ks
        return get_kaltura_embedded_ks(c, entry_id, partner_id)

    found = []

    def onreq(r):
        if "kaltura.com/api_v3" in r.url and r.post_data:
            m = re.search(r'"ks"\s*:\s*"([^"]+)"', r.post_data)
            if m:
                found.append(m.group(1))
        m = re.search(r"/ks/([^/]+)/", r.url)
        if "playManifest" in r.url and m:
            found.append(m.group(1))

    page = c.new_page()
    page.on("request", onreq)
    try:
        page.goto(f"{BASE}/webapps/blackboard/execute/blti/launchLink?course_id={course_id}&content_id={content_id}")
        for _ in range(40):
            if found:
                break
            if any(k in page.url.lower() for k in ("auth-saml", "idp.", "login.microsoftonline", "adfs")):
                raise LoginRequired()
            time.sleep(0.5)
    finally:
        page.close()
    return found[-1] if found else None


def get_collab_stream_url(c, rec_id, auth_headers):
    """Fetches the video URL for a Collaborate recording."""
    page = c.new_page()
    video_url = None
    try:
        # 1. Get playback launch URL
        r = page.request.get(f"https://eu-lti.bbcollab.com/collab/api/csa/recordings/{rec_id}/url", headers=auth_headers)
        if r.status != 200:
            return None
        launch_url = r.json().get("url")
        if not launch_url:
            return None

        # 2. Open playback page and extract media src
        page.goto(launch_url)
        for _ in range(25):
            dom_srcs = page.evaluate("""() => {
                const v = Array.from(document.querySelectorAll('video, audio, source')).map(el => el.src || el.currentSrc);
                return v.filter(Boolean);
            }""")
            if dom_srcs:
                video_url = dom_srcs[0]
                break
            time.sleep(0.5)
    except Exception:
        pass
    finally:
        page.close()

    return video_url


def download_media(urls, dest, video=True, progress=None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest_mp4 = dest.with_suffix(".mp4")
    dest_mp3 = dest.with_suffix(".mp3")
    tmpl_mp4 = str(dest.with_suffix("")) + ".mp4"
    tmpl_any = str(dest.with_suffix("")) + ".%(ext)s"

    if isinstance(urls, str):
        urls = [urls]

    def run_download(cmd):
        if progress is None:
            return subprocess.run(cmd).returncode == 0
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace") as process:
            last_update = 0.0
            for line in process.stdout:
                log(_redact_secrets(line.rstrip()))
                if re.match(r"\[download\]\s+\d+(?:\.\d+)?%", line) and time.monotonic() - last_update >= 1:
                    progress(_redact_secrets(line.strip()))
                    last_update = time.monotonic()
            return process.wait() == 0

    for u in urls:
        if video:
            cmd = [YTDLP, "--newline", "-o", tmpl_mp4, u]
            if run_download(cmd) and dest_mp4.exists() and dest_mp4.stat().st_size > 0:
                # Extract 16 kHz mono MP3 alongside MP4 for Whisper & lightweight audio
                if not dest_mp3.exists():
                    tmp_mp3 = dest.with_suffix(".tmp.mp3")
                    ffmpeg_cmd = [
                        "ffmpeg", "-y", "-i", str(dest_mp4),
                        "-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k",
                        str(tmp_mp3)
                    ]
                    res = subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if res.returncode == 0 and tmp_mp3.exists() and tmp_mp3.stat().st_size > 0:
                        tmp_mp3.replace(dest_mp3)
                    else:
                        tmp_mp3.unlink(missing_ok=True)
                return True
        else:
            cmd = [
                YTDLP, "--newline", "-f", "ba/w", "-x", "--audio-format", "mp3",
                "--postprocessor-args", "ffmpeg:-ac 1 -ar 16000 -b:a 32k",
                "-o", tmpl_any, u
            ]
            if run_download(cmd) and dest_mp3.exists() and dest_mp3.stat().st_size > 0:
                return True

    # Fallback to audio-only if video download failed
    if video and not (dest_mp4.exists() and dest_mp4.stat().st_size > 0):
        for u in urls:
            cmd = [
                YTDLP, "--newline", "-f", "ba/w", "-x", "--audio-format", "mp3",
                "--postprocessor-args", "ffmpeg:-ac 1 -ar 16000 -b:a 32k",
                "-o", tmpl_any, u
            ]
            if run_download(cmd) and dest_mp3.exists() and dest_mp3.stat().st_size > 0:
                return True

    return False


def download_onedrive_media(c, url, dest, video=True):
    """Downloads lecture from OneDrive as MP4 and extracts 16 kHz mono MP3 alongside it."""
    _ensure_within_dir(dest, OUT)
    page = c.new_page()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest_mp4 = dest.with_suffix(".mp4")
    dest_mp3 = dest.with_suffix(".mp3")
    tmp_mp4 = dest.with_suffix(".tmp.mp4")
    tmp_mp3 = dest.with_suffix(".tmp.mp3")
    try:
        page.goto(url, wait_until="commit", timeout=35000)
        time.sleep(6)
        with page.expect_download(timeout=25000) as dl_info:
            dl_btn = page.query_selector('button[aria-label="Download"], button[title="Download"]')
            if not dl_btn:
                log("   Could not find OneDrive download button")
                return False
            dl_btn.click()
        dl = dl_info.value
        dl.save_as(str(tmp_mp4))
        if tmp_mp4.exists() and tmp_mp4.stat().st_size > 0:
            tmp_mp4.replace(dest_mp4)

        if dest_mp4.exists() and dest_mp4.stat().st_size > 0 and not dest_mp3.exists():
            cmd = [
                "ffmpeg", "-y", "-i", str(dest_mp4),
                "-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k",
                str(tmp_mp3)
            ]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0 and tmp_mp3.exists() and tmp_mp3.stat().st_size > 0:
                tmp_mp3.replace(dest_mp3)
            else:
                tmp_mp3.unlink(missing_ok=True)
        return (dest_mp4.exists() and dest_mp4.stat().st_size > 0) or (dest_mp3.exists() and dest_mp3.stat().st_size > 0)
    except Exception as e:
        log(f"   OneDrive download error: {_redact_secrets(e)}")
        for tmp_f in (tmp_mp4, tmp_mp3):
            try:
                tmp_f.unlink(missing_ok=True)
            except Exception:
                pass
        return False
    finally:
        page.close()


# ---------------------------------------------------------------- PDFs
def _extract_body_files(it, content_id, seen, ext_filter=None):
    """Extracts embedded bbcswebdav / data-bbfile files from Ultra Document HTML bodies."""
    body = it.get("body") or ""
    if not body:
        return []

    found = []
    title_fallback = it.get("title") or ""

    # Match <a> tags pointing to bbcswebdav (with or without data-bbfile)
    for m in re.finditer(r'<a\b([^>]*)>(.*?)</a>', body, re.I | re.S):
        attrs, inner = m.group(1), m.group(2)
        href_m = re.search(r'href=["\']([^"\']+)["\']', attrs, re.I)
        if not href_m:
            continue
        url = html.unescape(href_m.group(1))
        if "/bbcswebdav/" not in url:
            continue

        bbfile = {}
        bb_m = re.search(r'data-bbfile=["\']([^"\']+)["\']', attrs, re.I)
        if bb_m:
            try:
                bbfile = json.loads(html.unescape(bb_m.group(1)))
            except Exception:
                bbfile = {}

        inner_text = re.sub(r"<[^>]+>", "", html.unescape(inner)).strip()
        fname = (
            bbfile.get("displayName")
            or bbfile.get("originalName")
            or bbfile.get("linkName")
            or bbfile.get("alternativeText")
            or inner_text
            or Path(urllib.parse.urlparse(url).path).name
        )
        if not fname:
            continue

        parsed_path = urllib.parse.urlparse(url).path
        fkey = f"bb:{parsed_path}"
        if fkey in seen:
            continue

        ext = Path(fname).suffix.lower().lstrip(".")
        if ext_filter and ext not in ext_filter:
            continue

        seen.add(fkey)
        found.append({
            "content_id": content_id,
            "att_id": None,
            "fkey": fkey,
            "url": url,
            "file_name": fname,
            "title": title_fallback or fname,
        })

    # Fallback: standalone data-bbfile attributes not inside an <a> tag
    for bb_m in re.finditer(r'data-bbfile=["\']([^"\']+)["\']', body, re.I):
        try:
            bbfile = json.loads(html.unescape(bb_m.group(1)))
        except Exception:
            continue
        url = bbfile.get("resourceUrl") or bbfile.get("viewerUrl") or ""
        if "/bbcswebdav/" not in url:
            continue
        if url.startswith("/"):
            url = BASE + url
        parsed_path = urllib.parse.urlparse(url).path
        fkey = f"bb:{parsed_path}"
        if fkey in seen:
            continue
        fname = (
            bbfile.get("displayName")
            or bbfile.get("originalName")
            or bbfile.get("linkName")
            or bbfile.get("alternativeText")
            or Path(parsed_path).name
        )
        if not fname:
            continue
        ext = Path(fname).suffix.lower().lstrip(".")
        if ext_filter and ext not in ext_filter:
            continue
        seen.add(fkey)
        found.append({
            "content_id": content_id,
            "att_id": None,
            "fkey": fkey,
            "url": url,
            "file_name": fname,
            "title": title_fallback or fname,
        })

    return found


def _fetch_all_contents(c, path):
    """Fetches all pages of a Blackboard Learn REST API collection."""
    results = []
    next_url = BASE + path
    while next_url:
        r = c.request.get(next_url)
        if r.status == 401:
            raise LoginRequired()
        if r.status != 200:
            break
        data = r.json()
        results.extend(data.get("results", []))
        next_page = data.get("paging", {}).get("nextPage")
        if next_page:
            next_url = next_page if next_page.startswith("http") else BASE + next_page
        else:
            next_url = None
    return results


def _unique_dest(dest_dir, fname, fkey, db):
    """Returns a collision-safe, path-length-safe destination path."""
    if fkey in db:
        prev = Path(db[fkey])
        if prev.exists() and prev.stat().st_size > 0:
            return prev
    p = Path(fname)
    stem, suffix = p.stem, p.suffix
    max_path_len = 240
    allowed_stem = max(12, max_path_len - len(str(dest_dir)) - 1 - len(suffix) - 6)
    if len(stem) > allowed_stem:
        stem = stem[:allowed_stem].strip(" .") or "file"
    dest = dest_dir / f"{stem}{suffix}"
    if not dest.exists():
        return dest
    idx = 2
    while True:
        candidate = dest_dir / f"{stem}_{idx}{suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


def gather_pdfs(c, cid):
    pdfs = []
    seen = set()

    def walk(parent, folder_parts):
        path = f"/learn/api/public/v1/courses/{cid}/contents" + (f"/{parent}/children" if parent else "") + "?limit=200"
        for it in _fetch_all_contents(c, path):
            h = it.get("contentHandler", {})
            title = it.get("title") or ""
            file_info = h.get("file", {})
            file_name = file_info.get("fileName", "")
            if file_name.lower().endswith(".pdf") or "pdf" in file_info.get("mimeType", "").lower():
                seen.add(it["id"])
                pdfs.append({
                    "content_id": it["id"],
                    "title": title or file_name,
                    "file_name": file_name,
                    "folder_parts": list(folder_parts),
                })
            if it.get("body"):
                for bf in _extract_body_files(it, it["id"], seen, ext_filter={"pdf"}):
                    bf["folder_parts"] = list(folder_parts)
                    pdfs.append(bf)
            if it.get("hasChildren"):
                next_parts = folder_parts + [clean_filename(title)[:60]] if (title and title.lower() != "ultradocumentbody") else folder_parts
                walk(it["id"], next_parts)

    walk(None, [])
    return pdfs


def cmd_pdfs(args):
    db_file = PDF_DB
    db = json.loads(db_file.read_text(encoding="utf-8")) if db_file.exists() else {}

    with _require_playwright() as p:
        c = open_ctx(p)
        try:
            if not logged_in(c):
                raise LoginRequired()

            course_list = [(cid, name) for cid, name in courses(c) if not args.course or args.course.lower() in name.lower()]
            log(f"Scanning {len(course_list)} course(s) for PDFs...")

            total_dl = 0
            for cid, cname in course_list:
                cleaned_cname = clean(cname)
                course_pdfs = gather_pdfs(c, cid)

                new_pdfs = [pdf for pdf in course_pdfs if (pdf.get("fkey") or pdf["content_id"]) not in db]
                if not new_pdfs:
                    continue

                log(f"\n[{cleaned_cname}] Found {len(new_pdfs)} new PDF(s):")

                for pdf in new_pdfs:
                    title = clean_filename(pdf["file_name"] or pdf["title"])
                    if not title.lower().endswith(".pdf"):
                        title += ".pdf"
                    folder_parts = pdf.get("folder_parts") or []
                    dest_dir = OUT / cleaned_cname / Path(*folder_parts) if folder_parts else (OUT / cleaned_cname / "PDFs")
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    fkey = pdf.get("fkey") or pdf["content_id"]
                    dest = _unique_dest(dest_dir, title, fkey, db)
                    log(f"  • {title}")

                    if args.dry_run:
                        continue

                    if pdf.get("url"):
                        dl_res = c.request.get(pdf["url"])
                        if dl_res.status == 200 and len(dl_res.body()) > 0:
                            dest.write_bytes(dl_res.body())
                            db[fkey] = str(dest)
                            total_dl += 1
                            log(f"    ✓ Saved: {dest.name}")
                        else:
                            log("    ✗ Failed to download attachment")
                        continue

                    # Get attachment download url
                    att_res = c.request.get(f"{BASE}/learn/api/public/v1/courses/{cid}/contents/{pdf['content_id']}/attachments")
                    if att_res.status == 200 and att_res.json().get("results"):
                        att_id = att_res.json()["results"][0]["id"]
                        dl_res = c.request.get(f"{BASE}/learn/api/public/v1/courses/{cid}/contents/{pdf['content_id']}/attachments/{att_id}/download")
                        if dl_res.status == 200 and len(dl_res.body()) > 0:
                            dest.write_bytes(dl_res.body())
                            db[fkey] = str(dest)
                            total_dl += 1
                            log(f"    ✓ Saved: {dest.name}")
                        else:
                            log("    ✗ Failed to download attachment")
                    else:
                        log("    ✗ Attachment not found")

            db_file.parent.mkdir(parents=True, exist_ok=True)
            db_file.write_text(json.dumps(db, indent=2), encoding="utf-8")
            log(f"\nDone! Downloaded {total_dl} PDF(s).")
            return 0
        except LoginRequired:
            log(_LOGIN_MSG)
            return 2
        finally:
            c.close()


def gather_files(c, cid, ext_filter=None):
    """Recursively walks course content to find all attached files, datasets, and external links."""
    items = []
    seen = set()

    def walk(parent, folder_parts):
        path = f"/learn/api/public/v1/courses/{cid}/contents" + (f"/{parent}/children" if parent else "") + "?limit=200"
        for it in _fetch_all_contents(c, path):
            content_id = it["id"]
            h = it.get("contentHandler", {})
            hid = h.get("id", "")
            title = it.get("title") or ""

            # Skip recordings or external LTI links
            if hid == "resource/x-bb-blti-link":
                continue

            # Save external links (OneDrive, Drive, YouTube, etc.) as .url shortcuts + Links.md
            if hid == "resource/x-bb-externallink" and h.get("url"):
                ext_url = h["url"]
                if not ext_filter or "url" in ext_filter:
                    fkey = f"link:{content_id}"
                    if fkey not in seen:
                        seen.add(fkey)
                        items.append({
                            "content_id": content_id,
                            "att_id": None,
                            "fkey": fkey,
                            "url": ext_url,
                            "is_link": True,
                            "file_name": f"{clean_filename(title or 'link')}.url",
                            "title": title or ext_url,
                            "folder_parts": list(folder_parts),
                        })
                continue

            # Check attachments for non-folders
            if hid != "resource/x-bb-folder":
                att_res = c.request.get(f"{BASE}/learn/api/public/v1/courses/{cid}/contents/{content_id}/attachments")
                has_attachments = False
                if att_res.status == 200:
                    atts = att_res.json().get("results", [])
                    if atts:
                        has_attachments = True
                        for att in atts:
                            att_id = att["id"]
                            if att_id in seen:
                                continue
                            seen.add(att_id)
                            fname = att.get("fileName") or title or f"file_{att_id}"
                            ext = Path(fname).suffix.lower().lstrip(".")
                            if ext_filter and ext not in ext_filter:
                                continue
                            items.append({
                                "content_id": content_id,
                                "att_id": att_id,
                                "remote_version": att.get("modified") or it.get("modified"),
                                "file_name": fname,
                                "title": title or fname,
                                "folder_parts": list(folder_parts),
                            })

                # Fallback: check file in contentHandler if no attachments were returned
                if not has_attachments:
                    file_info = h.get("file", {})
                    fname = file_info.get("fileName")
                    if fname and content_id not in seen:
                        ext = Path(fname).suffix.lower().lstrip(".")
                        if not ext_filter or ext in ext_filter:
                            seen.add(content_id)
                            items.append({
                                "content_id": content_id,
                                "att_id": None,
                                "remote_version": file_info.get("modified") or it.get("modified"),
                                "file_name": fname,
                                "title": title or fname,
                                "folder_parts": list(folder_parts),
                            })

                # Also extract files embedded in Ultra Document HTML bodies (data-bbfile / bbcswebdav)
                if it.get("body"):
                    for bf in _extract_body_files(it, content_id, seen, ext_filter=ext_filter):
                        bf["folder_parts"] = list(folder_parts)
                        bf["remote_version"] = it.get("modified")
                        items.append(bf)

            if it.get("hasChildren"):
                next_parts = folder_parts + [clean_filename(title)[:60]] if (title and title.lower() != "ultradocumentbody") else folder_parts
                walk(content_id, next_parts)

    walk(None, [])
    return items


VIDEO_EXTS = {".mov", ".mp4", ".mkv", ".webm", ".avi", ".m4v"}


def _count_sync_item(report, course, kind, count=1):
    if report is not None and count:
        counts = report["courses"].setdefault(course, {})
        counts[kind] = counts.get(kind, 0) + count


def sync_files(c, course_list, dry_run=False, ext_filter=None, *, report=None, check_revisions=False):
    """Syncs new course files, PDFs, datasets, scripts, and external links. Returns count of downloaded files."""
    db_file = FILES_DB
    db = json.loads(db_file.read_text(encoding="utf-8")) if db_file.exists() else {}
    pdf_db = json.loads(PDF_DB.read_text(encoding="utf-8")) if PDF_DB.exists() else {}
    versions_path = db_file.with_name(".attachment_versions.json")
    versions = json.loads(versions_path.read_text(encoding="utf-8")) if versions_path.exists() else {}

    filter_str = f" [filter: {', '.join(ext_filter)}]" if ext_filter else ""
    log(f"Scanning {len(course_list)} course(s) for files & datasets{filter_str}...")

    total_dl = 0
    for cid, cname in course_list:
        cleaned_cname = clean(cname)
        course_files = gather_files(c, cid, ext_filter=ext_filter)

        pending = []
        for f in course_files:
            fkey = f.get("fkey") or f["att_id"] or f["content_id"]
            prior = db.get(fkey)
            if f["file_name"].lower().endswith(".pdf") and (not prior or not Path(prior).is_file()):
                prior = pdf_db.get(fkey) or (pdf_db.get(f["content_id"]) if not f.get("fkey") else None)
            existing = Path(prior) if prior and Path(prior).is_file() and Path(prior).stat().st_size else None
            version = f.get("remote_version")
            version_key = f"{cid}:{fkey}"
            cached = versions.get(version_key, {})
            needs_check = (
                not f.get("is_link") and Path(f["file_name"]).suffix.lower() not in VIDEO_EXTS
                and (check_revisions or (version and version != cached.get("version"))
                     or cached.get("etag") or cached.get("last_modified"))
            )
            if existing is None or needs_check:
                pending.append((f, fkey, existing, version_key, cached))

        if not pending:
            continue

        log(f"\n[{cleaned_cname}] {sum(existing is None for _, _, existing, _, _ in pending)} missing file(s), "
            f"{sum(existing is not None for _, _, existing, _, _ in pending)} revision check(s).")

        for f, fkey, existing, version_key, cached in pending:
            fname = clean_filename(f["file_name"])
            ext = Path(fname).suffix.lower()
            is_pdf = ext == ".pdf"
            is_video = ext in VIDEO_EXTS
            is_link = bool(f.get("is_link"))
            folder_parts = f.get("folder_parts") or []

            course_root = OUT / cleaned_cname
            if is_video:
                dest_dir = course_root / "Recordings"
            elif folder_parts:
                safe_parts = [clean(fp) for fp in folder_parts if str(fp).strip() not in ("", ".", "..")]
                dest_dir = course_root / Path(*safe_parts) if safe_parts else course_root / "Files"
            else:
                dest_dir = course_root / ("PDFs" if is_pdf else "Files")

            dest = existing or _unique_dest(dest_dir, fname, fkey, db)
            try:
                _ensure_within_dir(course_root, OUT)
                _ensure_within_dir(dest, OUT)
                _ensure_within_dir(dest, course_root)
            except ValueError as ve:
                _count_sync_item(report, cleaned_cname, "failed")
                log(f"    ✗ Skipped unsafe path for {fname}: {ve}")
                continue

            tag = "LINK" if is_link else ("VIDEO" if is_video else ("PDF" if is_pdf else "DATA/FILE"))
            rel_label = "/".join(folder_parts + [dest.name]) if (folder_parts and not is_video) else dest.name
            log(f"  • {rel_label} [{tag}]")

            if dry_run:
                continue

            if is_link:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _safe_write_text_within(dest, course_root, f"[InternetShortcut]\nURL={f['url']}\n")
                links_md = course_root / "Links.md"
                existing_md = (
                    links_md.read_text(encoding="utf-8")
                    if (links_md.exists() and not links_md.is_symlink() and _is_path_within_dir(links_md, course_root))
                    else f"# External Links: {cleaned_cname}\n\n"
                )
                entry_line = f"- [{f['title']}]({f['url']})"
                if f['url'] not in existing_md:
                    _safe_write_text_within(links_md, course_root, existing_md + entry_line + "\n")
                db[fkey] = str(dest)
                total_dl += 1
                _count_sync_item(report, cleaned_cname, "files")
                log(f"    ✓ Saved shortcut: {dest.name}")
                continue

            # Download attachment / bbcswebdav file
            endpoint = f"{BASE}/learn/api/public/v1/courses/{cid}/contents/{f['content_id']}/attachments"
            headers = {}
            if existing is not None and not check_revisions and f.get("remote_version") == cached.get("version"):
                if cached.get("etag"):
                    headers["If-None-Match"] = cached["etag"]
                elif cached.get("last_modified"):
                    headers["If-Modified-Since"] = cached["last_modified"]
            try:
                download_url = f.get("url")
                if not download_url and f["att_id"]:
                    download_url = f"{endpoint}/{f['att_id']}/download"
                if not download_url:
                    att_res = c.request.get(endpoint)
                    if att_res.status == 200 and att_res.json().get("results"):
                        download_url = f"{endpoint}/{att_res.json()['results'][0]['id']}/download"
                dl_res = c.request.get(download_url, **({"headers": headers} if headers else {})) if download_url else None
                if dl_res is not None and dl_res.status == 304 and existing is not None:
                    continue
                if dl_res is not None and dl_res.status == 401:
                    raise LoginRequired()
                data = dl_res.body() if dl_res is not None and dl_res.status == 200 else b""
                if not data:
                    raise OSError(f"download unavailable (HTTP {dl_res.status if dl_res is not None else 'unknown'})")
                response_headers = getattr(dl_res, "headers", {})
                incoming_hash = hashlib.sha256(data).hexdigest()
                if existing is None:
                    _replace_bytes(dest, data)
                    total_dl += 1
                    _count_sync_item(report, cleaned_cname, "files")
                    log(f"    ✓ Saved: {dest.name}")
                else:
                    previous_hash = _file_sha256(existing)
                    if previous_hash != incoming_hash:
                        # Keep the old bytes outside the searchable course tree.
                        rev_dir = DATA / "revisions" / previous_hash
                        rev_dir.mkdir(parents=True, exist_ok=True)
                        _harden_private_path(DATA / "revisions", is_dir=True)
                        backup = rev_dir / existing.name
                        if not backup.exists():
                            _replace_bytes(backup, existing.read_bytes())
                        _replace_bytes(dest, data)
                        _count_sync_item(report, cleaned_cname, "revised")
                        log(f"    ↻ Revised: {dest.name} (previous copy: {backup})")
                db[fkey] = str(dest)
                if is_pdf:
                    pdf_db[fkey] = str(dest)
                    if not f.get("fkey"):
                        pdf_db[f["content_id"]] = str(dest)
                versions[version_key] = {
                    "version": f.get("remote_version"),
                    "etag": response_headers.get("etag"),
                    "last_modified": response_headers.get("last-modified"),
                }
            except LoginRequired:
                raise
            except Exception as exc:
                _count_sync_item(report, cleaned_cname, "failed")
                log(f"    ✗ Failed to download or check revision: {fname} ({_redact_secrets(exc)})")

    if not dry_run:
        _write_json(db_file, db)
        _write_json(PDF_DB, pdf_db)
        _write_json(versions_path, versions)

    log(f"Files sync: {total_dl} new file(s) downloaded.")
    return total_dl


def cmd_files(args):
    """Downloads all course files & attachments (PDFs, datasets, scripts, archives, docs) across courses."""
    ext_filter = None
    if getattr(args, "ext", None):
        ext_filter = {e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()}

    with _require_playwright() as p:
        c = open_ctx(p)
        try:
            if not logged_in(c):
                raise LoginRequired()

            course_list = [(cid, name) for cid, name in courses(c) if not args.course or args.course.lower() in name.lower()]
            report = {"courses": {}}
            sync_files(c, course_list, dry_run=args.dry_run, ext_filter=ext_filter, report=report,
                       check_revisions=getattr(args, "check_revisions", False))
            if not args.dry_run and not ext_filter:
                sync_announcements(c, course_list)
                try:
                    sync_calendar_and_deadlines(c, course_list)
                except Exception as e:
                    log(f"  [Calendar] Warning: Could not update calendar/deadlines: {e}")
            return int(any(counts.get("failed") for counts in report["courses"].values()))
        except LoginRequired:
            log(_LOGIN_MSG)
            return 2
        finally:
            c.close()


def html_to_text(html_content):
    """Converts HTML markup in announcements into clean readable text."""
    if not html_content:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html_content)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)</li>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "  • ", text)
    text = re.sub(r'<a\s+(?:[^>]*?\s+)?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r'\2 (\1)', text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sync_announcements(c, course_list, show_all=False):
    """Checks for newly added course announcements. Saves them to Announcements.md in each course."""
    db_file = ANNOUNCEMENTS_DB
    db = json.loads(db_file.read_text(encoding="utf-8")) if db_file.exists() else {}
    files_db = json.loads(FILES_DB.read_text(encoding="utf-8")) if FILES_DB.exists() else {}
    files_db_dirty = False
    ann_seen = set()

    new_count = 0
    all_course_anns = {}

    for cid, cname in course_list:
        try:
            anns = _fetch_all_contents(c, f"/learn/api/public/v1/courses/{cid}/announcements?limit=50")
            if not anns:
                continue
        except Exception:
            continue

        cleaned_cname = clean(cname)
        course_dir = OUT / cleaned_cname
        try:
            _ensure_within_dir(course_dir, OUT)
        except ValueError as ve:
            log(f"  ✗ Skipped unsafe announcement course path for {cleaned_cname}: {ve}")
            continue
        course_dir.mkdir(parents=True, exist_ok=True)
        md_file = course_dir / "Announcements.md"

        new_for_course = []
        course_md_entries = []

        for ann in anns:
            ann_id = ann["id"]
            title = ann.get("title", "Announcement")
            created = ann.get("created", "")
            date_str = created.replace("T", " ")[:16] if created else "Unknown date"
            raw_body = ann.get("body", "")
            body = html_to_text(raw_body)

            # Download any inline bbcswebdav / data-bbfile attachments in announcement
            for bf in _extract_body_files(ann, ann_id, ann_seen):
                fkey = bf.get("fkey")
                if fkey and fkey in files_db and Path(files_db[fkey]).exists():
                    continue
                ann_files_dir = course_dir / "Announcements"
                ann_files_dir.mkdir(parents=True, exist_ok=True)
                fname = clean_filename(bf["file_name"])
                dest = _unique_dest(ann_files_dir, fname, fkey, files_db)
                try:
                    dl_res = c.request.get(bf["url"])
                    if dl_res.status == 200 and len(dl_res.body()) > 0:
                        dest.write_bytes(dl_res.body())
                        files_db[fkey] = str(dest)
                        files_db_dirty = True
                        log(f"    ✓ Announcement attachment: {dest.name}")
                except Exception:
                    pass

            course_md_entries.append(f"### {title}\n**Posted:** {date_str}\n\n{body}\n\n---\n")

            if ann_id not in db:
                new_for_course.append({
                    "id": ann_id,
                    "title": title,
                    "date": date_str,
                    "body": body,
                    "created": created
                })
                db[ann_id] = {
                    "course": cleaned_cname,
                    "title": title,
                    "created": created,
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                new_count += 1
            elif show_all:
                new_for_course.append({
                    "id": ann_id,
                    "title": title,
                    "date": date_str,
                    "body": body,
                    "created": created
                })

        # Persist full announcement history in course folder
        if course_md_entries:
            md_content = f"# Announcements: {cleaned_cname}\n\n" + "\n".join(course_md_entries)
            _safe_write_text_within(md_file, course_dir, md_content)

        if new_for_course:
            all_course_anns[cleaned_cname] = new_for_course

    if all_course_anns:
        header = "📢 ALL ANNOUNCEMENTS" if show_all else "📢 NEW ANNOUNCEMENTS"
        log(f"\n{header}:")
        for cname, anns in all_course_anns.items():
            log("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            log(f"📚 {cname} ({len(anns)} announcement{'s' if len(anns) > 1 else ''}):")
            log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            for a in anns:
                log(f"\n  📌 {a['title']}")
                log(f"  🕒 Posted: {a['date']}")
                if a['body']:
                    indented_body = "\n".join("     " + line for line in a['body'].splitlines())
                    log(indented_body)
                log("")
    else:
        log("No new announcements.")

    db_file.parent.mkdir(parents=True, exist_ok=True)
    db_file.write_text(json.dumps(db, indent=2), encoding="utf-8")
    if files_db_dirty:
        FILES_DB.parent.mkdir(parents=True, exist_ok=True)
        FILES_DB.write_text(json.dumps(files_db, indent=2), encoding="utf-8")
    return new_count


def cmd_announcements(args):
    """Checks course announcements and displays new ones."""
    with _require_playwright() as p:
        c = open_ctx(p)
        try:
            if not logged_in(c):
                raise LoginRequired()

            course_list = [(cid, name) for cid, name in courses(c) if not args.course or args.course.lower() in name.lower()]
            log(f"Checking announcements across {len(course_list)} course(s)...")
            sync_announcements(c, course_list, show_all=getattr(args, "all", False))
            return 0
        except LoginRequired:
            log(_LOGIN_MSG)
            return 2
        finally:
            c.close()


# ---------------------------------------------------------------- Commands
def cmd_list(args):
    with _require_playwright() as p:
        c = open_ctx(p)
        try:
            if not logged_in(c):
                raise LoginRequired()
            items = gather(c, args.course)
        except LoginRequired:
            log(_LOGIN_MSG)
            return 2
        finally:
            c.close()

    db = load_db()
    for it in items:
        status = "✓ DONE" if it["entry"] in db else "  NEW "
        source = f"[{it['type'].upper()}]"
        log(f"  {status} {source:10} {it['course']} | {it['title']}")
    new_count = sum(it["entry"] not in db for it in items)
    log(f"\n{new_count} new / {len(items)} total recordings.")
    return 0


# ---------------------------------------------------------------- Transcription
_VAGUE_SUB_RE = re.compile(
    r"^(?:session(?:\s*\(\d{2}-\d{2}\))?|recording(?:\s*\d+)?|lecture(?:\s*\d+)?|video|class|part\s*\d+|untitled|"
    r"aula\b.*|\d{4}[-./]\d{2}[-./]\d{2}.*|\d{2}[-./]\d{2}(?:\s+\d{2}[:.-]\d{2})?)$",
    re.I,
)


def _is_vague_lecture_stem(stem: str) -> bool:
    """Return True if a lecture stem lacks a descriptive topic name."""
    if re.match(r"^(?:ANOVA\s+)?Lecture\s+\d+\s*$", stem, re.I):
        return True
    m = re.match(r"^(?:ANOVA\s+)?Lecture\s+\d+\s*-\s*(.+)$", stem, re.I)
    if not m:
        return False
    sub = m.group(1).strip()
    return not sub or bool(_VAGUE_SUB_RE.match(sub))


def _suggest_lecture_topic(txt_path: Path) -> str:
    """Generate a concise 3-6 word topic title from a transcript using local Ollama."""
    if not txt_path.exists():
        return ""
    excerpt = txt_path.read_text(encoding="utf-8", errors="ignore").strip()[:6000]
    if len(excerpt) < 100:
        return ""

    prompt = (
        "Given this university lecture transcript excerpt, reply with ONLY a concise 3-to-6-word "
        "topic title in Title Case (no quotes, no trailing punctuation, no 'Lecture' prefix). "
        "Example: Maximum Likelihood Estimation & Asymptotics\n\n"
        f"Transcript:\n{excerpt}"
    )
    raw_title = ""

    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            models = [m.get("name", "") for m in json.loads(r.read().decode("utf-8")).get("models", []) if m.get("name")]
        if models:
            chosen = next((m for m in models if any(p in m.lower() for p in ("phi4", "qwen", "llama", "gemma"))), models[0])
            body = json.dumps({"model": chosen, "prompt": prompt, "stream": False}).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/generate",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw_title = json.loads(resp.read().decode("utf-8")).get("response", "").strip()
    except Exception:
        raw_title = ""

    if not raw_title:
        return ""
    line = raw_title.splitlines()[0].strip().strip("\"'`.,:;!?")
    line = re.sub(r"^(?:ANOVA\s+)?Lecture\s+\d+\s*[-:]\s*", "", line, flags=re.I).strip()
    cleaned = clean(line)
    return "" if cleaned == "untitled" or len(cleaned) < 4 else cleaned[:80].strip()


def _maybe_rename_vague_lecture(audio_path: Path) -> Path:
    """Rename vague lecture files (e.g. 'Lecture 01 - Session (09-23)') using transcript content."""
    if not _is_vague_lecture_stem(audio_path.stem):
        return audio_path
    txt_path = audio_path.with_suffix(".txt")
    topic = _suggest_lecture_topic(txt_path)
    if not topic:
        return audio_path

    m_prefix = re.match(r"^((?:ANOVA\s+)?Lecture\s+\d+)", audio_path.stem, re.I)
    if not m_prefix:
        return audio_path
    new_stem = f"{m_prefix.group(1)} - {topic}"
    parent = audio_path.parent
    candidate_stem = new_stem
    n = 2
    while any((parent / f"{candidate_stem}{ext}").exists() for ext in (".mp4", ".mp3", ".txt", ".srt", ".vtt", ".md")) and candidate_stem != audio_path.stem:
        candidate_stem = f"{new_stem} ({n})"
        n += 1
    if candidate_stem == audio_path.stem:
        return audio_path

    old_paths = set()
    new_primary = audio_path
    for ext in (".mp4", ".mp3", ".txt", ".srt", ".vtt", ".md"):
        old_p = audio_path.with_suffix(ext)
        if old_p.exists():
            old_paths.add(str(old_p))
            new_p = parent / f"{candidate_stem}{ext}"
            old_p.rename(new_p)
            if ext == audio_path.suffix:
                new_primary = new_p

    # Carry transcript completion/in-progress markers along with the renamed stem.
    for marker_fn, suffix in (
        (_transcript_done_marker, ".transcript.done.json"),
        (_transcript_inprogress_marker, ".transcript.inprogress"),
    ):
        old_marker = marker_fn(audio_path)
        if old_marker.exists():
            old_marker.rename(parent / f".{candidate_stem}{suffix}")

    # Keep .downloaded.json in sync so renamed files aren't re-downloaded
    if DB.exists() and old_paths:
        try:
            db = load_db()
            changed = False
            best_target = parent / f"{candidate_stem}.mp4"
            if not best_target.exists():
                best_target = new_primary
            for k, v in list(db.items()):
                if v in old_paths:
                    db[k] = str(best_target)
                    changed = True
            if changed:
                save_db(db)
        except Exception:
            pass

    log(f"  ✓ Renamed vague lecture -> {candidate_stem}")
    return new_primary


def _release_gpu_vram():
    """Releases GPU VRAM after Whisper transcription so Ollama does not hit CUDA OOM."""
    import gc
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def transcribe_file(audio_path, model_size="auto", language=None, device_index=0, *, allow_cpu=True, device="auto"):
    """Transcribes an audio/video file locally using GPU (CUDA) or CPU Whisper."""
    txt_dest = audio_path.with_suffix(".txt")
    srt_dest = audio_path.with_suffix(".srt")

    if not _transcript_inprogress_marker(audio_path).exists() and txt_dest.exists() and srt_dest.exists():
        log(f"  [Transcribe] Skipping (already transcribed): {txt_dest.name}")
        _maybe_rename_vague_lecture(audio_path)
        return True

    try:
        if sys.platform == "win32":
            nvidia_roots = [
                Path(sys.prefix) / "Lib" / "site-packages" / "nvidia",
                Path(sys.executable).resolve().parent.parent / "Lib" / "site-packages" / "nvidia",
            ]
            path_entries = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
            for venv_nvidia in nvidia_roots:
                if not venv_nvidia.exists():
                    continue
                for bin_dir in venv_nvidia.glob("*/bin"):
                    bin_str = str(bin_dir)
                    if bin_str not in path_entries:
                        path_entries.insert(0, bin_str)
                    if hasattr(os, "add_dll_directory"):
                        try:
                            # Keep handle alive; CPython removes the DLL dir when garbage-collected.
                            _WIN_DLL_HANDLES.append(os.add_dll_directory(bin_str))
                        except Exception:
                            pass
            os.environ["PATH"] = os.pathsep.join(path_entries)
        from faster_whisper import WhisperModel
    except ImportError:
        log("  [Transcribe] Error: faster-whisper is not installed.")
        return False

    gpu_model = "medium" if model_size == "auto" else model_size
    cpu_model = "small" if model_size == "auto" else model_size
    src_path = audio_path.with_suffix(".mp3") if (audio_path.suffix.lower() != ".mp3" and audio_path.with_suffix(".mp3").exists()) else audio_path

    def _decode_audio(p):
        import numpy as np
        ff = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(p), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
            capture_output=True,
            check=True,
        )
        return np.frombuffer(ff.stdout, np.float32)

    def _transcribe_with(m):
        try:
            segs, inf = m.transcribe(str(src_path), language=language, beam_size=5)
            return list(segs), inf
        except TypeError:
            audio_np = _decode_audio(src_path)
            segs, inf = m.transcribe(audio_np, language=language, beam_size=5)
            return list(segs), inf

    model = None
    used_device = "cpu" if device == "cpu" else "cuda"
    start_time = time.time()
    try:
        if device == "cpu":
            log(f"  [Transcribe] Using local CPU with Whisper '{cpu_model}' (explicit --backend cpu)...")
            model = WhisperModel(cpu_model, device="cpu", compute_type="int8")
            log(f"  [Transcribe] Transcribing {src_path.name}...")
            segments, info = _transcribe_with(model)
        else:
            try:
                log(f"  [Transcribe] Loading Whisper '{gpu_model}' model on GPU {device_index} (CUDA)...")
                model = WhisperModel(gpu_model, device="cuda", device_index=device_index, compute_type="float16")
                log(f"  [Transcribe] [GPU {device_index}] Transcribing {src_path.name}...")
                segments, info = _transcribe_with(model)
            except Exception as e:
                if device == "cuda" or not allow_cpu:
                    log(f"  [Transcribe] CUDA unavailable ({e}). Recording left pending. Run 'toledo-dl transcribe' to retry with CPU fallback.")
                    return False
                used_device = "cpu"
                log(f"  [Transcribe] CUDA unavailable ({e}). Using local CPU with Whisper '{cpu_model}'; long recordings may take a while.")
                model = WhisperModel(cpu_model, device="cpu", compute_type="int8")
                log(f"  [Transcribe] Transcribing {src_path.name}...")
                segments, info = _transcribe_with(model)
    finally:
        if model is not None:
            del model
        _release_gpu_vram()

    detected_lang = info.language if hasattr(info, "language") else "en"
    log(f"  [Transcribe] Detected language: {detected_lang.upper()} (probability: {info.language_probability:.2f})")

    seg_dicts = [
        {"start": float(segment.start), "end": float(segment.end), "text": str(segment.text).strip()}
        for segment in segments
        if str(segment.text).strip()
    ]
    used_model = cpu_model if used_device == "cpu" else gpu_model
    recording_sha = _file_sha256(audio_path) or ""
    txt_dest, srt_dest = _commit_transcript_pair(
        audio_path,
        seg_dicts,
        backend=f"local-{used_device}",
        model=used_model,
        recording_sha256=recording_sha,
    )

    elapsed = time.time() - start_time
    log(f"  ✓ [Transcribe] Done in {elapsed:.1f}s!")
    log(f"    Saved transcript: {txt_dest.name}")
    log(f"    Saved subtitles:  {srt_dest.name}")

    # Automatically match PDF slides (strictly local unless explicitly approved)
    txt_dest = _maybe_rename_vague_lecture(audio_path).with_suffix(".txt")
    try:
        course_dir = txt_dest.parent
        while course_dir.parent != OUT and course_dir != OUT:
            course_dir = course_dir.parent
        if course_dir.parent == OUT:
            match_transcript_with_pdfs(txt_dest, course_dir)
    except Exception as e:
        log(f"  [Auto-Process] Warning: Could not auto-process transcript: {e}")

    return True


_TRANSCRIBE_CACHE_VERSION = "v1"
_GEMINI_INTERACTIONS_API_REVISION = "2026-05-20"
_GEMINI_INTERACTIONS_V1_URL = "https://generativelanguage.googleapis.com/v1/interactions"
_GEMINI_INTERACTIONS_V1BETA_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
_GEMINI_TRANSCRIBE_DEFAULT_MODEL = "gemini-3.5-transcribe"
_OPENAI_TRANSCRIBE_DEFAULT_MODEL = "whisper-1"
_GEMINI_TRANSCRIBE_CHUNK_SECONDS = 900
_MIN_GEMINI_SPLIT_SECONDS = 60.0
_MAX_GEMINI_SPLIT_DEPTH = 3
_GEMINI_TRANSCRIBE_MAX_INLINE_BYTES = 18 * 1024 * 1024
_DEFAULT_CLOUD_TRANSCRIBE_MAX_MINUTES = 180.0

_GEMINI_LANG_TO_BCP47 = {
    "en": "en-US",
    "nl": "nl-NL",
    "fr": "fr-FR",
    "de": "de-DE",
    "es": "es-ES",
    "it": "it-IT",
    "pt": "pt-PT",
    "pl": "pl-PL",
    "ru": "ru-RU",
    "zh": "cmn-Hans-CN",
    "ja": "ja-JP",
    "ko": "ko-KR",
}


def _estimate_cloud_cost(
    feature: str,
    provider: str,
    model: str = "",
    *,
    total_minutes: float = 0.0,
    item_count: int = 0,
    page_count: int = 0,
    has_video: bool = False,
) -> str:
    """Return a rough human-readable estimated API cost string for cloud batch previews."""
    prov = (provider or "").lower()
    if feature == "transcribe":
        mins = max(0.0, float(total_minutes))
        if prov == "openai":
            low = mins * 0.003 if "mini" in (model or "").lower() else mins * 0.006
            return f"~${low:.2f} USD (approx. ${0.006 if 'mini' not in (model or '').lower() else 0.003:.3f}/min)"
        # Gemini audio token rate (~32 tokens/sec)
        low = mins * 0.001
        high = mins * 0.004
        return f"~${low:.2f}-${high:.2f} USD (varies by model & prompt)"
    if feature == "pdf-vision":
        pages = max(0, int(page_count))
        pdfs = max(0, int(item_count))
        if pages > 0:
            low = pages * 0.0005
            high = pages * 0.0012
            return f"~${low:.2f}-${high:.2f} USD ({pdfs} PDF(s), ~{pages} page(s))"
        low = pdfs * 0.015
        high = pdfs * 0.05
        return f"~${low:.2f}-${high:.2f} USD ({pdfs} PDF(s))"
    count = max(0, int(item_count))
    per_item_low = 0.002 if has_video else 0.0005
    per_item_high = 0.015 if has_video else 0.004
    if prov in ("anthropic", "claude"):
        per_item_low = 0.004 if has_video else 0.001
        per_item_high = 0.025 if has_video else 0.008
    return f"~${count * per_item_low:.2f}-${count * per_item_high:.2f} USD ({count} lecture(s))"


def _ensure_api_key_interactive(env_var: str, provider_label: str) -> str:
    """Return API key from env, or prompt hidden via getpass in interactive TTY (never persisted)."""
    val = os.environ.get(env_var, "").strip()
    if val:
        return val
    if not sys.stdin.isatty():
        return ""
    try:
        entered = getpass.getpass(f"    Enter {provider_label} API key ({env_var}, hidden, current run only): ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    if entered:
        os.environ[env_var] = entered
    return entered


def _sync_prefs_path() -> Path:
    return DATA / "sync_prefs.json"


def _load_sync_preferences() -> dict:
    """Load saved non-secret backend preferences for scheduled syncs."""
    path = _sync_prefs_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    k: v for k, v in data.items()
                    if k in ("transcribe_backend", "match_backend", "pdf_vision_backend") and isinstance(v, str)
                }
    except Exception:
        pass
    return {}


def _save_sync_preferences(
    transcribe_backend: str | None = None,
    match_backend: str | None = None,
    pdf_vision_backend: str | None = None,
):
    """Persist only backend choice names (never API keys) for future scheduled syncs."""
    prefs = _load_sync_preferences()
    if transcribe_backend:
        prefs["transcribe_backend"] = transcribe_backend
    if match_backend:
        prefs["match_backend"] = match_backend
    if pdf_vision_backend:
        prefs["pdf_vision_backend"] = pdf_vision_backend
    try:
        _write_json(_sync_prefs_path(), prefs)
        _harden_private_path(_sync_prefs_path(), is_dir=False)
    except Exception:
        pass


class _GeminiMaxTokensError(RuntimeError):
    """Raised when Gemini hits MAX_TOKENS so a chunk can be split into smaller halves."""


def _transcript_done_marker(path: Path) -> Path:
    return path.parent / f".{path.stem}.transcript.done.json"


def _transcript_inprogress_marker(path: Path) -> Path:
    return path.parent / f".{path.stem}.transcript.inprogress"


def _is_transcript_complete(path: Path) -> bool:
    """Return True if path has a completed transcript (honoring explicit markers and legacy .txt)."""
    if _transcript_inprogress_marker(path).exists():
        return False
    done_marker = _transcript_done_marker(path)
    txt_path = path.with_suffix(".txt")
    srt_path = path.with_suffix(".srt")
    if done_marker.exists():
        return txt_path.is_file() and srt_path.is_file() and txt_path.stat().st_size > 0
    return txt_path.is_file()


def _format_srt_timestamp(seconds: float) -> str:
    sec = max(0.0, float(seconds))
    total_ms = int(round(sec * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _commit_transcript_pair(
    audio_path: Path,
    segments: list[dict],
    *,
    backend: str,
    model: str,
    recording_sha256: str,
) -> tuple[Path, Path]:
    """Atomically publish .txt, .srt, and an explicit completion marker as a finished pair."""
    txt_dest = audio_path.with_suffix(".txt")
    srt_dest = audio_path.with_suffix(".srt")
    done_marker = _transcript_done_marker(audio_path)
    inprog_marker = _transcript_inprogress_marker(audio_path)

    txt_lines: list[str] = []
    srt_entries: list[str] = []
    for idx, seg in enumerate(segments, 1):
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        start = max(0.0, float(seg.get("start", 0.0)))
        end = max(start + 0.1, float(seg.get("end", start + 1.0)))
        txt_lines.append(text)
        srt_entries.append(f"{idx}\n{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n{text}\n")

    txt_bytes = ("\n".join(txt_lines) + "\n").encode("utf-8")
    srt_bytes = ("\n".join(srt_entries) + "\n").encode("utf-8")

    # Mark in-progress while staging the two output files so indexing cannot see a half-committed pair.
    # Leave the marker in place if any write fails so partial outputs are never treated as complete.
    _replace_bytes(inprog_marker, b"committing\n")
    _replace_bytes(txt_dest, txt_bytes)
    _replace_bytes(srt_dest, srt_bytes)
    _write_json(done_marker, {
        "version": _TRANSCRIBE_CACHE_VERSION,
        "recording_sha256": recording_sha256,
        "backend": backend,
        "model": model,
        "txt_sha256": hashlib.sha256(txt_bytes).hexdigest(),
        "srt_sha256": hashlib.sha256(srt_bytes).hexdigest(),
        "segments": len(txt_lines),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    })
    inprog_marker.unlink(missing_ok=True)
    return txt_dest, srt_dest


def _resolve_transcribe_backend(backend: str | None = "auto") -> str:
    """Resolve transcription backend without ever treating API keys as implicit upload consent.

    - Explicit 'local'/'gpu'/'cpu' always overrides TOLEDO_TRANSCRIBE_BACKEND.
    - 'auto' stays local (CUDA GPU if present; no silent slow CPU hang) unless
      TOLEDO_TRANSCRIBE_BACKEND explicitly opts into 'gemini'.
    """
    choice = (backend or "auto").strip().lower()
    if choice in ("local", "gpu", "cpu", "gemini", "openai"):
        return choice
    if choice == "auto":
        env_pref = os.environ.get("TOLEDO_TRANSCRIBE_BACKEND", "").strip().lower()
        if env_pref in ("local", "gpu", "cpu", "gemini", "openai"):
            return env_pref
        return "auto"
    return "auto"


def _normalize_transcribe_language(language: str | None) -> str:
    raw = (language or "en").strip().lower()
    return raw if raw else "en"


def _transcribe_chunk_cache_dir(
    recording_sha256: str,
    *,
    backend: str,
    endpoint: str,
    model: str,
    language: str,
    chunk_seconds: int,
) -> Path:
    key_material = json.dumps({
        "version": _TRANSCRIBE_CACHE_VERSION,
        "recording_sha256": recording_sha256,
        "backend": backend,
        "endpoint": endpoint,
        "model": model,
        "language": _normalize_transcribe_language(language),
        "audio_format": "mp3_16k_mono_32k",
        "chunk_seconds": int(chunk_seconds),
    }, sort_keys=True)
    digest = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:24]
    root = DATA / "transcribe_cache"
    root.mkdir(parents=True, exist_ok=True)
    _harden_private_path(root, is_dir=True)
    job_dir = root / digest
    job_dir.mkdir(parents=True, exist_ok=True)
    _harden_private_path(job_dir, is_dir=True)
    return job_dir


def _split_audio_chunks(
    audio_path: Path,
    work_dir: Path,
    chunk_seconds: int = _GEMINI_TRANSCRIBE_CHUNK_SECONDS,
    *,
    duration_sec: float | None = None,
) -> list[dict]:
    """Split audio/video into uniform mono 16 kHz MP3 chunks via ffmpeg for resumable cloud upload."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to split lecture audio for cloud transcription.")
    duration = float(duration_sec) if duration_sec is not None else _probe_video_duration(audio_path)
    if not duration or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Could not determine audio duration for {audio_path.name}.")

    chunks: list[dict] = []
    offset = 0.0
    idx = 0
    while offset < duration - 0.25:
        seg_dur = min(float(chunk_seconds), duration - offset)
        out_mp3 = work_dir / f"chunk_{idx:04d}.mp3"
        proc = subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-y",
                "-v", "error",
                "-ss", f"{offset:.3f}",
                "-t", f"{seg_dur:.3f}",
                "-i", str(audio_path),
                "-vn",
                "-ac", "1",
                "-ar", "16000",
                "-b:a", "32k",
                str(out_mp3),
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0 or not out_mp3.is_file() or out_mp3.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg failed splitting chunk {idx} of {audio_path.name}.")
        chunks.append({
            "index": idx,
            "offset_sec": round(offset, 3),
            "duration_sec": round(seg_dur, 3),
            "path": out_mp3,
        })
        offset += chunk_seconds
        idx += 1
    return chunks


def _is_gemini_transcribe_model(model: str) -> bool:
    return "transcribe" in (model or "").strip().lower()


def _gemini_language_codes(language: str | None) -> list[str]:
    raw = (language or "").strip()
    if not raw or raw.lower() == "auto":
        return []
    low = raw.lower()
    if low in _GEMINI_LANG_TO_BCP47:
        return [_GEMINI_LANG_TO_BCP47[low]]
    if "-" in raw:
        return [raw]
    return []


def _parse_offset_seconds(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        fval = float(value)
        return fval if math.isfinite(fval) else None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("s"):
        s = s[:-1].strip()
    try:
        fval = float(s)
        return fval if math.isfinite(fval) else None
    except ValueError:
        return None


def _post_gemini_interaction(
    payload_obj: dict,
    *,
    api_key: str,
    api_version: str = "v1",
    timeout: int = 180,
) -> dict:
    """POST to Gemini Interactions API with header-only auth and bounded 429/503 retries."""
    url = _GEMINI_INTERACTIONS_V1BETA_URL if api_version == "v1beta" else _GEMINI_INTERACTIONS_V1_URL
    payload = json.dumps(payload_obj).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "Api-Revision": _GEMINI_INTERACTIONS_API_REVISION,
        },
    )
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < 2:
                wait_sec = 2.0 * (2 ** attempt)
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after:
                    try:
                        wait_sec = min(30.0, max(1.0, float(retry_after)))
                    except ValueError:
                        pass
                time.sleep(wait_sec)
                continue
            raise
    if not isinstance(data, dict):
        raise RuntimeError("Gemini Interactions API returned an invalid response.")
    status = str(data.get("status", "")).lower()
    if status == "incomplete":
        raise _GeminiMaxTokensError("Gemini interaction output was incomplete (token limit); retry with shorter chunks.")
    if status in ("failed", "cancelled"):
        err = data.get("error") or status
        raise RuntimeError(f"Gemini interaction {status}: {_redact_secrets(str(err))}")
    return data


def _extract_interaction_text(data: dict) -> str:
    """Collect text parts from model_output steps in an Interactions API response."""
    chunks: list[str] = []
    for step in data.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for item in step.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                chunks.append(str(item["text"]))
    return "\n".join(chunks)


def _extract_interaction_word_annotations(data: dict) -> list[dict]:
    """Extract word_info timestamp annotations from a gemini-3.5-transcribe Interactions response."""
    words: list[dict] = []
    for step in data.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for item in step.get("content") or []:
            if not isinstance(item, dict):
                continue
            for ann in item.get("annotations") or []:
                if not isinstance(ann, dict) or ann.get("type") != "word_info":
                    continue
                text = str(ann.get("text", "")).strip()
                if not text:
                    continue
                words.append({
                    "text": text,
                    "start": _parse_offset_seconds(ann.get("start_offset")),
                    "end": _parse_offset_seconds(ann.get("end_offset")),
                })
    return words


def _clamp_transcription_segments(raw_segments: list[dict], max_duration_sec: float) -> list[dict]:
    """Clamp segment timestamps to finite, strictly monotonic bounds within [0, max_duration_sec]."""
    max_dur = max(0.0, float(max_duration_sec))
    cleaned: list[dict] = []
    last_end = 0.0
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        try:
            start = float(item.get("start", last_end))
            end = float(item.get("end", start + 1.0))
        except (TypeError, ValueError):
            start = last_end
            end = start + 1.0
        if not math.isfinite(start):
            start = last_end
        if not math.isfinite(end):
            end = start + 1.0
        start = max(last_end, min(max_dur, max(0.0, start)))
        if start >= max_dur and max_dur >= 0.1:
            start = max(last_end, max_dur - 0.1)
        min_end = min(max_dur, start + 0.1) if max_dur > start else start
        end = max(min_end, min(max_dur, end))
        last_end = end
        cleaned.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return cleaned


def _group_words_into_segments(
    words: list[dict],
    *,
    max_duration_sec: float,
    max_cue_words: int = 18,
    max_cue_seconds: float = 10.0,
    pause_gap_sec: float = 1.2,
) -> list[dict]:
    """Group word-level timestamps into natural subtitle/transcript cues."""
    raw_segments: list[dict] = []
    buf_words: list[str] = []
    cue_start: float | None = None
    cue_end: float = 0.0

    def _flush():
        nonlocal buf_words, cue_start
        if not buf_words:
            return
        start_val = cue_start if cue_start is not None else cue_end
        end_val = max(start_val + 0.1, cue_end)
        raw_segments.append({
            "start": start_val,
            "end": end_val,
            "text": " ".join(buf_words),
        })
        buf_words = []
        cue_start = None

    for w in words:
        w_text = w["text"]
        w_start = w["start"] if w["start"] is not None else cue_end
        w_end = w["end"] if w["end"] is not None else (w_start + 0.25)

        if buf_words and (w_start - cue_end) >= pause_gap_sec:
            _flush()

        if cue_start is None:
            cue_start = w_start
        buf_words.append(w_text)
        cue_end = max(cue_end, w_end)

        ends_sentence = w_text.endswith((".", "?", "!"))
        if (
            ends_sentence
            or len(buf_words) >= max_cue_words
            or (cue_end - cue_start) >= max_cue_seconds
        ):
            _flush()

    _flush()
    return _clamp_transcription_segments(raw_segments, max_duration_sec)


def _call_gemini_audio_chunk(
    chunk_bytes: bytes,
    *,
    model: str,
    api_key: str,
    language: str,
    chunk_duration_sec: float,
) -> list[dict]:
    """Send a single MP3 audio chunk to Gemini Interactions API via header auth and return timestamped segments."""
    if len(chunk_bytes) > _GEMINI_TRANSCRIBE_MAX_INLINE_BYTES:
        raise RuntimeError(
            f"Audio chunk ({len(chunk_bytes)} bytes) exceeds inline safety limit ({_GEMINI_TRANSCRIBE_MAX_INLINE_BYTES} bytes)."
        )
    audio_b64 = base64.b64encode(chunk_bytes).decode("ascii")

    if _is_gemini_transcribe_model(model):
        transcription_cfg: dict = {
            "mode": {
                "type": "verbatim",
                "timestamp_granularities": ["word"],
            }
        }
        lang_codes = _gemini_language_codes(language)
        if lang_codes:
            transcription_cfg["language_codes"] = lang_codes
        payload_obj = {
            "model": model,
            "store": False,
            "input": [{
                "type": "audio",
                "data": audio_b64,
                "mime_type": "audio/mp3",
            }],
            "generation_config": {
                "transcription_config": transcription_cfg,
            },
        }
        data = _post_gemini_interaction(payload_obj, api_key=api_key, api_version="v1beta", timeout=180)
        words = _extract_interaction_word_annotations(data)
        if words:
            cleaned = _group_words_into_segments(words, max_duration_sec=chunk_duration_sec)
        else:
            raw_text = _extract_interaction_text(data).strip()
            if not raw_text:
                raise RuntimeError("Gemini returned empty transcription output.")
            cleaned = _clamp_transcription_segments(
                [{"start": 0.0, "end": max(0.1, float(chunk_duration_sec)), "text": raw_text}],
                chunk_duration_sec,
            )
        if not cleaned:
            raise RuntimeError("Gemini transcription contained no non-empty segments.")
        return cleaned

    lang_hint = _normalize_transcribe_language(language)
    prompt = (
        f"Transcribe this university lecture audio chunk verbatim (primary language: {lang_hint}). "
        f"Chunk duration is {chunk_duration_sec:.1f} seconds. "
        "Do not summarize or omit spoken content. Return timestamped segments using seconds "
        "relative to the start of this chunk (0.0 to chunk duration)."
    )
    payload_obj = {
        "model": model,
        "store": False,
        "input": [
            {"type": "text", "text": prompt},
            {
                "type": "audio",
                "data": audio_b64,
                "mime_type": "audio/mp3",
            },
        ],
        "generation_config": {
            "thinking_level": "low",
        },
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": {
                "type": "object",
                "properties": {
                    "segments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start": {"type": "number"},
                                "end": {"type": "number"},
                                "text": {"type": "string"},
                            },
                            "required": ["start", "end", "text"],
                        },
                    }
                },
                "required": ["segments"],
            },
        },
    }
    data = _post_gemini_interaction(payload_obj, api_key=api_key, api_version="v1", timeout=180)
    raw_text = _extract_interaction_text(data)
    parsed = _parse_json_object(raw_text)
    raw_segments = parsed.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise RuntimeError("Gemini returned empty or invalid transcription segments.")
    cleaned = _clamp_transcription_segments(raw_segments, chunk_duration_sec)
    if not cleaned:
        raise RuntimeError("Gemini transcription contained no non-empty segments.")
    return cleaned


def _call_openai_audio_chunk(
    chunk_bytes: bytes,
    model: str,
    api_key: str,
    language: str,
    chunk_duration_sec: float,
) -> list[dict]:
    """Send one mono MP3 chunk to OpenAI /v1/audio/transcriptions and return monotonic segments."""
    boundary = f"----ToledoDLBoundary{int(time.time() * 1000)}"
    parts: list[bytes] = []
    for name, value in (
        ("model", model),
        ("language", language),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "segment"),
    ):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"file\"; filename=\"chunk.mp3\"\r\n"
            "Content-Type: audio/mpeg\r\n\r\n"
        ).encode("utf-8")
        + chunk_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    req = urllib.request.Request(
        f"{base}/audio/transcriptions",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {api_key}",
        },
    )
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < 2:
                time.sleep(2.0 * (2 ** attempt))
                continue
            raise
    raw_segments = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(raw_segments, list) or not raw_segments:
        text_only = str((data or {}).get("text", "")).strip() if isinstance(data, dict) else ""
        if not text_only:
            raise RuntimeError("OpenAI returned empty or invalid transcription segments.")
        raw_segments = [{"start": 0.0, "end": max(1.0, float(chunk_duration_sec)), "text": text_only}]
    max_dur = max(0.0, float(chunk_duration_sec))
    cleaned: list[dict] = []
    last_end = 0.0
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        start = float(item.get("start", last_end))
        end = float(item.get("end", start + 1.0))
        start = max(last_end, min(max_dur, max(0.0, start)))
        end = max(min(max_dur, start + 0.1) if max_dur > start else start, min(max_dur, end))
        last_end = end
        cleaned.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    if not cleaned:
        raise RuntimeError("OpenAI transcription contained no non-empty segments.")
    return cleaned


def _extract_audio_subchunk(source_path: Path, out_mp3: Path, offset_sec: float, duration_sec: float) -> bytes:
    """Extract a mono 16 kHz MP3 sub-chunk from source_path via ffmpeg (fails closed if ffmpeg is unavailable)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to split lecture audio sub-chunks for cloud transcription.")
    proc = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-y",
            "-v", "error",
            "-ss", f"{offset_sec:.3f}",
            "-t", f"{duration_sec:.3f}",
            "-i", str(source_path),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "32k",
            str(out_mp3),
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0 or not out_mp3.is_file() or out_mp3.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg failed splitting sub-chunk at {offset_sec:.1f}s of {source_path.name}.")
    return out_mp3.read_bytes()


def _run_gemini_chunk_or_split(
    *,
    chunk_bytes: bytes,
    source_path: Path,
    work_dir: Path,
    cache_dir: Path,
    chunk_tag: str,
    rel_offset: float,
    dur: float,
    model: str,
    api_key: str,
    language: str,
    depth: int = 0,
    provider: str = "gemini",
) -> list[dict]:
    """Transcribe a chunk, or on MAX_TOKENS split it in half (bounded) and resume per sub-chunk."""
    sub_cache = cache_dir / f"{chunk_tag}.json"
    if depth > 0 and sub_cache.exists():
        try:
            cached = json.loads(sub_cache.read_text(encoding="utf-8"))
            if (
                isinstance(cached, dict)
                and cached.get("duration_sec") == round(dur, 3)
                and isinstance(cached.get("segments"), list)
                and cached["segments"]
            ):
                return cached["segments"]
        except Exception:
            pass

    try:
        call_fn = _call_openai_audio_chunk if provider == "openai" else _call_gemini_audio_chunk
        segs = call_fn(
            chunk_bytes,
            model=model,
            api_key=api_key,
            language=language,
            chunk_duration_sec=dur,
        )
        if depth > 0:
            _write_json(sub_cache, {
                "version": _TRANSCRIBE_CACHE_VERSION,
                "duration_sec": round(dur, 3),
                "segments": segs,
            })
            _harden_private_path(sub_cache, is_dir=False)
        return segs
    except _GeminiMaxTokensError:
        if depth >= _MAX_GEMINI_SPLIT_DEPTH or dur <= _MIN_GEMINI_SPLIT_SECONDS:
            raise
        half = round(dur / 2.0, 3)
        rem = round(dur - half, 3)
        if half < 1.0 or rem < 1.0:
            raise
        log(f"  ↻ [Transcribe/Gemini] MAX_TOKENS on {chunk_tag} ({dur:.0f}s); splitting into {half:.0f}s + {rem:.0f}s...")
        combined: list[dict] = []
        last_end = 0.0
        for part_idx, (part_off, part_dur) in enumerate(((0.0, half), (half, rem))):
            part_tag = f"{chunk_tag}_s{depth + 1}_{part_idx}"
            part_mp3 = work_dir / f"{part_tag}.mp3"
            part_bytes = _extract_audio_subchunk(source_path, part_mp3, rel_offset + part_off, part_dur)
            part_segs = _run_gemini_chunk_or_split(
                chunk_bytes=part_bytes,
                source_path=source_path,
                work_dir=work_dir,
                cache_dir=cache_dir,
                chunk_tag=part_tag,
                rel_offset=rel_offset + part_off,
                dur=part_dur,
                model=model,
                api_key=api_key,
                language=language,
                depth=depth + 1,
                provider=provider,
            )
            for seg in part_segs:
                s = max(last_end, min(dur, part_off + float(seg["start"])))
                e = max(s, min(dur, part_off + float(seg["end"])))
                last_end = e
                combined.append({"start": round(s, 3), "end": round(e, 3), "text": seg["text"]})
        if not combined:
            raise RuntimeError("Gemini split transcription contained no non-empty segments.")
        return combined


def _transcribe_audio_via_gemini(
    audio_path: Path,
    *,
    model: str = _GEMINI_TRANSCRIBE_DEFAULT_MODEL,
    language: str = "en",
    cloud_approval: dict | None = None,
    chunk_seconds: int = _GEMINI_TRANSCRIBE_CHUNK_SECONDS,
    course_dir: Path | None = None,
    provider: str = "gemini",
) -> bool:
    """Resumable chunked cloud transcription via Gemini or OpenAI API with strict explicit approval."""
    prov = (provider or "gemini").lower()
    default_model = _OPENAI_TRANSCRIBE_DEFAULT_MODEL if prov == "openai" else _GEMINI_TRANSCRIBE_DEFAULT_MODEL
    selected_model = default_model if not model or model == "auto" else model
    probed_dur = None
    if isinstance(cloud_approval, dict) and (
        cloud_approval.get("max_duration_sec") is not None
        or cloud_approval.get("max_file_durations") is not None
    ):
        probed_dur = _probe_video_duration(audio_path)

    if not _is_cloud_approved(
        cloud_approval,
        "transcribe",
        prov,
        audio_path,
        model=selected_model,
        data_types=("audio_chunks",),
        course_dir=course_dir,
        duration_sec=probed_dur,
    ):
        log(f"  ✗ [Transcribe] Refused cloud upload for {audio_path.name}: explicit approval missing or expired.")
        return False

    env_var = "OPENAI_API_KEY" if prov == "openai" else "GEMINI_API_KEY"
    api_key = os.environ.get(env_var, "").strip()
    if not api_key:
        log(f"  ✗ [Transcribe] {env_var} is not set.")
        return False

    if _is_transcript_complete(audio_path):
        log(f"  [Transcribe] Skipping (already transcribed): {audio_path.with_suffix('.txt').name}")
        _maybe_rename_vague_lecture(audio_path)
        return True

    if prov == "openai":
        base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        endpoint = f"{base}/audio/transcriptions"
    else:
        endpoint = (
            _GEMINI_INTERACTIONS_V1BETA_URL
            if _is_gemini_transcribe_model(selected_model)
            else _GEMINI_INTERACTIONS_V1_URL
        )
    recording_sha = _file_sha256(audio_path)
    if not recording_sha:
        log(f"  ✗ [Transcribe] Could not hash {audio_path.name}.")
        return False

    cache_dir = _transcribe_chunk_cache_dir(
        recording_sha,
        backend=prov,
        endpoint=endpoint,
        model=selected_model,
        language=language,
        chunk_seconds=chunk_seconds,
    )

    start_time = time.time()
    all_segments: list[dict] = []
    global_last_end = 0.0
    with tempfile.TemporaryDirectory(prefix="toledo_chunks_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        split_kwargs = {"chunk_seconds": chunk_seconds}
        if probed_dur is not None and "duration_sec" in inspect.signature(_split_audio_chunks).parameters:
            split_kwargs["duration_sec"] = probed_dur
        chunks = _split_audio_chunks(audio_path, tmp_dir, **split_kwargs)
        total_chunks = len(chunks)
        for ch in chunks:
            idx = ch["index"]
            offset = float(ch["offset_sec"])
            dur = float(ch["duration_sec"])
            chunk_cache_file = cache_dir / f"chunk_{idx:04d}.json"
            chunk_segments = None

            if chunk_cache_file.exists():
                try:
                    cached = json.loads(chunk_cache_file.read_text(encoding="utf-8"))
                    if (
                        isinstance(cached, dict)
                        and cached.get("offset_sec") == offset
                        and cached.get("duration_sec") == dur
                        and isinstance(cached.get("segments"), list)
                        and cached["segments"]
                    ):
                        chunk_segments = cached["segments"]
                        log(f"  ↻ [Transcribe/Gemini] Reusing cached chunk {idx + 1}/{total_chunks} for {audio_path.name}")
                except Exception:
                    chunk_segments = None

            if chunk_segments is None:
                log(f"  ☁️ [Transcribe/{prov.title()}] Uploading chunk {idx + 1}/{total_chunks} of {audio_path.name} ({selected_model})...")
                chunk_path = Path(ch["path"])
                chunk_bytes = chunk_path.read_bytes()
                chunk_segments = _run_gemini_chunk_or_split(
                    chunk_bytes=chunk_bytes,
                    source_path=audio_path,
                    work_dir=tmp_dir,
                    cache_dir=cache_dir,
                    chunk_tag=f"chunk_{idx:04d}",
                    rel_offset=offset,
                    dur=dur,
                    model=selected_model,
                    api_key=api_key,
                    language=language,
                    depth=0,
                    provider=prov,
                )
                _write_json(chunk_cache_file, {
                    "version": _TRANSCRIBE_CACHE_VERSION,
                    "index": idx,
                    "offset_sec": offset,
                    "duration_sec": dur,
                    "segments": chunk_segments,
                })
                _harden_private_path(chunk_cache_file, is_dir=False)

            chunk_cap = offset + dur
            for seg in chunk_segments:
                g_start = max(global_last_end, min(chunk_cap, offset + float(seg["start"])))
                g_end = max(g_start, min(chunk_cap, offset + float(seg["end"])))
                global_last_end = g_end
                all_segments.append({
                    "start": round(g_start, 3),
                    "end": round(g_end, 3),
                    "text": seg["text"],
                })

    txt_dest, srt_dest = _commit_transcript_pair(
        audio_path,
        all_segments,
        backend=prov,
        model=selected_model,
        recording_sha256=recording_sha,
    )
    # Prune completed chunk cache after the final pair and completion marker are committed.
    shutil.rmtree(cache_dir, ignore_errors=True)

    elapsed = time.time() - start_time
    log(f"  ✓ [Transcribe/Gemini] Done in {elapsed:.1f}s!")
    log(f"    Saved transcript: {txt_dest.name}")
    log(f"    Saved subtitles:  {srt_dest.name}")

    # Post-transcription rename and strictly local slide matching (separate opt-in!)
    txt_dest = _maybe_rename_vague_lecture(audio_path).with_suffix(".txt")
    try:
        course_dir = txt_dest.parent
        while course_dir.parent != OUT and course_dir != OUT:
            course_dir = course_dir.parent
        if course_dir.parent == OUT:
            match_transcript_with_pdfs(txt_dest, course_dir)
    except Exception as e:
        log(f"  [Auto-Process] Warning: Could not auto-process transcript: {e}")

    return True


# ---------------------------------------------------------------- Checkpoint Extractor (Ollama)
MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _academic_year_for_month(month: int, today: date | None = None) -> int:
    """Returns the calendar year corresponding to an academic-year month (Aug-Jul)."""
    today = today or date.today()
    start_yr = today.year if today.month >= 8 else today.year - 1
    return start_yr if month >= 8 else start_yr + 1


def transcript_sort_key(txt_path: Path):
    """Returns a chronological sort tuple for a transcript filename."""
    name = txt_path.stem
    # 1. Check for (MM-DD HHhMM) e.g. (10-28 12h18)
    m_paren = re.search(r"\((\d{2})-(\d{2})\s+(\d{2})h(\d{2})\)", name)
    if m_paren:
        mm, dd, hh, mi = map(int, m_paren.groups())
        yr = _academic_year_for_month(mm)
        return (1, f"{yr:04d}-{mm:02d}-{dd:02d} {hh:02d}:{mi:02d}", 0, name)

    # 2. Check for "DD Month" e.g. "22 October" or "8 October"
    m_dmon = re.search(
        r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b",
        name, re.I
    )
    if m_dmon:
        dd = int(m_dmon.group(1))
        mm = MONTH_MAP.get(m_dmon.group(2).lower(), 9)
        yr = _academic_year_for_month(mm)
        # Prefer "Lecture" over "Exercises" on the same date so main lecture is primary
        is_lec = 1 if "lecture" in name.lower().split("-")[-1] or "exercises" not in name.lower() else 0
        return (1, f"{yr:04d}-{mm:02d}-{dd:02d} {is_lec:02d}:00", is_lec, name)

    # 3. Check for DD-MM e.g. "27-10"
    m_ddmm = re.search(r"\b(\d{1,2})-(\d{2})\b", name)
    if m_ddmm:
        v1, v2 = int(m_ddmm.group(1)), int(m_ddmm.group(2))
        if 1 <= v2 <= 12 and 1 <= v1 <= 31:
            yr = _academic_year_for_month(v2)
            return (1, f"{yr:04d}-{v2:02d}-{v1:02d} 00:00", 0, name)

    # 4. Fallback to Lecture NN number
    m_lec = re.search(r"Lecture\s*(\d+)", name, re.I)
    lec_num = int(m_lec.group(1)) if m_lec else 0
    return (0, f"{lec_num:04d}", lec_num, name)


def get_pdf_chapter_title(pdf_path: Path):
    """Extracts clean human chapter title directly from the course PDF slides."""
    if not pdf_path or not pdf_path.exists():
        return ""
    fn = pdf_path.stem

    slide_title = ""
    if pypdf:
        try:
            reader = pypdf.PdfReader(str(pdf_path))
            raw = reader.pages[0].extract_text()
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            clean_lines = []
            for line in lines:
                if re.match(r"^\d+\s*$", line):
                    continue
                if re.search(r"(prof|dr\.|kuleuven|b-kul|segers|alonso|neyens|meulders)", line, re.I):
                    continue
                if re.match(r"^multivariate statistics$", line, re.I):
                    continue
                if re.match(r"^linear models", line, re.I):
                    continue
                if re.match(r"^fundamental concepts", line, re.I):
                    continue
                clean_lines.append(line)
            if clean_lines:
                slide_title = clean_lines[0]
                if len(clean_lines) > 1 and len(slide_title) < 18:
                    slide_title += " - " + clean_lines[1]
        except Exception:
            pass

    # Extract chapter number
    m_ch = re.search(r"(?:chapter|ch\.?)\s*(\d+)", fn, re.I) or re.search(r"^(\d+)[-_]", fn)
    ch_num = m_ch.group(1) if m_ch else None

    # Fallback to cleaned filename
    clean_fn = re.sub(r"(?:chapter|\bch\b)[_\s]*\d+[-_\s]*", "", fn, flags=re.I)
    clean_fn = re.sub(r"^\d+[-_]", "", clean_fn)
    clean_fn = re.sub(r"[-_](?:handouts|slides|toledo.*)$", "", clean_fn, flags=re.I)
    clean_fn = clean_fn.replace("_", " ").strip().title()

    title_part = slide_title if (slide_title and len(slide_title) > 3) else clean_fn
    title_part = re.sub(r"^chapter\s*\d+[\.\:\s-]*", "", title_part, flags=re.I).strip()

    if ch_num:
        return f"Chapter {ch_num}: {title_part}"
    return title_part


def locate_slide_in_pdf(pdf_path: Path, last_topic: str, stopped_at: str):
    """Pinpoints the specific slide/page number and position where the professor stopped."""
    if not pdf_path or not pdf_path.exists() or not pypdf:
        return None
    try:
        reader = pypdf.PdfReader(str(pdf_path))
    except Exception:
        return None
    total = len(reader.pages)
    if total == 0:
        return None

    words = re.findall(r"\b[A-Za-z]{4,}\b", f"{last_topic} {stopped_at}".lower())
    ignore = {
        "professor", "lecture", "discussed", "concluded", "explained", "covered",
        "mentioned", "session", "using", "class", "example", "topic", "section",
        "stated", "would", "about", "which", "their", "there", "these", "after"
    }
    keywords = [w for w in words if w not in ignore]
    if not keywords:
        return None

    page_texts = [(p.extract_text() or "").lower() for p in reader.pages]

    # Compute document frequency across pages
    df = {}
    for kw in set(keywords):
        df[kw] = sum(1 for txt in page_texts if kw in txt)

    scores = []
    for idx, txt in enumerate(page_texts, 1):
        if not txt:
            continue
        score = 0.0
        for kw in keywords:
            if kw in txt:
                # IDF weighting: heavily rewards specific topic words while discounting ubiquitous words
                idf = math.log((total + 1.0) / (df[kw] + 1.0)) + 1.0
                score += txt.count(kw) * idf
        if score > 0:
            scores.append((score, idx))

    if not scores:
        return None

    scores.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_page = scores[0][1]
    pct = best_page / total
    if pct <= 0.25:
        pos = "Beginning"
    elif pct >= 0.80:
        pos = "Near the end"
    else:
        pos = "Midway"

    slide_title = ""
    try:
        lines = [line.strip() for line in reader.pages[best_page - 1].extract_text().splitlines() if line.strip()]
        if lines:
            # Clean up leading slide numbering or symbols if present
            slide_title = re.sub(r"^(slide|\d+[\.\:\-]?)\s*", "", lines[0], flags=re.I).strip()[:60]
    except Exception:
        pass

    text_desc = f"Slide {best_page} of {total} ({pos} of {pdf_path.stem})"
    if slide_title:
        text_desc = f"Slide {best_page} of {total}: \"{slide_title}\" ({pos})"

    return {
        "page": best_page,
        "total_pages": total,
        "position": pos,
        "slide_title": slide_title,
        "pdf_name": pdf_path.name,
        "text": text_desc,
        "score": scores[0][0],
    }


def extract_visual_checkpoint_from_video(video_path: Path, pdf_path: Path = None, course_dir: Path = None, vision_model="minicpm-v:latest"):
    """Extracts ground-truth slide and topic directly from ending video frames of an MP4 lecture using local Ollama vision."""
    if not video_path.exists():
        return None

    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=10
        )
        duration = float(res.stdout.strip())
    except Exception:
        return None

    if duration < 60:
        return None

    if not course_dir:
        p = video_path.parent
        course_dir = p.parent if p.name in ("ANOVA Lectures", "Regression Analysis") else p

    candidate_pdfs = []
    if pdf_path and pdf_path.exists():
        candidate_pdfs.append(pdf_path)
    elif course_dir and (course_dir / "PDFs").exists():
        all_pdfs = list((course_dir / "PDFs").glob("*.pdf"))
        sub_name = video_path.parent.name.lower()
        if "anova" in sub_name:
            all_pdfs = [p for p in all_pdfs if "anova" in p.name.lower()]
        elif "regression" in sub_name:
            all_pdfs = [p for p in all_pdfs if "anova" not in p.name.lower()]
        candidate_pdfs = all_pdfs

    timestamps = [max(10, duration - 40), max(10, duration - 100), max(10, duration - 200)]
    tmp_dir = Path(tempfile.gettempdir()) / "toledo_frames"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for ts in timestamps:
        frame_file = tmp_dir / f"{video_path.stem}_{int(ts)}.jpg"
        cmd = ["ffmpeg", "-y", "-ss", str(ts), "-i", str(video_path), "-vframes", "1", "-q:v", "2", str(frame_file)]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not frame_file.exists():
            continue

        try:
            b64_img = base64.b64encode(frame_file.read_bytes()).decode("utf-8")
        except Exception:
            continue

        prompt = (
            "Look at this frame from a university lecture video screen recording. "
            "Return JSON ONLY with these keys:\n"
            "{\n"
            "  \"is_slide\": true or false,\n"
            "  \"slide_title\": \"<exact title heading of the slide shown, or empty>\",\n"
            "  \"slide_number\": \"<visible slide number/page or null>\",\n"
            "  \"summary\": \"<1 concise sentence describing the formula, theorem, or topic shown>\"\n"
            "}"
        )

        payload = json.dumps({
            "model": vision_model,
            "prompt": prompt,
            "images": [b64_img],
            "stream": False,
            "format": "json"
        }).encode("utf-8")

        try:
            req = urllib.request.Request("http://localhost:11434/api/generate", data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=35) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                vision_out = json.loads(res_data.get("response", "{}"))

            slide_title = (vision_out.get("slide_title") or "").strip()
            summary = (vision_out.get("summary") or "").strip()
            slide_num_raw = vision_out.get("slide_number")
            ts_min = int(ts // 60)
            ts_sec = int(ts % 60)

            if slide_title and candidate_pdfs:
                best_match = None
                highest_score = 0.0
                for c_pdf in candidate_pdfs:
                    sl = locate_slide_in_pdf(c_pdf, slide_title, summary)
                    if sl and sl.get("page") and sl.get("score", 0) > highest_score:
                        highest_score = sl.get("score", 0)
                        best_match = sl

                if best_match and highest_score > 3.0:
                    return {
                        "page": best_match["page"],
                        "total_pages": best_match["total_pages"],
                        "position": best_match["position"],
                        "slide_title": best_match["slide_title"] or slide_title,
                        "pdf_name": best_match["pdf_name"],
                        "text": best_match["text"],
                        "visual_verified": True,
                        "video_timestamp": f"{ts_min:02d}:{ts_sec:02d}",
                        "visual_summary": summary,
                    }

            # If no direct PDF match found, return the visual extraction directly!
            if slide_title or summary:
                page_val = None
                if slide_num_raw:
                    try:
                        m = re.search(r"\d+", str(slide_num_raw))
                        if m:
                            page_val = int(m.group(0))
                    except Exception:
                        pass
                return {
                    "page": page_val,
                    "total_pages": None,
                    "position": f"Slide {page_val}" if page_val else "End of video",
                    "slide_title": slide_title,
                    "pdf_name": candidate_pdfs[0].name if candidate_pdfs else "",
                    "text": f"Slide {page_val}: \"{slide_title}\"" if page_val else f"Slide \"{slide_title}\"",
                    "visual_verified": True,
                    "video_timestamp": f"{ts_min:02d}:{ts_sec:02d}",
                    "visual_summary": summary,
                }
        except Exception:
            pass

    return None


def extract_checkpoint_for_transcript(txt_path: Path, course_dir: Path, llm_model="phi4:14b", force=False):
    """Extracts 'Where the Professor Left Off' from the last ~15 minutes of a transcript using local Ollama."""
    cp_file = course_dir / ".checkpoints.json"
    cp_data = {"updated_at": "", "latest": [], "entries": {}}
    if cp_file.exists():
        try:
            cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
            if "entries" not in cp_data:
                cp_data["entries"] = {}
        except Exception:
            pass

    rel_key = str(txt_path.relative_to(course_dir)).replace("\\", "/")
    mtime = int(txt_path.stat().st_mtime)

    # Check slide locator if already extracted
    if not force and rel_key in cp_data["entries"]:
        existing = cp_data["entries"][rel_key]
        if existing.get("mtime") == mtime and existing.get("last_topic"):
            # Update chapter and slide locator if missing or if chapter was set to "Lecture XX"
            pdf_map_file = course_dir / ".pdf_mapping.json"
            needs_save = False
            if pdf_map_file.exists():
                try:
                    pmap = json.loads(pdf_map_file.read_text(encoding="utf-8"))
                    m_entry = pmap.get(rel_key) or pmap.get(txt_path.stem)
                    if m_entry and m_entry.get("matched_pdfs"):
                        pdf_p = course_dir / "PDFs" / m_entry["matched_pdfs"][0]
                        pdf_ch = get_pdf_chapter_title(pdf_p)
                        if pdf_ch and (not existing.get("chapter") or existing.get("chapter", "").lower().startswith("lecture")):
                            existing["chapter"] = pdf_ch
                            needs_save = True
                        if not existing.get("slide_estimate"):
                            sl = locate_slide_in_pdf(pdf_p, existing.get("last_topic", ""), existing.get("stopped_at", ""))
                            if sl:
                                existing["slide_estimate"] = sl["text"]
                                existing["slide_page"] = sl["page"]
                                existing["matched_pdf"] = sl["pdf_name"]
                                needs_save = True
                except Exception:
                    pass
            if needs_save:
                cp_data["entries"][rel_key] = existing
                cp_file.write_text(json.dumps(cp_data, indent=2, ensure_ascii=False), encoding="utf-8")
            return existing

    text = txt_path.read_text(encoding="utf-8", errors="ignore").strip()
    words = text.split()
    if len(words) < 30:
        return None

    tail_words = words[-1500:]
    tail_text = " ".join(tail_words)
    module_name = txt_path.parent.name if txt_path.parent != course_dir else "Main"

    log(f"  🧠 [Checkpoint] Analyzing end of '{rel_key}' with local AI ({llm_model})...")
    prompt = f"""You are analyzing the final ~15 minutes of a university lecture transcript.
Course: {course_dir.name} ({module_name})
Lecture file: {txt_path.stem}

Transcript excerpt (end of lecture):
\"\"\"
{tail_text}
\"\"\"

Extract concisely where the professor left off. Fix any obvious speech-to-text phonetic typos (for example "bone for only" -> "Bonferroni", "trust get a city" -> "heteroscedasticity").
Respond ONLY in valid JSON with these exact 4 keys:
- "chapter": (the specific chapter or section number/title if mentioned in the transcript or lecture title, e.g. "Chapter 4: Convergence of Random Variables", or null if not mentioned)
- "last_topic": (1 concise sentence: the main concept/topic taught in the final section of this lecture)
- "stopped_at": (1-2 concise sentences: the exact detail, formula, example, or exercise where the professor stopped)
- "next_up": (1 concise sentence: what the professor announced for the next session, or "Continue with the next section of the course material" if not explicitly stated)
"""

    payload = json.dumps({
        "model": llm_model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.1}
    }).encode("utf-8")

    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
        parsed = json.loads(raw.get("response", "{}"))

    last_top = parsed.get("last_topic", "").strip()
    stop_at = parsed.get("stopped_at", "").strip()

    # Resolve matched PDF & Chapter title directly from the PDF slides
    slide_info = None
    pdf_chapter = ""
    pdf_map_file = course_dir / ".pdf_mapping.json"
    matched_pdf_name = ""
    visual_cp = None
    if pdf_map_file.exists():
        try:
            pmap = json.loads(pdf_map_file.read_text(encoding="utf-8"))
            m_entry = pmap.get(rel_key) or pmap.get(txt_path.stem)
            if m_entry and m_entry.get("matched_pdfs"):
                matched_pdf_name = m_entry["matched_pdfs"][0]
                pdf_p = course_dir / "PDFs" / matched_pdf_name
                pdf_chapter = get_pdf_chapter_title(pdf_p)

                # Check if matching MP4 video exists for visual verification
                mp4_cand = txt_path.with_suffix(".mp4")
                if not mp4_cand.exists():
                    matches = list(txt_path.parent.glob(f"{txt_path.stem}.mp4"))
                    if matches:
                        mp4_cand = matches[0]

                if mp4_cand.exists() and pdf_p.exists():
                    log(f"  👁️ [Video Vision] Inspecting ending frames of '{mp4_cand.name}' with vision AI (minicpm-v)...")
                    visual_cp = extract_visual_checkpoint_from_video(mp4_cand, pdf_p)
                    if visual_cp:
                        slide_info = visual_cp
                        log(f"  ✓ [Video Vision] Exact slide confirmed from MP4: Slide {visual_cp['page']} of {visual_cp['total_pages']} (at {visual_cp['video_timestamp']})")

                if not slide_info:
                    slide_info = locate_slide_in_pdf(pdf_p, last_top, stop_at)
        except Exception:
            pass

    # Never allow "Lecture XX" as chapter - track chapters strictly by PDF
    chapter_val = pdf_chapter
    if not chapter_val:
        cand = (parsed.get("chapter") or "").strip()
        if cand and not cand.lower().startswith("lecture"):
            chapter_val = cand

    entry = {
        "rel_path": rel_key,
        "lecture": txt_path.stem,
        "module": module_name,
        "word_count": len(words),
        "mtime": mtime,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "chapter": chapter_val,
        "last_topic": visual_cp["visual_summary"] if (visual_cp and visual_cp.get("visual_summary")) else last_top,
        "stopped_at": f"Verified from lecture video at {visual_cp['video_timestamp']}: \"{visual_cp['slide_title']}\"" if visual_cp else stop_at,
        "next_up": parsed.get("next_up", "").strip(),
        "slide_estimate": slide_info["text"] if slide_info else "",
        "slide_page": slide_info["page"] if slide_info else None,
        "matched_pdf": slide_info["pdf_name"] if slide_info else (matched_pdf_name or ""),
        "visual_verified": bool(visual_cp),
        "video_timestamp": visual_cp.get("video_timestamp") if visual_cp else None,
    }

    cp_data["entries"][rel_key] = entry
    cp_data["updated_at"] = datetime.now().isoformat()
    cp_file.write_text(json.dumps(cp_data, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"  ✓ [Checkpoint] Saved for {txt_path.stem}")
    return entry


def extract_checkpoint_from_mp4(mp4_path: Path, course_dir: Path, vision_model="minicpm-v:latest", force=False):
    """Extracts checkpoint directly from ending video frames of an MP4 lecture using local Ollama vision."""
    cp_file = course_dir / ".checkpoints.json"
    cp_data = {"updated_at": "", "latest": [], "entries": {}}
    if cp_file.exists():
        try:
            cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
            if "entries" not in cp_data:
                cp_data["entries"] = {}
        except Exception:
            pass

    rel_key = str(mp4_path.relative_to(course_dir)).replace("\\", "/")
    mtime = int(mp4_path.stat().st_mtime)

    if not force and rel_key in cp_data["entries"] and cp_data["entries"][rel_key].get("visual_verified"):
        return cp_data["entries"][rel_key]

    log(f"  👁️ [Video Vision] Inspecting ending frames of '{mp4_path.name}' with vision AI ({vision_model})...")
    visual_cp = extract_visual_checkpoint_from_video(mp4_path, course_dir=course_dir, vision_model=vision_model)

    module_name = mp4_path.parent.name if mp4_path.parent != course_dir else "Main"
    pdf_chapter = ""
    if visual_cp and visual_cp.get("pdf_name"):
        pdf_p = course_dir / "PDFs" / visual_cp["pdf_name"]
        if pdf_p.exists():
            pdf_chapter = get_pdf_chapter_title(pdf_p)

    last_topic = visual_cp.get("visual_summary") if visual_cp else f"Concluded lecture on {mp4_path.stem}"
    slide_title = visual_cp.get("slide_title") if visual_cp else ""
    stopped_at = f"Verified from video at {visual_cp['video_timestamp']}: \"{slide_title}\"" if (visual_cp and visual_cp.get("video_timestamp")) else f"Ended at final slide of {mp4_path.stem}"

    entry = {
        "rel_path": rel_key,
        "lecture": mp4_path.stem,
        "module": module_name,
        "word_count": 0,
        "mtime": mtime,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "chapter": pdf_chapter or (visual_cp.get("pdf_name", "") if visual_cp else ""),
        "last_topic": last_topic,
        "stopped_at": stopped_at,
        "next_up": f"Continue from {slide_title or 'next section'} in the course slides.",
        "slide_estimate": visual_cp.get("text", "") if visual_cp else "",
        "slide_page": visual_cp.get("page") if visual_cp else None,
        "matched_pdf": visual_cp.get("pdf_name", "") if visual_cp else "",
        "visual_verified": bool(visual_cp),
        "video_timestamp": visual_cp.get("video_timestamp") if visual_cp else None,
    }

    cp_data["entries"][rel_key] = entry
    txt_cand = mp4_path.with_suffix(".txt")
    if txt_cand.exists():
        rel_txt = str(txt_cand.relative_to(course_dir)).replace("\\", "/")
        cp_data["entries"][rel_txt] = entry

    cp_file.write_text(json.dumps(cp_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return entry


def update_course_checkpoints(course_dir: Path, llm_model="phi4:14b", vision_model="minicpm-v:latest", all_transcripts=False, force=False):
    """Finds ONLY the latest lecture(s) in course_dir and extracts checkpoint directly from MP4 video."""
    by_module = {}
    for f in course_dir.rglob("*"):
        if not f.is_file() or f.name.startswith("."):
            continue
        if "Transcripts & Slides" in f.parts or f.parent.name in ("PDFs", "Files"):
            continue
        ext = f.suffix.lower()
        if ext in (".mp4", ".txt", ".mp3"):
            rel_dir = f.parent.relative_to(course_dir)
            mod = str(rel_dir).replace("\\", "/") if str(rel_dir) != "." else "Main"
            by_module.setdefault(mod, {"mp4s": [], "txts": []})
            if ext == ".mp4" and f.stat().st_size > 1024 * 1024:  # Valid MP4 > 1MB
                by_module[mod]["mp4s"].append(f)
            elif ext == ".txt":
                by_module[mod]["txts"].append(f)

    if not by_module:
        return []

    latest_entries = []
    for mod, media in sorted(by_module.items()):
        mp4s = media["mp4s"]
        txts = media["txts"]
        mp4s.sort(key=transcript_sort_key)
        txts.sort(key=transcript_sort_key)

        e = None
        # Prioritize MP4 Video Checkpoint (Vision AI minicpm-v + PDF Slide Ground Truth)
        if mp4s:
            latest_mp4 = mp4s[-1]
            e = extract_checkpoint_from_mp4(latest_mp4, course_dir, vision_model=vision_model, force=force)

        # Fallback to transcript only if no video exists at all
        if not e and txts:
            latest_txt = txts[-1]
            e = extract_checkpoint_for_transcript(latest_txt, course_dir, llm_model=llm_model, force=force)

        if e:
            latest_entries.append(e)

    cp_file = course_dir / ".checkpoints.json"
    cp_data = {"updated_at": "", "latest": [], "entries": {}}
    if cp_file.exists():
        try:
            cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    cp_data["latest"] = latest_entries
    cp_data["updated_at"] = datetime.now().isoformat()
    cp_file.write_text(json.dumps(cp_data, indent=2, ensure_ascii=False), encoding="utf-8")

    return latest_entries


# ---------------------------------------------------------------- AI Slide & PDF Matcher (Option B)
def link_or_copy(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        try:
            dst.unlink()
        except Exception:
            pass
    try:
        os.symlink(str(src.resolve()), str(dst))
    except (OSError, NotImplementedError, AttributeError):
        shutil.copy2(str(src), str(dst))


def _deterministic_pdf_match(
    txt_path: Path,
    intro: str,
    outro: str,
    pdf_candidates: list[Path],
    pdf_first_pages: dict[str, str],
    full_text: str = "",
) -> list[str]:
    """Deterministic CPU matcher: joined-token/folder rules + BM25 + 2-half split + char 4-gram fuzzy rerank."""
    stop = {
        "lecture", "slides", "chapter", "part", "session", "and", "the", "with", "for",
        "from", "into", "this", "that", "course", "introduction", "exercises", "handouts",
        "material", "recording", "recordings", "solutions", "solution", "pdf",
    }
    title_text = re.sub(r"^Lecture\s+\d+\s*[-:]?\s*", "", txt_path.stem, flags=re.I)
    title_tokens = {w for w in re.findall(r"[a-z0-9]{3,}", title_text.lower()) if w not in stop}
    title_joined = re.sub(r"[^a-z0-9]+", "", txt_path.stem.lower())

    source_text = (full_text or f"{intro} {outro}").lower()
    raw_body_words = [w for w in re.findall(r"[a-z0-9]{4,}", source_text) if w not in stop]
    body_counts = Counter(raw_body_words)
    mid = len(raw_body_words) // 2
    half1_counts = Counter(raw_body_words[:mid]) if mid else body_counts
    half2_counts = Counter(raw_body_words[mid:]) if mid else body_counts

    chap_nums = set(re.findall(r"(?:chapter|ch\.?|module|part)\s*(\d+)", title_text, flags=re.I))
    chap_nums.update(re.findall(r"\b(\d+)\.\s+[a-z]", title_text, flags=re.I))
    is_exercise_rec = bool(re.search(r"\b(?:homework|pc\s*session|exercise[s]?|practicum|wco)\b", txt_path.stem, flags=re.I))

    def _char_ngrams(words: set[str]) -> set[str]:
        grams: set[str] = set()
        for w in words:
            if len(w) >= 6:
                for i in range(len(w) - 3):
                    grams.add(w[i : i + 4])
        return grams

    body_rare_words = {w for w, _ in body_counts.most_common(60)}
    body_ngrams = _char_ngrams(body_rare_words)

    # Build BM25 document representations (filename + folder path + cached outline/first-page text)
    doc_tokens_list: list[list[str]] = []
    doc_meta: list[tuple[Path, str, set[str], set[str], set[str], set[str]]] = []
    for p in pdf_candidates:
        p_stem_clean = re.sub(r"[_-]+", " ", p.stem.lower())
        p_tokens = {w for w in re.findall(r"[a-z0-9]{3,}", p_stem_clean) if w not in stop}
        folder_clean = re.sub(r"[_/-]+", " ", " ".join(p.parts[-3:-1]).lower())
        folder_tokens = {w for w in re.findall(r"[a-z0-9]{3,}", folder_clean) if w not in stop}
        fp_words = [w for w in re.findall(r"[a-z0-9]{4,}", pdf_first_pages.get(p.name, "").lower()) if w not in stop]
        fp_tokens = set(fp_words)
        doc_tokens = list(p_tokens) * 3 + list(folder_tokens) * 2 + fp_words
        doc_tokens_list.append(doc_tokens)
        doc_meta.append((p, p_stem_clean, p_tokens, folder_tokens, fp_tokens, _char_ngrams(p_tokens | fp_tokens)))

    n_docs = max(1, len(doc_tokens_list))
    avg_dl = max(1.0, sum(len(d) for d in doc_tokens_list) / n_docs)
    df: Counter = Counter()
    for d in doc_tokens_list:
        df.update(set(d))

    def _bm25_score(query_counts: Counter, doc_tokens: list[str]) -> float:
        if not doc_tokens or not query_counts:
            return 0.0
        tf = Counter(doc_tokens)
        dl = len(doc_tokens)
        k1, b = 1.2, 0.75
        total = 0.0
        for term, q_freq in query_counts.items():
            if term not in tf:
                continue
            idf = math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1.0)
            f = tf[term]
            tf_norm = (f * (k1 + 1.0)) / (f + k1 * (1.0 - b + b * (dl / avg_dl)))
            total += idf * tf_norm * min(3.0, 1.0 + math.log1p(q_freq))
        return total

    scored: list[tuple[float, float, float, str]] = []
    for (p, p_stem_clean, p_tokens, folder_tokens, fp_tokens, doc_ngrams), doc_tokens in zip(doc_meta, doc_tokens_list):
        score = 0.0
        score += 3.0 * len(title_tokens & p_tokens)
        score += 2.0 * len(title_tokens & folder_tokens)
        score += 1.5 * len(title_tokens & fp_tokens)

        p_joined = re.sub(r"[^a-z0-9]+", "", p.stem.lower())
        folder_joined = re.sub(r"[^a-z0-9]+", "", " ".join(p.parts[-3:-1]).lower())
        if len(p_joined) >= 5 and p_joined in title_joined:
            score += 12.0
        elif len(title_joined) >= 5 and title_joined in p_joined:
            score += 8.0
        for tok in re.findall(r"[a-z]+[0-9]+", title_joined):
            if tok in p_joined or tok in folder_joined:
                score += 8.0

        p_num_m = re.match(r"^(?:chapter|ch)?[_\s-]*0*(\d+)\b", p.stem, flags=re.I)
        if p_num_m and p_num_m.group(1) in chap_nums:
            score += 5.0
        if p_stem_clean.strip() and p_stem_clean.strip() in title_text.lower():
            score += 6.0

        pdf_is_ex = bool(re.search(r"homework|exercise|pc\s*session|pcsession", f"{p.stem} {' '.join(p.parts[-3:-1])}", flags=re.I))
        if is_exercise_rec and pdf_is_ex:
            score += 3.5
        elif not is_exercise_rec and pdf_is_ex:
            score -= 2.0

        bm25_full = _bm25_score(body_counts, doc_tokens)
        bm25_h1 = _bm25_score(half1_counts, doc_tokens)
        bm25_h2 = _bm25_score(half2_counts, doc_tokens)
        score += bm25_full * 0.35

        # Pure-Python fuzzy character 4-gram similarity for speech-to-text typos
        if body_ngrams and doc_ngrams:
            jaccard = len(body_ngrams & doc_ngrams) / max(1, len(body_ngrams | doc_ngrams))
            score += jaccard * 8.0

        scored.append((score, bm25_h1, bm25_h2, p.name))

    scored.sort(key=lambda x: (-x[0], x[3].lower()))
    if not scored:
        return []
    top_score, _, _, top_name = scored[0]
    if top_score <= 0:
        return [pdf_candidates[0].name] if len(pdf_candidates) == 1 else []
    selected = [top_name]
    if len(scored) > 1:
        second_score, s_h1, s_h2, second_name = scored[1]
        best_h1_name = max(scored, key=lambda x: x[1])[3]
        best_h2_name = max(scored, key=lambda x: x[2])[3]
        split_support = second_name in (best_h1_name, best_h2_name) and second_score >= max(3.5, top_score * 0.55)
        if second_score >= max(4.0, top_score * 0.7) or split_support:
            selected.append(second_name)
    return selected


def _lecture_number(stem: str) -> int | None:
    match = re.match(r"Lecture\s+(\d+)\b", stem, re.I)
    return int(match[1]) if match else None


def _numbered_slide_chapter(filename: str) -> int | None:
    match = re.match(r"^(?:(?:chapter|ch)[ _-]*)?(\d{1,2})(?=[ _.-])", filename, re.I)
    return int(match[1]) if match else None


def _explicit_chapters_in_stem(stem: str) -> set[int]:
    """Find explicit chapter/part mentions in lecture stem like:
    'Lecture 05 - 0. Introduction. 1. Probability' -> {0, 1}
    'Lecture 01 - 2. Univariate Distributions' -> {2}
    'Lecture 03 - 2. Univariate... 3. Multivariate...' -> {2, 3}
    'Lecture 08 - 5 Sample statistics' -> {5}
    """
    chaps: set[int] = set()
    s = re.sub(r"^(?:ANOVA\s+)?Lecture\s+\d+\s*[-:]?\s*", "", stem, flags=re.I)
    s = re.sub(r"\((?:part|pt\.?)\s*\d+\)", "", s, flags=re.I)
    for m in re.finditer(r"\b(?:chapter|chap\.?|ch\.?)\s*(\d+)\b", s, re.I):
        chaps.add(int(m.group(1)))
    for m in re.finditer(r"(?:^|[ -])(\d{1,2})\.\s+([A-Z])", s):
        chaps.add(int(m.group(1)))
    for m in re.finditer(r"(?:^|[ -])(\d{1,2})\s+([A-Z][a-z]{3,})", s):
        chaps.add(int(m.group(1)))
    return chaps


def _is_review_lecture(stem: str) -> bool:
    return bool(re.search(r"\b(?:exercise[s]?|review|recap|revision)\b", stem, re.I))


def _lecture_chapter_floor(txt_path: Path, course_dir: Path, mapping: dict) -> int | None:
    """Require agreement from two nearby lectures in the same recording series."""
    current = _lecture_number(txt_path.stem)
    if current is None or _is_review_lecture(txt_path.stem):
        return None

    chapters_by_lecture: dict[int, set[int]] = {}
    for rel, entry in mapping.items():
        if not isinstance(entry, dict):
            continue
        previous = course_dir / rel
        number = _lecture_number(previous.stem)
        if (previous.parent != txt_path.parent or number is None or not current - 4 <= number < current
                or not previous.is_file() or _is_review_lecture(previous.stem) or entry.get("confidence") == "low"):
            continue
        known = {_numbered_slide_chapter(name) for name in entry.get("matched_pdfs", [])}
        chapters_by_lecture.setdefault(number, set()).update(ch for ch in known if ch is not None)

    support = Counter(ch for chapters in chapters_by_lecture.values() for ch in chapters)
    return max((ch for ch, count in support.items() if count >= 2), default=None)


def _find_lecture_topic_in_slides(txt_path: Path, pdf_candidates: list[Path]) -> list[str]:
    """Look beyond title slides when a recent lecture teaches a subtopic."""
    if not pypdf:
        return []
    title = re.sub(r"^Lecture\s+\d+\s*[-:]?\s*", "", txt_path.stem, flags=re.I)
    terms = set(re.findall(r"[a-z]{5,}", title.lower())) - {
        "chapter", "course", "introduction", "lecture", "session", "techniques", "methods", "analysis",
    }
    if not terms:
        return []
    matches = {}
    for pdf in pdf_candidates:
        if _numbered_slide_chapter(pdf.name) is None:
            continue
        try:
            text = " ".join(page.extract_text() or "" for page in pypdf.PdfReader(str(pdf)).pages).lower()
        except Exception:
            continue
        document_terms = set(re.findall(r"[a-z]{5,}", text))
        matches[pdf.name] = terms & document_terms
    frequency = Counter(term for found in matches.values() for term in found)
    scored = sorted((sum(1 / frequency[term] for term in found), name) for name, found in matches.items())
    if not scored or scored[-1][0] == 0 or (len(scored) > 1 and scored[-1][0] == scored[-2][0]):
        return []
    return [scored[-1][1]]


_MATCH_DEFAULT_MODELS = {
    "gemini": "gemini-4-hydrogen",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
    "ollama": "phi4:14b",
}
_VIDEO_FRAME_FRACTIONS = (0.10, 0.25, 0.40, 0.55, 0.70, 0.85)


def _extract_pdf_outline(pdf_path: Path, max_pages: int = 120, max_chars: int = 1400) -> tuple[str, int, str]:
    """Extract Slide 1 snippet, page count, and a compact all-slide heading outline via pypdf."""
    if not pypdf:
        return "", 1, ""
    try:
        reader = pypdf.PdfReader(str(pdf_path))
        page_count = len(reader.pages)
        if page_count == 0:
            return "", 0, ""
        first_page = (reader.pages[0].extract_text() or "")[:250].replace("\n", " ").strip()
        outline_parts = []
        last_heading = ""
        used_chars = 0
        for idx, page in enumerate(reader.pages[:max_pages], start=1):
            raw_text = page.extract_text() or ""
            lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw_text.splitlines()]
            lines = [ln for ln in lines if ln and not re.fullmatch(r"\d+(?:\s*/\s*\d+)?", ln)]
            if not lines:
                continue
            heading = " — ".join(lines[:2])[:110]
            norm = heading.lower()
            if norm == last_heading:
                continue
            last_heading = norm
            entry = f"p{idx}: {heading}"
            if used_chars + len(entry) + 3 > max_chars:
                break
            outline_parts.append(entry)
            used_chars += len(entry) + 3
        return first_page, page_count, " | ".join(outline_parts)
    except Exception:
        return "", 1, ""


def _load_or_build_pdf_outlines(course_dir: Path, pdf_candidates: list[Path]) -> dict[str, dict]:
    """Return cached per-PDF outline metadata keyed by PDF filename, persisted by SHA-256."""
    cache_file = course_dir / ".pdf_outlines.json"
    cache: dict[str, dict] = {}
    if cache_file.exists():
        try:
            loaded = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = loaded
        except Exception:
            cache = {}

    results: dict[str, dict] = {}
    dirty = False
    for p in pdf_candidates:
        sha = _file_sha256(p)
        cached = cache.get(sha) if sha else None
        if (
            isinstance(cached, dict)
            and isinstance(cached.get("page_count"), int)
            and "outline" in cached
            and (cached.get("outline") or not pypdf)
        ):
            results[p.name] = cached
            continue
        first_page, page_count, outline = _extract_pdf_outline(p)
        meta = {
            "first_page": first_page,
            "page_count": page_count,
            "outline": outline,
        }
        results[p.name] = meta
        if sha and (outline or first_page):
            cache[sha] = meta
            dirty = True

    if dirty:
        try:
            _write_json(cache_file, cache)
        except Exception:
            pass
    return results


def _find_lecture_video(txt_path: Path, course_dir: Path | None = None) -> Path | None:
    """Locate the sibling lecture video file (.mp4/.mkv/.webm/.mov) matching txt_path."""
    for ext in (".mp4", ".mkv", ".webm", ".mov"):
        cand = txt_path.with_suffix(ext)
        if cand.is_file():
            if course_dir is not None and not _is_path_within_dir(cand, course_dir):
                continue
            return cand
    return None


def _probe_video_duration(video_path: Path) -> float | None:
    """Return video duration in seconds via ffprobe, or None if unavailable."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            return None
        dur = float(proc.stdout.strip())
        return dur if dur > 0 else None
    except Exception:
        return None


def _sample_video_frames(
    video_path: Path,
    fractions: tuple[float, ...] = _VIDEO_FRAME_FRACTIONS,
    max_width: int = 1280,
) -> list[dict]:
    """Extract evenly spaced JPEG screenshots across a lecture video without writing temp files."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not video_path.is_file():
        return []
    duration = _probe_video_duration(video_path)
    if not duration or duration < 30.0:
        return []

    frames = []
    scale_filter = f"scale='min({max_width},iw)':-2"
    for frac in fractions:
        ts = max(1.0, min(duration - 1.0, duration * frac))
        mins, secs = divmod(int(ts), 60)
        try:
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-v", "error",
                    "-ss", f"{ts:.2f}",
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-vf", scale_filter,
                    "-q:v", "4",
                    "-f", "image2pipe",
                    "-vcodec", "mjpeg",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=20,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout and len(proc.stdout) > 512:
                frames.append({
                    "timestamp": f"{mins:02d}:{secs:02d}",
                    "pct": int(round(frac * 100)),
                    "mime_type": "image/jpeg",
                    "b64": base64.b64encode(proc.stdout).decode("ascii"),
                })
        except Exception:
            continue
    return frames


def _sample_transcript_slices(words: list[str]) -> str:
    """Return full text for short transcripts, or 4 evenly distributed slices across the lecture."""
    if len(words) <= 1200:
        return " ".join(words)
    n = len(words)
    span = 250
    slices = [
        ("Start (~0%)", " ".join(words[:span])),
        ("Early-Mid (~33%)", " ".join(words[max(0, n // 3 - span // 2): n // 3 + span // 2])),
        ("Late-Mid (~66%)", " ".join(words[max(0, (2 * n) // 3 - span // 2): (2 * n) // 3 + span // 2])),
        ("End (~100%)", " ".join(words[-span:])),
    ]
    return "\n\n".join(f"[{label}]\n{chunk}" for label, chunk in slices)


def _resolve_match_backend(backend: str | None = "auto") -> str:
    """Resolve slide-matching backend.

    API keys are treated strictly as credentials, never as implicit consent to upload.
    Default 'auto' stays local (Ollama -> deterministic CPU fallback) unless the user
    explicitly opts into cloud matching via --backend gemini|openai|anthropic|claude or TOLEDO_MATCH_BACKEND.
    Passing --backend local/ollama/keyword always overrides any cloud environment preference.
    """
    choice = (backend or "auto").strip().lower()
    if choice == "claude":
        choice = "anthropic"
    if choice == "local":
        return "keyword"
    if choice == "auto":
        env_pref = os.environ.get("TOLEDO_MATCH_BACKEND", "").strip().lower()
        if env_pref == "claude":
            env_pref = "anthropic"
        if env_pref == "local":
            return "keyword"
        if env_pref in ("gemini", "openai", "anthropic", "ollama", "keyword"):
            return env_pref
        return "ollama"
    return choice if choice in ("gemini", "openai", "anthropic", "ollama", "keyword") else "ollama"


def _parse_json_object(raw_text: str) -> dict:
    """Parse a JSON object from an LLM response, tolerating markdown code fences."""
    s = (raw_text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", s, flags=re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else {}
            except Exception:
                pass
    return {}


def _call_match_gemini(prompt: str, frames: list[dict], model: str, api_key: str) -> dict:
    """Call Gemini Interactions API with optional MP4 frames and structured JSON output via header auth."""
    input_blocks: list[dict] = [{"type": "text", "text": prompt}]
    for frame in frames:
        input_blocks.append({
            "type": "text",
            "text": f"Lecture video frame at {frame['timestamp']} ({frame['pct']}% through lecture):",
        })
        input_blocks.append({
            "type": "image",
            "data": frame["b64"],
            "mime_type": frame["mime_type"],
        })
    payload_obj = {
        "model": model,
        "store": False,
        "input": input_blocks,
        "generation_config": {
            "thinking_level": "low",
        },
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": {
                "type": "object",
                "properties": {
                    "matched_pdfs": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "slide_range": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["matched_pdfs", "confidence", "slide_range", "reason"],
            },
        },
    }
    data = _post_gemini_interaction(payload_obj, api_key=api_key, api_version="v1", timeout=120)
    text = _extract_interaction_text(data)
    return _parse_json_object(text)


def _call_match_openai(prompt: str, frames: list[dict], model: str, api_key: str, base_url: str | None = None) -> dict:
    """Call an OpenAI-compatible /chat/completions endpoint with optional multimodal video frames."""
    base = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/chat/completions"
    if frames:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for frame in frames:
            content.append({"type": "text", "text": f"Lecture video frame at {frame['timestamp']} ({frame['pct']}% through lecture):"})
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{frame['mime_type']};base64,{frame['b64']}"},
            })
        messages = [{"role": "user", "content": content}]
    else:
        messages = [{"role": "user", "content": prompt}]

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choices = data.get("choices") or []
    if not choices:
        return {}
    text = ((choices[0].get("message") or {}).get("content")) or ""
    return _parse_json_object(text)


def _call_match_anthropic(prompt: str, frames: list[dict], model: str, api_key: str) -> dict:
    """Call Anthropic Claude /v1/messages endpoint with optional multimodal video frames."""
    content: list[dict] = []
    for fr in frames:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": fr["mime_type"],
                "data": fr["b64"],
            },
        })
    content.append({"type": "text", "text": prompt})
    payload = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": content}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    blocks = data.get("content") or []
    raw_text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    return _parse_json_object(raw_text)


def _call_match_ollama(prompt: str, model: str) -> dict:
    """Call local Ollama /api/generate endpoint for offline slide matching."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 2048},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    return _parse_json_object(raw.get("response", "{}"))


def _make_cloud_approval(
    feature: str,
    provider: str,
    model: str,
    allowed_paths: list[Path],
    *,
    allow_video: bool = False,
    data_types: tuple[str, ...] = (),
    course_dirs: list[Path] | None = None,
    expected_hashes: dict[Path, str] | None = None,
    max_duration_sec: float | None = None,
    max_file_durations: dict[Path, float] | None = None,
) -> dict:
    """Create an explicit, scoped cloud upload approval bound to feature, provider, model, and exact inputs."""
    resolved_types = tuple(data_types)
    if not resolved_types:
        if feature == "match-slides":
            resolved_types = (
                ("transcript_excerpts", "pdf_outlines", "video_frames")
                if allow_video
                else ("transcript_excerpts", "pdf_outlines")
            )
        elif feature == "transcribe":
            resolved_types = ("audio_chunks",)
    approval: dict = {
        "feature": feature,
        "provider": provider,
        "model": model,
        "allowed_paths": {Path(p).resolve() for p in allowed_paths},
        "allow_video": bool(allow_video),
        "data_types": resolved_types,
    }
    if course_dirs is not None:
        approval["allowed_courses"] = {Path(d).resolve() for d in course_dirs}
    if expected_hashes:
        approval["expected_hashes"] = {Path(k).resolve(): v for k, v in expected_hashes.items() if v}
    if max_duration_sec is not None:
        approval["max_duration_sec"] = float(max_duration_sec)
    if max_file_durations:
        approval["max_file_durations"] = {Path(k).resolve(): float(v) for k, v in max_file_durations.items()}
    return approval


def _is_cloud_approved(
    approval: dict | None,
    feature: str,
    provider: str,
    path: Path,
    *,
    allow_video: bool | None = None,
    model: str | None = None,
    data_types: tuple[str, ...] | list[str] | None = None,
    course_dir: Path | None = None,
    duration_sec: float | None = None,
) -> bool:
    """Verify an explicit cloud approval covers this exact feature, provider, model, data types, and input."""
    if not isinstance(approval, dict):
        return False
    if approval.get("feature") != feature or approval.get("provider") != provider:
        return False
    if model is not None and approval.get("model") != model:
        return False
    if allow_video is not None and bool(approval.get("allow_video")) != bool(allow_video):
        return False
    if data_types is not None:
        approved_types = set(approval.get("data_types") or ())
        if not approved_types or not set(data_types).issubset(approved_types):
            return False
    allowed = approval.get("allowed_paths")
    if not isinstance(allowed, set):
        return False
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    if resolved not in allowed:
        return False
    if course_dir is not None and not _is_path_within_dir(path, course_dir):
        return False
    allowed_courses = approval.get("allowed_courses")
    if isinstance(allowed_courses, set):
        if course_dir is not None:
            try:
                if Path(course_dir).resolve() not in allowed_courses:
                    return False
            except OSError:
                return False
        if not any(_is_path_within_dir(resolved, c) for c in allowed_courses):
            return False
    expected_hashes = approval.get("expected_hashes")
    if isinstance(expected_hashes, dict) and resolved in expected_hashes:
        if _file_sha256(path) != expected_hashes[resolved]:
            return False
    max_dur = approval.get("max_duration_sec")
    max_file_durs = approval.get("max_file_durations")
    has_file_dur_limit = isinstance(max_file_durs, dict) and resolved in max_file_durs
    if max_dur is not None or has_file_dur_limit or duration_sec is not None:
        if duration_sec is None or not math.isfinite(duration_sec) or duration_sec <= 0:
            return False
        if max_dur is not None and duration_sec > float(max_dur):
            return False
        if has_file_dur_limit and duration_sec > float(max_file_durs[resolved]) + 1.0:
            return False
    return True


def match_transcript_with_pdfs(
    txt_path: Path,
    course_dir: Path,
    llm_model: str | None = None,
    force: bool = False,
    backend: str = "auto",
    visual: bool = True,
    cloud_approval: dict | None = None,
):
    """Matches a lecture transcript (and optional sibling MP4 slide frames) to its PDF slides
    using cloud AI (Gemini/OpenAI-compatible, strictly when explicitly approved), local Ollama,
    or deterministic CPU fallback, and organizes them side-by-side inside
    ~/Lectures/<Course>/Transcripts & Slides/<Lecture Name>/.
    """
    if not _is_path_within_dir(txt_path, course_dir):
        log(f"  ✗ [Match-Slides] Rejected transcript outside course directory: {txt_path}")
        return None
    map_file = course_dir / ".pdf_mapping.json"
    mapping = {}
    if map_file.exists():
        try:
            mapping = json.loads(map_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    rel_key = str(txt_path.relative_to(course_dir)).replace("\\", "/")
    pair_dir = course_dir / "Transcripts & Slides" / txt_path.stem
    _ensure_within_dir(pair_dir, course_dir)
    previous = mapping.get(rel_key, {})
    floor = _lecture_chapter_floor(txt_path, course_dir, mapping)
    explicit_chaps = _explicit_chapters_in_stem(txt_path.stem)

    def eligible(filename):
        chapter = _numbered_slide_chapter(filename)
        if explicit_chaps:
            return chapter is None or chapter in explicit_chaps
        return floor is None or chapter is None or chapter >= floor

    if not force and previous and pair_dir.is_dir() and all(eligible(name) for name in previous.get("matched_pdfs", [])):
        return previous

    # Gather available candidate PDFs in course (excluding Transcripts & Slides directory)
    pdf_candidates = []
    pdf_sources: dict[str, list[Path]] = {}
    seen_hashes = set()
    for p in sorted(course_dir.rglob("*.pdf")):
        if (not p.is_file() or p.name.startswith(".") or "Transcripts & Slides" in p.parts
                or "LLM_Knowledge" in p.parts or "Announcements" in p.parts
                or not p.resolve().is_relative_to(course_dir.resolve())):
            continue
        pdf_sources.setdefault(p.name, []).append(p)
        if not eligible(p.name):
            continue
        sha = _file_sha256(p)
        if sha and sha in seen_hashes:
            continue
        if sha:
            seen_hashes.add(sha)
        pdf_candidates.append(p)

    if not pdf_candidates and not previous:
        return None

    # Build cached all-slide outlines (falls back cleanly when pypdf is unavailable)
    outlines = _load_or_build_pdf_outlines(course_dir, pdf_candidates)
    pdf_summaries = []
    pdf_first_pages = {}
    pdf_map = {p.name: p for p in pdf_candidates}
    for p in pdf_candidates:
        meta = outlines.get(p.name, {})
        first_page = meta.get("first_page", "")
        page_count = meta.get("page_count", 1)
        outline = meta.get("outline", "")
        pdf_first_pages[p.name] = f"{first_page} {outline}".strip()
        if outline:
            pdf_summaries.append(f"- Filename: {p.name} ({page_count} pages)\n  Slides outline: {outline}")
        else:
            pdf_summaries.append(f"- Filename: {p.name} ({page_count} pages) | Title/Slide 1: {first_page}")

    text = txt_path.read_text(encoding="utf-8", errors="ignore").strip()
    words = text.split()
    if len(words) < 20:
        return None

    intro = " ".join(words[:300])
    outro = " ".join(words[-300:])
    transcript_excerpts = _sample_transcript_slices(words)
    module_name = txt_path.parent.name if txt_path.parent != course_dir else "Main"

    resolved_backend = _resolve_match_backend(backend)
    selected_model = llm_model or _MATCH_DEFAULT_MODELS.get(resolved_backend, "phi4:14b")
    video_path = _find_lecture_video(txt_path, course_dir=course_dir) if visual else None
    if resolved_backend in ("gemini", "openai", "anthropic"):
        req_data_types = (
            ("transcript_excerpts", "pdf_outlines", "video_frames")
            if (visual and video_path is not None)
            else ("transcript_excerpts", "pdf_outlines")
        )
        if not _is_cloud_approved(
            cloud_approval,
            "match-slides",
            resolved_backend,
            txt_path,
            allow_video=visual,
            model=selected_model,
            data_types=req_data_types,
            course_dir=course_dir,
        ):
            log(f"  ℹ️ [Match-Slides] Cloud upload to '{resolved_backend}' not approved for '{txt_path.stem}'; staying local.")
            resolved_backend = "ollama"
            selected_model = llm_model or _MATCH_DEFAULT_MODELS.get(resolved_backend, "phi4:14b")

    frames: list[dict] = []
    if visual and video_path is not None and resolved_backend in ("gemini", "openai", "anthropic"):
        frames = _sample_video_frames(video_path)

    visual_instructions = ""
    if frames:
        visual_instructions = (
            f"\nVisual evidence: You are also given {len(frames)} evenly spaced screenshots from the lecture video "
            f"(10% to 85% through the recording). Look at the slide deck actually displayed on screen (either full-screen "
            f"or in the slide panel of a split-screen recording). Visual slide titles/content are the strongest ground truth.\n"
        )

    prompt = f"""You are matching a university lecture recording/transcript to its accompanying PDF slide deck.
Course: {course_dir.name} ({module_name})
Lecture Title: {txt_path.stem}
Recent course sequence: {f"Repeated matches put this lecture at Chapter {floor} or later." if floor is not None else "No confirmed chapter sequence."}
{visual_instructions}
Lecture Transcript Excerpts:
\"\"\"{transcript_excerpts}\"\"\"

Available PDF files in the course (with slide-by-slide outlines):
{chr(10).join(pdf_summaries)}

Rules:
- Select ONLY the PDF slide file(s) with SUSTAINED primary coverage across this lecture.
- Ignore brief 1-slide refreshers or quick recaps of an earlier chapter (for example, flashing an old PDF briefly at the start before spending the rest of the class on the actual chapter).
- Return two PDF files ONLY for a genuine chapter transition where both decks have sustained coverage.
- Return an empty list if none apply.

Respond ONLY in valid JSON:
{{
  "matched_pdfs": ["exact_filename.pdf"],
  "confidence": "high|medium|low",
  "slide_range": "optional approximate slide range, e.g. slides 1-24, or empty string",
  "reason": "1 short sentence explaining why this PDF matches the lecture"
}}
"""

    parsed: dict = {}
    used_fallback = False
    actual_backend = resolved_backend

    if resolved_backend == "keyword":
        used_fallback = True
        actual_backend = "keyword"
        log(f"  📑 [Match-Slides] Pairing '{txt_path.stem}' with course PDFs via keyword matcher...")
    elif resolved_backend == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            used_fallback = True
            actual_backend = "keyword"
            log("  ℹ️ [Match-Slides] GEMINI_API_KEY not set; using deterministic matching...")
        else:
            mode_tag = f"Gemini ({selected_model}, {len(frames)} video frames)" if frames else f"Gemini ({selected_model})"
            log(f"  📑 [Match-Slides] Pairing '{txt_path.stem}' with course PDFs via {mode_tag}...")
            try:
                parsed = _call_match_gemini(prompt, frames, selected_model, api_key)
            except Exception as exc:
                used_fallback = True
                actual_backend = "keyword"
                log(f"  ℹ️ [Match-Slides] Gemini API unavailable ({_redact_secrets(str(exc))}); using deterministic matching...")
    elif resolved_backend == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            used_fallback = True
            actual_backend = "keyword"
            log("  ℹ️ [Match-Slides] OPENAI_API_KEY not set; using deterministic matching...")
        else:
            mode_tag = f"OpenAI API ({selected_model}, {len(frames)} video frames)" if frames else f"OpenAI API ({selected_model})"
            log(f"  📑 [Match-Slides] Pairing '{txt_path.stem}' with course PDFs via {mode_tag}...")
            try:
                parsed = _call_match_openai(prompt, frames, selected_model, api_key)
            except Exception as exc:
                used_fallback = True
                actual_backend = "keyword"
                log(f"  ℹ️ [Match-Slides] OpenAI API unavailable ({_redact_secrets(str(exc))}); using deterministic matching...")
    elif resolved_backend == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            used_fallback = True
            actual_backend = "keyword"
            log("  ℹ️ [Match-Slides] ANTHROPIC_API_KEY not set; using deterministic matching...")
        else:
            mode_tag = f"Anthropic Claude ({selected_model}, {len(frames)} video frames)" if frames else f"Anthropic Claude ({selected_model})"
            log(f"  📑 [Match-Slides] Pairing '{txt_path.stem}' with course PDFs via {mode_tag}...")
            try:
                parsed = _call_match_anthropic(prompt, frames, selected_model, api_key)
            except Exception as exc:
                used_fallback = True
                actual_backend = "keyword"
                log(f"  ℹ️ [Match-Slides] Anthropic API unavailable ({_redact_secrets(str(exc))}); using deterministic matching...")
    else:
        log(f"  📑 [Match-Slides] Pairing '{txt_path.stem}' with course PDFs via local AI ({selected_model})...")
        try:
            parsed = _call_match_ollama(prompt, selected_model)
        except Exception:
            used_fallback = True
            actual_backend = "keyword"
            log("  ℹ️ [Match-Slides] Local Ollama unavailable; using deterministic keyword/title matching...")

    matched_names = parsed.get("matched_pdfs", [])
    if isinstance(matched_names, str):
        matched_names = [matched_names]
    elif not isinstance(matched_names, list):
        matched_names = []
    valid_matches = list(dict.fromkeys(m for m in matched_names if isinstance(m, str) and m in pdf_map))

    explicit_matches = [p.name for p in pdf_candidates if _numbered_slide_chapter(p.name) in explicit_chaps]

    if not valid_matches and (used_fallback or matched_names or explicit_matches):
        if explicit_matches:
            valid_matches = explicit_matches
            actual_backend = "keyword"
            parsed = {
                "confidence": "high",
                "reason": f"Matched from explicit chapter reference(s) {sorted(explicit_chaps)} in lecture title.",
            }
        elif floor is not None:
            valid_matches = _find_lecture_topic_in_slides(txt_path, pdf_candidates)
        elif used_fallback:
            valid_matches = _deterministic_pdf_match(txt_path, intro, outro, pdf_candidates, pdf_first_pages, full_text=text)
        if valid_matches and not parsed.get("reason"):
            actual_backend = "keyword"
            parsed = {
                "confidence": "medium",
                "reason": "Matched deterministically using the lecture topic and eligible slides.",
            }

    # Create Option B structure: ~/Lectures/<Course>/Transcripts & Slides/<Lecture Name>/
    pair_dir.mkdir(parents=True, exist_ok=True)

    # Link transcript .txt and .srt
    link_or_copy(txt_path, pair_dir / txt_path.name)
    srt_path = txt_path.with_suffix(".srt")
    if srt_path.exists():
        link_or_copy(srt_path, pair_dir / srt_path.name)

    # Link matched PDFs
    for m in valid_matches:
        src_pdf = pdf_map[m]
        link_or_copy(src_pdf, pair_dir / src_pdf.name)

    # Remove only stale files from the previous generated pairing.
    for name in set(previous.get("matched_pdfs", [])) - set(valid_matches):
        if Path(name).name == name and Path(name).suffix.lower() == ".pdf":
            stale = pair_dir / name
            sources = pdf_sources.get(name, [])
            if stale.is_symlink() and any(stale.resolve() == source.resolve() for source in sources):
                stale.unlink()
            elif stale.is_file() and not stale.is_symlink() and any(
                _file_sha256(stale) == _file_sha256(source) for source in sources
            ):
                stale.unlink()

    entry = {
        "lecture": txt_path.stem,
        "rel_path": rel_key,
        "matched_pdfs": valid_matches,
        "folder": str(pair_dir.relative_to(course_dir)),
        "reason": parsed.get("reason", ""),
        "confidence": parsed.get("confidence", "medium"),
        "backend": actual_backend,
        "used_video_frames": len(frames) if actual_backend in ("gemini", "openai") else 0,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if parsed.get("slide_range"):
        entry["slide_range"] = str(parsed["slide_range"]).strip()
    mapping[rel_key] = entry
    _write_json(map_file, mapping)
    log(f"  ✓ [Match-Slides] Paired '{txt_path.stem}' with {valid_matches} -> Transcripts & Slides/{txt_path.stem}/")
    return entry


def _course_match_transcripts(course_dir: Path) -> list[Path]:
    """Return candidate transcript files in chronological lecture order for slide matching."""
    txts = [
        f for f in course_dir.rglob("*.txt")
        if f.is_file()
        and _is_path_within_dir(f, course_dir)
        and not _transcript_inprogress_marker(f).exists()
        and (not _transcript_done_marker(f).exists() or f.with_suffix(".srt").is_file())
        and (_is_transcript_txt(f) or _lecture_number(f.stem) is not None)
        and not any(part in f.parts for part in ("Transcripts & Slides", "LLM_Knowledge"))
    ]
    return sorted(
        txts,
        key=lambda f: (_lecture_number(f.stem) is None, _lecture_number(f.stem) or 0, transcript_sort_key(f)),
    )


def _plan_match_jobs(course_dirs: list[Path], force: bool = False, limit: int | None = None) -> list[dict]:
    """Build the single explicit job list shared by preview, limit, confirmation, and execution.

    Uses the exact same chronology/eligibility check as match_transcript_with_pdfs() so stale
    cached pairings cannot bypass preview or CLI confirmation.
    """
    jobs: list[dict] = []
    for d in course_dirs:
        map_file = d / ".pdf_mapping.json"
        mapping = {}
        if map_file.exists():
            try:
                mapping = json.loads(map_file.read_text(encoding="utf-8"))
            except Exception:
                mapping = {}

        for t in _course_match_transcripts(d):
            rel_key = str(t.relative_to(d)).replace("\\", "/")
            pair_dir = d / "Transcripts & Slides" / t.stem
            previous = mapping.get(rel_key, {})
            floor = _lecture_chapter_floor(t, d, mapping)
            explicit_chaps = _explicit_chapters_in_stem(t.stem)

            def _eligible(filename, _floor=floor, _explicit=explicit_chaps):
                chapter = _numbered_slide_chapter(filename)
                if _explicit:
                    return chapter is None or chapter in _explicit
                return _floor is None or chapter is None or chapter >= _floor

            if (
                not force
                and previous
                and pair_dir.is_dir()
                and all(_eligible(name) for name in previous.get("matched_pdfs", []))
            ):
                continue

            jobs.append({
                "course_dir": d,
                "txt_path": t,
                "has_video": _find_lecture_video(t, course_dir=d) is not None,
            })
            if limit is not None and limit > 0 and len(jobs) >= limit:
                return jobs
    return jobs


def update_course_pdf_matches(
    course_dir: Path,
    llm_model: str | None = None,
    force: bool = False,
    backend: str = "local",
    visual: bool = True,
    cloud_approval: dict | None = None,
):
    """Matches all transcripts in course_dir to their PDFs and organizes Transcripts & Slides/<Lecture>/ folders."""
    txts = _course_match_transcripts(course_dir)
    if not txts:
        return []

    results = []
    for t in txts:
        res = match_transcript_with_pdfs(
            t,
            course_dir,
            llm_model=llm_model,
            force=force,
            backend=backend,
            visual=visual,
            cloud_approval=cloud_approval,
        )
        if res:
            results.append(res)
    return results


def cmd_match_slides(args):
    """CLI command to run AI matching of transcripts to PDF slides and create Option B folders."""
    if not OUT.exists():
        log("No matching course directories found.")
        return 1
    if args.course:
        dirs = [
            d for d in sorted(OUT.iterdir())
            if d.is_dir() and not d.name.startswith(".") and _is_path_within_dir(d, OUT) and args.course.lower() in d.name.lower()
        ]
    else:
        dirs = [
            d for d in sorted(OUT.iterdir())
            if d.is_dir() and not d.name.startswith(".") and _is_path_within_dir(d, OUT)
        ]

    if not dirs:
        log("No matching course directories found.")
        return 1

    backend = getattr(args, "backend", "auto")
    resolved_backend = _resolve_match_backend(backend)
    visual = not getattr(args, "no_video", False)
    dry_run = getattr(args, "dry_run", False)
    auto_yes = getattr(args, "yes", False)
    limit = getattr(args, "limit", None)
    if limit is not None and limit <= 0:
        limit = None

    jobs = _plan_match_jobs(dirs, force=args.force, limit=limit)
    pending_count = len(jobs)
    video_count = sum(1 for j in jobs if j["has_video"])

    cloud_approval = None
    if resolved_backend in ("gemini", "openai", "anthropic"):
        selected_model = args.model or _MATCH_DEFAULT_MODELS.get(resolved_backend, "")
        data_types = (
            ("transcript_excerpts", "pdf_outlines", "video_frames")
            if visual and video_count > 0
            else ("transcript_excerpts", "pdf_outlines")
        )
        data_desc = (
            f"transcript excerpts, PDF slide outlines, and up to {len(_VIDEO_FRAME_FRACTIONS)} JPEG video frames/lecture"
            if visual and video_count > 0
            else "transcript excerpts and PDF slide outlines (no video frames)"
        )
        est_cost = _estimate_cloud_cost(
            "match-slides",
            resolved_backend,
            selected_model,
            item_count=pending_count,
            has_video=bool(visual and video_count > 0),
        )
        log(f"\n☁️  [Cloud Match-Slides] Provider: {resolved_backend} ({selected_model})")
        log(f"    Data to upload: {data_desc}")
        log(f"    Pending lectures: {pending_count} ({video_count} with sibling video)")
        log(f"    Estimated cost: {est_cost}")
        if pending_count == 0:
            log("    Nothing pending — zero cloud API requests sent.")
            return 0
        if dry_run:
            for j in jobs:
                log(f"    • {j['course_dir'].name} / {j['txt_path'].stem}")
            log("    Dry run — no cloud API requests sent.")
            return 0
        if not auto_yes:
            if not sys.stdin.isatty():
                log("    Aborted: cloud upload requires --yes in non-interactive mode.")
                return 1
            try:
                ans = input("    Proceed with cloud slide matching? [y/N]: ").strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes"):
                log("    Cancelled — no cloud API requests sent.")
                return 0
        env_var = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[resolved_backend]
        if not _ensure_api_key_interactive(env_var, resolved_backend.title()):
            log(f"    Aborted: {env_var} is not set.")
            return 1
        cloud_approval = _make_cloud_approval(
            feature="match-slides",
            provider=resolved_backend,
            model=selected_model,
            allowed_paths=[j["txt_path"] for j in jobs],
            allow_video=visual,
            data_types=data_types,
            course_dirs=dirs,
            expected_hashes={j["txt_path"]: _file_sha256(j["txt_path"]) for j in jobs},
        )
    else:
        if dry_run:
            log(f"\n💻 [Local Match-Slides] Backend: {resolved_backend} | Pending lectures: {pending_count} ({video_count} with video)")
            for j in jobs:
                log(f"    • {j['course_dir'].name} / {j['txt_path'].stem}")
            log("    Dry run — no changes written.")
            return 0
        if pending_count == 0:
            log("All lecture transcripts are already paired with their slides.")
            return 0

    # Execute strictly the planned/approved job list so zero unapproved lectures can upload.
    jobs_by_course: dict[Path, list[dict]] = {}
    for j in jobs:
        jobs_by_course.setdefault(j["course_dir"], []).append(j)

    total_matched = 0
    matched_courses: set[Path] = set()
    for d, course_jobs in jobs_by_course.items():
        log(f"\n📘 {d.name} ({len(course_jobs)} lecture(s))")
        for j in course_jobs:
            m = match_transcript_with_pdfs(
                j["txt_path"],
                d,
                llm_model=args.model,
                force=args.force,
                backend=backend,
                visual=visual,
                cloud_approval=cloud_approval,
            )
            if m:
                total_matched += 1
                matched_courses.add(d)
                pdfs_str = ", ".join(m["matched_pdfs"]) if m["matched_pdfs"] else "No direct match"
                log(f"  • {m['lecture']} -> {pdfs_str}")

    log(f"\nAI Slide Matching complete: Paired {total_matched} lecture(s) with accompanying PDFs in 'Transcripts & Slides/' folders!")
    _reindex_after_standalone_transcribe(args, matched_courses)
    return 0


def cmd_checkpoint(args):
    """CLI command to generate and display 'Where the Professor Left Off' across courses."""
    if not OUT.exists():
        log("No matching course directories found.")
        return 1
    if args.course:
        dirs = [
            d for d in sorted(OUT.iterdir())
            if d.is_dir() and not d.name.startswith(".") and _is_path_within_dir(d, OUT) and args.course.lower() in d.name.lower()
        ]
    else:
        dirs = [
            d for d in sorted(OUT.iterdir())
            if d.is_dir() and not d.name.startswith(".") and _is_path_within_dir(d, OUT)
        ]

    if not dirs:
        log("No matching course directories found.")
        return 1

    for d in dirs:
        txts = list(d.rglob("*.txt"))
        if not txts:
            continue
        log(f"\n📘 {d.name}")
        latest = update_course_checkpoints(d, llm_model=args.model, all_transcripts=args.all, force=args.force)
        for item in latest:
            mod_tag = f" [{item['module']}]" if item.get("module") and item["module"] != "Main" else ""
            log(f"  🎙️ {item['lecture']}{mod_tag}")
            log(f"     • Last Topic:  {item['last_topic']}")
            log(f"     • Stopped At:  {item['stopped_at']}")
            log(f"     • Next Up:     {item['next_up']}")
    return 0


def cmd_ui(args):
    """Launches the minimalist Personal Toledo local dashboard."""
    dashboard_script = Path(__file__).parent / "dashboard.py"
    if not dashboard_script.exists():
        dashboard_script = Path.home() / "dashboard.py"
    cmd = [sys.executable, str(dashboard_script), "--port", str(args.port)]
    if args.no_browser:
        cmd.append("--no-browser")
    return subprocess.call(cmd)


def _cuda_device_count():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


def _plan_transcribe_jobs(
    candidates: list[Path],
    *,
    limit: int | None = None,
    max_minutes: float | None = None,
    probe_duration: bool = False,
) -> tuple[list[dict], int, float]:
    """Build the single explicit transcription job list after applying --limit and --max-minutes."""
    jobs: list[dict] = []
    skipped_by_cap = 0
    total_sec = 0.0
    max_sec = (float(max_minutes) * 60.0) if (max_minutes is not None and max_minutes > 0) else None

    for f in candidates:
        if limit is not None and limit > 0 and len(jobs) >= limit:
            skipped_by_cap += 1
            continue
        dur = _probe_video_duration(f) if probe_duration else None
        if probe_duration and (dur is None or not math.isfinite(dur) or dur <= 0):
            skipped_by_cap += 1
            continue
        dur_val = float(dur) if (dur is not None and dur > 0) else 0.0
        if max_sec is not None and dur_val > 0 and jobs and (total_sec + dur_val > max_sec):
            skipped_by_cap += 1
            continue
        if max_sec is not None and dur_val > 0 and not jobs and dur_val > max_sec:
            skipped_by_cap += 1
            continue
        jobs.append({"path": f, "duration_sec": dur_val})
        total_sec += dur_val
    return jobs, skipped_by_cap, total_sec


def _reindex_after_standalone_transcribe(args, course_dirs: set[Path]):
    """Rebuild the search index for transcribed courses after standalone `toledo-dl transcribe`."""
    if getattr(args, "report", None) is not None or not course_dirs:
        return
    try:
        indexed = build_llm_index(target_dirs=sorted(course_dirs))
        log(f"  ✓ Updated search index ({indexed} item(s))")
    except Exception as e:
        log(f"  ⚠ Search index update skipped: {e}")


def _prompt_transcribe_backend(has_gpu: bool, default_backend: str = "auto", allow_skip: bool = False) -> str:
    """Prompt the user in an interactive TTY to choose local GPU, local CPU (with warning), or Skip."""
    if has_gpu:
        log("\n🎙️  Choose transcription mode:")
        log("    [1] Local GPU (CUDA Whisper) [recommended]")
        log("    [2] Local CPU (Whisper — warning: takes a really long time XD)")
        if allow_skip:
            log("    [3] Skip transcription for now")
        default_num = "1"
        max_num = "3" if allow_skip else "2"
        mapping = {"1": "gpu", "2": "cpu"}
        if allow_skip:
            mapping["3"] = "skip"
    else:
        log("\nℹ️  No GPU detected for transcription:")
        if allow_skip:
            log("    [1] Continue without transcribing lectures [recommended]")
            log("    [2] Transcribe with CPU instead (takes much longer — ~10–35+ min per 1h lecture XD)")
            default_num = "1"
            max_num = "2"
            mapping = {"1": "skip", "2": "cpu"}
        else:
            log("    [1] Transcribe with CPU instead (takes much longer — ~10–35+ min per 1h lecture XD)")
            default_num = "1"
            max_num = "1"
            mapping = {"1": "cpu"}
    try:
        choice = input(f"    Select [1-{max_num}] (default {default_num}): ").strip()
    except (EOFError, KeyboardInterrupt):
        return default_backend
    if not choice:
        choice = default_num
    return mapping.get(choice, default_backend)


def _prompt_match_backend(default_backend: str = "local") -> str:
    """Prompt the user in an interactive TTY to choose local or cloud slide-matching backend."""
    log("\n📑 Choose slide-matching backend:")
    log("    [1] Local (Ollama / keyword fallback) [default]")
    log("    [2] Cloud API: Gemini")
    log("    [3] Cloud API: OpenAI")
    log("    [4] Cloud API: Anthropic Claude")
    try:
        choice = input("    Select [1-4] (default 1): ").strip()
    except (EOFError, KeyboardInterrupt):
        return default_backend
    return {"1": "local", "2": "gemini", "3": "openai", "4": "anthropic"}.get(choice or "1", default_backend)


def _prompt_pdf_vision_backend(has_gpu: bool, default_backend: str = "off") -> str:
    """Prompt the user in an interactive TTY whether to visually scan PDFs (local GPU or cloud API)."""
    log("\n🖼️  Scan PDFs visually (convert diagrams & images into structured Markdown)?")
    log("    Recommended if your PDFs have lots of high-value diagrams/images (especially with few/no lecture transcripts).")
    log("    Not recommended (usually overkill) if your PDFs are mostly plain text or already covered by lecture transcripts.")
    gpu_note = "GPU detected — free & offline via Ollama" if has_gpu else "requires a GPU — slow/unavailable without one"
    log("    [1] No — standard text extraction only [default]")
    log(f"    [2] Yes — Local GPU vision (Ollama: {gpu_note})")
    log("    [3] Yes — Cloud API (Gemini Vision — works without a GPU; incurs a small API cost, ~$0.50-$2/semester)")
    default_num = {"off": "1", "ollama": "2", "gemini": "3"}.get((default_backend or "off").lower(), "1")
    try:
        choice = input(f"    Select [1-3] (default {default_num}): ").strip()
    except (EOFError, KeyboardInterrupt):
        return default_backend
    return {"1": "off", "2": "ollama", "3": "gemini"}.get(choice or default_num, default_backend)


def cmd_transcribe(args):
    """Transcribes downloaded lecture audio locally (GPU/CPU Whisper) or via opt-in Gemini API."""
    if not OUT.exists():
        log("No downloaded recordings yet. Run 'toledo-sync run' first.")
        return 0
    if args.course:
        matching = [
            d for d in OUT.iterdir()
            if d.is_dir() and _is_path_within_dir(d, OUT) and args.course.lower() in d.name.lower()
        ]
        if not matching:
            log(f"No lecture directory found matching '{args.course}' in {OUT}")
            return 1
        target_dirs = matching
    else:
        target_dirs = [
            d for d in OUT.iterdir()
            if d.is_dir() and not d.name.startswith(".") and _is_path_within_dir(d, OUT)
        ]

    by_stem = {}
    course_by_path: dict[Path, Path] = {}
    for d in target_dirs:
        for f in sorted(d.rglob("*")):
            if "LLM_Knowledge" in f.parts or "Transcripts & Slides" in f.parts:
                continue
            if not _is_path_within_dir(f, d):
                continue
            if f.is_file() and f.suffix.lower() in [".mp3", ".mp4", ".mov", ".m4v", ".mkv", ".webm"]:
                stem_key = f.with_suffix("")
                if stem_key not in by_stem or f.suffix.lower() == ".mp3":
                    by_stem[stem_key] = f
                    course_by_path[f] = d
    audio_files = list(by_stem.values())

    if not audio_files:
        log("No MP3/MP4 audio files found to transcribe.")
        return 0

    candidates = [f for f in audio_files if not _is_transcript_complete(f)]
    if getattr(args, "file", None):
        target = args.file.lower()
        candidates = [f for f in candidates if target in f.name.lower() or target in str(f).lower()]

    raw_backend = getattr(args, "backend", "auto")
    resolved_backend = _resolve_transcribe_backend(raw_backend)
    if (
        raw_backend == "auto"
        and not os.environ.get("TOLEDO_TRANSCRIBE_BACKEND", "").strip()
        and getattr(args, "report", None) is None
        and not getattr(args, "dry_run", False)
        and candidates
        and sys.stdin.isatty()
    ):
        resolved_backend = _prompt_transcribe_backend(bool(_cuda_device_count()), default_backend="auto")
    limit = getattr(args, "limit", None)
    if limit is not None and limit <= 0:
        limit = None
    max_minutes = getattr(args, "max_minutes", None)
    is_cloud = resolved_backend in ("gemini", "openai")
    if is_cloud and max_minutes is None:
        max_minutes = _DEFAULT_CLOUD_TRANSCRIBE_MAX_MINUTES

    jobs, skipped_by_cap, total_sec = _plan_transcribe_jobs(
        candidates,
        limit=limit,
        max_minutes=max_minutes if is_cloud else None,
        probe_duration=is_cloud,
    )
    to_transcribe = [j["path"] for j in jobs]

    log("\n=======================================================")
    log(f"Found {len(candidates)} pending audio file(s) (out of {len(audio_files)} total).")
    log(f"Backend: {resolved_backend} | Model: {args.model}")
    if skipped_by_cap:
        log(f"Selected {len(to_transcribe)} recording(s) for this batch ({skipped_by_cap} deferred by --limit/--max-minutes).")
    log("=======================================================\n")

    if is_cloud:
        default_cloud_model = _OPENAI_TRANSCRIBE_DEFAULT_MODEL if resolved_backend == "openai" else _GEMINI_TRANSCRIBE_DEFAULT_MODEL
        selected_model = default_cloud_model if not args.model or args.model == "auto" else args.model
        total_min = total_sec / 60.0
        total_hr = total_min / 60.0
        est_cost = _estimate_cloud_cost("transcribe", resolved_backend, selected_model, total_minutes=total_min)
        log(f"☁️  [Cloud Transcribe] Provider: {resolved_backend} ({selected_model})")
        log("    Data to upload: mono 16 kHz MP3 lecture audio chunks")
        log(f"    Selected recordings: {len(to_transcribe)} (~{total_min:.1f} min / {total_hr:.2f} hr; cap: {max_minutes:.0f} min)")
        log(f"    Estimated cost: {est_cost}")
        log("    Note: Cloud API usage may incur charges depending on your provider plan and region.")
        if getattr(args, "dry_run", False):
            for j in jobs:
                dur_label = f" ({j['duration_sec'] / 60.0:.1f} min)" if j["duration_sec"] > 0 else ""
                log(f"    • {j['path'].parent.name} / {j['path'].name}{dur_label}")
            log("    Dry run — no cloud API requests sent.")
            return 0
        if not to_transcribe:
            log("    Nothing selected — zero cloud API requests sent.")
            return 0
        auto_yes = getattr(args, "yes", False)
        if not auto_yes:
            if not sys.stdin.isatty():
                log("    Aborted: cloud transcription requires --yes in non-interactive mode.")
                return 1
            try:
                ans = input("    Proceed with cloud transcription? [y/N]: ").strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes"):
                log("    Cancelled — no cloud API requests sent.")
                return 0
        env_var = "OPENAI_API_KEY" if resolved_backend == "openai" else "GEMINI_API_KEY"
        if not _ensure_api_key_interactive(env_var, resolved_backend.title()):
            log(f"    Aborted: {env_var} is not set.")
            return 1

        raw_chunk_sec = getattr(args, "chunk_seconds", None)
        chunk_seconds = (
            max(30, min(_GEMINI_TRANSCRIBE_CHUNK_SECONDS, int(raw_chunk_sec)))
            if raw_chunk_sec is not None
            else _GEMINI_TRANSCRIBE_CHUNK_SECONDS
        )
        cloud_approval = _make_cloud_approval(
            feature="transcribe",
            provider=resolved_backend,
            model=selected_model,
            allowed_paths=to_transcribe,
            allow_video=False,
            data_types=("audio_chunks",),
            course_dirs=target_dirs,
            expected_hashes={j["path"]: _file_sha256(j["path"]) for j in jobs},
            max_duration_sec=float(max_minutes) * 60.0 if max_minutes else None,
            max_file_durations={j["path"]: j["duration_sec"] for j in jobs},
        )
        progress = getattr(args, "progress", None)
        success_count = 0
        transcribed_courses: set[Path] = set()
        for idx, f in enumerate(to_transcribe, 1):
            log(f"\n[{idx}/{len(to_transcribe)}] [{resolved_backend.title()}] {f.parent.name} / {f.name}")
            if progress:
                progress(f"Recording {idx}/{len(to_transcribe)} · {f.name}")
            try:
                if _transcribe_audio_via_gemini(
                    f,
                    model=selected_model,
                    language=args.lang,
                    cloud_approval=cloud_approval,
                    chunk_seconds=chunk_seconds,
                    course_dir=course_by_path.get(f),
                    provider=resolved_backend,
                ):
                    success_count += 1
                    if course_by_path.get(f):
                        transcribed_courses.add(course_by_path[f])
            except Exception as e:
                log(f"  ✗ Cloud transcription failed: {f.name} ({_redact_secrets(str(e))})")

        message = f"Transcribed {success_count}/{len(to_transcribe)} recording(s) via {resolved_backend.title()}."
        log(f"\n{message}")
        _reindex_after_standalone_transcribe(args, transcribed_courses)
        if getattr(args, "report", None) is not None:
            args.report["transcription_note"] = message
        return int(success_count != len(to_transcribe))

    if args.dry_run:
        for f in to_transcribe:
            log(f"  • {f.parent.name} / {f.name}")
        return 0

    if not to_transcribe:
        return 0

    success_count = 0
    transcribed_courses: set[Path] = set()
    gpu_count = _cuda_device_count()
    defer_cpu = getattr(args, "defer_cpu", False)
    explicit_cpu = resolved_backend == "cpu"
    explicit_gpu = resolved_backend == "gpu"
    device_arg = "cpu" if explicit_cpu else ("cuda" if explicit_gpu else "auto")
    allow_cpu = explicit_cpu or (resolved_backend == "local" and not defer_cpu)
    if not gpu_count:
        if resolved_backend == "gpu":
            message = f"{len(to_transcribe)} transcription(s) skipped: --backend gpu requested, but no CUDA GPU detected."
            log(message)
            if getattr(args, "report", None) is not None:
                args.report["transcription_note"] = message
            return 1
        if defer_cpu or not allow_cpu:
            message = (
                f"{len(to_transcribe)} transcription(s) deferred. No CUDA GPU detected. "
                "Set GEMINI_API_KEY and run 'toledo-dl transcribe --backend gemini' for fast cloud transcription, "
                "or run 'toledo-dl transcribe --backend cpu' to transcribe slowly on CPU."
            )
            log(message)
            if getattr(args, "report", None) is not None:
                args.report["transcription_note"] = message
            return 0
        log("No CUDA GPU detected. Local CPU transcription can take a long time for full lectures.")
    progress = getattr(args, "progress", None)

    if gpu_count > 1 and len(to_transcribe) > 1 and not explicit_cpu:
        import concurrent.futures
        import queue

        workers = min(gpu_count, len(to_transcribe))
        log(f"Using {workers} CUDA GPUs in parallel!")
        gpu_q = queue.Queue()
        for gid in range(workers):
            gpu_q.put(gid)

        def _worker(idx, path):
            gid = gpu_q.get()
            try:
                log(f"\n[{idx}/{len(to_transcribe)}] [GPU {gid}] {path.parent.name} / {path.name}")
                return transcribe_file(path, model_size=args.model, language=args.lang, device_index=gid, allow_cpu=allow_cpu, device=device_arg)
            finally:
                gpu_q.put(gid)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_worker, idx, f): f for idx, f in enumerate(to_transcribe, 1)}
            for idx, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                f = futs[fut]
                try:
                    if fut.result():
                        success_count += 1
                        if course_by_path.get(f):
                            transcribed_courses.add(course_by_path[f])
                except Exception as e:
                    log(f"  ✗ Worker error: {e}")
                if progress:
                    progress(f"{idx}/{len(to_transcribe)} recordings processed · {success_count} transcribed")
    else:
        for idx, f in enumerate(to_transcribe, 1):
            log(f"\n[{idx}/{len(to_transcribe)}] {f.parent.name} / {f.name}")
            if progress:
                progress(f"Recording {idx}/{len(to_transcribe)} · {f.name}")
            try:
                if transcribe_file(f, model_size=args.model, language=args.lang, device_index=0, allow_cpu=allow_cpu, device=device_arg):
                    success_count += 1
                    if course_by_path.get(f):
                        transcribed_courses.add(course_by_path[f])
            except Exception as e:
                log(f"  ✗ Transcription failed: {f.name} ({e})")

    _release_gpu_vram()
    message = f"Transcribed {success_count}/{len(to_transcribe)} recording(s)."
    log(f"\n{message}")
    _reindex_after_standalone_transcribe(args, transcribed_courses)
    if getattr(args, "report", None) is not None:
        args.report["transcription_note"] = message
    return int(success_count != len(to_transcribe))


# ---------------------------------------------------------------- LLM Dataset / Markdown Index
_SUPPORTED_DOC_TEXT_EXTS = {
    ".md", ".txt", ".do", ".r", ".sas", ".py", ".sql", ".csv", ".tsv", ".rmd", ".qmd", ".ipynb",
}
_MEDIA_EXTS = (".mp3", ".mp4", ".mov", ".m4v", ".mkv", ".webm")


def _is_incomplete_transcript_txt(path: Path) -> bool:
    """Return True if a .txt file belongs to an in-progress or partially committed transcript."""
    if path.suffix.lower() != ".txt":
        return False
    if _transcript_inprogress_marker(path).exists():
        return True
    if _transcript_done_marker(path).exists() and not path.with_suffix(".srt").exists():
        return True
    return False


def _is_transcript_txt(path: Path) -> bool:
    if path.suffix.lower() != ".txt":
        return False
    if _transcript_inprogress_marker(path).exists():
        return False
    done_marker = _transcript_done_marker(path)
    if done_marker.exists():
        return path.with_suffix(".srt").exists()
    return path.with_suffix(".srt").exists() or any(path.with_suffix(v).exists() for v in _MEDIA_EXTS)


def _compute_source_id(course_name: str, rel_path: str) -> str:
    norm = f"{course_name}/{rel_path.replace(chr(92), '/')}"
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _parse_srt_segments(srt_path: Path) -> list[dict]:
    """Parse an .srt file into timestamped subtitle segments."""
    if not srt_path.exists():
        return []
    raw = srt_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    blocks = re.split(r"\n{2,}", raw.strip())
    segments: list[dict] = []
    ts_re = re.compile(r"(\d{2}:\d{2}:\d{2})(?:[,.]\d+)?\s*-->\s*(\d{2}:\d{2}:\d{2})(?:[,.]\d+)?")
    for idx, blk in enumerate(blocks, 1):
        lines = [ln.strip() for ln in blk.splitlines() if ln.strip()]
        if not lines:
            continue
        ts_line_idx = 0
        if lines[0].isdigit() and len(lines) >= 2:
            ts_line_idx = 1
        m = ts_re.search(lines[ts_line_idx]) if ts_line_idx < len(lines) else None
        if not m:
            continue
        text = " ".join(lines[ts_line_idx + 1:]).strip()
        if text:
            segments.append({
                "index": len(segments) + 1,
                "start_ts": m.group(1),
                "end_ts": m.group(2),
                "text": text,
            })
    return segments


def _extract_docx_text(docx_path: Path) -> str:
    """Extract paragraph text from a .docx file using stdlib zipfile and ElementTree."""
    try:
        with zipfile.ZipFile(docx_path, "r") as zf:
            xml_bytes = zf.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        paras: list[str] = []
        for p_elem in root.iter():
            if p_elem.tag.endswith("}p") or p_elem.tag == "p":
                texts = [
                    t_elem.text
                    for t_elem in p_elem.iter()
                    if (t_elem.tag.endswith("}t") or t_elem.tag == "t") and t_elem.text
                ]
                joined = "".join(texts).strip()
                if joined:
                    paras.append(joined)
        return "\n\n".join(paras)
    except Exception:
        return ""


def _extract_document_text(file_path: Path) -> tuple[str, str]:
    """Extract readable text from handouts/scripts/notebooks; return (text, status)."""
    ext = file_path.suffix.lower()
    if ext == ".docx":
        txt = _extract_docx_text(file_path).strip()
        return (txt, "ok" if txt else "empty_or_unreadable")
    if ext == ".ipynb":
        try:
            nb = json.loads(file_path.read_text(encoding="utf-8", errors="replace"))
            cells = nb.get("cells") or []
            parts = []
            for cell in cells:
                if not isinstance(cell, dict):
                    continue
                src = cell.get("source")
                txt_cell = "".join(src) if isinstance(src, list) else str(src or "")
                if txt_cell.strip():
                    parts.append(txt_cell.strip())
            joined = "\n\n".join(parts)
            return (joined, "ok" if joined else "empty")
        except Exception:
            return ("", "unreadable")
    if ext in _SUPPORTED_DOC_TEXT_EXTS:
        try:
            txt = file_path.read_text(encoding="utf-8", errors="replace").strip()
            return (txt[:200000], "ok" if txt else "empty")
        except Exception:
            return ("", "unreadable")
    return ("", f"unsupported:{ext or 'none'}")


def _md_matches_source(md_path: Path, rel_source: str) -> bool:
    if not md_path.exists():
        return False
    try:
        head = md_path.read_text(encoding="utf-8", errors="replace")[:1000]
        return f"source: {json.dumps(rel_source)}" in head
    except Exception:
        return False


_PDF_VISION_GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
_PDF_VISION_OLLAMA_DEFAULT_MODEL = "qwen2.5vl:7b"
_PDF_VISION_CHUNK_PAGES = 20
_PDF_VISION_MAX_INLINE_BYTES = 18 * 1024 * 1024

_PDF_VISION_PROMPT = (
    "Convert these university lecture PDF slides into clean, structured Markdown, slide by slide.\n"
    "Rules:\n"
    "- Keep the original language of the slides (do not translate).\n"
    "- Do NOT summarize, paraphrase, or skip any content. Include all headings, bullet points, footnotes, figure/chapter references, and speaker notes.\n"
    "- Label each slide clearly starting with `## Slide N: [Title]` using the exact slide numbers.\n"
    "- For diagrams, flowcharts, or reaction pathways: preserve every label, number, and arrow direction explicitly using arrow notation (e.g. `glucose -> glycogeen (via insuline)`), and note any asterisks or special conditions.\n"
    "- For tables: recreate them as Markdown tables.\n"
    "- For purely visual images (photos, anatomy, graphs): briefly describe what is shown and list all labeled parts or axes/trends.\n"
    "- If anything is blurry or unreadable, mark it as `[unreadable]` instead of guessing.\n"
    "- Output ONLY the Markdown content."
)


def _strip_outer_markdown_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            cleaned = "\n".join(lines[1:-1]).strip()
    return cleaned


def _parse_vision_markdown_pages(md_text: str) -> list[str]:
    """Split slide-by-slide vision Markdown into per-slide text chunks for passages.jsonl."""
    cleaned = _strip_outer_markdown_fence(md_text)
    if not cleaned:
        return []
    parts = re.split(r"(?m)^(?=##\s+(?:Slide|Page)\s+\d+\b)", cleaned)
    pages = [p.strip() for p in parts if p.strip()]
    return pages if pages else [cleaned]


def _read_cached_vision_markdown(
    md_dest: Path,
    rel_source: str,
    pdf_sha: str,
    *,
    expected_backend: str | None = None,
    expected_model: str | None = None,
) -> tuple[bool, list[str], str]:
    """Return cached vision Markdown if md_dest matches rel_source, pdf_sha, and (optionally) backend/model."""
    if not md_dest.is_file() or md_dest.is_symlink():
        return False, [], ""
    try:
        raw = md_dest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, [], ""
    if not raw.startswith("---\n"):
        return False, [], ""
    end_idx = raw.find("\n---\n", 4)
    if end_idx == -1:
        return False, [], ""
    fm_block = raw[4:end_idx]
    body = raw[end_idx + 5:].strip()
    meta: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            try:
                meta[k] = str(json.loads(v))
            except Exception:
                meta[k] = v.strip("\"'")
    if meta.get("source") != rel_source:
        return False, [], ""
    cached_backend = meta.get("vision_backend", "").strip().lower()
    if cached_backend not in ("ollama", "gemini"):
        return False, [], ""
    if pdf_sha and meta.get("source_sha256") != pdf_sha:
        return False, [], ""
    if expected_backend and expected_backend != "off":
        if cached_backend != expected_backend:
            return False, [], ""
        if expected_model and meta.get("vision_model", "").strip() != expected_model.strip():
            return False, [], ""
    # Strip top `# Title` line before splitting into per-slide passages
    body_no_title = re.sub(r"^# [^\n]+\n+", "", body, count=1).strip()
    pages = _parse_vision_markdown_pages(body_no_title)
    if not pages:
        return False, [], ""
    return True, pages, "vision_ok"


def _verified_pdf_page_count(pdf_path: Path) -> int | None:
    """Return an authoritative PDF page count using pypdf or pdfinfo, or None if unverifiable."""
    if pypdf:
        try:
            return len(pypdf.PdfReader(str(pdf_path)).pages)
        except Exception:
            return None
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        try:
            proc = subprocess.run(
                [pdfinfo, str(pdf_path)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0:
                text = proc.stdout.decode("utf-8", errors="replace")
                m = re.search(r"(?m)^Pages:\s*(\d+)\s*$", text)
                if m:
                    return int(m.group(1))
        except Exception:
            return None
    return None


def _count_pdf_pages(pdf_path: Path) -> int:
    verified = _verified_pdf_page_count(pdf_path)
    return verified if verified is not None else 0


def _slice_pdf_page_chunks(pdf_path: Path, chunk_pages: int = _PDF_VISION_CHUNK_PAGES) -> list[tuple[int, int, bytes]]:
    """Slice a PDF into in-memory page chunks for Gemini Vision (requires verified page count; fails closed)."""
    step = max(1, int(chunk_pages))
    if pypdf:
        reader = pypdf.PdfReader(str(pdf_path))
        total = len(reader.pages)
        if total == 0:
            return []
        if total <= step:
            raw = pdf_path.read_bytes()
            if len(raw) <= _PDF_VISION_MAX_INLINE_BYTES:
                return [(1, total, raw)]
        chunks: list[tuple[int, int, bytes]] = []
        for start in range(0, total, step):
            end = min(total, start + step)
            writer = pypdf.PdfWriter()
            for idx in range(start, end):
                writer.add_page(reader.pages[idx])
            buf = io.BytesIO()
            writer.write(buf)
            chunks.append((start + 1, end, buf.getvalue()))
        return chunks
    verified_pages = _verified_pdf_page_count(pdf_path)
    if verified_pages is None or verified_pages <= 0 or verified_pages > step:
        raise RuntimeError(
            f"pypdf is required to verify and slice {pdf_path.name} within the {step}-page chunk limit for Gemini Vision."
        )
    return [(1, verified_pages, pdf_path.read_bytes())]


def _call_gemini_pdf_vision_chunk(
    pdf_bytes: bytes,
    *,
    model: str,
    api_key: str,
    start_page: int,
    end_page: int,
) -> str:
    """Send one PDF page chunk to Gemini generateContent via header auth and return structured Markdown."""
    if len(pdf_bytes) > _PDF_VISION_MAX_INLINE_BYTES:
        raise RuntimeError(
            f"PDF chunk ({len(pdf_bytes)} bytes) exceeds inline safety limit ({_PDF_VISION_MAX_INLINE_BYTES} bytes)."
        )
    range_hint = (
        f"This chunk contains slides {start_page} to {end_page}. Number the slides starting at {start_page}."
        if end_page >= start_page and end_page > 0
        else "Number each slide sequentially starting at Slide 1."
    )
    payload = json.dumps({
        "contents": [{
            "role": "user",
            "parts": [
                {"text": f"{_PDF_VISION_PROMPT}\n\n{range_hint}"},
                {
                    "inlineData": {
                        "mimeType": "application/pdf",
                        "data": base64.b64encode(pdf_bytes).decode("ascii"),
                    }
                },
            ],
        }],
        "generationConfig": {"temperature": 0.0},
    }).encode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model, safe='')}:generateContent"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini Vision returned no candidates for PDF chunk.")
    finish_reason = str(candidates[0].get("finishReason", "")).upper()
    if finish_reason == "MAX_TOKENS":
        raise _GeminiMaxTokensError("Gemini PDF vision output truncated (MAX_TOKENS); refusing to cache incomplete deck.")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    raw_text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text"))
    cleaned = _strip_outer_markdown_fence(raw_text)
    if not cleaned:
        raise RuntimeError("Gemini Vision returned empty Markdown for PDF chunk.")
    return cleaned


def _extract_pdf_via_gemini_vision(
    pdf_path: Path,
    *,
    model: str | None = None,
    cloud_approval: dict | None = None,
    course_dir: Path | None = None,
) -> tuple[str, list[str]]:
    """Convert a PDF to structured arrow-notation Markdown via approved Gemini Vision API."""
    selected_model = (model or os.environ.get("TOLEDO_PDF_VISION_GEMINI_MODEL") or _PDF_VISION_GEMINI_DEFAULT_MODEL).strip()
    if not _is_cloud_approved(
        cloud_approval,
        "pdf-vision",
        "gemini",
        pdf_path,
        model=selected_model,
        data_types=("pdf_pages",),
        course_dir=course_dir,
    ):
        raise PermissionError(f"Refused cloud PDF vision upload for {pdf_path.name}: explicit approval missing.")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")

    chunks = _slice_pdf_page_chunks(pdf_path, chunk_pages=_PDF_VISION_CHUNK_PAGES)
    if not chunks:
        raise RuntimeError(f"No pages found in {pdf_path.name}.")
    md_parts: list[str] = []
    for idx, (s_pg, e_pg, chunk_bytes) in enumerate(chunks, 1):
        label = f"pages {s_pg}-{e_pg}" if e_pg > 0 else "full PDF"
        log(f"  ☁️ [PDF Vision/Gemini] Scanning {pdf_path.name} ({label}, chunk {idx}/{len(chunks)})...")
        md_parts.append(
            _call_gemini_pdf_vision_chunk(
                chunk_bytes,
                model=selected_model,
                api_key=api_key,
                start_page=s_pg,
                end_page=e_pg,
            )
        )
    full_md = "\n\n".join(md_parts).strip()
    return full_md, _parse_vision_markdown_pages(full_md)


def _render_pdf_pages_to_pngs(pdf_path: Path, out_dir: Path, dpi: int = 150) -> list[Path]:
    """Render PDF pages to PNG images using pdftoppm (poppler-utils)."""
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise RuntimeError("pdftoppm (poppler-utils) is required for local GPU PDF vision scanning.")
    prefix = out_dir / "page"
    proc = subprocess.run(
        [pdftoppm, "-png", "-r", str(dpi), str(pdf_path), str(prefix)],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pdftoppm failed rendering {pdf_path.name}.")
    return sorted(out_dir.glob("page-*.png"))


def _call_ollama_pdf_vision_page(image_bytes: bytes, *, model: str, page_num: int) -> str:
    """Send a single rendered PDF page PNG to local Ollama vision model."""
    prompt = (
        f"{_PDF_VISION_PROMPT}\n\n"
        f"This image is Slide {page_num}. Start with `## Slide {page_num}: [Title]`."
    )
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(image_bytes).decode("ascii")],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 2048},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    # Bypass HTTP/HTTPS proxy env vars so local slide PNGs never leave loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    cleaned = _strip_outer_markdown_fence(str(data.get("response") or ""))
    if not cleaned:
        raise RuntimeError(f"Ollama vision returned empty output for slide {page_num}.")
    return cleaned


def _extract_pdf_via_ollama_vision(pdf_path: Path, *, model: str | None = None) -> tuple[str, list[str]]:
    """Convert a PDF to structured arrow-notation Markdown locally using Ollama vision (GPU)."""
    selected_model = (model or os.environ.get("TOLEDO_PDF_VISION_OLLAMA_MODEL") or _PDF_VISION_OLLAMA_DEFAULT_MODEL).strip()
    fallback_pages: list[str] = []
    if pypdf:
        try:
            fallback_pages = [(page.extract_text() or "").strip() for page in pypdf.PdfReader(str(pdf_path)).pages]
        except Exception:
            fallback_pages = []
    with tempfile.TemporaryDirectory(prefix="toledo_pdf_vis_") as tmp_str:
        pngs = _render_pdf_pages_to_pngs(pdf_path, Path(tmp_str))
        if not pngs:
            raise RuntimeError(f"No pages rendered for {pdf_path.name}.")
        slide_texts: list[str] = []
        vision_ok_count = 0
        last_err: Exception | None = None
        for idx, png in enumerate(pngs, 1):
            log(f"  🖥️ [PDF Vision/Ollama] Scanning {pdf_path.name} slide {idx}/{len(pngs)} ({selected_model})...")
            try:
                slide_texts.append(_call_ollama_pdf_vision_page(png.read_bytes(), model=selected_model, page_num=idx))
                vision_ok_count += 1
            except Exception as e:
                last_err = e
                fb = fallback_pages[idx - 1] if idx - 1 < len(fallback_pages) else ""
                if fb:
                    slide_texts.append(f"## Slide {idx}\n\n{fb}")
                else:
                    slide_texts.append(f"## Slide {idx}\n\n_[Ollama vision unavailable for this slide: {e}]_")
                log(f"  ⚠️ [PDF Vision/Ollama] Slide {idx}/{len(pngs)} of {pdf_path.name} failed ({e}); keeping slide fallback.")
        if vision_ok_count == 0 and last_err is not None:
            raise last_err
    full_md = "\n\n".join(slide_texts).strip()
    return full_md, slide_texts


def _ollama_list_models() -> tuple[bool, set[str]]:
    """Return (ollama_running, installed_model_names) via loopback without proxy routing."""
    req = urllib.request.Request("http://localhost:11434/api/tags")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = {
            str(m.get("name") or "").strip()
            for m in (data.get("models") or [])
            if isinstance(m, dict) and m.get("name")
        }
        return True, names
    except Exception:
        return False, set()


def _check_local_pdf_vision_deps(model: str) -> dict:
    """Inspect local GPU PDF vision prerequisites (pdftoppm, ollama CLI/daemon, vision model)."""
    has_pdftoppm = bool(shutil.which("pdftoppm"))
    has_ollama_cli = bool(shutil.which("ollama"))
    running, models = _ollama_list_models()
    model_norm = (model or _PDF_VISION_OLLAMA_DEFAULT_MODEL).strip()
    model_present = running and any(
        m == model_norm or (":" not in model_norm and m.startswith(f"{model_norm}:"))
        for m in models
    )
    return {
        "pdftoppm": has_pdftoppm,
        "ollama_cli": has_ollama_cli,
        "ollama_running": running,
        "model_present": model_present,
        "model": model_norm,
    }


def _install_local_vision_package(kind: str) -> bool:
    """Best-effort OS package install for 'poppler' or 'ollama'."""
    if kind == "poppler":
        if sys.platform == "win32":
            cmd = ["winget", "install", "--id", "oschwartz10612.Poppler", "--accept-source-agreements", "--accept-package-agreements"]
        elif sys.platform == "darwin":
            cmd = ["brew", "install", "poppler"]
        elif shutil.which("apt-get"):
            cmd = ["sh", "-c", "sudo apt-get update && sudo apt-get install -y poppler-utils"]
        else:
            log("    ⚠️ Could not auto-install poppler-utils (no apt-get/brew/winget). Install poppler-utils manually.")
            return False
    else:
        if sys.platform == "win32":
            cmd = ["winget", "install", "--id", "Ollama.Ollama", "--accept-source-agreements", "--accept-package-agreements"]
        elif sys.platform == "darwin":
            cmd = ["brew", "install", "ollama"]
        elif shutil.which("curl"):
            cmd = ["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"]
        else:
            log("    ⚠️ Could not auto-install Ollama (curl not found). Download from https://ollama.com/download")
            return False
    if not shutil.which(cmd[0]):
        log(f"    ⚠️ Installer '{cmd[0]}' not found on PATH.")
        return False
    log(f"    ⬇️ Installing {kind} via {cmd[0]}...")
    proc = subprocess.run(cmd, check=False)
    return proc.returncode == 0


def _ensure_local_pdf_vision_setup(
    model: str,
    *,
    interactive: bool = False,
    auto_install: bool = False,
) -> bool:
    """Check local GPU PDF vision dependencies and optionally download/install missing pieces on user confirmation."""
    status = _check_local_pdf_vision_deps(model)
    if status["pdftoppm"] and status["ollama_running"] and status["model_present"]:
        return True

    missing: list[str] = []
    if not status["pdftoppm"]:
        missing.append("poppler-utils (pdftoppm)")
    if not status["ollama_cli"] and not status["ollama_running"]:
        missing.append("Ollama")
    elif not status["ollama_running"]:
        missing.append("Ollama background service")
    if not status["model_present"]:
        missing.append(f"Ollama vision model '{status['model']}'")

    log("  ℹ️ [Local GPU PDF Vision] Missing setup: " + ", ".join(missing))
    if not auto_install:
        if not interactive:
            log("    Run interactively or pass --yes to download and set up missing local GPU PDF vision dependencies.")
            return False
        try:
            ans = input("    Download and set up missing local GPU PDF vision dependencies now? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            log("    Skipped local GPU vision setup — falling back to standard text extraction.")
            return False

    if not status["pdftoppm"]:
        _install_local_vision_package("poppler")
    if not status["ollama_cli"] and not status["ollama_running"]:
        _install_local_vision_package("ollama")

    ollama_bin = shutil.which("ollama")
    if ollama_bin and not _ollama_list_models()[0]:
        log("    🚀 Starting Ollama service...")
        try:
            subprocess.Popen(
                [ollama_bin, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(10):
                time.sleep(1)
                if _ollama_list_models()[0]:
                    break
        except OSError as e:
            log(f"    ⚠️ Could not start Ollama service: {e}")

    status_after_start = _check_local_pdf_vision_deps(model)
    if ollama_bin and status_after_start["ollama_running"] and not status_after_start["model_present"]:
        log(f"    ⬇️ Pulling Ollama vision model '{status_after_start['model']}'...")
        subprocess.run([ollama_bin, "pull", status_after_start["model"]], check=False)

    final = _check_local_pdf_vision_deps(model)
    ready = bool(final["pdftoppm"] and final["ollama_running"] and final["model_present"])
    if not ready:
        log("    ⚠️ Local GPU PDF vision setup incomplete; falling back to standard text extraction.")
    return ready


def _plan_pdf_vision_jobs(
    target_dirs: list[Path],
    *,
    vision_backend: str,
    vision_model: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Build the single explicit PDF vision job list (deduplicated per course by SHA-256, skipping cached vision PDFs)."""
    selected_model = (
        (vision_model or os.environ.get("TOLEDO_PDF_VISION_GEMINI_MODEL") or _PDF_VISION_GEMINI_DEFAULT_MODEL).strip()
        if vision_backend == "gemini"
        else (vision_model or os.environ.get("TOLEDO_PDF_VISION_OLLAMA_MODEL") or _PDF_VISION_OLLAMA_DEFAULT_MODEL).strip()
    )
    jobs: list[dict] = []
    for cdir in target_dirs:
        if not cdir.is_dir() or not _is_path_within_dir(cdir, OUT):
            continue
        seen_shas: set[str] = set()
        pdf_md_dir = cdir / "LLM_Knowledge" / "pdfs"
        pdf_files = [
            f for f in sorted(cdir.rglob("*"))
            if f.is_file()
            and f.suffix.lower() == ".pdf"
            and not f.name.startswith(".")
            and _is_path_within_dir(f, cdir)
            and "LLM_Knowledge" not in f.parts
            and "Transcripts & Slides" not in f.parts
            and "Flashcards" not in f.parts
        ]
        pdf_stem_counts = Counter(clean_filename(f.stem) for f in pdf_files)
        for f in pdf_files:
            if limit is not None and limit > 0 and len(jobs) >= limit:
                return jobs
            rel = str(f.relative_to(cdir)).replace("\\", "/")
            sha = _file_sha256(f)
            if sha and sha in seen_shas:
                continue
            md_out = _unique_md_dest(pdf_md_dir, f, rel, cdir, cdir.name, pdf_stem_counts)
            cached_ok, _, _ = _read_cached_vision_markdown(
                md_out,
                rel,
                sha,
                expected_backend=vision_backend,
                expected_model=selected_model,
            )
            if cached_ok:
                if sha:
                    seen_shas.add(sha)
                continue
            if sha:
                seen_shas.add(sha)
            jobs.append({
                "course_dir": cdir,
                "pdf_path": f,
                "rel_source": rel,
                "sha256": sha,
                "pages": _count_pdf_pages(f),
            })
    return jobs


def _extract_pdf_markdown_with_pages(
    pdf_path: Path,
    md_dest: Path,
    course_name: str,
    rel_source: str,
    *,
    vision_backend: str = "off",
    vision_model: str | None = None,
    cloud_approval: dict | None = None,
    course_dir: Path | None = None,
    allow_vision: bool = True,
) -> tuple[bool, list[str], str]:
    """Extract PDF text (or opt-in local/cloud vision Markdown) and return (ok, per_page_texts, extraction_status)."""
    v_backend = (vision_backend or "off").strip().lower()
    pdf_sha = _file_sha256(pdf_path)
    expected_model = None
    if v_backend == "gemini":
        expected_model = (vision_model or os.environ.get("TOLEDO_PDF_VISION_GEMINI_MODEL") or _PDF_VISION_GEMINI_DEFAULT_MODEL).strip()
    elif v_backend == "ollama":
        expected_model = (vision_model or os.environ.get("TOLEDO_PDF_VISION_OLLAMA_MODEL") or _PDF_VISION_OLLAMA_DEFAULT_MODEL).strip()

    # Preserve existing valid vision Markdown cache (even during normal vision_backend='off' reindexes)
    cached_ok, cached_pages, cached_status = _read_cached_vision_markdown(
        md_dest,
        rel_source,
        pdf_sha,
        expected_backend=None if v_backend == "off" else v_backend,
        expected_model=expected_model,
    )
    if cached_ok:
        return True, cached_pages, cached_status

    if allow_vision and v_backend in ("ollama", "gemini"):
        try:
            if v_backend == "gemini":
                selected_model = (vision_model or os.environ.get("TOLEDO_PDF_VISION_GEMINI_MODEL") or _PDF_VISION_GEMINI_DEFAULT_MODEL).strip()
                body, page_texts = _extract_pdf_via_gemini_vision(
                    pdf_path,
                    model=selected_model,
                    cloud_approval=cloud_approval,
                    course_dir=course_dir,
                )
            else:
                selected_model = (vision_model or os.environ.get("TOLEDO_PDF_VISION_OLLAMA_MODEL") or _PDF_VISION_OLLAMA_DEFAULT_MODEL).strip()
                body, page_texts = _extract_pdf_via_ollama_vision(pdf_path, model=selected_model)
            frontmatter = (
                f"---\n"
                f"course: {json.dumps(course_name)}\n"
                f"type: pdf\n"
                f"source: {json.dumps(rel_source)}\n"
                f"source_sha256: {json.dumps(pdf_sha)}\n"
                f"vision_backend: {json.dumps(v_backend)}\n"
                f"vision_model: {json.dumps(selected_model)}\n"
                f"pages: {len(page_texts)}\n"
                f"---\n\n"
                f"# {pdf_path.stem}\n\n"
            )
            root_guard = course_dir if course_dir is not None else md_dest.parent
            md_dest.parent.mkdir(parents=True, exist_ok=True)
            if _safe_write_text_within(md_dest, root_guard, frontmatter + body + "\n"):
                return True, page_texts, "vision_ok"
            return False, [], "unsafe_path"
        except Exception as e:
            log(f"  ⚠️ [PDF Vision/{v_backend}] Could not vision-scan {pdf_path.name} ({e}); falling back to local text extraction.")

    if not pypdf:
        return False, [], "pypdf_unavailable"
    try:
        reader = pypdf.PdfReader(str(pdf_path))
        page_texts = [(page.extract_text() or "").strip() for page in reader.pages]
        status = "ok" if any(page_texts) else "visual_only"

        if md_dest.exists() and md_dest.stat().st_mtime >= pdf_path.stat().st_mtime and _md_matches_source(md_dest, rel_source):
            return True, page_texts, status

        pages_md = [f"## Page {idx}\n\n{txt}" for idx, txt in enumerate(page_texts, 1) if txt]
        body = "\n\n".join(pages_md) if pages_md else "_No extractable text found in PDF._"
        frontmatter = (
            f"---\n"
            f"course: {json.dumps(course_name)}\n"
            f"type: pdf\n"
            f"source: {json.dumps(rel_source)}\n"
            f"pages: {len(page_texts)}\n"
            f"---\n\n"
            f"# {pdf_path.stem}\n\n"
        )
        root_guard = course_dir if course_dir is not None else md_dest.parent
        md_dest.parent.mkdir(parents=True, exist_ok=True)
        if _safe_write_text_within(md_dest, root_guard, frontmatter + body + "\n"):
            return True, page_texts, status
        return False, [], "unsafe_path"
    except Exception:
        return False, [], "unreadable"


def _extract_pdf_markdown(pdf_path, md_dest, course_name, rel_source):
    """Extracts PDF text into a structured Markdown file with YAML frontmatter."""
    ok, _, _ = _extract_pdf_markdown_with_pages(pdf_path, md_dest, course_name, rel_source)
    return ok


def _unique_md_dest(md_dir: Path, src_file: Path, rel_source: str, cdir: Path, course_name: str, stem_counts: Counter) -> Path:
    base_stem = clean_filename(src_file.stem)
    if stem_counts[base_stem] > 1:
        rel_parent = str(src_file.parent.relative_to(cdir)).replace("\\", "/") if src_file.parent != cdir else "root"
        slug_parent = clean_filename(rel_parent.replace("/", "_"))
        cand = md_dir / f"{slug_parent}__{base_stem}.md"
    else:
        cand = md_dir / f"{base_stem}.md"
    if cand.exists() and not _md_matches_source(cand, rel_source):
        cand = md_dir / f"{cand.stem}_{_compute_source_id(course_name, rel_source)[:6]}.md"
    return cand


def _cleanup_stale_chapter_links(chap_dir: Path, keep_names: set[str]) -> None:
    preserve = {
        "flashcards.json",
        "concepts_checklist.json",
        "flashcards_concepts.json",
        "cache_meta.json",
        "report.md",
        "concepts_partial.json",
        "flashcards_partial.json",
        "CHAPTER_BUNDLE.md",
        "passages.jsonl",
        ".chapter_meta.json",
    }
    for item in list(chap_dir.iterdir()):
        if item.name in preserve or item.name in keep_names:
            continue
        if item.is_file() or item.is_symlink():
            try:
                item.unlink()
            except OSError:
                pass


def _repair_and_build_chapter_bundles(
    cdir: Path,
    kb_dir: Path,
    pdf_md_by_rel: dict[str, Path] | None = None,
    course_passages: list[dict] | None = None,
):
    """Repair symlinks, group distinct PDFs/standalone materials into stable chapter bundles under LLM_Knowledge/Chapters/."""
    ts_dir = cdir / "Transcripts & Slides"
    pdf_md_map = pdf_md_by_rel or {}

    real_files_by_name: dict[str, Path] = {}
    all_pdfs: list[Path] = []
    all_txts: list[Path] = []
    all_docs: list[Path] = []

    for f in sorted(cdir.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        if "LLM_Knowledge" in f.parts or "Transcripts & Slides" in f.parts:
            continue
        if not _is_path_within_dir(f, cdir) or _is_incomplete_transcript_txt(f):
            continue
        real_files_by_name.setdefault(f.name, f)
        if "Announcements" in f.parts:
            continue
        ext = f.suffix.lower()
        if ext == ".pdf":
            all_pdfs.append(f)
        elif _is_transcript_txt(f):
            all_txts.append(f)
        elif (
            f.name not in ("INDEX.md", "Announcements.md", "Links.md")
            and ext not in (".srt", ".vtt", ".url", *_MEDIA_EXTS)
            and (ext == ".docx" or ext in _SUPPORTED_DOC_TEXT_EXTS)
        ):
            doc_txt, _ = _extract_document_text(f)
            if doc_txt.strip():
                all_docs.append(f)

    pdf_hashes: dict[Path, str] = {}
    for p in all_pdfs:
        pdf_hashes[p] = _file_sha256(p)[:16]

    def _pdf_unit_key(pdf_p: Path) -> tuple[str, str]:
        sha = pdf_hashes.get(pdf_p) or _file_sha256(pdf_p)[:16]
        try:
            rel_p = str(pdf_p.relative_to(cdir)).replace("\\", "/")
        except ValueError:
            rel_p = pdf_p.name
        if not rel_p.startswith("Transcripts & Slides/"):
            return f"pdf:{rel_p}:{sha}", rel_p
        real_matches = [rf for rf in all_pdfs if rf.name == pdf_p.name and pdf_hashes.get(rf) == sha]
        if len(real_matches) == 1:
            real_rel = str(real_matches[0].relative_to(cdir)).replace("\\", "/")
            return f"pdf:{real_rel}:{sha}", real_rel
        return f"pdf:{rel_p}:{sha}", rel_p

    # 1. Repair broken symlinks in Transcripts & Slides/ and collect distinct PDF units
    units_by_key: dict[str, dict] = {}
    paired_pdf_rels: set[str] = set()
    paired_txts: set[Path] = set()

    if ts_dir.exists():
        for sub in sorted(ts_dir.iterdir()):
            if not sub.is_dir():
                continue
            sub_pdfs: list[Path] = []
            sub_txts: list[Path] = []
            for item in sorted(sub.iterdir()):
                if item.is_symlink() and not item.exists():
                    target = real_files_by_name.get(item.name)
                    if not target:
                        target = next(
                            (rf for rf in real_files_by_name.values() if rf.suffix == item.suffix and rf.stem.startswith(item.stem + " - ")),
                            None,
                        )
                    if target:
                        try:
                            item.unlink()
                            new_link = sub / target.name
                            if not new_link.exists():
                                link_or_copy(target, new_link)
                            item = new_link
                        except Exception:
                            continue
                    else:
                        continue
                if not item.exists():
                    continue
                resolved = item.resolve()
                if not _is_path_within_dir(resolved, cdir):
                    continue
                if item.suffix.lower() == ".pdf":
                    sub_pdfs.append(resolved)
                elif item.suffix.lower() == ".txt" and not _is_incomplete_transcript_txt(resolved):
                    sub_txts.append(resolved)

            for pdf_p in sub_pdfs:
                ukey, rel_src = _pdf_unit_key(pdf_p)
                paired_pdf_rels.add(rel_src)
                origin_label = (
                    pdf_p.parent.name
                    if not rel_src.startswith("Transcripts & Slides/") and pdf_p.parent != cdir
                    else sub.name
                )
                unit = units_by_key.setdefault(ukey, {
                    "unit_key": ukey,
                    "kind": "pdf",
                    "pdf_src": pdf_p,
                    "rel_src": rel_src,
                    "base_stem": clean_filename(pdf_p.stem),
                    "origin_label": origin_label,
                    "txts": [],
                    "docs": [],
                })
                for t in sub_txts:
                    if t not in unit["txts"]:
                        unit["txts"].append(t)
                    paired_txts.add(t)

    # 2. Include unpaired PDFs, unpaired transcripts, and standalone readable handouts/scripts
    for pdf_p in all_pdfs:
        ukey, rel_src = _pdf_unit_key(pdf_p)
        if ukey not in units_by_key and rel_src not in paired_pdf_rels:
            origin_label = pdf_p.parent.name if pdf_p.parent != cdir else "Main"
            units_by_key[ukey] = {
                "unit_key": ukey,
                "kind": "pdf",
                "pdf_src": pdf_p,
                "rel_src": rel_src,
                "base_stem": clean_filename(pdf_p.stem),
                "origin_label": origin_label,
                "txts": [],
                "docs": [],
            }

    for t_p in all_txts:
        resolved_t = t_p.resolve()
        if resolved_t in paired_txts or t_p in paired_txts:
            continue
        rel_t = str(t_p.relative_to(cdir)).replace("\\", "/")
        ukey = f"txt:{rel_t}"
        origin_label = t_p.parent.name if t_p.parent != cdir else "Main"
        units_by_key[ukey] = {
            "unit_key": ukey,
            "kind": "transcript",
            "pdf_src": None,
            "rel_src": rel_t,
            "base_stem": clean_filename(t_p.stem),
            "origin_label": origin_label,
            "txts": [t_p],
            "docs": [],
        }

    docs_by_folder: dict[str, list[Path]] = {}
    for doc_p in all_docs:
        rel_parent = str(doc_p.parent.relative_to(cdir)).replace("\\", "/") if doc_p.parent != cdir else "Main"
        docs_by_folder.setdefault(rel_parent, []).append(doc_p)

    for rel_parent, doc_list in sorted(docs_by_folder.items()):
        ukey = f"docs:{rel_parent}"
        stem_label = clean_filename(doc_list[0].stem if len(doc_list) == 1 else f"Materials_{rel_parent.replace('/', '_')}")
        units_by_key[ukey] = {
            "unit_key": ukey,
            "kind": "docs",
            "pdf_src": None,
            "rel_src": rel_parent,
            "base_stem": stem_label,
            "origin_label": rel_parent.replace("/", "_"),
            "txts": [],
            "docs": sorted(doc_list),
        }

    if not units_by_key:
        return

    kind_order = {"pdf": 0, "transcript": 1, "docs": 2}
    ordered_units = sorted(
        units_by_key.values(),
        key=lambda u: (kind_order.get(u["kind"], 9), u["base_stem"].lower(), u["origin_label"].lower(), u["unit_key"]),
    )
    stem_counts = Counter(u["base_stem"].lower() for u in ordered_units)
    used_chap_stems: set[str] = set()
    for u in ordered_units:
        b_stem = u["base_stem"]
        if stem_counts[b_stem.lower()] > 1:
            cand = f"{clean_filename(u['origin_label'])}_{b_stem}"
        else:
            cand = b_stem
        if cand.lower() in used_chap_stems:
            cand = f"{cand}_{hashlib.sha1(u['unit_key'].encode('utf-8')).hexdigest()[:6]}"
        used_chap_stems.add(cand.lower())
        u["chap_stem"] = cand

    if kb_dir.is_symlink() or not _is_path_within_dir(kb_dir, cdir):
        log(f"  ✗ Refused chapter indexing under unsafe knowledge dir: {kb_dir}")
        return
    chapters_dir = kb_dir / "Chapters"
    if chapters_dir.is_symlink() or not _is_path_within_dir(chapters_dir, cdir):
        log(f"  ✗ Refused chapter indexing under unsafe Chapters dir: {chapters_dir}")
        return
    chapters_dir.mkdir(parents=True, exist_ok=True)

    # 3. Map existing managed chapter directories by unit_key so reordered chapters migrate cleanly without duplicates
    old_by_unit: dict[str, Path] = {}
    managed_existing: list[Path] = []
    for existing in sorted(chapters_dir.iterdir()):
        if not existing.is_dir() or existing.is_symlink() or not _is_path_within_dir(existing, chapters_dir):
            continue
        meta_path = existing / ".chapter_meta.json"
        if meta_path.exists():
            managed_existing.append(existing)
            try:
                m_data = json.loads(meta_path.read_text(encoding="utf-8"))
                if m_data.get("unit_key"):
                    old_by_unit[str(m_data["unit_key"])] = existing
            except Exception:
                pass
        elif re.match(r"^\d{2}_", existing.name):
            managed_existing.append(existing)
            epdfs = sorted(existing.glob("*.pdf"))
            if epdfs:
                esha = _file_sha256(epdfs[0])[:16]
                match_u = next(
                    (
                        u for u in ordered_units
                        if u["pdf_src"] is not None
                        and u["pdf_src"].name == epdfs[0].name
                        and _file_sha256(u["pdf_src"])[:16] == esha
                    ),
                    None,
                )
                if match_u and match_u["unit_key"] not in old_by_unit:
                    old_by_unit[match_u["unit_key"]] = existing

    target_dirs = [chapters_dir / f"{idx:02d}_{u['chap_stem']}" for idx, u in enumerate(ordered_units, 1)]
    target_set = set(target_dirs)

    # Migrate renamed folders via temporary names to avoid collisions during shifts/swaps
    temp_moves: list[tuple[Path, Path]] = []
    preserved_dirs: set[Path] = set()
    for idx, (u, target_dir) in enumerate(zip(ordered_units, target_dirs)):
        old_dir = old_by_unit.get(u["unit_key"])
        if old_dir is not None and old_dir.exists() and old_dir != target_dir:
            if target_dir.is_symlink() or not _is_path_within_dir(target_dir, chapters_dir):
                log(f"  ✗ Refused chapter migration onto unsafe symlink target: {target_dir}")
                preserved_dirs.add(old_dir)
                continue
            tmp_dir = chapters_dir / f".__migrate_{idx}_{old_dir.name}"
            if tmp_dir.exists():
                _safe_rmtree(tmp_dir, chapters_dir)
            old_dir.rename(tmp_dir)
            temp_moves.append((tmp_dir, target_dir))

    for tmp_dir, target_dir in temp_moves:
        if target_dir.exists():
            _safe_rmtree(target_dir, chapters_dir)
        tmp_dir.rename(target_dir)

    for existing in managed_existing:
        if existing.exists() and existing not in target_set and existing not in preserved_dirs:
            _safe_rmtree(existing, chapters_dir)

    # Index course passages by source_id and source_path for per-chapter passages.jsonl
    passages_by_source_id: dict[str, list[dict]] = {}
    if course_passages:
        for rec in course_passages:
            sid = str(rec.get("source_id") or "")
            if sid:
                passages_by_source_id.setdefault(sid, []).append(rec)

    # 4. Populate each chapter folder
    for u, chap_dir in zip(ordered_units, target_dirs):
        if chap_dir.is_symlink() or not _is_path_within_dir(chap_dir, chapters_dir):
            log(f"  ✗ Refused unsafe chapter dir outside Chapters root: {chap_dir}")
            continue
        chap_dir.mkdir(parents=True, exist_ok=True)
        keep_names: set[str] = set()
        unit_source_ids: list[str] = []
        bundle_parts = [f"# Chapter: {u['chap_stem']}\n\nCourse: {cdir.name}\n"]

        pdf_src: Path | None = u.get("pdf_src")
        if pdf_src is not None:
            link_or_copy(pdf_src, chap_dir / pdf_src.name)
            keep_names.add(pdf_src.name)
            try:
                rel_pdf = str(pdf_src.relative_to(cdir)).replace("\\", "/")
            except ValueError:
                rel_pdf = u["rel_src"]
            unit_source_ids.append(_compute_source_id(cdir.name, rel_pdf))
            pdf_md = pdf_md_map.get(rel_pdf) or (kb_dir / "pdfs" / f"{clean_filename(pdf_src.stem)}.md")
            if pdf_md.exists():
                link_or_copy(pdf_md, chap_dir / pdf_md.name)
                keep_names.add(pdf_md.name)
                bundle_parts.append(f"## Slides ({pdf_src.name})\n\n{pdf_md.read_text(encoding='utf-8', errors='replace').strip()}\n")

        txts = sorted(u["txts"], key=lambda p: p.name.lower())
        used_txt_names: set[str] = set()
        for t_idx, t_path in enumerate(txts, 1):
            t_dest_name = t_path.name
            if t_dest_name.lower() in used_txt_names:
                t_dest_name = f"{clean_filename(t_path.parent.name)}_{t_path.name}"
            used_txt_names.add(t_dest_name.lower())
            link_or_copy(t_path, chap_dir / t_dest_name)
            keep_names.add(t_dest_name)
            srt_path = t_path.with_suffix(".srt")
            if srt_path.exists():
                srt_dest_name = Path(t_dest_name).with_suffix(".srt").name
                link_or_copy(srt_path, chap_dir / srt_dest_name)
                keep_names.add(srt_dest_name)
            try:
                rel_t = str(t_path.relative_to(cdir)).replace("\\", "/")
                unit_source_ids.append(_compute_source_id(cdir.name, rel_t))
            except ValueError:
                pass
            t_text = t_path.read_text(encoding="utf-8", errors="replace").strip()
            bundle_parts.append(f"## Lecture Transcript {t_idx}: {Path(t_dest_name).stem}\n\n{t_text}\n")

        for doc_p in u.get("docs") or []:
            link_or_copy(doc_p, chap_dir / doc_p.name)
            keep_names.add(doc_p.name)
            try:
                rel_d = str(doc_p.relative_to(cdir)).replace("\\", "/")
                unit_source_ids.append(_compute_source_id(cdir.name, rel_d))
            except ValueError:
                pass
            d_text, _ = _extract_document_text(doc_p)
            if d_text.strip():
                bundle_parts.append(f"## Course Material: {doc_p.name}\n\n{d_text.strip()}\n")

        _cleanup_stale_chapter_links(chap_dir, keep_names)
        _safe_write_text_within(chap_dir / "CHAPTER_BUNDLE.md", cdir, "\n---\n\n".join(bundle_parts))
        _safe_write_text_within(
            chap_dir / ".chapter_meta.json",
            cdir,
            json.dumps({"managed_by": "toledo_dl", "unit_key": u["unit_key"], "source_ids": unit_source_ids}, indent=2) + "\n",
        )

        chap_passages: list[dict] = []
        for sid in unit_source_ids:
            chap_passages.extend(passages_by_source_id.get(sid, []))
        chap_pass_file = chap_dir / "passages.jsonl"
        if chap_passages:
            _safe_write_text_within(
                chap_pass_file,
                cdir,
                "\n".join(json.dumps(r, ensure_ascii=False) for r in chap_passages) + "\n",
            )
        elif chap_pass_file.exists() and not chap_pass_file.is_symlink() and _is_path_within_dir(chap_pass_file, cdir):
            chap_pass_file.unlink()

        # Human-friendly chapter view in Transcripts & Slides/<Chapter>
        if ts_dir.exists() and _is_path_within_dir(ts_dir, cdir) and not ts_dir.is_symlink():
            ts_chap_dir = ts_dir / chap_dir.name
            ts_chap_dir.mkdir(parents=True, exist_ok=True)
            ts_keep_names: set[str] = set()
            if pdf_src is not None:
                link_or_copy(pdf_src, ts_chap_dir / pdf_src.name)
                ts_keep_names.add(pdf_src.name)
            for t_path in txts:
                link_or_copy(t_path, ts_chap_dir / t_path.name)
                ts_keep_names.add(t_path.name)
                srt_p = t_path.with_suffix(".srt")
                if srt_p.exists():
                    link_or_copy(srt_p, ts_chap_dir / srt_p.name)
                    ts_keep_names.add(srt_p.name)
            for doc_p in u.get("docs") or []:
                link_or_copy(doc_p, ts_chap_dir / doc_p.name)
                ts_keep_names.add(doc_p.name)
            _cleanup_stale_chapter_links(ts_chap_dir, ts_keep_names)


def _summarize_course_from_manifest(entries: list[dict]) -> dict[str, tuple[int, int, int]]:
    counts: dict[str, list[int]] = {}
    for e in entries:
        cname = str(e.get("course") or "")
        if not cname:
            continue
        c = counts.setdefault(cname, [0, 0, 0])
        tp = e.get("type")
        if tp == "transcript":
            c[0] += 1
        elif tp == "pdf":
            c[1] += 1
        else:
            c[2] += 1
    return {k: (v[0], v[1], v[2]) for k, v in counts.items()}


def _ensure_study_profile(course_names: list[str] | None = None):
    """Create STUDY_PROFILE.md once at OUT root if missing (never overwrites user/LLM updates)."""
    profile_path = OUT / "STUDY_PROFILE.md"
    if profile_path.exists() or profile_path.is_symlink():
        return
    rows = "\n".join(
        f"| {name} | 3/5 (default) | untouched | — | |"
        for name in sorted(course_names or [])
    ) or "| _Add courses after first sync_ | 3/5 | untouched | — | |"
    content = f"""# Personal Study Profile

> **Instructions for AI assistants (Antigravity / Codex / Claude Code):**
> - Keep this file updated as you study with the student. **Do not wait for them to explicitly say "this is hard"**—if they ask repeated follow-ups on a topic, struggle with an exercise, or spend extra time on a chapter, deduce the difficulty/confidence and update the table + notes below automatically.
> - When building a study schedule, combine this file with `DEADLINES_AND_EXAMS.md`, `CALENDAR.md`, `LLM_INDEX.md`, and each course's `Chapters/` + `INDEX.md` to allocate realistic daily hours based on material density, weak spots, and free time.

## Availability & Preferences
- **Weekday study hours:** _e.g. 2–3h/day after classes_
- **Weekend study hours:** _e.g. 5–6h/day_
- **Target buffer before exams:** _e.g. finish first pass 3 days before exam_
- **Preferred study style:** _e.g. slides + lecture summary first, then exercises/past exams_

## Course Difficulty & Confidence
| Course | Difficulty (1-5) | Overall Confidence | Weak / Time-Heavy Topics | Observed Pace Notes |
| --- | --- | --- | --- | --- |
{rows}

## Session Feedback Log
_AI assistants: append brief dated observations here (e.g. `2026-11-20: Needed extra walkthrough on Weighted Least Squares & Box-Cox in Linear Models; budget ~1.5x time for diagnostic/residual chapters.`)._
"""
    _safe_write_text_within(profile_path, OUT, content)


def build_llm_index(
    target_dirs=None,
    *,
    vision_backend: str = "off",
    vision_model: str | None = None,
    cloud_approval: dict | None = None,
    vision_allowed_paths: set[Path] | None = None,
):
    """Generates LLM-ready Markdown transcripts, extracted PDF Markdown, passages.jsonl, per-course INDEX.md, and global llm_manifest.jsonl."""
    if not OUT.exists():
        return 0
    if target_dirs is None:
        target_dirs = sorted([
            d for d in OUT.iterdir()
            if d.is_dir() and not d.name.startswith(".") and _is_path_within_dir(d, OUT)
        ])
    else:
        target_dirs = [d for d in target_dirs if d.is_dir() and _is_path_within_dir(d, OUT)]

    target_names = {d.name for d in target_dirs}
    new_manifest_entries: list[dict] = []
    course_counts: dict[str, tuple[int, int, int]] = {}
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    vision_idx = 0
    vision_total = len(vision_allowed_paths) if vision_allowed_paths is not None else 0

    for cdir in target_dirs:
        kb_dir = cdir / "LLM_Knowledge"
        tr_dir = kb_dir / "transcripts"
        pdf_md_dir = kb_dir / "pdfs"

        all_course_files = [
            f for f in sorted(cdir.rglob("*"))
            if f.is_file()
            and not f.name.startswith(".")
            and _is_path_within_dir(f, cdir)
            and not _is_incomplete_transcript_txt(f)
            and "LLM_Knowledge" not in f.parts
            and "Transcripts & Slides" not in f.parts
            and "Flashcards" not in f.parts
        ]
        txt_files = [f for f in all_course_files if _is_transcript_txt(f)]
        pdf_files = [f for f in all_course_files if f.suffix.lower() == ".pdf"]
        txt_stem_counts = Counter(clean_filename(f.stem) for f in txt_files)
        pdf_stem_counts = Counter(clean_filename(f.stem) for f in pdf_files)

        transcripts, pdfs, other_files = [], [], []
        pdf_md_by_rel: dict[str, Path] = {}
        course_passages: list[dict] = []
        seen_pdf_sha: dict[str, dict] = {}
        seen_doc_sha: dict[str, dict] = {}

        def _sha256_file(path: Path) -> str:
            h = hashlib.sha256()
            try:
                with path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        h.update(chunk)
                return h.hexdigest()
            except OSError:
                return ""

        for f in all_course_files:
            rel = str(f.relative_to(cdir)).replace("\\", "/")
            ext = f.suffix.lower()
            source_id = _compute_source_id(cdir.name, rel)

            if _is_transcript_txt(f):
                md_out = _unique_md_dest(tr_dir, f, rel, cdir, cdir.name, txt_stem_counts)
                raw_txt = f.read_text(encoding="utf-8", errors="replace").strip()
                if not md_out.exists() or md_out.stat().st_mtime < f.stat().st_mtime or not _md_matches_source(md_out, rel):
                    wc = len(raw_txt.split())
                    mod_name = str(f.parent.relative_to(cdir)).replace("\\", "/") if f.parent != cdir else "Main"
                    fm = (
                        f"---\n"
                        f"course: {json.dumps(cdir.name)}\n"
                        f"source_id: {json.dumps(source_id)}\n"
                        f"type: transcript\n"
                        f"module: {json.dumps(mod_name)}\n"
                        f"title: {json.dumps(f.stem)}\n"
                        f"source: {json.dumps(rel)}\n"
                        f"words: {wc}\n"
                        f"---\n\n"
                        f"# {f.stem}\n\n"
                    )
                    if not tr_dir.is_symlink() and _is_path_within_dir(tr_dir, cdir):
                        tr_dir.mkdir(parents=True, exist_ok=True)
                        _safe_write_text_within(md_out, cdir, fm + raw_txt + "\n")
                transcripts.append((f.stem, rel, f"LLM_Knowledge/transcripts/{md_out.name}"))
                new_manifest_entries.append({
                    "source_id": source_id,
                    "course": cdir.name,
                    "type": "transcript",
                    "title": f.stem,
                    "source_path": f"{cdir.name}/{rel}",
                    "markdown_path": f"{cdir.name}/LLM_Knowledge/transcripts/{md_out.name}",
                })

                media_file = next((f.with_suffix(v) for v in _MEDIA_EXTS if f.with_suffix(v).exists()), f)
                media_rel = str(media_file.relative_to(cdir)).replace("\\", "/")
                srt_segs = _parse_srt_segments(f.with_suffix(".srt"))
                if srt_segs:
                    for seg in srt_segs:
                        course_passages.append({
                            "passage_id": f"{source_id}:t{seg['index']}",
                            "course": cdir.name,
                            "source_id": source_id,
                            "source_path": f"{cdir.name}/{rel}",
                            "source_name": f.name,
                            "kind": "transcript_segment",
                            "page": None,
                            "start_ts": seg["start_ts"],
                            "end_ts": seg["end_ts"],
                            "text": seg["text"],
                            "media_path": f"{cdir.name}/{media_rel}#t={seg['start_ts']}",
                            "extraction_status": "ok",
                        })
                else:
                    paras = [p.strip() for p in re.split(r"\n{2,}", raw_txt) if p.strip()] or ([raw_txt] if raw_txt else [])
                    for p_idx, p_txt in enumerate(paras, 1):
                        course_passages.append({
                            "passage_id": f"{source_id}:t{p_idx}",
                            "course": cdir.name,
                            "source_id": source_id,
                            "source_path": f"{cdir.name}/{rel}",
                            "source_name": f.name,
                            "kind": "transcript_segment",
                            "page": None,
                            "start_ts": None,
                            "end_ts": None,
                            "text": p_txt,
                            "media_path": f"{cdir.name}/{media_rel}",
                            "extraction_status": "ok" if p_txt else "empty",
                        })
            elif ext == ".pdf":
                sha = _sha256_file(f)
                dup_canon = seen_pdf_sha.get(sha) if sha else None
                md_out = _unique_md_dest(pdf_md_dir, f, rel, cdir, cdir.name, pdf_stem_counts)
                allow_vis = (
                    vision_backend in ("ollama", "gemini")
                    and dup_canon is None
                    and (vision_allowed_paths is None or f.resolve() in vision_allowed_paths)
                )
                if not pdf_md_dir.is_symlink() and _is_path_within_dir(pdf_md_dir, cdir) and not md_out.is_symlink() and _is_path_within_dir(md_out, cdir):
                    if allow_vis:
                        vision_idx += 1
                        progress_tag = f"{vision_idx}/{vision_total}" if vision_total else str(vision_idx)
                        log(f"  📄 [PDF Vision {progress_tag}] {cdir.name} / {rel}")
                    if "vision_backend" in inspect.signature(_extract_pdf_markdown_with_pages).parameters:
                        ok_pdf, page_texts, pdf_status = _extract_pdf_markdown_with_pages(
                            f,
                            md_out,
                            cdir.name,
                            rel,
                            vision_backend=vision_backend if allow_vis else "off",
                            vision_model=vision_model,
                            cloud_approval=cloud_approval,
                            course_dir=cdir,
                            allow_vision=allow_vis,
                        )
                    else:
                        ok_pdf, page_texts, pdf_status = _extract_pdf_markdown_with_pages(f, md_out, cdir.name, rel)
                else:
                    ok_pdf, page_texts, pdf_status = False, [], "unsafe_path"
                md_rel = f"LLM_Knowledge/pdfs/{md_out.name}" if ok_pdf else None
                if ok_pdf:
                    pdf_md_by_rel[rel] = md_out
                pdfs.append((f.name, rel, md_rel))
                entry = {
                    "source_id": source_id,
                    "course": cdir.name,
                    "type": "pdf",
                    "title": f.stem,
                    "source_path": f"{cdir.name}/{rel}",
                    "markdown_path": f"{cdir.name}/{md_rel}" if md_rel else None,
                    "extraction_status": pdf_status,
                    "content_sha256": sha,
                    "duplicate_of": dup_canon["source_id"] if dup_canon else None,
                    "alias_paths": [],
                }
                new_manifest_entries.append(entry)
                if dup_canon is not None:
                    dup_canon["alias_paths"].append(f"{cdir.name}/{rel}")
                    for rec in dup_canon.get("_passages", []):
                        rec.setdefault("alias_paths", []).append(f"{cdir.name}/{rel}")
                else:
                    emitted: list[dict] = []
                    if page_texts:
                        for pno, ptxt in enumerate(page_texts, 1):
                            rec = {
                                "passage_id": f"{source_id}:p{pno}",
                                "course": cdir.name,
                                "source_id": source_id,
                                "source_path": f"{cdir.name}/{rel}",
                                "source_name": f.name,
                                "kind": "pdf_page",
                                "page": pno,
                                "start_ts": None,
                                "end_ts": None,
                                "text": ptxt,
                                "media_path": f"{cdir.name}/{rel}#page={pno}",
                                "extraction_status": "ok" if ptxt else "visual_only",
                                "content_sha256": sha,
                            }
                            course_passages.append(rec)
                            emitted.append(rec)
                    else:
                        rec = {
                            "passage_id": f"{source_id}:p0",
                            "course": cdir.name,
                            "source_id": source_id,
                            "source_path": f"{cdir.name}/{rel}",
                            "source_name": f.name,
                            "kind": "pdf_page",
                            "page": None,
                            "start_ts": None,
                            "end_ts": None,
                            "text": "",
                            "media_path": f"{cdir.name}/{rel}",
                            "extraction_status": pdf_status,
                            "content_sha256": sha,
                        }
                        course_passages.append(rec)
                        emitted.append(rec)
                    if sha:
                        seen_pdf_sha[sha] = {
                            "source_id": source_id,
                            "alias_paths": entry["alias_paths"],
                            "_passages": emitted,
                        }
            elif f.name not in ("INDEX.md", "Announcements.md", "Links.md") and ext not in (".srt", ".vtt", ".mp3", ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".url"):
                sha = _sha256_file(f)
                dup_canon = seen_doc_sha.get(sha) if sha else None
                doc_txt, doc_status = _extract_document_text(f)
                other_files.append((f.name, rel))
                entry = {
                    "source_id": source_id,
                    "course": cdir.name,
                    "type": "file",
                    "title": f.name,
                    "source_path": f"{cdir.name}/{rel}",
                    "markdown_path": None,
                    "extraction_status": doc_status,
                    "content_sha256": sha,
                    "duplicate_of": dup_canon["source_id"] if dup_canon else None,
                    "alias_paths": [],
                }
                new_manifest_entries.append(entry)
                if dup_canon is not None:
                    dup_canon["alias_paths"].append(f"{cdir.name}/{rel}")
                    for rec in dup_canon.get("_passages", []):
                        rec.setdefault("alias_paths", []).append(f"{cdir.name}/{rel}")
                else:
                    emitted_doc: list[dict] = []
                    if doc_txt.strip():
                        for s_idx in range(0, len(doc_txt), 2000):
                            chunk_txt = doc_txt[s_idx:s_idx + 2000].strip()
                            if chunk_txt:
                                rec = {
                                    "passage_id": f"{source_id}:s{s_idx // 2000 + 1}",
                                    "course": cdir.name,
                                    "source_id": source_id,
                                    "source_path": f"{cdir.name}/{rel}",
                                    "source_name": f.name,
                                    "kind": "document_section",
                                    "page": None,
                                    "start_ts": None,
                                    "end_ts": None,
                                    "text": chunk_txt,
                                    "media_path": f"{cdir.name}/{rel}",
                                    "extraction_status": doc_status,
                                    "content_sha256": sha,
                                }
                                course_passages.append(rec)
                                emitted_doc.append(rec)
                    else:
                        rec = {
                            "passage_id": f"{source_id}:s0",
                            "course": cdir.name,
                            "source_id": source_id,
                            "source_path": f"{cdir.name}/{rel}",
                            "source_name": f.name,
                            "kind": "document_section",
                            "page": None,
                            "start_ts": None,
                            "end_ts": None,
                            "text": "",
                            "media_path": f"{cdir.name}/{rel}",
                            "extraction_status": doc_status,
                            "content_sha256": sha,
                        }
                        course_passages.append(rec)
                        emitted_doc.append(rec)
                    if sha:
                        seen_doc_sha[sha] = {
                            "source_id": source_id,
                            "alias_paths": entry["alias_paths"],
                            "_passages": emitted_doc,
                        }

        passages_file = kb_dir / "passages.jsonl"
        if course_passages:
            if not kb_dir.is_symlink() and _is_path_within_dir(kb_dir, cdir):
                kb_dir.mkdir(parents=True, exist_ok=True)
                _safe_write_text_within(
                    passages_file,
                    cdir,
                    "\n".join(json.dumps(r, ensure_ascii=False) for r in course_passages) + "\n",
                )
        elif passages_file.exists() and not passages_file.is_symlink() and _is_path_within_dir(passages_file, cdir):
            passages_file.unlink()

        if (kb_dir / "syllabus_manifest.json").exists():
            # Preserve curated syllabus_manifest.json chapters; refresh their passages.jsonl and chapter bundles in-place
            passages_by_name: dict[str, list[dict]] = {}
            for rec in course_passages:
                sname = str(rec.get("source_name") or "")
                if sname:
                    passages_by_name.setdefault(sname, []).append(rec)
            try:
                mf_data = json.loads((kb_dir / "syllabus_manifest.json").read_text(encoding="utf-8"))
                ts_dir = cdir / "Transcripts & Slides"
                all_course_files = {
                    f.name: f
                    for f in cdir.rglob("*")
                    if f.is_file()
                    and not f.name.startswith(".")
                    and not any(p in f.parts for p in ("LLM_Knowledge", "Transcripts & Slides"))
                }
                valid_ch_folders = set()
                for ent in (mf_data.get("chapters") or []):
                    ch_name = str(ent.get("folder") or "")
                    if not ch_name:
                        continue
                    valid_ch_folders.add(ch_name)
                    ch_p = kb_dir / "Chapters" / ch_name
                    if ch_p.is_symlink() or not _is_path_within_dir(ch_p, cdir):
                        continue
                    ch_p.mkdir(parents=True, exist_ok=True)

                    ts_ch_p = ts_dir / ch_name if ts_dir.exists() and not ts_dir.is_symlink() and _is_path_within_dir(ts_dir, cdir) else None
                    if ts_ch_p:
                        ts_ch_p.mkdir(parents=True, exist_ok=True)

                    keep_in_ch: set[str] = set()
                    for pdf_name in (ent.get("pdfs") or []):
                        pdf_file = all_course_files.get(pdf_name)
                        if pdf_file and pdf_file.exists():
                            link_or_copy(pdf_file, ch_p / pdf_file.name)
                            if ts_ch_p:
                                link_or_copy(pdf_file, ts_ch_p / pdf_file.name)
                            keep_in_ch.add(pdf_file.name)

                    for lec_name in (ent.get("lectures") or []):
                        lec_file = all_course_files.get(lec_name)
                        if lec_file and lec_file.exists():
                            link_or_copy(lec_file, ch_p / lec_file.name)
                            if ts_ch_p:
                                link_or_copy(lec_file, ts_ch_p / lec_file.name)
                            keep_in_ch.add(lec_file.name)
                            srt_file = lec_file.with_suffix(".srt")
                            if srt_file.exists():
                                link_or_copy(srt_file, ch_p / srt_file.name)
                                if ts_ch_p:
                                    link_or_copy(srt_file, ts_ch_p / srt_file.name)
                                keep_in_ch.add(srt_file.name)

                    if ts_ch_p:
                        _cleanup_stale_chapter_links(ts_ch_p, keep_in_ch)

                    c_pass: list[dict] = []
                    for nm in list(ent.get("pdfs") or []) + list(ent.get("lectures") or []):
                        c_pass.extend(passages_by_name.get(nm, []))
                    ch_pass_file = ch_p / "passages.jsonl"
                    if c_pass:
                        _safe_write_text_within(
                            ch_pass_file,
                            cdir,
                            "\n".join(json.dumps(r, ensure_ascii=False) for r in c_pass) + "\n",
                        )
                    elif ch_pass_file.exists() and not ch_pass_file.is_symlink() and _is_path_within_dir(ch_pass_file, cdir):
                        ch_pass_file.unlink()

                # Clean up legacy/unlisted chapter bundles in LLM_Knowledge/Chapters
                if (kb_dir / "Chapters").is_dir():
                    for existing in (kb_dir / "Chapters").iterdir():
                        if existing.is_dir() and existing.name not in valid_ch_folders and not existing.is_symlink():
                            _safe_rmtree(existing, kb_dir / "Chapters")
            except Exception:
                pass
        else:
            _repair_and_build_chapter_bundles(
                cdir,
                kb_dir,
                pdf_md_by_rel=pdf_md_by_rel,
                course_passages=course_passages,
            )

        # Write per-course INDEX.md
        idx_lines = [
            "---",
            f"course: {json.dumps(cdir.name)}",
            f"updated_at: {json.dumps(now_str)}",
            f"transcripts: {len(transcripts)}",
            f"pdfs: {len(pdfs)}",
            f"other_files: {len(other_files)}",
            "---",
            "",
            f"# {cdir.name} — LLM Knowledge Index",
            "",
            "## Lecture Transcripts",
        ]
        if transcripts:
            for title, src_rel, md_rel in transcripts:
                idx_lines.append(f"- [{title}]({md_rel}) _(source: `{src_rel}`)_")
        else:
            idx_lines.append("- _None yet_")

        idx_lines.extend(["", "## PDFs & Slides"])
        if pdfs:
            for name, src_rel, md_rel in pdfs:
                if md_rel:
                    idx_lines.append(f"- [{name}]({md_rel}) _(original: `{src_rel}`)_")
                else:
                    idx_lines.append(f"- `{src_rel}`")
        else:
            idx_lines.append("- _None yet_")

        idx_lines.extend(["", "## Course Files, Code & Datasets"])
        if other_files:
            for name, src_rel in other_files:
                idx_lines.append(f"- `{src_rel}`")
        else:
            idx_lines.append("- _None yet_")

        if (cdir / "Announcements.md").exists() or (cdir / "Links.md").exists():
            idx_lines.extend(["", "## Course Metadata"])
            if (cdir / "Announcements.md").exists():
                idx_lines.append("- [Announcements](Announcements.md)")
            if (cdir / "Links.md").exists():
                idx_lines.append("- [External Links](Links.md)")

        _safe_write_text_within(cdir / "INDEX.md", cdir, "\n".join(idx_lines) + "\n")
        course_counts[cdir.name] = (len(transcripts), len(pdfs), len(other_files))

    # Merge with existing manifest entries for courses outside target_dirs (e.g. filtered index --course)
    manifest_file = OUT / "llm_manifest.jsonl"
    preserved_entries: list[dict] = []
    if manifest_file.exists():
        try:
            for line in manifest_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                ent = json.loads(line)
                if not isinstance(ent, dict):
                    continue
                cname = str(ent.get("course") or "")
                if cname and cname not in target_names and (OUT / cname).is_dir():
                    preserved_entries.append(ent)
        except Exception:
            preserved_entries = []

    merged_manifest = sorted(
        preserved_entries + new_manifest_entries,
        key=lambda e: (str(e.get("course") or "").lower(), str(e.get("source_path") or "").lower()),
    )
    all_counts = _summarize_course_from_manifest(preserved_entries)
    all_counts.update(course_counts)
    global_rows = [
        f"| [{cname}]({urllib.parse.quote(cname)}/INDEX.md) | {cnts[0]} | {cnts[1]} | {cnts[2]} |"
        for cname, cnts in sorted(all_counts.items(), key=lambda kv: kv[0].lower())
    ]

    if global_rows:
        g_lines = [
            "# University LLM Knowledge Base",
            f"_Updated: {now_str}_",
            "",
            "| Course | Transcripts | PDFs | Other Files |",
            "| --- | ---: | ---: | ---: |",
            *global_rows,
            "",
            "Machine-readable manifest: `llm_manifest.jsonl`",
        ]
        _ensure_study_profile(list(all_counts.keys()))
        if (OUT / "STUDY_PROFILE.md").is_file():
            g_lines.append("- Personal Study Profile (AI-maintained): [STUDY_PROFILE.md](STUDY_PROFILE.md)")
        if (OUT / "DEADLINES_AND_EXAMS.md").is_file():
            g_lines.append("- Deadlines & Exam Schedule: [DEADLINES_AND_EXAMS.md](DEADLINES_AND_EXAMS.md)")
        if (OUT / "CALENDAR.md").is_file():
            g_lines.append("- Timetable: [CALENDAR.md](CALENDAR.md)")
        _safe_write_text_within(OUT / "LLM_INDEX.md", OUT, "\n".join(g_lines) + "\n")
        _safe_write_text_within(
            manifest_file,
            OUT,
            "\n".join(json.dumps(e, ensure_ascii=False) for e in merged_manifest) + ("\n" if merged_manifest else ""),
        )
    return len(new_manifest_entries)


def cmd_index(args):
    """Builds LLM-ready Markdown transcripts, PDF text/vision exports, and course indices."""
    if args.course:
        target_dirs = [
            d for d in sorted(OUT.iterdir())
            if d.is_dir() and not d.name.startswith(".") and _is_path_within_dir(d, OUT) and args.course.lower() in d.name.lower()
        ] if OUT.exists() else []
    else:
        target_dirs = [
            d for d in sorted(OUT.iterdir())
            if d.is_dir() and not d.name.startswith(".") and _is_path_within_dir(d, OUT)
        ] if OUT.exists() else []

    vision_backend = (getattr(args, "vision_backend", "off") or "off").strip().lower()
    vision_model = getattr(args, "vision_model", None)
    limit = getattr(args, "limit", None)
    if limit is not None and limit <= 0:
        limit = None
    dry_run = bool(getattr(args, "dry_run", False))
    auto_yes = bool(getattr(args, "yes", False))

    cloud_approval = None
    vision_allowed_paths: set[Path] | None = None

    if vision_backend in ("ollama", "gemini"):
        jobs = _plan_pdf_vision_jobs(target_dirs, vision_backend=vision_backend, vision_model=vision_model, limit=limit)
        total_pages = sum(j["pages"] for j in jobs)
        fallback_on_cancel = bool(getattr(args, "fallback_on_cancel", False))
        if vision_backend == "gemini":
            selected_model = (vision_model or os.environ.get("TOLEDO_PDF_VISION_GEMINI_MODEL") or _PDF_VISION_GEMINI_DEFAULT_MODEL).strip()
            est_cost = _estimate_cloud_cost("pdf-vision", "gemini", selected_model, item_count=len(jobs), page_count=total_pages)
            log(f"\n☁️  [Cloud PDF Vision] Provider: gemini ({selected_model})")
            log("    Data to upload: PDF slide pages (diagrams, images & text)")
            log(f"    Pending PDFs: {len(jobs)}" + (f" (~{total_pages} page(s))" if total_pages else ""))
            log(f"    Estimated cost: {est_cost}")
            log("    Note: Cloud API usage may incur charges depending on your provider plan and region.")
            if dry_run:
                for j in jobs:
                    pg_label = f" ({j['pages']}p)" if j["pages"] else ""
                    log(f"    • {j['course_dir'].name} / {j['rel_source']}{pg_label}")
                log("    Dry run — no cloud API requests sent and no index changes written.")
                return 0
            if not jobs:
                log("    Nothing pending — zero cloud API requests sent.")
                vision_allowed_paths = set()
            else:
                if not auto_yes:
                    if not sys.stdin.isatty():
                        log("    Aborted: cloud PDF vision requires --yes in non-interactive mode.")
                        return 1
                    try:
                        ans = input("    Proceed with cloud PDF vision scanning? [y/N]: ").strip().lower()
                    except EOFError:
                        ans = ""
                    if ans not in ("y", "yes"):
                        if fallback_on_cancel:
                            log("    Cancelled cloud PDF vision — building standard text index instead.")
                            vision_backend = "off"
                            vision_allowed_paths = set()
                        else:
                            log("    Cancelled — no cloud API requests sent.")
                            return 0
                if vision_backend == "gemini":
                    if not _ensure_api_key_interactive("GEMINI_API_KEY", "Gemini"):
                        log("    ✗ GEMINI_API_KEY is not set. Set GEMINI_API_KEY or run interactively to enter it.")
                        return 1
                    cloud_approval = _make_cloud_approval(
                        feature="pdf-vision",
                        provider="gemini",
                        model=selected_model,
                        allowed_paths=[j["pdf_path"] for j in jobs],
                        data_types=("pdf_pages",),
                        course_dirs=target_dirs,
                        expected_hashes={j["pdf_path"]: j["sha256"] for j in jobs if j.get("sha256")},
                    )
                    vision_allowed_paths = {j["pdf_path"].resolve() for j in jobs}
        else:
            selected_model = (vision_model or os.environ.get("TOLEDO_PDF_VISION_OLLAMA_MODEL") or _PDF_VISION_OLLAMA_DEFAULT_MODEL).strip()
            log(f"\n🖥️  [Local GPU PDF Vision] Backend: ollama ({selected_model}) | Pending PDFs: {len(jobs)}" + (f" (~{total_pages} page(s))" if total_pages else ""))
            if dry_run:
                for j in jobs:
                    pg_label = f" ({j['pages']}p)" if j["pages"] else ""
                    log(f"    • {j['course_dir'].name} / {j['rel_source']}{pg_label}")
                log("    Dry run — no changes written.")
                return 0
            if jobs:
                if not _ensure_local_pdf_vision_setup(
                    selected_model,
                    interactive=sys.stdin.isatty(),
                    auto_install=auto_yes,
                ):
                    vision_backend = "off"
                    vision_allowed_paths = set()
                else:
                    vision_allowed_paths = {j["pdf_path"].resolve() for j in jobs}
            else:
                vision_allowed_paths = set()
    elif dry_run:
        log("Dry run — no index changes written.")
        return 0

    count = build_llm_index(
        target_dirs=target_dirs if args.course else None,
        vision_backend=vision_backend,
        vision_model=vision_model,
        cloud_approval=cloud_approval,
        vision_allowed_paths=vision_allowed_paths,
    )
    log(f"LLM Knowledge Index updated ({count} indexed item(s) in {OUT}).")
    return 0


def _recording_subfolder(course_name: str, rec_type: str, clean_name: str, custom_subfolder: str = "") -> str:
    """Determines the recording subfolder for a course (generalized across students, keeping Linear Models layout)."""
    if custom_subfolder:
        return custom_subfolder
    env_sub = os.environ.get("TOLEDO_LECTURE_SUBFOLDER", "").strip()
    if env_sub:
        return env_sub
    if "linear models" in course_name.lower():
        if rec_type == "onedrive" or "anova" in clean_name.lower():
            return "ANOVA Lectures"
        return "Regression Analysis"
    return "Lectures"


def _sync_summary(report):
    labels = {"discovered": "recording(s) first seen since the previous scan",
              "recordings": "recording(s) downloaded", "files": "file(s) downloaded",
              "revised": "file(s) revised", "failed": "download(s) failed"}
    lines = [f"{course} · " + ", ".join(f"{counts[key]} {label}" for key, label in labels.items() if counts.get(key))
             for course, counts in report["courses"].items()]
    if not lines:
        lines.append("No files or recordings downloaded or revised.")
    if report.get("transcription_note"):
        lines.append(report["transcription_note"])
    lines.append(f"Saved to: {OUT}")
    return "\n".join(lines)


def cmd_sync_status(args):
    path = DATA / "sync-status.json"
    previous = None
    try:
        while True:
            if not path.exists():
                log("No sync status yet. Start a sync with 'toledo-sync run'.")
                return 0
            status = json.loads(path.read_text(encoding="utf-8"))
            if status != previous:
                log(f"{status['stage']} · {status['detail']}")
                previous = status
            if not args.watch or status["finished"]:
                return 0
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def _notify_windows_sync(title, summary):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return
    script = """
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$icon = New-Object System.Windows.Forms.NotifyIcon
try {
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.Visible = $true
    $icon.ShowBalloonTip(4000, $env:TOLEDO_NOTICE_TITLE, $env:TOLEDO_NOTICE_BODY, 'Info')
    Start-Sleep -Seconds 5
} finally {
    $icon.Dispose()
}
"""
    try:
        subprocess.Popen(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            env=dict(os.environ, TOLEDO_NOTICE_TITLE=title, TOLEDO_NOTICE_BODY=summary),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError:
        pass


def cmd_sync(args):
    DATA.mkdir(parents=True, exist_ok=True)
    _harden_private_path(DATA, is_dir=True)
    lock = open(LOCK, "a+", encoding="utf-8")
    _harden_private_path(LOCK, is_dir=False)
    if fcntl:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            log("Another toledo-dl run is in progress.")
            return 1
    elif msvcrt:
        try:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock.close()
            log("Another toledo-dl run is in progress.")
            return 1

    db = load_db()
    ok, failed = [], []
    report = {"courses": {}}
    transcribe_rc = 0
    status_path = DATA / "sync-status.json"

    def publish(stage, detail, finished=False):
        if not args.dry_run:
            try:
                _write_json(status_path, {"stage": stage, "detail": detail, "finished": finished})
            except OSError:
                pass
    video_mode = not getattr(args, "audio_only", False)
    ext = ".mp4" if video_mode else ".mp3"

    with _require_playwright() as p:
        c = open_ctx(p)
        try:
            if not logged_in(c):
                raise LoginRequired()

            publish("Starting", "Finding your Toledo courses")
            course_list = [(cid, name) for cid, name in courses(c) if not args.course or args.course.lower() in name.lower()]

            # ------------------------------------------------------------- 1. Sync Course Files & Documents
            if not getattr(args, "lectures_only", False):
                log("\n" + "=" * 60)
                log("📁 [1/5] SYNCING COURSE FILES, PDFs & DATASETS")
                log("=" * 60)
                publish("Stage 1/5 · Course files", "Checking PDFs, scripts and attachments")
                sync_files(c, course_list, dry_run=args.dry_run, report=report,
                           check_revisions=getattr(args, "check_revisions", False))

            # ------------------------------------------------------------- 2. Sync Lecture Recordings
            if not getattr(args, "files_only", False):
                publish("Stage 2/5 · Recordings", "Finding recordings missing from your archive")
                log("\n" + "=" * 60)
                log("🎬 [2/5] SYNCING LECTURE RECORDINGS")
                log("=" * 60)

                # Chronological sort key
                def item_sort_key(it):
                    t = it.get("title", "")
                    m_date = re.search(r"(\d{4})[./-](\d{2})[./-](\d{2})", t)
                    date_val = f"{m_date.group(1)}-{m_date.group(2)}-{m_date.group(3)}" if m_date else "9999"
                    m_time = re.search(r"(\d{2})[:.-](\d{2})", t)
                    time_val = f"{m_time.group(1)}:{m_time.group(2)}" if m_time else "99:99"
                    m_num = re.search(r"(?:recording|session|part|lecture)[_\s]*(\d+)", t, re.I)
                    num_val = int(m_num.group(1)) if m_num else 9999
                    return (date_val, time_val, num_val, t)

                all_items = gather(c, args.course)
                all_items.sort(key=item_sort_key)
                if not args.dry_run:
                    inventory_path = DATA / "recording-inventory.json"
                    inventory = json.loads(inventory_path.read_text(encoding="utf-8")) if inventory_path.exists() else {}
                    current = {clean(name): set() for _, name in course_list}
                    for item in all_items:
                        current.setdefault(item["course"], set()).add(item["entry"])
                    for name, entries in current.items():
                        if name in inventory:
                            seen = set(inventory[name])
                            _count_sync_item(report, name, "discovered", len(entries - seen))
                            entries.update(seen)
                        inventory[name] = sorted(entries)
                    _write_json(inventory_path, inventory)

                # Map each item entry to its chronological lecture index within its course
                course_counters = {}
                item_index_map = {}
                for it in all_items:
                    cid_name = it["course"]
                    course_counters[cid_name] = course_counters.get(cid_name, 0) + 1
                    item_index_map[it["entry"]] = course_counters[cid_name]

                reserved_stems: dict[Path, str] = {}
                for db_entry, db_path_str in db.items():
                    try:
                        p_obj = Path(db_path_str)
                        p_stem = p_obj.with_suffix("")
                        if p_obj.exists() or p_stem.with_suffix(".mp4").exists() or p_stem.with_suffix(".mp3").exists():
                            reserved_stems.setdefault(p_stem, db_entry)
                    except Exception:
                        pass

                todo = []
                for it in all_items:
                    entry = it["entry"]
                    lec_idx = item_index_map.get(entry, 1)
                    db_stem = Path(db[entry]).with_suffix("") if entry in db else None
                    if db_stem is not None and (
                        Path(db[entry]).exists() or db_stem.with_suffix(".mp4").exists() or db_stem.with_suffix(".mp3").exists()
                    ):
                        dest = db_stem.with_suffix(ext)
                    else:
                        base_clean = format_clean_filename(it, lec_idx)
                        subfolder = _recording_subfolder(it["course"], it["type"], base_clean)
                        base_dir = OUT / it["course"] / subfolder
                        cand_stem = base_dir / base_clean
                        suffix_n = 2
                        while (
                            (cand_stem in reserved_stems and reserved_stems[cand_stem] != entry)
                            or (
                                (cand_stem.with_suffix(".mp4").exists() or cand_stem.with_suffix(".mp3").exists())
                                and reserved_stems.get(cand_stem) != entry
                            )
                        ):
                            cand_stem = base_dir / f"{base_clean} ({suffix_n})"
                            suffix_n += 1
                        dest = cand_stem.with_suffix(ext)

                    reserved_stems[dest.with_suffix("")] = entry
                    dest_mp4 = dest.with_suffix(".mp4")
                    dest_mp3 = dest.with_suffix(".mp3")

                    if entry not in db:
                        todo.append((it, dest, dest_mp4, dest_mp3))
                    else:
                        if video_mode and not dest_mp4.exists():
                            todo.append((it, dest, dest_mp4, dest_mp3))
                        elif not video_mode and not dest_mp3.exists():
                            todo.append((it, dest, dest_mp4, dest_mp3))

                if args.limit:
                    todo = todo[: args.limit]

                log(f"\nFound {len(todo)} recording(s) missing from your archive.\n")

                for idx, (it, dest, dest_mp4, dest_mp3) in enumerate(todo, 1):
                    recording_label = f"Recording {idx}/{len(todo)} · {it['course']} · {it['title']}"
                    log(f"\n{recording_label}")
                    publish("Stage 2/5 · Recordings", recording_label)

                    def progress(detail):
                        publish("Stage 2/5 · Recordings", f"{recording_label}\n{detail}")
                    log(f"  Saving: {dest.name}")
                    log("-------------------------------------------------------")

                    if args.dry_run:
                        continue
                    if dest_mp4.exists() if video_mode else dest_mp3.exists():
                        primary = dest_mp4 if dest_mp4.exists() else dest_mp3
                        db[it["entry"]] = str(primary)
                        save_db(db)
                        ok.append(primary)
                        continue

                    if it["type"] == "kaltura":
                        partner = it.get("partner_id") or PARTNER
                        ks = get_kaltura_ks(
                            c,
                            it["course_id"],
                            it["content_id"],
                            entry_id=it["entry"],
                            partner_id=partner,
                            is_embedded=it.get("is_embedded", False),
                        )
                        if ks:
                            base = f"https://www.kaltura.com/p/{partner}/sp/{partner}00/playManifest/entryId/{it['entry']}/protocol/https"
                            urls = [f"{base}/format/applehttp/ks/{ks}/a.m3u8", f"{base}/format/url/ks/{ks}/a.mp4"]
                            success = download_media(urls, dest, video=video_mode, progress=progress)
                        else:
                            success = False
                    elif it["type"] == "collab":
                        video_url = get_collab_stream_url(c, it["entry"], it["auth_headers"])
                        if video_url:
                            success = download_media(video_url, dest, video=video_mode, progress=progress)
                        else:
                            success = False
                    elif it["type"] == "onedrive":
                        success = download_onedrive_media(c, it["url"], dest, video=video_mode)
                    else:
                        success = False

                    if success:
                        primary = dest_mp4 if dest_mp4.exists() else dest_mp3
                        db[it["entry"]] = str(primary)
                        save_db(db)
                        ok.append(primary)
                        _count_sync_item(report, it["course"], "recordings")
                        log(f"✓ FINISHED: Saved {primary.name}")
                    else:
                        failed.append(it["title"])
                        _count_sync_item(report, it["course"], "failed")
                        log(f"✗ FAILED: {it['title']}")

                log(f"\nRecordings sync summary: {len(ok)} downloaded, {len(failed)} failed.")

            # ------------------------------------------------------------- 3. Check New Announcements
            if not getattr(args, "files_only", False) and not getattr(args, "lectures_only", False):
                publish("Stage 3/5 · Announcements", "Checking Toledo announcements")
                log("\n" + "=" * 60)
                log("📢 [3/5] CHECKING FOR NEW ANNOUNCEMENTS")
                log("=" * 60)
                sync_announcements(c, course_list)
                try:
                    sync_calendar_and_deadlines(c, course_list)
                except Exception as e:
                    log(f"  [Calendar] Warning: Could not update calendar/deadlines: {e}")

            # ------------------------------------------------------------- 4. Auto-transcribe & Study Packs
            if not getattr(args, "no_transcribe", False) and not args.dry_run and not getattr(args, "files_only", False):
                publish("Stage 4/5 · Transcription", "Checking downloaded recordings")
                log("\n" + "=" * 60)
                log("🎙️ [4/5] AUTO-TRANSCRIBING LECTURES")
                log("=" * 60)
                saved_prefs = _load_sync_preferences()
                explicit_t_backend = getattr(args, "transcribe_backend", None)
                explicit_m_backend = getattr(args, "match_backend", None)
                interactive_sync = sys.stdin.isatty() and not getattr(args, "defer_cpu_transcription", False)

                if explicit_t_backend:
                    transcribe_choice = explicit_t_backend
                elif interactive_sync:
                    transcribe_choice = _prompt_transcribe_backend(
                        bool(_cuda_device_count()),
                        default_backend=saved_prefs.get("transcribe_backend", "local"),
                        allow_skip=True,
                    )
                else:
                    transcribe_choice = saved_prefs.get("transcribe_backend", "local")

                if explicit_m_backend:
                    match_choice = explicit_m_backend
                else:
                    match_choice = saved_prefs.get("match_backend", "keyword")

                if interactive_sync or explicit_t_backend or explicit_m_backend:
                    _save_sync_preferences(
                        transcribe_backend=None if transcribe_choice == "skip" else transcribe_choice,
                        match_backend=match_choice,
                    )

                if transcribe_choice == "skip":
                    log("  [Transcribe] Skipped for now; pending recordings remain queued.")
                elif transcribe_choice in ("gemini", "openai") and not sys.stdin.isatty():
                    note = f"Cloud transcription ({transcribe_choice}) left pending: no interactive approval prompt available."
                    log(f"  ℹ️ [Transcribe] {note}")
                    report["transcription_note"] = note
                else:
                    transcribe_rc = cmd_transcribe(argparse.Namespace(
                        course=args.course,
                        model=getattr(args, "model", "auto"),
                        lang=getattr(args, "lang", "en"),
                        backend=transcribe_choice,
                        limit=None,
                        max_minutes=None,
                        yes=False,
                        dry_run=False,
                        file=None,
                        defer_cpu=getattr(args, "defer_cpu_transcription", False),
                        progress=lambda detail: publish("Stage 4/5 · Transcription", detail),
                        report=report,
                    ))

                if match_choice in ("gemini", "openai", "anthropic", "claude"):
                    if not sys.stdin.isatty():
                        log(f"  ℹ️ [Match-Slides] Cloud slide matching ({match_choice}) left pending: no interactive approval prompt available.")
                    else:
                        cmd_match_slides(argparse.Namespace(
                            course=args.course,
                            backend=match_choice,
                            model=None,
                            no_video=False,
                            limit=None,
                            dry_run=False,
                            yes=False,
                            force=False,
                            report=report,
                        ))
                elif ok:
                    affected_courses = set()
                    for p in ok:
                        p_obj = Path(p)
                        for parent in p_obj.parents:
                            if parent.parent == OUT:
                                affected_courses.add(parent)
                                break
                    for cdir in affected_courses:
                        try:
                            update_course_pdf_matches(cdir, backend=match_choice)
                        except Exception as e:
                            log(f"    Note: {e}")

            if not args.dry_run:
                publish("Stage 5/5 · Search index", "Updating the searchable archive")
                log("\n" + "=" * 60)
                log("📚 [5/5] BUILDING LLM KNOWLEDGE BASE & MARKDOWN INDICES")
                log("=" * 60)
                saved_idx_prefs = _load_sync_preferences()
                explicit_v_backend = getattr(args, "pdf_vision_backend", None)
                interactive_idx = sys.stdin.isatty() and not getattr(args, "defer_cpu_transcription", False)
                if explicit_v_backend:
                    pdf_vision_choice = explicit_v_backend
                elif interactive_idx:
                    pdf_vision_choice = _prompt_pdf_vision_backend(
                        bool(_cuda_device_count()),
                        default_backend=saved_idx_prefs.get("pdf_vision_backend", "off"),
                    )
                else:
                    pdf_vision_choice = saved_idx_prefs.get("pdf_vision_backend", "off")

                if interactive_idx or explicit_v_backend:
                    _save_sync_preferences(pdf_vision_backend=pdf_vision_choice)

                if pdf_vision_choice == "gemini" and not sys.stdin.isatty():
                    log("  ℹ️ [PDF Vision] Cloud PDF vision (gemini) left pending: no interactive approval prompt available.")
                    indexed = build_llm_index()
                    log(f"  ✓ Updated LLM_INDEX.md, llm_manifest.jsonl & per-course INDEX.md ({indexed} item(s))")
                elif pdf_vision_choice in ("ollama", "gemini"):
                    idx_rc = cmd_index(argparse.Namespace(
                        course=args.course,
                        vision_backend=pdf_vision_choice,
                        vision_model=None,
                        limit=None,
                        dry_run=False,
                        yes=False,
                        fallback_on_cancel=True,
                    ))
                    if idx_rc != 0:
                        log("  ℹ️ [PDF Vision] Falling back to standard text index without cloud upload.")
                        indexed = build_llm_index()
                        log(f"  ✓ Updated LLM_INDEX.md, llm_manifest.jsonl & per-course INDEX.md ({indexed} item(s))")
                else:
                    indexed = build_llm_index()
                    log(f"  ✓ Updated LLM_INDEX.md, llm_manifest.jsonl & per-course INDEX.md ({indexed} item(s))")

            log("\n" + "=" * 60)
            if transcribe_rc:
                report["transcription_note"] += " See the sync output for transcription errors."
            sync_failed = bool(transcribe_rc or any(counts.get("failed") for counts in report["courses"].values()))
            if args.dry_run:
                log("Dry run complete.")
            elif sync_failed:
                log("⚠️ SYNC COMPLETED WITH ERRORS")
            else:
                log("SYNC COMPLETE: All course files, recordings & announcements up to date!")
            log("=" * 60)

            save_state(c)
            if not args.dry_run:
                summary = _sync_summary(report)
                log(summary)
                result = "Completed with errors" if sync_failed else "Completed"
                publish(result, summary, finished=True)
                if sys.platform == "win32" and getattr(args, "notify", False):
                    _notify_windows_sync(f"Toledo sync · {result}", summary)
        except LoginRequired:
            log(_LOGIN_MSG)
            publish("Login required", "Sign in with 'toledo-login' and retry.", finished=True)
            return 2
        except KeyboardInterrupt:
            publish("Interrupted", "Saved downloads are available. Run sync again to continue.", finished=True)
            return 130
        except Exception:
            publish("Failed", "Sync stopped unexpectedly. See the sync log for details.", finished=True)
            raise
        finally:
            c.close()
            try:
                lock.close()
            except Exception:
                pass

    return int(sync_failed)


# ---------------------------------------------------------------- Calendar & Timetable
def fetch_kuloket_schedule(c):
    """Fetches calendar items & deadlines from UAntwerpen Blackboard Ultra (/learn/api/public/v1/calendars/items)."""
    log("⏳ Fetching calendar & deadlines from UAntwerpen Blackboard...")
    today = date.today()
    start_yr = today.year if today.month >= 8 else today.year - 1
    end_yr = start_yr + 1
    since = f"{start_yr}-08-15T00:00:00.000Z"
    until = f"{end_yr}-07-31T23:59:59.000Z"
    items: list[dict] = []
    offset = 0
    while True:
        r = c.request.get(
            f"{BASE}/learn/api/public/v1/calendars/items?since={urllib.parse.quote(since)}&until={urllib.parse.quote(until)}&offset={offset}&limit=200",
            timeout=15000,
        )
        if r.status in (401, 403):
            raise LoginRequired()
        if r.status != 200:
            break
        data = r.json()
        results = data.get("results") or []
        if not results:
            break
        for ev in results:
            raw_start = ev.get("start") or ""
            raw_end = ev.get("end") or raw_start
            raw_date = raw_start.split("T")[0] if "T" in raw_start else raw_start[:10]
            start_t = raw_start.split("T")[1][:5] if "T" in raw_start else "00:00"
            end_t = raw_end.split("T")[1][:5] if "T" in raw_end else start_t
            dur_h = "1.0"
            try:
                dt_s = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
                dt_e = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
                dur_h = f"{max(0.0, (dt_e - dt_s).total_seconds() / 3600.0):.1f}"
            except Exception:
                pass
            items.append({
                "id": ev.get("id") or "",
                "course": ev.get("calendarName") or ev.get("calendarId") or "UAntwerpen",
                "code": "",
                "date": raw_date,
                "dayOfWeek": None,
                "startTime": start_t,
                "endTime": end_t,
                "durationHours": dur_h,
                "location": ev.get("location") or "",
                "teacher": "",
                "detail": ev.get("title") or ev.get("description") or "",
                "eventType": ev.get("type") or "",
            })
        if len(results) < 200:
            break
        offset += len(results)
    return items


def get_schedule(c=None, refresh=False):
    """Returns schedule events from local cache or fetches freshly from Kuloket."""
    if not refresh and CALENDAR_DB.exists():
        try:
            data = json.loads(CALENDAR_DB.read_text(encoding="utf-8"))
            if data.get("events"):
                return data["events"]
        except Exception:
            pass

    events = None
    if not c:
        with _require_playwright() as p:
            ctx = open_ctx(p)
            try:
                if not logged_in(ctx):
                    raise LoginRequired()
                events = fetch_kuloket_schedule(ctx)
                save_state(ctx)
            finally:
                ctx.close()
    else:
        events = fetch_kuloket_schedule(c)

    if events:
        CALENDAR_DB.parent.mkdir(parents=True, exist_ok=True)
        CALENDAR_DB.write_text(json.dumps({"updated_at": datetime.now().isoformat(), "events": events}, indent=2), encoding="utf-8")
    return events or []


def display_calendar(events, course_filter=None, semester="1", upcoming_only=False):
    today_str = date.today().isoformat()

    # Filter by semester
    # Semester 1: Sept 1 to Jan 31
    # Semester 2: Feb 1 to June 30
    sem_events = []
    for ev in events:
        d = ev.get("date")
        if not d:
            continue
        month = int(d.split("-")[1])
        if semester == "1":
            if not (month >= 9 or month == 1):
                continue
        elif semester == "2":
            if not (2 <= month <= 7):
                continue
        sem_events.append(ev)

    if course_filter:
        cf = course_filter.lower()
        sem_events = [ev for ev in sem_events if cf in ev.get("course", "").lower() or cf in ev.get("code", "").lower()]

    if not sem_events:
        log(f"No classes found matching criteria (Semester: {semester}, Course filter: {course_filter or 'None'}).")
        return

    if upcoming_only:
        upcoming_events = [ev for ev in sem_events if ev["date"] >= today_str]
        upcoming_events.sort(key=lambda x: (x["date"], x["startTime"]))
        log("\n" + "=" * 78)
        log(f"📅 UPCOMING KU LEUVEN CLASSES (from {today_str} onwards | Semester {semester})")
        log("=" * 78)
        if not upcoming_events:
            log("✓ No upcoming classes found for the rest of this semester.")
            return
        for ev in upcoming_events:
            day_name = DAY_MAP.get(ev.get("dayOfWeek"), "")[:3] if ev.get("dayOfWeek") else "---"
            loc = f" | Room: {ev['location']}" if ev.get("location") else ""
            prof = f" | Prof: {ev['teacher']}" if ev.get("teacher") else ""
            log(f" • {day_name} {ev['date']} | {ev['startTime']}–{ev['endTime']} | {ev['course']} [{ev['code']}]{loc}{prof}")
        log("-" * 78)
        log(f"Total upcoming classes: {len(upcoming_events)}")
        return

    # Group by course
    by_course = {}
    for ev in sem_events:
        c_key = (ev["course"], ev["code"])
        by_course.setdefault(c_key, []).append(ev)

    sem_name = f"Semester {semester}" if semester in ("1", "2") else "Full Academic Year"
    log("\n" + "=" * 78)
    log(f"📅 KU LEUVEN CLASS SCHEDULE & FREQUENCY ({sem_name})")
    log(f"Current Date: {today_str}")
    log("=" * 78)

    total_classes_all = 0
    total_remaining_all = 0

    for (course, code), evs in sorted(by_course.items()):
        evs.sort(key=lambda x: (x["date"], x["startTime"]))
        total = len(evs)
        total_classes_all += total

        past = [e for e in evs if e["date"] < today_str]
        upcoming = [e for e in evs if e["date"] >= today_str]
        total_remaining_all += len(upcoming)

        # Weekly pattern
        slot_counts = {}
        for e in evs:
            day_name = DAY_MAP.get(e["dayOfWeek"], e["dayOfWeek"] or "Unknown")
            slot = f"{day_name} {e['startTime']}–{e['endTime']}"
            slot_counts[slot] = slot_counts.get(slot, 0) + 1

        freq_str = ", ".join(f"{slot} ({count}x)" for slot, count in slot_counts.items())
        dates = [e["date"] for e in evs]
        locations = set(e["location"] for e in evs if e.get("location"))
        teachers = set(e["teacher"] for e in evs if e.get("teacher"))

        log(f"\n📘 {course} [{code}]")
        log(f"   • Total Classes:   {total} sessions ({len(past)} completed, {len(upcoming)} remaining)")
        log(f"   • Weekly Pattern:  {freq_str}")
        log(f"   • Date Range:      {dates[0]} → {dates[-1]}")
        if locations:
            log(f"   • Locations:       {', '.join(sorted(locations))}")
        if teachers:
            log(f"   • Instructors:     {', '.join(sorted(teachers))}")

        if upcoming:
            nxt = upcoming[0]
            nxt_day = DAY_MAP.get(nxt["dayOfWeek"], "")
            loc_str = f" in {nxt['location']}" if nxt.get("location") else ""
            log(f"   • Next Session:    {nxt_day}, {nxt['date']} at {nxt['startTime']}–{nxt['endTime']}{loc_str}")
        else:
            log("   • Next Session:    ✓ All classes for this semester have concluded")

    log("\n" + "-" * 78)
    log(f"📊 SUMMARY: {total_classes_all} total scheduled sessions across {len(by_course)} courses | {total_remaining_all} classes remaining")
    log("-" * 78 + "\n")


def _is_exam_event(title: str, event_type: str = "") -> bool:
    return bool(re.search(r"\b(?:exam|examen|exams|tentamen|toets|midterm|final)\b", f"{title} {event_type}", re.I))


def sync_calendar_and_deadlines(c, course_list):
    """Fetch Kuloket timetable + Toledo calendar/assignment deadlines and write prominent Markdown files."""
    if not OUT.exists():
        OUT.mkdir(parents=True, exist_ok=True)
    today_str = date.today().isoformat()

    # 1. Kuloket timetable (classes + exams when published)
    schedule_events = []
    try:
        schedule_events = get_schedule(c, refresh=True)
    except Exception as e:
        log(f"  [Calendar] Could not refresh Kuloket timetable ({e}); checking cache...")
        if CALENDAR_DB.exists():
            try:
                schedule_events = json.loads(CALENDAR_DB.read_text(encoding="utf-8")).get("events", [])
            except Exception:
                schedule_events = []

    # 2. Blackboard calendar items & assignment due dates
    course_name_by_id = {cid: clean(name) for cid, name in course_list}
    deadlines: list[dict] = []
    seen_dl: set[tuple[str, str, str]] = set()

    def _add_deadline(course_name: str, title: str, due_iso: str, kind: str, location: str = ""):
        due_clean = (due_iso or "")[:16].replace("T", " ").strip()
        date_only = due_clean[:10]
        if not date_only or not title:
            return
        key = (course_name, title.strip().lower(), date_only)
        if key in seen_dl:
            return
        seen_dl.add(key)
        deadlines.append({
            "course": course_name,
            "title": title.strip(),
            "due": due_clean,
            "date": date_only,
            "kind": "EXAM" if _is_exam_event(title, kind) else kind,
            "location": location.strip(),
        })

    if c is not None:
        try:
            cal_items = _fetch_all_contents(c, f"{BASE}/learn/api/public/v1/calendars/items")
            for it in cal_items:
                due = it.get("end") or it.get("start") or ""
                cname = course_name_by_id.get(it.get("calendarId", ""), it.get("calendarName") or "Toledo")
                _add_deadline(cname, it.get("title") or "", due, it.get("type") or "Calendar", it.get("location") or "")
        except Exception:
            pass

        for cid, raw_name in course_list:
            cname = clean(raw_name)
            try:
                cols = _fetch_all_contents(c, f"{BASE}/learn/api/public/v1/courses/{cid}/gradebook/columns")
                for col in cols:
                    grading = col.get("grading") if isinstance(col.get("grading"), dict) else {}
                    due = grading.get("due") or ""
                    if due:
                        _add_deadline(cname, col.get("name") or "Assignment", due, "Assignment")
            except Exception:
                continue

    for ev in schedule_events:
        if _is_exam_event(ev.get("title") or "", ev.get("eventType") or ""):
            ev_date = ev.get("date") or ""
            ev_time = f"{ev.get('startTime', '')}-{ev.get('endTime', '')}".strip("-")
            due_str = f"{ev_date} {ev_time}".strip()
            _add_deadline(clean(ev.get("course") or ev.get("title") or "Exam"), ev.get("title") or "Exam", due_str, "EXAM", ev.get("location") or "")

    deadlines.sort(key=lambda d: (d["date"], d["course"].lower(), d["title"].lower()))
    upcoming_exams = [d for d in deadlines if d["kind"] == "EXAM" and d["date"] >= today_str]
    upcoming_tasks = [d for d in deadlines if d["kind"] != "EXAM" and d["date"] >= today_str]
    past_items = [d for d in deadlines if d["date"] < today_str]

    dl_lines = [
        "# Deadlines & Exam Schedule",
        f"_Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        "## Upcoming Exams",
    ]
    if upcoming_exams:
        dl_lines += ["| Date / Time | Course | Exam | Location |", "| --- | --- | --- | --- |"]
        for d in upcoming_exams:
            dl_lines.append(f"| {d['due']} | {d['course']} | {d['title']} | {d['location'] or '-'} |")
    else:
        dl_lines.append("_No upcoming exams published yet in Kuloket/Toledo._")

    dl_lines += ["", "## Upcoming Assignments & Deadlines"]
    if upcoming_tasks:
        dl_lines += ["| Due | Course | Item | Type |", "| --- | --- | --- | --- |"]
        for d in upcoming_tasks:
            dl_lines.append(f"| {d['due']} | {d['course']} | {d['title']} | {d['kind']} |")
    else:
        dl_lines.append("_No upcoming assignment deadlines found._")

    if past_items:
        dl_lines += ["", "## Past Deadlines & Exams", "| Date | Course | Item | Type |", "| --- | --- | --- | --- |"]
        for d in past_items:
            dl_lines.append(f"| {d['due']} | {d['course']} | {d['title']} | {d['kind']} |")

    _safe_write_text_within(OUT / "DEADLINES_AND_EXAMS.md", OUT, "\n".join(dl_lines) + "\n")

    # 3. Full Timetable (CALENDAR.md)
    cal_lines = [
        "# University Timetable (Kuloket)",
        f"_Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
    ]
    if schedule_events:
        sorted_ev = sorted(schedule_events, key=lambda e: (e.get("date", ""), e.get("startTime", "")))
        upcoming_ev = [e for e in sorted_ev if e.get("date", "") >= today_str]
        cal_lines += [f"## Upcoming Sessions ({len(upcoming_ev)})", "| Date | Time | Course | Type | Location |", "| --- | --- | --- | --- | --- |"]
        for ev in upcoming_ev[:120]:
            t_str = f"{ev.get('startTime', '')}-{ev.get('endTime', '')}".strip("-")
            cal_lines.append(f"| {ev.get('date', '')} | {t_str} | {ev.get('course', '')} | {ev.get('eventType', '') or ev.get('title', '')} | {ev.get('location', '') or '-'} |")
    else:
        cal_lines.append("_No Kuloket timetable events cached._")
    _safe_write_text_within(OUT / "CALENDAR.md", OUT, "\n".join(cal_lines) + "\n")

    # 4. Per-course Deadlines.md
    by_course: dict[str, list[dict]] = {}
    for d in deadlines:
        by_course.setdefault(d["course"], []).append(d)
    for cname, items in by_course.items():
        cdir = OUT / cname
        if cdir.is_dir() and _is_path_within_dir(cdir, OUT):
            c_lines = [f"# {cname} — Deadlines & Exams", "", "| Due | Item | Type | Location |", "| --- | --- | --- | --- |"]
            for d in items:
                c_lines.append(f"| {d['due']} | {d['title']} | {d['kind']} | {d['location'] or '-'} |")
            _safe_write_text_within(cdir / "Deadlines.md", cdir, "\n".join(c_lines) + "\n")

    log(f"  ✓ Calendar & deadlines updated: {len(upcoming_exams)} upcoming exam(s), {len(upcoming_tasks)} upcoming deadline(s) -> DEADLINES_AND_EXAMS.md & CALENDAR.md")


def cmd_calendar(args):
    try:
        refresh = getattr(args, "refresh", False)
        if refresh or not CALENDAR_DB.exists():
            with _require_playwright() as p:
                c = open_ctx(p)
                if not logged_in(c):
                    log(_LOGIN_MSG)
                    c.close()
                    return 2
                events = get_schedule(c, refresh=True)
                save_state(c)
                c.close()
        else:
            events = get_schedule(refresh=False)
    except LoginRequired:
        log(_LOGIN_MSG)
        return 2
    except Exception as e:
        log(f"Failed to fetch calendar: {e}")
        return 1

    if not events:
        log("No calendar events found or failed to load schedule.")
        return 1

    if getattr(args, "json", False):
        print(json.dumps(events, indent=2))
        return 0

    display_calendar(events, course_filter=args.course, semester=args.semester, upcoming_only=args.upcoming)
    return 0


# ---------------------------------------------------------------- Google Drive Sync
def cmd_drive_sync(args):
    """Syncs ~/Lectures (or a specific course) to Google Drive via rclone."""
    rclone_bin = shutil.which("rclone") or "/usr/bin/rclone"
    if not Path(rclone_bin).exists():
        log("Error: rclone is not installed.")
        return 1

    base_src = OUT
    base_dest = "gdrive:transcribed classes/Lectures"

    if getattr(args, "course", None):
        matching = [d for d in base_src.iterdir() if d.is_dir() and args.course.lower() in d.name.lower()]
        if not matching:
            log(f"No course folder found matching '{args.course}' in {base_src}")
            return 1
        target = matching[0]
        src = str(target)
        dest = f"{base_dest}/{target.name}"
        log(f"☁️ Syncing course to Google Drive: {target.name}...")
    else:
        src = str(base_src)
        dest = base_dest
        log(f"☁️ Syncing ALL lectures from {src} to Google Drive ({dest})...")

    cmd = [rclone_bin, "sync", "-P", src, dest]
    try:
        ret = subprocess.run(cmd)
        if ret.returncode == 0:
            log("\n✓ Google Drive sync completed successfully!")
            return 0
        else:
            log("\n✗ Google Drive sync encountered an error.")
            return 1
    except KeyboardInterrupt:
        log("\nSync cancelled by user.")
        return 130
    except Exception as e:
        log(f"Error during Google Drive sync: {e}")
        return 1


def cmd_doctor(_args):
    """Checks dependencies, browser availability, login session, compute backend & AI setup."""
    log("=== Toledo DL Environment Diagnostics ===")
    has_missing = False

    # 1. Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        log(f"  [OK]      Python {py_ver}")
    else:
        log(f"  [MISSING] Python {py_ver} (requires >= 3.10)")
        has_missing = True

    # 2. ffmpeg
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        log(f"  [OK]      ffmpeg ({ffmpeg_path})")
    else:
        log("  [MISSING] ffmpeg not found. Install via apt/brew/winget.")
        has_missing = True

    # 3. yt-dlp
    ytdlp_path = shutil.which("yt-dlp") or (YTDLP if Path(YTDLP).exists() else None)
    if ytdlp_path:
        log(f"  [OK]      yt-dlp ({ytdlp_path})")
    else:
        log("  [MISSING] yt-dlp not found. Run: pip install yt-dlp")
        has_missing = True

    # 4. Chrome / Playwright
    ch = _browser_channel()
    if sync_playwright is not None and ch == "chrome":
        log("  [OK]      Browser: Google Chrome + Playwright")
    elif sync_playwright is not None:
        log("  [OK]      Browser: Playwright Chromium fallback")
    else:
        log("  [MISSING] Playwright not installed. Run the installer or 'pip install playwright && playwright install chromium'.")
        has_missing = True

    # 5. Toledo login session
    session_ok = False
    if STATE.exists():
        try:
            cookies = json.loads(STATE.read_text(encoding="utf-8")).get("cookies", [])
            session_ok = bool(cookies)
        except Exception:
            session_ok = False
    if session_ok:
        log(f"  [OK]      Toledo login session saved ({STATE})")
    else:
        log("  [WARN]    No saved Toledo login session. Run 'toledo-login' to sign in.")

    # 6. Compute backend (CUDA vs CPU)
    try:
        import ctranslate2
        cuda_n = ctranslate2.get_cuda_device_count()
        if cuda_n > 0:
            log(f"  [OK]      Compute backend: {cuda_n} CUDA GPU(s) detected (fast Whisper)")
        else:
            log("  [WARN]    Compute backend: CPU fallback (int8); use --no-transcribe on slow laptops.")
    except Exception:
        log("  [WARN]    faster-whisper / ctranslate2 not detected; transcription unavailable until installed.")

    # 7. Local Ollama reachability
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5) as resp:
            models = [m.get("name", "") for m in json.loads(resp.read().decode("utf-8")).get("models", [])]
        log(f"  [OK]      Local Ollama reachable ({len(models)} model(s) loaded)")
    except Exception:
        log("  [WARN]    Local Ollama not running (localhost:11434); match-slides uses keyword fallback.")

    # 8. Optional cloud API keys (presence only — never verified or used without explicit opt-in)
    gemini_set = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    openai_set = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    anthropic_set = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if gemini_set:
        log("  [INFO]    Cloud API: GEMINI_API_KEY detected in environment (connection not verified; cloud upload requires explicit --backend gemini)")
    if openai_set:
        log("  [INFO]    Cloud API: OPENAI_API_KEY detected in environment (connection not verified; cloud upload requires explicit --backend openai)")
    if anthropic_set:
        log("  [INFO]    Cloud API: ANTHROPIC_API_KEY detected in environment (connection not verified; cloud upload requires explicit --backend anthropic)")
    if not gemini_set and not openai_set and not anthropic_set:
        log("  [INFO]    Cloud API: no API keys set (all features stay 100% local)")

    log(f"  [INFO]    Output folder: {OUT}")
    return 1 if has_missing else 0


_SEARCH_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "to", "for", "with", "by", "as",
    "is", "are", "was", "were", "be", "what", "how", "why", "when", "where", "which",
    "does", "do", "can", "from", "about", "into", "between", "this", "that", "it",
}


def search_passages(
    query: str,
    course_filter: str | None = None,
    limit: int = 8,
    context_window: int = 2,
    kind_filter: str | None = None,
) -> list[dict]:
    """Local full-text search across deduplicated passages.jsonl with surrounding context."""
    q_clean = query.strip()
    if not q_clean or not OUT.exists():
        return []

    q_lower = q_clean.lower()
    raw_terms = re.findall(r"[a-z0-9_]+", q_lower)
    terms = [t for t in raw_terms if t not in _SEARCH_STOPWORDS and len(t) >= 2] or raw_terms
    if not terms:
        return []

    course_dirs = [
        d for d in sorted(OUT.iterdir())
        if d.is_dir() and not d.name.startswith(".") and _is_path_within_dir(d, OUT)
        and (not course_filter or course_filter.lower() in d.name.lower())
    ]

    scored_hits: list[tuple[float, dict, list[dict], int]] = []
    for cdir in course_dirs:
        pfile = cdir / "LLM_Knowledge" / "passages.jsonl"
        if not pfile.exists() or pfile.is_symlink() or not _is_path_within_dir(pfile, cdir):
            continue
        by_source: dict[str, list[dict]] = {}
        try:
            for line in pfile.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    continue
                sid = str(rec.get("source_id") or rec.get("source_path") or "")
                by_source.setdefault(sid, []).append(rec)
        except Exception:
            continue

        for sid, recs in by_source.items():
            for idx, rec in enumerate(recs):
                if kind_filter and rec.get("kind") != kind_filter:
                    continue
                txt = str(rec.get("text") or "").strip()
                if not txt:
                    continue
                t_low = txt.lower()
                matched_terms = [t for t in terms if re.search(rf"\b{re.escape(t)}", t_low)]
                if not matched_terms:
                    continue
                coverage = len(set(matched_terms)) / len(set(terms))
                tf = sum(t_low.count(t) for t in set(matched_terms))
                phrase_bonus = 3.0 if q_lower in t_low else 0.0
                score = coverage * 10.0 + min(tf, 8) * 0.6 + phrase_bonus
                # Small preference for concise slides/excerpts when coverage ties
                if rec.get("kind") == "pdf_page":
                    score += 0.3
                scored_hits.append((score, rec, recs, idx))

    scored_hits.sort(key=lambda x: (-x[0], str(x[1].get("course", "")), str(x[1].get("passage_id", ""))))

    results: list[dict] = []
    seen_dedup: set[tuple] = set()
    for score, rec, recs, idx in scored_hits:
        if len(results) >= max(1, limit):
            break
        sha = str(rec.get("content_sha256") or "")
        dedup_key = (
            (rec.get("course"), sha, rec.get("page"))
            if sha
            else (rec.get("course"), rec.get("source_id"), rec.get("passage_id"))
        )
        if dedup_key in seen_dedup:
            continue
        seen_dedup.add(dedup_key)

        win = max(0, context_window)
        if rec.get("kind") == "pdf_page":
            win = min(win, 1)
        lo = max(0, idx - win)
        hi = min(len(recs), idx + win + 1)
        ctx_recs = recs[lo:hi]
        ctx_parts: list[str] = []
        for cr in ctx_recs:
            ct = str(cr.get("text") or "").strip()
            if not ct:
                continue
            if cr.get("start_ts"):
                ctx_parts.append(f"[{cr['start_ts']}] {ct}")
            elif cr.get("page") is not None:
                ctx_parts.append(f"[p.{cr['page']}] {ct}")
            else:
                ctx_parts.append(ct)

        results.append({
            "score": round(score, 2),
            "course": rec.get("course"),
            "kind": rec.get("kind"),
            "source_path": rec.get("source_path"),
            "media_path": rec.get("media_path"),
            "page": rec.get("page"),
            "start_ts": ctx_recs[0].get("start_ts") if rec.get("start_ts") else None,
            "end_ts": ctx_recs[-1].get("end_ts") if rec.get("end_ts") else None,
            "match_text": str(rec.get("text") or "").strip(),
            "context_text": "\n".join(ctx_parts),
            "alias_paths": list(rec.get("alias_paths") or []),
            "passage_id": rec.get("passage_id"),
        })
    return results


def cmd_search(args):
    """Search deduplicated local passages.jsonl across courses with surrounding context."""
    query = " ".join(args.query).strip()
    if not query:
        print("Usage: toledo-dl search <query> [--course X] [--limit 8] [--context 2]")
        return 1
    hits = search_passages(
        query,
        course_filter=args.course,
        limit=args.limit,
        context_window=args.context,
        kind_filter=args.kind,
    )
    if args.json:
        print(json.dumps(hits, indent=2, ensure_ascii=False))
        return 0
    if not hits:
        print(f"No matches found for '{query}'.")
        return 0
    for i, h in enumerate(hits, 1):
        loc = f"p.{h['page']}" if h.get("page") is not None else (
            f"{h['start_ts']} -> {h['end_ts']}" if h.get("start_ts") else h.get("kind", "")
        )
        print(f"\n[{i}] ({h['score']}) {h['source_path']} [{loc}]")
        if h.get("alias_paths"):
            print(f"    aliases: {', '.join(h['alias_paths'])}")
        print("    " + h["context_text"].replace("\n", "\n    ")[:900])
    return 0


def main():
    ap = argparse.ArgumentParser(prog="toledo-dl", description="Download KU Leuven Toledo lecture recordings, course files & announcements")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Log in to Toledo via Chrome")
    sub.add_parser("check", help="Verify if Toledo session is active")
    sub.add_parser("doctor", help="Check system dependencies, GPU/CPU backend, login session & AI setup")

    # Calendar subparser
    scal = sub.add_parser("calendar", aliases=["schedule"], help="Check class schedule, total semester lectures, frequency & classrooms")
    scal.add_argument("--course", help="Filter by course name or code (case-insensitive substring)")
    scal.add_argument("--semester", default="1", choices=["1", "2", "all"], help="Semester to display (1, 2, or all; default: 1)")
    scal.add_argument("--refresh", action="store_true", help="Force re-fetch timetable from KU Leuven Kuloket")
    scal.add_argument("--upcoming", action="store_true", help="Only display upcoming classes from today onwards")
    scal.add_argument("--json", action="store_true", help="Output raw schedule data as JSON")

    # Match Slides subparser
    smatch = sub.add_parser("match-slides", aliases=["match-pdfs", "match"], help="Pair lecture transcripts with matching PDF slide decks (Option B)")
    smatch.add_argument("--course", help="Filter by course name")
    smatch.add_argument(
        "--backend",
        default="keyword",
        choices=["auto", "local", "gemini", "openai", "anthropic", "claude", "ollama", "keyword"],
        help="Matching backend: keyword (default CPU matcher), auto, local, gemini, openai, anthropic/claude, or ollama",
    )
    smatch.add_argument("--model", default=None, help="Model override (defaults: gemini-4-hydrogen, gpt-4o-mini, claude-sonnet-4-5, or phi4:14b)")
    smatch.add_argument("--no-video", action="store_true", help="Disable MP4 video frame sampling for cloud vision backends")
    smatch.add_argument("--limit", type=int, default=None, help="Maximum number of pending lectures to process")
    smatch.add_argument("--dry-run", action="store_true", help="Preview pending lectures and cloud upload scope without sending requests")
    smatch.add_argument("-y", "--yes", action="store_true", help="Confirm cloud upload without interactive prompt")
    smatch.add_argument("--force", action="store_true", help="Re-match even if already paired")

    # Transcribe subparser
    stran = sub.add_parser("transcribe", help="Transcribe downloaded lecture audio/video (local GPU/CPU Whisper or opt-in Gemini API)")
    stran.add_argument("--course", help="Filter by course name")
    stran.add_argument("--file", help="Filter by filename")
    stran.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "local", "gpu", "cpu", "gemini", "openai"],
        help="Transcription backend: auto (local CUDA GPU if present; stays local unless explicitly opted into cloud), local, gpu, cpu, gemini, or openai",
    )
    stran.add_argument("--model", default="auto", help="Model override (Whisper size or Gemini model, default: auto)")
    stran.add_argument("--lang", default="en", help="Language code (default: en)")
    stran.add_argument("--limit", type=int, default=None, help="Maximum number of pending recordings to transcribe")
    stran.add_argument("--max-minutes", type=float, default=None, help="Maximum total audio minutes for a cloud transcription batch (default: 180 for cloud)")
    stran.add_argument("--chunk-seconds", type=int, default=None, help="Audio chunk length in seconds for cloud transcription (default: 900)")
    stran.add_argument("-y", "--yes", action="store_true", help="Confirm cloud upload without interactive prompt")
    stran.add_argument("--dry-run", action="store_true", help="List pending recordings and cloud upload scope without transcribing")

    sstatus = sub.add_parser("sync-status", help="Show the latest sync result or watch a running sync")
    sstatus.add_argument("--watch", action="store_true", help="Watch until the current sync finishes")

    # Search subparser
    ssearch = sub.add_parser("search", help="Full-text search across deduplicated course passages with surrounding context")
    ssearch.add_argument("query", nargs="+", help="Search query terms")
    ssearch.add_argument("--course", help="Filter by course name (case-insensitive substring)")
    ssearch.add_argument("--kind", choices=["pdf_page", "transcript_segment", "document_section"], help="Restrict to passage type")
    ssearch.add_argument("--limit", type=int, default=8, help="Max results to return (default: 8)")
    ssearch.add_argument("--context", type=int, default=2, help="Surrounding segments/pages to include (default: 2)")
    ssearch.add_argument("--json", action="store_true", help="Output JSON results")

    help_texts = {
        "list": "List courses and available/new lecture recordings",
        "sync": "Complete sync: files, recordings, announcements, local transcription & LLM index",
        "pdfs": "Download all course PDFs",
        "files": "Download all course files, datasets, code, documents & external links",
        "announcements": "Check and display new professor announcements",
        "index": "Rebuild LLM_Knowledge/ markdown exports, Chapters/ bundles & LLM_INDEX.md",
    }
    for n in ("list", "sync", "pdfs", "files", "announcements", "index"):
        s = sub.add_parser(n, help=help_texts[n])
        s.add_argument("--course", help="Filter by course name (case-insensitive substring)")
        if n == "sync":
            s.add_argument("--limit", type=int, help="Max number of recordings to download")
            s.add_argument("--video", action="store_true", default=True, help="Download full MP4 video in addition to MP3 audio (default: True)")
            s.add_argument("--audio-only", action="store_true", help="Download only MP3 audio (skip MP4 video)")
            s.add_argument("--dry-run", action="store_true", help="List what would download without downloading")
            s.add_argument("--lectures-only", action="store_true", help="Only sync lecture recordings (skip files/documents)")
            s.add_argument("--files-only", action="store_true", help="Only sync course files & documents (skip recordings)")
            s.add_argument("--transcribe", action="store_true", help="Auto-transcribe lectures after sync (enabled by default)")
            s.add_argument("--no-transcribe", action="store_true", help="Skip automatic transcription during sync")
            s.add_argument("--defer-cpu-transcription", action="store_true", help="Leave transcription pending when no CUDA GPU is detected")
            s.add_argument("--transcribe-backend", default=None, choices=["local", "gpu", "cpu", "gemini", "openai", "skip"], help="Transcription backend override for sync")
            s.add_argument("--match-backend", default=None, choices=["local", "gemini", "openai", "anthropic", "claude", "ollama", "keyword"], help="Slide-matching backend override for sync")
            s.add_argument("--pdf-vision-backend", default=None, choices=["off", "ollama", "gemini"], help="Visual PDF scanning backend override for sync (off, ollama local GPU, or gemini cloud API)")
            s.add_argument("--check-revisions", action="store_true", help="Compare attachment contents even when Toledo supplies no revision metadata")
            s.add_argument("--notify", action="store_true", help="Show the completion summary as a Windows desktop notification")
            s.add_argument("--model", default="auto", help="Whisper model size for auto-transcription (default: auto)")
            s.add_argument("--lang", default="en", help="Language code for auto-transcription (default: en)")
        elif n == "pdfs":
            s.add_argument("--dry-run", action="store_true", help="List PDFs without downloading")
        elif n == "files":
            s.add_argument("--check-revisions", action="store_true", help="Compare attachment contents even without revision metadata")
            s.add_argument("--ext", help="Filter by file extension(s) separated by comma, e.g. 'csv,xlsx,r,sas'")
            s.add_argument("--dry-run", action="store_true", help="List files without downloading")
        elif n == "announcements":
            s.add_argument("--all", action="store_true", help="Show all announcements (including previously checked)")
        elif n == "index":
            s.add_argument("--vision-backend", default="off", choices=["off", "ollama", "gemini"], help="Visual PDF scanning backend: off (default text extraction), ollama (local GPU), or gemini (opt-in cloud API)")
            s.add_argument("--vision-model", default=None, help="Override PDF vision model (defaults: qwen2.5vl:7b for ollama, gemini-2.5-flash for gemini)")
            s.add_argument("--limit", type=int, default=None, help="Maximum number of PDFs to vision-scan in this batch")
            s.add_argument("--dry-run", action="store_true", help="Preview pending PDFs and estimated cloud cost without scanning")
            s.add_argument("-y", "--yes", action="store_true", help="Confirm cloud PDF vision upload without interactive prompt")

    a = ap.parse_args()
    cmds = {
        "login": cmd_login,
        "check": cmd_check,
        "doctor": cmd_doctor,
        "calendar": cmd_calendar,
        "schedule": cmd_calendar,
        "match-slides": cmd_match_slides,
        "match-pdfs": cmd_match_slides,
        "match": cmd_match_slides,
        "list": cmd_list,
        "sync": cmd_sync,
        "sync-status": cmd_sync_status,
        "pdfs": cmd_pdfs,
        "files": cmd_files,
        "announcements": cmd_announcements,
        "index": cmd_index,
        "search": cmd_search,
        "transcribe": cmd_transcribe,
    }
    sys.exit(cmds[a.cmd](a))


if __name__ == "__main__":
    main()
