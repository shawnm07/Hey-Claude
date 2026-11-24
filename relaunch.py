"""
Starts Hey Claude again after "Restart Hey Claude" (chat gear menu).

Hey Claude can't start its own replacement: everything it starts is closed with it (job object), and Task
Scheduler ignores a start while the old one is still running. So it has Windows start this small helper
outside its own process tree, then exits. The helper waits for it to be gone, starts the "Hey Claude"
scheduled task, and falls back to starting the program directly.

Usage: pythonw relaunch.py <pid of the Hey Claude that is exiting>
"""
import ctypes
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK, MUTEX = "Hey Claude", "Local\\HeyClaudeListener"
NO_WINDOW = 0x08000000
kernel32 = ctypes.WinDLL("kernel32")
kernel32.OpenProcess.restype = kernel32.OpenMutexW.restype = ctypes.c_void_p


def wait_gone(pid, seconds):
    h = kernel32.OpenProcess(0x00100000, False, pid)                 # SYNCHRONIZE
    if h:
        kernel32.WaitForSingleObject(ctypes.c_void_p(h), int(seconds * 1000))
        kernel32.CloseHandle(ctypes.c_void_p(h))


def running():
    h = kernel32.OpenMutexW(0x00100000, False, MUTEX)
    if h:
        kernel32.CloseHandle(ctypes.c_void_p(h))
    return bool(h)


def started_within(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if running():
            return True
        time.sleep(0.3)
    return False


def main():
    wait_gone(int(sys.argv[1]), 20)
    time.sleep(2)                                                    # let Task Scheduler notice it ended
    for _ in range(2):
        if running():
            return
        r = subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True, creationflags=NO_WINDOW)
        if r.returncode == 0 and started_within(20):
            return
    subprocess.Popen([sys.executable, str(HERE / "hey_claude.py")], cwd=str(HERE))


if __name__ == "__main__":
    main()
