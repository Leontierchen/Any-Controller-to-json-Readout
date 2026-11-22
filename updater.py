import os
import sys
import json
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
        return None

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
        "asset_url": target_asset.get("url"),
        "asset_name": target_asset.get("name"),
        "size": target_asset.get("size", 0),
    }


def download_and_install(update_info: dict, progress_callback=None) -> bool:
    """
    Lädt das Update herunter und führt den Austausch komplett unsichtbar
    über VBScript + File-Lock-Prüfung durch (0 Konsolenfenster, kein Aufhängen).
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

    # 1. Asset über die GitHub API-URL herunterladen
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

    # 2. Entpacken falls ZIP (--onedir oder ZIP-Release), sonst direkte EXE
    source_target = download_path
    is_zip = asset_name.lower().endswith(".zip")
    if is_zip:
        extract_dir = os.path.join(temp_dir, "extracted")
        with zipfile.ZipFile(download_path, "r") as zf:
            zf.extractall(extract_dir)
        entries = os.listdir(extract_dir)
        if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
            source_target = os.path.join(extract_dir, entries[0])
        else:
            source_target = extract_dir

    # 3. Unsichtbares VBScript zum Austausch & Neustart erzeugen
    vbs_path = os.path.join(temp_dir, "apply_update.vbs")

    def _vbs_escape(path_str: str) -> str:
        return path_str.replace('"', '""')

    exe_esc = _vbs_escape(current_exe)
    dir_esc = _vbs_escape(app_dir)
    src_esc = _vbs_escape(source_target)
    tmp_esc = _vbs_escape(temp_dir)

    if is_zip:
        copy_block = f'fso.CopyFolder "{src_esc}\\*", "{dir_esc}\\", True\nOn Error Resume Next\nfso.CopyFile "{src_esc}\\*", "{dir_esc}\\", True'
    else:
        copy_block = f'fso.CopyFile "{src_esc}", "{exe_esc}", True'

    vbs_content = f"""Option Explicit
Dim fso, shell, env, i, locked, testFile
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' 1. Warten, bis Windows die alte EXE-Datei freigibt (max. 20 Sekunden)
For i = 1 To 40
    WScript.Sleep 500
    On Error Resume Next
    Err.Clear
    Set testFile = fso.OpenTextFile("{exe_esc}", 8, False)
    If Err.Number = 0 Then
        testFile.Close
        locked = False
        Exit For
    Else
        locked = True
    End If
    On Error GoTo 0
Next

' 2. Neue Dateien kopieren / überschreiben
On Error Resume Next
{copy_block}
On Error GoTo 0

' 3. PyInstaller-Umgebungsvariablen vor dem Neustart bereinigen
Set env = shell.Environment("Process")
env.Remove("_MEIPASS")
env.Remove("_MEIPASS2")
env.Remove("_PYI_APPLICATION_HOME_DIR")
env.Remove("_PYI_PARENT_PROCESS_LEVEL")
env.Remove("_PYI_SPLASH_IPC")

' 4. Aktualisierte EXE normal starten & Temp-Ordner aufräumen
WScript.Sleep 300
shell.Run Chr(34) & "{exe_esc}" & Chr(34), 1, False
WScript.Sleep 1000
On Error Resume Next
fso.DeleteFolder "{tmp_esc}", True
"""

    with open(vbs_path, "w", encoding="utf-16") as f:
        f.write(vbs_content)

    # 4. Umgebung bereinigen und wscript.exe komplett fensterlos starten
    clean_env = os.environ.copy()
    for key in list(clean_env.keys()):
        if key.startswith("_PYI") or key.startswith("_MEI"):
            clean_env.pop(key, None)

    subprocess.Popen(
        ["wscript.exe", "//B", "//Nologo", vbs_path],
        env=clean_env,
        close_fds=True,
    )
    os._exit(0)