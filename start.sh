#!/usr/bin/env bash
# 一键启动：首次运行时建 venv 并安装依赖，然后启动本地服务并在浏览器打开页面。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "创建虚拟环境 .venv"
  python3 -m venv .venv
fi

# 只在 requirements.txt 变化后重新安装
stamp=.venv/.requirements.installed
if [ ! -f "$stamp" ] || ! cmp -s requirements.txt "$stamp"; then
  echo "安装依赖"
  .venv/bin/pip install -r requirements.txt
  cp requirements.txt "$stamp"
fi

# macOS 用 open，Linux 用 xdg-open；都没有时只打印地址
open_url() {
  # Debian 上的 open 可能是 openvt，所以按系统判断而不是看命令是否存在
  if [ "$(uname)" = Darwin ]; then open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1
  else echo "请在浏览器中打开 $1"
  fi
}

url=$(.venv/bin/python -c 'from glow.config import load_config; s = load_config()["server"]; print(f"http://{s["host"]}:{s["port"]}")')

if curl -fs "$url/api/status" >/dev/null 2>&1; then
  echo "服务已在运行：$url"
  open_url "$url"
  exit 0
fi

# 等服务能响应后再打开浏览器
(
  for _ in $(seq 1 60); do
    if curl -fs "$url/api/status" >/dev/null 2>&1; then
      open_url "$url"
      exit 0
    fi
    sleep 0.5
  done
) &

echo "启动服务：$url（Ctrl+C 停止）"
exec .venv/bin/python -m glow serve
