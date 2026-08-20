"""允许 `python -m corpus_bot` 启动 CLI。"""

import sys

from corpus_bot.cli import main

if __name__ == "__main__":
    sys.exit(main())
