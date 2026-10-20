"""예약 마우스 클릭 매크로 (밀리초 단위, 서버 시간 기준)

지정한 서버 시각(HH:MM:SS.mmm)에 지정한 좌표를 클릭합니다.
- F8 : 현재 마우스 위치를 클릭 좌표로 저장
- ESC: 예약 취소
"""

import datetime as dt
import email.utils
import http.client
import math
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
    raise ValueError("시각 형식은 HH:MM:SS.mmm 이어야 합니다. 예) 16:30:00.000")


def measure_server_offset(url: str, samples: int = 20, progress=None):
    """서버 응답의 Date 헤더로 서버 시계와 PC 시계의 차이를 측정.

    Date 헤더는 초 단위라서, 요청을 여러 번 보내 초가 바뀌는 순간을 좁혀 들어가는 방식으로
    밀리초 단위 차이를 추정한다. 반환: (offset 초, 오차 범위 ±초). 서버시각 = PC시각 + offset.
    """
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.netloc, timeout=5)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    headers = {"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}

    # 서버 시각이 [S, S+1) 이고 응답이 PC 시각 [t0, t1] 사이에 만들어졌다면
    # offset 은 [S - t1, S + 1 - t0) 범위. 여러 번 측정해 범위의 교집합을 구한다.
    lo, hi = -float("inf"), float("inf")
    rtt = 0.0
    try:
        conn.request("HEAD", path, headers=headers)  # 연결(TLS 핸드셰이크) 미리 맺기
        conn.getresponse().read()
        for i in range(samples):
            now = time.time()
            if i < 5 or hi - lo > 0.9:
                # 처음에는 초 경계의 여러 위치를 고르게 훑는다
                time.sleep(0.05 + 0.21 * (i % 5))
            else:
                # 범위가 좁아지면 예상되는 서버 초 경계 근처를 겨냥해 범위를 더 좁힌다
                mid = (lo + hi) / 2
                edge = math.floor(now + mid) + 1 - mid  # 다음 서버 초 경계의 PC 시각
                aim = edge + (hi - lo) * (0.25 if i % 2 else -0.25) - rtt / 2
                while aim < now + 0.01:
                    aim += 1
                time.sleep(aim - now)
            t0 = time.time()
            conn.request("HEAD", path, headers=headers)
            resp = conn.getresponse()
            resp.read()
            t1 = time.time()
            rtt = t1 - t0
            date = resp.getheader("Date")
            if not date:
                raise RuntimeError("서버 응답에 Date 헤더가 없습니다.")
            server_sec = email.utils.parsedate_to_datetime(date).timestamp()
            new_lo, new_hi = max(lo, server_sec - t1), min(hi, server_sec + 1 - t0)
            if new_lo < new_hi:  # 튀는 측정값(지연 등)은 무시
                lo, hi = new_lo, new_hi
            if progress:
                progress(i + 1, samples, (lo + hi) / 2, (hi - lo) / 2)
    finally:
        conn.close()
    return (lo + hi) / 2, (hi - lo) / 2


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


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.mouse = mouse.Controller()
        self.cancel = threading.Event()
        self.worker = None
        self.server_offset = 0.0  # 서버시각 = PC시각 + server_offset (초)

        root.title("예약 클릭 매크로")
        root.attributes("-topmost", True)
        root.resizable(False, False)

        pad = {"padx": 6, "pady": 3}
        row = 0

        tk.Label(root, text="서버 주소").grid(row=row, column=0, sticky="w", **pad)
        self.url_var = tk.StringVar(value="https://berriz.in")
        tk.Entry(root, textvariable=self.url_var, width=24).grid(row=row, column=1, sticky="w", **pad)
        self.measure_btn = tk.Button(root, text="서버 시간 측정", command=self.measure)
        self.measure_btn.grid(row=row, column=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="서버 시차").grid(row=row, column=0, sticky="w", **pad)
        self.offset_info_var = tk.StringVar(value="측정 안 함 (PC 시각 사용)")
        tk.Label(root, textvariable=self.offset_info_var).grid(row=row, column=1, columnspan=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="서버 시각").grid(row=row, column=0, sticky="w", **pad)
        self.now_var = tk.StringVar()
        tk.Label(root, textvariable=self.now_var, font=("Consolas", 14, "bold"), fg="red").grid(
            row=row, column=1, columnspan=2, sticky="w", **pad
        )
        row += 1

        tk.Label(root, text="예약 시각").grid(row=row, column=0, sticky="w", **pad)
        self.time_var = tk.StringVar(value=(dt.datetime.now() + dt.timedelta(minutes=1)).strftime("%H:%M:00.000"))
        tk.Entry(root, textvariable=self.time_var, width=16, font=("Consolas", 12)).grid(
            row=row, column=1, sticky="w", **pad
        )
        tk.Label(root, text="서버 기준 HH:MM:SS.mmm").grid(row=row, column=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="추가 보정(ms)").grid(row=row, column=0, sticky="w", **pad)
        self.extra_var = tk.StringVar(value="0")
        tk.Entry(root, textvariable=self.extra_var, width=8).grid(row=row, column=1, sticky="w", **pad)
        tk.Label(root, text="음수=일찍, 양수=늦게").grid(row=row, column=2, sticky="w", **pad)
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
        tk.Label(root, textvariable=self.status_var, fg="blue").grid(row=row, column=0, columnspan=3, **pad)

        self.hotkeys = keyboard.Listener(on_press=self.on_key)
        self.hotkeys.daemon = True
        self.hotkeys.start()

        self.tick()

    # ---------- UI ----------
    def tick(self):
        server_now = dt.datetime.fromtimestamp(time.time() + self.server_offset)
        self.now_var.set(server_now.strftime("%H:%M:%S.%f")[:-3])
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

    # ---------- 서버 시간 ----------
    def measure(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("입력 오류", "서버 주소를 입력하세요.")
            return
        self.measure_btn.config(state="disabled")
        self.set_status("서버 시간 측정 중... (약 20초)")

        def progress(i, n, off, err):
            self.set_status(f"측정 중 {i}/{n}  시차 {off * 1000:+.0f}ms (±{err * 1000:.0f}ms)")

        def work():
            try:
                off, err = measure_server_offset(url, progress=progress)
                self.server_offset = off
                info = f"PC보다 {abs(off) * 1000:.0f}ms {'빠름' if off >= 0 else '느림'} (오차 ±{err * 1000:.0f}ms)"
                self.root.after(0, self.offset_info_var.set, info)
                self.set_status("서버 시간 측정 완료")
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
            extra_ms = float(self.extra_var.get() or 0)
            x, y = int(self.x_var.get()), int(self.y_var.get())
            count = max(1, int(self.count_var.get()))
            interval = max(0.0, float(self.interval_var.get())) / 1000
            button = BUTTONS[self.button_var.get()]
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e))
            return

        # 서버 시각 → PC 시각으로 변환
        target_epoch = target.timestamp() - self.server_offset + extra_ms / 1000
        if target_epoch <= time.time():
            messagebox.showerror("입력 오류", "예약 시각이 이미 지났습니다.")
            return

        self.cancel.clear()
        self.start_btn.config(state="disabled")
        self.set_status(f"예약됨: 서버 {target.strftime('%H:%M:%S.%f')[:-3]} → ({x}, {y})")
        self.worker = threading.Thread(
            target=self.run, args=(target_epoch, x, y, count, interval, button), daemon=True
        )
        self.worker.start()

    def run(self, target_epoch, x, y, count, interval, button):
        try:
            # 미리 커서를 옮겨 두면 클릭 순간의 지연이 줄어듦 (1초 전)
            if not wait_until(target_epoch - 1.0, self.cancel):
                self.set_status("취소됨")
                return
            self.mouse.position = (x, y)

            if not wait_until(target_epoch, self.cancel):
                self.set_status("취소됨")
                return

            fired = []
            for i in range(count):
                if i:
                    if not wait_until(target_epoch + interval * i, self.cancel):
                        break
                self.mouse.position = (x, y)
                self.mouse.click(button)
                fired.append(time.time())

            if fired:
                err_ms = (fired[0] - target_epoch) * 1000
                first = dt.datetime.fromtimestamp(fired[0] + self.server_offset).strftime("%H:%M:%S.%f")[:-3]
                self.set_status(f"완료: {len(fired)}회 클릭, 첫 클릭 서버 {first} (오차 {err_ms:+.2f}ms)")
            else:
                self.set_status("취소됨")
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
