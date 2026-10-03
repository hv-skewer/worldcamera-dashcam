#!/usr/bin/env bash
# 一键装依赖（Linux/macOS 参考；本工具核心在 Windows，此脚本仅装 Python 依赖）
python3 -m pip install -r requirements.txt
echo ""
echo "依赖装好了。自检（需 Windows + 记录仪插上）："
echo "  python3 tools/live_view.py --probe"
