"""예약 마우스 클릭 매크로 (밀리초 단위, 서버 시간 + 실시간 지연 보정)

기준 클릭 시각(서버 기준, 예: 16:29:59.800)에 지정한 좌표를 클릭합니다.
클릭 직전 몇 초 동안 서버 응답 속도를 계속 재서, 평소보다 느려졌으면 그만큼 일찍,
빨라졌으면 그만큼 늦게 클릭합니다.
- F8 : 현재 마우스 위치를 클릭 좌표로 저장
- ESC: 예약 취소
"""

import csv
import datetime as dt
import email.utils
import http.client
import math
import os
import statistics
import sys
import threading
import time
import tkinter as tk
import urllib.parse
from tkinter import messagebox

from pynput import keyboard, mouse

# Windows: DPI 배율 때문에 좌표가 어긋나는 문제 방지 + 타이머 해상도 1ms로 설정
if sys.platform == "win32":
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()
    ctypes.windll.winmm.timeBeginPeriod(1)

BUSY_WAIT_SEC = 0.02  # 목표 시각 20ms 전부터는 sleep 대신 busy-wait
LIVE_START_SEC = 6.0  # 클릭 몇 초 전부터 실시간 지연을 잴지
LIVE_WINDOW = 5  # 최근 몇 번의 측정값(중앙값)으로 판단할지
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "click_log.csv")

BUTTONS = {"왼쪽": mouse.Button.left, "오른쪽": mouse.Button.right, "가운데": mouse.Button.middle}


def parse_target(text: str) -> dt.datetime:
    """'HH:MM:SS.mmm' (또는 'HH:MM:SS', 'HH:MM')을 오늘 날짜의 datetime으로 변환."""
    text = text.strip()
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            t = dt.datetime.strptime(text, fmt).time()
            return dt.datetime.combine(dt.date.today(), t)
        except ValueError:
            continue
    raise ValueError("시각 형식은 HH:MM:SS.mmm 이어야 합니다. 예) 16:29:59.800")


def fmt_ms(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S.%f")[:-3]


class Probe:
    """한 서버에 연결을 유지한 채 HEAD 요청을 반복 보내는 도우미."""

    HEADERS = {"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}

    def __init__(self, url: str):
        if "://" not in url:
            url = "https://" + url
        parts = urllib.parse.urlsplit(url)
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        self.conn = conn_cls(parts.netloc, timeout=5)
        self.path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        self.ping()  # 연결(TLS 핸드셰이크)을 미리 맺어 두어 이후 측정에서 제외

    def ping(self):
        """요청 1회. 반환: (보낸 PC 시각, 받은 PC 시각, 서버 Date 헤더 epoch 초)."""
        t0 = time.time()
        self.conn.request("HEAD", self.path, headers=self.HEADERS)
        resp = self.conn.getresponse()
        resp.read()
        t1 = time.time()
        date = resp.getheader("Date")
        if not date:
            raise RuntimeError("서버 응답에 Date 헤더가 없습니다.")
        return t0, t1, email.utils.parsedate_to_datetime(date).timestamp()

    def close(self):
        self.conn.close()


def measure_server(url: str, samples: int = 20, progress=None):
    """서버 시계와 PC 시계의 차이, 왕복 지연(RTT)을 측정.

    Date 헤더는 초 단위라서, 요청을 여러 번 보내 초가 바뀌는 순간을 좁혀 들어가는 방식으로
    밀리초 단위 차이를 추정한다. 서버시각 = PC시각 + offset.
    반환: (offset 초, offset 오차 ±초, RTT 목록 초)
    """
    # 서버 시각이 [S, S+1) 이고 응답이 PC 시각 [t0, t1] 사이에 만들어졌다면
    # offset 은 [S - t1, S + 1 - t0) 범위. 여러 번 측정해 범위의 교집합을 구한다.
    lo, hi = -float("inf"), float("inf")
    rtts = []
    probe = Probe(url)
    try:
        for i in range(samples):
            now = time.time()
            if i < 5 or hi - lo > 0.9:
                # 처음에는 초 경계의 여러 위치를 고르게 훑는다
                time.sleep(0.05 + 0.21 * (i % 5))
            else:
                # 범위가 좁아지면 예상되는 서버 초 경계 근처를 겨냥해 범위를 더 좁힌다
                mid = (lo + hi) / 2
                edge = math.floor(now + mid) + 1 - mid  # 다음 서버 초 경계의 PC 시각
                aim = edge + (hi - lo) * (0.25 if i % 2 else -0.25) - rtts[-1] / 2
                while aim < now + 0.01:
                    aim += 1
                time.sleep(aim - now)
            t0, t1, server_sec = probe.ping()
            rtts.append(t1 - t0)
            new_lo, new_hi = max(lo, server_sec - t1), min(hi, server_sec + 1 - t0)
            if new_lo < new_hi:  # 튀는 측정값(지연 등)은 무시
                lo, hi = new_lo, new_hi
            if progress:
                progress(i + 1, samples, (lo + hi) / 2, (hi - lo) / 2)
    finally:
        probe.close()
    return (lo + hi) / 2, (hi - lo) / 2, rtts


def recent_rtt(rtts, pending: float = 0.0) -> float:
    """최근 RTT 중앙값. 아직 응답이 안 온 요청이 그보다 오래 걸리고 있으면 그 시간을 하한으로 본다."""
    return max(statistics.median(rtts[-LIVE_WINDOW:]) if rtts else 0.0, pending)


def live_adjustment(rtt: float, baseline_rtt: float, max_adj: float) -> float:
    """최근 RTT가 기준보다 늘어난 만큼(편도 = 절반) 일찍 클릭할 시간(초). ±max_adj로 제한."""
    lead = (rtt - baseline_rtt) / 2
    return max(-max_adj, min(max_adj, lead))


def wait_until(target_epoch: float, cancel: threading.Event) -> bool:
    """target_epoch(초, time.time() 기준)까지 대기. 취소되면 False."""
    # 벽시계 기준 남은 시간을 단조 시계(perf_counter) 기준으로 변환해서 정밀하게 대기
    deadline = time.perf_counter() + (target_epoch - time.time())
    while True:
        remaining = deadline - time.perf_counter()
        if cancel.is_set():
            return False
        if remaining <= 0:
            return True
        if remaining > BUSY_WAIT_SEC:
            time.sleep(min(remaining - BUSY_WAIT_SEC, 0.05))


def write_log(row: dict):
    """매 실행 결과를 CSV에 한 줄씩 추가 (실제 당첨 결과와 비교해 기준 시각을 조정하는 용도)."""
    try:
        new = not os.path.exists(LOG_PATH)
        with open(LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)
    except OSError:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.mouse = mouse.Controller()
        self.cancel = threading.Event()
        self.worker = None
        self.server_offset = 0.0  # 서버시각 = PC시각 + server_offset (초)
        self.baseline_rtt = None  # 서버 측정 때 잰 평소 왕복 지연 (초)

        root.title("예약 클릭 매크로")
        root.attributes("-topmost", True)
        root.resizable(False, False)

        pad = {"padx": 6, "pady": 3}
        row = 0

        tk.Label(root, text="서버 주소").grid(row=row, column=0, sticky="w", **pad)
        self.url_var = tk.StringVar(value="https://berriz.in")
        tk.Entry(root, textvariable=self.url_var, width=24).grid(row=row, column=1, sticky="w", **pad)
        self.measure_btn = tk.Button(root, text="서버 측정", command=self.measure)
        self.measure_btn.grid(row=row, column=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="서버 시차").grid(row=row, column=0, sticky="w", **pad)
        self.offset_info_var = tk.StringVar(value="측정 안 함 (PC 시각 사용)")
        tk.Label(root, textvariable=self.offset_info_var).grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="평소 지연").grid(row=row, column=0, sticky="w", **pad)
        self.base_info_var = tk.StringVar(value="측정 안 함")
        tk.Label(root, textvariable=self.base_info_var).grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="서버 시각").grid(row=row, column=0, sticky="w", **pad)
        self.now_var = tk.StringVar()
        tk.Label(root, textvariable=self.now_var, font=("Consolas", 14, "bold"), fg="red").grid(
            row=row, column=1, columnspan=2, sticky="w", **pad
        )
        row += 1

        tk.Label(root, text="기준 클릭 시각").grid(row=row, column=0, sticky="w", **pad)
        default = (dt.datetime.now() + dt.timedelta(minutes=2)).replace(second=0, microsecond=0)
        default -= dt.timedelta(milliseconds=200)
        self.time_var = tk.StringVar(value=default.strftime("%H:%M:%S.%f")[:-3])
        tk.Entry(root, textvariable=self.time_var, width=16, font=("Consolas", 12)).grid(
            row=row, column=1, sticky="w", **pad
        )
        tk.Label(root, text="서버 기준, 예) 16:29:59.800").grid(row=row, column=2, sticky="w", **pad)
        row += 1

        self.live_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            root, text=f"실시간 지연 보정 (클릭 {LIVE_START_SEC:g}초 전부터 측정)", variable=self.live_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="최대 보정(ms)").grid(row=row, column=0, sticky="w", **pad)
        self.max_adj_var = tk.StringVar(value="200")
        tk.Entry(root, textvariable=self.max_adj_var, width=8).grid(row=row, column=1, sticky="w", **pad)
        tk.Label(root, text="기준에서 앞뒤로 최대 이만큼만 이동").grid(row=row, column=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="실시간 지연").grid(row=row, column=0, sticky="w", **pad)
        self.live_info_var = tk.StringVar(value="-")
        tk.Label(root, textvariable=self.live_info_var, fg="darkgreen").grid(
            row=row, column=1, columnspan=2, sticky="w", **pad
        )
        row += 1

        tk.Label(root, text="좌표 X, Y").grid(row=row, column=0, sticky="w", **pad)
        pos = tk.Frame(root)
        pos.grid(row=row, column=1, sticky="w", **pad)
        self.x_var = tk.StringVar(value="0")
        self.y_var = tk.StringVar(value="0")
        tk.Entry(pos, textvariable=self.x_var, width=6).pack(side="left")
        tk.Entry(pos, textvariable=self.y_var, width=6).pack(side="left", padx=(4, 0))
        tk.Label(root, text="F8 = 현재 위치 저장").grid(row=row, column=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="버튼").grid(row=row, column=0, sticky="w", **pad)
        self.button_var = tk.StringVar(value="왼쪽")
        tk.OptionMenu(root, self.button_var, *BUTTONS).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        tk.Label(root, text="클릭 횟수").grid(row=row, column=0, sticky="w", **pad)
        self.count_var = tk.StringVar(value="1")
        tk.Entry(root, textvariable=self.count_var, width=6).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        tk.Label(root, text="클릭 간격(ms)").grid(row=row, column=0, sticky="w", **pad)
        self.interval_var = tk.StringVar(value="50")
        tk.Entry(root, textvariable=self.interval_var, width=6).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        btns = tk.Frame(root)
        btns.grid(row=row, column=0, columnspan=3, **pad)
        self.start_btn = tk.Button(btns, text="예약 시작", width=12, command=self.start)
        self.start_btn.pack(side="left", padx=4)
        tk.Button(btns, text="취소 (ESC)", width=12, command=self.stop).pack(side="left", padx=4)
        row += 1

        self.status_var = tk.StringVar(value="대기 중")
        tk.Label(root, textvariable=self.status_var, fg="blue", justify="left").grid(
            row=row, column=0, columnspan=3, **pad
        )

        self.hotkeys = keyboard.Listener(on_press=self.on_key)
        self.hotkeys.daemon = True
        self.hotkeys.start()

        self.tick()

    # ---------- UI ----------
    def tick(self):
        self.now_var.set(fmt_ms(time.time() + self.server_offset))
        self.root.after(15, self.tick)

    def set_status(self, text: str):
        self.root.after(0, self.status_var.set, text)

    def on_key(self, key):
        if key == keyboard.Key.f8:
            x, y = self.mouse.position
            self.root.after(0, self.x_var.set, str(int(x)))
            self.root.after(0, self.y_var.set, str(int(y)))
            self.set_status(f"좌표 저장: ({int(x)}, {int(y)})")
        elif key == keyboard.Key.esc:
            self.stop()

    # ---------- 서버 측정 ----------
    def measure(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("입력 오류", "서버 주소를 입력하세요.")
            return
        self.measure_btn.config(state="disabled")
        self.set_status("서버 측정 중... (약 20초)")

        def progress(i, n, off, err):
            self.set_status(f"측정 중 {i}/{n}  시차 {off * 1000:+.0f}ms (±{err * 1000:.0f}ms)")

        def work():
            try:
                off, err, rtts = measure_server(url, progress=progress)
                self.server_offset = off
                self.baseline_rtt = statistics.median(rtts)
                info = f"PC보다 {abs(off) * 1000:.0f}ms {'빠름' if off >= 0 else '느림'} (오차 ±{err * 1000:.0f}ms)"
                self.root.after(0, self.offset_info_var.set, info)
                self.root.after(
                    0,
                    self.base_info_var.set,
                    f"왕복 {self.baseline_rtt * 1000:.0f}ms"
                    f" (최소 {min(rtts) * 1000:.0f} / 최대 {max(rtts) * 1000:.0f})",
                )
                self.set_status("측정 완료")
            except Exception as e:
                self.set_status(f"측정 실패: {e}")
            finally:
                self.root.after(0, lambda: self.measure_btn.config(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    # ---------- 예약 ----------
    def start(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("알림", "이미 예약되어 있습니다.")
            return
        try:
            target = parse_target(self.time_var.get())
            max_adj = max(0.0, float(self.max_adj_var.get() or 0)) / 1000
            x, y = int(self.x_var.get()), int(self.y_var.get())
            count = max(1, int(self.count_var.get()))
            interval = max(0.0, float(self.interval_var.get())) / 1000
            button = BUTTONS[self.button_var.get()]
            live = self.live_var.get()
            url = self.url_var.get().strip()
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e))
            return

        if live and (self.baseline_rtt is None or not url):
            messagebox.showerror("입력 오류", "실시간 보정을 쓰려면 먼저 '서버 측정'을 해 주세요.")
            return

        base_click = target.timestamp() - self.server_offset  # 기준 클릭 시각 (PC 시계)
        if base_click - max_adj <= time.time():
            messagebox.showerror("입력 오류", "예약 시각이 이미 지났습니다.")
            return

        self.cancel.clear()
        self.start_btn.config(state="disabled")
        self.live_info_var.set("-")
        self.set_status(f"예약됨: 서버 {fmt_ms(target.timestamp())} 기준 → ({x}, {y})")
        self.worker = threading.Thread(
            target=self.run, args=(base_click, max_adj, live, url, x, y, count, interval, button), daemon=True
        )
        self.worker.start()

    def monitor(self, url, base_click, max_adj, rtts, stop_at, inflight):
        """stop_at(PC 시각)까지 서버에 요청을 계속 보내 RTT를 rtts에 쌓는다.
        응답을 기다리는 중인 요청의 시작 시각은 inflight[0]에 둔다."""
        probe = None
        try:
            probe = Probe(url)
            while time.time() < stop_at and not self.cancel.is_set():
                inflight[0] = time.time()
                t0, t1, _ = probe.ping()
                inflight[0] = None
                rtts.append(t1 - t0)
                rtt = recent_rtt(rtts)
                adj = live_adjustment(rtt, self.baseline_rtt, max_adj)
                self.root.after(
                    0,
                    self.live_info_var.set,
                    f"왕복 {rtt * 1000:.0f}ms"
                    f" (평소 {self.baseline_rtt * 1000:.0f}) → {abs(adj) * 1000:.0f}ms {'일찍' if adj >= 0 else '늦게'}"
                    f" = 서버 {fmt_ms(base_click - adj + self.server_offset)}",
                )
                time.sleep(0.03)
        except Exception as e:
            self.set_status(f"실시간 측정 오류({e}), 측정된 값까지만 반영")
        finally:
            if probe:
                probe.close()

    def run(self, base_click, max_adj, live, url, x, y, count, interval, button):
        try:
            adj = 0.0
            rtts = []
            rtt = None
            if live:
                # 기준 시각 몇 초 전부터 측정 시작, 가장 일찍 누를 수 있는 시점 직전에 측정 종료
                if not wait_until(base_click - LIVE_START_SEC, self.cancel):
                    self.set_status("취소됨")
                    return
                stop_at = base_click - max_adj - 0.15
                inflight = [None]
                # 측정은 별도 스레드에서: 서버가 느려 응답이 늦어져도 클릭이 밀리지 않게
                threading.Thread(
                    target=self.monitor, args=(url, base_click, max_adj, rtts, stop_at, inflight), daemon=True
                ).start()
                if not wait_until(stop_at, self.cancel):
                    self.set_status("취소됨")
                    return
                started = inflight[0]
                rtt = recent_rtt(list(rtts), time.time() - started if started else 0.0)
                if rtt > 0:
                    adj = live_adjustment(rtt, self.baseline_rtt, max_adj)
                    self.root.after(
                        0, self.live_info_var.set, f"최종 왕복 {rtt * 1000:.0f}ms → {adj * 1000:+.0f}ms 보정"
                    )

            click_at = base_click - adj
            self.mouse.position = (x, y)  # 커서를 미리 옮겨 두어 클릭 순간의 지연을 줄임

            if not wait_until(click_at, self.cancel):
                self.set_status("취소됨")
                return

            fired = []
            for i in range(count):
                if i:
                    if not wait_until(click_at + interval * i, self.cancel):
                        break
                self.mouse.position = (x, y)
                self.mouse.click(button)
                fired.append(time.time())

            if not fired:
                self.set_status("취소됨")
                return
            first = fmt_ms(fired[0] + self.server_offset)
            self.set_status(
                f"완료: {len(fired)}회 클릭, 첫 클릭 서버 {first}"
                f" (보정 {adj * 1000:+.0f}ms, 타이머 오차 {(fired[0] - click_at) * 1000:+.2f}ms)"
            )
            write_log(
                {
                    "날짜": dt.date.today().isoformat(),
                    "기준 클릭(서버)": fmt_ms(base_click + self.server_offset),
                    "실제 클릭(서버)": first,
                    "보정(ms, +는 일찍)": round(adj * 1000),
                    "평소 왕복(ms)": round(self.baseline_rtt * 1000) if self.baseline_rtt else "",
                    "직전 왕복(ms)": round(rtt * 1000) if rtt else "",
                    "서버 시차(ms)": round(self.server_offset * 1000),
                    "결과(직접 기입)": "",
                }
            )
        finally:
            self.root.after(0, lambda: self.start_btn.config(state="normal"))

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
