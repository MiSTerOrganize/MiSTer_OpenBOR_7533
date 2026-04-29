#!/usr/bin/env python3
"""
Deploy MiSTer_OpenBOR_7533 to a running MiSTer over SSH.

Stops the running OpenBOR + daemon, uploads the three files that changed
(ARM binary, daemon script, RBF), removes the old RBFs, and re-launches
the daemon.

Uses paramiko so we don't need sshpass on Windows. Credentials are the
MiSTer default (root/1).
"""

import os
import sys
import glob
import paramiko
from pathlib import Path

HOST = "192.168.1.51"
USER = "root"
PASS = "1"
REPO = Path(__file__).resolve().parent

# Find the latest local RBF (by name sort = by date in filename).
_rbfs = sorted(glob.glob(str(REPO / "_Other" / "OpenBOR_7533_*.rbf")))
LOCAL_RBF = Path(_rbfs[-1]) if _rbfs else None

FILES = [
    # (local path, remote path, chmod)
    (REPO / "games/OpenBOR_7533/OpenBOR",
     "/media/fat/games/OpenBOR_7533/OpenBOR", 0o755),
    (REPO / "games/OpenBOR_7533/openbor_7533_daemon.sh",
     "/media/fat/games/OpenBOR_7533/openbor_7533_daemon.sh", 0o755),
]
if LOCAL_RBF:
    FILES.append((LOCAL_RBF,
                  f"/media/fat/_Other/{LOCAL_RBF.name}", 0o644))


def run(client, cmd, show=True):
    if show:
        print(f"  $ {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    status = stdout.channel.recv_exit_status()
    if out:
        print(f"    {out}")
    if err:
        print(f"    stderr: {err}")
    return status


def main():
    # Verify local files before connecting.
    for src, _, _ in FILES:
        if not src.exists():
            print(f"MISSING: {src}", file=sys.stderr)
            sys.exit(1)
        print(f"  OK     {src}  ({src.stat().st_size} bytes)")

    print(f"\nConnecting to {USER}@{HOST} ...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASS,
                   look_for_keys=False, allow_agent=False)

    print("\n-- Stopping running OpenBOR + daemon --")
    run(client, "killall -q OpenBOR || true")
    run(client, "killall -q openbor_7533_daemon.sh || true")
    run(client, "kill $(cat /tmp/openbor_7533_arm.pid 2>/dev/null) 2>/dev/null || true")
    run(client, "rm -f /tmp/openbor_7533_arm.pid")
    run(client, "rm -rf /tmp/openbor_7533_daemon.lock")

    print("\n-- Removing old RBFs --")
    run(client, "rm -f /media/fat/_Other/OpenBOR_7533_*.rbf")

    print("\n-- Uploading files --")
    sftp = client.open_sftp()
    for src, dst, mode in FILES:
        print(f"  -> {dst}")
        # Ensure the remote directory exists.
        parent = os.path.dirname(dst)
        try:
            sftp.stat(parent)
        except FileNotFoundError:
            run(client, f"mkdir -p {parent}", show=False)
        sftp.put(str(src), dst)
        sftp.chmod(dst, mode)
    sftp.close()

    print("\n-- Re-launching daemon --")
    run(client, "sed -i 's/\\r$//' /media/fat/games/OpenBOR_7533/openbor_7533_daemon.sh")
    run(client, "nohup /media/fat/games/OpenBOR_7533/openbor_7533_daemon.sh </dev/null >/dev/null 2>&1 & disown")

    print("\n-- Verifying deployed files --")
    run(client, "ls -lh /media/fat/games/OpenBOR_7533/ /media/fat/_Other/OpenBOR_7533_*.rbf")

    client.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
