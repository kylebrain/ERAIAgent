#Requires AutoHotkey v2.0
SendMode "Input"

; Global mic hotkey for the Elden Ring AI Guide.
;
; Press F8 anywhere (e.g. while playing the game) to toggle voice recording in
; the guide. F8 is forwarded to the page as F9, so set the in-page "Mic keybind"
; (gear menu) to F9 to match.
;
; Why two different keys: AHK installs a keyboard hook, so if the global hotkey
; and the forwarded key were identical, AHK's own Send would re-trigger the
; hotkey and loop forever. F8 (global) -> F9 (page) avoids that.
;
; Chrome only processes a keystroke when its window is focused, so we can't
; deliver F9 to a background tab. Instead we remember the current foreground
; window (the game), flick focus to Chrome just long enough to send F9, then
; hand focus straight back to the game. Run Elden Ring in Borderless /
; "Fullscreen" (not exclusive fullscreen) so the swap is a brief flicker rather
; than a display-mode switch. The guide must be the foreground TAB in its Chrome
; window: Chrome's window title reflects only the active tab.

*F8:: {
    guide := "Elden Ring AI Guide ahk_exe chrome.exe"
    if !WinExist(guide)
        return
    prev := WinExist("A")            ; remember the foreground window (the game)
    WinActivate guide
    if WinWaitActive(guide, , 1) {
        Send "{F9}"
        Sleep 30                     ; let Chrome process the key before it loses focus
    }
    if prev
        WinActivate "ahk_id " prev   ; hand focus straight back to the game
}
