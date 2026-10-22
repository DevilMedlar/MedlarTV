"""Fixed MediaPlayer launch; filenames and duration travel only as JSON data."""
import json
import subprocess


# Never interpolate configuration into this program. ASCII JSON preserves Unicode
# through escapes, independently of the Windows PowerShell console code page.
MEDIA_PLAYER_PROGRAM = """
$ErrorActionPreference = 'Stop'
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
Add-Type -AssemblyName presentationCore
$player = New-Object System.Windows.Media.MediaPlayer
$player.Open([Uri]$payload.path)
$player.Volume = 1
$player.Play()
Start-Sleep -Milliseconds ([int]$payload.duration_ms)
$player.Close()
""".strip()


def launch_media_player(path: str, duration_ms: int) -> bool:
    """Report launch/input success, not verified audible playback; do not wait."""
    payload = json.dumps({"path": path, "duration_ms": duration_ms}, ensure_ascii=True)
    try:
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", MEDIA_PLAYER_PROGRAM],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, encoding="ascii", shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Close stdin so the fixed reader sees EOF. Playback remains asynchronous.
        try:
            process.stdin.write(payload)
        finally:
            process.stdin.close()
        return True
    except (OSError, ValueError, AttributeError):
        return False
