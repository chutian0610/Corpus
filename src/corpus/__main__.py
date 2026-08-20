"""允许 `python -m corpus` 启动 CLI。"""

import sys

from corpus.cli import main

if __name__ == "__main__":
    sys.exit(main())
