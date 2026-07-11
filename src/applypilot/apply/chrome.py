"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import json
import logging
import platform
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from applypilot import config
from applypilot.apply.google_passwords import (
    PROVIDER_NAME as GOOGLE_PASSWORD_MANAGER,
    choose_chrome_profile_for_google_passwords,
    configure_google_password_preferences,
)
from applypilot.apply.onepassword import DEFAULT_EXTENSION_ID, choose_chrome_profile_for_extension

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = 9222

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            # Unix: kill entire process group
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


def _kill_on_port(port: int) -> None:
    """Kill any process listening on a specific port (zombie cleanup).

    Uses netstat on Windows, lsof on macOS/Linux.
    """
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        _kill_process_tree(int(pid))
        else:
            # macOS / Linux
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    _kill_process_tree(int(pid_str))
    except FileNotFoundError:
        logger.debug("Port-kill tool not found (netstat/lsof) for port %d", port)
    except Exception:
        logger.debug("Failed to kill process on port %d", port, exc_info=True)


# ---------------------------------------------------------------------------
# Worker profile management
# ---------------------------------------------------------------------------

def setup_worker_profile(worker_id: int) -> Path:
    """Create a least-privilege Chrome profile for a worker.

    Only the selected Chrome profile's password-store databases and preference
    files are copied. History, cookies, autofill data, extensions, other Chrome
    profiles, and browsing telemetry are intentionally excluded.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the worker's Chrome user-data directory.
    """
    source_profile_name = config.get_chrome_profile_directory()
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}-minimal-v1"
    marker = profile_dir / ".applypilot-minimal-profile"
    if marker.exists() and (profile_dir / source_profile_name).exists():
        return profile_dir  # Already initialized

    # Reuse another minimal worker as the source when available. Never reuse the
    # legacy broad worker-N clones, which may contain full browser histories.
    source: Path | None = None
    for wid in range(10):
        if wid == worker_id:
            continue
        candidate = config.CHROME_WORKER_DIR / f"worker-{wid}-minimal-v1"
        if (
            (candidate / ".applypilot-minimal-profile").exists()
            and (candidate / source_profile_name).exists()
        ):
            source = candidate
            break
    if source is None:
        source = config.get_chrome_user_data()

    logger.info(
        "[worker-%d] Creating minimal Chrome profile from %s/%s...",
        worker_id,
        source.name,
        source_profile_name,
    )
    profile_dir.mkdir(parents=True, exist_ok=True)

    root_files = ("Local State",)
    profile_files = (
        "Preferences",
        "Secure Preferences",
        "Login Data",
        "Login Data-journal",
        "Login Data For Account",
        "Login Data For Account-journal",
    )
    source_profile = source / source_profile_name
    destination_profile = profile_dir / source_profile_name
    destination_profile.mkdir(parents=True, exist_ok=True)

    for name in root_files:
        item = source / name
        if item.is_file():
            shutil.copy2(item, profile_dir / name)
    for name in profile_files:
        item = source_profile / name
        if item.is_file():
            shutil.copy2(item, destination_profile / name)

    marker.write_text("minimal-v1\n", encoding="utf-8")

    return profile_dir


def _patch_chrome_preferences(
    profile_dir: Path,
    *,
    profile_directory: str,
    credential_provider: str,
) -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / profile_directory / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open blank
        prefs.setdefault("session", {}).pop("startup_urls", None)
        if credential_provider == GOOGLE_PASSWORD_MANAGER:
            prefs["credentials_enable_service"] = True
            prefs.setdefault("password_manager", {})["saving_enabled"] = True
            prefs.setdefault("autofill", {})["profile_enabled"] = True
        else:
            prefs["credentials_enable_service"] = False
            prefs.setdefault("password_manager", {})["saving_enabled"] = False
            prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
        if credential_provider == GOOGLE_PASSWORD_MANAGER:
            configure_google_password_preferences(profile_dir / profile_directory)
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

def launch_chrome(
    worker_id: int,
    port: int | None = None,
    headless: bool = False,
    profile_directory: str | None = None,
    credential_provider: str = GOOGLE_PASSWORD_MANAGER,
    onepassword_extension_id: str = DEFAULT_EXTENSION_ID,
) -> subprocess.Popen:
    """Launch a Chrome instance with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).
        profile_directory: Chrome profile directory inside the user-data root.
        credential_provider: Job-site credential provider.
        onepassword_extension_id: Extension id used for automatic profile choice.

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    profile_dir = setup_worker_profile(worker_id)

    # Kill any zombie Chrome from a previous run on this port
    _kill_on_port(port)

    chrome_exe = config.get_chrome_path()
    if credential_provider == "onepassword":
        launch_profile = (
            profile_directory
            or choose_chrome_profile_for_extension(profile_dir, onepassword_extension_id)
            or config.get_chrome_profile_directory()
        )
    elif credential_provider == GOOGLE_PASSWORD_MANAGER:
        launch_profile = (
            profile_directory
            or choose_chrome_profile_for_google_passwords(profile_dir)
            or config.get_chrome_profile_directory()
        )
    else:
        launch_profile = profile_directory or config.get_chrome_profile_directory()

    # Patch preferences to suppress restore nag and honor credential provider.
    _patch_chrome_preferences(
        profile_dir,
        profile_directory=launch_profile,
        credential_provider=credential_provider,
    )

    disable_features = ["InfiniteSessionRestore"]
    if credential_provider != GOOGLE_PASSWORD_MANAGER:
        disable_features.append("PasswordManagerOnboarding")

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        f"--profile-directory={launch_profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1024,768",
        "--disable-session-crashed-bubble",
        f"--disable-features={','.join(disable_features)}",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--disable-popup-blocking",
        # Block dangerous permissions at browser level
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--deny-permission-prompts",
        "--disable-notifications",
    ]
    if credential_provider != GOOGLE_PASSWORD_MANAGER:
        cmd.extend([
            "--password-store=basic",
            "--disable-save-password-bubble",
        ])
    if headless:
        cmd.append("--headless=new")

    # On Unix, start in a new process group so we can kill the whole tree
    kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() != "Windows":
        import os
        kwargs["preexec_fn"] = os.setsid

    proc = subprocess.Popen(cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc

    # Give Chrome time to start and open the debug port
    time.sleep(3)
    logger.info("[worker-%d] Chrome started on port %d (pid %d)",
                worker_id, port, proc.pid)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port in case of zombies
    _kill_on_port(BASE_CDP_PORT)


def reset_worker_dir(worker_id: int) -> Path:
    """Create a unique per-job working directory without deleting evidence.

    Each job gets a fresh directory so file conflicts do not bleed between
    jobs, while prior resumes, screenshots, and confirmation artifacts remain
    available for the campaign audit trail.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_root = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    worker_root.mkdir(parents=True, exist_ok=True)
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + f"-{uuid4().hex[:8]}"
    )
    worker_dir = worker_root / run_id
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port for any orphan
    _kill_on_port(BASE_CDP_PORT)
