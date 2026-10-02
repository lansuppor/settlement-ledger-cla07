import sys
from pathlib import Path

# 让 README 中的 `pytest -q` 无需手动设置 PYTHONPATH 即可导入 app 包。
sys.path.insert(0, str(Path(__file__).resolve().parent))
