from pathlib import Path
import stat


ROOT = Path(__file__).resolve().parents[1]


def write_executable(path, body):
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
