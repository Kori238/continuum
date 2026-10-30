' Continuum autostart launcher.
'
' Post-migration shape: the server lives on the hub, not here. This machine
' starts two things, and they are deliberately separate processes:
'
'   1. the desktop client - headless, reports machine state to the hub. This is
'      the one that must always run: if it stops, the hub silently stops
'      counting activity, and a budget that quietly stops measuring is worse
'      than one that is obviously off.
'   2. the window - a viewer onto the hub. Closing it must NOT stop (1), which
'      is why it is not a child of it.
'
' It used to run "-m continuum app" with no --hub, which stood up a full local
' server with its own database. After the cutover that would have been a second
' diverging Continuum, so the --hub flag below is load-bearing, not cosmetic.
'
' Key detail, kept from the original: pythonw with NO valid stdout crashes on
' the first startup write, which is why a plain "pythonw -m continuum app"
' shortcut silently died at boot. Redirecting stdout/stderr to a log gives it
' real handles, so it survives. Both runs are hidden (window style 0) so no
' console flashes; the pywebview UI window still shows normally.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\kori\continuum"

hub = "https://continuum-hub.skate-wyvern.ts.net"

' The reporter first, so the hub is already hearing from this machine by the
' time anything is on screen. Network may not be up yet at login; the client
' retries its own connection, so this does not need to wait for the tailnet.
sh.Run "cmd /c "".venv\Scripts\pythonw.exe -m continuum desktop --hub wss://continuum-hub.skate-wyvern.ts.net/desktop/ws > _desktop_client.out.log 2> _desktop_client.err.log""", 0, False

' Let the desktop / WebView2 runtime settle after login before we open the
' window. Also gives DNS and the tailnet a moment, since unlike the client the
' window does not retry - it would just render an error page.
WScript.Sleep 10000

' Delete this line if you would rather open Continuum yourself from the
' Start Menu shortcut instead of having it appear at every login.
sh.Run "cmd /c "".venv\Scripts\pythonw.exe -m continuum app --hub " & hub & " > _boot.out.log 2> _boot.err.log""", 0, False
