import subprocess
import time


def session_exists(session: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    )
    return result.returncode == 0


def window_exists(session: str, window: str) -> bool:
    result = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#W"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return window in result.stdout.splitlines()


def send_input(session: str, text: str, window: str) -> None:
    # text is passed as a list element — shell is not involved, so no quoting needed.
    # Applying shlex.quote here would send literal quote characters to Claude Code.
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{session}:{window}", text, "Enter"],
        check=True,
    )
    # Brief pause to ensure tmux flushes the keys to the pty buffer.
    time.sleep(0.3)


def create_window(session: str, window: str, cwd: str) -> None:
    subprocess.run(
        ["tmux", "new-window", "-t", session, "-n", window, "-c", cwd],
        check=True,
    )
    # Allow Claude Code to start before any input is sent.
    time.sleep(1.0)
    send_input(session, "claude --dangerously-skip-permissions", window)


def ensure_window(session: str, window: str, cwd: str) -> None:
    if not window_exists(session, window):
        create_window(session, window, cwd)
