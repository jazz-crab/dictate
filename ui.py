"""Окна диктовщика в тёмной палитре Tokyo Night.

Один Tk-поток обслуживает три окна:

* плашка статуса — поверх всех окон, но фокус не забирает (WS_EX_NOACTIVATE);
* окно правки — обычное окно с заголовком, иконкой в панели задач и кнопкой
  «Свернуть»: сначала в нём живой черновик записи (текст появляется по мере
  диктовки), потом — готовый текст с кнопками «Копировать в буфер» и
  «Умная формулировка». Клавиши стирания едины в обоих режимах:
  Backspace — символ, Ctrl/Alt+Backspace — слово, Ctrl/Alt+Shift+Backspace —
  фразу, Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y — отмена/повтор, Ctrl+C — выделенное,
  Ctrl+Shift+C — весь текст;
* главное окно (Ctrl+Alt+Shift+D) — история записей со статистикой,
  текст в двух видах (распознанный/отредактированный) и лог.

Снаружи окна дёргают только через очередь; виджеты трогает лишь Tk-поток.
Палитра — тёмная, в духе Tokyo Night; шрифт моноширинный.
"""

import ctypes
import queue
import re
import threading
import time
from ctypes import wintypes
from pathlib import Path

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
BLUE_HOVER = "#5d87e8"

# --mono = JetBrainsMono -> Cascadia Mono -> Consolas
MONO = "Consolas"

APP_ICON = Path(__file__).resolve().parent / "dictate.ico"

STATUS_COLORS = {
    "record": RED,
    "work": BLUE,
    "done": GREEN,
    "error": ORANGE,
    "warn": YELLOW,
}

WIN_WIDTH = 760
MIN_HEIGHT = 250
MAIN_W, MAIN_H = 1080, 660


def _fmt_dur(s):
    """12.4 -> 0:12, 195.0 -> 3:15."""
    try:
        s = float(s or 0)
    except (TypeError, ValueError):
        return "0:00"
    return f"{int(s) // 60}:{int(s) % 60:02d}"


class VoiceUI:
    """Плашка статуса + окно правки + главное окно. Методы можно звать из любого потока."""

    def __init__(self, on_copy=None, on_stop=None, on_formulate=None,
                 on_close=None, on_abort=None, on_live_delete=None,
                 history_fn=None, log_fn=None, save_edit_fn=None,
                 enabled=True, status_enabled=True, min_smart_chars=200):
        self.enabled = enabled
        self.status_enabled = status_enabled
        self.on_copy = on_copy
        self.on_stop = on_stop
        self.on_formulate = on_formulate
        self.on_close = on_close
        self.on_abort = on_abort
        self.on_live_delete = on_live_delete
        self.history_fn = history_fn
        self.log_fn = log_fn
        self.save_edit_fn = save_edit_fn
        # Короче этого формулировка только портит текст: чистить нечего,
        # а модель норовит «домыслить» короткую фразу по-своему.
        self.min_smart_chars = int(min_smart_chars or 0)
        self._q = queue.Queue()
        self._sid = 0
        self._editor = None
        # Окно правки по умолчанию поверх всех; открепляется галочкой в шапке.
        self._pinned = True
        self._pill = None
        self._main = None
        self._source = ""
        self._formulated = False
        self._live = False
        self._anim_job = None
        self._busy_label = "Формулирую"
        self._status_hide_job = None
        self._place_job = None
        self._placed_once = False
        # главное окно
        self._history = []
        self._sel = 0
        self._main_tab = "records"
        self._main_shown = False
        self._save_job = None
        self._detail_ts = None

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

    def open_editor(self, sid, text, meta="", live=False):
        if self.enabled:
            self._q.put(("open", sid, text, meta, live))

    def editor_live(self, sid, confirmed, draft, meta=""):
        """Обновить живой черновик: подтверждённый текст + акцентный хвост."""
        if self.enabled:
            self._q.put(("live", sid, confirmed, draft, meta))

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

    def open_main(self):
        if self.enabled:
            self._q.put(("main", "show"))

    def close_main(self):
        if self.enabled:
            self._q.put(("main", "hide"))

    def toggle_main(self):
        if self.enabled:
            self._q.put(("main", "toggle"))

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
        self._build_main(tk)
        root.after(40, self._pump)
        root.after(2500, self._main_refresh)
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
        # Обычное окно: заголовок, панель задач, «Свернуть», ручное изменение
        # размера — а ещё авто-рост по мере роста текста (см. _place).
        ed.title("Диктовка")
        ed.attributes("-topmost", bool(self._pinned))
        ed.configure(bg=BORDER)
        try:
            if APP_ICON.exists():
                ed.iconbitmap(str(APP_ICON))
            _dark_titlebar(ed)
        except Exception:
            pass

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
        # Открепление: «поверх всех» — галочка, а не навсегда. Нажата —
        # окно висит над другими (как раньше), снята — живёт обычной жизнью.
        self._pin_var = tk.BooleanVar(value=self._pinned)
        pin = tk.Checkbutton(head, text="поверх всех", variable=self._pin_var,
                             command=self._toggle_pin,
                             bg=BG_POPUP, fg=COMMENT, activebackground=BG_POPUP,
                             activeforeground=FG, selectcolor=BG_SOFT,
                             indicatoron=0, bd=0, relief="flat", overrelief="flat",
                             font=(MONO, 8), padx=6, pady=0, cursor="hand2",
                             takefocus=0, highlightthickness=0)
        pin.pack(side="right", padx=(0, 8))

        meta = tk.Label(body, text="", bg=BG_POPUP, fg=COMMENT, font=(MONO, 9),
                        anchor="w", justify="left")
        meta.pack(fill="x", pady=(6, 8))

        ring = tk.Frame(body, bg=BORDER)
        ring.pack(fill="both", expand=True)

        text = tk.Text(ring, bg=BG, fg=FG, insertbackground=BLUE,
                       selectbackground=BG_SELECT, selectforeground=FG,
                       relief="flat", bd=0, highlightthickness=0,
                       padx=12, pady=10, wrap="word", font=(MONO, 11),
                       spacing1=2, spacing3=2, undo=True, maxundo=100,
                       height=6)
        # Черновик записи — акцентным цветом: видно, что это ещё не факт.
        text.tag_configure("draft", foreground=YELLOW)
        bar = tk.Scrollbar(ring, command=text.yview, width=10, bd=0,
                           bg=BG_SOFT, troughcolor=BG_POPUP,
                           activebackground=COMMENT, highlightthickness=0)
        text.configure(yscrollcommand=bar.set)
        text.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        bar.pack(side="right", fill="y", padx=(0, 1), pady=1)

        # Тонкая полоса прогресса: показывается только пока идёт работа.
        progress = tk.Canvas(body, height=3, bg=BG_POPUP, bd=0,
                             highlightthickness=0, takefocus=0)
        progress.pack(fill="x", pady=(3, 0))

        foot = tk.Frame(body, bg=BG_POPUP)
        foot.pack(fill="x", pady=(12, 0))
        hint = tk.Label(foot, text="", bg=BG_POPUP, fg=COMMENT, font=(MONO, 9), anchor="w")
        hint.pack(fill="x", pady=(0, 8))

        btns = tk.Frame(foot, bg=BG_POPUP)
        btns.pack(fill="x")
        btn_cancel = _button(btns, "Отмена", self._on_cancel_clicked, "ghost")
        btn_smart = _button(btns, "Умная формулировка", self._on_smart_clicked, "secondary")
        btn_send = _button(btns, "Копировать в буфер", self._on_copy_clicked, "primary")
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
        ed.protocol("WM_DELETE_WINDOW", self._on_cancel_clicked)

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
        self._set_live_mode(False)

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
                    self._open(item[1], item[2], item[3], item[4])
                elif kind == "live":
                    if item[1] == self._sid:
                        self._apply_live(item[2], item[3], item[4])
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
                elif kind == "main":
                    if item[1] == "toggle":
                        self._main_toggle()
                    elif item[1] == "show":
                        self._open_main()
                    else:
                        self._close_main()
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

    def _open(self, sid, text, meta, live=False):
        self._sid = sid
        self._source = text
        self._formulated = False
        self._live = bool(live)
        self._hide_status()
        self._set_live_mode(self._live)
        self._set_text(text)
        self._meta.configure(text=meta, fg=COMMENT)
        self._set_busy(False)
        self._place()
        ed = self._editor
        ed.deiconify()
        ed.update_idletasks()
        ed.update()
        ed.lift()
        ed.attributes("-topmost", bool(self._pinned))
        if self._pinned:
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
        """Окно подстраивается под текст: растёт по высоте (до 82% экрана),
        дальше — скроллбар. Позицию и ширину, заданные человеком, не трогаем."""
        self._place_job = None
        ed = self._editor
        ed.update_idletasks()
        try:
            lines = self._text.count("displaylines", "1.0", "end-1c")[0]
        except Exception:
            lines = 6
        self._text.configure(height=max(6, min(lines + 2, 60)))
        ed.update_idletasks()
        h = min(max(ed.winfo_reqheight(), MIN_HEIGHT), int(ed.winfo_screenheight() * 0.82))
        if not self._placed_once:
            sw, sh = ed.winfo_screenwidth(), ed.winfo_screenheight()
            x = (sw - WIN_WIDTH) // 2
            y = int(sh * 0.22)
            w = WIN_WIDTH
            self._placed_once = True
        else:
            # Человек мог сам изменить размер/позицию — уважаем его ширину.
            w, x, y = max(ed.winfo_width(), 320), ed.winfo_x(), ed.winfo_y()
        ed.geometry(f"{w}x{h}+{x}+{y}")
        ed.update_idletasks()
        self._meta.configure(wraplength=max(160, ed.winfo_width() - 40))

    def _schedule_place(self, delay=250):
        if self._place_job:
            try:
                self._root.after_cancel(self._place_job)
            except Exception:
                pass
        self._place_job = self._root.after(delay, self._place)

    def _set_text(self, text):
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", text)
        self._text.edit_reset()

    def _current_text(self):
        return self._text.get("1.0", "end-1c").strip()

    def _on_modified(self, event=None):
        """Поле изменилось: пересчитать состояние формулировки и рост окна."""
        try:
            if not self._text.edit_modified():
                return
            self._text.edit_modified(False)
        except Exception:
            return
        self._update_smart_state()
        self._schedule_place()

    # ── живой режим ────────────────────────────────────────────────────────

    def _set_live_mode(self, live):
        self._live = live
        # Клавиши единые в обоих режимах:
        #   Backspace                     — один символ
        #   Ctrl/Alt+Backspace            — слово
        #   Ctrl/Alt+Shift+Backspace      — фразу (до точки/пред. знака или
        #                                   до предыдущего переноса строки)
        #   Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y — отмена / повтор (история 100)
        #   Ctrl+C / Ctrl+Shift+C         — выделенное / весь текст
        # В живом режиме те же клавиши дополнительно режут и аудио-буфер,
        # поэтому удалённое не «воскресает» на следующем проходе ASR.
        for spec in ("<BackSpace>", "<Control-BackSpace>", "<Alt-BackSpace>",
                     "<Control-Shift-BackSpace>", "<Alt-Shift-BackSpace>"):
            self._text.unbind(spec)
        if live:
            self._btn_send.configure(text="Стоп", state="normal")
            self._btn_smart.configure(text="Умная формулировка", state="disabled")
            self._hint.configure(
                text=("Во время записи: Backspace — символ · Ctrl+Backspace — слово · "
                      "Ctrl+Shift+Backspace — фраза · Esc — отмена"))
            # Локальные биндинги — страховка: обычно клавиши перехватывают
            # глобальные хоткеи, но если регистрация не прошла, сработают они.
            self._text.bind("<BackSpace>", lambda e: self._on_live_delete("char", e))
            self._text.bind("<Control-BackSpace>", lambda e: self._on_live_delete("word", e))
            self._text.bind("<Alt-BackSpace>", lambda e: self._on_live_delete("word", e))
            self._text.bind("<Control-Shift-BackSpace>", lambda e: self._on_live_delete("phrase", e))
            self._text.bind("<Alt-Shift-BackSpace>", lambda e: self._on_live_delete("phrase", e))
        else:
            self._btn_send.configure(text="Копировать в буфер")
            self._text.bind("<Control-BackSpace>", self._delete_word_back)
            self._text.bind("<Alt-BackSpace>", self._delete_word_back)
            self._text.bind("<Control-Shift-BackSpace>", self._delete_phrase_back)
            self._text.bind("<Alt-Shift-BackSpace>", self._delete_phrase_back)
            self._update_smart_state()
        # Общий для обоих режимов: отмена/повтор и копирование.
        # В Tk <Control-z> и <Control-Z> — одна и та же клавиша: shift
        # различается только явным модификатором, поэтому redo — только
        # через <Control-Shift-Z> / <Control-y>.
        self._text.bind("<Control-z>", lambda e: self._undo_redo("undo"))
        self._text.bind("<Control-Shift-Z>", lambda e: self._undo_redo("redo"))
        self._text.bind("<Control-Shift-z>", lambda e: self._undo_redo("redo"))
        self._text.bind("<Control-y>", lambda e: self._undo_redo("redo"))
        self._text.bind("<Control-c>", self._copy_selection)
        self._text.bind("<Control-Shift-c>", self._copy_all)

    def _on_live_delete(self, kind, _event):
        """Удаление в живом режиме: текст и аудио-буфер. kind: char/word/phrase."""
        if self.on_live_delete:
            self.on_live_delete(kind)
        return "break"

    def _undo_redo(self, which):
        """'undo' | 'redo' — прямыми вызовами Text.edit_undo/redo."""
        if str(self._text["state"]) == "disabled":
            return "break"
        try:
            if which == "undo":
                self._text.edit_undo()
            else:
                self._text.edit_redo()
        except Exception:
            pass
        return "break"

    def _delete_word_back(self, _event=None):
        """Ctrl/Alt+Backspace: удалить предыдущее слово целиком.
        Курсор внутри слова — удаляет всё слово. Точка/запятая после слова
        уходят вместе с ним: «…здесь.» + Ctrl+BS -> «…» (иначе пришлось бы
        жать два раза)."""
        if str(self._text["state"]) == "disabled":
            return "break"
        try:
            line_no, col = (int(x) for x in self._text.index("insert").split("."))
        except Exception:
            return "break"
        line = self._text.get(f"{line_no}.0", f"{line_no}.end")
        i = col
        if 0 < i < len(line) and line[i - 1].isalnum() and line[i].isalnum():
            # Курсор внутри слова — всё слово целиком.
            s = i
            while s > 0 and line[s - 1].isalnum():
                s -= 1
            e = i
            while e < len(line) and line[e].isalnum():
                e += 1
        else:
            # Влево: пробелы, затем пунктуация, затем само слово.
            j = i
            while j > 0 and line[j - 1] == " ":
                j -= 1
            p = j
            while p > 0 and not line[p - 1].isalnum():
                p -= 1
            s = p
            while s > 0 and line[s - 1].isalnum():
                s -= 1
            e = i
        if e > s:
            self._text.delete(f"{line_no}.{s}", f"{line_no}.{e}")
        return "break"

    def _cursor_offset(self):
        """Позиция курсора в символах от начала текста (или None)."""
        text = self._text.get("1.0", "end-1c")
        try:
            line_no, col = (int(x) for x in self._text.index("insert").split("."))
        except Exception:
            return None
        lines = text.split("\n")
        if line_no - 1 >= len(lines):
            return None
        offset = col
        for i in range(line_no - 1):
            offset += len(lines[i]) + 1
        return offset

    def _delete_phrase_back(self, _event=None):
        """Ctrl/Alt+Shift+Backspace: удалить последнюю фразу — до последней
        точки/восклицания/вопроса/многоточия (с пробелом или в конце текста),
        иначе — до предыдущего переноса строки, иначе — до начала текста.
        Курсор сразу после точки — стирает всю предшествующую фразу целиком."""
        if str(self._text["state"]) == "disabled":
            return "break"
        offset = self._cursor_offset()
        if not offset:
            return "break"
        text = self._text.get("1.0", "end-1c")
        seg = text[:offset]
        ms = list(re.finditer(r"[.!?…]+(?:\s+|$)|\n+", seg))
        if ms and ms[-1].end() == len(seg):
            # Точка/перенос ровно под курсором: фраза закончилась,
            # стираем предшествующую (а не «ничего»).
            start = ms[-2].end() if len(ms) > 1 else 0
        else:
            start = ms[-1].end() if ms else 0
        if start >= offset:
            return "break"
        self._text.delete(self._char_to_index(start), "insert")
        return "break"

    def _char_to_index(self, start_char):
        """Смещение символа в тексте -> Tk-индекс (строка.столбец)."""
        text = self._text.get("1.0", "end-1c")
        pos = 0
        for i, ln in enumerate(text.split("\n")):
            if pos <= start_char <= pos + len(ln):
                return f"{i + 1}.{start_char - pos}"
            pos += len(ln) + 1
        return "end-1c"

    def _copy_selection(self, _event):
        if str(self._text["state"]) == "disabled":
            return "break"
        try:
            sel = self._text.get("sel.first", "sel.last")
        except Exception:
            return "break"
        if sel.strip():
            self._do_copy(sel)
        return "break"

    def _copy_all(self, _event):
        text = self._current_text()
        if text:
            self._do_copy(text)
        return "break"

    def _do_copy(self, text):
        try:
            self._editor.clipboard_clear()
            self._editor.clipboard_append(text)
            self._meta.configure(text="Скопировано в буфер", fg=GREEN)
        except Exception:
            pass

    def _apply_live(self, confirmed, draft, meta):
        if not self._live:
            return
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("end-1c", confirmed)
        if draft:
            self._text.insert("end-1c", (" " if confirmed else "") + draft, ("draft",))
        self._text.see("end")
        # Живой текст принадлежит ASR: история отмены по нему бессмысленна.
        self._text.edit_reset()
        self._meta.configure(text=meta, fg=COMMENT)
        self._schedule_place()

    def _update_smart_state(self):
        """Кнопка формулировки доступна только на достаточно длинном тексте."""
        if self._live:
            return
        if self._busy:
            return
        try:
            if self._formulated:
                self._btn_smart.configure(text="Вернуть исходный", state="normal")
                self._hint.configure(
                    text=("Ctrl+Enter — в буфер · F2 — вернуть исходный · Esc — закрыть · "
                          "Ctrl+BS — слово · Ctrl+Shift+BS — фраза"))
                return
            n = len(self._current_text())
            need = self.min_smart_chars
            if need and n < need:
                self._btn_smart.configure(text="Умная формулировка", state="disabled")
                self._hint.configure(
                    text=(f"Короткий текст ({n} из {need} символов) — формулировка "
                          "не нужна, копируйте как есть"))
            else:
                self._btn_smart.configure(text="Умная формулировка", state="normal")
                self._hint.configure(
                    text=("Ctrl+Enter — в буфер · F2 — умная формулировка · Esc — закрыть · "
                          "Ctrl+BS — слово · Ctrl+Shift+BS — фраза"))
        except Exception:
            pass

    def _set_busy(self, busy, label="Формулирую"):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self._text.configure(state=state)
        if not self._live:
            self._btn_send.configure(state=state)
        if busy:
            # Никаких «часиков»: окно живое, оно просто ждёт результат.
            # Отмена остаётся доступной — это главный признак, что всё работает.
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

    def _toggle_pin(self):
        """Галочка «поверх всех» в шапке окна правки."""
        self._pinned = bool(self._pin_var.get())
        try:
            self._editor.attributes("-topmost", self._pinned)
        except Exception:
            pass
        if self._pinned:
            try:
                self._editor.lift()
            except Exception:
                pass

    def _start_progress(self):
        """Бегущая полоска: 60 кадров в секунду, прямоугольник двигаем, а не
        пересоздаём. Позиция считается от времени старта — даже если Tk
        пропустит кадр, полоска не «дёрнется» назад."""
        self._stop_progress()
        canvas = self._progress
        canvas.configure(bg=BG_SOFT)
        self._progress_on = True
        span = 140
        t0 = time.time()
        canvas.delete("bar")
        canvas.create_rectangle(-span, 0, 0, 3, fill=BLUE, width=0, tags="bar")

        def step():
            if not self._progress_on:
                return
            width = max(canvas.winfo_width(), 1)
            elapsed = time.time() - t0
            pos = (elapsed * 300) % (width + span) - span
            try:
                canvas.coords("bar", pos, 0, pos + span, 3)
            except Exception:
                return
            self._progress_job = canvas.after(16, step)

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
        # Финальный текст после записи: окно выходит из живого режима.
        self._live = False
        self._set_live_mode(False)
        self._set_busy(False)
        self._set_text(text)
        self._source = text
        self._formulated = False
        self._update_smart_state()
        self._meta.configure(text=meta, fg=GREEN)
        self._place()
        self._focus_if_active()
        self._text.mark_set("insert", "end-1c")
        self._text.see("end")

    def _apply_error(self, message):
        self._set_busy(False)
        self._meta.configure(text=message, fg=ORANGE)
        self._place()
        self._focus_if_active()

    def _focus_if_active(self):
        """Вернуть фокус в поле, только если окно и так было активным.

        Пока шла работа, человек мог уйти в другое окно; выдёргивать фокус
        из-под него нельзя.
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

    def _on_send_event(self, _event):
        if self._live:
            self._on_stop_clicked()
        else:
            self._on_copy_clicked()
        return "break"

    def _on_smart_event(self, _event):
        self._on_smart_clicked()
        return "break"

    def _on_stop_clicked(self):
        """Остановить запись: идёт финальное распознавание, окно ждёт."""
        if not self._live or self._busy:
            return
        self._set_busy(True, "Распознаю запись")
        if self.on_stop:
            self.on_stop(self._sid)

    def _on_copy_clicked(self):
        text = self._current_text()
        if not text:
            self._meta.configure(text="Копировать нечего — текст пустой", fg=ORANGE)
            return
        if self.on_copy:
            self.on_copy(self._sid, text)
        # Окно остаётся открытым: можно поправить текст и скопировать ещё раз.

    def _on_smart_clicked(self):
        if self._busy:
            # Кнопка во время ожидания — это «Отменить», а не «ещё раз».
            sid = self._sid
            self._set_busy(False)
            self._update_smart_state()
            self._meta.configure(text="Операция отменена", fg=COMMENT)
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
                      "формулировать нечего, копируйте как есть"), fg=COMMENT)
            return
        self._source = text
        sid = self._sid
        self._set_busy(True, "Формулирую")
        if self.on_formulate:
            self.on_formulate(sid, text)

    def _on_cancel_clicked(self):
        if self._busy:
            self._set_busy(False)
        sid = self._sid
        text = self._current_text()
        self._hide_editor()
        if self.on_close:
            # Приложение само решит: во время записи это отмена сессии,
            # в режиме правки — просто закрытие (с автосохранением правок).
            self.on_close(sid, text)

    # ── главное окно: история + лог ────────────────────────────────────────

    def _build_main(self, tk):
        win = tk.Toplevel(self._root)
        self._main = win
        win.withdraw()
        win.title("Диктовщик")
        win.configure(bg=BG)
        try:
            if APP_ICON.exists():
                win.iconbitmap(str(APP_ICON))
            _dark_titlebar(win)
        except Exception:
            pass
        win.minsize(880, 500)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{MAIN_W}x{MAIN_H}+{(sw - MAIN_W) // 2}+{(sh - MAIN_H) // 2}")
        win.protocol("WM_DELETE_WINDOW", self._close_main)

        tabs = tk.Frame(win, bg=BG)
        tabs.pack(fill="x", padx=14, pady=(10, 6))
        tab_records = tk.Label(tabs, text="  Записи  ", bg=BG, fg=FG,
                               font=(MONO, 10, "bold"), cursor="hand2")
        tab_log = tk.Label(tabs, text="  Лог  ", bg=BG, fg=COMMENT,
                           font=(MONO, 10, "bold"), cursor="hand2")
        tab_records.pack(side="left")
        tab_log.pack(side="left", padx=(4, 0))
        tab_records.bind("<Button-1>", lambda e: self._main_switch("records"))
        tab_log.bind("<Button-1>", lambda e: self._main_switch("log"))
        self._tab_records_btn = tab_records
        self._tab_log_btn = tab_log

        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        # ── вкладка «Записи» ──
        rec = tk.Frame(body, bg=BG)
        rec.pack(fill="both", expand=True)

        listbox = tk.Frame(rec, bg=BG_SOFT, padx=2, pady=2, width=310)
        listbox.pack(side="left", fill="y")
        tk.Label(listbox, text=" время   запись   аср   символы", bg=BG_SOFT,
                 fg=COMMENT, font=(MONO, 8), anchor="w").pack(fill="x", padx=8, pady=(6, 2))
        canvas = tk.Canvas(listbox, bg=BG_SOFT, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(listbox, command=canvas.yview, width=10, bd=0,
                           bg=BG_SOFT, troughcolor=BG_POPUP,
                           activebackground=COMMENT, highlightthickness=0)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        inner = tk.Frame(canvas, bg=BG_SOFT)
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="rows")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure("rows", width=e.width))
        canvas.bind("<MouseWheel>", self._main_list_wheel)
        self._list_canvas = canvas
        self._list_inner = inner

        detail = tk.Frame(rec, bg=BG)
        detail.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self._dmeta = tk.Label(detail, text="Выберите запись из списка", bg=BG, fg=COMMENT,
                               font=(MONO, 9), anchor="w", justify="left")
        self._dmeta.pack(fill="x", pady=(4, 6))
        act = tk.Frame(detail, bg=BG)
        act.pack(fill="x", pady=(0, 4))
        tk.Label(act, text="Правки в правом тексте сохраняются автоматически",
                 bg=BG, fg=COMMENT, font=(MONO, 8), anchor="e").pack(side="right", padx=(0, 10))
        self._main_copy_btn = _button(act, "Копировать", self._main_copy, "primary")
        self._main_copy_btn.pack(side="right")
        panes = tk.Frame(detail, bg=BG)
        panes.pack(fill="both", expand=True)
        left = tk.Frame(panes, bg=BORDER)
        right = tk.Frame(panes, bg=BORDER)
        left.pack(side="left", fill="both", expand=True)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        tk.Label(left, text="Распознанный", bg=BG, fg=COMMENT,
                 font=(MONO, 8), anchor="w").pack(fill="x", padx=9, pady=(5, 0))
        tk.Label(right, text="Отредактированный", bg=BG, fg=COMMENT,
                 font=(MONO, 8), anchor="w").pack(fill="x", padx=9, pady=(5, 0))
        self._raw_text = self._make_pane(left, editable=False)
        self._edit_text = self._make_pane(right, editable=True)
        self._edit_text.bind("<<Modified>>", self._main_edit_modified)
        self._tab_records = rec

        # ── вкладка «Лог» ──
        logf = tk.Frame(body, bg=BG)
        self._log_text = self._make_pane(logf, editable=False, mono=9, wrap="none")
        self._tab_log = logf

    def _make_pane(self, parent, editable=True, mono=10, wrap="word"):
        import tkinter as tk

        text = tk.Text(parent, bg=BG, fg=FG_DARK, insertbackground=BLUE,
                       relief="flat", bd=0, highlightthickness=0,
                       padx=10, pady=8, wrap=wrap, font=(MONO, mono),
                       state="normal" if editable else "disabled")
        bar = tk.Scrollbar(parent, command=text.yview, width=10, bd=0,
                           bg=BG_SOFT, troughcolor=BG_POPUP,
                           activebackground=COMMENT, highlightthickness=0)
        if wrap == "none":
            bar2 = tk.Scrollbar(parent, orient="horizontal", command=text.xview,
                                width=10, bd=0, bg=BG_SOFT, troughcolor=BG_POPUP,
                                activebackground=COMMENT, highlightthickness=0)
            text.configure(xscrollcommand=bar2.set)
            bar2.pack(side="bottom", fill="x", padx=1, pady=(1, 1))
        text.configure(yscrollcommand=bar.set)
        text.pack(side="left", fill="both", expand=True, padx=(1, 1), pady=(1, 1))
        bar.pack(side="right", fill="y", padx=(0, 1), pady=(1, 1))
        return text

    def _open_main(self):
        win = self._main
        win.deiconify()
        win.lift()
        win.focus_force()
        self._main_shown = True
        self._main_refresh()

    def _close_main(self):
        try:
            self._main.withdraw()
        except Exception:
            pass
        self._main_shown = False

    def _main_toggle(self):
        try:
            visible = self._main.winfo_viewable()
        except Exception:
            visible = False
        if visible and self._main_shown:
            self._close_main()
        else:
            self._open_main()

    def _main_switch(self, tab):
        self._main_tab = tab
        self._tab_records_btn.configure(fg=FG if tab == "records" else COMMENT)
        self._tab_log_btn.configure(fg=FG if tab == "log" else COMMENT)
        if tab == "records":
            self._tab_log.pack_forget()
            self._tab_records.pack(fill="both", expand=True)
            self._main_render_list()
            self._main_render_detail()
        else:
            self._tab_records.pack_forget()
            self._tab_log.pack(fill="both", expand=True)
            self._main_render_log()

    def _main_refresh(self):
        try:
            self._root.after(2500, self._main_refresh)
        except Exception:
            return
        if not self._main_shown:
            return
        if self.history_fn is not None:
            try:
                rows = self.history_fn() or []
            except Exception:
                rows = []
            prev_ts = self._history[self._sel].get("ts") if 0 <= self._sel < len(self._history) else None
            self._history = rows
            self._sel = next((i for i, r in enumerate(rows) if r.get("ts") == prev_ts), 0)
        if self._main_tab == "records":
            self._main_render_list()
            self._main_render_detail()
        else:
            self._main_render_log()

    def _main_list_wheel(self, event):
        if not self._list_canvas.winfo_manager():
            return
        try:
            self._list_canvas.yview_scroll(-1 * (event.delta // 120), "units")
        except Exception:
            pass

    def _main_render_list(self):
        import tkinter as tk
        inner = self._list_inner
        for ch in inner.winfo_children():
            ch.destroy()
        if not self._history:
            tk.Label(inner, text="Записей пока нет", bg=BG_SOFT, fg=COMMENT,
                     font=(MONO, 9)).pack(padx=10, pady=16)
            return
        for i, row in enumerate(self._history[:60]):
            self._main_make_row(i, row)

    def _main_make_row(self, i, row):
        import tkinter as tk
        action = row.get("action", "")
        color = {"copy": GREEN, "send": GREEN, "cancel": RED, "quick": CYAN}.get(action, COMMENT)
        edited = bool(row.get("final")) and row.get("final") != row.get("raw")
        ts = row.get("ts", "")
        time_s = ts[11:16] if len(ts) >= 16 else ts
        mark = "  ✎" if edited else ""
        txt = (f"{time_s}  {_fmt_dur(row.get('sec'))}  "
               f"{_fmt_dur((row.get('asr_ms') or 0) / 1000)}  "
               f"{row.get('chars') or len(row.get('raw') or '')}{mark}")
        selected = (i == self._sel)
        row_bg = BG_SELECT if selected else BG_SOFT
        fg = FG if selected else FG_DARK
        frame = tk.Frame(self._list_inner, bg=row_bg, cursor="hand2")
        frame.pack(fill="x", padx=4, pady=1)
        dot = tk.Label(frame, text="●", bg=row_bg, fg=color, font=(MONO, 9), width=2)
        dot.pack(side="left")
        lbl = tk.Label(frame, text=txt, bg=row_bg, fg=fg, font=(MONO, 9), anchor="w")
        lbl.pack(side="left", fill="x", expand=True, padx=(2, 4), pady=3)

        def click(_e, i=i):
            self._sel = i
            self._main_render_list()
            self._main_render_detail()

        def enter(_e):
            if i != self._sel:
                frame.configure(bg=BG_POPUP)
                dot.configure(bg=BG_POPUP)
                lbl.configure(bg=BG_POPUP, fg=FG)

        def leave(_e):
            if i != self._sel:
                frame.configure(bg=BG_SOFT)
                dot.configure(bg=BG_SOFT)
                lbl.configure(bg=BG_SOFT, fg=FG_DARK)

        for w in (frame, dot, lbl):
            w.bind("<Button-1>", click)
            w.bind("<Enter>", enter)
            w.bind("<Leave>", leave)

    def _main_render_detail(self):
        entry = self._history[self._sel] if 0 <= self._sel < len(self._history) else None
        if not entry:
            self._dmeta.configure(text="Выберите запись из списка", fg=COMMENT)
            self._set_pane(self._raw_text, "")
            self._set_pane(self._edit_text, "")
            self._detail_ts = None
            return
        # Те же запись и те же правки — не пересоздавать поля: пользователь
        # может прямо сейчас печатать в правом тексте.
        if entry.get("ts") == self._detail_ts:
            return
        self._detail_ts = entry.get("ts")
        raw = entry.get("raw") or ""
        final = entry.get("final") or ""
        sec = entry.get("sec") or 0
        asr_ms = entry.get("asr_ms") or 0
        action = entry.get("action", "")
        action_s = {"copy": "скопировано", "send": "отправлено",
                    "cancel": "отменено", "quick": "быстрый ввод"}.get(action, action)
        self._dmeta.configure(
            text=(f"{entry.get('ts', '')} · {action_s} · запись {_fmt_dur(sec)} · "
                  f"распознавание {_fmt_dur(asr_ms / 1000)} · {len(raw)} символов"),
            fg=COMMENT)
        self._set_pane(self._raw_text, raw)
        if self._save_job:
            try:
                self._root.after_cancel(self._save_job)
            except Exception:
                pass
            self._save_job = None
        self._set_pane(self._edit_text, final if final else raw)

    def _set_pane(self, widget, text):
        editable = str(widget["state"]) == "normal"
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if text:
            widget.insert("1.0", text)
        widget.edit_reset()
        if not editable:
            widget.configure(state="disabled")

    def _main_edit_modified(self, _event=None):
        try:
            self._edit_text.edit_modified(False)
        except Exception:
            return
        if self._save_job:
            try:
                self._root.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self._root.after(1200, self._main_save_edit)

    def _main_save_edit(self):
        self._save_job = None
        if not self.save_edit_fn or not (0 <= self._sel < len(self._history)):
            return
        entry = self._history[self._sel]
        text = self._edit_text.get("1.0", "end-1c").strip()
        raw = (entry.get("raw") or "").strip()
        text = "" if text == raw else text
        if text == (entry.get("final") or ""):
            return
        try:
            self.save_edit_fn(entry.get("ts", ""), text)
        except Exception:
            pass

    def _main_copy(self):
        text = self._edit_text.get("1.0", "end-1c").strip()
        if not text:
            return
        try:
            self._main.clipboard_clear()
            self._main.clipboard_append(text)
        except Exception:
            return
        if 0 <= self._sel < len(self._history):
            self._dmeta.configure(text="Скопировано в буфер обмена", fg=GREEN)

    def _main_render_log(self):
        if not self.log_fn:
            return
        try:
            at_bottom = self._log_text.yview()[1] >= 0.999
        except Exception:
            at_bottom = True
        try:
            lines = self.log_fn() or ""
        except Exception:
            lines = ""
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        if lines:
            self._log_text.insert("1.0", lines)
        if at_bottom:
            self._log_text.see("end")


# ── мелкие помощники ────────────────────────────────────────────────────────


def _button(parent, text, command, kind):
    import tkinter as tk

    btn = tk.Button(parent, text=text, command=command, font=(MONO, 10),
                    bd=0, relief="flat", overrelief="flat", padx=12, pady=6,
                    cursor="hand2", takefocus=0, highlightthickness=1,
                    activeforeground=FG, disabledforeground=COMMENT)
    if kind == "primary":
        # Текст всегда тёмным: светлый текст на голубом на некоторых мониторах
        # почти сливался — это была та самая «кнопка без надписи».
        bg, fg, border, hover_bg = BLUE, BG_ALT, BLUE, BLUE_HOVER
    elif kind == "secondary":
        bg, fg, border, hover_bg = BG_SOFT, FG, BORDER, BG_SELECT
    else:  # ghost
        bg, fg, border, hover_bg = BG_POPUP, FG_DARK, BG_POPUP, BG_SOFT
    btn.configure(bg=bg, fg=fg, activebackground=hover_bg,
                  highlightbackground=border, highlightcolor=border)
    _hover(btn, bg, hover_bg, fg, fg, border, BLUE if kind != "ghost" else BG_SOFT)
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


def _dark_titlebar(window):
    """Тёмная системная панель заголовка (Windows 10 2004+ / 11)."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                              ctypes.c_void_p, wintypes.DWORD]
        flag = ctypes.c_int(1)  # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        dwm.DwmSetWindowAttribute(_hwnd(window), 20, ctypes.byref(flag), ctypes.sizeof(flag))
    except Exception:
        pass


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
