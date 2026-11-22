import os
import sys
import json
import time
import shutil
import zipfile
import tempfile
import subprocess
import urllib.request
import urllib.error

# ==============================================================================
# KONFIGURATION (Wird vor dem Kompilieren durch dein Build-Tool befüllt)
# ==============================================================================
GITHUB_TOKEN = "TOKEN_PLACEHOLDER"  # Wird vor dem Build injiziert
# ==============================================================================


def _parse_version(v_str: str) -> tuple:
    """Wandelt 'v1.2.3' oder '1.2.3' sicher in ein vergleichbares Tuple (1, 2, 3) um."""
    clean = v_str.strip().lstrip("vV").split("-")[0]
    parts = []
    for p in clean.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _get_headers(accept: str = "application/vnd.github+json") -> dict:
    headers = {
        "Accept": accept,
        "User-Agent": "Python-AutoUpdater-App",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN and GITHUB_TOKEN != "TOKEN_PLACEHOLDER":
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def check_for_updates(repo: str, current_version: str) -> dict | None:
    """
    Prüft auf ein neues GitHub-Release.
    :param repo: Format 'Leontierchen/RepoName'
    :param current_version: Deine __version__ Variable
    :return: Dict mit Update-Infos oder None, wenn aktuell / Fehler
    """
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers=_get_headers())

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[Updater] GitHub API Fehler ({e.code}): {e.reason}")
        return None
    except Exception as e:
        print(f"[Updater] Verbindungsfehler: {e}")
        return None

    latest_tag = data.get("tag_name", "")
    if not latest_tag:
        return None

    if _parse_version(latest_tag) <= _parse_version(current_version):
        return None  # Bereits aktuell

    # Passendes Asset suchen (.exe bevorzugt, alternativ .zip für --onedir)
    assets = data.get("assets", [])
    target_asset = None
    for asset in assets:
        name = asset.get("name", "").lower()
        if name.endswith(".exe") or name.endswith(".zip"):
            target_asset = asset
            break

    if not target_asset:
        print("[Updater] Neues Release gefunden, aber kein .exe oder .zip Asset vorhanden.")
        return None

    return {
        "version": latest_tag,
        "notes": data.get("body", ""),
        "asset_url": target_asset.get("url"),  # Wichtig: API-URL für private Repos!
        "asset_name": target_asset.get("name"),
        "size": target_asset.get("size", 0),
    }


def download_and_install(update_info: dict, progress_callback=None) -> bool:
    """
    Lädt das Update herunter, schreibt ein sauberes Batch-Skript zum Austausch
    und startet die Anwendung ohne PyInstaller-Env-Leichen neu.
    """
    if not getattr(sys, "frozen", False):
        print("[Updater] Nicht als EXE kompiliert – Update-Installation übersprungen.")
        return False

    asset_url = update_info["asset_url"]
    asset_name = update_info["asset_name"]
    current_exe = os.path.abspath(sys.executable)
    app_dir = os.path.dirname(current_exe)

    temp_dir = tempfile.mkdtemp(prefix="app_update_")
    download_path = os.path.join(temp_dir, asset_name)

    # 1. Asset über die API-URL herunterladen (funktioniert für Public & Private Repos)
    req = urllib.request.Request(
        asset_url,
        headers=_get_headers(accept="application/octet-stream"),
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response, open(download_path, "wb") as out_file:
            total_size = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 65536
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                out_file.write(chunk)
                downloaded += len(chunk)
                if progress_callback and total_size > 0:
                    progress_callback(int((downloaded / total_size) * 100))
    except Exception as e:
        print(f"[Updater] Download fehlgeschlagen: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False

    # 2. Entpacken falls ZIP (--onedir), sonst direkte EXE (--onefile)
    source_target = download_path
    is_zip = asset_name.lower().endswith(".zip")
    if is_zip:
        extract_dir = os.path.join(temp_dir, "extracted")
        with zipfile.ZipFile(download_path, "r") as zf:
            zf.extractall(extract_dir)
        # Falls im ZIP ein einzelner Unterordner liegt, diesen als Quelle nehmen
        entries = os.listdir(extract_dir)
        if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
            source_target = os.path.join(extract_dir, entries[0])
        else:
            source_target = extract_dir

    # 3. Updater-Batch erstellen (wartet auf Prozess-Ende, kopiert, startet neu)
    bat_path = os.path.join(temp_dir, "apply_update.bat")
    pid = os.getpid()

    if is_zip:
        copy_cmd = f'xcopy /E /I /Y "{source_target}\\*" "{app_dir}\\"'
    else:
        copy_cmd = f'move /Y "{source_target}" "{current_exe}"'

    bat_content = f"""@echo off
chcp 65001 > nul
:waitloop
tasklist /FI "PID eq {pid}" 2>NUL | find /I /N "{pid}">NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 1 /nobreak > nul
    goto waitloop
)

timeout /t 1 /nobreak > nul
{copy_cmd}

REM PyInstaller-Umgebungsvariablen vor dem Neustart zwingend löschen!
set _MEIPASS=
set _MEIPASS2=
set _PYI_APPLICATION_HOME_DIR=
set _PYI_PARENT_PROCESS_LEVEL=
set _PYI_SPLASH_IPC=

start "" "{current_exe}"
rmdir /S /Q "{temp_dir}"
exit
"""

    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(bat_content)

    # 4. Batch komplett abgekoppelt starten und aktuelle App beenden
    clean_env = os.environ.copy()
    for key in list(clean_env.keys()):
        if key.startswith("_PYI") or key.startswith("_MEI"):
            clean_env.pop(key, None)

    subprocess.Popen(
        ["cmd.exe", "/c", bat_path],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        env=clean_env,
        close_fds=True,
    )
    os._exit(0)