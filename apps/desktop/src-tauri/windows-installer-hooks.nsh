!macro NSIS_HOOK_PREINSTALL
  ; Existing 1.0.x clients start these PyInstaller helpers before invoking the
  ; updater. Stop the complete helper trees before NSIS overwrites their files.
  nsExec::ExecToStack '"$SYSDIR\taskkill.exe" /F /T /IM ai-trader-runtime.exe'
  Pop $0
  Pop $1
  nsExec::ExecToStack '"$SYSDIR\taskkill.exe" /F /T /IM ai-trader-tqkq-sidecar.exe'
  Pop $0
  Pop $1
  Sleep 500
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ; Seed future deltas only for the normal user installation. Disposable smoke
  ; installs and custom directories must not write to the user's app profile.
  ${If} $INSTDIR == "$LOCALAPPDATA\${PRODUCTNAME}"
    CreateDirectory "$LOCALAPPDATA\${BUNDLEID}\updates"
    CopyFiles /SILENT "$EXEPATH" "$LOCALAPPDATA\${BUNDLEID}\updates\installer-seed.bin"
  ${EndIf}
!macroend
