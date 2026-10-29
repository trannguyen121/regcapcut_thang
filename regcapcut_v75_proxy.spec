# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['legacy_regcapcut_v75_proxy.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('analysis\\regcapcut_v7_5\\reg_capcut_tab.pyc', 'legacy'),
        ('analysis\\regcapcut_v7_5\\PYZ.pyz', 'legacy'),
        ('modules\\actions\\capcut_workflow.py', 'modern'),
        ('REGCAPCUT_V75_VERSION.txt', '.'),
        ('modules\\browser\\chromium.py', 'modules\\browser'),
        ('H:\\regcapcut_v7.5\\_internal\\certifi\\cacert.pem', 'certifi'),
        ('H:\\regcapcut_v7.5\\_internal\\certifi\\py.typed', 'certifi'),
        ('H:\\regcapcut_v7.5\\_internal\\core\\settings.json', 'core'),
        ('H:\\regcapcut_v7.5\\_internal\\cred.json', '.'),
    ],
    hiddenimports=[
        'tkinter',
        'tkinter.filedialog',
        'tkinter.messagebox',
        'tkinter.scrolledtext',
        'tkinter.ttk',
        'playwright.sync_api',
        'modules.browser.chromium',
        'cryptography.hazmat.bindings._rust',
        'charset_normalizer',
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
    a.binaries,
    a.datas,
    [],
    exclude_binaries=False,
    name='regcapcut_v7.5.507',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    Tree('H:\\regcapcut_v7.5\\chrome-154', prefix='chrome-154'),
    strip=False,
    upx=False,
    upx_exclude=[],
    name='regcapcut_v7.5.507',
)
