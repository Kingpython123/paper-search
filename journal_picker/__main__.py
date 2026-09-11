"""让 `python -m journal_picker` 直接进命令行。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
