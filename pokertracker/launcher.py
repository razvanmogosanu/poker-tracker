"""The desktop shortcut: a splash that starts the server and gets out of the way.

Run with `pythonw -m pokertracker.launcher`, so there is no console window at
all. The splash deals four suits while the server comes up, says so, opens the
dashboard and fades out. The server is a separate, windowless process that
outlives the splash; the Stop button on the live page is how it ends, since
there is no window left to close.

Its output goes to `tracker.log` beside the project, because a pythonw process
has nowhere else to print and a server that dies on start-up has to be able to
say why. The splash shows the tail of that log rather than vanishing.
"""

from __future__ import annotations

import subprocess
import sys
import time
import tkinter as tk
import webbrowser
from pathlib import Path

from .serve import _already_running

PORT = 8765
URL = f"http://127.0.0.1:{PORT}/"
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "tracker.log"
START_TIMEOUT = 60.0

W, H = 360, 200
FELT, FELT_2, INK, MUTED = "#0f3d2e", "#16533f", "#f4f1e8", "#9fbfb0"
RED, BLACK = "#e0565b", "#f4f1e8"
SUITS = [("♠", BLACK), ("♥", RED), ("♦", RED), ("♣", BLACK)]


def _start_server() -> subprocess.Popen:
    """The server as a detached process with no window, logging to LOG."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    log = open(LOG, "w", encoding="utf-8", buffering=1)
    flags = 0
    if sys.platform == "win32":
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                 | subprocess.CREATE_NEW_PROCESS_GROUP)
    return subprocess.Popen(
        [str(pythonw if pythonw.exists() else exe), "-u",
         "-m", "pokertracker.cli", "serve", "--no-open"],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)


class Splash:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        x = (self.root.winfo_screenwidth() - W) // 2
        y = (self.root.winfo_screenheight() - H) // 2
        self.root.geometry(f"{W}x{H}+{x}+{y}")
        self.c = tk.Canvas(self.root, width=W, height=H, bg=FELT,
                           highlightthickness=0)
        self.c.pack()
        self.c.create_rectangle(6, 6, W - 6, H - 6, outline=FELT_2, width=2)
        self.c.create_text(W / 2, 38, text="Poker tracker", fill=INK,
                           font=("Segoe UI Semibold", 16))
        self.cards = []
        for i, (suit, colour) in enumerate(SUITS):
            cx = W / 2 + (i - 1.5) * 52
            card = self.c.create_rectangle(cx - 20, 72, cx + 20, 124,
                                           fill=FELT_2, outline=MUTED)
            pip = self.c.create_text(cx, 98, text=suit, fill=FELT_2,
                                     font=("Segoe UI Symbol", 22))
            self.cards.append((card, pip, colour, cx))
        self.status = self.c.create_text(W / 2, 158, text="Starting…",
                                         fill=MUTED, font=("Segoe UI", 10))
        self.tick = 0
        self.spinning = True
        self.c.bind("<Button-1>", lambda _e: self.root.destroy())

    # -- animation ---------------------------------------------------------
    def fade_in(self, alpha: float = 0.0) -> None:
        alpha = min(alpha + 0.08, 1.0)
        self.root.attributes("-alpha", alpha)
        if alpha < 1.0:
            self.root.after(16, self.fade_in, alpha)

    def spin(self) -> None:
        if not self.spinning:
            return
        # One card at a time turns face up and lifts, then settles back.
        lit = (self.tick // 6) % len(self.cards)
        for i, (card, pip, colour, cx) in enumerate(self.cards):
            up = i == lit
            lift = -8 if up else 0
            self.c.coords(card, cx - 20, 72 + lift, cx + 20, 124 + lift)
            self.c.coords(pip, cx, 98 + lift)
            self.c.itemconfig(card, fill=INK if up else FELT_2)
            self.c.itemconfig(pip, fill=(FELT if colour == BLACK else RED)
                              if up else FELT_2)
        self.tick += 1
        self.root.after(45, self.spin)

    def reveal(self, text: str) -> None:
        """All four face up: the server is listening."""
        self.spinning = False
        for card, pip, colour, cx in self.cards:
            self.c.coords(card, cx - 20, 72, cx + 20, 124)
            self.c.coords(pip, cx, 98)
            self.c.itemconfig(card, fill=INK)
            self.c.itemconfig(pip, fill=FELT if colour == BLACK else RED)
        self.c.itemconfig(self.status, text=text, fill=INK)

    def fade_out(self, alpha: float = 1.0) -> None:
        alpha -= 0.05
        if alpha <= 0:
            self.root.destroy()
            return
        self.root.attributes("-alpha", alpha)
        self.root.after(20, self.fade_out, alpha)

    def fail(self, text: str) -> None:
        self.spinning = False
        self.c.itemconfig(self.status, text="Could not start — click to close",
                          fill=RED)
        self.root.geometry(f"{W}x{H + 120}")
        self.c.config(height=H + 120)
        self.c.create_text(18, H, text=text, fill=MUTED, anchor="nw",
                           width=W - 36, font=("Consolas", 8))

    # -- flow --------------------------------------------------------------
    def run(self) -> None:
        self.fade_in()
        self.spin()
        self.root.after(300, self.launch)
        self.root.mainloop()

    def launch(self) -> None:
        if _already_running(PORT):
            self.done("Already running — opening the dashboard")
            return
        self.proc = _start_server()
        self.deadline = time.monotonic() + START_TIMEOUT
        self.wait()

    def wait(self) -> None:
        if _already_running(PORT):
            self.done("Running in the background")
        elif self.proc.poll() is not None or time.monotonic() > self.deadline:
            tail = LOG.read_text(encoding="utf-8", errors="replace")
            self.fail("\n".join(tail.strip().splitlines()[-8:]) or "no output")
        else:
            self.root.after(150, self.wait)

    def done(self, text: str) -> None:
        self.reveal(text)
        webbrowser.open(URL)
        self.root.after(1400, self.fade_out)


def main() -> None:
    Splash().run()


if __name__ == "__main__":
    main()
