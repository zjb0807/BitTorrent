Outfile bittorrent.exe
Name BitTorrent
SilentInstall silent
InstallDir "$PROGRAMFILES\BitTorrent\"
Section "Install"
  SetOutPath $INSTDIR
  File _StreamEncrypter.pyd
  File _socket.pyd
  File _sre.pyd
  File _tkinter.pyd
  File _winreg.pyd
  File python21.dll
  File PyWinTypes21.dll
  File select.pyd
  File tcl83.dll
  File tk83.dll
  File windownload.exe
  File /r tcl
  WriteRegStr HKCR .torrent "" torrent_auto_file
  WriteRegStr HKCR "MIME\Database\Content Type\bittorrent/redirect" Extension .torrent
  WriteRegStr HKCR torrent_auto_file "" "TORRENT File"
  WriteRegBin HKCR torrent_auto_file EditFlags 00000100
  WriteRegStr HKCR "torrent_auto_file\shell\open\command" "" '$INSTDIR\windownload.exe "%1"'
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BitTorrent" "DisplayName" "BitTorrent 2.3.2"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BitTorrent" "UninstallString" '"$INSTDIR\uninstall.exe"'
  MessageBox MB_OK "Hyperlinks in Internet Explorer which use BitTorrent will now work!"
SectionEnd

UninstallExeName "uninstall.exe"

Section "Uninstall"
  DeleteRegKey HKCR .torrent
  DeleteRegKey HKCR "MIME\Database\Content Type\bittorrent/redirect"
  DeleteRegKey HKCR torrent_auto_file
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BitTorrent"
  RMDir /r "$INSTDIR"
SectionEnd