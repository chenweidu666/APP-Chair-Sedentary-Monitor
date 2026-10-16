#!/usr/bin/env bash
# 安装椅子久坐检测：启动器 + systemd 用户服务（开机自动连接传感器）。
#
#   ./install.sh             安装并启用（headless 常驻，开机自动连接）
#   ./install.sh --tray      改为托盘界面模式（登录后自启 GUI，禁用 headless 服务）
#   ./install.sh --uninstall 卸载
set -euo pipefail

APP="chair-monitor"
REPO="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
AUTOSTART_DIR="$HOME/.config/autostart"
ICON_NAME="chair-monitor"
WT9011_URL="https://github.com/chenweidu666/APP-WT9011DCL-BT50-Linux.git"
WT9011_LOCAL="$HOME/Linux-App/01-WT9011DCL-BT50/APP-WT9011DCL-BT50-Linux"

die() { echo "错误：$*" >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl --user disable --now "$APP.service" 2>/dev/null || true
    rm -f "$UNIT_DIR/$APP.service" "$AUTOSTART_DIR/$APP-tray.desktop" "$BIN_DIR/$APP"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "已卸载（配置与历史记录保留在 ~/.config/chair-monitor/）"
    exit 0
fi

MODE="headless"
[[ "${1:-}" == "--tray" ]] && MODE="tray"

# ---------- 找解释器 ----------
CANDIDATES=("${PY:-}" "$REPO/.venv/bin/python" "$(command -v python3 || true)" \
            "$HOME/miniconda3/bin/python3" "/usr/bin/python3")
PY=""
for cand in "${CANDIDATES[@]}"; do
    [[ -n "$cand" && -x "$cand" ]] || continue
    "$cand" -c "import bleak" >/dev/null 2>&1 && { PY="$cand"; break; }
done
[[ -n "$PY" ]] || die "找不到装了 bleak 的 Python。先执行：pip install -r requirements.txt"

# ---------- 确保 wt9011 协议库可用 ----------
if ! "$PY" -c "import wt9011.protocol" >/dev/null 2>&1; then
    if [[ -d "$WT9011_LOCAL" ]]; then
        echo "安装协议库（本地）：$WT9011_LOCAL"
        "$PY" -m pip install -q -e "$WT9011_LOCAL"
    else
        echo "安装协议库（GitHub）：$WT9011_URL"
        "$PY" -m pip install -q "$WT9011_URL"
    fi
fi
"$PY" -c "import wt9011.protocol" >/dev/null 2>&1 || die "wt9011 协议库安装失败"

# ---------- 启动器 ----------
mkdir -p "$BIN_DIR" "$UNIT_DIR" "$AUTOSTART_DIR"
cat > "$BIN_DIR/$APP" <<EOF
#!/usr/bin/env bash
# 由 chair-sedentary-monitor/install.sh 生成
export PYTHONPATH="$REPO\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PY" -m chairmon.cli "\$@"
EOF
chmod +x "$BIN_DIR/$APP"
echo "启动器：$BIN_DIR/$APP"

# ---------- systemd 用户服务（开机自动连接）----------
if [[ "$MODE" == "headless" ]]; then
    cat > "$UNIT_DIR/$APP.service" <<EOF
[Unit]
Description=椅子久坐检测（BLE 姿态传感器，自动连接）
After=bluetooth.target graphical-session.target
Wants=bluetooth.target

[Service]
Type=simple
ExecStart=$BIN_DIR/$APP run
Restart=always
RestartSec=10
# 允许访问用户会话总线（notify-send 需要）
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$APP.service"
    rm -f "$AUTOSTART_DIR/$APP-tray.desktop"
    echo "systemd 用户服务：已启用并启动（$UNIT_DIR/$APP.service）"
    echo "  查看状态：systemctl --user status $APP"
    echo "  看日志  ：journalctl --user -u $APP -f"
    echo "  想让开机后无需登录也运行：sudo loginctl enable-linger $USER"
else
    systemctl --user disable --now "$APP.service" 2>/dev/null || true
    cat > "$AUTOSTART_DIR/$APP-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=椅子久坐检测（托盘）
Comment=BLE 姿态传感器的久坐提醒
Exec=$BIN_DIR/$APP tray
Icon=$ICON_NAME
Terminal=false
X-GNOME-Autostart-enabled=true
Categories=Utility;
EOF
    echo "托盘模式：登录后自启（$AUTOSTART_DIR/$APP-tray.desktop）"
    echo "  手动启动：$APP tray"
fi

# ---------- 图标（可选）----------
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
if [[ -f "$REPO/packaging/$ICON_NAME.svg" ]]; then
    mkdir -p "$ICON_DIR"
    cp "$REPO/packaging/$ICON_NAME.svg" "$ICON_DIR/$ICON_NAME.svg"
fi

cat <<'TIP'

提示：
1) 传感器要贴在【椅背背面中上部】或【座板下方中央】，用硬质固定（别贴软垫上）。
2) 第一次安装后，椅子空着时跑一次基线校准：
       chair-monitor mark-empty
3) 调参：空椅/坐姿各采一段带标签数据，让它推荐阈值：
       chair-monitor record --label empty   --seconds 60 --csv empty.csv
       chair-monitor record --label leaning --seconds 60 --csv leaning.csv
       chair-monitor tune empty.csv
TIP
