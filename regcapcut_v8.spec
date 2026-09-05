# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['modules\\ui\\reg_capcut_tab.py'],
    pathex=['.'],
    binaries=[],
    datas=[('core\\settings.json', 'core'), ('cred.json', '.')],
    hiddenimports=[
        'modules.actions.capcut_workflow',
        'modules.actions.capcut_add_link',
        'modules.proxy.expressvpn',
        'modules.proxy.thuecloud',
        'modules.storage.google_sheets',
        'modules.browser.chromium',
        'core.orchestrator',
        'core.settings',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='regcapcut',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='regcapcut_v8',
)
