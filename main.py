"""Screen OCR Typist: capture, edit, and confirm before Windows keyboard input."""
import argparse
import ctypes
import json
import queue
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import scrolledtext
import mss
from PIL import Image
import pytesseract
from pynput import keyboard
from pynput._util.win32 import INPUT, INPUT_union, KEYBDINPUT

def type_unicode(character, target, cancel, ctrl):
    encoded = character.encode("utf-16le")
    units = [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]
    events = (INPUT * (2 * len(units)))(*(
        INPUT(type=INPUT.KEYBOARD, value=INPUT_union(ki=KEYBDINPUT(
            wScan=unit, dwFlags=KEYBDINPUT.UNICODE | flags)))
        for flags in (0, KEYBDINPUT.KEYUP) for unit in units))
    if cancel.is_set() or ctrl:
        raise RuntimeError("Typing cancelled or Ctrl pressed")
    if ctypes.windll.user32.GetForegroundWindow() != target:
        raise RuntimeError("Target lost focus; typing stopped")
    if ctypes.windll.user32.SendInput(len(events), events, ctypes.sizeof(INPUT)) != len(events):
        raise RuntimeError("Unicode input was not fully sent; typing stopped")


def failed_before_input(error):
    cause = error.__context__
    if cause is None:
        return False
    frames = []
    trace = cause.__traceback__
    while trace:
        frames.append(trace.tb_frame.f_code)
        trace = trace.tb_next
    return frames[:3] == [keyboard.Controller.type.__code__, keyboard.Controller.press.__code__,
                           keyboard.Controller._resolve.__code__]


VERSION = "1.0.0"

def enable_dpi_awareness():
    """Keep screen-capture and Tk coordinates in physical pixels on scaled Windows displays."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)

@dataclass
class Settings:
    left: int = 100
    top: int = 100
    width: int = 900
    height: int = 100
    cpm: int = 2000
    language: str = "rus"
    tesseract_cmd: str = ""
    focus_delay: float = 1.0

    def validate(self):
        for name in ("left", "top", "width", "height", "cpm"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        if self.width <= 0 or self.height <= 0 or self.cpm <= 0:
            raise ValueError("width, height and cpm must be positive")
        if not 0.2 <= self.focus_delay <= 10:
            raise ValueError("focus_delay must be between 0.2 and 10 seconds")
        if not self.language:
            raise ValueError("language must not be empty")

def configure_ocr(settings):
    settings.validate()
    command = settings.tesseract_cmd or shutil.which("tesseract")
    if not command:
        candidate = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        command = str(candidate) if candidate.is_file() else None
    if not command:
        raise ValueError("Tesseract not found: set tesseract_cmd in the JSON configuration")
    pytesseract.pytesseract.tesseract_cmd = command
    languages = pytesseract.get_languages(config="")
    missing = set(settings.language.split("+")) - set(languages)
    if missing:
        raise ValueError("Missing Tesseract language data: " + ", ".join(sorted(missing)))
    return str(pytesseract.get_tesseract_version())

def capture_region(settings):
    with mss.mss() as capture:
        bounds = capture.monitors[0]
        if (settings.left < bounds["left"] or settings.top < bounds["top"]
                or settings.left + settings.width > bounds["left"] + bounds["width"]
                or settings.top + settings.height > bounds["top"] + bounds["height"]):
            raise ValueError("Capture region is outside the connected displays")
        shot = capture.grab(dict(left=settings.left, top=settings.top,
                                 width=settings.width, height=settings.height))
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

def recognize(image, settings):
    return pytesseract.image_to_string(image, lang=settings.language,
                                      config="--psm 6", timeout=15).rstrip("\n")

class App:
    def __init__(self, root, settings):
        self.root, self.settings = root, settings
        ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
        ctypes.windll.user32.IsWindow.argtypes = [ctypes.c_void_p]
        ctypes.windll.user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        self.events = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()
        self.ctrl = set()
        self.trigger_down = False
        self.target = None
        self.preview = None
        self.last_text = None
        self.last_result = None
        self.controller = keyboard.Controller()
        self.root.withdraw()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release,
                                          win32_event_filter=self.filter_hotkey)
        self.listener.start()
        self.root.after(40, self.poll)

    def filter_hotkey(self, message, event):
        if event.vkCode == 8:
            if message in (0x100, 0x104) and (self.ctrl or ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000):
                if not self.trigger_down:
                    self.trigger_down = True
                    self.events.put(("capture", ctypes.windll.user32.GetForegroundWindow()))
                # Consume only the hotkey's Backspace, keeping the target text intact.
                self.listener.suppress_event()
            elif message in (0x101, 0x105) and self.trigger_down:
                self.trigger_down = False
                self.listener.suppress_event()
        return True

    def on_press(self, key):
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self.ctrl.add(key)
        elif key == keyboard.Key.esc:
            self.cancel.set()
            self.events.put(("cancel", None))
        elif key == keyboard.Key.backspace and self.ctrl and not self.trigger_down:
            self.trigger_down = True
            self.events.put(("capture", ctypes.windll.user32.GetForegroundWindow()))

    def on_release(self, key):
        self.ctrl.discard(key)
        if key == keyboard.Key.backspace:
            self.trigger_down = False

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "capture" and not self.busy:
                    self.busy = True
                    self.cancel.clear()
                    self.target = value
                    threading.Thread(target=self.capture_worker, daemon=True).start()
                elif kind == "preview":
                    if self.cancel.is_set():
                        self.busy = False
                    else:
                        self.show_preview(value)
                elif kind == "done":
                    self.last_result = value
                    print(value, flush=True)
                    self.busy = False
                elif kind == "error":
                    self.last_result = value
                    print(value, flush=True)
                    self.show_preview("", error=value)
                elif kind == "cancel" and self.preview is not None:
                    self.skip()
        except queue.Empty:
            pass
        self.root.after(40, self.poll)

    def capture_worker(self):
        try:
            # Capture without an overlay: its tint must not contaminate OCR pixels.
            text = recognize(capture_region(self.settings), self.settings)
            if not text.strip():
                self.events.put(("done", "No text found; nothing typed"))
            else:
                self.events.put(("preview", text))
        except Exception as error:
            self.events.put(("error", f"Capture/OCR failed: {error}"))

    def show_preview(self, text, error=None):
        self.preview = tk.Toplevel(self.root)
        self.preview.title("Screen OCR Typist — preview")
        self.preview.geometry("600x300+200+250")
        self.preview.attributes("-topmost", True)
        self.preview.protocol("WM_DELETE_WINDOW", self.skip)
        tk.Label(self.preview, text=error or "Edit the text. Confirm restores the original target window.",
                 wraplength=560).pack(padx=10, pady=8)
        self.editor = scrolledtext.ScrolledText(self.preview, width=65, height=10)
        self.editor.insert("1.0", text)
        self.editor.pack(fill="both", expand=True, padx=10)
        buttons = tk.Frame(self.preview)
        buttons.pack(pady=8)
        self.confirm_button = tk.Button(buttons, text="Confirm", command=self.confirm,
                                       state="disabled" if error else "normal")
        self.confirm_button.pack(side="left", padx=8)
        self.skip_button = tk.Button(buttons, text="Skip", command=self.skip)
        self.skip_button.pack(side="left", padx=8)
        self.last_text = text
        self.preview.update_idletasks()
        self.preview.lift()
        self.editor.focus_force()

    def skip(self):
        self.cancel.set()
        if self.preview is not None:
            self.preview.destroy()
            self.preview = None
        self.busy = False
        self.last_result = "Skipped; nothing typed"

    def confirm(self):
        text = self.editor.get("1.0", "end-1c")
        self.preview.destroy()
        self.preview = None
        threading.Thread(target=self.type_worker, args=(text,), daemon=True).start()

    def type_worker(self, text):
        try:
            user32 = ctypes.windll.user32
            if not self.target or not user32.IsWindow(self.target):
                raise RuntimeError("Original target window is no longer available")
            user32.SetForegroundWindow(self.target)
            if self.cancel.wait(self.settings.focus_delay):
                self.events.put(("done", "Typing cancelled"))
                return
            if self.ctrl:
                raise RuntimeError("Release Ctrl before confirming")
            for character in text:
                if self.cancel.is_set():
                    self.events.put(("done", "Typing cancelled"))
                    return
                if user32.GetForegroundWindow() != self.target:
                    raise RuntimeError("Target lost focus; typing stopped")
                if self.ctrl:
                    raise RuntimeError("Ctrl pressed; typing stopped")
                if character == "\n":
                    self.controller.tap(keyboard.Key.enter)
                elif character == "\t":
                    self.controller.tap(keyboard.Key.tab)
                else:
                    try:
                        self.controller.type(character)
                    except keyboard.Controller.InvalidCharacterException as error:
                        if not failed_before_input(error):
                            raise
                        type_unicode(character, self.target, self.cancel, self.ctrl)
                if self.cancel.wait(60 / self.settings.cpm):
                    self.events.put(("done", "Typing cancelled"))
                    return
            self.events.put(("done", "Text typed"))
        except Exception as error:
            self.events.put(("done", f"Typing stopped: {error}"))

    def close(self):
        self.cancel.set()
        self.listener.stop()
        self.root.destroy()

def main():
    enable_dpi_awareness()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--check", action="store_true", help="Validate Tesseract and configuration")
    parser.add_argument("--image", type=Path, help="Recognize an image without keyboard input")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args()
    try:
        settings = Settings(**json.loads(args.config.read_text(encoding="utf-8-sig"))) if args.config else Settings()
        version = configure_ocr(settings)
        if args.check:
            print(f"Tesseract {version}; language {settings.language}; configuration OK")
        elif args.image:
            with Image.open(args.image) as image:
                print(recognize(image, settings))
        else:
            root = tk.Tk()
            app = App(root, settings)
            print("Ctrl+Backspace: capture. Escape: cancel. Ctrl+C in terminal: quit.", flush=True)
            try:
                root.mainloop()
            except KeyboardInterrupt:
                app.close()
    except Exception as error:
        parser.exit(1, f"Error: {error}\n")

if __name__ == "__main__":
    main()
