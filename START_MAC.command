#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 server.py --open
status=$?
if [ "$status" -ne 0 ]; then
  echo '启动失败，请确认安装了 Python 3.10 或更新版本。'
  read -r answer
fi
exit "$status"
