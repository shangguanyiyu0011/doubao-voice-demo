"""Windows launcher: preserve errors and reuse this app when already running."""
import json
from pathlib import Path
import subprocess
import sys
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8765/"

def main():
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required.")
        return 1
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL + "api/bootstrap", timeout=2) as response:
            existing = json.load(response)
        if existing.get("app") == "voice-script-studio-local":
            webbrowser.open(URL)
            print("The service is already running. Opened the correct page:", URL)
            print("Keep the original server window open.")
            return 0
    except Exception:
        pass
    print("Starting local service. Keep this window open.", flush=True)
    try:
        return subprocess.call([sys.executable, str(ROOT / "server.py"), "--open"], cwd=ROOT)
    except OSError as error:
        print("Could not start the local service:", error)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
