#Requires AutoHotkey v2.0
SendMode "Input"

; Global mic hotkey for the Elden Ring AI Guide.
;
; Press F8 anywhere (e.g. while playing the game fullscreen) to focus the guide
; tab in Chrome and toggle voice recording. F8 is forwarded to the page as F9,
; so set the in-page "Mic keybind" (gear menu) to F9 to match.
;
; Why two different keys: AHK installs a keyboard hook, so if the global hotkey
; and the forwarded key were identical, AHK's own Send would re-trigger the
; hotkey and loop forever. F8 (global) -> F9 (page) avoids that.
;
; The guide must be the foreground tab in its Chrome window: Chrome's window
; title reflects only the active tab, so a backgrounded guide tab won't match.

*F8:: {
    if WinExist("Elden Ring AI Guide ahk_exe chrome.exe") {
        WinActivate
        WinWaitActive("Elden Ring AI Guide ahk_exe chrome.exe", , 1)
        Send "{F9}"
    }
}
