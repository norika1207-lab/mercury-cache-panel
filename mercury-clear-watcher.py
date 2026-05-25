#!/usr/bin/env python3
"""mercury-clear-watcher.py — watches the panel's auto-clear-trigger.json and
optionally fires a real /clear to the focused Claude Code terminal window.

SAFETY by default:
  - Manual mode (default): shows a macOS confirmation dialog. You click "Send /clear" to proceed.
  - Auto mode (--auto): no confirmation, sends /clear directly. Use only if you trust the heuristic.

How /clear is sent:
  - Finds the frontmost terminal app (Terminal.app, iTerm2, Ghostty, Warp)
  - Uses AppleScript keystroke to type "/clear" + Enter
  - Only fires if a terminal is actually focused

This does NOT touch background sessions or non-focused windows.
"""
import json, time, sys, subprocess, argparse
from pathlib import Path

TRIGGER = Path.home() / ".mercury-cache" / "auto-clear-trigger.json"
LAST_FIRED = Path.home() / ".mercury-cache" / ".last-clear-ts"

SUPPORTED_TERMINALS = ["Terminal", "iTerm2", "iTerm", "Ghostty", "WarpPreview", "Warp"]

def frontmost_app():
    try:
        r = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=3
        )
        return r.stdout.strip()
    except Exception:
        return None

def send_clear():
    app = frontmost_app()
    if app not in SUPPORTED_TERMINALS:
        return False, f"frontmost app is {app}, not a supported terminal"
    script = f'''
    tell application "System Events"
        tell process "{app}"
            keystroke "/clear"
            key code 36
        end tell
    end tell
    '''
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=5)
        return True, f"sent /clear to {app}"
    except subprocess.CalledProcessError as e:
        return False, f"AppleScript failed: {e.stderr.decode()[:100]}"

def confirm_dialog(trigger_data):
    n = len(trigger_data.get("triggers", []))
    msg = f"Mercury detected {n} session(s) with high wasted cache while idle. Send /clear to the focused terminal now?"
    script = f'''
    display dialog "{msg}" with title "Mercury Cache Panel" buttons {{"Skip", "Send /clear"}} default button "Send /clear" with icon caution
    '''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
        return "Send /clear" in r.stdout
    except Exception:
        return False

def notify(title, msg):
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}" sound name "Pop"'],
                   check=False, capture_output=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true", help="No confirmation dialog (dangerous)")
    ap.add_argument("--cooldown", type=int, default=600, help="Min seconds between auto-fires")
    args = ap.parse_args()

    last_seen = 0
    print(f"watching {TRIGGER} (auto={args.auto}, cooldown={args.cooldown}s)")
    while True:
        try:
            if TRIGGER.exists():
                d = json.load(open(TRIGGER))
                ts = d.get("ts", 0)
                if ts > last_seen:
                    last_seen = ts
                    # cooldown
                    if LAST_FIRED.exists():
                        last = float(LAST_FIRED.read_text().strip() or "0")
                        if time.time() - last < args.cooldown:
                            print(f"[{time.strftime('%H:%M:%S')}] cooldown active, skip"); time.sleep(20); continue
                    proceed = True if args.auto else confirm_dialog(d)
                    if proceed:
                        ok, msg = send_clear()
                        print(f"[{time.strftime('%H:%M:%S')}] {msg}")
                        notify("Mercury", msg)
                        if ok: LAST_FIRED.write_text(str(time.time()))
                    else:
                        print(f"[{time.strftime('%H:%M:%S')}] user declined")
        except Exception as e:
            print(f"err: {e}")
        time.sleep(15)

if __name__ == "__main__":
    main()
