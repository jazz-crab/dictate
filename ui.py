"""Окна диктовщика в тёмной палитре Tokyo Night.

Один Tk-поток обслуживает два окна:

* плашка статуса — поверх всех окон, но фокус не забирает
  (WS_EX_NOACTIVATE): запись, распознавание, отправка;
* окно правки — наоборот, забирает фокус: в нём распознанный текст,
  его можно поправить, сформулировать заново или отправить.

Снаружи окна дёргают только через очередь; виджеты трогает лишь Tk-поток.
Палитра — тёмная, в духе Tokyo Night; шрифт моноширинный.
"""

import ctypes
import queue
import threading
import time
from ctypes import wintypes

# ── палитра ────────────────────────────────────────────────────────────────
BG = "#1a1b26"
BG_ALT = "#16161e"
BG_POPUP = "#1f2335"
BG_SOFT = "#24283b"
BG_SELECT = "#283457"
BORDER = "#2f3b54"
FG = "#c0caf5"
FG_DARK = "#a9b1d6"
COMMENT = "#565f89"
RED = "#f7768e"
ORANGE = "#ff9e64"
YELLOW = "#e0af68"
GREEN = "#9ece6a"
CYAN = "#7dcfff"
BLUE = "#7aa2f7"

# --mono = JetBrainsMono -> Cascadia Mono -> Consolas
MONO = "Consolas"

STATUS_COLORS = {
    "record": RED,
    "work": BLUE,
    "done": GREEN,
    "error": ORANGE,
    "warn": YELLOW,
}

WIN_WIDTH = 760
MIN_HEIGHT = 250
MAX_HEIGHT = 560


class VoiceUI:
    """Плашка статуса + окно правки. Все методы можно звать из любого потока."""

    def __init__(self, on_send=None, on_formulate=None, on_cancel=None,
                 on_abort=None, enabled=True, status_enabled=True,
                 min_smart_chars=200):
        self.enabled = enabled
        self.status_enabled = status_enabled
        self.on_send = on_send
        self.on_formulate = on_formulate
        self.on_cancel = on_cancel
        self.on_abort = on_abort
        # Короче этого формулировка только портит текст: чистить нечего,
        # а модель норовит «домыслить» короткую фразу по-своему.
        self.min_smart_chars = int(min_smart_chars or 0)
        self._q = queue.Queue()
        self._sid = 0
        self._editor = None
        self._pill = None
        self._source = ""
        self._formulated = False
        self._anim_job = None
        self._busy_label = "Формулирую"
        self._status_hide_job = None

    # ── снаружи ────────────────────────────────────────────────────────────

    def start(self):
        if not self.enabled:
            return
        threading.Thread(target=self._run, name="ui", daemon=True).start()

    def status(self, text, kind="work", timeout=None):
        if self.enabled and self.status_enabled:
            self._q.put(("status", text, kind, timeout))

    def hide_status(self, delay=0.0):
        if self.enabled and self.status_enabled:
            self._q.put(("hide_status", delay))

    def open_editor(self, sid, text, meta=""):
        if self.enabled:
            self._q.put(("open", sid, text, meta))

    def editor_busy(self, sid, label="Формулирую"):
        if self.enabled:
            self._q.put(("busy", sid, label))

    def editor_progress(self, sid, label):
        """Обновить подпись под текстом, пока идёт долгая формулировка."""
        if self.enabled:
            self._q.put(("busy_label", sid, label))

    def editor_result(self, sid, text, meta=""):
        if self.enabled:
            self._q.put(("result", sid, text, meta))

    def editor_error(self, sid, message):
        if self.enabled:
            self._q.put(("error", sid, message))

    def close_editor(self, sid, wait=False):
        """Закрыть окно правки. wait=True — дождаться, пока Tk реально скроет окно."""
        if not self.enabled:
            return
        done = threading.Event() if wait else None
        self._q.put(("close", sid, done))
        if done is not None:
            done.wait(1.5)

    def flash(self):
        if self.enabled:
            self._q.put(("flash",))

    def call(self, fn):
        """Выполнить fn в Tk-потоке — единственный безопасный способ трогать виджеты снаружи."""
        if self.enabled:
            self._q.put(("call", fn))

    # ── Tk-поток ───────────────────────────────────────────────────────────

    def _run(self):
        try:
            import tkinter as tk
        except Exception:
            return
        try:
            root = tk.Tk()
        except Exception:
            return

        self._root = root
        root.withdraw()
        self._build_pill(tk)
        self._build_editor(tk)
        root.after(40, self._pump)
        try:
            root.mainloop()
        except Exception:
            pass

    def _build_pill(self, tk):
        pill = tk.Toplevel(self._root)
        pill.withdraw()
        pill.overrideredirect(True)
        pill.attributes("-topmost", True)
        pill.configure(bg=BORDER)
        frame = tk.Frame(pill, bg=BG_POPUP, padx=16, pady=8)
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        label = tk.Label(frame, text="", bg=BG_POPUP, fg=FG, font=(MONO, 10), justify="left")
        label.pack()
        self._pill = pill
        self._pill_label = label
        try:
            _no_activate(pill)
            _round_corners(pill)
        except Exception:
            pass

    def _build_editor(self, tk):
        ed = tk.Toplevel(self._root)
        ed.withdraw()
        ed.overrideredirect(True)
        ed.attributes("-topmost", True)
        ed.configure(bg=BORDER)

        body = tk.Frame(ed, bg=BG_POPUP, padx=16, pady=14)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(body, bg=BG_POPUP)
        head.pack(fill="x")
        tk.Label(head, text="Диктовка", bg=BG_POPUP, fg=FG,
                 font=(MONO, 10, "bold")).pack(side="left")
        close = tk.Button(head, text="\u00d7", command=self._on_cancel_clicked,
                          bg=BG_POPUP, fg=COMMENT, activebackground=BG_SOFT,
                          activeforeground=FG, bd=0, relief="flat", overrelief="flat",
                          font=(MONO, 14), padx=4, pady=0, cursor="hand2", takefocus=0,
                          highlightthickness=0)
        close.pack(side="right")
        _hover(close, BG_POPUP, BG_SOFT, COMMENT, FG)

        meta = tk.Label(body, text="", bg=BG_POPUP, fg=COMMENT, font=(MONO, 9),
                        anchor="w", justify="left")
        meta.pack(fill="x", pady=(6, 8))

        ring = tk.Frame(body, bg=BORDER)
        ring.pack(fill="both", expand=True)

        text = tk.Text(ring, bg=BG, fg=FG, insertbackground=BLUE,
                       selectbackground=BG_SELECT, selectforeground=FG,
                       relief="flat", bd=0, highlightthickness=0,
                       padx=12, pady=10, wrap="word", font=(MONO, 11),
                       spacing1=2, spacing3=2, undo=True, maxundo=-1,
                       height=6)
        bar = tk.Scrollbar(ring, command=text.yview, width=10, bd=0,
                           bg=BG_SOFT, troughcolor=BG_POPUP,
                           activebackground=COMMENT, highlightthickness=0)
        text.configure(yscrollcommand=bar.set)
        text.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        bar.pack(side="right", fill="y", padx=(0, 1), pady=1)

        # Тонкая полоса прогресса: показывается только пока идёт формулировка.
        # Нужна, чтобы окно не выглядело зависшим, но и не крутило «часики».
        progress = tk.Canvas(body, height=3, bg=BG_POPUP, bd=0,
                             highlightthickness=0, takefocus=0)
        progress.pack(fill="x", pady=(3, 0))

        foot = tk.Frame(body, bg=BG_POPUP)
        foot.pack(fill="x", pady=(12, 0))
        hint = tk.Label(foot, text="Ctrl+Enter — отправить · F2 — умная формулировка · Esc — закрыть",
                        bg=BG_POPUP, fg=COMMENT, font=(MONO, 9), anchor="w")
        hint.pack(fill="x", pady=(0, 8))

        btns = tk.Frame(foot, bg=BG_POPUP)
        btns.pack(fill="x")
        btn_cancel = _button(btns, "Отмена", self._on_cancel_clicked, "ghost")
        btn_smart = _button(btns, "Умная формулировка", self._on_smart_clicked, "secondary")
        btn_send = _button(btns, "В opencode", self._on_send_clicked, "primary")
        btn_send.pack(side="right")
        btn_smart.pack(side="right", padx=(8, 0))
        btn_cancel.pack(side="right", padx=(8, 0))

        text.bind("<Control-Return>", self._on_send_event)
        text.bind("<F2>", self._on_smart_event)
        text.bind("<<Modified>>", self._on_modified)
        text.bind("<Control-a>", lambda e: (text.tag_add("sel", "1.0", "end-1c"), "break")[1])
        ed.bind("<Escape>", lambda e: self._on_cancel_clicked())
        text.bind("<FocusIn>", lambda e: ring.configure(bg=BLUE))
        text.bind("<FocusOut>", lambda e: ring.configure(bg=BORDER))

        _drag(ed, head)

        self._editor = ed
        self._text = text
        self._ring = ring
        self._meta = meta
        self._hint = hint
        self._progress = progress
        self._progress_job = None
        self._progress_on = False
        self._btn_cancel = btn_cancel
        self._btn_smart = btn_smart
        self._btn_send = btn_send
        self._busy = False
        try:
            _round_corners(ed)
        except Exception:
            pass

    # ── очередь ────────────────────────────────────────────────────────────

    def _pump(self):
        try:
            while True:
                item = self._q.get_nowait()
                kind = item[0]
                if kind == "status":
                    self._show_status(item[1], item[2], item[3])
                elif kind == "hide_status":
                    self._schedule_hide(item[1])
                elif kind == "open":
                    self._open(item[1], item[2], item[3])
                elif kind == "busy":
                    if item[1] == self._sid:
                        self._set_busy(True, item[2])
                elif kind == "busy_label":
                    if item[1] == self._sid and self._busy:
                        self._busy_label = item[2]
                elif kind == "result":
                    if item[1] == self._sid:
                        self._apply_result(item[2], item[3])
                elif kind == "error":
                    if item[1] == self._sid:
                        self._apply_error(item[2])
                elif kind == "close":
                    if item[1] == self._sid:
                        self._hide_editor()
                    if item[2] is not None:
                        item[2].set()
                elif kind == "flash":
                    self._flash()
                elif kind == "call":
                    try:
                        item[1]()
                    except Exception:
                        pass
        except queue.Empty:
            pass
        try:
            self._root.after(40, self._pump)
        except Exception:
            pass

    # ── плашка ─────────────────────────────────────────────────────────────

    def _show_status(self, text, kind, timeout):
        if not self.status_enabled:
            return
        if self._status_hide_job:
            try:
                self._root.after_cancel(self._status_hide_job)
            except Exception:
                pass
            self._status_hide_job = None
        self._pill_label.configure(text=text, fg=STATUS_COLORS.get(kind, FG))
        pill = self._pill
        pill.update_idletasks()
        w = max(pill.winfo_reqwidth(), 140)
        h = pill.winfo_reqheight()
        sw = pill.winfo_screenwidth()
        sh = pill.winfo_screenheight()
        pill.geometry(f"{w}x{h}+{(sw - w) // 2}+{sh - h - 90}")
        pill.deiconify()
        pill.lift()
        if timeout:
            self._status_hide_job = self._root.after(int(timeout * 1000), self._hide_status)

    def _schedule_hide(self, delay):
        if delay:
            self._status_hide_job = self._root.after(int(delay * 1000), self._hide_status)
        else:
            self._hide_status()

    def _hide_status(self):
        self._status_hide_job = None
        try:
            self._pill.withdraw()
        except Exception:
            pass

    # ── окно правки ────────────────────────────────────────────────────────

    def _open(self, sid, text, meta):
        self._sid = sid
        self._source = text
        self._formulated = False
        self._hide_status()
        self._set_text(text)
        self._meta.configure(text=meta, fg=COMMENT)
        self._set_busy(False)
        self._btn_smart.configure(text="Умная формулировка")
        self._update_smart_state()
        self._place()
        ed = self._editor
        ed.deiconify()
        ed.update_idletasks()
        ed.update()
        ed.lift()
        ed.attributes("-topmost", True)
        try:
            _force_foreground(ed)
        except Exception:
            pass
        ed.focus_force()
        self._text.focus_force()
        self._text.mark_set("insert", "1.0")
        self._text.see("1.0")
        # Иногда Windows отдаёт фокус с задержкой — повторяем чуть позже.
        self._root.after(120, self._focus_text)

    def _focus_text(self):
        try:
            if not self._editor.winfo_viewable():
                return
            _force_foreground(self._editor)
            self._editor.focus_force()
            self._text.focus_force()
        except Exception:
            pass

    def _hide_editor(self):
        self._stop_anim()
        self._stop_progress()
        self._busy = False
        try:
            self._editor.withdraw()
        except Exception:
            pass

    def _place(self):
        self._text.configure(height=self._text_height())
        ed = self._editor
        ed.update_idletasks()
        h = min(max(ed.winfo_reqheight(), MIN_HEIGHT), MAX_HEIGHT)
        sw = ed.winfo_screenwidth()
        sh = ed.winfo_screenheight()
        x = (sw - WIN_WIDTH) // 2
        y = int(sh * 0.22)
        ed.geometry(f"{WIN_WIDTH}x{h}+{x}+{y}")
        ed.update_idletasks()
        self._meta.configure(wraplength=WIN_WIDTH - 40)

    def _text_height(self):
        text = self._text.get("1.0", "end-1c")
        lines = text.count("\n") + 1 + len(text) // 82
        return max(6, min(16, lines))

    def _set_text(self, text):
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", text)
        self._text.edit_reset()
        self._text.configure(state="normal")

    def _current_text(self):
        return self._text.get("1.0", "end-1c").strip()

    def _on_modified(self, event=None):
        """Поле изменилось — пересчитать, есть ли смысл в формулировке."""
        try:
            if not self._text.edit_modified():
                return
            self._text.edit_modified(False)
        except Exception:
            return
        self._update_smart_state()

    def _update_smart_state(self):
        """Кнопка формулировки доступна только на достаточно длинном тексте.

        На короткой фразе чистить нечего, а модель легко «додумает» её
        по-своему — пусть уходит как есть, OpenCode поймёт.
        """
        if self._busy:
            return
        try:
            if self._formulated:
                self._btn_smart.configure(text="Вернуть исходный", state="normal")
                self._hint.configure(
                    text="Ctrl+Enter — отправить · F2 — вернуть исходный · Esc — закрыть")
                return
            n = len(self._current_text())
            need = self.min_smart_chars
            if need and n < need:
                self._btn_smart.configure(text="Умная формулировка", state="disabled")
                self._hint.configure(
                    text=(f"Короткий текст ({n} из {need} символов) — формулировка "
                          "не нужна, отправляйте как есть"))
            else:
                self._btn_smart.configure(text="Умная формулировка", state="normal")
                self._hint.configure(
                    text=("Ctrl+Enter — отправить · F2 — умная формулировка · Esc — закрыть"))
        except Exception:
            pass

    def _set_busy(self, busy, label="Формулирую"):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self._text.configure(state=state)
        self._btn_send.configure(state=state)
        if busy:
            # Никаких «часиков»: окно живое, оно просто ждёт ответ модели.
            # Отмену держим доступной — это главный признак, что всё работает.
            self._btn_smart.configure(state="normal", text="Отменить")
            self._text.configure(cursor="arrow")
            self._start_anim(label)
        else:
            self._btn_smart.configure(state="normal")
            self._text.configure(cursor="xterm")
            self._stop_anim()
            self._stop_progress()
            self._update_smart_state()

    def _start_anim(self, label):
        self._stop_anim()
        self._busy_label = label
        started = time.time()

        def tick():
            if not self._busy:
                return
            elapsed = time.time() - started
            dots = "." * (int(elapsed * 2.5) % 4)
            self._meta.configure(text=f"{self._busy_label}{dots} {elapsed:.1f} с", fg=BLUE)
            self._anim_job = self._root.after(150, tick)

        tick()
        self._start_progress()

    def _stop_anim(self):
        if self._anim_job:
            try:
                self._root.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None

    def _start_progress(self):
        """Бегущая полоска: 40 кадров в секунду, но каждый — два вызова Canvas."""
        self._stop_progress()
        canvas = self._progress
        canvas.configure(bg=BG_SOFT)
        self._progress_on = True
        span = 120

        def step():
            if not self._progress_on:
                return
            width = max(canvas.winfo_width(), 1)
            pos = (time.time() * 420) % (width + span) - span
            canvas.delete("bar")
            canvas.create_rectangle(pos, 0, pos + span, 3, fill=BLUE, width=0, tags="bar")
            self._progress_job = canvas.after(40, step)

        step()

    def _stop_progress(self):
        self._progress_on = False
        if self._progress_job:
            try:
                self._progress.after_cancel(self._progress_job)
            except Exception:
                pass
            self._progress_job = None
        try:
            self._progress.delete("bar")
            self._progress.configure(bg=BG_POPUP)
        except Exception:
            pass

    def _apply_result(self, text, meta):
        self._set_busy(False)
        self._set_text(text)
        self._formulated = True
        self._update_smart_state()
        self._meta.configure(text=meta, fg=GREEN)
        self._place()
        self._focus_if_active()
        self._text.mark_set("insert", "1.0")
        self._text.see("1.0")

    def _apply_error(self, message):
        self._set_busy(False)
        self._meta.configure(text=message, fg=ORANGE)
        self._place()
        self._focus_if_active()

    def _focus_if_active(self):
        """Вернуть фокус в поле, только если окно и так было активным.

        Пока шла формулировка, человек мог уйти в другое окно; выдёргивать
        фокус из-под него нельзя.
        """
        try:
            if not self._editor.winfo_viewable():
                return
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetForegroundWindow.restype = wintypes.HWND
            if user32.GetForegroundWindow() == _hwnd(self._editor):
                self._text.focus_force()
        except Exception:
            pass

    def _flash(self):
        if self._editor is None or not self._editor.winfo_viewable():
            return
        self._editor.lift()
        self._editor.bell()

    # ── кнопки ─────────────────────────────────────────────────────────────

    def _on_send_event(self, event):
        self._on_send_clicked()
        return "break"

    def _on_smart_event(self, event):
        self._on_smart_clicked()
        return "break"

    def _on_send_clicked(self):
        if self._busy:
            return
        text = self._current_text()
        if not text:
            self._meta.configure(text="Пустой текст отправлять нечего", fg=ORANGE)
            return
        sid = self._sid
        self._set_busy(True, "Отправляю")
        if self.on_send:
            self.on_send(sid, text)

    def _on_smart_clicked(self):
        if self._busy:
            # Вторая кнопка во время ожидания — это «Отменить», а не «ещё раз».
            sid = self._sid
            self._set_busy(False)
            self._update_smart_state()
            self._meta.configure(text="Формулировка отменена", fg=COMMENT)
            self._focus_if_active()
            if self.on_abort:
                self.on_abort(sid)
            return
        if self._formulated:
            self._set_text(self._source)
            self._formulated = False
            self._update_smart_state()
            self._meta.configure(text="Исходный текст", fg=COMMENT)
            self._place()
            self._focus_if_active()
            return
        text = self._current_text()
        if not text:
            return
        if self.min_smart_chars and len(text) < self.min_smart_chars:
            self._meta.configure(
                text=(f"Короткий текст ({len(text)} из {self.min_smart_chars} символов) — "
                      "формулировать нечего, отправляйте как есть"), fg=COMMENT)
            return
        self._source = text
        sid = self._sid
        self._set_busy(True, "Формулирую")
        if self.on_formulate:
            self.on_formulate(sid, text)

    def _on_cancel_clicked(self):
        if self._busy:
            # Формулировку можно бросить: ответ просто не покажем.
            self._set_busy(False)
        sid = self._sid
        self._hide_editor()
        if self.on_cancel:
            self.on_cancel(sid)


# ── мелкие помощники ────────────────────────────────────────────────────────


def _button(parent, text, command, kind):
    import tkinter as tk

    btn = tk.Button(parent, text=text, command=command, font=(MONO, 10),
                    bd=0, relief="flat", overrelief="flat", padx=12, pady=6,
                    cursor="hand2", takefocus=0, highlightthickness=1,
                    activeforeground=FG, disabledforeground=COMMENT)
    if kind == "primary":
        bg, fg, border, hover_bg = BLUE, BG, BLUE, CYAN
    elif kind == "secondary":
        bg, fg, border, hover_bg = BG_SOFT, FG, BORDER, BG_SELECT
    else:  # ghost
        bg, fg, border, hover_bg = BG_POPUP, FG_DARK, BG_POPUP, BG_SOFT
    btn.configure(bg=bg, fg=fg, activebackground=hover_bg,
                  highlightbackground=border, highlightcolor=border)
    _hover(btn, bg, hover_bg, fg, FG, border, BLUE if kind != "ghost" else BG_SOFT)
    return btn


def _hover(widget, bg, hover_bg, fg, hover_fg, border=None, hover_border=None):
    def enter(_):
        if str(widget["state"]) == "disabled":
            return
        widget.configure(bg=hover_bg, fg=hover_fg)
        if border:
            widget.configure(highlightbackground=hover_border)

    def leave(_):
        if str(widget["state"]) == "disabled":
            return
        widget.configure(bg=bg, fg=fg)
        if border:
            widget.configure(highlightbackground=border)

    widget.bind("<Enter>", enter)
    widget.bind("<Leave>", leave)


def _drag(window, handle):
    state = {"x": 0, "y": 0}

    def start(event):
        state["x"] = event.x_root - window.winfo_x()
        state["y"] = event.y_root - window.winfo_y()

    def move(event):
        window.geometry(f"+{event.x_root - state['x']}+{event.y_root - state['y']}")

    handle.bind("<ButtonPress-1>", start)
    handle.bind("<B1-Motion>", move)
    for child in handle.winfo_children():
        child.bind("<ButtonPress-1>", start)
        child.bind("<B1-Motion>", move)


def _hwnd(window):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
    return int(hwnd)


def _no_activate(window):
    """WS_EX_NOACTIVATE, чтобы плашка не отбирала фокус у активного окна."""
    GWL_EXSTYLE = -20
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    hwnd = _hwnd(window)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


def _force_foreground(window):
    """Вывести окно вперёд и отдать ему клавиатурный фокус."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    hwnd = _hwnd(window)
    fg = user32.GetForegroundWindow()
    if fg != hwnd:
        tid = kernel32.GetCurrentThreadId()
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        attached = bool(tid_fg and tid_fg != tid and user32.AttachThreadInput(tid, tid_fg, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(tid, tid_fg, False)
        if user32.GetForegroundWindow() != hwnd:
            user32.SwitchToThisWindow(hwnd, True)
        if user32.GetForegroundWindow() != hwnd:
            # Одиночное нажатие Alt снимает foreground lock Windows.
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            time.sleep(0.03)
            user32.SetForegroundWindow(hwnd)
            user32.SwitchToThisWindow(hwnd, True)
    window.focus_force()


def _round_corners(window):
    """Скруглённые углы и цвет рамки силами DWM (Windows 11)."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                             ctypes.c_void_p, wintypes.DWORD]
        hwnd = _hwnd(window)
        pref = ctypes.c_int(2)          # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        color = ctypes.c_int(0x00543B2F)  # COLORREF для #2f3b54
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))
    except Exception:
        pass
