"""pytest 配置。"""

import sys
from pathlib import Path

# 让 src/corpus/ 可被 import
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
