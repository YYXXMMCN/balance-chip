#!/usr/bin/env pythonw
# -*- coding: utf-8 -*-
"""
balance-chip —— 桌面余额牌（平民酒馆工作室）

形态沿用早期 DSH 内那颗余额 chip，搬到 Windows 桌面并放大：
  · 外框颜色 = 时段（半价绿 / 高峰红 / 半价将尽黄 / 未知灰）
  · 内字颜色 = 余额是否充足（不足红 / 偏低黄 / 充足绿）
  · 单击 = 展开详细信息面板；双击 = 立即刷新并打开面板
  · 固定位置（可拖，记住，面板随牌一起动），不走动、无形象
  · 每 5 分钟自动刷新（悬浮窗在跑就刷）

颜色口径（总裁指定纯色）：
  绿 = rgb(0,255,0)   红 = rgb(255,0,0)   黄 = rgb(255,255,0)

随 DSH 启停：插件写心跳文件，本程序心跳过期即自行退出（DSH 被强杀也能收尾）。

计费规则版本：RULE_UPDATED 是"我们最后一次核对官方定价页"的日期；
距上次核对超过 RULE_STALE_DAYS 天，面板会把它标黄并提示"建议复核"。
"""
import io
import json
import os
import re
import sys
import threading
import time
import winreg
from datetime import datetime, timedelta, timezone

import requests
from PySide6.QtCore import Qt, QPoint, QRectF, QTimer, QLockFile, Signal, QObject
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget, QMenu, QSystemTrayIcon

APP_NAME = "balance-chip"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or APP_DIR, "CiviWineHouse", APP_NAME)
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
USAGE_PATH = os.path.join(STATE_DIR, "usage.json")
HEARTBEAT_PATH = os.path.join(STATE_DIR, "dsh-alive.txt")
CRED_PATH = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
WALLET_URL = "https://platform.deepseek.com/api/v0/users/get_user_summary"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

FOLLOW_DSH = "--follow-dsh" in sys.argv
HEARTBEAT_STALE_SEC = 15

# 计费规则：最后一次核对官方定价页的日期（换规则时要改这里）
RULE_UPDATED = "2026-10-09"
RULE_STALE_DAYS = 30
HALF_WARN_SEC = 3600          # 半价剩余不足 1 小时 → 外框与倒计时变黄

CHIP_H = 40
CHIP_PAD_X = 13
CHIP_RADIUS = 8
CHIP_BORDER = 2.0
CHIP_FONT = 17
PANEL_W = 330
PANEL_PAD = 14
PANEL_RADIUS = 10
PANEL_ROW_H = 25
PANEL_FONT = 14

STATE_COLOR = {
    "error": "#FF0000",
    "warn": "#FFFF00",
    "ok": "#00FF00",
    "neutral": "#AAAAAA",
}

DARK = dict(bg="#141414", border="#444444", text="#FFFFFF", sub="#E6E6E6",
            panel_bg="#1B1B1B", panel_border="#555555")
LIGHT = dict(bg="#FFFFFF", border="#999999", text="#000000", sub="#222222",
             panel_bg="#FFFFFF", panel_border="#666666")


def is_light_theme():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"):
            v, _ = winreg.QueryValueEx(winreg, "AppsUseLightTheme")
            return bool(v)
    except Exception:
        return False


def load_json(path, default):
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------- 余额取数（DSH 账号钱包，免 API key） ----------------

def dsh_wallet_token():
    try:
        with io.open(CRED_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception:
        return ""
    m = re.search(r"deepseek-account-platform/default:[\s\S]*?\btoken:\s*(\S+)", raw)
    return m.group(1).strip() if m else ""


def _sum_cost(items):
    total = 0.0
    for it in (items or []):
        try:
            total += float(it.get("amount") or 0)
        except (TypeError, ValueError, AttributeError):
            pass
    return round(total, 4)


def query_wallet():
    tok = dsh_wallet_token()
    if not tok:
        return {"ok": False, "error": "DSH 账号未登入"}
    try:
        r = requests.get(WALLET_URL, timeout=15, headers={
            "accept": "application/json",
            "authorization": "Bearer " + tok,
            "x-dsh-auth-token": tok,
            "user-agent": UA,
        })
        if r.status_code != 200:
            return {"ok": False, "error": "HTTP %d" % r.status_code}
        j = r.json() or {}
        d = ((j.get("data") or {}).get("biz_data") or {})
        if not isinstance(d, dict):
            return {"ok": False, "error": "返回结构异常"}
        normal = sum(float(x.get("balance") or 0) for x in (d.get("normal_wallets") or []))
        bonus = sum(float(x.get("balance") or 0) for x in (d.get("bonus_wallets") or []))
        wallets = d.get("normal_wallets") or [{}]
        cur = (wallets[0].get("currency") if wallets else None) or "CNY"
        return {"ok": True, "total": round(normal + bonus, 4), "currency": cur,
                "topped_up": round(normal, 4), "granted": round(bonus, 4),
                "total_cost": _sum_cost(d.get("total_costs"))}
    except Exception as ex:
        return {"ok": False, "error": str(ex)[:60]}


# ---------------- 平台用量/消费（私有网页接口，登录态可用） ----------------
USAGE_COST_URL = "https://platform.deepseek.com/api/v0/usage/cost"


def _day_cost(day):
    tot = 0.0
    for mrec in (day.get("data") or []):
        for u in (mrec.get("usage") or []):
            if u.get("type") == "REQUEST":
                continue
            try:
                tot += float(u.get("amount") or 0)
            except (TypeError, ValueError):
                pass
    return tot


def query_usage_cost(days_back=30):
    """返回 {ok, today, month, last30, days:{date:cost}, currency}；失败给 error。"""
    tok = dsh_wallet_token()
    if not tok:
        return {"ok": False, "error": "DSH 账号未登入"}
    now = beijing_now()
    days = {}
    currency = "CNY"
    try:
        for (y, m) in _months_needed(now, days_back):
            r = requests.get(USAGE_COST_URL, params={"month": m, "year": y}, timeout=20, headers={
                "accept": "application/json",
                "authorization": "Bearer " + tok,
                "x-dsh-auth-token": tok,
                "user-agent": UA,
            })
            if r.status_code != 200:
                return {"ok": False, "error": "HTTP %d" % r.status_code}
            j = r.json() or {}
            biz = (j.get("data") or {}).get("biz_data")
            if isinstance(biz, list) and biz:
                biz = biz[0]
            if not isinstance(biz, dict):
                return {"ok": False, "error": "用量返回结构异常"}
            currency = biz.get("currency") or currency
            for day in (biz.get("days") or []):
                date = day.get("date")
                if date:
                    days[date] = round(_day_cost(day), 4)
        today_key = now.strftime("%Y-%m-%d")
        month_prefix = now.strftime("%Y-%m")
        cut = (now - timedelta(days=days_back - 1)).strftime("%Y-%m-%d")
        last30 = sum(v for k, v in days.items() if k >= cut)
        month = sum(v for k, v in days.items() if k.startswith(month_prefix))
        return {"ok": True, "today": round(days.get(today_key, 0.0), 4),
                "month": round(month, 4), "last30": round(last30, 4),
                "days": days, "currency": currency}
    except Exception as ex:
        return {"ok": False, "error": str(ex)[:60]}


def _months_needed(now, days_back):
    """30 天窗口可能跨两个月，返回需要查询的 (year, month) 列表。"""
    out = []
    cur = (now.year, now.month)
    out.append(cur)
    start = now - timedelta(days=days_back - 1)
    if (start.year, start.month) != cur:
        out.append((start.year, start.month))
    return out


# ---------------- 峰谷 ----------------
PEAK_WINDOWS = [(9, 12), (14, 18)]


def beijing_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)


def pricing_state(now=None):
    now = now or beijing_now()
    weekday = now.weekday()
    if weekday >= 5:
        days = 7 - weekday
        nxt = (now + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
        return True, nxt
    minute = now.hour * 60 + now.minute
    for start, end in PEAK_WINDOWS:
        if start * 60 <= minute < end * 60:
            nxt = now.replace(hour=end, minute=0, second=0, microsecond=0)
            return False, nxt
    candidates = []
    for start, _ in PEAK_WINDOWS:
        t = now.replace(hour=start, minute=0, second=0, microsecond=0)
        if t > now:
            candidates.append(t)
    if candidates:
        return True, min(candidates)
    nxt = (now + timedelta(days=1)).replace(hour=PEAK_WINDOWS[0][0], minute=0, second=0, microsecond=0)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return True, nxt


def fmt_hms(seconds):
    """小时累计、精确到秒：33:12:45（不写"天"）"""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return "%02d:%02d:%02d" % (h, m, sec)


def fmt_money(v, sym="¥"):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    return "%s%s" % (sym, "{:,.2f}".format(v))


def days_since(date_str):
    try:
        d0 = datetime.strptime(date_str, "%Y-%m-%d")
        return (beijing_now() - d0).days
    except Exception:
        return 0


# ---------------- 记账：今日 + 按天留存（供近期合计） ----------------
def update_usage(total):
    today = beijing_now().strftime("%Y-%m-%d")
    st = load_json(USAGE_PATH, {})
    days = st.get("days") if isinstance(st.get("days"), dict) else {}
    if st.get("date") == today and isinstance(st.get("last"), (int, float)):
        delta = float(st["last"]) - float(total)
        add = delta if 0 < delta <= 20 else 0.0
        used = float(st.get("used") or 0.0) + add
    else:
        used = 0.0
    days[today] = round(used, 4)
    keep = sorted(days.keys())[-60:]
    days = {k: days[k] for k in keep}
    since = st.get("since") or today
    save_json(USAGE_PATH, {"date": today, "last": total, "used": round(used, 4),
                           "days": days, "since": since})
    return round(used, 4), days, since


def recent_sum(days, n=30):
    keys = sorted(days.keys())[-n:]
    return round(sum(float(days[k] or 0) for k in keys), 4)


class Fetcher(QObject):
    done = Signal(dict)

    def __init__(self):
        super().__init__()
        self.busy = False

    def start(self):
        if self.busy:
            return
        self.busy = True

        def work():
            res = query_wallet()
            res["at"] = time.time()
            if res.get("ok"):
                try:
                    res["usage"] = query_usage_cost()
                except Exception as ex:
                    res["usage"] = {"ok": False, "error": str(ex)[:60]}
            self.busy = False
            self.done.emit(res)

        threading.Thread(target=work, daemon=True).start()


class ChipWindow(QWidget):
    clicked = Signal()
    double_clicked = Signal()

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedHeight(CHIP_H)
        self.setCursor(Qt.PointingHandCursor)
        self._press = None
        self._drag_offset = None
        self._dragged = False
        self._suppress_click = False
        self._last_dbl = 0.0
        self._last_release = 0.0
        self._label = "余额 --"
        self._tone = STATE_COLOR["neutral"]
        self._ring = STATE_COLOR["neutral"]
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self.clicked.emit)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.recalc_size()

    def recalc_size(self):
        f = QFont("Microsoft YaHei UI", CHIP_FONT)
        f.setBold(True)
        w = QFontMetrics(f).horizontalAdvance(self._label) + CHIP_PAD_X * 2
        self.setFixedWidth(max(126, w))
        self.update()

    def set_state(self, label, tone_key, ring_key):
        self._label = label
        self._tone = STATE_COLOR.get(tone_key, STATE_COLOR["neutral"])
        self._ring = STATE_COLOR.get(ring_key, STATE_COLOR["neutral"])
        self.recalc_size()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = self.app.colors
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        path.addRoundedRect(r, CHIP_RADIUS, CHIP_RADIUS)
        p.fillPath(path, QColor(c["bg"]))
        pen = QPen(QColor(self._ring))
        pen.setWidthF(CHIP_BORDER)
        p.setPen(pen)
        p.drawPath(path)
        f = QFont("Microsoft YaHei UI", CHIP_FONT)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(self._tone))
        p.drawText(r, Qt.AlignCenter, self._label)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = ev.globalPosition().toPoint()
            self._drag_offset = self._press - self.pos()
            self._dragged = False
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._press is None:
            return
        cur = ev.globalPosition().toPoint()
        if (cur - self._press).manhattanLength() > 4:
            self._dragged = True
        if self._dragged:
            self.move(cur - self._drag_offset)
            self.app.reposition_panel()      # 面板跟着牌一起动
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        was_drag = self._dragged
        self._press = None
        self._dragged = False
        if was_drag:
            self.app.save_position(self.pos())
            self.app.reposition_panel()
        elif self._suppress_click:
            self._suppress_click = False      # 双击之后那次释放不再触发单击
        elif time.time() - self._last_dbl < 0.5:
            pass                              # 双击时间窗内，忽略残留的单击
        else:
            now = time.time()
            interval = max(0.2, QApplication.doubleClickInterval() / 1000.0)
            if now - getattr(self, "_last_release", 0.0) <= interval:
                # 自己判双击：取消待定的单击、直接刷新+开面板
                # 注意：这里就是释放事件本身，不能留 _suppress_click（会吞掉下一次单击）
                self._last_release = 0.0
                self._click_timer.stop()
                self._last_dbl = now
                self.double_clicked.emit()
            else:
                self._last_release = now
                self._click_timer.start(int(interval * 1000))
        ev.accept()

    def mouseDoubleClickEvent(self, ev):
        # 双击由 mouseReleaseEvent 自己判定（更稳，慢双击也能识别）；
        # Qt 这个事件只做吞掉处理，避免重复触发刷新。
        if ev.button() == Qt.LeftButton:
            ev.accept()

    def _menu(self, pos):
        m = QMenu(self)
        m.addAction("立即刷新", lambda: self.app.refresh(manual=True))
        m.addAction("详细信息", self.app.toggle_panel)
        m.addSeparator()
        m.addAction("退出", self.app.quit)
        m.exec(self.mapToGlobal(pos))


class PanelWindow(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedWidth(PANEL_W + PANEL_PAD * 2)
        self.rows = []
        self._refresh_rect = None
        self.setMouseTracking(True)

    def set_lines(self, lines):
        self.rows = lines
        self.setFixedHeight(PANEL_PAD + len(lines) * PANEL_ROW_H + 8)
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = self.app.colors
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        path.addRoundedRect(r, PANEL_RADIUS, PANEL_RADIUS)
        p.fillPath(path, QColor(c["panel_bg"]))
        pen = QPen(QColor(c["panel_border"]))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.drawPath(path)

        y = PANEL_PAD
        self._refresh_rect = None
        for i, (text, kind, color) in enumerate(self.rows):
            f = QFont("Microsoft YaHei UI", PANEL_FONT)
            if i == 0:
                f.setPointSize(PANEL_FONT + 7)
                f.setBold(True)
            elif kind == "button":
                f.setBold(True)
            p.setFont(f)
            col = QColor(color) if color else QColor(c["sub"] if i > 0 else c["text"])
            rect = QRectF(PANEL_PAD, y, self.width() - PANEL_PAD * 2, PANEL_ROW_H)
            p.setPen(col)
            p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, text)
            if kind == "button":
                self._refresh_rect = rect
            y += PANEL_ROW_H

    def mousePressEvent(self, ev):
        if self._refresh_rect and self._refresh_rect.contains(ev.position()):
            self.app.refresh(manual=True)


class App(QObject):
    def __init__(self, qapp):
        super().__init__()
        self.qapp = qapp
        os.makedirs(STATE_DIR, exist_ok=True)
        self.cfg = load_json(CONFIG_PATH, {})
        self.cfg.setdefault("low", 20.0)
        self.cfg.setdefault("critical", 5.0)
        self.cfg.setdefault("refreshSec", 300)
        self.cfg.setdefault("theme", "auto")
        self.cfg.setdefault("pos", None)
        self.colors = LIGHT if (self.cfg.get("theme") == "light" or
                                (self.cfg.get("theme") == "auto" and is_light_theme())) else DARK
        self.data = None
        self.error = None
        self.last_ok = None
        self.today_used = None
        self.days = {}
        self.since = None
        self.u = None
        self.u_error = None
        self._pending_open = False
        self._loading = False
        self.fetcher = Fetcher()
        self.fetcher.done.connect(self.on_fetched)

        self.chip = ChipWindow(self)
        self.panel = PanelWindow(self)
        self.chip.clicked.connect(self.toggle_panel)
        self.chip.double_clicked.connect(self.on_double_click)

        self._panel_timer = QTimer(self)
        self._panel_timer.setSingleShot(True)
        self._panel_timer.timeout.connect(self.panel.hide)

        self._restore_position()
        self.chip.show()
        QTimer.singleShot(250, lambda: self.refresh(manual=False))

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.refresh(manual=False))
        self.timer.start(max(30, int(self.cfg.get("refreshSec") or 300)) * 1000)

        self.tick = QTimer(self)
        self.tick.timeout.connect(self.on_tick)
        self.tick.start(1000)

        if FOLLOW_DSH:
            self.hb = QTimer(self)
            self.hb.timeout.connect(self._check_heartbeat)
            self.hb.start(3000)

        self._setup_tray()

    def _check_heartbeat(self):
        try:
            age = time.time() - os.path.getmtime(HEARTBEAT_PATH)
        except Exception:
            age = HEARTBEAT_STALE_SEC + 1
        if age > HEARTBEAT_STALE_SEC:
            self.quit()

    def _restore_position(self):
        pos = self.cfg.get("pos")
        screen = self.qapp.primaryScreen().availableGeometry()
        if isinstance(pos, dict) and isinstance(pos.get("x"), (int, float)):
            x, y = int(pos["x"]), int(pos["y"])
            x = min(max(screen.left(), x), screen.right() - self.chip.width())
            y = min(max(screen.top(), y), screen.bottom() - self.chip.height())
            self.chip.move(x, y)
        else:
            self.chip.move(screen.right() - self.chip.width() - 24,
                           screen.bottom() - self.chip.height() - 24)

    def save_position(self, pos):
        self.cfg["pos"] = {"x": int(pos.x()), "y": int(pos.y())}
        save_json(CONFIG_PATH, self.cfg)

    def reposition_panel(self):
        if not self.panel.isVisible():
            return
        origin = self.chip.mapToGlobal(QPoint(0, 0))
        chip_x, chip_y = origin.x(), origin.y()
        cw, ch = self.chip.width(), self.chip.height()
        screen = self.chip.screen().availableGeometry() if self.chip.screen() else self.qapp.primaryScreen().availableGeometry()
        pw, ph = self.panel.width(), self.panel.height()
        x = chip_x + cw - pw
        x = max(screen.left() + 4, min(x, screen.right() - pw - 4))
        # 默认放在牌上方；上方放不下或会压到牌，就放到下方
        y = chip_y - ph - 8
        overlaps = (x < chip_x + cw) and (x + pw > chip_x) and (y < chip_y + ch) and (y + ph > chip_y)
        if y < screen.top() + 4 or overlaps:
            y = chip_y + ch + 8
        # 下方也越界就夹回屏幕内（宁可稍微压一点，也不要跑到屏幕外）
        if y + ph > screen.bottom() - 4:
            y = max(screen.top() + 4, screen.bottom() - ph - 4)
        self.panel.move(x, y)

    def refresh(self, manual=False):
        self._loading = True
        self.chip.set_state("¥…", "neutral", self._ring_key())
        self.fetcher.start()

    def on_fetched(self, res):
        if res.get("ok"):
            self.data = res
            self.error = None
            self.last_ok = res.get("at") or time.time()
            self.today_used, self.days, self.since = update_usage(res["total"])
            u = res.get("usage") or {}
            if u.get("ok"):
                self.u = u
                self.u_error = None
            else:
                self.u = None
                self.u_error = (u.get("error") if isinstance(u, dict) else None)
        else:
            self.error = res.get("error") or "未知错误"
        self._loading = False
        self.update_chip()
        if self._pending_open:
            # 与"点点点变成数字"同一时刻弹出：先让牌重绘完，再立刻显示面板
            self._pending_open = False
            self.chip.repaint()
            self.panel.show()
            self.update_panel()
            self.reposition_panel()
            self.panel.raise_()
            self._panel_timer.start(60000)
        elif self.panel.isVisible():
            self.update_panel()

    # --- 时段 / 颜色 ---
    def _pricing(self):
        half, nxt = pricing_state()
        left = (nxt - beijing_now()).total_seconds()
        return half, nxt, left

    def _ring_key(self):
        half, _, left = self._pricing()
        if not half:
            return "error"
        return "warn" if left < HALF_WARN_SEC else "ok"

    def _tone_key(self):
        if not self.data or not isinstance(self.data.get("total"), (int, float)):
            return "neutral"
        t = float(self.data["total"])
        if t < float(self.cfg.get("critical") or 5):
            return "error"
        if t < float(self.cfg.get("low") or 20):
            return "warn"
        return "ok"

    def update_chip(self):
        ring = self._ring_key()
        if self.data and isinstance(self.data.get("total"), (int, float)):
            self.chip.set_state(fmt_money(self.data["total"]), self._tone_key(), ring)
        elif self.error:
            self.chip.set_state("余额 ?", "neutral", "neutral")
        else:
            self.chip.set_state("余额 --", "neutral", ring)

    # --- 面板 ---
    def on_double_click(self):
        # 双击：刷新；面板要等【余额数据返回的那一刻】才和更新一起出现
        if self.panel.isVisible():
            self._pending_open = False
            self.refresh(manual=True)
        else:
            self._pending_open = True
            self.refresh(manual=True)

    def _open_panel(self):
        self.update_panel()
        self.panel.show()
        self.reposition_panel()
        self.panel.raise_()
        self._panel_timer.start(60000)

    def toggle_panel(self):
        self._pending_open = False
        if self.panel.isVisible():
            self._panel_timer.stop()
            self.panel.hide()
            return
        self.panel.show()
        self.update_panel()
        self.reposition_panel()
        self.panel.raise_()
        self._panel_timer.start(60000)

    def update_panel(self):
        rows = []
        half, _, left = self._pricing()
        soon = half and left < HALF_WARN_SEC
        time_color = STATE_COLOR["warn"] if soon else (STATE_COLOR["ok"] if half else STATE_COLOR["error"])
        time_text = "%s %s" % ("半价剩余" if half else "高峰剩余", fmt_hms(left))
        if soon:
            time_text += "（即将转高峰）"

        if self.data and isinstance(self.data.get("total"), (int, float)):
            rows.append((fmt_money(self.data["total"]), "big", STATE_COLOR[self._tone_key()]))
            rows.append(("充值 %s ｜ 赠送 %s" % (fmt_money(self.data.get("topped_up")),
                                             fmt_money(self.data.get("granted"))), "sub", None))
            if self.u:
                rows.append(("今日已用 %s" % fmt_money(self.u.get("today")), "sub", None))
                rows.append(("本月消费 %s" % fmt_money(self.u.get("month")), "sub", None))
                rows.append(("近 30 天消费 %s" % fmt_money(self.u.get("last30")), "sub", None))
            else:
                if self.today_used is not None:
                    rows.append(("今日已用 %s" % fmt_money(self.today_used), "sub", None))
                if self.days:
                    n = min(30, len(self.days))
                    rows.append(("近 %d 天消费 %s" % (n, fmt_money(recent_sum(self.days, 30))), "sub", None))
            tc = self.data.get("total_cost")
            if isinstance(tc, (int, float)):
                rows.append(("累计消费 %s" % fmt_money(tc), "sub", None))
            rows.append((time_text, "sub", time_color))
        else:
            rows.append(("取不到余额", "big", STATE_COLOR["neutral"]))
            rows.append((self.error or "查询中…", "sub", None))
            rows.append((time_text, "sub", time_color))

        if self.last_ok:
            rows.append(("上次刷新时间 %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(self.last_ok)), "sub", None))
        age = days_since(RULE_UPDATED)
        rule_color = STATE_COLOR["warn"] if age > RULE_STALE_DAYS else None
        rule_text = "计费规则 %s" % RULE_UPDATED
        if age > RULE_STALE_DAYS:
            rule_text += " · 建议复核"
        rows.append((rule_text, "sub", rule_color))
        rows.append(("双击余额框即可立即刷新", "button", STATE_COLOR["ok"]))
        self.panel.set_lines(rows)

    def on_tick(self):
        # 取数期间牌子保持"¥…"，不要用旧数据把它刷回去（否则看起来数字先变、面板后到）
        if not self._loading:
            self.update_chip()                   # 半价将尽时外框变黄
        if self.panel.isVisible():
            self.update_panel()

    def _setup_tray(self):
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setBrush(QColor(STATE_COLOR["ok"]))
        p.setPen(Qt.NoPen)
        p.drawEllipse(4, 4, 24, 24)
        p.end()
        self.tray = QSystemTrayIcon(QIcon(pm), self)
        menu = QMenu()
        menu.addAction("立即刷新", lambda: self.refresh(manual=True))
        menu.addAction("详细信息", self.toggle_panel)
        menu.addSeparator()
        menu.addAction("退出", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("桌面余额牌")
        self.tray.show()

    def quit(self):
        save_json(CONFIG_PATH, self.cfg)
        try:
            self.tray.hide()
        except Exception:
            pass
        self.qapp.quit()


def main():
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = QLockFile(os.path.join(STATE_DIR, "instance.lock"))
    lock.setStaleLockTime(15000)
    if not lock.tryLock(100):
        return 0
    app = App(qapp)
    code = qapp.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
