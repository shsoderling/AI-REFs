# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for AI REFs."""

import sys
from pathlib import Path

block_cipher = None
root = Path(SPECPATH).parent

a = Analysis(
    [str(root / 'src' / 'app.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / 'assets'), 'assets'),
    ],
    hiddenimports=[
        'PySide6.QtWidgets',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'pydantic',
        'docx',
        'openpyxl',
        'Bio',
        'Bio.Entrez',
        'requests',
        'anthropic',
        'dotenv',
        'spacy',
        'src',
        'src.gui',
        'src.gui.main_window',
        'src.gui.inputs_tab',
        'src.gui.library_tab',
        'src.gui.run_tab',
        'src.gui.review_tab',
        'src.gui.styles',
        'src.gui.widgets',
        'src.gui.widgets.chat_panel',
        'src.gui.widgets.chat_worker',
        'src.pipeline',
        'src.pipeline.orchestrator',
        'src.pipeline.llm_citation_agent',
        'src.pipeline.marker_locator',
        'src.pipeline.document_parser',
        'src.pipeline.existing_citation_parser',
        'src.pipeline.global_qa',
        'src.pipeline.renumbering',
        'src.services',
        'src.services.pubmed_client',
        'src.services.orcid_client',
        'src.services.docx_io',
        'src.services.ref_library',
        'src.services.biorxiv_client',
        'src.services.europepmc_client',
        'src.services.tool_executor',
        'src.models',
        'src.models.project',
        'src.models.sentence',
        'src.models.citation',
        'src.models.evidence',
        'src.models.existing_refs',
        'src.storage',
        'src.storage.cache_db',
        'src.storage.project_io',
        'src.utils',
        'src.utils.constants',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AI REFs',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / 'assets' / 'icon.icns') if sys.platform == 'darwin' else str(root / 'assets' / 'icon.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AI REFs',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='AI REFs.app',
        icon=str(root / 'assets' / 'icon.icns'),
        bundle_identifier='com.airefs.app',
        info_plist={
            'CFBundleName': 'AI REFs',
            'CFBundleDisplayName': 'AI REFs',
            'CFBundleVersion': '1.0.0',
            'CFBundleShortVersionString': '1.0.0',
            'NSHighResolutionCapable': True,
        },
    )
