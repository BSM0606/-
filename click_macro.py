"""예약 마우스 클릭 매크로 (밀리초 단위)

지정한 시각(HH:MM:SS.mmm)에 지정한 좌표를 클릭합니다.
- F8 : 현재 마우스 위치를 클릭 좌표로 저장
- ESC: 예약 취소
"""

import datetime as dt
import sys
import threading
import time
import tkinter as tk
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
    raise ValueError("시각 형식은 HH:MM:SS.mmm 이어야 합니다. 예) 20:00:00.000")


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

        root.title("예약 클릭 매크로")
        root.attributes("-topmost", True)
        root.resizable(False, False)

        pad = {"padx": 6, "pady": 3}
        row = 0

        tk.Label(root, text="현재 시각").grid(row=row, column=0, sticky="w", **pad)
        self.now_var = tk.StringVar()
        tk.Label(root, textvariable=self.now_var, font=("Consolas", 14, "bold")).grid(
            row=row, column=1, columnspan=2, sticky="w", **pad
        )
        row += 1

        tk.Label(root, text="예약 시각").grid(row=row, column=0, sticky="w", **pad)
        self.time_var = tk.StringVar(value=(dt.datetime.now() + dt.timedelta(minutes=1)).strftime("%H:%M:00.000"))
        tk.Entry(root, textvariable=self.time_var, width=16, font=("Consolas", 12)).grid(
            row=row, column=1, sticky="w", **pad
        )
        tk.Label(root, text="HH:MM:SS.mmm").grid(row=row, column=2, sticky="w", **pad)
        row += 1

        tk.Label(root, text="보정(ms)").grid(row=row, column=0, sticky="w", **pad)
        self.offset_var = tk.StringVar(value="0")
        tk.Entry(root, textvariable=self.offset_var, width=8).grid(row=row, column=1, sticky="w", **pad)
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
        self.now_var.set(dt.datetime.now().strftime("%H:%M:%S.%f")[:-3])
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

    # ---------- 예약 ----------
    def start(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("알림", "이미 예약되어 있습니다.")
            return
        try:
            target = parse_target(self.time_var.get())
            offset_ms = float(self.offset_var.get() or 0)
            x, y = int(self.x_var.get()), int(self.y_var.get())
            count = max(1, int(self.count_var.get()))
            interval = max(0.0, float(self.interval_var.get())) / 1000
            button = BUTTONS[self.button_var.get()]
        except ValueError as e:
            messagebox.showerror("입력 오류", str(e))
            return

        target_epoch = target.timestamp() + offset_ms / 1000
        if target_epoch <= time.time():
            messagebox.showerror("입력 오류", "예약 시각이 이미 지났습니다.")
            return

        self.cancel.clear()
        self.start_btn.config(state="disabled")
        self.set_status(f"예약됨: {target.strftime('%H:%M:%S.%f')[:-3]} → ({x}, {y})")
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
                first = dt.datetime.fromtimestamp(fired[0]).strftime("%H:%M:%S.%f")[:-3]
                self.set_status(f"완료: {len(fired)}회 클릭, 첫 클릭 {first} (오차 {err_ms:+.2f}ms)")
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
