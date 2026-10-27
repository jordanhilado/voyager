"""A small Win32 window with standard controls, for exercising the Windows backend without
touching a real app: a name box, a password box, a check box, a Greet button, a 40-item list and
a color drop-down. The status line at the top reports what happened ("Status: hello Ada").

Usage: python scripts/win_fixture.py   (close the window or Ctrl+C to quit)
"""
from __future__ import annotations

import win32api
import win32con
import win32gui

TITLE = "Voyager Test Form"
ID_STATUS, ID_NAME, ID_PW, ID_CHECK, ID_GREET, ID_LIST, ID_COLOR = range(100, 107)
ES_PASSWORD = 0x20
LBS_NOTIFY = 0x1
CBS_DROPDOWNLIST = 0x3


def main() -> None:
    hinst = win32api.GetModuleHandle(None)
    controls: dict[int, int] = {}

    def text(cid: int) -> str:
        return win32gui.GetWindowText(controls[cid])

    def status(s: str) -> None:
        win32gui.SetWindowText(controls[ID_STATUS], "Status: " + s)

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == win32con.WM_COMMAND:
            cid, code = win32api.LOWORD(wparam), win32api.HIWORD(wparam)
            if cid == ID_GREET:
                status("hello " + text(ID_NAME))
            elif cid == ID_LIST and code == win32con.LBN_SELCHANGE:
                lb = controls[ID_LIST]
                i = win32gui.SendMessage(lb, win32con.LB_GETCURSEL, 0, 0)
                status(f"picked Fruit item {i + 1}")
            elif cid == ID_CHECK and code == win32con.BN_CLICKED:
                on = win32gui.SendMessage(controls[ID_CHECK], win32con.BM_GETCHECK, 0, 0)
                status("subscribed" if on else "unsubscribed")
            elif cid == ID_COLOR and code == win32con.CBN_SELCHANGE:
                status("color " + text(ID_COLOR))
            return 0
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    wc = win32gui.WNDCLASS()
    wc.lpszClassName = "VoyagerTestForm"
    wc.lpfnWndProc = wndproc
    wc.hInstance = hinst
    wc.hbrBackground = win32con.COLOR_WINDOW + 1
    wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
    win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindow("VoyagerTestForm", TITLE, win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE,
                                 100, 100, 540, 480, 0, 0, hinst, None)

    def child(cls: str, label: str, cid: int, x: int, y: int, w: int, h: int, style: int = 0) -> int:
        c = win32gui.CreateWindow(cls, label, win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_TABSTOP | style,
                                  x, y, w, h, hwnd, cid, hinst, None)
        controls[cid] = c
        return c

    child("STATIC", "Status: idle", ID_STATUS, 10, 10, 480, 20)
    child("STATIC", "Your name", 0, 10, 40, 90, 20)
    child("EDIT", "", ID_NAME, 110, 38, 300, 24, win32con.WS_BORDER | win32con.ES_AUTOHSCROLL)
    child("STATIC", "Password", 0, 10, 72, 90, 20)
    child("EDIT", "", ID_PW, 110, 70, 300, 24, win32con.WS_BORDER | ES_PASSWORD | win32con.ES_AUTOHSCROLL)
    child("BUTTON", "Subscribe to newsletter", ID_CHECK, 10, 102, 300, 24, win32con.BS_AUTOCHECKBOX)
    child("BUTTON", "Greet", ID_GREET, 10, 132, 100, 28, win32con.BS_PUSHBUTTON)
    lb = child("LISTBOX", "Fruits", ID_LIST, 10, 170, 300, 130, win32con.WS_BORDER | win32con.WS_VSCROLL | LBS_NOTIFY)
    for i in range(1, 41):
        win32gui.SendMessage(lb, win32con.LB_ADDSTRING, 0, f"Fruit item {i}")
    cb = child("COMBOBOX", "Color", ID_COLOR, 10, 310, 200, 200, CBS_DROPDOWNLIST | win32con.WS_VSCROLL)
    for c in ("Red", "Green", "Blue"):
        win32gui.SendMessage(cb, win32con.CB_ADDSTRING, 0, c)
    # The first ShowWindow takes the launcher's show flag (hidden when started in the background).
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
    win32gui.PumpMessages()


if __name__ == "__main__":
    main()
