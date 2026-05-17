import mss
from pynput.keyboard import Controller as Keyboard
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as Mouse

_mouse = Mouse()
_kb = Keyboard()

_KEYS: dict = {
    "Enter": Key.enter, "Backspace": Key.backspace, "Delete": Key.delete,
    "Tab": Key.tab, "Escape": Key.esc, " ": Key.space,
    "ArrowLeft": Key.left, "ArrowRight": Key.right,
    "ArrowUp": Key.up, "ArrowDown": Key.down,
    "Home": Key.home, "End": Key.end,
    "PageUp": Key.page_up, "PageDown": Key.page_down,
    "Control": Key.ctrl_l, "Shift": Key.shift_l,
    "Alt": Key.alt_l, "Meta": Key.cmd,
    "CapsLock": Key.caps_lock,
    "F1": Key.f1, "F2": Key.f2, "F3": Key.f3, "F4": Key.f4,
    "F5": Key.f5, "F6": Key.f6, "F7": Key.f7, "F8": Key.f8,
    "F9": Key.f9, "F10": Key.f10, "F11": Key.f11, "F12": Key.f12,
}


def _screen_size() -> tuple[int, int]:
    with mss.mss() as sct:
        m = sct.monitors[1]
        return m["width"], m["height"]


def _resolve_key(key_str: str):
    if key_str in _KEYS:
        return _KEYS[key_str]
    if len(key_str) == 1:
        return key_str
    return None


def handle_event(data: dict) -> None:
    t = data.get("type")

    if t == "mousemove":
        w, h = _screen_size()
        _mouse.position = (int(data["x"] * w), int(data["y"] * h))

    elif t == "mousedown":
        btn = Button.right if data.get("button") == 2 else Button.left
        _mouse.press(btn)

    elif t == "mouseup":
        btn = Button.right if data.get("button") == 2 else Button.left
        _mouse.release(btn)

    elif t == "dblclick":
        w, h = _screen_size()
        _mouse.position = (int(data["x"] * w), int(data["y"] * h))
        _mouse.click(Button.left, 2)

    elif t == "scroll":
        _mouse.scroll(data.get("dx", 0), -data.get("dy", 0))

    elif t == "keydown":
        k = _resolve_key(data.get("key", ""))
        if k:
            _kb.press(k)

    elif t == "keyup":
        k = _resolve_key(data.get("key", ""))
        if k:
            _kb.release(k)
