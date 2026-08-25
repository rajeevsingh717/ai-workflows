"""Generate portable launchd plists using the current checkout path."""

import argparse
import plistlib
from pathlib import Path


LABELS = {
    "market-check": "com.aiworkflows.marketcheck",
    "telegram-listener": "com.aiworkflows.telegramlistener",
}


def build_plist(job: str, project_root: Path) -> dict:
    python = project_root / ".venv" / "bin" / "python3"
    output_dir = project_root / "fidelity_data" / "output"
    if job == "market-check":
        schedule = [
            {"Weekday": weekday, "Hour": hour, "Minute": minute}
            for weekday in range(2, 7)  # Monday=2 through Friday=6 in launchd
            for hour, minute in ((9, 35), (12, 30), (15, 45))
        ]
        return {
            "Label": LABELS[job],
            "ProgramArguments": [str(python), str(project_root / "market_check.py")],
            "WorkingDirectory": str(project_root),
            "StartCalendarInterval": schedule,
            "StandardOutPath": str(output_dir / "market_check.log"),
            "StandardErrorPath": str(output_dir / "market_check.error.log"),
            "RunAtLoad": False,
        }
    return {
        "Label": LABELS[job],
        "ProgramArguments": [str(python), "-u", str(project_root / "telegram_listener.py")],
        "WorkingDirectory": str(project_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(output_dir / "telegram_listener.log"),
        "StandardErrorPath": str(output_dir / "telegram_listener.error.log"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=LABELS)
    parser.add_argument("output", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()

    root = args.project_root.expanduser().resolve()
    output = args.output.expanduser().resolve()
    python = root / ".venv" / "bin" / "python3"
    if not python.exists():
        raise SystemExit(f"Virtual environment not found: {python}")
    (root / "fidelity_data" / "output").mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as handle:
        plistlib.dump(build_plist(args.job, root), handle, sort_keys=False)
    print(output)


if __name__ == "__main__":
    main()
