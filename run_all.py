"""Start Reviewly (:5050) and both example partner shops (:5001, :5002).

    python run_all.py           # start everything
    python run_all.py --reset   # delete all three databases first, then start
"""
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DBS = ["reviewly.db", "shops/kickvault/shop.db", "shops/fernfig/shop.db"]
APPS = [
    ("Reviewly  ", ["-c", "from app import app; app.run(port=5050, debug=False)"], 5050),
    ("KICKVAULT ", ["-m", "shops.kickvault.app"], 5001),
    ("Fern & Fig", ["-m", "shops.fernfig.app"], 5002),
]


def wait_for(url, seconds=20):
    for _ in range(seconds * 4):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except OSError:
            time.sleep(0.25)
    return False


def main():
    if "--reset" in sys.argv:
        for db in DBS:
            path = os.path.join(ROOT, db)
            if os.path.exists(path):
                os.remove(path)
                print(f"removed {db}")
    procs = []
    try:
        for i, (name, args, port) in enumerate(APPS):
            procs.append(subprocess.Popen([sys.executable, *args], cwd=ROOT))
            if i == 0 and not wait_for("http://127.0.0.1:5050/api/v1/health"):
                print("Reviewly did not start on :5050.")
        time.sleep(1.5)
        print("\n" + "\n".join(f"  {name}  http://127.0.0.1:{port}" for name, _, port in APPS))
        print("\n  Ctrl+C stops all three.\n")
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait()


if __name__ == "__main__":
    main()
