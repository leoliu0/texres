#!/usr/bin/env python3
"""
scripts/bundle_packages.py

Builds a reproducible bundled TEXMF package archive (`packages.tar.zst`),
the corresponding source distribution archive (`sources.tar.zst`),
and the authoritative machine-readable asset lock (`packages.lock.json`).

Integrates complete offline font families and TeX resources into TeXres:
- Latin Modern (Type 1 text & math, OpenType, metrics, maps)
- CM-Super (Type 1 EC/LH/T2A/B/C/X2 outlines, encodings, maps)
- LH Cyrillic (T2A, T2B metrics including larm1000, lbrm1000, and styles)
- LH (T2A/B/C/X2 Computer Modern metrics)
- Cyrillic (LaTeX font definitions and language support)
- CBfonts LGR Greek (Type 1, metrics, maps, fontdefs, babel-greek)
- Greek Inputenc (complete lgrenc.dfu and 8-bit Greek definitions)
- Wadalab (Type 1, virtual fonts, metrics, maps)
- CJK (LaTeX macro support for CJKutf8, Wadalab, and Arphic)
- IPAex (TrueType, retaining existing Type 1)
- Harano Aji (OpenType Mincho & Gothic, extra weights, fontspec)
- Arphic (Type 1, TrueType, virtual fonts, metrics, maps)
- UHC Korean (Type 1, virtual fonts, metrics, maps, c70mj closure)
- Un-fonts Core (TrueType, GPLv2 source distribution archive)
- Nanum (Type 1, maps)
- stmaryrd (Type 1, metrics, maps, macros)
- bbding (Type 1, metrics, maps, macros)
- FdSymbol (Type 1, OpenType, METAFONT, metrics, encodings, maps, macros)
- LaTeX Base legal files and Font notices

Strictly enforces:
- ZERO host-derived payload: all assets extracted from hash-pinned upstream archives + baseline.
- Authoritative source attribution: each contributing package is explicitly tracked.
- Complete source compliance: corresponding source archives packaged in sources.tar.zst.
- Zero conflicting basenames and deterministic sharded archive creation.
"""

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import struct
import sys
import tarfile
import tempfile
import time

BASELINE_FAMILY_MAP_ROOTS = [
    "fonts/map/dvips/ipaex-type1/ipaex-type1.map",
    "fonts/map/dvips/newtx/newtx.map",
    "fonts/map/dvips/cfr-lm/clm.map",
    "fonts/map/dvips/amsfonts/cm.map",
    "fonts/map/dvips/amsfonts/cmextra.map",
    "fonts/map/dvips/amsfonts/cyrillic.map",
    "fonts/map/dvips/amsfonts/euler.map",
    "fonts/map/dvips/amsfonts/latxfont.map",
    "fonts/map/dvips/amsfonts/symbols.map",
    "fonts/map/dvips/tex-gyre/qtm.map",
    "fonts/map/dvips/xypic/xypic.map",
    "fonts/map/dvips/cm/cmtext-bsr-interpolated.map",
    "fonts/map/dvips/txfonts/txfonts.map",
    "fonts/map/dvips/sansmathaccent/sansmathaccent.map",
    "fonts/map/dvips/tetex/ps2pk35.map",
    "fonts/map/dvips/psnfss/psnfss.map",
    "fonts/map/dvips/psnfss/charter.map",
    "fonts/map/dvips/psnfss/utopia.map",
    "fonts/map/dvips/metapost/troff.map",
    "fonts/map/dvips/psnfss/pazo.map",
]

CYRILLIC_EC_FAMILIES = [
    'labi', 'labl', 'labx', 'lacc', 'ladh', 'lafb', 'laff', 'lafi', 'lafs', 'larb', 'larm', 'lasi', 'lasl', 'laso', 'lass', 'lasx', 'lati', 'laui', 'laxc',
    'lbbi', 'lbbl', 'lbbx', 'lbcc', 'lbdh', 'lbfb', 'lbff', 'lbfi', 'lbfs', 'lbrb', 'lbrm', 'lbsi', 'lbsl', 'lbso', 'lbss', 'lbsx', 'lbti', 'lbui', 'lbxc',
    'lcbi', 'lcbl', 'lcbx', 'lccc', 'lcdh', 'lcfb', 'lcff', 'lcfi', 'lcfs', 'lcrb', 'lcrm', 'lcsi', 'lcsl', 'lcso', 'lcss', 'lcsx', 'lcti', 'lcui', 'lcxc',
    'rxbi', 'rxbl', 'rxbx', 'rxcc', 'rxdh', 'rxfb', 'rxff', 'rxfi', 'rxfs', 'rxrb', 'rxrm', 'rxsi', 'rxsl', 'rxso', 'rxss', 'rxsx', 'rxti', 'rxui', 'rxxc',
]
STANDARD_SIZES = [
    "0500", "0600", "0700", "0800", "0900", "1000",
    "1095", "1200", "1440", "1728", "2074", "2488",
    "2986", "3583",
]
FIXED_MTIME = 1704067200  # 2024-01-01 00:00:00 UTC
PINNED_BASELINE_SHA256 = "29331539190ce01e054611c00094d44b46b1b91ac9cbfd9c5d47b96e384c639f"
CHUNK_SIZE_LIMIT = 65 * 1024 * 1024  # 65 MiB (ensures 6 sequential parts and strictly <= 70 MiB)

# Authoritative upstream packages with verified SHA-256 archive pins
UPSTREAM_PACKAGES = {
    "lm": {
        "version": "2.005",
        "revision": 77682,
        "license": "GUST-FONT-LICENSE / LPPL-1.3c",
        "ctan_path": "/fonts/lm",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/lm.tar.xz",
        "upstream_sha256": "6ecafd1f066d189a0688c10572a01d45c231fa236d6ec1cca726ca4eef41e57c",
        "upstream_size_bytes": 11944800,
        "description": "Latin Modern font family: Type 1 text and math, OpenType, metrics",
        "source_obligations": "GUST Font License; modification permitted provided renamed",
        "license_files": [
            "doc/fonts/lm/GUST-FONT-LICENSE.TXT",
            "doc/fonts/lm/MANIFEST-Latin-Modern.TXT",
        ],
        "map_files": ["fonts/map/dvips/lm/lm.map"],
        "tds_dirs": [
            ("fonts/type1/public/lm", "fonts/type1/public/lm"),
            ("fonts/opentype/public/lm", "fonts/opentype/public/lm"),
            ("fonts/tfm/public/lm", "fonts/tfm/public/lm"),
            ("fonts/enc/dvips/lm", "fonts/enc/dvips/lm"),
            ("fonts/map/dvips/lm", "fonts/map/dvips/lm"),
            ("tex/latex/lm", "tex/latex/lm"),
        ],
    },
    "cm-super": {
        "version": "0.3.4",
        "revision": 15878,
        "license": "GPL-2.0-or-later with font exception",
        "ctan_path": "/fonts/ps-type1/cm-super",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/cm-super.tar.xz",
        "upstream_sha256": "70867904e38451ab1a11ba4677dd5891914f128170b53fac26d3e9dca9d326e4",
        "upstream_size_bytes": 64562404,
        "description": "CM-Super Type 1 outlines and encodings (T1, T2A/B/C, TS1, X2)",
        "source_obligations": "GPL-2.0 with font exception; document embedding permitted without copyleft; corresponding source preserved in sources.tar.zst",
        "license_files": [
            "doc/fonts/cm-super/COPYING",
            "doc/fonts/cm-super/README",
        ],
        "map_files": [
            "fonts/map/dvips/cm-super/cm-super-t1.map",
            "fonts/map/dvips/cm-super/cm-super-t2a.map",
            "fonts/map/dvips/cm-super/cm-super-t2b.map",
            "fonts/map/dvips/cm-super/cm-super-t2c.map",
            "fonts/map/dvips/cm-super/cm-super-ts1.map",
            "fonts/map/dvips/cm-super/cm-super-x2.map",
        ],
        "tds_dirs": [
            ("fonts/type1/public/cm-super", "fonts/type1/public/cm-super"),
            ("fonts/enc/dvips/cm-super", "fonts/enc/dvips/cm-super"),
            ("fonts/map/dvips/cm-super", "fonts/map/dvips/cm-super"),
            ("tex/latex/cm-super", "tex/latex/cm-super"),
        ],
    },
    "lhcyr": {
        "version": "2024",
        "revision": 77838,
        "license": "other-free / LPPL",
        "ctan_path": "/macros/latex/contrib/lhcyr",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/lhcyr.tar.xz",
        "upstream_sha256": "e414d8dc28d0ec03c96ae13d9f6d98834487f8066ab543827d93ab7d020617fc",
        "upstream_size_bytes": 55216,
        "description": "LH Cyrillic T2A and T2B font metrics (including larm1000, lbrm1000)",
        "source_obligations": "Freely redistributable TeX macros and font metrics; source preserved in sources.tar.zst",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("fonts/tfm/lhcyr", "fonts/tfm/lhcyr"),
        ],
    },
    "lh": {
        "version": "3.5g",
        "revision": 77838,
        "license": "LPPL-1.3c",
        "ctan_path": "/fonts/lh",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/lh.tar.xz",
        "upstream_sha256": "eb1f4ef2e7ccd737f1c5463e43e49bc4ce6f69a9f0de6aeb1157446f1c7c22f8",
        "upstream_size_bytes": 361316,
        "description": "LH Cyrillic Computer Modern font metrics (T2A)",
        "source_obligations": "LPPL-1.3c; METAFONT source code preserved in sources.tar.zst",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("fonts/tfm/lh", "fonts/tfm/lh"),
        ],
    },
    "cyrillic": {
        "version": "2024",
        "revision": 71408,
        "license": "LPPL-1.3c",
        "ctan_path": "/macros/latex/required/cyrillic",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/cyrillic.tar.xz",
        "upstream_sha256": "c41a4015b43948ab104e397c24f55750e5b11619ba249a86b78e3b91d538a2a9",
        "upstream_size_bytes": 15772,
        "description": "LaTeX Cyrillic font definitions (T2A, T2B, T2C, X2) and language support",
        "source_obligations": "LPPL-1.3c; official LaTeX project distribution",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("tex/latex/cyrillic", "tex/latex/cyrillic"),
        ],
    },
    "babel-russian": {
        "version": "1.3m",
        "revision": 57375,
        "license": "LPPL-1.3c",
        "ctan_path": "/macros/latex/contrib/babel-contrib/russian",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/babel-russian.tar.xz",
        "upstream_sha256": "5962e93e52acb95a13705a9534bdef6faf3fcc89ba36f97cd85af15a94e83679",
        "upstream_size_bytes": 19008,
        "description": "Russian language support for Babel (russianb.ldf)",
        "source_obligations": "LPPL-1.3c; source archive preserved in sources.tar.zst",
        "license_files": ["doc/generic/babel-russian/README.md"],
        "map_files": [],
        "tds_dirs": [
            ("tex/generic/babel-russian", "tex/generic/babel-russian"),
        ],
    },
    "babel-spanish": {
        "version": "5.0q",
        "revision": 79461,
        "license": "LPPL-1.3c",
        "ctan_path": "/macros/latex/contrib/babel-contrib/spanish",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/babel-spanish.tar.xz",
        "upstream_sha256": "58b92ff26bc27683adbb19a1805a6a6c2e06594f5aa4c80170b92b0ff7c10b74",
        "upstream_size_bytes": 8892,
        "description": "Spanish language support for Babel (spanish.ldf, romanidx.sty)",
        "source_obligations": "LPPL-1.3c; source archive preserved in sources.tar.zst",
        "license_files": ["doc/generic/babel-spanish/README.md"],
        "map_files": [],
        "tds_dirs": [
            ("tex/generic/babel-spanish", "tex/generic/babel-spanish"),
        ],
    },
    "babel-portuges": {
        "version": "1.2u",
        "revision": 77682,
        "license": "LPPL-1.3c",
        "ctan_path": "/macros/latex/contrib/babel-contrib/portuges",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/babel-portuges.tar.xz",
        "upstream_sha256": "640354f13f5f17019c19060437ab8b7bbda7cd2975239865fe5a2c7465e200f7",
        "upstream_size_bytes": 2632,
        "description": "Portuguese and Brazilian support for Babel (portuges.ldf, brazilian.ldf)",
        "source_obligations": "LPPL-1.3c; source archive preserved in sources.tar.zst",
        "license_files": ["doc/generic/babel-portuges/README.md"],
        "map_files": [],
        "tds_dirs": [
            ("tex/generic/babel-portuges", "tex/generic/babel-portuges"),
        ],
    },
    "hyphen-spanish": {
        "version": "5.0",
        "revision": 78069,
        "license": "LPPL-1.3c / MIT",
        "ctan_path": "/language/hyphenation/eshyph",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/hyphen-spanish.tar.xz",
        "upstream_sha256": "ba1c9340f186573770ad28a513bb4aaa334b7bee50f5f902544f381fb058d835",
        "upstream_size_bytes": 16428,
        "description": "Spanish hyphenation patterns (loadhyph-es.tex, hyph-es.tex)",
        "source_obligations": "LPPL-1.3c / MIT; source archive preserved in sources.tar.zst",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("tex/generic/hyph-utf8", "tex/generic/hyph-utf8"),
        ],
    },
    "hyphen-portuguese": {
        "version": "2024",
        "revision": 78069,
        "license": "GPL-2.0-or-later / LPPL",
        "ctan_path": "/language/hyphenation/pt-hyph",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/hyphen-portuguese.tar.xz",
        "upstream_sha256": "68031bec21717fe36f442b2af3fa168aa6bfb4c35f981c9e167b276c497e448e",
        "upstream_size_bytes": 3772,
        "description": "Portuguese hyphenation patterns (loadhyph-pt.tex, hyph-pt.tex)",
        "source_obligations": "GPL-2.0-or-later / LPPL; source archive preserved in sources.tar.zst",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("tex/generic/hyph-utf8", "tex/generic/hyph-utf8"),
        ],
    },
    "hyphen-english": {
        "version": "2024",
        "revision": 78069,
        "license": "LPPL-1.3c / Knuth",
        "ctan_path": "/language/hyphenation/hyph-utf8",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/hyphen-english.tar.xz",
        "upstream_sha256": "cb22c0c51786d47aff5cef7dc6d0a47ab291d6da88c21ac6a9929f4409a456ab",
        "upstream_size_bytes": 41348,
        "description": "English hyphenation patterns (loadhyph-en-gb.tex, loadhyph-en-us.tex)",
        "source_obligations": "LPPL-1.3c; source archive preserved in sources.tar.zst",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("tex/generic/hyph-utf8", "tex/generic/hyph-utf8"),
        ],
    },
    "hyphen-russian": {
        "version": "2024",
        "revision": 78069,
        "license": "LPPL-1.2",
        "ctan_path": "/language/hyphenation/hyph-utf8",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/hyphen-russian.tar.xz",
        "upstream_sha256": "0886909c81731d7a51636f831fac58ff19317d2f1f81a87953090bb13aef9a8a",
        "upstream_size_bytes": 34300,
        "description": "Russian hyphenation patterns (loadhyph-ru.tex, hyph-ru.tex, hyph-ru.t2a.tex)",
        "source_obligations": "LPPL-1.2; source archive preserved in sources.tar.zst",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("tex/generic/hyph-utf8", "tex/generic/hyph-utf8"),
        ],
    },
    "ruhyphen": {
        "version": "1.6",
        "revision": 79618,
        "license": "LPPL-1.2",
        "ctan_path": "/language/hyphenation/ruhyphen",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ruhyphen.tar.xz",
        "upstream_sha256": "09a5ee8b916df34c038574d78fb090b1a22d479f318c72e554617fa59504b3f7",
        "upstream_size_bytes": 56708,
        "description": "Russian hyphenation system (ruhyphen.tex, koi2t2a.tex, ruhyphal.tex, hypht2.tex)",
        "source_obligations": "LPPL-1.2; source archive preserved in sources.tar.zst",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("tex/generic/ruhyphen", "tex/generic/ruhyphen"),
        ],
    },
    "cbfonts": {
        "version": "2024",
        "revision": 54080,
        "license": "LPPL-1.3c",
        "ctan_path": "/fonts/greek/cbfonts",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/cbfonts.tar.xz",
        "upstream_sha256": "b4ec36812e215fcc2ba2e7b12df17bf98adf31b77c044f22d4c5568465e8f0ee",
        "upstream_size_bytes": 66006520,
        "description": "Complete Greek fonts (LGR encoding) Type 1, TFMs, and font definitions",
        "source_obligations": "LPPL-1.3c; modifications must document changes",
        "license_files": ["doc/fonts/cbfonts/README"],
        "map_files": ["fonts/map/dvips/cbfonts/cbgreek-full.map"],
        "tds_dirs": [
            ("fonts/type1/public/cbfonts", "fonts/type1/public/cbfonts"),
            ("fonts/tfm/public/cbfonts", "fonts/tfm/public/cbfonts"),
            ("fonts/enc/dvips/cbfonts", "fonts/enc/dvips/cbfonts"),
            ("fonts/map/dvips/cbfonts", "fonts/map/dvips/cbfonts"),
            ("tex/latex/cbfonts-fd", "tex/latex/cbfonts-fd"),
            ("tex/generic/babel-greek", "tex/generic/babel-greek"),
            ("tex/latex/greek-fontenc", "tex/latex/greek-fontenc"),
        ],
    },
    "greek-inputenc": {
        "version": "1.9",
        "revision": 66634,
        "license": "LPPL-1.3c",
        "ctan_path": "/language/greek/greek-inputenc",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/greek-inputenc.tar.xz",
        "upstream_sha256": "ce0405bb0402a07266a0d43b13a313fb897ecf96b355b00c2394c9c31416f513",
        "upstream_size_bytes": 6864,
        "description": "Greek UTF-8 input encoding definitions (lgrenc.dfu) and 8-bit support",
        "source_obligations": "LPPL-1.3c; modifications must document changes",
        "license_files": ["doc/latex/greek-inputenc/README.md"],
        "map_files": [],
        "tds_dirs": [
            ("tex/latex/greek-inputenc", "tex/latex/greek-inputenc"),
        ],
    },
    "wadalab": {
        "version": "0-12",
        "revision": 42428,
        "license": "Wadalab Font License",
        "ctan_path": "/fonts/wadalab",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/wadalab.tar.xz",
        "upstream_sha256": "81918f1cd7ef1ced85c2adb30c8d6e5eaeba17d43af86b4a888b698085c47018",
        "upstream_size_bytes": 17772744,
        "description": "Wadalab Japanese Kanji Type 1, Virtual Fonts, and CJKutf8 support",
        "source_obligations": "Freely redistributable for non-commercial and commercial use with derivation notice",
        "license_files": ["doc/fonts/wadalab/README"],
        "map_files": [
            "fonts/map/dvips/wadalab/dmj.map",
            "fonts/map/dvips/wadalab/dgj.map",
            "fonts/map/dvips/wadalab/mcj.map",
            "fonts/map/dvips/wadalab/mrj.map",
        ],
        "tds_dirs": [
            ("fonts/type1/wadalab", "fonts/type1/wadalab"),
            ("fonts/vf/wadalab", "fonts/vf/wadalab"),
            ("fonts/tfm/wadalab", "fonts/tfm/wadalab"),
            ("fonts/map/dvips/wadalab", "fonts/map/dvips/wadalab"),
        ],
    },
    "cjk": {
        "version": "4.8.5",
        "revision": 74490,
        "license": "GPL-2.0",
        "ctan_path": "/language/chinese/CJK",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/cjk.tar.xz",
        "upstream_sha256": "744904abda9141fa1a448b6ad3ab6d1389e515e98dbad9489613d98194eee0f2",
        "upstream_size_bytes": 58480,
        "description": "CJK macro package support for Wadalab and Arphic fonts",
        "source_obligations": "GPL-2.0; CJK LaTeX support macros",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("tex/latex/cjk/contrib/wadalab", "tex/latex/cjk/contrib/wadalab"),
            ("tex/latex/cjk/texinput", "tex/latex/cjk/texinput"),
        ],
    },
    "ipaex": {
        "version": "00401",
        "revision": 61719,
        "license": "IPA Font License v1.0",
        "ctan_path": "/fonts/ipaex",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ipaex.tar.xz",
        "upstream_sha256": "7db75b91663a5fa711d72fba2d1934580e0570c58d2937f5f54aa3ac7cf43e15",
        "upstream_size_bytes": 15865176,
        "description": "IPAex Mincho and IPAex Gothic TrueType fonts",
        "source_obligations": "IPA Font License v1.0; redistribution permitted with notice",
        "license_files": ["doc/fonts/ipaex/IPA_Font_License_Agreement_v1.0.txt"],
        "map_files": [],
        "tds_dirs": [
            ("fonts/truetype/public/ipaex", "fonts/truetype/public/ipaex"),
        ],
    },
    "haranoaji": {
        "version": "20250811",
        "revision": 76078,
        "license": "OFL-1.1",
        "ctan_path": "/fonts/haranoaji",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/haranoaji.tar.xz",
        "upstream_sha256": "e9410a8fe32dde7421fb8e4ae46f4e7aa58fe2a487fdb5c2e268ed622c14252b",
        "upstream_size_bytes": 26259784,
        "description": "Harano Aji Mincho and Gothic OpenType fonts",
        "source_obligations": "SIL Open Font License 1.1; bundling and embedding permitted",
        "license_files": ["doc/fonts/haranoaji/LICENSE"],
        "map_files": [],
        "tds_dirs": [
            ("fonts/opentype/public/haranoaji", "fonts/opentype/public/haranoaji"),
            ("tex/latex/haranoaji", "tex/latex/haranoaji"),
        ],
    },
    "haranoaji-extra": {
        "version": "20250811",
        "revision": 76079,
        "license": "OFL-1.1",
        "ctan_path": "/fonts/haranoaji-extra",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/haranoaji-extra.tar.xz",
        "upstream_sha256": "2de4b181bc5f36aa8a8bb3a967d50261150c8a31432c5d40723e2c61cc3e5a89",
        "upstream_size_bytes": 26101888,
        "description": "Harano Aji Mincho and Gothic extra weights OpenType fonts",
        "source_obligations": "SIL Open Font License 1.1; bundling and embedding permitted",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("fonts/opentype/public/haranoaji-extra", "fonts/opentype/public/haranoaji-extra"),
        ],
    },
    "arphic": {
        "version": "1999",
        "revision": 15878,
        "license": "Arphic Public License",
        "ctan_path": "/fonts/arphic",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/arphic.tar.xz",
        "upstream_sha256": "fb0eb8ec13e210a3d1023478c35bd897f5e60049a05c87258ca19fa8452cb31b",
        "upstream_size_bytes": 27383316,
        "description": "Arphic Chinese Type 1, VFs, TFMs, and maps",
        "source_obligations": "Arphic Public License; commercial distribution allowed with attribution",
        "license_files": ["doc/fonts/arphic-ttf/ARPHICPL.txt", "doc/fonts/arphic/gbsnu/README"],
        "map_files": [
            "fonts/map/dvips/arphic/gbsnu.map",
            "fonts/map/dvips/arphic/gkaiu.map",
            "fonts/map/dvips/arphic/bsmiu.map",
            "fonts/map/dvips/arphic/bkaiu.map",
        ],
        "tds_dirs": [
            ("fonts/type1/arphic", "fonts/type1/arphic"),
            ("fonts/vf/arphic", "fonts/vf/arphic"),
            ("fonts/tfm/arphic", "fonts/tfm/arphic"),
            ("fonts/map/dvips/arphic", "fonts/map/dvips/arphic"),
        ],
    },
    "arphic-ttf": {
        "version": "1999",
        "revision": 42675,
        "license": "Arphic Public License",
        "ctan_path": "/fonts/arphic-ttf",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/arphic-ttf.tar.xz",
        "upstream_sha256": "a44031616debf9ffc30c174727c0c33d28540f3b18fa9d980b52aa39752e267e",
        "upstream_size_bytes": 12663696,
        "description": "Arphic Chinese TrueType fonts (bkai00mp, bsmi00lp, gbsn00lp, gkai00mp)",
        "source_obligations": "Arphic Public License; redistribution permitted with notice",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("fonts/truetype/public/arphic-ttf", "fonts/truetype/public/arphic-ttf"),
        ],
    },
    "uhc": {
        "version": "1.0",
        "revision": 16791,
        "license": "LPPL",
        "ctan_path": "/fonts/korean/HLaTeX",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/uhc.tar.xz",
        "upstream_sha256": "6c3df3ca1acee37f2a46fc2c42c24b4f27b89f985e95760f8a6b28f03f6c43af",
        "upstream_size_bytes": 3600148,
        "description": "Korean UHC fonts: c70mj -> uwmj VF/TFM, wmj VF/TFM, umj Type 1/TFM, and map",
        "source_obligations": "LaTeX Project Public License; author Koaunghi Un",
        "license_files": ["doc/fonts/uhc/umj/README"],
        "map_files": ["fonts/map/dvips/uhc/umj.map"],
        "tds_dirs": [
            ("fonts/type1/uhc/umj", "fonts/type1/uhc/umj"),
            ("fonts/vf/uhc/uwmj", "fonts/vf/uhc/uwmj"),
            ("fonts/vf/uhc/wmj", "fonts/vf/uhc/wmj"),
            ("fonts/tfm/uhc/uwmj", "fonts/tfm/uhc/uwmj"),
            ("fonts/tfm/uhc/wmj", "fonts/tfm/uhc/wmj"),
            ("fonts/tfm/uhc/umj", "fonts/tfm/uhc/umj"),
            ("fonts/map/dvips/uhc", "fonts/map/dvips/uhc"),
        ],
    },
    "unfonts-core": {
        "version": "1.0.2",
        "revision": 56291,
        "license": "GPL-2.0",
        "ctan_path": "/fonts/unfonts-core",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/unfonts-core.tar.xz",
        "upstream_sha256": "513990ad0501f2388cdcad68ac635ad94d21b4fed9424a58db7ef9d7d6922bc1",
        "upstream_size_bytes": 14727532,
        "description": "Un-fonts Korean TrueType font family (UnBatang, UnDotum, UnGraphic, UnGungseo, UnPilgi)",
        "source_obligations": "Standard GPLv2 without font exception; source archive preserved in sources.tar.zst",
        "license_files": ["doc/fonts/unfonts-core/COPYING", "doc/fonts/unfonts-core/README.md"],
        "map_files": [],
        "tds_dirs": [
            ("fonts/truetype/public/unfonts-core", "fonts/truetype/public/unfonts-core"),
        ],
    },
    "nanumtype1": {
        "version": "3.0",
        "revision": 29558,
        "license": "OFL-1.1",
        "ctan_path": "/fonts/nanumtype1",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/nanumtype1.tar.xz",
        "upstream_sha256": "77e94f530f1e3298c81b40ea5a2dd0f3bd8956af0ea750d3cf68dee581ffdbd9",
        "upstream_size_bytes": 28242732,
        "description": "Nanum Korean Type 1 fonts, maps, and LaTeX definitions",
        "source_obligations": "SIL Open Font License 1.1; embedding permitted",
        "license_files": ["doc/fonts/nanumtype1/COPYING"],
        "map_files": ["fonts/map/dvips/nanumtype1/nanumfonts.map"],
        "tds_dirs": [
            ("fonts/type1/public/nanumtype1", "fonts/type1/public/nanumtype1"),
            ("fonts/tfm/public/nanumtype1", "fonts/tfm/public/nanumtype1"),
            ("fonts/vf/public/nanumtype1", "fonts/vf/public/nanumtype1"),
            ("fonts/map/dvips/nanumtype1", "fonts/map/dvips/nanumtype1"),
            ("tex/latex/nanumtype1", "tex/latex/nanumtype1"),
        ],
    },
    "stmaryrd": {
        "version": "1998",
        "revision": 77682,
        "license": "Public Domain",
        "ctan_path": "/fonts/stmaryrd",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/stmaryrd.tar.xz",
        "upstream_sha256": "3dcd9aeeeb8c502cc0ee908838a5154f0ea03c810cbf5d4e5088c1eee44a4079",
        "upstream_size_bytes": 166144,
        "description": "St Mary's Road symbol font (Type 1, TFMs, map, sty)",
        "source_obligations": "Public Domain; free use and redistribution; source preserved in sources.tar.zst",
        "license_files": ["doc/fonts/stmaryrd/README", "doc/fonts/stmaryrd/README.hoekwater"],
        "map_files": ["fonts/map/dvips/stmaryrd/stmaryrd.map"],
        "tds_dirs": [
            ("fonts/type1/public/stmaryrd", "fonts/type1/public/stmaryrd"),
            ("fonts/tfm/public/stmaryrd", "fonts/tfm/public/stmaryrd"),
            ("fonts/map/dvips/stmaryrd", "fonts/map/dvips/stmaryrd"),
            ("tex/latex/stmaryrd", "tex/latex/stmaryrd"),
        ],
    },
    "bbding": {
        "version": "1.01",
        "revision": 77682,
        "license": "LPPL-1.3c",
        "ctan_path": "/fonts/bbding",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/bbding.tar.xz",
        "upstream_sha256": "55e0bca1e18927c792b1fd5cc48592c02fdd5fff3691c26ab0db82ba4b335892",
        "upstream_size_bytes": 11680,
        "description": "Blakowski Dingbats font (bbding10 TFM, LaTeX sty)",
        "source_obligations": "LPPL-1.3c; source preserved in sources.tar.zst",
        "license_files": ["doc/latex/bbding/README"],
        "map_files": [],
        "tds_dirs": [
            ("fonts/tfm/public/bbding", "fonts/tfm/public/bbding"),
            ("tex/latex/bbding", "tex/latex/bbding"),
        ],
    },
    "niceframe-type1": {
        "version": "2024",
        "revision": 77682,
        "license": "other-free",
        "ctan_path": "/fonts/type1/niceframe-type1",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/niceframe-type1.tar.xz",
        "upstream_sha256": "14adfe8533417dd0aa59f1c554a02a3f46a600b939815babeec4226dcc445c99",
        "upstream_size_bytes": 275504,
        "description": "NiceFrame and bbding10 Type 1 outline fonts and dvips map",
        "source_obligations": "Freely redistributable Type 1 fonts",
        "license_files": [],
        "map_files": ["fonts/map/dvips/niceframe-type1/niceframe.map"],
        "tds_dirs": [
            ("fonts/type1/public/niceframe-type1", "fonts/type1/public/niceframe-type1"),
            ("fonts/map/dvips/niceframe-type1", "fonts/map/dvips/niceframe-type1"),
        ],
    },
    "rsfs": {
        "version": "1.0",
        "revision": 15878,
        "license": "Public Domain",
        "ctan_path": "/fonts/rsfs",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/rsfs.tar.xz",
        "upstream_sha256": "1afec0c5e9711f652675e38b7cd7e88101c44aa0d0ff317ad6ac06f1d2cc7043",
        "upstream_size_bytes": 56092,
        "description": "Ralph Smith's Formal Script symbol fonts (Type 1 outlines, metrics, map)",
        "source_obligations": "Public Domain; source code preserved in sources.tar.zst",
        "license_files": ["doc/fonts/rsfs/README", "doc/fonts/rsfs/README.type1"],
        "map_files": ["fonts/map/dvips/rsfs/rsfs.map"],
        "tds_dirs": [
            ("fonts/type1/public/rsfs", "fonts/type1/public/rsfs"),
            ("fonts/tfm/public/rsfs", "fonts/tfm/public/rsfs"),
            ("fonts/map/dvips/rsfs", "fonts/map/dvips/rsfs"),
        ],
    },
    "times": {
        "version": "2024",
        "revision": 77161,
        "license": "GPL-2.0-or-later",
        "ctan_path": "/fonts/urw/base35",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/times.tar.xz",
        "upstream_sha256": "55f6097b5685a22a3ec1db61ae372ffe00b25d622ed4ee3d010cd8a037e48a00",
        "upstream_size_bytes": 286900,
        "description": "URW 'Base 35' font pack for LaTeX: Times (Nimbus Roman No9 L) Type 1, metrics, VFs, maps",
        "source_obligations": "GNU General Public License v2.0 or later with font exception; corresponding source in base35.zip",
        "license_files": ["doc/fonts/urw-base35/COPYING", "doc/fonts/urw-base35/README"],
        "map_files": ["fonts/map/dvips/times/utm.map"],
        "tds_dirs": [
            ("fonts/type1/urw/times", "fonts/type1/urw/times"),
            ("fonts/afm/urw/times", "fonts/afm/urw/times"),
            ("fonts/tfm/adobe/times", "fonts/tfm/adobe/times"),
            ("fonts/tfm/urw35vf/times", "fonts/tfm/urw35vf/times"),
            ("fonts/vf/adobe/times", "fonts/vf/adobe/times"),
            ("fonts/vf/urw35vf/times", "fonts/vf/urw35vf/times"),
            ("fonts/map/dvips/times", "fonts/map/dvips/times"),
            ("tex/latex/times", "tex/latex/times"),
        ],
    },
    "helvetic": {
        "version": "2024",
        "revision": 77161,
        "license": "GPL-2.0-or-later",
        "ctan_path": "/fonts/urw/base35",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/helvetic.tar.xz",
        "upstream_sha256": "155b23ee6096e32fe7a481500a75269027042abebc3955e7966327c1d1f41db4",
        "upstream_size_bytes": 539616,
        "description": "URW 'Base 35' font pack for LaTeX: Helvetica (Nimbus Sans L) Type 1, metrics, VFs, maps",
        "source_obligations": "GNU General Public License v2.0 or later with font exception; corresponding source in base35.zip",
        "license_files": [],
        "map_files": ["fonts/map/dvips/helvetic/uhv.map"],
        "tds_dirs": [
            ("fonts/type1/urw/helvetic", "fonts/type1/urw/helvetic"),
            ("fonts/afm/urw/helvetic", "fonts/afm/urw/helvetic"),
            ("fonts/tfm/adobe/helvetic", "fonts/tfm/adobe/helvetic"),
            ("fonts/tfm/urw35vf/helvetic", "fonts/tfm/urw35vf/helvetic"),
            ("fonts/vf/adobe/helvetic", "fonts/vf/adobe/helvetic"),
            ("fonts/vf/urw35vf/helvetic", "fonts/vf/urw35vf/helvetic"),
            ("fonts/map/dvips/helvetic", "fonts/map/dvips/helvetic"),
            ("tex/latex/helvetic", "tex/latex/helvetic"),
        ],
    },
    "palatino": {
        "version": "2024",
        "revision": 77161,
        "license": "GPL-2.0-or-later",
        "ctan_path": "/fonts/urw/base35",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/palatino.tar.xz",
        "upstream_sha256": "a5950b7ce364231aace8830cdedbe6ab511bfe663a2f1ba0bd270e618463b6f1",
        "upstream_size_bytes": 325796,
        "description": "URW 'Base 35' font pack for LaTeX: Palatino (URW Palladio L) Type 1, metrics, VFs, maps",
        "source_obligations": "GNU General Public License v2.0 or later with font exception; corresponding source in base35.zip",
        "license_files": [],
        "map_files": ["fonts/map/dvips/palatino/upl.map"],
        "tds_dirs": [
            ("fonts/type1/urw/palatino", "fonts/type1/urw/palatino"),
            ("fonts/afm/urw/palatino", "fonts/afm/urw/palatino"),
            ("fonts/tfm/adobe/palatino", "fonts/tfm/adobe/palatino"),
            ("fonts/tfm/urw35vf/palatino", "fonts/tfm/urw35vf/palatino"),
            ("fonts/vf/adobe/palatino", "fonts/vf/adobe/palatino"),
            ("fonts/vf/urw35vf/palatino", "fonts/vf/urw35vf/palatino"),
            ("fonts/map/dvips/palatino", "fonts/map/dvips/palatino"),
            ("tex/latex/palatino", "tex/latex/palatino"),
        ],
    },
    "zapfding": {
        "version": "2024",
        "revision": 77161,
        "license": "GPL-2.0-or-later",
        "ctan_path": "/fonts/urw/base35",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/zapfding.tar.xz",
        "upstream_sha256": "7c95b0011067228c4395aef990d92e190a66f825f6f3fb5bf09a738893b47ece",
        "upstream_size_bytes": 46940,
        "description": "URW 'Base 35' font pack for LaTeX: Zapf Dingbats Type 1, metrics, and maps",
        "source_obligations": "GNU General Public License v2.0 or later with font exception; corresponding source in base35.zip",
        "license_files": [],
        "map_files": ["fonts/map/dvips/zapfding/uzd.map"],
        "tds_dirs": [
            ("fonts/type1/urw/zapfding", "fonts/type1/urw/zapfding"),
            ("fonts/afm/urw/zapfding", "fonts/afm/urw/zapfding"),
            ("fonts/tfm/adobe/zapfding", "fonts/tfm/adobe/zapfding"),
            ("fonts/tfm/urw35vf/zapfding", "fonts/tfm/urw35vf/zapfding"),
            ("fonts/map/dvips/zapfding", "fonts/map/dvips/zapfding"),
            ("tex/latex/zapfding", "tex/latex/zapfding"),
        ],
    },
    "symbol": {
        "version": "2024",
        "revision": 77161,
        "license": "GPL-2.0-or-later",
        "ctan_path": "/fonts/urw/base35",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/symbol.tar.xz",
        "upstream_sha256": "c6b615bf830b8260a18f26a2aedd406c0e3aa9a24831cc1b2bc73be1774a669b",
        "upstream_size_bytes": 36104,
        "description": "URW 'Base 35' font pack for LaTeX: Symbol (Standard Symbols L) Type 1, metrics, maps",
        "source_obligations": "GNU General Public License v2.0 or later with font exception; corresponding source in base35.zip",
        "license_files": [],
        "map_files": ["fonts/map/dvips/symbol/usy.map"],
        "tds_dirs": [
            ("fonts/type1/urw/symbol", "fonts/type1/urw/symbol"),
            ("fonts/afm/urw/symbol", "fonts/afm/urw/symbol"),
            ("fonts/tfm/adobe/symbol", "fonts/tfm/adobe/symbol"),
            ("fonts/tfm/urw35vf/symbol", "fonts/tfm/urw35vf/symbol"),
            ("fonts/map/dvips/symbol", "fonts/map/dvips/symbol"),
            ("tex/latex/symbol", "tex/latex/symbol"),
        ],
    },
    "courier": {
        "version": "2024",
        "revision": 77161,
        "license": "GPL-2.0-or-later",
        "ctan_path": "/fonts/urw/base35",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/courier.tar.xz",
        "upstream_sha256": "eaecb5bcd119e6409ac549fdffbe73a6bf7087daef43085104a1ba03787ec989",
        "upstream_size_bytes": 481072,
        "description": "URW 'Base 35' font pack for LaTeX: Courier (Nimbus Mono L) Type 1, metrics, VFs, maps",
        "source_obligations": "GNU General Public License v2.0 or later with font exception; corresponding source in base35.zip",
        "license_files": [],
        "map_files": ["fonts/map/dvips/courier/ucr.map"],
        "tds_dirs": [
            ("fonts/type1/urw/courier", "fonts/type1/urw/courier"),
            ("fonts/type1/adobe/courier", "fonts/type1/adobe/courier"),
            ("fonts/afm/urw/courier", "fonts/afm/urw/courier"),
            ("fonts/afm/adobe/courier", "fonts/afm/adobe/courier"),
            ("fonts/tfm/adobe/courier", "fonts/tfm/adobe/courier"),
            ("fonts/tfm/urw35vf/courier", "fonts/tfm/urw35vf/courier"),
            ("fonts/vf/adobe/courier", "fonts/vf/adobe/courier"),
            ("fonts/vf/urw35vf/courier", "fonts/vf/urw35vf/courier"),
            ("fonts/map/dvips/courier", "fonts/map/dvips/courier"),
            ("tex/latex/courier", "tex/latex/courier"),
        ],
    },
    "bookman": {
        "version": "2024",
        "revision": 77161,
        "license": "GPL-2.0-or-later",
        "ctan_path": "/fonts/urw/base35",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/bookman.tar.xz",
        "upstream_sha256": "ec4736522d6749513268a83502febc4dcb73346f8e0b70dafaa76a74ae8998ca",
        "upstream_size_bytes": 278740,
        "description": "URW 'Base 35' font pack for LaTeX: Bookman (URW Bookman L) Type 1, metrics, VFs, maps",
        "source_obligations": "GNU General Public License v2.0 or later with font exception; corresponding source in base35.zip",
        "license_files": [],
        "map_files": ["fonts/map/dvips/bookman/ubk.map"],
        "tds_dirs": [
            ("fonts/type1/urw/bookman", "fonts/type1/urw/bookman"),
            ("fonts/afm/urw/bookman", "fonts/afm/urw/bookman"),
            ("fonts/tfm/adobe/bookman", "fonts/tfm/adobe/bookman"),
            ("fonts/tfm/urw35vf/bookman", "fonts/tfm/urw35vf/bookman"),
            ("fonts/vf/adobe/bookman", "fonts/vf/adobe/bookman"),
            ("fonts/vf/urw35vf/bookman", "fonts/vf/urw35vf/bookman"),
            ("fonts/map/dvips/bookman", "fonts/map/dvips/bookman"),
            ("tex/latex/bookman", "tex/latex/bookman"),
        ],
    },
    "avantgar": {
        "version": "2024",
        "revision": 77161,
        "license": "GPL-2.0-or-later",
        "ctan_path": "/fonts/urw/base35",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/avantgar.tar.xz",
        "upstream_sha256": "4200ef68e6a3564eb6dd3bbad9f760e70c10a81d7ad3a81b93988800dbd50141",
        "upstream_size_bytes": 241568,
        "description": "URW 'Base 35' font pack for LaTeX: Avant Garde (URW Gothic L) Type 1, metrics, VFs, maps",
        "source_obligations": "GNU General Public License v2.0 or later with font exception; corresponding source in base35.zip",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("fonts/type1/urw/avantgar", "fonts/type1/urw/avantgar"),
            ("fonts/afm/urw/avantgar", "fonts/afm/urw/avantgar"),
            ("fonts/tfm/adobe/avantgar", "fonts/tfm/adobe/avantgar"),
            ("fonts/tfm/urw35vf/avantgar", "fonts/tfm/urw35vf/avantgar"),
            ("fonts/vf/adobe/avantgar", "fonts/vf/adobe/avantgar"),
            ("fonts/vf/urw35vf/avantgar", "fonts/vf/urw35vf/avantgar"),
            ("fonts/map/dvips/avantgar", "fonts/map/dvips/avantgar"),
            ("tex/latex/avantgar", "tex/latex/avantgar"),
        ],
    },
    "ncntrsbk": {
        "version": "2024",
        "revision": 77161,
        "license": "GPL-2.0-or-later",
        "ctan_path": "/fonts/urw/base35",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ncntrsbk.tar.xz",
        "upstream_sha256": "4976f1859fb159505e60f1a21274b6b63c5af88f2e03cd0ea6a7e3c84512f4b7",
        "upstream_size_bytes": 290852,
        "description": "URW 'Base 35' font pack for LaTeX: New Century Schoolbook (Century Schoolbook L) Type 1, metrics, VFs, maps",
        "source_obligations": "GNU General Public License v2.0 or later with font exception; corresponding source in base35.zip",
        "license_files": [],
        "map_files": ["fonts/map/dvips/ncntrsbk/unc.map"],
        "tds_dirs": [
            ("fonts/type1/urw/ncntrsbk", "fonts/type1/urw/ncntrsbk"),
            ("fonts/afm/urw/ncntrsbk", "fonts/afm/urw/ncntrsbk"),
            ("fonts/tfm/adobe/ncntrsbk", "fonts/tfm/adobe/ncntrsbk"),
            ("fonts/tfm/urw35vf/ncntrsbk", "fonts/tfm/urw35vf/ncntrsbk"),
            ("fonts/vf/adobe/ncntrsbk", "fonts/vf/adobe/ncntrsbk"),
            ("fonts/vf/urw35vf/ncntrsbk", "fonts/vf/urw35vf/ncntrsbk"),
            ("fonts/map/dvips/ncntrsbk", "fonts/map/dvips/ncntrsbk"),
            ("tex/latex/ncntrsbk", "tex/latex/ncntrsbk"),
        ],
    },
    "zapfchan": {
        "version": "2024",
        "revision": 77161,
        "license": "GPL-2.0-or-later",
        "ctan_path": "/fonts/urw/base35",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/zapfchan.tar.xz",
        "upstream_sha256": "b1af3ec046fc3bdc9c1f35586a523cfd1393d02fd28e146b3efd2260d3311214",
        "upstream_size_bytes": 79900,
        "description": "URW 'Base 35' font pack for LaTeX: Zapf Chancery (URW Chancery L) Type 1, metrics, VFs, maps",
        "source_obligations": "GNU General Public License v2.0 or later with font exception; corresponding source in base35.zip",
        "license_files": [],
        "map_files": ["fonts/map/dvips/zapfchan/uzc.map"],
        "tds_dirs": [
            ("fonts/type1/urw/zapfchan", "fonts/type1/urw/zapfchan"),
            ("fonts/afm/urw/zapfchan", "fonts/afm/urw/zapfchan"),
            ("fonts/tfm/adobe/zapfchan", "fonts/tfm/adobe/zapfchan"),
            ("fonts/tfm/urw35vf/zapfchan", "fonts/tfm/urw35vf/zapfchan"),
            ("fonts/vf/adobe/zapfchan", "fonts/vf/adobe/zapfchan"),
            ("fonts/vf/urw35vf/zapfchan", "fonts/vf/urw35vf/zapfchan"),
            ("fonts/map/dvips/zapfchan", "fonts/map/dvips/zapfchan"),
            ("tex/latex/zapfchan", "tex/latex/zapfchan"),
        ],
    },
    "charter": {
        "version": "2024",
        "revision": 15878,
        "license": "other-free",
        "ctan_path": "/fonts/charter",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/charter.tar.xz",
        "upstream_sha256": "34a08f3370093966207b4a3fed029c215882c89658925624362a877f927c9bd0",
        "upstream_size_bytes": 175180,
        "description": "Bitstream Charter fonts: Type 1 outlines, metrics, and VFs",
        "source_obligations": "Bitstream Free License; unrestricted redistribution and use",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("fonts/type1/bitstrea/charter", "fonts/type1/bitstrea/charter"),
            ("fonts/afm/bitstrea/charter", "fonts/afm/bitstrea/charter"),
            ("fonts/tfm/bitstrea/charter", "fonts/tfm/bitstrea/charter"),
            ("fonts/vf/bitstrea/charter", "fonts/vf/bitstrea/charter"),
        ],
    },
    "utopia": {
        "version": "2024",
        "revision": 77682,
        "license": "other-free",
        "ctan_path": "/fonts/utopia",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/utopia.tar.xz",
        "upstream_sha256": "d8148e06274d9f3eca68d90586ee4f5797068ce8bb9ff93a7cb24872f520a643",
        "upstream_size_bytes": 205540,
        "description": "Adobe Utopia Type 1 outline fonts and support",
        "source_obligations": "Adobe / X Consortium free font license",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("fonts/type1/adobe/utopia", "fonts/type1/adobe/utopia"),
        ],
    },
    "calligra-type1": {
        "version": "001.000",
        "revision": 24302,
        "license": "other-free",
        "ctan_path": "/fonts/calligra-type1",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/calligra-type1.tar.xz",
        "upstream_sha256": "981abbc90cbf776705154feed49ebf2f2e5f8402b8e2e58fc04d0bc19a9a35bf",
        "upstream_size_bytes": 59668,
        "description": "Type 1 version of Calligra calligraphy font",
        "source_obligations": "Freely redistributable Type 1 outline",
        "license_files": [],
        "map_files": ["fonts/map/dvips/calligra-type1/calligra.map"],
        "tds_dirs": [
            ("fonts/type1/public/calligra-type1", "fonts/type1/public/calligra-type1"),
            ("fonts/map/dvips/calligra-type1", "fonts/map/dvips/calligra-type1"),
        ],
    },
    "mathabx-type1": {
        "version": "2024",
        "revision": 21129,
        "license": "LPPL",
        "ctan_path": "/fonts/mathabx-type1",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/mathabx-type1.tar.xz",
        "upstream_sha256": "7a3df592ec65c261221db2c82f256844d646f2e72cd1b96cf0121580a8bad41b",
        "upstream_size_bytes": 1858004,
        "description": "Outline Type 1 version of mathabx mathematical fonts",
        "source_obligations": "LaTeX Project Public License; source conversion",
        "license_files": [],
        "map_files": ["fonts/map/dvips/mathabx-type1/mathabx.map"],
        "tds_dirs": [
            ("fonts/type1/public/mathabx-type1", "fonts/type1/public/mathabx-type1"),
            ("fonts/map/dvips/mathabx-type1", "fonts/map/dvips/mathabx-type1"),
        ],
    },
    "doublestroke": {
        "version": "1.111",
        "revision": 77682,
        "license": "other-free",
        "ctan_path": "/fonts/doublestroke",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/doublestroke.tar.xz",
        "upstream_sha256": "ae9a49b2855d2328659496e58890c60f75cb791fd0326fbc3345f0bc9c1dfc00",
        "upstream_size_bytes": 67020,
        "description": "Typeset mathematical double stroke symbols (dsrom/dsss Type 1 and metrics)",
        "source_obligations": "Freely redistributable Type 1 fonts",
        "license_files": [],
        "map_files": ["fonts/map/dvips/doublestroke/dstroke.map"],
        "tds_dirs": [
            ("fonts/type1/public/doublestroke", "fonts/type1/public/doublestroke"),
            ("fonts/tfm/public/doublestroke", "fonts/tfm/public/doublestroke"),
            ("fonts/map/dvips/doublestroke", "fonts/map/dvips/doublestroke"),
            ("tex/latex/doublestroke", "tex/latex/doublestroke"),
        ],
    },
    "bbold-type1": {
        "version": "2024",
        "revision": 33143,
        "license": "other-free",
        "ctan_path": "/fonts/bbold-type1",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/bbold-type1.tar.xz",
        "upstream_sha256": "48bc70b5d0035df360d94780f78a4b7f7d752e3efb196ae7f2478ee75859a3ca",
        "upstream_size_bytes": 70004,
        "description": "Adobe Type 1 format version of bbold Blackboard Bold font",
        "source_obligations": "Freely redistributable Type 1 fonts",
        "license_files": [],
        "map_files": ["fonts/map/dvips/bbold-type1/bbold.map"],
        "tds_dirs": [
            ("fonts/type1/public/bbold-type1", "fonts/type1/public/bbold-type1"),
            ("fonts/map/dvips/bbold-type1", "fonts/map/dvips/bbold-type1"),
        ],
    },
    "yhmath": {
        "version": "1.6",
        "revision": 77682,
        "license": "LPPL-1.3c",
        "ctan_path": "/fonts/yhmath",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/yhmath.tar.xz",
        "upstream_sha256": "51de28ec43186316245cb1a08a3855cf3d13672b66c3515a88ebbc1b2801d7a9",
        "upstream_size_bytes": 37016,
        "description": "Extended maths fonts for LaTeX (yhcmex Type 1, VFs, metrics)",
        "source_obligations": "LPPL-1.3c; corresponding source in yhmath.source.tar.xz",
        "license_files": [],
        "map_files": ["fonts/map/dvips/yhmath/yhmath.map"],
        "tds_dirs": [
            ("fonts/type1/public/yhmath", "fonts/type1/public/yhmath"),
            ("fonts/tfm/public/yhmath", "fonts/tfm/public/yhmath"),
            ("fonts/vf/public/yhmath", "fonts/vf/public/yhmath"),
            ("fonts/map/dvips/yhmath", "fonts/map/dvips/yhmath"),
            ("tex/latex/yhmath", "tex/latex/yhmath"),
        ],
    },
    "ae": {
        "version": "1.4",
        "revision": 15878,
        "license": "LPPL",
        "ctan_path": "/fonts/ae",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ae.tar.xz",
        "upstream_sha256": "8afa5c20210007d2fd289c067c50f1b6fd4a4ee4d4afc465990cfd083c7e7a65",
        "upstream_size_bytes": 57336,
        "description": "Virtual fonts for T1 encoded Computer Modern Roman fonts",
        "source_obligations": "LPPL; corresponding source in ae.source.tar.xz",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [
            ("fonts/tfm/public/ae", "fonts/tfm/public/ae"),
            ("fonts/vf/public/ae", "fonts/vf/public/ae"),
            ("tex/latex/ae", "tex/latex/ae"),
        ],
    },
    "esint-type1": {
        "version": "2024",
        "revision": 15878,
        "license": "Public Domain",
        "ctan_path": "/fonts/esint-type1",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/esint-type1.tar.xz",
        "upstream_sha256": "12f0b199e9d29af3742d105a52c521f8ec2953bc404e4cf72502fc02051f7ce3",
        "upstream_size_bytes": 31568,
        "description": "Font esint10 in Type 1 format",
        "source_obligations": "Public Domain; Eddie Saudrais",
        "license_files": [],
        "map_files": ["fonts/map/dvips/esint-type1/esint.map"],
        "tds_dirs": [
            ("fonts/type1/public/esint-type1", "fonts/type1/public/esint-type1"),
            ("fonts/map/dvips/esint-type1", "fonts/map/dvips/esint-type1"),
        ],
    },
    "txfonts": {
        "version": "2024",
        "revision": 77682,
        "license": "GPL",
        "ctan_path": "/fonts/txfonts",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/txfonts.tar.xz",
        "upstream_sha256": "03189f5a8b40535a5ca705aabb80aa9e9c9b9c993ba5885f891a3cdfc80afb1d",
        "upstream_size_bytes": 709532,
        "description": "Times-like fonts in support of mathematics: complete Type 1 outlines, metrics, VFs, maps",
        "source_obligations": "GNU General Public License; Young Ryu; corresponding source in txfonts.zip",
        "license_files": ["doc/fonts/txfonts/README"],
        "map_files": ["fonts/map/dvips/txfonts/txfonts.map"],
        "tds_dirs": [
            ("fonts/type1/public/txfonts", "fonts/type1/public/txfonts"),
            ("fonts/tfm/public/txfonts", "fonts/tfm/public/txfonts"),
            ("fonts/vf/public/txfonts", "fonts/vf/public/txfonts"),
            ("fonts/afm/public/txfonts", "fonts/afm/public/txfonts"),
            ("fonts/map/dvips/txfonts", "fonts/map/dvips/txfonts"),
            ("tex/latex/txfonts", "tex/latex/txfonts"),
        ],
    },
    "libertine": {
        "version": "5.3.0",
        "revision": 77682,
        "license": "GPL / OFL / LPPL",
        "ctan_path": "/fonts/libertine",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/libertine.tar.xz",
        "upstream_sha256": "22c57eb8fb021c16a98c1c4ad286c4852133527b6e7de27ac1f9781f42e6b20e",
        "upstream_size_bytes": 13846900,
        "description": "Linux Libertine and Biolinum fonts: Type 1, OpenType, metrics, encodings, and maps",
        "source_obligations": "GNU General Public License with font exception / SIL Open Font License / LPPL",
        "license_files": [],
        "map_files": ["fonts/map/dvips/libertine/libertine.map"],
        "tds_dirs": [
            ("fonts/type1/public/libertine", "fonts/type1/public/libertine"),
            ("fonts/opentype/public/libertine", "fonts/opentype/public/libertine"),
            ("fonts/tfm/public/libertine", "fonts/tfm/public/libertine"),
            ("fonts/vf/public/libertine", "fonts/vf/public/libertine"),
            ("fonts/enc/dvips/libertine", "fonts/enc/dvips/libertine"),
            ("fonts/map/dvips/libertine", "fonts/map/dvips/libertine"),
            ("tex/latex/libertine", "tex/latex/libertine"),
        ],
    },
    "fontawesome5": {
        "version": "5.15.4",
        "revision": 77682,
        "license": "OFL-1.1 / LPPL-1.3c",
        "ctan_path": "/fonts/fontawesome5",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/fontawesome5.tar.xz",
        "upstream_sha256": "dc0df9192cdb088eb7bca42753128b0c464e61836f02b1fe5dc994c528a51ea4",
        "upstream_size_bytes": 866328,
        "description": "Font Awesome 5 with LaTeX support: Type 1, OpenType, encodings, and maps",
        "source_obligations": "SIL Open Font License 1.1 / LPPL-1.3c",
        "license_files": [],
        "map_files": ["fonts/map/dvips/fontawesome5/fontawesome5.map"],
        "tds_dirs": [
            ("fonts/type1/public/fontawesome5", "fonts/type1/public/fontawesome5"),
            ("fonts/opentype/public/fontawesome5", "fonts/opentype/public/fontawesome5"),
            ("fonts/tfm/public/fontawesome5", "fonts/tfm/public/fontawesome5"),
            ("fonts/enc/dvips/fontawesome5", "fonts/enc/dvips/fontawesome5"),
            ("fonts/map/dvips/fontawesome5", "fonts/map/dvips/fontawesome5"),
            ("tex/latex/fontawesome5", "tex/latex/fontawesome5"),
        ],
    },
    "stix": {
        "version": "1.1.3",
        "revision": 78101,
        "license": "OFL-1.1 / LPPL-1.3",
        "ctan_path": "/fonts/stix",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/stix.tar.xz",
        "upstream_sha256": "58f34c42f0321e505cccfbdefb34db9bab372e8dbba9a1fdcab02d0f64f30752",
        "upstream_size_bytes": 2595416,
        "description": "Scientific and Technical Information Exchange (STIX) fonts: Type 1, OpenType, metrics, maps",
        "source_obligations": "SIL Open Font License 1.1 / LPPL-1.3; corresponding source in stix.source.tar.xz",
        "license_files": [],
        "map_files": ["fonts/map/dvips/stix/stix.map"],
        "tds_dirs": [
            ("fonts/type1/public/stix", "fonts/type1/public/stix"),
            ("fonts/opentype/public/stix", "fonts/opentype/public/stix"),
            ("fonts/tfm/public/stix", "fonts/tfm/public/stix"),
            ("fonts/vf/public/stix", "fonts/vf/public/stix"),
            ("fonts/enc/dvips/stix", "fonts/enc/dvips/stix"),
            ("fonts/map/dvips/stix", "fonts/map/dvips/stix"),
            ("tex/latex/stix", "tex/latex/stix"),
        ],
    },
    "mathdesign": {
        "version": "2.31",
        "revision": 31639,
        "license": "GPL",
        "ctan_path": "/fonts/mathdesign",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/mathdesign.tar.xz",
        "upstream_sha256": "6dca88771e554da97364802f2cfc7827d1e26b7c712752dc8b629a64eb050cef",
        "upstream_size_bytes": 2170832,
        "description": "Mathematical fonts to fit with particular text fonts (mdput, mdbch, mdugm Type 1, metrics, maps)",
        "source_obligations": "GNU General Public License; Paul Pichaureau; corresponding source in mathdesign.zip",
        "license_files": ["doc/fonts/mathdesign/README"],
        "map_files": [
            "fonts/map/dvips/mathdesign/mdbch.map",
            "fonts/map/dvips/mathdesign/mdput.map",
            "fonts/map/dvips/mathdesign/mdugm.map",
        ],
        "tds_dirs": [
            ("fonts/type1/public/mathdesign", "fonts/type1/public/mathdesign"),
            ("fonts/tfm/public/mathdesign", "fonts/tfm/public/mathdesign"),
            ("fonts/vf/public/mathdesign", "fonts/vf/public/mathdesign"),
            ("fonts/afm/public/mathdesign", "fonts/afm/public/mathdesign"),
            ("fonts/enc/dvips/mathdesign", "fonts/enc/dvips/mathdesign"),
            ("fonts/map/dvips/mathdesign", "fonts/map/dvips/mathdesign"),
            ("tex/latex/mathdesign", "tex/latex/mathdesign"),
        ],
    },
    "latex-base-legal": {
        "version": "2024",
        "revision": 76924,
        "license": "LPPL-1.3c",
        "ctan_path": "/macros/latex/base",
        "upstream_url": "https://mirrors.ctan.org/macros/latex/base.zip",
        "upstream_sha256": "",
        "upstream_size_bytes": 0,
        "description": "LaTeX base distribution legal notices, manifest, and license text",
        "source_obligations": "LPPL-1.3c; bundled to satisfy derived TU adapter distribution obligations",
        "license_files": [
            "doc/latex/base/manifest.txt",
            "doc/latex/base/legal.txt",
            "doc/latex/base/lppl.txt",
            "doc/latex/base/README.md",
            "doc/fonts/NOTICES-FONTS.txt",
        ],
        "map_files": [],
        "tds_dirs": [],
    },
    'arev': {
        'ctan_path': '/fonts/arev',
        'description': 'Fonts and LaTeX support files for Arev Sans',
        'license': 'lppl1.3a',
        'license_files': ['doc/fonts/arev/ArevSansLicense.txt', 'doc/fonts/arev/BitstreamVeraLicense.txt', 'doc/fonts/arev/README'],
        'map_files': ['fonts/map/dvips/arev/arev.map'],
        'revision': 79618,
        'source_obligations': 'lppl1.3a; official CTAN distribution',
        'tds_dirs': [('fonts/afm/public/arev', 'fonts/afm/public/arev'), ('fonts/enc/dvips/arev', 'fonts/enc/dvips/arev'), ('fonts/map/dvips/arev', 'fonts/map/dvips/arev'), ('fonts/tfm/public/arev', 'fonts/tfm/public/arev'), ('fonts/type1/public/arev', 'fonts/type1/public/arev'), ('fonts/vf/public/arev', 'fonts/vf/public/arev'), ('tex/latex/arev', 'tex/latex/arev')],
        'upstream_sha256': 'e765d7c7a83a752adcf3212a48fc4a07240cf7cddfe9edb6449f2abb829eee7c',
        'upstream_size_bytes': 964648,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/arev.tar.xz',
        'version': '2024',
    },
    'baskervaldx': {
        'ctan_path': '/fonts/baskervaldx',
        'description': 'Extension and modification of BaskervaldADF with LaTeX support',
        'license': 'gpl2+ lppl1.3',
        'license_files': ['doc/fonts/baskervaldx/COPYING', 'doc/fonts/baskervaldx/README'],
        'map_files': ['fonts/map/dvips/baskervaldx/Baskervaldx.map'],
        'revision': 78931,
        'source_obligations': 'gpl2+ lppl1.3; official CTAN distribution',
        'tds_dirs': [('fonts/afm/public/baskervaldx', 'fonts/afm/public/baskervaldx'), ('fonts/enc/dvips/baskervaldx', 'fonts/enc/dvips/baskervaldx'), ('fonts/map/dvips/baskervaldx', 'fonts/map/dvips/baskervaldx'), ('fonts/opentype/public/baskervaldx', 'fonts/opentype/public/baskervaldx'), ('fonts/tfm/public/baskervaldx', 'fonts/tfm/public/baskervaldx'), ('fonts/type1/public/baskervaldx', 'fonts/type1/public/baskervaldx'), ('fonts/vf/public/baskervaldx', 'fonts/vf/public/baskervaldx'), ('tex/latex/baskervaldx', 'tex/latex/baskervaldx')],
        'upstream_sha256': 'ad08b214d7cfe5cab350e52c347f22b7e06ddaf1200d0321eda69d756bf65ac0',
        'upstream_size_bytes': 680416,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/baskervaldx.tar.xz',
        'version': '1.08',
    },
    'bbm': {
        'ctan_path': '/fonts/cm/bbm',
        'description': '"Blackboard-style" cm fonts',
        'license': 'other-free',
        'license_files': ['doc/fonts/bbm/README'],
        'map_files': [],
        'revision': 77682,
        'source_obligations': 'other-free; authentic METAFONT source archive',
        'tds_dirs': [('fonts/source/public/bbm', 'fonts/source/public/bbm'), ('fonts/tfm/public/bbm', 'fonts/tfm/public/bbm')],
        'upstream_sha256': '5684bfe87f5153ad9a05fb6c3df58ea06d6d0a3bee747c07f891b3206398bc66',
        'upstream_size_bytes': 32608,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/bbm.tar.xz',
        'version': '2024',
    },
    'boondox': {
        'ctan_path': '/fonts/boondox',
        'description': 'Mathematical alphabets derived from the STIX fonts',
        'license': 'ofl lppl1.1',
        'license_files': ['doc/fonts/boondox/README'],
        'map_files': ['fonts/map/dvips/boondox/boondox.map'],
        'revision': 79618,
        'source_obligations': 'ofl lppl1.1; official CTAN distribution',
        'tds_dirs': [('fonts/map/dvips/boondox', 'fonts/map/dvips/boondox'), ('fonts/tfm/public/boondox', 'fonts/tfm/public/boondox'), ('fonts/type1/public/boondox', 'fonts/type1/public/boondox'), ('fonts/vf/public/boondox', 'fonts/vf/public/boondox'), ('tex/latex/boondox', 'tex/latex/boondox')],
        'upstream_sha256': '8cba8ce1f30d8e7a471a4f755a9adbf6db09009aa19a501fd5e7b07b393a73a9',
        'upstream_size_bytes': 204704,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/boondox.tar.xz',
        'version': '1.02d',
    },
    'cabin': {
        'ctan_path': '/fonts/cabin',
        'description': 'A humanist Sans Serif font, with LaTeX support',
        'license': 'ofl lppl',
        'license_files': ['doc/fonts/cabin/OFL.txt', 'doc/fonts/cabin/README'],
        'map_files': ['fonts/map/dvips/cabin/cabin.map'],
        'revision': 77682,
        'source_obligations': 'ofl lppl; official CTAN distribution',
        'tds_dirs': [('fonts/enc/dvips/cabin', 'fonts/enc/dvips/cabin'), ('fonts/map/dvips/cabin', 'fonts/map/dvips/cabin'), ('fonts/opentype/impallari/cabin', 'fonts/opentype/impallari/cabin'), ('fonts/tfm/impallari/cabin', 'fonts/tfm/impallari/cabin'), ('fonts/type1/impallari/cabin', 'fonts/type1/impallari/cabin'), ('fonts/vf/impallari/cabin', 'fonts/vf/impallari/cabin'), ('tex/latex/cabin', 'tex/latex/cabin')],
        'upstream_sha256': 'b29c8cf5cd39e208b3ca707ea88cea2cb5d9286c656ae936c0389f757f860e1b',
        'upstream_size_bytes': 2896428,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/cabin.tar.xz',
        'version': '2024',
    },
    'cjk-ko': {
        'ctan_path': '/language/korean/cjk-ko',
        'description': 'Korean TeX (ko.TeX) macros and kotex.sty package for CJKutf8 and Korean document support',
        'license': 'LPPL-1.3c / GPL-2.0 / Public Domain',
        'license_files': [],
        'map_files': [],
        'revision': 70300,
        'source_obligations': 'LPPL-1.3c / GPL-2.0; corresponding upstream source archive preserved in cjk-ko.zip',
        'tds_dirs': [('tex/latex/cjk-ko', 'tex/latex/cjk-ko')],
        'upstream_sha256': '026b8f250c5045fd4e5f760b4f201b225f95194cfb89f69286614485ef3a4f35',
        'upstream_size_bytes': 8840,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/cjk-ko.tar.xz',
        'version': '2.5',
    },
    'cm-mf-extra-bold': {
        'ctan_path': '/fonts/cm/mf-extra/bold',
        'description': 'Extra Metafont files for CM',
        'license': 'gpl pd',
        'license_files': [],
        'map_files': [],
        'revision': 54512,
        'source_obligations': 'gpl pd; authentic METAFONT source archive',
        'tds_dirs': [('fonts/source/public/cm-mf-extra-bold', 'fonts/source/public/cm-mf-extra-bold'), ('fonts/tfm/public/cm-mf-extra-bold', 'fonts/tfm/public/cm-mf-extra-bold')],
        'upstream_sha256': 'b65fca9dfff9d0b22c6a55e28b2fc4c2071b17d9b0c7674eb26cbdd06f144dab',
        'upstream_size_bytes': 4896,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/cm-mf-extra-bold.tar.xz',
        'version': '2024',
    },
    'cmcyr': {
        'ctan_path': '/fonts/cyrillic/cmcyr',
        'description': 'Computer Modern fonts with cyrillic extensions',
        'license': 'pd',
        'license_files': [],
        'map_files': [],
        'revision': 68681,
        'source_obligations': 'pd; authentic METAFONT source archive',
        'tds_dirs': [('fonts/map/dvips/cmcyr', 'fonts/map/dvips/cmcyr'), ('fonts/source/public/cmcyr', 'fonts/source/public/cmcyr'), ('fonts/tfm/public/cmcyr', 'fonts/tfm/public/cmcyr'), ('fonts/type1/public/cmcyr', 'fonts/type1/public/cmcyr'), ('fonts/vf/public/cmcyr', 'fonts/vf/public/cmcyr')],
        'upstream_sha256': '263793446f5389af3e61174db0c4b7600c109210443a948c3f28923258f0d90a',
        'upstream_size_bytes': 901468,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/cmcyr.tar.xz',
        'version': '2024',
    },
    'dutchcal': {
        'ctan_path': '/fonts/dutchcal',
        'description': 'A reworking of ESSTIX13, adding a bold version',
        'license': 'lppl',
        'license_files': ['doc/fonts/dutchcal/README'],
        'map_files': ['fonts/map/dvips/dutchcal/dutchcal.map'],
        'revision': 77682,
        'source_obligations': 'lppl; official CTAN distribution',
        'tds_dirs': [('fonts/afm/public/dutchcal', 'fonts/afm/public/dutchcal'), ('fonts/map/dvips/dutchcal', 'fonts/map/dvips/dutchcal'), ('fonts/tfm/public/dutchcal', 'fonts/tfm/public/dutchcal'), ('fonts/type1/public/dutchcal', 'fonts/type1/public/dutchcal'), ('fonts/vf/public/dutchcal', 'fonts/vf/public/dutchcal'), ('tex/latex/dutchcal', 'tex/latex/dutchcal')],
        'upstream_sha256': 'cb7ddc48e0d2d5153a6715d5c333554b981d0233074481bf444f162b4c1af1c4',
        'upstream_size_bytes': 36192,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/dutchcal.tar.xz',
        'version': '1.0',
    },
    'ebgaramond': {
        'ctan_path': '/fonts/ebgaramond',
        'description': 'LaTeX support for EBGaramond fonts',
        'license': 'ofl lppl',
        'license_files': ['doc/fonts/ebgaramond/OFL.txt', 'doc/fonts/ebgaramond/README'],
        'map_files': ['fonts/map/dvips/ebgaramond/EBGaramond.map'],
        'revision': 78251,
        'source_obligations': 'ofl lppl; official CTAN distribution',
        'tds_dirs': [('fonts/enc/dvips/ebgaramond', 'fonts/enc/dvips/ebgaramond'), ('fonts/map/dvips/ebgaramond', 'fonts/map/dvips/ebgaramond'), ('fonts/opentype/public/ebgaramond', 'fonts/opentype/public/ebgaramond'), ('fonts/tfm/public/ebgaramond', 'fonts/tfm/public/ebgaramond'), ('fonts/type1/public/ebgaramond', 'fonts/type1/public/ebgaramond'), ('fonts/vf/public/ebgaramond', 'fonts/vf/public/ebgaramond'), ('tex/latex/ebgaramond', 'tex/latex/ebgaramond')],
        'upstream_sha256': '905f7f54b37e065fd8d8be81f8de4becd3b2ccf4920adb852f9721b0b611d278',
        'upstream_size_bytes': 8391124,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ebgaramond.tar.xz',
        'version': '2024',
    },
    'els-cas-templates': {
        'ctan_path': '/macros/latex/contrib/els-cas-templates',
        'description': 'Elsevier CAS journal templates (cas-dc.cls, cas-sc.cls, cas-common.sty) and thumbnail stock icons',
        'license': 'LPPL-1.3c',
        'license_files': [],
        'map_files': [],
        'revision': 71189,
        'source_obligations': 'LPPL-1.3c; corresponding upstream source archive preserved in els-cas-templates.zip',
        'tds_dirs': [('tex/latex/els-cas-templates', 'tex/latex/els-cas-templates'), ('bibtex/bst/els-cas-templates', 'bibtex/bst/els-cas-templates')],
        'upstream_sha256': '7a0503f99e128d0c93b6c3a5b48c244c6a27ed45e5271c339f3162301443d813',
        'upstream_size_bytes': 54876,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/els-cas-templates.tar.xz',
        'version': '2.4',
    },
    'esstix': {
        'ctan_path': '/fonts/esstix',
        'description': 'PostScript versions of the ESSTIX, with macro support',
        'license': 'ofl',
        'license_files': ['doc/fonts/esstix/README'],
        'map_files': ['fonts/map/dvips/esstix/ESSTIX.map'],
        'revision': 77682,
        'source_obligations': 'ofl; official CTAN distribution',
        'tds_dirs': [('fonts/afm/esstix', 'fonts/afm/esstix'), ('fonts/map/dvips/esstix', 'fonts/map/dvips/esstix'), ('fonts/tfm/public/esstix', 'fonts/tfm/public/esstix'), ('fonts/type1/public/esstix', 'fonts/type1/public/esstix'), ('fonts/vf/public/esstix', 'fonts/vf/public/esstix'), ('tex/latex/esstix', 'tex/latex/esstix')],
        'upstream_sha256': '7cbfaa770ce2b8a3345b617f8d0a9829917d2834a6083ec9bf45b9c5638df6f3',
        'upstream_size_bytes': 202188,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/esstix.tar.xz',
        'version': '1.0',
    },
    'esvect': {
        'ctan_path': '/macros/latex/contrib/esvect',
        'description': 'Vector arrows',
        'license': 'gpl',
        'license_files': ['doc/latex/esvect/README'],
        'map_files': ['fonts/map/dvips/esvect/esvect.map'],
        'revision': 77682,
        'source_obligations': 'gpl; official CTAN distribution',
        'tds_dirs': [('fonts/map/dvips/esvect', 'fonts/map/dvips/esvect'), ('fonts/source/public/esvect', 'fonts/source/public/esvect'), ('fonts/tfm/public/esvect', 'fonts/tfm/public/esvect'), ('fonts/type1/public/esvect', 'fonts/type1/public/esvect'), ('tex/latex/esvect', 'tex/latex/esvect')],
        'upstream_sha256': '9cc260745a4c9cb2cc329ab39f4e2ad33a226f508785a282db42ec9f6fd1a431',
        'upstream_size_bytes': 66876,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/esvect.tar.xz',
        'version': '1.3',
    },
    'eurosym': {
        'ctan_path': '/fonts/eurosym',
        'description': 'Metafont and macros for Euro sign',
        'license': 'other-free',
        'license_files': ['doc/fonts/eurosym/COPYING', 'doc/fonts/eurosym/README', 'doc/fonts/eurosym/README.type1'],
        'map_files': ['fonts/map/dvips/eurosym/eurosym.map'],
        'revision': 78101,
        'source_obligations': 'other-free; official CTAN distribution',
        'tds_dirs': [('fonts/map/dvips/eurosym', 'fonts/map/dvips/eurosym'), ('fonts/source/public/eurosym', 'fonts/source/public/eurosym'), ('fonts/tfm/public/eurosym', 'fonts/tfm/public/eurosym'), ('fonts/type1/public/eurosym', 'fonts/type1/public/eurosym'), ('tex/latex/eurosym', 'tex/latex/eurosym')],
        'upstream_sha256': 'db2f7325383f6fa4ed2dc805c432da40fa1ecc742026928001b0cded9b3de07f',
        'upstream_size_bytes': 139868,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/eurosym.tar.xz',
        'version': '1.4-subrfix',
    },
    'fontawesome': {
        'ctan_path': '/fonts/fontawesome',
        'description': 'Font containing web-related icons',
        'license': 'lppl1.3',
        'license_files': ['doc/fonts/fontawesome/README.md'],
        'map_files': ['fonts/map/dvips/fontawesome/fontawesome.map'],
        'revision': 78348,
        'source_obligations': 'lppl1.3; official CTAN distribution',
        'tds_dirs': [('fonts/enc/dvips/fontawesome', 'fonts/enc/dvips/fontawesome'), ('fonts/map/dvips/fontawesome', 'fonts/map/dvips/fontawesome'), ('fonts/opentype/public/fontawesome', 'fonts/opentype/public/fontawesome'), ('fonts/tfm/public/fontawesome', 'fonts/tfm/public/fontawesome'), ('fonts/type1/public/fontawesome', 'fonts/type1/public/fontawesome'), ('tex/latex/fontawesome', 'tex/latex/fontawesome')],
        'upstream_sha256': '98afe54919526e4d98381b3f6e6034fd3ae44c4c6174b17fe603d58206ddbfb0',
        'upstream_size_bytes': 276176,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/fontawesome.tar.xz',
        'version': '4.6.3.2',
    },
    'fpl': {
        'ctan_path': '/fonts/fpl',
        'description': 'SC and OsF fonts for URW Palladio L',
        'license': 'gpl2 lppl1',
        'license_files': ['doc/fonts/fpl/COPYING', 'doc/fonts/fpl/README'],
        'map_files': [],
        'revision': 79618,
        'source_obligations': 'GPL-2.0 with font exception / LPPL-1.0; corresponding source preserved in fpl.source.tar.xz',
        'tds_dirs': [('fonts/afm/public/fpl', 'fonts/afm/public/fpl'), ('fonts/type1/public/fpl', 'fonts/type1/public/fpl')],
        'upstream_sha256': '98ea140d101e0802ff266614d57f486495f1a6d063561d1474e68ac764fbc4aa',
        'upstream_size_bytes': 288420,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/fpl.tar.xz',
        'version': '1.003',
    },
    'heuristica': {
        'ctan_path': '/fonts/heuristica',
        'description': 'Fonts extending Utopia, with LaTeX support files',
        'license': 'ofl lppl1.3',
        'license_files': ['doc/fonts/heuristica/OFL.txt', 'doc/fonts/heuristica/README'],
        'map_files': ['fonts/map/dvips/heuristica/Heuristica.map'],
        'revision': 79618,
        'source_obligations': 'ofl lppl1.3; official CTAN distribution',
        'tds_dirs': [('fonts/enc/dvips/heuristica', 'fonts/enc/dvips/heuristica'), ('fonts/map/dvips/heuristica', 'fonts/map/dvips/heuristica'), ('fonts/opentype/public/heuristica', 'fonts/opentype/public/heuristica'), ('fonts/tfm/public/heuristica', 'fonts/tfm/public/heuristica'), ('fonts/type1/public/heuristica', 'fonts/type1/public/heuristica'), ('fonts/vf/public/heuristica', 'fonts/vf/public/heuristica'), ('tex/latex/heuristica', 'tex/latex/heuristica')],
        'upstream_sha256': 'caea9d54929833f329a68dda0977b88c8ef3b481b4a3137d9a0ce2e10c25db4c',
        'upstream_size_bytes': 1079032,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/heuristica.tar.xz',
        'version': '1.093',
    },
    'hfbright': {
        'ctan_path': '/fonts/ps-type1/hfbright',
        'description': 'The hfbright fonts',
        'license': 'lppl',
        'license_files': ['doc/fonts/hfbright/README'],
        'map_files': ['fonts/map/dvips/hfbright/hfbright.map'],
        'revision': 29349,
        'source_obligations': 'lppl; official CTAN distribution',
        'tds_dirs': [('fonts/afm/public/hfbright', 'fonts/afm/public/hfbright'), ('fonts/enc/dvips/hfbright', 'fonts/enc/dvips/hfbright'), ('fonts/map/dvips/hfbright', 'fonts/map/dvips/hfbright'), ('fonts/type1/public/hfbright', 'fonts/type1/public/hfbright')],
        'upstream_sha256': '6db651416b07dbb7946e46f7bb7ab141d2bb1cf099de3f6daf7c91a95b0269a8',
        'upstream_size_bytes': 832824,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/hfbright.tar.xz',
        'version': '2024',
    },
    'ifsym': {
        'ctan_path': '/fonts/ifsym',
        'description': 'A collection of symbols',
        'license': 'other-free',
        'license_files': [],
        'map_files': [],
        'revision': 77682,
        'source_obligations': 'other-free; authentic METAFONT source archive',
        'tds_dirs': [('fonts/source/public/ifsym', 'fonts/source/public/ifsym'), ('fonts/tfm/public/ifsym', 'fonts/tfm/public/ifsym'), ('tex/latex/ifsym', 'tex/latex/ifsym')],
        'upstream_sha256': 'f29b0f761b885098d8bbe2d770fb23c3d4d75c6a8d263c4179e2432f611df9c8',
        'upstream_size_bytes': 9800,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ifsym.tar.xz',
        'version': '2024',
    },
    'inconsolata': {
        'ctan_path': '/fonts/inconsolata',
        'description': 'A monospaced font, with support files for use with TeX',
        'license': 'ofl apache2 lppl1.3',
        'license_files': ['doc/fonts/inconsolata/OFL.txt', 'doc/fonts/inconsolata/README'],
        'map_files': ['fonts/map/dvips/inconsolata/zi4.map'],
        'revision': 79618,
        'source_obligations': 'ofl apache2 lppl1.3; official CTAN distribution',
        'tds_dirs': [('fonts/enc/dvips/inconsolata', 'fonts/enc/dvips/inconsolata'), ('fonts/map/dvips/inconsolata', 'fonts/map/dvips/inconsolata'), ('fonts/opentype/public/inconsolata', 'fonts/opentype/public/inconsolata'), ('fonts/tfm/public/inconsolata', 'fonts/tfm/public/inconsolata'), ('fonts/type1/public/inconsolata', 'fonts/type1/public/inconsolata'), ('tex/latex/inconsolata', 'tex/latex/inconsolata')],
        'upstream_sha256': 'd3d9d4c42f421588a8f50f34e4aa8d482ec684d8baed536c27b7b3604a7af06e',
        'upstream_size_bytes': 299552,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/inconsolata.tar.xz',
        'version': '1.121',
    },
    'kpfonts': {
        'ctan_path': '/fonts/kpfonts',
        'description': 'A complete set of fonts for text and mathematics',
        'license': 'lppl gpl',
        'license_files': ['doc/fonts/kpfonts/README.txt'],
        'map_files': ['fonts/map/dvips/kpfonts/kpfonts.map'],
        'revision': 77682,
        'source_obligations': 'lppl gpl; official CTAN distribution',
        'tds_dirs': [('fonts/afm/public/kpfonts', 'fonts/afm/public/kpfonts'), ('fonts/enc/dvips/kpfonts', 'fonts/enc/dvips/kpfonts'), ('fonts/map/dvips/kpfonts', 'fonts/map/dvips/kpfonts'), ('fonts/tfm/public/kpfonts', 'fonts/tfm/public/kpfonts'), ('fonts/type1/public/kpfonts', 'fonts/type1/public/kpfonts'), ('fonts/vf/public/kpfonts', 'fonts/vf/public/kpfonts'), ('tex/latex/kpfonts', 'tex/latex/kpfonts')],
        'upstream_sha256': '097726a3219d4725fffc9c900beb47edeefe743ce16cda6d2a5b73c6de3f1c80',
        'upstream_size_bytes': 2241572,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/kpfonts.tar.xz',
        'version': '3.36',
    },
    'marvosym': {
        'ctan_path': '/fonts/marvosym',
        'description': "Martin Vogel's Symbols (marvosym) font",
        'license': 'ofl',
        'license_files': ['doc/fonts/marvosym/OFL.txt', 'doc/fonts/marvosym/README'],
        'map_files': ['fonts/map/dvips/marvosym/marvosym.map'],
        'revision': 79618,
        'source_obligations': 'ofl; official CTAN distribution',
        'tds_dirs': [('fonts/afm/public/marvosym', 'fonts/afm/public/marvosym'), ('fonts/map/dvips/marvosym', 'fonts/map/dvips/marvosym'), ('fonts/tfm/public/marvosym', 'fonts/tfm/public/marvosym'), ('fonts/truetype/public/marvosym', 'fonts/truetype/public/marvosym'), ('fonts/type1/public/marvosym', 'fonts/type1/public/marvosym'), ('tex/latex/marvosym', 'tex/latex/marvosym')],
        'upstream_sha256': 'd8b1740e2c639f3acc6827fd040d14c55d9c3a62c3e8874fa15aed4e1f0c8cf7',
        'upstream_size_bytes': 133504,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/marvosym.tar.xz',
        'version': '2.2a',
    },
    'mathpazo': {
        'ctan_path': '/fonts/mathpazo',
        'description': 'Pazo Math fonts matching Palatino text fonts',
        'license': 'GPL-2.0 with font exception',
        'license_files': ['doc/fonts/mathpazo/README', 'doc/fonts/mathpazo/gpl.txt'],
        'map_files': [],
        'revision': 77682,
        'source_obligations': 'GPL-2.0 with font exception; corresponding source preserved in mathpazo.source.tar.xz',
        'tds_dirs': [('fonts/afm/public/mathpazo', 'fonts/afm/public/mathpazo'), ('fonts/tfm/public/mathpazo', 'fonts/tfm/public/mathpazo'), ('fonts/type1/public/mathpazo', 'fonts/type1/public/mathpazo'), ('fonts/vf/public/mathpazo', 'fonts/vf/public/mathpazo')],
        'upstream_sha256': 'b42822082e609bc2c707bb2657ff5bf6f491718ab391d2c884f7dfed0d51ec15',
        'upstream_size_bytes': 63628,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/mathpazo.tar.xz',
        'version': '1.003',
    },
    'mnsymbol': {
        'ctan_path': '/fonts/mnsymbol',
        'description': 'Mathematical symbol font for Adobe MinionPro',
        'license': 'pd',
        'license_files': ['doc/latex/mnsymbol/README'],
        'map_files': ['fonts/map/dvips/mnsymbol/MnSymbol.map'],
        'revision': 78931,
        'source_obligations': 'pd; official CTAN distribution',
        'tds_dirs': [('fonts/enc/dvips/mnsymbol', 'fonts/enc/dvips/mnsymbol'), ('fonts/map/dvips/mnsymbol', 'fonts/map/dvips/mnsymbol'), ('fonts/map/vtex/mnsymbol', 'fonts/map/vtex/mnsymbol'), ('fonts/opentype/public/mnsymbol', 'fonts/opentype/public/mnsymbol'), ('fonts/source/public/mnsymbol', 'fonts/source/public/mnsymbol'), ('fonts/tfm/public/mnsymbol', 'fonts/tfm/public/mnsymbol'), ('fonts/type1/public/mnsymbol', 'fonts/type1/public/mnsymbol'), ('tex/latex/mnsymbol', 'tex/latex/mnsymbol')],
        'upstream_sha256': '7bdaf593dda367c23342fc4d3c551c736a50ceeb5d28eed99feb9779afd91038',
        'upstream_size_bytes': 4426360,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/mnsymbol.tar.xz',
        'version': '1.4',
    },
    'pxfonts': {
        'ctan_path': '/fonts/pxfonts',
        'description': 'Palatino-like fonts in support of mathematics',
        'license': 'gpl',
        'license_files': [],
        'map_files': ['fonts/map/dvips/pxfonts/pxfonts.map', 'fonts/map/dvips/pxfonts/pxr.map', 'fonts/map/dvips/pxfonts/pxr1.map', 'fonts/map/dvips/pxfonts/pxr2.map', 'fonts/map/dvips/pxfonts/pxr3.map'],
        'revision': 77682,
        'source_obligations': 'gpl; official CTAN distribution',
        'tds_dirs': [('fonts/afm/public/pxfonts', 'fonts/afm/public/pxfonts'), ('fonts/map/dvips/pxfonts', 'fonts/map/dvips/pxfonts'), ('fonts/tfm/public/pxfonts', 'fonts/tfm/public/pxfonts'), ('fonts/type1/public/pxfonts', 'fonts/type1/public/pxfonts'), ('fonts/vf/public/pxfonts', 'fonts/vf/public/pxfonts'), ('tex/latex/pxfonts', 'tex/latex/pxfonts')],
        'upstream_sha256': 'bdddf89946b2a237a75d8d9ee9f2bc6597c868ba2eb73c0a4925a1e631aecd60',
        'upstream_size_bytes': 459976,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/pxfonts.tar.xz',
        'version': '2024',
    },
    'skaknew': {
        'ctan_path': '/fonts/chess/skaknew',
        'description': 'The skak chess fonts redone in Adobe Type 1',
        'license': 'lppl1.2',
        'license_files': ['doc/fonts/skaknew/README'],
        'map_files': ['fonts/map/dvips/skaknew/SkakNew.map'],
        'revision': 79618,
        'source_obligations': 'lppl1.2; official CTAN distribution',
        'tds_dirs': [('fonts/afm/public/skaknew', 'fonts/afm/public/skaknew'), ('fonts/map/dvips/skaknew', 'fonts/map/dvips/skaknew'), ('fonts/opentype/public/skaknew', 'fonts/opentype/public/skaknew'), ('fonts/tfm/public/skaknew', 'fonts/tfm/public/skaknew'), ('fonts/type1/public/skaknew', 'fonts/type1/public/skaknew')],
        'upstream_sha256': '0de354f04a18d3fb99de5e969d693464290b2464c4128a5b3aa6d88b81b47839',
        'upstream_size_bytes': 160044,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/skaknew.tar.xz',
        'version': '2024',
    },
    't2': {
        'ctan_path': '/macros/latex/contrib/t2',
        'description': 'T2 Cyrillic support package providing mathtext.sty, citehack.sty, and misccorr.sty',
        'license': 'LPPL-1.3c',
        'license_files': [],
        'map_files': [],
        'revision': 47870,
        'source_obligations': 'LPPL-1.3c; corresponding upstream source archive preserved in t2.zip',
        'tds_dirs': [('tex/latex/t2', 'tex/latex/t2')],
        'upstream_sha256': 'b58966fcb138e6b0e6f3a59f3b25b2567fcb30fa60f770ae8b61ccff14553b24',
        'upstream_size_bytes': 27048,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/t2.tar.xz',
        'version': '2018',
    },
    'tex-gyre': {
        'ctan_path': '/fonts/tex-gyre',
        'description': 'TeX Fonts extending freely available URW fonts',
        'license': 'gfl',
        'license_files': ['doc/fonts/tex-gyre/GUST-FONT-LICENSE.txt', 'doc/fonts/tex-gyre/README-TeX-Gyre-Adventor.txt', 'doc/fonts/tex-gyre/README-TeX-Gyre-Bonum.txt'],
        'map_files': ['fonts/map/dvips/tex-gyre/qag.map', 'fonts/map/dvips/tex-gyre/qbk.map', 'fonts/map/dvips/tex-gyre/qcr.map', 'fonts/map/dvips/tex-gyre/qcs.map', 'fonts/map/dvips/tex-gyre/qhv.map', 'fonts/map/dvips/tex-gyre/qpl.map', 'fonts/map/dvips/tex-gyre/qtm.map', 'fonts/map/dvips/tex-gyre/qzc.map'],
        'revision': 68624,
        'source_obligations': 'gfl; official CTAN distribution',
        'tds_dirs': [('fonts/afm/public/tex-gyre', 'fonts/afm/public/tex-gyre'), ('fonts/enc/dvips/tex-gyre', 'fonts/enc/dvips/tex-gyre'), ('fonts/map/dvips/tex-gyre', 'fonts/map/dvips/tex-gyre'), ('fonts/opentype/public/tex-gyre', 'fonts/opentype/public/tex-gyre'), ('fonts/tfm/public/tex-gyre', 'fonts/tfm/public/tex-gyre'), ('fonts/type1/public/tex-gyre', 'fonts/type1/public/tex-gyre'), ('tex/latex/tex-gyre', 'tex/latex/tex-gyre')],
        'upstream_sha256': 'dfb4f55c4b02993003777a48663987bd102a3c5a2913172571e05fe2eb18407d',
        'upstream_size_bytes': 7748428,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/tex-gyre.tar.xz',
        'version': '2.501',
    },
    'utfsym': {
        'ctan_path': '/graphics/pgf/contrib/utfsym',
        'description': 'Unicode symbol macro package providing utfsym.sty and 1680 vector TikZ symbol definitions',
        'license': 'CC0-1.0',
        'license_files': [],
        'map_files': [],
        'revision': 63076,
        'source_obligations': 'CC0-1.0 Universal Public Domain Dedication; corresponding source in utfsym.zip',
        'tds_dirs': [('tex/latex/utfsym', 'tex/latex/utfsym')],
        'upstream_sha256': 'f0f94d99e53d285319cec2c045296f7ccb4bc3814083a8de536a9cf82bb401e8',
        'upstream_size_bytes': 1889160,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/utfsym.tar.xz',
        'version': '0.9.0',
    },
    'wasy-type1': {
        'ctan_path': '/fonts/wasy-type1',
        'description': 'Type 1 versions of wasy fonts',
        'license': 'pd',
        'license_files': ['doc/fonts/wasy-type1/README'],
        'map_files': ['fonts/map/dvips/wasy-type1/wasy.map'],
        'revision': 53534,
        'source_obligations': 'pd; official CTAN distribution',
        'tds_dirs': [('fonts/afm/public/wasy-type1', 'fonts/afm/public/wasy-type1'), ('fonts/map/dvips/wasy-type1', 'fonts/map/dvips/wasy-type1'), ('fonts/type1/public/wasy-type1', 'fonts/type1/public/wasy-type1')],
        'upstream_sha256': '1bd86eff809059e4a7c009d8fbe2dd756e568de1d91d6489d82766a1b6f60129',
        'upstream_size_bytes': 261536,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/wasy-type1.tar.xz',
        'version': '001.002',
    },
    'yfonts-t1': {
        'ctan_path': '/fonts/ps-type1/yfonts',
        'description': 'Old German-style fonts, in Adobe type 1 format',
        'license': 'other-free',
        'license_files': ['doc/fonts/yfonts-t1/README'],
        'map_files': ['fonts/map/dvips/yfonts-t1/yfrak.map'],
        'revision': 36013,
        'source_obligations': 'other-free; official CTAN distribution',
        'tds_dirs': [('dvips/yfonts-t1', 'dvips/yfonts-t1'), ('fonts/afm/public/yfonts-t1', 'fonts/afm/public/yfonts-t1'), ('fonts/map/dvips/yfonts-t1', 'fonts/map/dvips/yfonts-t1'), ('fonts/type1/public/yfonts-t1', 'fonts/type1/public/yfonts-t1')],
        'upstream_sha256': 'a62b2726e5d6c68c640863b50eeda748763740b81be48cf2237a705c3ac62528',
        'upstream_size_bytes': 145940,
        'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/yfonts-t1.tar.xz',
        'version': '1.0',
    },
    'bera': {'version': 'r77682',
     'revision': 77682,
     'license': 'Bitstream-Vera',
     'source_obligations': 'Bitstream Vera license; original copyright and trademark notice retained; '
                           'unmodified upstream fonts',
     'license_files': ['doc/fonts/bera/LICENSE'],
     'ctan_path': '/fonts/bera',
     'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/bera.tar.xz',
     'upstream_sha256': 'a3174491eacc119a1747345242c68e35194a28a5af160361ba291ca84429324b',
     'upstream_size_bytes': 312592,
     'description': 'Bera fonts',
     'map_files': ['fonts/map/dvips/bera/bera.map'],
     'tds_dirs': [('fonts/afm/public/bera', 'fonts/afm/public/bera'),
                  ('fonts/map/dvips/bera', 'fonts/map/dvips/bera'),
                  ('fonts/tfm/public/bera', 'fonts/tfm/public/bera'),
                  ('fonts/type1/public/bera', 'fonts/type1/public/bera'),
                  ('fonts/vf/public/bera', 'fonts/vf/public/bera'),
                  ('tex/latex/bera', 'tex/latex/bera')]},
    'ccicons': {'version': '1.6',
     'revision': 77682,
     'license': 'OFL-1.1 / LPPL-1.3c',
     'source_obligations': 'SIL Open Font License for fonts, LPPL-1.3c for macros; upstream source archive '
                           'retained',
     'license_files': ['doc/fonts/ccicons/OFL.txt'],
     'ctan_path': '/fonts/ccicons',
     'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ccicons.tar.xz',
     'upstream_sha256': '26b63ae8eb4718bc8176fa69557309476d2d244d4f938a973dc4170a40dc4a01',
     'upstream_size_bytes': 15584,
     'description': 'LaTeX support for Creative Commons icons',
     'map_files': ['fonts/map/dvips/ccicons/ccicons.map'],
     'tds_dirs': [('fonts/enc/dvips/ccicons', 'fonts/enc/dvips/ccicons'),
                  ('fonts/map/dvips/ccicons', 'fonts/map/dvips/ccicons'),
                  ('fonts/opentype/public/ccicons', 'fonts/opentype/public/ccicons'),
                  ('fonts/tfm/public/ccicons', 'fonts/tfm/public/ccicons'),
                  ('fonts/type1/public/ccicons', 'fonts/type1/public/ccicons'),
                  ('tex/latex/ccicons', 'tex/latex/ccicons')]},
    'doclicense': {'version': '3.3.0',
     'revision': 77682,
     'license': 'CC0-1.0 / LPPL-1.3c',
     'source_obligations': 'CC0 license graphics and texts; LPPL macros with corresponding source archive '
                           'retained',
     'license_files': [],
     'ctan_path': '/macros/latex/contrib/doclicense',
     'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/doclicense.tar.xz',
     'upstream_sha256': '033d4289a33e4b6b2e1c866232f8a7a67b60af83ee14efa25d027aa8b0a97848',
     'upstream_size_bytes': 236980,
     'description': 'Support for putting documents under a license',
     'map_files': [],
     'tds_dirs': [('tex/latex/doclicense', 'tex/latex/doclicense')]},
    'lato': {'version': '3.3',
     'revision': 79618,
     'license': 'OFL-1.1 / LPPL-1.3c',
     'source_obligations': 'SIL Open Font License for unmodified fonts; complete LPPL macro sources included in runtime tree',
     'license_files': ['doc/fonts/lato/README', 'doc/fonts/lato/OFL.txt'],
     'ctan_path': '/fonts/lato',
     'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/lato.tar.xz',
     'upstream_sha256': '76f75b64c2e597c174007c12b51de2a80b47d63bf8738684298b1e23b636a1b0',
     'upstream_size_bytes': 12322520,
     'description': 'Lato font family and LaTeX support',
     'map_files': ['fonts/map/dvips/lato/lato.map'],
     'tds_dirs': [('fonts/enc/dvips/lato', 'fonts/enc/dvips/lato'),
                  ('fonts/map/dvips/lato', 'fonts/map/dvips/lato'),
                  ('fonts/tfm/typoland/lato', 'fonts/tfm/typoland/lato'),
                  ('fonts/truetype/typoland/lato', 'fonts/truetype/typoland/lato'),
                  ('fonts/type1/typoland/lato', 'fonts/type1/typoland/lato'),
                  ('fonts/vf/typoland/lato', 'fonts/vf/typoland/lato'),
                  ('tex/latex/lato', 'tex/latex/lato')]},
    'stix2-type1': {'version': '2.0.2',
     'revision': 79618,
     'license': 'OFL-1.1 / LPPL-1.3',
     'source_obligations': 'SIL Open Font License for unmodified fonts; LPPL support files with corresponding source archive retained',
     'license_files': ['doc/fonts/stix2-type1/README.txt', 'doc/fonts/stix2-type1/OFL.txt'],
     'ctan_path': '/fonts/stix2-type1',
     'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/stix2-type1.tar.xz',
     'upstream_sha256': '6dcfc35bd01aadcfd3113f55bd481e8c65dc93d7e6f6cc8bc4be7f9726fcb9e0',
     'upstream_size_bytes': 3110752,
     'description': 'Type1 versions of the STIX Two OpenType fonts',
     'map_files': ['fonts/map/dvips/stix2-type1/stix2.map'],
     'tds_dirs': [('fonts/enc/dvips/stix2-type1', 'fonts/enc/dvips/stix2-type1'),
                  ('fonts/map/dvips/stix2-type1', 'fonts/map/dvips/stix2-type1'),
                  ('fonts/tfm/public/stix2-type1', 'fonts/tfm/public/stix2-type1'),
                  ('fonts/type1/public/stix2-type1', 'fonts/type1/public/stix2-type1'),
                  ('tex/latex/stix2-type1', 'tex/latex/stix2-type1')]},
    'twemojis': {'version': '1.3.1 (twemoji v14.0.1)',
     'revision': 79618,
     'license': 'CC-BY-4.0 / LPPL-1.3',
     'source_obligations': 'Emoji graphics copyright 2019 Twitter, Inc. and other contributors, CC-BY-4.0; '
                           'LPPL macros with source archive retained',
     'license_files': ['doc/latex/twemojis/NOTICE.txt'],
     'ctan_path': '/macros/latex/contrib/twemojis',
     'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/twemojis.tar.xz',
     'upstream_sha256': '97c49a218e4c9a570dccda5cf1cb74939465a7da81818ab304cc43ceb7b2155e',
     'upstream_size_bytes': 4425052,
     'description': "Use Twitter's open source emojis through LaTeX commands",
     'map_files': [],
     'tds_dirs': [('tex/latex/twemojis', 'tex/latex/twemojis')]},
    'xcharter': {'version': '1.26',
     'revision': 78931,
     'license': 'Bitstream-Charter / LPPL-1.3',
     'source_obligations': 'Original Bitstream Charter copyright and trademark notice retained; complete LPPL macro sources included in runtime tree',
     'license_files': ['doc/fonts/xcharter/README'],
     'ctan_path': '/fonts/xcharter',
     'upstream_url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xcharter.tar.xz',
     'upstream_sha256': '92ae152668b18a271830f2efa4d26792c7dc69cc36ee8f2967695de154f1bf04',
     'upstream_size_bytes': 2210356,
     'description': 'Extension of Bitstream Charter fonts',
     'map_files': ['fonts/map/dvips/xcharter/XCharter.map'],
     'tds_dirs': [('fonts/afm/public/xcharter', 'fonts/afm/public/xcharter'),
                  ('fonts/enc/dvips/xcharter', 'fonts/enc/dvips/xcharter'),
                  ('fonts/map/dvips/xcharter', 'fonts/map/dvips/xcharter'),
                  ('fonts/opentype/public/xcharter', 'fonts/opentype/public/xcharter'),
                  ('fonts/tfm/public/xcharter', 'fonts/tfm/public/xcharter'),
                  ('fonts/type1/public/xcharter', 'fonts/type1/public/xcharter'),
                  ('fonts/vf/public/xcharter', 'fonts/vf/public/xcharter'),
                  ('tex/latex/xcharter', 'tex/latex/xcharter')]},
    "duckuments": {
        "version": "0.5",
        "revision": 77682,
        "license": "LPPL-1.3c",
        "ctan_path": "/macros/latex/contrib/duckuments",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/duckuments.tar.xz",
        "upstream_sha256": "a78498f371b8ec4ed9fe94d43865673af0a77da95b9221ba317070d5ddd65d93",
        "upstream_size_bytes": 484468,
        "description": "Duckuments macros and both bundled example-image PDFs",
        "source_obligations": "LPPL-1.3c; corresponding source archive retained",
        "license_files": [],
        "map_files": [],
        "tds_dirs": [("tex/latex/duckuments", "tex/latex/duckuments")],
    },
    "fdsymbol": {
        "version": "1.0",
        "revision": 77682,
        "license": "OFL-1.1 / LPPL-1.3c",
        "ctan_path": "/fonts/fdsymbol",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/fdsymbol.tar.xz",
        "upstream_sha256": "6c7dd3cfdfb4fe751f8103cf0c9ce6dbb0fcfa8103062e2777fd5b0189a688f9",
        "upstream_size_bytes": 878640,
        "description": "FdSymbol mathematical symbol font family: Type 1, OpenType, metrics, encodings, maps",
        "source_obligations": "SIL Open Font License 1.1 for font files, LPPL-1.3c for LaTeX macros; corresponding source preserved in sources.tar.zst",
        "license_files": [
            "doc/fonts/fdsymbol/OFL.txt",
            "doc/latex/fdsymbol/README.txt",
        ],
        "map_files": ["fonts/map/dvips/fdsymbol/fdsymbol.map"],
        "tds_dirs": [
            ("fonts/enc/dvips/fdsymbol", "fonts/enc/dvips/fdsymbol"),
            ("fonts/map/dvips/fdsymbol", "fonts/map/dvips/fdsymbol"),
            ("fonts/opentype/public/fdsymbol", "fonts/opentype/public/fdsymbol"),
            ("fonts/source/public/fdsymbol", "fonts/source/public/fdsymbol"),
            ("fonts/tfm/public/fdsymbol", "fonts/tfm/public/fdsymbol"),
            ("fonts/type1/public/fdsymbol", "fonts/type1/public/fdsymbol"),
            ("tex/latex/fdsymbol", "tex/latex/fdsymbol"),
        ],
    },
}

# Append-only overlay on the main archive (`packages-supplement.tar.zst`,
# recorded under "supplement_archive" in the lock). The baseline archive kept
# TeX sources and metrics but dropped runtime files that classes and fonts
# load implicitly: beamer's navigation icons (`\pgfdeclareimage` of
# beamericonbook etc.), mwe's example images, tcolorbox's skin textures, and
# the outline and virtual fonts of families whose metrics it kept (newpx),
# and the LuaTeX Lua modules and support packages that LuaLaTeX loads (the
# kernel requires luaotfload-main at format time, which pulls in lualibs, and
# expl3.lua requires lua-uni-data). Only runtime files are taken: luaotfload's
# scripts/luaotfload-tool.lua is the standalone database CLI, not loaded by TeX.
# pdfTeX cannot read EPS, so only the graphics formats it embeds are taken.
# Only files absent from the main archive are added; a basename that the main
# archive already uses for another path is rejected, because lookup is by
# basename. The overlay is read before the main parts, so its consolidated
# pdftex.map supersedes the main one.
SUPPLEMENT_ARCHIVE_NAME = "packages-supplement.tar.zst"
RUNTIME_GRAPHICS = (".pdf", ".png", ".jpg", ".jpeg", ".mps")
SUPPLEMENT_PACKAGES = {
    "beamer": {
        "version": "3.78",
        "revision": 80053,
        "license": "LPPL-1.3c / GPL-2.0-or-later / FDL",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/beamer.tar.xz",
        "upstream_sha256": "a46dedb272225d226bc4ba085351ad38634889d0de6c52e30960dec83bb34168",
        "upstream_size_bytes": 168604,
        "select": [("tex/latex/beamer", RUNTIME_GRAPHICS)],
        "map_files": [],
    },
    "mwe": {
        "version": "0.5",
        "revision": 77682,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/mwe.tar.xz",
        "upstream_sha256": "24c9ac357c80f501f5ffcc5ebd22f81495ef97965bbf82672df39e67b9da32a3",
        "upstream_size_bytes": 807100,
        "select": [("tex/latex/mwe", RUNTIME_GRAPHICS)],
        "map_files": [],
    },
    "tcolorbox": {
        "version": "6.10.0",
        "revision": 79191,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/tcolorbox.tar.xz",
        "upstream_sha256": "fbf38b44bccead42f1e99dfaa38c28272406c7e7fb87799fb7239fe116071306",
        "upstream_size_bytes": 236636,
        "select": [("tex/latex/tcolorbox", RUNTIME_GRAPHICS)],
        "map_files": [],
    },
    # The main archive holds newpx 1.551's metrics, encodings, map and macros
    # byte-identical to this archive; the outlines and virtual fonts complete it.
    "newpx": {
        "version": "1.551",
        "revision": 79618,
        "license": "LPPL-1.3 / GUST-FONT-LICENSE",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/newpx.tar.xz",
        "upstream_sha256": "086ed91b5987062753e9a30c81a6ac1e57d7dad97af67be081379a33c4e7233f",
        "upstream_size_bytes": 4776612,
        "select": [
            ("fonts/type1/public/newpx", None),
            ("fonts/vf/public/newpx", None),
            ("fonts/opentype/public/newpx", None),
        ],
        # tlpobj: `execute addMap newpx.map`
        "map_files": ["fonts/map/dvips/newpx/newpx.map"],
    },
    # The main archive holds rsfso's metrics, map and macros (rsfso.map is a
    # SUPPLEMENT_MAP_ROOTS root); the virtual fonts rsfso{5,7,10}.vf, which
    # slant the rsfs outlines through the rrsfso* base fonts, complete it.
    "rsfso": {
        "version": "1.03",
        "revision": 79618,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/rsfso.tar.xz",
        "upstream_sha256": "020dceed71a12c218a39f00031b625914f95df8a0936e7ee033a5d8499b06007",
        "upstream_size_bytes": 3796,
        "select": [("fonts/vf/public/rsfso", None)],
        "map_files": [],
    },
    "lua-uni-algos": {
        "version": "0.5",
        "revision": 76195,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/lua-uni-algos.tar.xz",
        "upstream_sha256": "a7f9a14f2cb5dedd3d67997d9db3de9cfdef7003a797b33892fa8728578dfb95",
        "upstream_size_bytes": 12332,
        "select": [("tex/luatex/lua-uni-algos", None)],
        "map_files": [],
    },
    "lualibs": {
        "version": "2.76",
        "revision": 78415,
        "license": "GPL-2.0",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/lualibs.tar.xz",
        "upstream_sha256": "578bc9507b0d3e2e039296d87a5f419cef9461c1baedd83277f8fa43af5fb364",
        "upstream_size_bytes": 133324,
        "select": [("tex/luatex/lualibs", None)],
        "map_files": [],
    },
    "luaotfload": {
        "version": "3.29",
        "revision": 74324,
        "license": "GPL-2.0",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/luaotfload.tar.xz",
        "upstream_sha256": "90cee617e8cdb170c64c9acf340ad125fb1d10ab296991086e77f5efde236a9c",
        "upstream_size_bytes": 623572,
        "select": [("tex/luatex/luaotfload", None)],
        "map_files": [],
    },
    "ctablestack": {
        "version": "1.0",
        "revision": 78101,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ctablestack.tar.xz",
        "upstream_sha256": "acf569fb4652f87c01068d6cc29f7d57989a3e00afe0bc1d1f641bd50599713d",
        "upstream_size_bytes": 1112,
        "select": [("tex/luatex/ctablestack", None)],
        "map_files": [],
    },
    "luatexbase": {
        "version": "1.3",
        "revision": 77682,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/luatexbase.tar.xz",
        "upstream_sha256": "1a497d3bd314884a07f4eb60d22d4c3d3496233f6d8e1e89da16fb3ffb86cb65",
        "upstream_size_bytes": 3496,
        "select": [("tex/luatex/luatexbase", None)],
        "map_files": [],
    },
    "luacode": {
        "version": "1.2a",
        "revision": 78415,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/luacode.tar.xz",
        "upstream_sha256": "f8ce2e985588f4b0f708f6e3b13cff8e4f67c5930a8ee9abf6fa888fb81682d1",
        "upstream_size_bytes": 2176,
        "select": [("tex/lualatex/luacode", None)],
        "map_files": [],
    },
    "lualatex-math": {
        "version": "1.12",
        "revision": 77682,
        "license": "LPPL-1.3c",
        "upstream_url": "https://mirror.ctan.org/systems/texlive/tlnet/archive/lualatex-math.tar.xz",
        "upstream_sha256": "244c0a4256ddcfdf3475f798eb98afb7e175c5c7232b4a83b302b8d9bafbcf63",
        "upstream_size_bytes": 3340,
        "select": [("tex/lualatex/lualatex-math", None)],
        "map_files": [],
    },
    "luavlna": {
        "version": "0.1n",
        "revision": 77682,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/luavlna.tar.xz",
        "upstream_sha256": "176cfebacbaa950dec01370d86c68d1c0c8c1ef9db52fcfc4bd991ca3a684e0c",
        "upstream_size_bytes": 9280,
        "select": [("tex/luatex/luavlna", None)],
        "map_files": [],
    },
    # XeTeX (xelatex) runtime fonts. The archive lacked the OpenType files that
    # real XeLaTeX documents load by name: ctex's default fontset, unicode-math's
    # default math font, stix2 and the other maintained math/text OpenType
    # families. Only runtime fonts are taken; each package's license text is
    # shipped from the legal directory at the "notices" destination.
    "fandol": {
        "version": "0.3",
        "revision": 37889,
        "license": "GPL-3.0 with font exception",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/fandol.tar.xz",
        "upstream_sha256": "0b71eebb1c9b9fbd1eb4281f8af3f6c1a556a394ba2b6eae69ed9bb76c5fa56a",
        "upstream_size_bytes": 21582588,
        "description": "Fandol Song, Hei, Kai and Fang OpenType fonts (ctex's default Chinese fonts on Linux)",
        "source_obligations": 'GPL-3.0 with font exception. The OTF files are the upstream preferred form: CTAN and TeX Live distribute no other source for Fandol, so no corresponding-source archive is added to sources.tar.zst; the pinned upstream archive is the source distribution.',
        "select": [("fonts/opentype/public/fandol", (".otf",))],
        "map_files": [],
        "notices": [
            ("fandol-COPYING.txt", "doc/fonts/fandol/COPYING"),
            ("fandol-README.txt", "doc/fonts/fandol/README"),
        ],
    },
    "lm-math": {
        "version": "1.959",
        "revision": 67718,
        "license": "GUST-FONT-LICENSE",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/lm-math.tar.xz",
        "upstream_sha256": "876211c73c151423bb4d1fc17064847e09436ffbfd183d7f0078a1fa8ba9c387",
        "upstream_size_bytes": 377796,
        "description": 'Latin Modern Math OpenType math font (unicode-math default)',
        "source_obligations": 'GUST Font License; modification permitted provided renamed. Unmodified; no separate source obligations.',
        "select": [("fonts/opentype/public/lm-math", (".otf",))],
        "map_files": [],
        "notices": [
            ("lm-math-GUST-FONT-LICENSE.txt", "doc/fonts/lm-math/GUST-FONT-LICENSE.txt"),
            ("lm-math-README.txt", "doc/fonts/lm-math/README-Latin-Modern-Math.txt"),
            ("lm-math-MANIFEST.txt", "doc/fonts/lm-math/MANIFEST-Latin-Modern-Math.txt"),
        ],
    },
    "tex-gyre-math": {
        "version": "2016-05-19",
        "revision": 41264,
        "license": "GUST-FONT-LICENSE / DejaVu (TeX Gyre DejaVu Math)",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/tex-gyre-math.tar.xz",
        "upstream_sha256": "8972d19fec5a701499fb6edc86f3ac4266343d42140cf6ae27edd44c2e916da3",
        "upstream_size_bytes": 1182712,
        "description": 'TeX Gyre math OpenType fonts',
        "source_obligations": 'GUST Font License (DejaVu license for the DejaVu Math font). Unmodified; no separate source obligations.',
        "select": [("fonts/opentype/public/tex-gyre-math", (".otf",))],
        "map_files": [],
        "notices": [
            ("tex-gyre-math-GUST-FONT-LICENSE.txt", "doc/fonts/tex-gyre-math/GUST-FONT-LICENSE.txt"),
            ("tex-gyre-math-LICENSE-DejaVu.txt", "doc/fonts/tex-gyre-math/LICENSE-DejaVu.txt"),
            ("tex-gyre-math-README.txt", "doc/fonts/tex-gyre-math/README"),
        ],
    },
    "stix2-otf": {
        "version": "2.12",
        "revision": 79618,
        "license": "OFL-1.1",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/stix2-otf.tar.xz",
        "upstream_sha256": "9582ec46aeda3581f9e6a065559f7096872aaa6aad3f76c0aa94937a43c9403e",
        "upstream_size_bytes": 1382988,
        "description": 'STIX Two Text and Math OpenType fonts',
        "source_obligations": 'SIL Open Font License 1.1. Unmodified; no separate source obligations.',
        "select": [("fonts/opentype/public/stix2-otf", (".otf",))],
        "map_files": [],
        "notices": [
            ("stix2-otf-OFL.txt", "doc/fonts/stix2-otf/OFL.txt"),
            ("stix2-otf-README.md", "doc/fonts/stix2-otf/README.md"),
        ],
    },
    "xits": {
        "version": "1.302",
        "revision": 55730,
        "license": "OFL-1.1",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xits.tar.xz",
        "upstream_sha256": "cdc51390b6ad6e1151d0186c5be4b24d8bc910e338fa472db282023825c74409",
        "upstream_size_bytes": 541240,
        "description": 'XITS OpenType text and math fonts',
        "source_obligations": 'SIL Open Font License 1.1. Unmodified; no separate source obligations.',
        "select": [("fonts/opentype/public/xits", (".otf",))],
        "map_files": [],
        "notices": [
            ("xits-OFL.txt", "doc/fonts/xits/OFL.txt"),
            ("xits-README.txt", "doc/fonts/xits/README.txt"),
        ],
    },
    "libertinus-fonts": {
        "version": "7.051",
        "revision": 72484,
        "license": "OFL-1.1",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/libertinus-fonts.tar.xz",
        "upstream_sha256": "376c628a838987fa9ae0ba407a0ed8731063acf911b8efdef917365a01705193",
        "upstream_size_bytes": 1795384,
        "description": 'Libertinus OpenType fonts',
        "source_obligations": 'SIL Open Font License 1.1. Unmodified; no separate source obligations.',
        "select": [("fonts/opentype/public/libertinus-fonts", (".otf",))],
        "map_files": [],
        "notices": [
            ("libertinus-fonts-OFL.txt", "doc/fonts/libertinus-fonts/OFL.txt"),
            ("libertinus-fonts-README.md", "doc/fonts/libertinus-fonts/README.md"),
        ],
    },
    # Computer Modern Unicode (CMU Serif, Sans, Typewriter, ...), the usual
    # fontspec choice for Cyrillic and Greek documents under XeLaTeX and
    # LuaLaTeX. TeX Live enables no map for it (the tlpobj has no addMap), so
    # only the OpenType files are taken.
    "cm-unicode": {
        "version": "0.7.0",
        "revision": 58661,
        "license": "OFL-1.1",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/cm-unicode.tar.xz",
        "upstream_sha256": "d4d050c5613fc47b2722cf2232c2f91b6e08875fc65c72069a151101d4e5ac2d",
        "upstream_size_bytes": 14122248,
        "description": 'Computer Modern Unicode OpenType fonts',
        "source_obligations": 'SIL Open Font License 1.1. Unmodified; no separate source obligations.',
        "select": [("fonts/opentype/public/cm-unicode", (".otf",))],
        "map_files": [],
        "notices": [
            ("cm-unicode-OFL.txt", "doc/fonts/cm-unicode/OFL.txt"),
            ("cm-unicode-README.txt", "doc/fonts/cm-unicode/README"),
        ],
    },
    # XeTeX TECkit mappings (fontspec `Mapping=`, polyglossia/arabxetex digit and
    # transliteration maps) and the XeLaTeX macro packages that the main archive
    # lacks. xetex's tex-text and qx-unicode back `Mapping=tex-text`.
    "xetex": {
        "version": "TeX Live 2026",
        "revision": 80015,
        "license": "X11",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xetex.tar.xz",
        "upstream_sha256": "1916d6706a413a9c711d6012d7396f06cf559497e5fa0288692f9550fb741769",
        "upstream_size_bytes": 7988,
        "select": [("fonts/misc/xetex/fontmapping/base", None)],
        "map_files": [],
    },
    "polyglossia": {
        "version": "2.13",
        "revision": 80238,
        "license": "MIT / LPPL-1.3 / CC0-1.0",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/polyglossia.tar.xz",
        "upstream_sha256": "69022dd99105ad75862dc33b3d6b10c13a9f456f39b58c9aa67384124e25bee5",
        "upstream_size_bytes": 132092,
        # The main archive carries polyglossia 2.9's TeX files (they differ from
        # this revision's); mixing in 2.13's language modules would split the
        # package across versions, so only the digit mappings are taken.
        "select": [("fonts/misc/xetex/fontmapping/polyglossia", None)],
        "map_files": [],
    },
    "arabxetex": {
        "version": "1.2.1",
        "revision": 79618,
        "license": "LPPL-1.3c",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/arabxetex.tar.xz",
        "upstream_sha256": "862b1b7fe698286d46779f4b0b0feeb068334ca4322dc1319dae98c88e8674cf",
        "upstream_size_bytes": 147676,
        "select": [
            ("tex/xelatex/arabxetex", None),
            ("fonts/misc/xetex/fontmapping/arabxetex", None),
        ],
        "map_files": [],
    },
    "bidi": {
        "version": "26.01.08",
        "revision": 77682,
        "license": "LPPL-1.3c",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/bidi.tar.xz",
        "upstream_sha256": "eaa01a43b4a21659a65810c2afbebc232c725ac81b01b66b5b5227499444db79",
        "upstream_size_bytes": 131248,
        "select": [("tex/latex/bidi", None)],
        "map_files": [],
    },
    "xepersian": {
        "version": "26.01.01",
        "revision": 77682,
        "license": "LPPL-1.3c",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xepersian.tar.xz",
        "upstream_sha256": "033dd53ecd3236e9e8a4ac46f4d33f150bc9724c6717d72c18f2079039a0ce0c",
        "upstream_size_bytes": 61040,
        "select": [
            ("tex/latex/xepersian", None),
            ("fonts/misc/xetex/fontmapping/xepersian", None),
        ],
        "map_files": [],
    },
    "xetex-devanagari": {
        "version": "0.5",
        "revision": 34296,
        "license": "LPPL",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xetex-devanagari.tar.xz",
        "upstream_sha256": "2b8ff45641f207deacaa06b1e7c0d0e025f3c8a813829c0ef330b019b35fd2b5",
        "upstream_size_bytes": 12452,
        "select": [("fonts/misc/xetex/fontmapping/xetex-devanagari", (".map", ".tec"))],
        "map_files": [],
    },
    "xetex-itrans": {
        "version": "4.2",
        "revision": 55475,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xetex-itrans.tar.xz",
        "upstream_sha256": "8c20f36ae226d089fee5bf8be80614cb418d8615b2900e8c742a7e3e65180523",
        "upstream_size_bytes": 24624,
        "select": [("fonts/misc/xetex/fontmapping/xetex-itrans", None)],
        "map_files": [],
    },
    "xetex-tibetan": {
        "version": "0.1",
        "revision": 28847,
        "license": "LPPL",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xetex-tibetan.tar.xz",
        "upstream_sha256": "b16ea132c2a42f89dd75c435e5c179cd52148dc68cff514c2be043c68f8f16fe",
        "upstream_size_bytes": 7048,
        "select": [("fonts/misc/xetex/fontmapping/xetex-tibetan", None)],
        "map_files": [],
    },
    "xunicode": {
        "version": "0.981",
        "revision": 77682,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xunicode.tar.xz",
        "upstream_sha256": "e0ab404031563884e3769dda49113ddb363c01172b0008a2efdbc0560420b4db",
        "upstream_size_bytes": 25760,
        "select": [("tex/xelatex/xunicode", None)],
        "map_files": [],
    },
    "xltxtra": {
        "version": "0.7",
        "revision": 77682,
        "license": "LPPL-1.3c",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xltxtra.tar.xz",
        "upstream_sha256": "9a42c28340a004d9cc35b0ca94eb21f09491617a7b4d308760d76d4ede6a99e8",
        "upstream_size_bytes": 2532,
        "select": [("tex/xelatex/xltxtra", None)],
        "map_files": [],
    },
    "xecolor": {
        "version": "0.1",
        "revision": 77682,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xecolor.tar.xz",
        "upstream_sha256": "66d88a80b0528f57237c78e61ee67032db01fa2cffff59ad5918a2213b320311",
        "upstream_size_bytes": 2292,
        "select": [("tex/xelatex/xecolor", None)],
        "map_files": [],
    },
    "mathspec": {
        "version": "0.2b",
        "revision": 79618,
        "license": "LPPL-1.3c",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/mathspec.tar.xz",
        "upstream_sha256": "a4e6fea1fac6ae350d37044ef960fea5a8b49765c7325456c3c1dfd5c0e7898f",
        "upstream_size_bytes": 9728,
        "select": [("tex/xelatex/mathspec", None)],
        "map_files": [],
    },
    "xecyr": {
        "version": "1.2",
        "revision": 77682,
        "license": "LPPL-1.3",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xecyr.tar.xz",
        "upstream_sha256": "593896b80b3a4ac794d6364b6601a3eb709272fe46454d23e784413370339d64",
        "upstream_size_bytes": 3580,
        "select": [("tex/xelatex/xecyr", None)],
        "map_files": [],
    },
    "xetexko": {
        "version": "4.6",
        "revision": 77682,
        "license": "LPPL-1.3c",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/xetexko.tar.xz",
        "upstream_sha256": "c3167cf55470716b18900f08928d7e36419381b7a2511bd86f314b5defc50e30",
        "upstream_size_bytes": 54620,
        "select": [("tex/xetex/xetexko", None)],
        "map_files": [],
    },
    # LuaTeX-ja typesets Japanese under LuaLaTeX (the route for pLaTeX
    # documents, #27). At load time it builds its CID fallback font from the
    # Adobe-Japan1 CMaps and Adobe-Japan1-UCS2, which adobemapping provides;
    # the other orderings' CMaps load only for Chinese and Korean CID fonts.
    "luatexja": {
        "version": "20261029.0",
        "revision": 80852,
        "license": "BSD-3-Clause",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/luatexja.tar.xz",
        "upstream_sha256": "94ab4c9d226fcfe7413c46d049fc8d1f3a5d57533eaa8a1c8f83d21283219108",
        "upstream_size_bytes": 201448,
        "description": "LuaTeX-ja: Japanese typesetting for LuaLaTeX (classes, packages, Lua modules, JFMs)",
        "source_obligations": "BSD-3-Clause; the TeX and Lua files are their own source. The copyright notice ships at the notices destination.",
        "select": [("tex/luatex/luatexja", None)],
        "map_files": [],
        "notices": [
            ("luatexja-COPYING.txt", "doc/luatex/luatexja/COPYING"),
        ],
    },
    # luacas (#30): symbolic computation in LuaLaTeX. The whole runtime tree
    # ships, including `test/luacas-parser.lua`, which `luacas.sty` requires.
    "luacas": {
        "version": "1.0.2",
        "revision": 67247,
        "license": "LPPL-1.3c",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/luacas.tar.xz",
        "upstream_sha256": "ff514cbe687115aab3a82981682f6232a3a20f39fd18fd245e5b157d38172a41",
        "upstream_size_bytes": 63948,
        "description": "luacas: a computer algebra system for LuaLaTeX",
        "select": [("tex/lualatex/luacas", None)],
        "map_files": [],
    },
    "adobemapping": {
        "version": "TeX Live r66552",
        "revision": 66552,
        "license": "BSD-3-Clause",
        "upstream_url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/adobemapping.tar.xz",
        "upstream_sha256": "a387389a11bd1220be3016ef74342bb9a4e1e888cee21379f3aadac786da1da1",
        "upstream_size_bytes": 2178116,
        "description": "Adobe-Japan1 CMaps and the pdf2unicode mappings LuaTeX-ja reads",
        "source_obligations": "BSD-3-Clause; the CMap files are their own source. The copyright notices ship at the notices destinations.",
        "select": [
            ("fonts/cmap/adobemapping/cmap-resources/Adobe-Japan1-7/CMap", None),
            ("fonts/cmap/adobemapping/mapping-resources-pdf/pdf2unicode", None),
        ],
        "map_files": [],
        "notices": [
            ("adobemapping-cmap-resources-LICENSE.md", "doc/fonts/adobemapping/cmap-resources/LICENSE.md"),
            ("adobemapping-mapping-resources-pdf-LICENSE.txt", "doc/fonts/adobemapping/mapping-resources-pdf/LICENSE.txt"),
        ],
    },
}
# TeX Live's hyphenation support as its collection-lang* collections install
# it: hyph-utf8 (encoding conversions, luatex-hyphen.lua), hyphen-base (the
# language.us* heads of the generated configuration files), ukrhyph (the 8-bit
# Ukrainian patterns loadhyph-uk.tex inputs under pdfTeX) and every package
# with an `execute AddHyphen`. Values: (TeX Live revision, catalogue license,
# archive sha256, archive size). Members the main archive already holds at
# the same path are byte-identical and skipped.
HYPHENATION_PINS = {
    "hyph-utf8": (79618, "mit", "bb7ddfa4129050cd5d446ecad5bf033c9be3ee2a5614e0035c344a0323f6d344", 16008),
    "hyphen-base": (78076, "lppl", "fe8136043cf4f3a9b2750066e031fee12e0a2d15724aa484abd0b4ca1fbf6f8b", 22644),
    "ukrhyph": (79618, "lppl1", "a09831d43c83b06f57d37038f96f6459e695c45d4638dd58a267a64e878155e7", 39096),
    "hyphen-arabic": (74115, "lppl", "c356a1bf482003e07e4c4eb06845474470e72fcda1222daf7f87a2f80ed6b38c", 720),
    "hyphen-farsi": (74115, "lppl", "f6c944a5ad26f9536742c801f33356d6a1d5aaa978ee53cd7b96ddbd8d97278a", 740),
    "hyphen-chinese": (78069, "lppl", "e0c60d93c408b3f80ee1eaed33e84475de61ecf29a3225655d609d3edd8353a6", 3224),
    "hyphen-belarusian": (78069, "lppl", "db3de23f3211d4850fb362632cad4964ef38aac284d20a2d05a0572c3398780c", 11972),
    "hyphen-bulgarian": (78069, "lppl", "aef72d8e7c604834cf08b3330744d4b07cc6d8a90891f4a743ac8e365ed13a60", 28228),
    "hyphen-churchslavonic": (78069, "lppl", "8cafbbcb73cbfc01350f7a15daf488879171e325ef4588490a731d631270f82e", 31556),
    "hyphen-kazakh": (78069, "lppl", "984b356501d42d7789b013d0953d6407ef6aea0ce1f8350d3c14145523e52b8c", 4972),
    "hyphen-mongolian": (78069, "lppl", "5f5444918dc76cbd374b60e48f56f17b3cc2139256d418cb131dd15dbe45e511", 10476),
    "hyphen-russian": (78069, "lppl", "0886909c81731d7a51636f831fac58ff19317d2f1f81a87953090bb13aef9a8a", 34300),
    "hyphen-serbian": (78069, "gpl", "f55789b5f4829a5f3e032087b7f3ec1aae94ef98e5d2182b16acfb674857ed22", 25360),
    "hyphen-ukrainian": (78069, "lppl", "b214aea47e659359cbd8abbb83647747cd0f9a9d4accc38a6e8e47e9f9711530", 18720),
    "hyphen-czech": (78069, "lppl", "4fbb69046377ce47a3ca5ba76a4c4d18405ba7e88c52d85d3c334b825ee92c9b", 15760),
    "hyphen-slovak": (78069, "lppl", "315b2acf3226c653bea1bf8f6ade661083eca326db0597b75214d1a480b20ed0", 10824),
    "hyphen-english": (78069, "lppl", "cb22c0c51786d47aff5cef7dc6d0a47ab291d6da88c21ac6a9929f4409a456ab", 41348),
    "hyphen-albanian": (78069, "lppl", "3b98d0de99a1504f8278c2aa66bebb9c28553224bcf8af3b709e5cb086013679", 3364),
    "hyphen-croatian": (78069, "lppl1.3", "10a04b6615509dfbc06d61ee540f07acdd883b3e98d27f92272465cd41f9a5fb", 5568),
    "hyphen-danish": (79618, "lppl1.3", "80a460a696e6794a285358f5c59ed3e5f74ea041cde00b7c8d3130ab751df0d8", 5748),
    "hyphen-dutch": (79618, "lppl1", "2ded1602ae66c64235744ad72db659601026de79ed7e08e1cd8f1e6763fde630", 37984),
    "hyphen-estonian": (78069, "lppl", "c1ccf8dfc934ea708ef9ceea6b255d7acee5754f0f698e42672ebc44fb1ee319", 14584),
    "hyphen-finnish": (78069, "pd", "edb4c8b9b7872f023bef4fb793d51a5926dd2268287c2d0e7f668679db575c5a", 4668),
    "hyphen-friulan": (78069, "lppl", "0fea355457ddb570fd4300a02b5025cac73776de482110bfca477fd5b3c6ab9b", 3788),
    "hyphen-hungarian": (78069, "gpl", "d309b4b6d3f2b5d8940dbea1d9137ab3b405ead29e9b9fb41304f44f79f2d1bb", 285360),
    "hyphen-icelandic": (79618, "lppl1.2", "e1811dadceab064bba9d4a150fe5411741019970142f7dd6f53ef15eb0f40e4a", 19496),
    "hyphen-irish": (78069, "lppl", "48814c1d7e7f62df211fe23118210d3966e74984326b56108df894b2e107da54", 30400),
    "hyphen-kurmanji": (78069, "lppl", "e7214892af8467c869b67c7ddc960e7e512ed35140efa40e6c07c969369dda4b", 2700),
    "hyphen-latin": (79618, "lppl1", "86a7c4156d7ff06791c841bbc794732536d445eacb783742e6728eed5d16f04c", 102028),
    "hyphen-latvian": (78069, "lppl", "190c304cd3a51d7aae91ffc9cd9e315e57bcabc579aa5590b0988952c1c2ab99", 52448),
    "hyphen-lithuanian": (78069, "lppl", "aab9889806e94f9900e20f7ef0443f7e5cb6b6639f70fed5e7fc79d6bd78de97", 7424),
    "hyphen-macedonian": (78069, "lppl", "f3655157a277e2127b5f124045c982492c517d703b8b3162c85a9e8bca23cea2", 5532),
    "hyphen-norwegian": (78069, "lppl", "f0bfac073fae3a7d192e28560882481e07758b77a9834272fc52b1c5eaa59431", 96224),
    "hyphen-occitan": (78069, "lppl", "ebe25efa007bbdeddc595b592e85f020549c24639825d3593dc4b64ca541b6a8", 3304),
    "hyphen-piedmontese": (78069, "lppl", "8369684dedd43e690247e27312d1881f15562696edf09f0baa2761409a8d9325", 3336),
    "hyphen-romanian": (78069, "lppl", "d754e02a910f1bb64b627ffe8e803edf3cbcbadca7d483a1194b3e12931d3804", 4340),
    "hyphen-romansh": (78069, "lppl", "6b686f607e0c55a14c7150c3f8786760411e943e2b6437cafb62aeef63be16e2", 4236),
    "hyphen-slovenian": (78069, "lppl", "c200321919ad5a6690804d602f73d871b839d5cbd3cea20c73ec6442f1360667", 6200),
    "hyphen-swedish": (78069, "lppl", "6773d972a0c3c67209482d44ea9e04db728153a09196f9a4ad3987661da1e0a6", 18240),
    "hyphen-turkish": (78069, "other-free", "ab777d12d352d9dd75c2beb9167f21e4c3870f9413d08236342771474e03af68", 3868),
    "hyphen-uppersorbian": (78069, "lppl", "71f5bb491c3e936387b5a067497b5697354f33dbc6c386098cd33a8aa9a93daf", 5504),
    "hyphen-welsh": (78069, "lppl", "1cce9635790a3c17aa9f0bff3e0f2ff0313c46ea34f7c779320b2eb95103c9f5", 19988),
    "hyphen-basque": (78069, "other-free", "7af159568c7135034573a7280eb92680b28e1c8c4b22410b07322f2352b688c5", 3220),
    "hyphen-french": (78069, "lppl", "57100a3a7072b37a23de7efef01f0b089655ab10ec43b4d84aafcbd5ad690bd3", 11432),
    "dehyph-exptl": (79618, "mit lppl1.3", "ffdcf44d13d6455812c41ccf620ce093b5ac00ea380be6f9ceaf29247f3f4970", 134308),
    "hyphen-german": (78069, "lppl", "2bbfc00f6362b8ff525f902c1b5de47bbbb8cba8840a2ccfaf6591daa748ab12", 229712),
    "hyphen-ancientgreek": (78069, "lppl", "14340cb7c98969d33e7f2b6d0b88a75f166f899d735fdb98c8ae610ffe17ebd4", 38280),
    "hyphen-greek": (78069, "other-free", "c9853da8a044e4d3edbc2b03e0943604076cf369e5be90478e9cc718ef929999", 13296),
    "hyphen-italian": (78069, "lgpl", "6efa7bf159b8bf3297404527716edd84a7032220644edfcd46c62fc9ff07c466", 3508),
    "hyphen-afrikaans": (78069, "lppl", "43ee2d58424311b268fc69cce837376bdd2247dee00ecb06ddc1d331988d4727", 37008),
    "hyphen-armenian": (78069, "lppl", "b14cfa809054b12e1501f53706e5ebb9e8d3e566248bf38c2abc6c126f10acec", 2620),
    "hyphen-coptic": (78069, "lppl", "85810dc53f90de4af96d89c1fa11b84a0b2fbc709c05f52a0cf1d04960c65452", 6148),
    "hyphen-esperanto": (78069, "lppl", "f58ae460bb944545b05ed10d6529e0382b04d74816f1ba5c7b5a385b9c877e16", 10668),
    "hyphen-ethiopic": (78069, "lppl", "839731689a8bb8af7ac2c5126d7f3cfb004d69bf77a5d400428844251e857867", 4460),
    "hyphen-georgian": (78069, "lppl", "94d5c9475969171d9920a25d4ecc5417d6eb9c69792f5540e0d6b0ac5b8c8efa", 11040),
    "hyphen-hebrew": (74032, "lppl", "773bfae22baf211a5985e669aa93c3503ca9f43768bf82fe32b050eebb4aa50b", 720),
    "hyphen-indic": (78069, "lppl", "6292d9acd3acb829216c55cdeeff449c5e9a2408ae2d729ee908c20e3a1be210", 5172),
    "hyphen-indonesian": (78069, "lppl", "cc11c4cf7b567fa9efee20fb58848b75be0f18f54c8093f46092a2ef5a4ea146", 2412),
    "hyphen-interlingua": (78069, "lppl", "599ea6de171629099ddc5c238865b90a9985445055195402e49536b5c359cf84", 2880),
    "hyphen-sanskrit": (78069, "lppl", "1dfec14ef68848d13d027bf16d35266e485f96deba8465f821904a34818dc783", 3596),
    "hyphen-thai": (78069, "lppl", "ba365d1acd79a0574ac76850ec7f2fd5ffe0d4d37dd22a846add0326e413d1cc", 33716),
    "hyphen-turkmen": (78069, "lppl", "7cb65fbd4d185eeeda11d71b2235b1b67cff54d0dae79de470d385ec086d3209", 6284),
    "hyphen-vietnamese": (74032, "lppl", "bb552e50a88ed639f4c86326cb01a3adf1685093e85cd12af2cddfdfb17c6de8", 712),
    "hyphen-polish": (78069, "knuth", "128b68a22215ffc5066a58af9bd3f8fe792412bfd4b5d5203583266048ad8a1e", 12500),
    "hyphen-portuguese": (78069, "lppl", "68031bec21717fe36f442b2af3fa168aa6bfb4c35f981c9e167b276c497e448e", 3772),
    "hyphen-catalan": (78069, "lppl", "f45d9535088ff7549ddd48624c661701fd8c87cad8c41e3a836c54de12515577", 5048),
    "hyphen-galician": (78069, "lppl", "6496a76d5d589c353ce6ec90a45a4f91e96c8bfd858f8c75b74b5e1347b87ccf", 10372),
    "hyphen-spanish": (78069, "mit", "ba1c9340f186573770ad28a513bb4aaa334b7bee50f5f902544f381fb058d835", 16428),
}
# The AddHyphen packages of each collection-lang* collection. The installed
# language.dat, language.def and language.dat.lua concatenate the collections
# alphabetically and each collection's packages in dependency order, which
# fixes the \language numbers the formats assign (polish is 86).
HYPHENATION_COLLECTIONS = [
    ("collection-langarabic", ["hyphen-arabic", "hyphen-farsi"]),
    ("collection-langchinese", ["hyphen-chinese"]),
    ("collection-langcyrillic", [
        "hyphen-belarusian", "hyphen-bulgarian", "hyphen-churchslavonic", "hyphen-kazakh",
        "hyphen-mongolian", "hyphen-russian", "hyphen-serbian", "hyphen-ukrainian",
    ]),
    ("collection-langczechslovak", ["hyphen-czech", "hyphen-slovak"]),
    ("collection-langenglish", ["hyphen-english"]),
    ("collection-langeuropean", [
        "hyphen-albanian", "hyphen-croatian", "hyphen-danish", "hyphen-dutch", "hyphen-estonian",
        "hyphen-finnish", "hyphen-friulan", "hyphen-hungarian", "hyphen-icelandic", "hyphen-irish",
        "hyphen-kurmanji", "hyphen-latin", "hyphen-latvian", "hyphen-lithuanian",
        "hyphen-macedonian", "hyphen-norwegian", "hyphen-occitan", "hyphen-piedmontese",
        "hyphen-romanian", "hyphen-romansh", "hyphen-slovenian", "hyphen-swedish",
        "hyphen-turkish", "hyphen-uppersorbian", "hyphen-welsh",
    ]),
    ("collection-langfrench", ["hyphen-basque", "hyphen-french"]),
    ("collection-langgerman", ["dehyph-exptl", "hyphen-german"]),
    ("collection-langgreek", ["hyphen-ancientgreek", "hyphen-greek"]),
    ("collection-langitalian", ["hyphen-italian"]),
    ("collection-langother", [
        "hyphen-afrikaans", "hyphen-armenian", "hyphen-coptic", "hyphen-esperanto",
        "hyphen-ethiopic", "hyphen-georgian", "hyphen-hebrew", "hyphen-indic", "hyphen-indonesian",
        "hyphen-interlingua", "hyphen-sanskrit", "hyphen-thai", "hyphen-turkmen",
        "hyphen-vietnamese",
    ]),
    ("collection-langpolish", ["hyphen-polish"]),
    ("collection-langportuguese", ["hyphen-portuguese"]),
    ("collection-langspanish", ["hyphen-catalan", "hyphen-galician", "hyphen-spanish"]),
]
for _pkg, (_rev, _license, _sha, _size) in HYPHENATION_PINS.items():
    SUPPLEMENT_PACKAGES[_pkg] = {
        "version": f"TeX Live r{_rev}",
        "revision": _rev,
        "license": _license,
        "upstream_url": f"https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/{_pkg}.tar.xz",
        "upstream_sha256": _sha,
        "upstream_size_bytes": _size,
        # hyph-utf8's tex/luatex/hyph-utf8/etex.src is the LuaTeX plain-format
        # variant, which would shadow tex/plain/etex/etex.src by basename
        "select": [("tex/generic", None)]
        + ([("tex/luatex/hyph-utf8", (".lua",))] if _pkg == "hyph-utf8" else []),
        "map_files": [],
    }
# Pure-macro, Lua, XeTeX, BibTeX and babel-* language definition packages from
# TeX Live that were absent from the pdflatex-era baseline archive.
MACRO_SUPPLEMENT_PREFIXES = (
    "tex/latex", "tex/lualatex", "tex/luatex", "tex/xelatex", "tex/xetex",
    "tex/generic", "tex/plain", "bibtex/bst", "metapost",
)
MACRO_SUPPLEMENT_EXTS = (
    ".sty", ".cls", ".def", ".ldf", ".fd", ".cfg", ".clo", ".tex", ".lua",
    ".ltx", ".bst", ".bbx", ".cbx", ".dbx", ".lbx", ".ini", ".mkii", ".mkiv",
    ".pgf", ".dict", ".dfu", ".cmap", ".csv", ".rtx", ".mp",
) + RUNTIME_GRAPHICS
# Packages whose runtime Lua modules live under `scripts/<pkg>/` on LUAINPUTS.
MACRO_SUPPLEMENT_SCRIPT_PKGS = {
    "barracuda", "cloze", "gitinfo-lua", "lua-placeholders", "luaindex",
    "luamesh", "luasseq", "lyluatex", "pgfmolbio", "placeat", "spelling",
}
MACRO_SUPPLEMENT_PINS = {
    "aalok": (76924, "gpl3+ other-free fdl", "717eea008780f040b3b36dbcc20cee84d977af500ddb0f93564ed86978a7cba4", 6088),
    "abntex2-ufv": (80955, "lppl1.3c", "dd4963134fb16da01ada4d8426e8d7b15199cd8b6ed0ff73bfde29292e96482c", 12388),
    "abntexto-ufc": (80690, "lppl1.3c", "b013e96c2b855656f71fce5ceb3e429682f0d292327440870bfe96788deffc32", 15380),
    "academic-paper-abnt": (80983, "lppl1.3c", "57b984116e3acdf01bc74273f9d61166e76417876d9eecacfb83a0992bdc563f", 6448),
    "addliga": (78793, "pd", "47bdc8befe3b18742986fddd67403744bb4b39b971af105ef4474b4f1e0a5ed5", 784),
    "addtoluatexpath": (73424, "mit", "988511042519ed927fe491b15fda68247c530630ca52a6888a67653d665cb5e1", 2320),
    "aidisclose": (79452, "lppl1.3c", "f0a3ed968ed8c8bcfe5a0bbe86b670d50e5eea1553860929cf7f7631e0346de6", 24952),
    "akshar": (76924, "lppl1.3c", "32ba1108734785b1167b253d51d7ad2d31bf3db825d3c8bf1dea667540ba29f3", 2992),
    "algorithmlite": (80842, "lppl1.3c", "2cced7aaa54d3101f93f93c8510f81ead60fe0293dedcaa11061528073454273", 4040),
    "alpha-persian": (79618, "lppl1.3c", "456c7f6964e58242dd111499a5e7fc7fe35bab04b95e39b6bcab0297179df119", 7424),
    "animategif": (80531, "lppl1.3c", "bac1d70fc0627933f644d8649692c02a884e14049231b1ae9b231b8dcce2b128", 11160),
    "antanilipsum": (77161, "lppl1.3c", "87ba119d451b86b1417c9c0dbe8584a05e5b4de6e8e6d4ba779ad5dc8e2ed4a0", 10396),
    "arabdoc": (79884, "lppl1.3c", "7d5b87a8e410fc4071ce63bd5645fe29883c9f964c7df87df76671f5031b21e6", 5016),
    "arabi-add": (67573, "lppl1.3", "2e2791bd74927d7bcb8e3f4363ef9755dfd9da57a6ecb9434f4cbd6fc9fb4be8", 2576),
    "arabic-book": (59594, "lppl1.3", "75ad94dac016c876140c696bc082bb8d3d8cc089dd58a516a31184d588551ee1", 2868),
    "arabluatex": (79618, "gpl3+ cc-by-sa-4", "ecd0bc23076dcad5a891ada17364496ee9071908688301c74da3619519489a7c", 19828),
    "arabtex-granada": (80539, "lppl1.3c", "6b1ee02683b46a811db0c91d4d95c0c17c3ced49fc81a1223ba515e5935a9525", 2156),
    "arcs2": (80096, "lppl1.3c", "138748f7ebf9cdff37cc6509a7e9b53f0eb2aeede9e7aa8f9f2517a2774b3f8d", 2364),
    "ascii-emoticon": (80095, "lppl1.3c", "46ddcaa2a8e07906f148b2f84daae981698be4d0932048429b20aad5b443d6e6", 4844),
    "atomicreactions": (79355, "lppl1.3c", "4d3e4eeba2862c0d47dfd92d62f4c6f0a2c494acc581b02c6a5ea1ade040eb0f", 4688),
    "atsresume": (80734, "lppl1.3c", "94257b9adc530aec36f9c3caf2a0efafb9aadd35d070e63ad8d12485ccad97af", 5332),
    "attinormativi": (79199, "lppl1.3c", "a4b618577d4b4ba6b89961c1eaf9dda71346bdbeac3c3799166516089237a29d", 5464),
    "autoaffil": (79311, "lppl1.3c", "d784c620c95e70aab2b40b0554c43b0069b7a3c68478379201f2a1e14fc15d03", 3364),
    "autopuncitems": (78415, "mit", "eaf09d9a2d5485f6c9752f6038d1ad4671673bb5014fbbebd767c36c2d9eb3a0", 3076),
    "babel-albanian": (77682, "lppl1.3", "a958c12892684911830d6637b210e7ea11ffeebcfd9d0a3dd39cfe0350e1504f", 2276),
    "babel-azerbaijani": (79618, "lppl1.3", "40c1a7301d1bd6be43792a5d429317ed249350b22f91e092f82b595f85039d53", 3056),
    "babel-belarusian": (49022, "lppl1.3c", "534120ec648c14b89c23c52d55d91ffa9d3f6486d4ad4d685292f15a056f6deb", 5112),
    "babel-bosnian": (77682, "lppl1.3", "14a83ce0f21ed11012544d0d3b70bcf83c0ecbbf822345c1bd2acb8a444cfacc", 2068),
    "babel-breton": (79363, "lppl1.3c", "8887c2faf6fed7ced51355f49bbf07a861fbab304de4bb40096a66662d8da640", 3900),
    "babel-bulgarian": (31902, "lppl1.3", "1e8897dd18e5b00c90abe49c455873daae52e231bb3057e95e9ce1a1b6e2352f", 5808),
    "babel-catalan": (30259, "lppl1.3", "6c15a0e6f6c73b70e0bb649c378fc4c6d0d5908df2b7be26596a3bcff8041c8b", 3444),
    "babel-croatian": (77682, "lppl1.3", "179433947841c069e108ef37a22d18ddf1ac7e40e56e67e31e37f12812c054cd", 2160),
    "babel-czech": (30261, "lppl1.3", "32f57457af647f8a6934138c01e95a8a785b29f66cd58febcc00526d9dbe1cc7", 4880),
    "babel-danish": (77682, "lppl1.3", "d4b56061fa18685934cd78bf25c13ae62cdfd84d90ac57423b06f24fb0451d3b", 2504),
    "babel-dutch": (77682, "lppl1.3", "c9955e9f5613abe9668d57c9b75bb68ecb5807811b3361b0f2828208f40e8664", 2932),
    "babel-esperanto": (77682, "lppl1.3", "b2144f78cf55081322ec62aeb2624875ea3639178fab16abff4a3278e0ae45b9", 2464),
    "babel-estonian": (38064, "lppl1.3", "932d89b3b92b976d7c04ea922d00968e931d49ce67bda767448ca36ac309dcdf", 3040),
    "babel-finnish": (77682, "lppl1.3", "e2dd99486a59e04a4ed468a60494434292dbe5f8931a7d17d201b87ef3907d39", 2432),
    "babel-friulan": (77682, "lppl1.3", "91c82a23820cfc76afa7334feea533b402fa6e59595975bf4ec54950247302f9", 1560),
    "babel-galician": (30270, "lppl1.3", "8bd7427e6f01b873af1f2b534a951a07f65013bf77be6d4b193dc6c18fd71fbe", 7192),
    "babel-georgian": (79618, "lppl1.3", "3e60d916b39448b02b80e286d1c2a84d4bbd9cadc2f084da9b0bdb1d38918de0", 5008),
    "babel-hebrew": (77914, "lppl1.3", "fd32580b1488b024918016a2ae74628d73be4ee800664cb17852a31ff4ee64aa", 14208),
    "babel-hungarian": (80546, "lppl1.3", "89a170ccc2eed521a5658bebd5f5f1d7ac2e0f33a896d409491cd4036d23b1f8", 45872),
    "babel-icelandic": (51551, "lppl1.3", "8764bbe4fe6a57b365d2ea7ce0d0d88b72ababd7559ca57bb194ec060fa48724", 4120),
    "babel-indonesian": (77682, "lppl1.3", "7b4dcd74e544cbbcf18374e3f4737f507f5b41cd0158b9b47d207e8fb14207f8", 2488),
    "babel-interlingua": (77682, "lppl1.3", "2dad9a98183f5b2b51a2474dba2bc7c4f7e0194bf49b9364dd266f789b353dbc", 2240),
    "babel-irish": (77682, "lppl1.3", "7b7de8d4155c7847c25b3e778945283721c3cb5332471ac9f9fe8b066fbf8403", 2260),
    "babel-italian": (77371, "lppl1.3c", "ac5573b2ac94a89c252bf221faf8acc9719023393ad54b2353c6667ab7418dbd", 3944),
    "babel-kurmanji": (30279, "lppl1.3", "4cb11b60777c3005639866f9ee001488fd6ded6bdcaaec2fad0c1ea34c6c6312", 2928),
    "babel-latin": (76176, "lppl1.3", "8b34e1efdb06701ca47c675ab109ccafb50acb801c325fca9bdbee7d773f0fe2", 7420),
    "babel-latvian": (71108, "lppl1.3", "c546c6705e2f82629c9119367a6ee0d9dff66f3a1a00bb5936fd5fab34f6bcb1", 4112),
    "babel-lithuanian": (66513, "lppl1.3", "1b66b4b2e40a8c5af1f551f21f06aa1e3e3d2ad69b6efa19ccc4c88902d1b30f", 2560),
    "babel-macedonian": (39587, "lppl1.3", "4fc5dc707b8b4183dba8e292157adacc8ac578086b14fe6fd49cbf6d1a2da840", 4832),
    "babel-malagasy": (80139, "lppl1.3c", "f05d5d3af620865a007010473f351707cd9df55845e8e891c873fca636906515", 1636),
    "babel-malay": (77682, "lppl1.3", "193bedb0bf80682075670a0249d6b27072379ad8794dc66e841cc09035d36d4a", 2504),
    "babel-norsk": (77682, "lppl1.3", "567810d406d243d6d414dbe2a563eb442f8696fb4412f87ccb29c650af795f4b", 2520),
    "babel-occitan": (39608, "lppl1.3", "c858e4d10d08f874db051b2b0ac0468e3420798281284e0da072204233ae78b1", 2384),
    "babel-piedmontese": (30282, "lppl1.3", "831aa36c09ed3eb20c28ac55699f24f42aaa250a94e54ebe67313647a165094c", 1804),
    "babel-polish": (77682, "lppl1.3", "16be2b9af603ecc6f5655493657cff2e7aa60bd1624a1f21d2e8a89f7dd24144", 3912),
    "babel-romanian": (77682, "lppl1.3", "589309271e244b99b37facbb5cfc4c75c3de0deed1371cacdd328547e531fcb6", 2136),
    "babel-romansh": (77682, "lppl1.3", "6853b959c0de51bc3e12a50ed77a742a3b838c2ed77741757219fcaa9b59b1fb", 1116),
    "babel-samin": (77682, "lppl1.3", "6349430ef2995fa5addf6e2c9bb149214c02d3d321ca83195136567325f5b6e8", 1912),
    "babel-scottish": (77682, "lppl1.3", "c3fc4851654ef653017201dd042571599daaaa65dac54d9af5e8f77493bd7248", 1836),
    "babel-serbian": (64571, "lppl1.3", "21648b726f4e984d7a7e3a4177531e66e9d27e80e44abe3e3d1bfa62a8133e2e", 3360),
    "babel-serbianc": (64588, "lppl1.3", "13266c341a847a61dfd15dd5173507348c6b416cdac2cce2b4b0e7c9a717d56b", 4336),
    "babel-slovak": (30292, "lppl1.3", "9d0ae7157d23c7251e0047f00312f6c8e6bbef13a932453acf0ed2218408f81f", 5432),
    "babel-slovenian": (77682, "lppl1.3", "132c11f090ae4e5b2c7407acd3a4f13519614813a218c840a00f20ee641c33ea", 1992),
    "babel-sorbian": (77682, "lppl1.3", "adfd16a006210352eae2700350ccff99fba3262f2cdf86cc9f9a2a6508ff70f7", 3172),
    "babel-swedish": (77682, "lppl1.3", "580cf4011816d97c166626381905576221e61ed43d534a960aba63dc18d9d2e3", 2712),
    "babel-thai": (30564, "lppl1.3", "7878e1bac5985e7cf2f8f3ada23f7018838418287c32725b4cba789b21a6a5ed", 6376),
    "babel-turkish": (51560, "lppl1.3", "817228c913a735acce183fedc6039eed08fbb496f99b2d68c33298b77526c1e6", 2932),
    "babel-ukrainian": (79184, "lppl1.3c", "84024e3a7c4fc6c2105410bf1aec163b1a41bec43ff2306f61289b04b6b7b1bb", 5248),
    "babel-vietnamese": (39246, "lppl1.3", "9ada0de31bafb7e4356de784773ea0e1936d99f198cdbac1427ca08459a2b429", 2480),
    "babel-welsh": (77682, "lppl1.3", "de8ff865004127c6bc9899851c240105e65198cb8c27233a23413568a890bb61", 1816),
    "babelbidi": (80933, "lppl1.3c", "1c89ad1398ae7b237737b177bc29f887f0f2b47345bd04d189b1b987321245eb", 15028),
    "barracuda": (63708, "gpl2", "06921cb3c3293f4699eaa72c01882afee42fea473b0dba6405ba01e41d2e93d9", 35844),
    "beamer-rl": (79762, "lppl1.3c", "3bcce333ef3d7dc70eb72227cdc1b353d0fe0552a97a9722990fb807fb510e54", 7936),
    "beamercolortheme-chalkboard": (80666, "lppl1.3c", "bf844e32978b3b5ee49821da6cd63d2793f1202353977cdb5ef244cff20269eb", 1680),
    "beamertheme-geode": (79040, "lppl1.3c", "2c619d4508eaad73855522e062c3c2790cca979198e206600d0c12d92f44de2e", 4544),
    "beamertheme-niederrhein": (80954, "lppl1.3c", "74fb2df3af81dc8269bb0fa3117620152e11b03ab4381f04c74f35d915f75fca", 12888),
    "beamertheme-westlake": (79618, "lppl1.3c", "4254854d9b9e37faab0914baae5d97fa0a2f2b83d69ae0ed43842ea01bd00f8e", 2656),
    "bezierplot": (72750, "lppl1.3c", "3c48c0211217009ce2a87944b9537fe73b4c49e2f63a2643f16c165e0e2c0685", 8104),
    "bhcexam": (72638, "lppl1.3", "72bdd735c6ad5e604768b0b19734755f513c35e9cc20858fe38c0cb345e17c09", 3556),
    "biblatex-abs-annote": (78770, "lppl1.3c", "f3deb57f8a17a80533e81c3aacd8ed8e838d8faf32d682502bf38c9bc383921c", 2216),
    "biblatex-accursius": (72942, "lppl1.3", "d3fc5d9c6407654725fe74cfbea070f48ec6c4d63fbd6bb04af5d7fb83dfc71d", 10776),
    "biblatex-ams": (78803, "lppl", "11b1410793f3d335b0f10f6eec6a8afb0dbcb06db26a6ef98e570d3b9b7db514", 1736),
    "biblatex-apa-zh": (79975, "lppl1.3c", "3ace7d50517eae53b023b09799125dfda92be46fc11022604ead613986098ce6", 5264),
    "biblatex-asa": (79900, "lppl1.3c", "8fce4a3908efcda76592a3045bff115a02688f7f209b8743bd71745a505020bc", 3848),
    "biblatex-greifswald": (79143, "mit", "c508b1bd7adfd63b823430c642968bc7aa85c480ec2650efca74ac540d8f9fd0", 1880),
    "bibleref-sbl": (80965, "lppl1.3c", "fe2994cf0f1b996c6dc8af1201531482c645ab7f15f09797a77ad443bbfaabc2", 4048),
    "bibstyles-ru": (80633, "lppl1.3c", "fde34fcf34a841db27fe9a07208e34465762201e8df39f5f2a86a1bb5b83b9ab", 14216),
    "bidi-atbegshi": (62009, "lppl1.3", "1e2a310fd192e47c71574519ef571b8f3e858800dd22938e844d3ad21030d2a6", 1388),
    "bidicontour": (77682, "lppl1.3", "1af1bc5dcf8ee6264d01977e3e08d10bfb6df4df9a0150c047363daf7f521bb9", 1844),
    "bidihl": (77682, "lppl1.3", "ddd883bc3a03053584599cd976db2ca7eb136579547ff3971f03e50b78a9f30e", 2316),
    "bidipagegrid": (77682, "lppl1.3", "e7904ed059d624d7b7f9c6731d33d2baa8262ff127b7a695f7bb32c821d6d2a5", 3256),
    "bidipresentation": (35267, "lppl1.3", "40cdbc1824c051c7f83221e1f86b179fe3a31d2fd7e185a6a28a90635fcbd6ab", 28048),
    "bidishadowtext": (77682, "lppl1.3", "4e5f17ba32775fa0347d47344beca6994e5778db30e3dd21d9a73fbdba6cb898", 1304),
    "blopentype": (69080, "lppl1.3c mit", "af94a873faaf0a89c842dc3adae465a553c3b483dd28bbeac8c3d30c2b6505e9", 16308),
    "bookbangla": (80817, "lppl1.3c", "1d97e1982fc9c4eed5f9a00d3db4e87327afcd2d8a121590b5245cae153fffcb", 8084),
    "booktabstabular": (79618, "lppl1.3c", "c541b11d00cd52c4213d24826065b20db66d5491a214eece5e0c54295680bedd", 1692),
    "boolalg": (80770, "lppl1.3c", "d604a17c17f501bf8359e41c07c2c6051b5c9bfc0383a85a909970dd5fa9899a", 9920),
    "boustrophedon": (79090, "lppl1.3c", "88e9c8e034278a7045ba6f8a3c3ea3fe5a6bf1d3182104118c102f8bf2be1c10", 5804),
    "brandstyler": (79478, "lppl1.3c", "ca673c6968ac68802e6ba4f80f42ed4f90efef503577b1fe5658df14a77d6c35", 2328),
    "bredzenie": (44371, "lppl1.3c", "543029123476957ba63014087bed44a49b714f833506de6d8cf4d52743c00555", 49840),
    "businesscard-qrcode": (76924, "lgpl", "6b154dd86ee8e143bb9d674f4b09d223a95d6b4063e546c5515c9d6ba27ab2be", 3724),
    "calendar-grid": (80528, "lppl1.3c", "f445f272cde981200cf7db24d74456dcea7c7eabead869e97da47da885f1bc64", 4624),
    "callout-itembox": (79339, "mit", "9056ed0e872988c490e19910ec2d13651bed33e6de0be24c04cc7add3e564f2b", 2992),
    "cardgame": (78746, "lppl1.3c", "6c582289488c4c636687ad71f05bb7152376d0194872d0c14871bf26cacd332d", 7428),
    "carom-billiards": (79624, "lppl1.3c", "b46f2cd09f7b0050a63f854c8724bfdb7395b155832c1f6cb72b530952aecdcd", 3356),
    "causalmodels": (80075, "lppl1.3c", "0111144e4745042f018ef1b0cecaf0c330a82a874633f0655043575504ecf371", 2048),
    "chatui": (80052, "lppl", "f019da1d53e634226670750d8c1dfbb5ce8bf789ec3a97fd2b4bfab67974a363", 5140),
    "chemidentifier": (80336, "lppl1.3c", "61f8fba9889f08131051725b4d4c2b7517b3f467d52bfab4f5ec1797d3f9231d", 25828),
    "chempid": (78673, "mit", "e91e012ba4129666ef939cc187ffb7cfb108b65efe1f888f5cb6ab93e4feeff7", 11728),
    "chhaya": (61719, "gpl3+ other-free fdl", "48b55caf648e2c9ebfaca01e7c5a00830c3c3de9f968a86f9be899fbd5dcf4fb", 3408),
    "chickenize": (78415, "lppl1.3", "0890c0f98981195d07a3ed1405903e0a17def890195da21627da96871ce42b14", 14392),
    "churchslavonic": (67474, "mit", "d12147a66e893570e5f7ea969542b76a90c75a7f1aa7916960738165cb4f131e", 8668),
    "clawxiv": (78742, "lppl1.3c", "f209e2f610b3fe4a3f5b279c8071a696e26579e6a5f65143d36d2fc95ff0b873", 1700),
    "cleveref-icelandic": (78578, "lppl1.3c", "59ee89419fc8d1de4024dc6dadd10f9d11829d821bd7ecc13084a997bc595866", 2768),
    "cloze": (75681, "lppl1.3c", "754687a0354ca78532d3958e6f59296c3432b2b6d8aded1757915c57dd074a15", 29856),
    "cnbwp": (69910, "lppl", "d9af8417edf2ac14caca727d769f1be76bbe2b4bf0bf8765982c0d4db13d26a9", 10112),
    "codemwe": (80782, "lppl1.3c", "eb9c59563c4896cbfe1d59a23fd003d875a77c0ed0ef74a9fb848005c98f9ad3", 14892),
    "codicefiscaleitaliano": (29803, "lppl1.3", "588dd43054daa903bd0d65cf457a2405fd99c94d5d680b10b84a69cba9c66863", 1836),
    "combgames-tikz": (80840, "lppl1.3c", "a695d0fca1d0f43f40c0c9f40b3711418253e9ce3ad4a38c0f68ed8d20556fcf", 15780),
    "combofont": (78793, "lppl1.3c", "e1723d1bbb34653ada89ba9643f4125fc2299884f328111419220ab9014d83d7", 1808),
    "comment2tex": (79755, "lppl1.3c", "520db78261b81b44562920d039d02d6a7919112fc20c54c099df14f8ecf495e4", 5904),
    "concrete-vec": (79777, "lppl1.3c", "ea6b1b4115d7577b322994b8c2161e1f193df330a543f6d6aa3151760f4501a2", 3980),
    "corasdiagram": (78538, "mit", "60e8b367ee144ae0567ef2b157d119e2279453e1d3a956a08c255bdc01ef9936", 120672),
    "countwords": (78714, "lppl1.3c", "fcd51d3e3ce7e3db654ed878791268ee2ea0a4ef8fc075f3587e6f2872411e14", 2828),
    "cqubeamer": (54512, "mit cc-by-4", "d203e73cc4b9f38ca4e8fea3c95e1791c02b07b95f240c75cf6a4405d3790211", 3084),
    "csbulletin": (77112, "lppl1.3c", "3e4fc04c65e7617cb1cecf4d9516d30fb341215c454a6f0447934d1a7abff65f", 12956),
    "cssyntax": (79727, "lppl1.3c", "abea8552b40c7731d0acb584fb1143d4b540902b889747bc81e010c9c982e909", 8608),
    "cstypo": (80239, "mit", "4b2691aee0233a7380f97a4d14668d1350d859407d26a94b4c8d5c9b85032139", 2444),
    "cyrplain": (79618, "lppl1.3", "4c90080a4b0d59159b6ea80404b7a1880d50993918c7804f8b79adb17218e648", 8708),
    "data-driven-visualization": (80472, "lppl1.3c", "2365df5b3a19b3ed4f9c39edc4774abb875c9bd3e6272c05f2594e16cdfe0c6a", 4272),
    "datemultical": (80801, "lppl1.3c", "726fa0f27d479a13ffa548d485460a4ec328635b458d9917b68ec94ac250589e", 9848),
    "datestamp": (61719, "gpl3+ fdl", "902ee2861eaa3f84b772ac305a66c08c540e2211c3e6dfdf60d7c1bbcc90fe0a", 1552),
    "delvierre": (78550, "lppl1.3c", "e1b39cfd53374d7d532df76ae71b3a85427bb9e7164ca4bb2fc5653237936ef2", 12012),
    "dental-palmer": (79927, "lppl1.3", "37162d9d78a6e26edd285e8cbca26bdf7b6d5264a2f3379dabb394542a25981b", 6728),
    "dfg-crc": (80710, "lppl1.3c", "8b563f8cc62360ce309f9fa1f06427a53eca15d9449fd956c43d559a2308af18", 73356),
    "dichromacy": (79926, "mit", "c8273dc3291f26ad31b34b28fd5aeed26e1ba210a99ec573664e2bc49cee461d", 9492),
    "dirtreex": (80311, "lppl1.3c", "d2baa89e594a01a23cc698c34ba9e8670839b387f9c9c5e09759f79e0f3b4002", 20776),
    "disser": (43417, "lppl1.3", "43c4efdca3ebd11012704edc43f43fc0e40cb88ce094db0e0f3f727755e25465", 13032),
    "dnsrecord": (78328, "lppl1.3c", "b245054f24bfa6ab5ee499b1ee49d7517665b61a72ea6f01de81b6b5257821ab", 3944),
    "docext": (80999, "lppl1.3c", "390276df1243f8c9ecf8df3e140926308588dc6e66746d92915e59c7374815c1", 3468),
    "docref": (80809, "lppl1.3c", "3333005e5ceae43fa030db84785eac00cd3a1d2acfa28cf5e227bc2f10933632", 11128),
    "domaincoloring": (78793, "lppl1.3", "fed7e9278701d02cd58058f731681044cad188658513fcbafc85643e390c3dc4", 8316),
    "easter": (80102, "gpl3+", "12ffd32df736b7add7e38499e65854bbbb771925f95ba02f4523d7b4d311896f", 2052),
    "ecnuthesis": (79308, "lppl1.3c", "f2a97e1f038e59307e010f4dd3a1d2849bed19e7a9c04c34228ce60b07bf537b", 62480),
    "ekdosis": (79618, "gpl3+ fdl", "dd6bf5afca04b75348781da2ceec6978c3d504ded75f5443f686b0d0a066e8ba", 21696),
    "emojicite": (76924, "lppl1.3c", "3f15ad905f3b1eeb246bd4a8a5cdba987785d0cf86486ce66ce2878d241754a9", 1324),
    "emojinames": (79685, "mit", "06fbe9a5f3be46c3b3c038de64391f0e5d9482e887241cfdce0e3acb490327ea", 17892),
    "enslyonreview": (80254, "lppl1.3c", "af6fcb70dafe8e21df0c47d4d6abf4cb5a54488a12f9c8b682f226be8823274a", 14536),
    "enslyonstage": (80232, "lppl1.3c", "f97c3df907a8b8f92b11438047b6c9a6c2bf2eaf0e2a371dc29cac32b1b126e2", 11424),
    "eskd": (79618, "lppl1.2", "6c9eed93a967fe69ae5bdf93f610c994537ffd862d63952fa4ef86970895eb08", 3744),
    "eskdx": (29235, "lppl1.3", "5b4458b9173ab8b404de9cea60926485a12933e27f939cc53a509063a0259db3", 17252),
    "eurion": (78823, "other-free", "c7b640e5672badc15df84116db9a756c818b4a6248a4b9677f3ffc7829a2ce47", 2872),
    "europasscv2025": (79967, "lppl1.3c", "c306790429b6418a72bb2439d1ae07e722e17313ce8ea63baeaa7e0546711522", 14988),
    "evangelion-jfm": (69751, "mit", "47d94882cfd2790cdcd6b342dca165a621a583062503a4159091fc80e56a6c10", 3468),
    "exam-ltr": (80049, "lppl1.3c", "7d0fca9839c2741bbe3656d5589074989b6e92aba7f56f632f04fda297037c7c", 5748),
    "exam-rtl": (80050, "lppl1.3c", "6b55376da9988a066104f7449a2ac2896fe41bf4af0eaee323523d05a07f2700", 6364),
    "facture": (67538, "cc-by-sa-2", "734b2e009f06f3351a1e7064ba2ff03fe76f9cc41dd1dcbf36d7c6dba70a05cf", 3320),
    "farbe": (75449, "lppl1.3c", "f231166d1cd706e7cd3bbc5831d32673db769d83f3557feb2f4f88f4162f5b72", 14828),
    "fariscovernew": (78508, "lppl1.3c", "36e56195abb83fc857a57d707be66608421b6376eceb03bd2d4e44fcff1a52bb", 5700),
    "fddiagram": (80702, "lppl1.3c", "e95ff5a185206157ba268644069f08f9fe72796d5c157deaadbb68fb7a274cb9", 5556),
    "feupphdteses": (30962, "lppl", "d620ab81701c0872cf74275ba397f2721a94149214e42ca11954783a9b343df7", 6312),
    "finbib": (76790, "other-free", "2fa0f7fb22af065e25f76bfbcfc7aba422de5b594347b124c00bb8aa4e8985d3", 5344),
    "firstindexletter": (79458, "lppl1.3c", "b445682e67eab9882cfe3484ff99eae8c62dd8275a49cda0195c6b49a0f2e1a9", 2236),
    "fits-astro": (79632, "lppl1.3c", "13f6599d08c4bf2d24f8dfdbad1865c0c9f53b3417b5f60d854ecde6decfa762", 1972),
    "fixlatvian": (21631, "lppl1.3", "c947331575c6a177fbaaac3e0fc6ddf38e8bf777f463b6f0bea963d97bf8b54c", 2952),
    "fixltxhyph": (73227, "lppl1.3c", "8acb78457218c6b85f242ffb9b299193d82d7988393c06cead2e09903dfe9740", 1212),
    "florilegium": (81001, "mit lppl1.3c", "27da616ad56aa2e0adb0bac6a50e02a3ad8bc94d5d8f5cfbc5fc497b6fe4adde", 240628),
    "font-change-xetex": (40404, "other-free", "e8a254e69284b2e4f9b7167174ba856cca14dced1f56edec7838358696e03722", 6072),
    "fontbook": (23608, "lppl1.3", "43f5516f0a2d3e4da3099d40cfe1408f913041570deedf1830ae00ba8cdbd212", 1452),
    "fontwrap": (15878, "gpl", "5746e17c35c412783de7e0cdda542594adea56603e4788d8bb080d8ea44e6616", 12740),
    "footnotenosplit": (80732, "lppl1.3c", "60b1becc7a2358f8a54deb414f9728bd14b8bb746ab655da0e21e3d61e087aaf", 2788),
    "fox-listings": (78465, "lppl1.3c", "90f855825bbc1ec74813d1c6d2ae0928237d9c0f7194cee90b6abb8c7713c3e9", 2976),
    "frontespizio": (79618, "lppl1.3", "a11545fc0a64fffa84f16ed5fb938d9043da5ca39ba8002c94d38b2672b067c3", 5444),
    "fxpicture": (80644, "lppl1.3c", "320a732d5c78d961d5aba6dd66a1dfb673ebe335c2d30e0b41243f3a502f8e84", 3112),
    "gbt9704": (79828, "lppl1.3c", "5cd941102f5d34eb5554b1c0e4dbbb1409b1a4d338faf25bcc2923e90c0bfa79", 4904),
    "gene-logic": (79121, "other-free", "6c26582ea80dc4c0b10361a7952fdebeefc3a807e63126e8f0c604596f65e645", 2020),
    "gia-handout": (78927, "lppl1.3", "1f61f110c7cb8777b45b0ebfd38311f0ff7ff85a5ce7e96b033fa7226acae50e", 3456),
    "gitinfo-lua": (78348, "lppl1.3c", "f2e489ed7df00a2595c7fc35bb931f7103b8d3c601cdcdb95cb7b502548b6a9b", 5936),
    "glmatrix": (78601, "bsd3", "bf54bb6bfb9a7795b894bb23b78f7d9f3cee37a05e02d087000221280374eb81", 8976),
    "gost": (76790, "lppl1.3c", "81da065f0350bb0ff4bb136d144d303c2d66585c8c6a998d86838c922b8f0199", 17436),
    "gradbars": (80797, "mit cc-by-4", "ab40e6b6436d4a03a39d6402d1d149cfef282110dc627a99283600dd5adb785f", 56384),
    "graph-theory-symbol": (80310, "lgpl3", "fececa316acb52746c76a63d0afc9649008a67cfe735ac9ff47eff653d72e22d", 3428),
    "gyoudori": (80847, "mit", "3b0cdfca0f4a4c99f3cf64dd32b9d1ce57134a8a48d27c2a4ca18df85784e47d", 12020),
    "handstroke": (79683, "mit", "f7ce86bd99ba0f348658d55556cdf06b80cf6af05eb6fa2e4ed54890d5859642", 2008),
    "hanzibox": (79715, "lppl1.3c", "56baa78562f92fb67dd3b6783d7ee5577b6f64d028464c4a59dfa796471f1dde", 7516),
    "hauthesis": (80537, "lppl1.3c", "50e715044524e1709f0aa320de70bd5a229b3a8abe9dd9c20a910d49b3aa14a3", 6344),
    "hawkdraw": (80731, "lppl1.3c", "9a72e9af518fcf144e84c15fbd92af902a6d020c4162d8cff09b2c51d09d37fb", 30400),
    "hebrew-fonts": (68038, "lppl1.3", "f503115273c1829689faac81a02946300facab9b2327f616bd15cd4be3057fd3", 6368),
    "hexdumptikz": (79446, "lppl1.3c", "624e2ae4961b72751536692947518553ada25d98395a4043a0ed0d399a4a9779", 10300),
    "hicite": (80213, "gpl3", "36c57f8618b0ed35e1fa53cf748a974b9f3302d3f904d95f112a8ca3b07a5265", 66172),
    "hrlatex": (79618, "lppl1.2", "ac81df127ac328059d4f31ada28146f5e7430ddbc959e026be683e12c1424f78", 3512),
    "huaz": (80554, "lppl1.3", "9675f86efa0368648bfe9fd6b4a7e08e55ff5959060139051dbd64c0316fe773", 7904),
    "hulipsum": (77317, "lppl1.3", "e35cfafe6eba5bce932f43297a6be3fbf309029bd90877000ffdd87b4d695298", 55708),
    "hust-beamer": (79729, "cc-by-sa-4 lppl1.3c", "1742dfb29ff357db026dffe34511da0e8dabb8dda720c6a52af7ef9fda22ebb2", 6716),
    "hvarabic": (76924, "lppl1.3c", "2c042a86110cd2e8c86fe9834b21038ba10cbd3328cec28de1a64e10cbb2a4f5", 1472),
    "ideavault": (74773, "lppl1.3c", "7f1a063d2d2442a74d46325b9c6fa78d073e8817a9f92fcf6918dca2b23a55f4", 3276),
    "impe-framework": (80450, "mit", "4525a8ef601c189166a9c7457b6109bf4a3816d4ea8e1fa71ac03422fc923830", 39396),
    "import-citation-cff": (81019, "mit", "db8c9d9e4fb03a59401988a00a343a895f353faee8de082dd43ddb3983e51752", 24164),
    "imsproc": (29803, "lppl1.3", "c05f4f3445944685fdef809b960d7f333490fc54b132bbac21f2369a6072cdae", 14784),
    "infufrgs": (78436, "lppl1.3c", "4d69f8ae466906bbb3e16e9acf72340e2282bd2684ae2cfd6e009594af64eb5c", 9720),
    "innerscript": (75161, "lppl1.3c", "e78c025f95e0d65faa0106bea0b7f283b16ed77511a5515e8bdeb0d4cbec17dc", 3320),
    "integramme": (80072, "lppl1.3c", "6b52277f105920c942064b6f71fb15d094b147462aec44ab215eeffb00a08024", 9184),
    "interchar": (77682, "lppl1.3", "0cea538c05b04e68b79f02ed1a925cdd0b9960ed55a57ee508ba5885a265defd", 5304),
    "interpreter": (78415, "lppl", "ec48aa48d8add5752c85ce35fc56c6587333cf170760bc44fe8449d411a8b40f", 6204),
    "iptconf": (79227, "lppl1.3c", "b7d1d935070888776dc0f2dc2d16fe938523e3ae3a40aaae0365e5e5d38a1ba1", 4992),
    "iran-bibtex": (76790, "lppl1.3c", "ea8eaba9dbf1c565772d3429bf85c8a643a8b0b49c4f36dfa4dde4de698218e4", 14772),
    "itnumpar": (79618, "lppl1.2", "b416cc59fc47b8726720c30d8d652aea72e597931a3470fb41c40bd67983e226", 2256),
    "japanese-mathformulas": (64678, "lppl1.3c", "8c377d0ef0b5e400d73b3d5efdcefab67eb362c901d926167371bf207c00905a", 22132),
    "jiffy": (80266, "lppl1.3c", "d8e8c04da43f1b7139166414fa74e81894fbe4ed1930fc7aa5c6f02abe604b8b", 10100),
    "jouthesis": (78319, "lppl1.3c", "0b7d4eb1301aeb963f46e9119d71fed54ec553d7fa3b5df238b8fbccfd7001a2", 9856),
    "kanaparser": (48052, "bsd", "e07a9f55122a4b4911f069b15acab809635e38929682c55a50b873b5b74ccabe", 4620),
    "kbordermatrix": (79757, "lppl1.3", "bfebbc64ee471d24277cbe66645b7fa2fabe2648db5b7e603229f2a066421da9", 2864),
    "kkluaverb": (79443, "mit", "350ef4c9051635b93880225bbe6bd081c931e9517a26ebd25e761bfb57aa4b36", 7880),
    "kotex-oblivoir": (80209, "lppl1.3c", "cbb93bc6f352c03adc6901795f49118ec66e16d2a452dfbce67f0a3581273eab", 46924),
    "kotex-plain": (63689, "lppl1.3c", "ba63a9341e372ee6e5e8bc4a998c8d03cd4c67ddc0f5b8f8b962bf9a4e350a4e", 9116),
    "kotex-utf": (63690, "lppl1.3c", "adfb66b6280394dca4d7acc24fb9af527fd0bbccda53b9403536d0fdd0cbfc0b", 21076),
    "kurdishlipsum": (79618, "lppl1.2", "e43b5a58ed28f6c9d934ab0eab1d2e1aedb659d5f98168c5406188aeba55b7b5", 34892),
    "latexbangla": (80816, "lppl1.3c", "85f8a50c2c85594fb753947b6d3249173a00ce508d2b0401277043734691002f", 30388),
    "latino-sine-flexione": (69568, "pd", "7fdae5109687df82f30a6a14c190082eab9a33501e1af5ec941fc78442589fe5", 1372),
    "layaureo": (19087, "lppl", "503486424ce8f55bf1bf6f6544191723ad3ca3cfb4da3737114af6594c553987", 1748),
    "lcyw": (15878, "lppl1.3", "b6bbbde5c9c766aa52e6629c4094ca8c974f3feaf8312d9e8ab088427607dd76", 3996),
    "leanify": (81016, "apache2", "bc4bde502164af10a88d14e29b658850bbdea68d75b1b4b63f395a00f827f638", 5068),
    "lecturenotes": (74699, "mit", "e4abbb9f5b7fed52cacd628f9cdf755bd0bb47d37f4b2979b11480b9a3801066", 6756),
    "letgut": (78543, "lppl1.3c", "31ae7d6b5586ac3a4533dd2a45b4124a2ce3ad0adfa4f88a18abe1e95e8b79e0", 64148),
    "ligtype": (78793, "lppl1.3c", "dacd0c1359817f8f7c3215702faa3b9c44ce418d5d6b9053a6a3e0258ae6a65b", 11176),
    "linebreaker": (80709, "lppl1.3", "fc7a8ccc5f7d52418e9d6070d37c1935df59ca567efde4abdf6c1fbb841fc575", 6612),
    "linesys": (80591, "lppl1.3c", "3a20eb3d0bb0948b77c57f601ff2f55b7db8167b4b1cfcbce9c01bd84f3e067d", 18588),
    "linguexx": (80950, "lppl1.3c", "fda9539b77831837e38d8796fbcfebfbd46d208bf53fc911a1e6b23dbcfdb448", 130832),
    "longmath": (78632, "lppl1.3", "81bc55d390198b20bbdea748ff51a3d44697de7f852ab2337783e220e4db9062", 8388),
    "lparse": (78101, "lppl1.3c", "7cc86c23468caaf73ab4eae045eecb387ee6fa8c16944f494ed43792fc694f2a", 4596),
    "ltmermaid": (80756, "lppl1.3c", "23652dcda7fce316c5c5e852098aeb5ac394cd46ca6c5d6d2f660ff057da7f47", 7456),
    "lua-calendrica": (80661, "apache2", "1b1b52b3d1981b31ef20744d452ebc6e9e99093e1ccbb48bcb60032c08521ae4", 65728),
    "lua-check-hyphen": (77682, "mit", "e3be171d6626c501b5a59eb9e5fd18d0a51130af9f35fc9afada4cd4dadd2935", 3616),
    "lua-font-config": (80927, "lppl1.3c gpl2", "51c3cc834385315e36ed3a48d435bddfe7705e3bf6c03f58a49b23f377140139", 32104),
    "lua-list-hyphen": (80068, "lppl1.3c", "05911139900ce6c7b71c86eb04f5793c00a6fd6a0473b14d0461456851a2f8cd", 10096),
    "lua-placeholders": (79056, "lppl1.3c", "7dedf77ad30121a2deb182e5b30e7167c4b9fc70150b48ecc7fb760a9e219e38", 10156),
    "lua-regression": (74969, "lppl1.3", "6023126cff8d042b5fc5a5b92fe23ced689b9e7eb20805e5af4e40fa94de437c", 6764),
    "lua-typo": (77682, "lppl1.3c", "56acc3c423003e2388ff6f98f9076481fd9b0e53174fd21d01d4b00572f79535", 13456),
    "lua-ul": (77682, "lppl1.3c", "0cfb7e07c0ef14d2a99de611e3b240c8a498ff4c3d50377d678212bd10774fd0", 7480),
    "lua-unicode-math": (80357, "lppl1.3c", "c46f1549fef975c82576285118333bfcbf9f66d746a21889ddf23be465bc42d7", 20580),
    "lua-visual-debug": (78797, "mit", "0d5c09575836407df86709ae6122d706341fe0fbdb8ead1b352f05bc007e0910", 4588),
    "lua-widow-control": (77682, "other-free cc-by-sa-4", "bd949713abaf6f45ec25eddd0389b365e655d63348214fe81eb4e23ad91ba994", 19976),
    "luaaddplot": (72350, "lppl1.3", "f8454a50f8a6e06edf25c194c9a34d08441399b239ee857dc5ea1b6f17973229", 1468),
    "luabibentry": (55777, "lppl1.3", "be9d11e72770d7a8f63df3a86ac1cbe7194bad447d9010ab5f09ae3d9e983ea8", 2344),
    "luabidi": (80908, "lppl1.3c mit", "e28ef6d55dfa977ef30502cc8665de1149bc45bdbc8f67618d22d30ba14668c9", 4332),
    "luabreakurl": (79728, "mit", "2d9c69ea8e8a80e4f6469b60bb173aaaf1cc67b966f5f96d468a6e892db1f0d3", 1508),
    "luacomplex": (68883, "lppl1.3c", "9d75a2ebd408218743f0e26aae5dce5eab00071d4048eb3b709d60e572e32eb6", 1708),
    "luacoolprop": (80329, "lppl1.3c", "a9277b3ec24aec4cd002276ecfe6fb294c84c65832e0be4134cfb4438cf218b4", 44932),
    "luadraw": (80996, "lppl1.3c", "e1a422330f24adf87e7909e280f563fa0a2f11ded3abfca40f0ca6e9d7afd01f", 168596),
    "luagcd": (76790, "lppl1.3c", "bd4d30db77566a9caf2c3b4961dffe088095badf15975bd0b0ea9ea46b750ab2", 2620),
    "luahttp": (67348, "lppl1.3c", "c3543629c94c83500d4acb36a56b24d433fe4e4b33b2332f01f9b94683b25e88", 5080),
    "luahyperbolic": (78431, "cc0", "4c61b298051daca2161a14635dc19cf0973f3426454c9472f08c1dff35a0e935", 14608),
    "luahyphenrules": (78101, "lppl1.3", "8b04dc3ec299fd3540e02f4b018e7680980f00f4122d6e62b1373d23687c4120", 2376),
    "luaimageembed": (50788, "mit", "1e97b2c747cf019f5d2b52f97e679fd10d37d232e0fbf3caf7e43b9d6e0c2bf8", 1416),
    "luaindex": (25882, "lppl1.3", "7faff1b42bcc3503261098642b40c680037bdf7bd4fff3a00ef67615f7c32c1e", 4488),
    "luainputenc": (77682, "pd", "9d3e69eaabb02be910d09ba9306230b9615d56f8ab208e74b3308dd71b726428", 5024),
    "luakeys": (78101, "lppl1.3c", "223e05c20a51552dc20dd96a4b85b6ddb8c5ac68f141dbbbf32d6583cccef421", 16704),
    "luakeyval": (79472, "0bsd", "abf68c6a95026c4cf46de629dce95b180ea2f983ea375646e85b122b1d971f1a", 1656),
    "lualatex-truncate": (78632, "lppl1.3c", "0d9c64642c35214f84c32be9ca3c6021c75c83770426bd98cd85030306e094fb", 1924),
    "lualinalg": (76924, "lppl1.3c", "8ccf4f8d4d54645d54cdd1c8c888c32d6466349eb9ed7ffdab1ccac2a8e33d4b", 6740),
    "lualineno": (79471, "0bsd", "722b28dfcc682d77366a4ea11dd1001148e2014daac353d1d1bbaae787d93612", 13044),
    "luamathaccents": (80876, "lppl1.3c", "e0796c721c143c234d05635c896c7b5c24be5a960754088fe71b332d755f8eea", 2332),
    "luamathalign": (77682, "lppl1.3c", "e0e1bd5e63a34063bc15189a09565944b63a921a1b67dd17dc71e1fd1681acbc", 4088),
    "luamaths": (76924, "lppl1.3c", "8903029fe110ac1e436f5b87b77469cb7f5b54cd513a92dc4802f77387654fe9", 4436),
    "luamesh": (78415, "lppl1.3", "998d91ac42fe254ac1bd01af749b897231ede80bda7b8e0bf6605a2f47764676", 12892),
    "luamml": (80875, "lppl1.3c", "7f45a2f0f0e7bc6773b680d34013674248e59c639b8061c8557873c30e6e1b2c", 25728),
    "luamodulartables": (68893, "lppl1.3c", "dbd20661f8d4aa18d64227030af235e06472a6e372bbd4957300a462403b3e28", 1384),
    "luamplib": (80726, "gpl2", "2411e228ea0194214020256aecdd45cc57d30c2279f0a73418004c11069948e1", 37100),
    "luanumbers": (79335, "lppl1.3c", "f1539039acb9a8e892981f16e4ea5b10924977e075114833e4421a2a2f690ca3", 5120),
    "luanumint": (68918, "lppl1.3c", "e58c90618c60fa4468a9901dc4a2892853f02dd34a7fafe0785c9349702ad685", 2332),
    "luaoptions": (79068, "mit", "8188cf3f6451dad71bc5fa0542c3664cf1a782c1db182b075018520dc989598f", 6072),
    "luapackageloader": (54779, "lppl1.3c", "6f2f33838aa96987afb2dd155ed593ea8328bf9165b1d8bedfee21b6ef80187f", 1700),
    "luaplot": (68918, "lppl1.3c", "c8b7ea4aebaa34459cb89cc8eb7bd54aa475442956b64de68e012558f6f7d4dd", 2232),
    "luaprogtable": (56113, "mit", "f1492364ede6a89fb2b7101b2fa8faf8cd19579a16014e49f4e4dfdb0d2e5261", 7092),
    "luaquotes": (65652, "lppl1.3c cc-by-sa-3", "d9d2a80ef5ee5c89b258ba21375c48d2f6cf2ea314ccc47f76f4bc5b42c1cdad", 3384),
    "luarandom": (78101, "lppl1.3", "3f41b9da217aa165b7c0195fba31501983d44786b2604bd2ef82a884da1b13a5", 1424),
    "luaset": (76924, "lppl1.3c", "0045e1eecc3b702bf1065fa3ebe061739bd4438020da81047d3ef44c5890d240", 1584),
    "luasseq": (65511, "lppl1.3", "8ec8a6eb930a2e308aa40efd820592fee82a063becab4d2414565f0d0b9ec98e", 10276),
    "luatagging": (80693, "lppl1.3c", "617356d6dc77bd8ec686b80087920d96b2a42469bd64797f32852bc518acda08", 19832),
    "luatbls": (73999, "mit", "8dcdfa117f12d8d3d20405a2217479137e134aea6c5642a88e02b86562a309fd", 5056),
    "luatex-cn": (80858, "apache2", "9eb223e63264bea7410f8a2221ef472ba0154ed900476cc082c4a3aa58b00050", 269476),
    "luatex-type-definitions": (75890, "gpl2+", "69efc87b506637fd61e4bb98fd84683ade99196ea6245107040deff96197327c", 145508),
    "luatexko": (80851, "lppl1.3c", "987f2fcd216b1bd900264501588de3da7853659d46f26c9ec0ceda9517c9ff15", 30360),
    "luatextra": (77682, "pd", "172b50412fa43e5248766ee588bdeef5aaa4d0e3a27f82bfb9af1aa1d6bdc44a", 1044),
    "luatodonotes": (77682, "lppl1.2", "45487f01ffd9e1a0a1cb1635a8ac9c93d6e363acd14c51247d12d76afe52d99f", 26628),
    "luatruthtable": (78415, "lppl1.3c", "42a39da3f46513b8c9e84b568cfd3952e6603946bb80f6e8653b78dd1f931d9a", 2320),
    "luaxml": (78101, "other-free", "956006bd3045b5e949610371604008003554f17ae9daeb17e57952c901313207", 47992),
    "lucidcv": (80907, "lppl1.3c ofl mit", "50fe7731203a7f7ead5bcb66f98add036cab3de9cb74179d46da2d112fedd833", 21752),
    "lucide-admonitions": (78847, "lppl1.3c", "acb5203bfdaca6aba553b8fda2a6733f022c83e6ef52845d1729db6b163209b7", 2816),
    "lutabulartools": (73345, "mit", "6b811cb466850e148f0910ed362f1d3cad71a794860050f888c9433214a4a338", 7532),
    "luwa-ul": (77595, "mit", "2945ee0f93f727e175f115d168889bc40ce8c1406821a7dc7913f97f00a1675d", 4396),
    "lyluatex": (80321, "mit", "b639712d4bd5b7095b059aec61635d6129e925ee3f84a091e5dce122c99c9e52", 17308),
    "macros2e": (77050, "lppl1.3c", "8b8d150837bbdb3290c49c776f5d4d7538262d90940777d69a84d5fbd219791e", 816),
    "malagasy-numberwords": (80200, "lppl1.3c", "a8feaf1b94a83c8c7d15ee897d45902f58279edf3fcd32b874cf17475b7e387d", 6056),
    "marathi": (61719, "gpl3+ other-free fdl", "cba3b29ce443b7b964537c442c5a4161f5063626a076d1544af89afa9f0e0e7e", 5456),
    "marginalia": (80607, "lppl1.3c", "cc9c343c9fde171ca2d8bf3e4a8845867dab692169b6c749b5037bb9035ba14e", 8836),
    "matchedsplit": (80855, "lppl1.3c", "5f5edd9c9c9cdd77c516deaded59316e4b2fc119155d1f64e89df338390c850c", 3780),
    "math-shorthandz": (80643, "lppl1.3c", "a17a70859024c7d11de3872d54420b7e0355d0994200629c60dfdc196df4af3f", 18016),
    "mathaidisclose": (80590, "lppl1.3c", "f977f7a57ee651ef6b3667061e72d16097e6e637bc8353ced413851053fbd468", 2908),
    "mathformule": (79744, "lppl1.3c", "21422bbd39b59465212071a00e29b67ff6f47653171c8ccafbf2ad1c3676485b", 6700),
    "matrixdiagrams": (80697, "lppl1.3c", "c51fa1f6e92dbcdabf654f8a33e4dcc3c1c993c6ab2971d87c2fccd703a31e82", 4664),
    "mayanumber": (80449, "mit", "e44543d66c4ca852ff2fa339ee9c5955ada59bffe7d43a522ef839c059839b86", 3520),
    "metaphor": (78632, "lppl1.3c", "c723af9dcd6cea4cf7a01da9e13ff14c0f2a7fcfb11d16d55c1e3c8fc7e1f880", 1624),
    "minim": (73816, "eupl", "b89f4752e47439c7ca151ab8f013a3b94d2674c92da31095893df05535c9fba4", 25112),
    "minim-math": (73816, "eupl", "dadf32582921f37b5c43df5006a5b16caffc23df2dfbb59ba426c1b1f482e5cd", 34128),
    "minim-mp": (78415, "eupl", "2323dd9849d2a3c757ef09ad5a8b3ff553e811d95b7ad9a9d007eca7f60a3527", 22600),
    "minim-pdf": (77682, "eupl", "81a5da7a4178a500ed58599fdd07420641e731d9c4487197d365332550733ad6", 17620),
    "minim-xmp": (73816, "eupl", "63a1989e6a124fa43e1eb0fe1cfdc18ea96edaa8962f3c2b406b14de8b881874", 6372),
    "mnhyphn": (69727, "lppl1.3", "493dbbced7cc6a491cb657755ede7a04637a6afab3844ee6e13965751e173b94", 2968),
    "modernclassnotes": (79889, "lppl1.3c mit", "3123052a71fa895dfb77f0e11129c091a4cf67e020bf3f98df4255f51847190b", 6044),
    "mohe-book": (74912, "lppl1.3c", "77e6526c3ca6dcad59d6cfd4723272fa1d34608e3805da84f964f84487853e0b", 6088),
    "mohelper": (78603, "gpl3", "c88af5012f7f48a337a605cabf64f12cbc5120d62ec583ae29cc5e602e0050c0", 5712),
    "mongolian-babel": (79618, "lppl1.3", "df95cbe25e7c9cebc8b6930f4548d4b84b70d841958dc41a44245f16a9add9d1", 5276),
    "monoref": (80448, "lppl1.3c", "184594f6e0c6e6aad65ecce506c7e59dc88aa6f6501d56581a78b8763121fc76", 5312),
    "morse-simple": (79670, "lppl1.3", "f8fe88d4769a43dd448456f0b6cd92cc32b3065c6749a6e9a9addf8a1b9e2ba2", 4108),
    "mousik": (80083, "lppl1.3c", "07f167996ca0973a1af79866ec9466a552893605e350b0b943ddb8215052a28f", 14880),
    "mpformulation": (80444, "lppl1.3c", "990acdbda53436f7177fe28222f0d3967d04d6a98b86a3773b3017c5d0fa3d12", 8964),
    "multicoltab": (80234, "lppl1.3c", "897c76b6ab4a25496dcede7d42f1f26f55f68d6bfa4d19416b65f3d7fad583b4", 5384),
    "mwcls": (77050, "lppl1.2", "0456534e05d0e792145f0c55b2cf1e2491e66096485deb4be853227f796923c4", 11192),
    "na-box": (45130, "lppl", "add03b08898ae10e7fd70633c9018fec35cd6a34c3c9548bdb1fe7b54a45de3d", 2952),
    "na-cover": (78994, "lppl1.3c", "c781bcb3d83667d9161a07e6295d6d8851badb52db8fee9e1c4d347fd9e7f21e", 2384),
    "na-fancyborders": (78966, "lppl1.3c", "77cc6896704d9af081c721b363ea4080c3eaea85156faf71a069b5c494e78c30", 2372),
    "na-list": (79186, "lppl1.3c", "694b02ed72724294dbba0b4b159b1a67a155599ccb04bf8c4d108303cb6fb4be", 2296),
    "na-position": (55559, "lppl", "733c505cf2bf1e82e2f8543424bc147a44d1dcbaf8d246a1b1b18546f7a0b664", 1900),
    "na-tikzornament": (79019, "lppl1.3c", "194397f4056804653310cc4a1cf0ff0650547ce9ad7260152e5051648a086309", 3096),
    "na-urmborder": (78937, "lppl1.3c", "2057e0bf9ad907a65311f6b75d281901bb6c7926b9973f53c9fe366683eff00d", 1364),
    "nanicolle": (56224, "lppl1.3c", "c4d9c223b9142ba03ad005a009d0fead95e00cefb7761e2521ac7028a560b0a8", 5484),
    "nehgs-register": (79938, "lppl1.3c", "5bd031855307745d00a1e0506f73fe9867229654d70bae32a734301dbc7ed9dc", 5792),
    "nevelok": (39029, "lppl1.3", "fdb810d05ab57e7989b902bb7c8e929cd04454f8cb4a7ab47bc0a9d3ef59a6bd", 1860),
    "nodetree": (76924, "lppl1.3", "83dc7dd8fefb0b0edd5f0dae856fcb5808e9fe5913355556e517b50b71957eda", 17988),
    "novabeamer-theme": (79586, "lppl1.3c", "e13e72d8f65950fd428862d2ead7577c9cdab941f2ff7b48b1bd27b8a9e52c99", 2148),
    "numberpt": (76924, "lppl1.3c", "4511125c5d27af304f4c42c2e780cba99117fab5efeda79320951214122d3276", 2432),
    "numnameru": (44895, "lppl1.3", "1bde8a9e59e4b734a9b452230c1fc9d602fd93a04b86aa327efbe2bfed3b0030", 2512),
    "numodel-bundle": (80979, "lppl1.3c", "02d526e6c211d7efe70c1a9b2c9b471ceee87b99d9146523e2e66e3387d07e9a", 81964),
    "nwafupaper": (79239, "lppl1.3c", "5e4851eb905be0ea12bb05aaa6261a3b62b7bb1145dc1f731c7ef65899b362ba", 24504),
    "oddsmath": (80575, "lppl1.3c", "e83f1a02f40773d452f0078abca8e89b70e75c7dcb968349e32489937351a6c3", 1552),
    "odsfile": (78415, "lppl1.3c", "dbe9836ca772f4bb49e8db88c9f3d90e06d397c9138b350090feb22ce3e81cb7", 5896),
    "onlinebrief24": (78738, "lppl1.3c", "0d8c1ed7d2e9142c05d2bd803636de1279b7bbbc0eed6788afb12202d3867d7c", 9336),
    "orbitals": (79170, "lppl1.3c", "7415bf4de9db49e332c25f1bb5c01e8a36d17765c3b6e83997a04e0d02624e96", 4188),
    "ordinalpt": (79618, "lppl1.3", "df1788760e69a2cad10162d59b6ae8d7776728400c9e35cf382c5429d4a7eeae", 1880),
    "page-canons": (79829, "lppl1.3c", "274ac9fda7a93ce0f1ea77bfe42a0f9e47f88025141483458ca88ea0241314cf", 11124),
    "paracolrule": (80062, "lppl1.3c", "a703b2e3c18742cbc9d743ddd805f8088a4606b6c039cba743543d77a76d3ae0", 2544),
    "parsa": (54840, "lppl1.3c", "28b6626377fad062419dfc3b301ad4074637769a1a0da15f2948051d7317bf21", 6264),
    "pdfarticle": (80725, "mit", "34ae039ff75504e23f08e386c2f99b997053382dcf3433ab90931691c9bb1cd2", 3900),
    "pdfextra": (65184, "other-free", "ad4d1a97ab88f6ae99e1c596f76c3eef2d17226cb3282f527b947f7e4608816e", 25512),
    "pdfmathaccents": (79812, "lppl1.3c", "f941168af0e22c6a7b07f4a16ca6cd072ee1a4a3c4f44c02cd358139853a24dd", 3340),
    "penlight": (73362, "mit", "ac8b6cce0c76e09be3e971fc0a765f3a5b0daf49ec319bac04c82d78d4b45bd6", 102148),
    "penlightplus": (74000, "mit", "0f31499e778a79d2c495edd1b46e5078ef6770f5c56112cfda18586dc88b4e42", 12020),
    "persian-bib": (76790, "lppl1.3", "f0c552f030b4c0b7a9a089b820e84a26319b112028536b71642df88c30766d35", 21676),
    "persian-calendar": (81015, "lppl1.3c", "7f846fc4458f2382e5ccf6917e2ff90c2b0ace581a5133172d6d39989552fa60", 10700),
    "pgfmolbio": (71551, "lppl1.3", "84f9917535fb7846cc7010926b5406cbbe9d73d5c41a47881adb837cd46a499b", 13788),
    "pgfplots-autonode": (80328, "lppl1.3c", "8979cbcd583cb5599e37138b63465fbf7a45ca7773df26d38a5bf8c4d3e9012a", 16448),
    "philphys": (80632, "lppl1.3c", "b8f82dc5665fb9ae09deeac86bab98111224545d4c57ee10fc66e69629f261a5", 27440),
    "phtitration": (80002, "mit", "4271b556798a2d0e98e1e42298e8a59b4fc555322da7628bcf5f9503d8e4a73d", 3248),
    "physketch": (80859, "lppl1.3c", "a99e4865f9a518fff1a6f1bc8682afe53e3389d971b931e626cc6167ceee7f66", 10616),
    "piton": (80583, "lppl1.3", "7ae060e4f03f59768de8f17328401887c75456d526429b4d4d16de3b5a8bf6d7", 31792),
    "pkgmeta": (80445, "lppl1.3c", "6533277984fb6037aaa3ae4e1b04f5fbeb7b3f03cc830dade8165b367adae497", 4356),
    "placeat": (45145, "lppl1.3", "cf2b6bc2d60a35c811c2c06a28c2cae816243a1a2d9b5e50fbed5767ff0b0a8f", 4000),
    "plain-grid": (78986, "gpl3+", "f59e2dfb7e87bb10b9c1cef884f8e48b359ae6ca0ec7fdf005ba3196ac693d04", 7180),
    "plantuml": (80945, "lppl1.3c", "ee9daa3d542917e82189e1dc0163a8dae266906102900688ecce8122e3932c30", 11860),
    "pmhanguljamo": (78114, "lppl1.3", "462e6ff6ed31ec5f9094bd6eee60ad98d13d7123db21b4f0d379a7348570b39f", 10328),
    "polski": (78101, "lppl1.2", "5966d7bebaec65f15069a1cd2b7ea74e8547c4dd542aac53755125dbd7ebcf4a", 9304),
    "printcolormanager": (79127, "lppl1.3c", "142a925c4c72fc44af37ac2687843f57442a006ffe1e9f28836efdad0fdf0a86", 2520),
    "printfilelist": (80796, "lppl1.3c", "eec3661c2f91b84cb4640c44706952c87bd670e6cf1f4b2bd1bc068229301f37", 1480),
    "problib": (80499, "lppl1.3c", "da26ca4e133f65f88d2e999392e597e0b54e8ac843450ec64dc48757660e41e5", 7376),
    "programmes-cpge": (80729, "lppl1.3c other-free", "9749407b4285576c7282bce4c4a758c0a5ff3058c72120f069483c9ad47c4d5b", 6424),
    "promec-ufrgs": (79732, "lppl1.3c", "722816c6f1e2b30e302a7378d26f5bd020577f307fbd1e5b15a6839355be19c4", 5208),
    "proofgraph": (80912, "lppl1.3", "24578d7d098f008bb3500eb726788517ee41f0b4440e4c70cd4826188a35ac5c", 8536),
    "propositions": (79922, "lppl1.3c", "71676fae9cd123ed6a06b7f6c233a2851506380153fc97510d4fc398e488daad", 21896),
    "przechlewski-book": (23552, "lppl", "5a22e6def2f5a401e59077fd0e1ab3620ba77d2b12ec0a2c772e2096b7064b28", 9680),
    "pst-chladni": (79362, "lppl", "27cbed9698c29a1d4c1762d2662b140c9dd218d3d9b164ff623c6d2e0fab895b", 4232),
    "pst-electrolysis": (79338, "lppl", "72390d9652d206ffed6400523d6628352e3881b9bb84707009a356a01b2c105d", 2928),
    "pst-ellipsoid": (79354, "lppl", "642d09ed968c3e46c410f182532a71be1718ec02a2fb6f85cafda1070341b949", 1388),
    "pst-eqhz": (79121, "lppl", "392f103075240b1b7fc6c061e0f1dd4e886fe82d1bd920f6fe67eb2628a8e10f", 3852),
    "pst-hertz": (79205, "lppl", "e4ee58ed6c0e70573a6245e7a5724ed9b4123882e68400e168bebd0b00f3e1fb", 3564),
    "pst-hyperboloid": (79329, "lppl1.3", "699a57047bf9ac413c667fbf3f3b29a0aba0a23642389dc61aba930aa651aa35", 2604),
    "pst-hypertiling": (79461, "lppl1.3", "d6bc95eb6acb7bbe0eb16a8fdc2a658fa7f02ddae093e9a26792eaa9377f732a", 4196),
    "pst-spectrum": (79278, "lppl", "7752e55a236a1c7cd8834bdf75346ae97c9e15bf54c3a3904f706308d3ff01ea", 12048),
    "pst-titration": (79169, "lppl", "5230a9494bcf7a2df77492a221287f7abd8b6cff0e24e05cce4c1e3dbce8bcca", 3808),
    "ptlatexcommands": (67125, "mit", "6690487ced8e290a65c6476403fd9272a6a3835cd69c8dee1df5335405742663", 3412),
    "pxpgftate": (80969, "lppl1.3c gpl2+", "d68d63d763d86d9127b6c1e4fd6d24d1785f13e6e37b9a9b443b286839c5d727", 3684),
    "pyjupyter": (78632, "lppl1.3c", "432f741005a6262c9cf1afe1aa6f21859b718c34d57b31fd936b79a0f1f27582", 2304),
    "pyluatex": (78739, "mit lppl1.3c", "099e341fd900e2a8a25056353ba10d8d6ca9c7fc46a3165debee7947bab364c1", 6376),
    "query-astro": (78766, "lppl1.3c", "556be5b328c0edea87ef69515943e0a4d603d24d9da1498bccb10a81ed850868", 1924),
    "quickderivs": (79523, "mit", "c465d137a11113917d8a8163a5862a2b0468952036833a4a5fc097c8881dbec7", 2324),
    "rasti-journal": (80408, "lppl1.3", "1b5c76d2c7ee9da9b55fee410317fb58f8e0e9d6f5a304229d42e649d6bf7398", 24084),
    "register-transfer-level": (78780, "lppl1.3c", "9101d40beb1f41ce4f8b49af5d8f4ed5c03ede3e2b29b7291d189bdbb0b9edf1", 3440),
    "relatese": (79461, "lppl1.3c", "ca0d5122ebd217efcdad96e417f3fede5a27c98db1db1fc3c0a7272069da0eca", 3648),
    "revbadge": (80136, "mit", "3dbdec60c36c8c6aff26fe2a993d4ef207bcdfada5329ffb74f42bfebcbb459a", 2640),
    "rhode-island-thesis": (80854, "lppl1.3c", "0555fbe12d43e272231900ef1a490f51d497010fbe618dfaac1b349656c5027d", 28868),
    "robotkarol": (80874, "lppl1.3c", "16a5f28da33600fecf428ed2b52e375dbe8bbb3ba446219bee4d7de5ed9d62ee", 55684),
    "russ": (25209, "lppl", "f24397927d50a437228b21b35ff250ed38b5609d6716aa999ff226f5b10a9f3f", 25540),
    "sblarticle": (80966, "lppl1.3c", "5f735db6afd043b8c0d44a9dd7624b91414dc47628305a45a07c428d8c4bb525", 5740),
    "sblbook": (80964, "lppl1.3c", "0e798eefec6715e58516357ccdaf5294f66808da4bd8a1550bd1d816db25fb5e", 6368),
    "sblfonts": (78916, "lppl1.3c", "36d342d1c3d4402aecc8e929a93abcb2af8626be98dccda851f1ce1713fec1bf", 2492),
    "sblidx": (78596, "lppl1.3c", "b4ddeae3eeabb55a8de2f2443c6852095e787d7fdce5b321001cbd0cbb23c5b1", 5364),
    "sblreport": (80963, "lppl1.3c", "1703f2866cf4dd8544852f59c252ef7ed6f0ae430d848516af6dd05638511b39", 5668),
    "scholatex": (79588, "gpl3+", "14899eb0e5221144e805d84cb329efcdaa800acf794df1488d7141b4f68da0ff", 79976),
    "scikgtex": (66764, "mit", "4e11667ea1643f1db6c5885c5a9b2592ab48ac69dc5d0035c09707d19c227711", 6924),
    "screenwriter": (78965, "lppl1.3c", "936502e5029c5d9250d82ed321f48e204adcd70e23930240459b875487d369b5", 4464),
    "seatingchart": (79764, "lppl1.3c", "4e08df3ef0853c107c9056aa10067e8620059bf926f88d87c5f049e337f90a9d", 9936),
    "selnolig": (77682, "lppl1.3", "5eaca300bf9f3eef32a7c71e1cbe2f8bcdfa1d1b120e1f830ba94364d975cc1c", 53996),
    "semesterplanner": (56841, "lppl1.3", "581900dd70351904708783f709a4fb56a233e9a5f01947d5e5c9f6d93823e8a8", 1900),
    "semesterplannerlua": (71322, "lppl1.3", "30aeef2b54c4a79e09aa6a7e6982dae4eca04fec58eaea0ea56b104e0a07cb5a", 8396),
    "serbian-apostrophe": (23799, "lppl1.3", "828356e256094d8b4dfbb5de979977059870d2af7f96c72421fe7b03fe219f91", 10988),
    "serbian-date-lat": (23446, "gpl2", "376168c1eeb0f233a61d8e483705f8cda37acd2bbbc2ad94cc5b6da622bc0020", 776),
    "serbian-def-cyr": (23734, "lppl1.3", "1de77074ebf17c5c293db22450fb001d3d8ee18ac5dd2066fc7c7dd6892950b4", 1820),
    "serbian-lig": (53127, "lppl1.3", "e8ce10fb81b30f46e2cda64c86e387ccf7fc28efcee49ab01358df0de6df6635", 8708),
    "sesameseeds": (80957, "lppl1.3c", "aef33108bbca38b14e5177551b08b335d3517f6ea12339daa66e98e8e06182b4", 6076),
    "setbuilder": (79820, "mit", "2843c8bc6d538d389c59816ef4a26f57f9e1b514242a801c4aef80e4cacacd28", 2980),
    "sexam": (46628, "lppl1.3", "aa77d57753cdbc592a194c24dcdaf8283f222a1915cbd5550e2d75daafd14c3b", 2480),
    "sharedline": (78303, "lppl1.3c", "2769aaf1beac89c25e30afe3fdda599fd45094e95fb60a126a9b73325acaa5db", 1200),
    "showhyphenation": (77682, "lppl1.3c", "883b63d9a2e0eef603bfaebb7e030342dcead5cdf607d350d4649543471e32c9", 3284),
    "showkerning": (78632, "lppl1.3c", "1e2cc47f25cdf024cd37a9e22c8b285a51b07a367aad85fb0244bbc3405cccaf", 2844),
    "siicusp-abstracts": (80022, "lppl1.3c", "16ae59689aa7c1242872b61e67d7cd2ce9fdbb60a47c84cbc840f416765ee85d", 2216),
    "simple-resume-cv": (43057, "pd", "a4f61a537cc686cfdf41d7dafb2841c7ee834d6a960ee99f899c6b51ed0e210d", 3432),
    "simple-thesis-dissertation": (43058, "pd", "3cd3395ab2f23fa1d22589bfb448878f2a93469a34c7c1dbfe828bd5988d2c50", 5208),
    "simplesystemtex": (78503, "lppl1.3c", "5932779acea15747c24e52ae1f5715979fc5ed345b8f5c8154f2a96d8133a413", 5868),
    "simplex": (78263, "lppl1.3", "533dc0f709793ba063a969c89a748ba801a87c288a993eea6ba9b11cdc3e66b8", 2200),
    "simurgh": (31719, "gpl2", "f05706ba5086d9afc25482d8bbfc258cae0144d2026b61bf56752b89a8ece187", 246476),
    "smartnotes": (78610, "lppl1.3c", "20619b2ae591141a666d5b72ae350fa8f19cf01c44b2e0edc3f23f8b6babe3c9", 2828),
    "smileghost": (79819, "lppl1.3c", "a19713e5353e673fb5176b928be76398383e5bf9ad5e299502496c2231100498", 1976),
    "sneaker": (79247, "lppl1.3c", "9d9322b04bc3e9004d855c4f8ca1c69b3a2c14cbdbd3666d9485678bec92600f", 6212),
    "spacekern": (78632, "lppl1.3c", "2b273bbcde5fd8534947310d5ac6703522b246a882a8d573160d4a9f4aa92056", 2964),
    "spelling": (73571, "lppl1.3", "9c368f648264661ddbd808f3d466582f77bd91e41a9dafa867662faf73f5dba0", 14076),
    "spim-phdthesisthemes": (80778, "lgpl3", "7ec7aa8d0617cb16bd1e65556eb07a42d7974a081b514773e2052b7387f2fd5d", 26376),
    "spotxcolor": (80662, "lppl1.3c", "0acf83f3880ae4bf8491d2909a7ea1d6d1916fe91923003ceee779b14d1631f0", 21264),
    "statementsp": (78574, "lppl1.3c", "4a595674fd6f9f24480a0cf5db38113271f6ebf25e743a9598c132fe81952156", 2472),
    "statusstamp": (80522, "mit", "5db455b98d26df63382c06b3bea30f5f00e79aac108384b821e62b4b4cf160c8", 3032),
    "stricttex": (56320, "lppl1.3c", "90d4b9394b4bdb0ab8bb18a66b4d8330f8459089481ad8f0669282b6492557b1", 1880),
    "styleitems": (78427, "lppl1.3c", "32641cf67f70673785843afcb84e9723a840f878436b06675aae5cb0073191f4", 9936),
    "suai-report": (80521, "mit", "74631d1ba5c6c7ad21d95a0bf56db5397799557ae82a2c619c5b436f34091735", 17096),
    "subcaptionlite": (80824, "lppl1.3c", "bed9d7700139bbf98c01b35a4f9e21a8567460c7ad04ddf81e797a322d6c9d4c", 3792),
    "substnote": (79840, "mit", "d4231d822bb179d2bf83c687b31222d0aa3e1bb7b0a474d5de27ff94f991d1d8", 4404),
    "swebib": (76924, "lppl1.2", "32318902ed18c654b03f709d39bcb0533509f41a99f21966fcc0b28017508112", 6872),
    "sympycalc": (73069, "lppl1.3c", "65d804eee1f1fbe7f0c5a01bac44bc9fb7529d2c1235f70b2e8b4bc7051abe9d", 2232),
    "tabularray-abnt": (76924, "lppl1.3c", "a916a77be98486886345a2d280b7bd4e1e73f135e27e10c49223ceec1716fbc1", 5316),
    "talkpal": (79731, "gpl2+", "02438190507dcf70cdac972a41f047a019988c162735920b6ac31bcb6c1e507c", 2112),
    "tamu-talk": (80794, "lppl1.3c", "fd3c4b38199bb2a2a192db318f7562c63a437906c3519fa480aaefcc82b3451e", 5028),
    "tango": (71825, "lppl1.3c", "bd9fcddea44dbbd07b8c22c140770c9391de8cd8c3cf7edb7c9e3d9977b69226", 10060),
    "tap": (31731, "pd", "674df40974faacb73a2c7d93b388cf056e08e96cb13dcb78aaa696912c04db1a", 10672),
    "tensorstyles": (78864, "lppl1.3c", "7ac40159d68d0a5ddb56a306424bcbf323d3fd142a1a92565112ea2f183e0d2c", 6320),
    "terminaltrees": (80920, "lppl1.3c", "8d2c2b32f7eb4d22af748ec048f36eb612ede5eeace6fbffb34c0eae7199413f", 3544),
    "tetragonos": (49732, "lppl1.3c", "1b38f79676e1c7d06b85203c9e52aa9231b9a9110eff41309c7ee946399d0ead", 207756),
    "texecole": (79858, "gpl3+", "57eb5f28114af6fdabe5e9937770a41f31f000d3a903ecc777f038361df46ec8", 140964),
    "texnegar": (76924, "lppl1.3c", "ac8f4fa69e75747ab84c725b5ec1e248b735ed6b7b123a2bbb050b4ab56e509c", 14596),
    "textdate": (80822, "lppl1.3c", "820e385dd034f89463b4f497c45a51fb53493b06d9d47e76fab322f7ae68a750", 2796),
    "thaienum": (44140, "lppl1.3", "f9bae8cb787184ed3971a0ab1631e102a7bc707f09f0d4dfd4a5fb1a9c7022c4", 1556),
    "thaispec": (77682, "lppl1.3", "471244994eee77406874409f198012a68267b398dbf42e3d7a87ff2fd5a2df58", 3256),
    "tietreport": (80165, "mit", "6403ab71a799a1422dde24385ffc484c6f6098a23d249aa79fc1080c34b738f8", 2696),
    "tikz-catan": (79833, "mit", "93ea19234d6afcf61537b29794cd295e23ea3fc586757b0cb9b7905f86824d3d", 3800),
    "tikz-sections": (79148, "lppl1.3c", "eda8af56aedd559a3c613b700f9d9f98b7b772c60a3589fef231e0f70f57088c", 5640),
    "tikz-tensor-networks": (80355, "apache2", "2d36ccca4d717ed5da9e43f6fb8e771e370e050661d3af5722aec2b7eaaf271e", 193064),
    "tikzfins": (80971, "lppl1.3c", "04ac1db02c0e9d2eb5e68cc457a579f8f84a72c6248c5fe2cf2da9c0549f2250", 39952),
    "tikzphysics": (80593, "lppl1.3c", "49c282e8abbd7f896ed999dcca26a86cfe694e83760538edbc31a41bfe0799b5", 84732),
    "tikzpizza": (80970, "lppl1.3c", "c4e97692291115483d1ffdc8f8904749e3e83449329078bb5a1cc438b88a81ab", 18340),
    "tkz-fn": (80956, "lppl1.3c", "6bc3686cd291c6bca28ce62050cce0b518870cfdf18b6cb757a94c7ab8bea055", 15892),
    "tokstools": (78736, "lppl1.3c", "2ee5383c8b7b5065932a4a272cf772b2ee38af577046bb95f63489fbff70f8ec", 20788),
    "trainmap": (80119, "lppl1.3", "92dd0e7e007f90c71d076f781389bc9303f511f2170811467fee1fe20addf081", 7284),
    "transition-metals": (79082, "lppl1.3c", "cc2cbc8d1f4ffb2429cc2ddfdef00c0787aadcd3051401fb3ead1ecf8341a068", 8628),
    "truthtable": (68300, "lppl1.3c", "683b29b46151ec5eba9d3e857ea7f1a702af6f8c683d56f29ef7022814a0b3bf", 2196),
    "tsvtemplate": (65333, "other-free", "53c3e915f36d0850303d79b92251a8c38c9af72c7817028068eacfa30447f616", 1312),
    "turkce-sayi": (79461, "lppl1.3c", "92ef3809027b96d6009f3512f62d6b380bff7e95a009e64a9788c3dd276c3fa4", 5732),
    "turkmen": (79618, "lppl1.3", "441d26eef5b8524db929b05cdb10f45a3606fb269980a02df78aa044d8ba0627", 2204),
    "tuzuk": (74620, "lppl1.3c", "b5a6675dc681cf3c6917af45b72a975578859c0e67e764781306c67a05b10c4e", 1824),
    "tvthesis": (79740, "lppl1.3c", "1dd16ac7599018f6bc4f871efb9532ebf13215f8fd6584983e5d88ea92966177", 5636),
    "typewriter": (73877, "lppl", "0d6e26438dd00b1e1a53fd012b479039ecfd700ecf1ce89541f3a01b52f46f64", 25844),
    "ucharclasses": (77682, "pd", "f081c0d08dffa8802f0d90bbab87d57c3b581f2a7cab540797ae5b815370c3c9", 9200),
    "ugentdocs": (80997, "lppl1.3c", "a74c021794c5175163a446817f1ebdafdfbb4b66394faaa8c2f47ae22f14bb9b", 46672),
    "unhook": (80323, "lppl1.3c", "92baeb03327db1cba4bcb9bd6e642d8c8060f3c650f48997e6fc6376b805b8f5", 4316),
    "unibidi-lua": (80322, "gpl2", "13be5b97efa206ec621238ec9d847193de6dc90e64d887fc958f76c3927f0302", 24188),
    "unicode-alphabets": (66225, "cc-by-sa-4", "5b5f2e1e1b631ccc62f28351652a9f21ac25aafe3b22ad918b2c639d286e03c2", 44072),
    "unicode-bidi": (77682, "lppl1.3", "75d3efdb248351edcc57eb55a1b478f5fc833c536d876f79dc8c4ef1a00f8956", 1116),
    "uninormalize": (78101, "lppl1.3", "c01907975937d43e9a4b028dda0c8fd4202a69ae8f4d37d1eacf2f45666234f9", 5732),
    "unipersian": (80833, "lppl1.3c", "3734e7d381b92d32fed7fd01ae080436e9a00f6459f96cd5c259f24daf4b0ffe", 7456),
    "unisugar": (22357, "lppl1.3", "3acd0b75546976ae26b00c5af92e2b22bd33ca06c74eabca84a8d89764180a32", 2468),
    "unitconv": (76924, "lppl1.3", "1a39cf20db01841a174bc376214750c1a2c2ce581780642a5e2bb48791c5eaa1", 1792),
    "user-persona": (80838, "lppl1.3c", "0c79d524fe30adbddef7da286b2558034eb729d2aed8c71beb292eca93172fe6", 3660),
    "uvlhighlight": (78690, "lppl1.3c", "f46e6cd8107c74d3991c56e61ae85dd646229592399dfc80571d696f8c7b3499", 3620),
    "verifica": (75682, "lppl1.3c", "8010cbf392dd912d5080dc229244c34713f42975e2919d04d5789606fa9ed92d", 3996),
    "wiseref": (80586, "lppl1.3c", "3adbe024d2e01657df1ab771e36c2eecb89aa823354ab662068390d3c1d135c2", 10108),
    "wnri-latex": (22338, "gpl2", "af49a58ad937c6079bc4cc646212da2f41e05c47112e5c65b545369a8523cdfe", 2664),
    "xdvipsk-support": (78653, "lppl1.3c", "b82f69db9de03339e564ab19e6c1af7cdfa762f2cc4c57d4b51182b8893da544", 5236),
    "xechangebar": (77682, "lppl1.3", "1bed93d19602cabecfb2ce5ce9fb7c3f94ee542c721400def05098255fe86797", 6880),
    "xecyrmongolian": (53160, "lppl1.3c", "2c7c7555160e8d5b6349b70f796612ef62ead51812234751050e943f1195a682", 2232),
    "xeindex": (77682, "lppl", "64847058845da0a915fcbcbb6efb212c4160ad0ace73260a4ed054dc5f97fb1c", 1652),
    "xepersian-hm": (77682, "lppl1.3c", "2a2ac5db636d25ad43c109a91336ae700006285e1f73e36b56286e8cfa44b5fb", 5896),
    "xesearch": (77682, "lppl", "db9981ca6bd973df205f0c94a02f5e296b40d2f7ef4046aaa51e0bb1fb213fee", 5896),
    "xespotcolor": (77682, "lppl1.3c", "3955c4b87241821e8c546e5a44d0576be933c64adfad445dbd53501d619683a2", 2364),
    "xetexfontinfo": (15878, "apache2", "8208b938d8112b12c132059b287dbfa16befa1d466f6a0bb0b2668c43231bbc0", 2340),
    "xevlna": (77682, "lppl1.3", "fb029d5dfbe0f3f50f2097e50efd31553d12cda6d565cf2009d7d54928ebfe31", 1336),
    "xlistings": (80013, "lppl1.3c", "82fe5620a50f5593566591faa7ac420197a07203376919661243e70b48d354ef", 21792),
    "yamlvars": (73922, "mit", "96b9020ee67d4df48f466d38170a02eaccc94f53d897f3e06636018dbbc42350", 6212),
    "zbmath-review-template": (59693, "gpl3 cc-by-sa-4", "41b88cd20b737535afaaf96d1daacffd301d7287f4516847f05bd5cd73c2573f", 2228),
    "zitie": (77682, "lppl1.3c", "fe7533575bbf61eb1ea1633bb5eb9326d4b756926e62d92d4cbc7c6ef61637bc", 9596),
}
for _pkg, (_rev, _license, _sha, _size) in MACRO_SUPPLEMENT_PINS.items():
    SUPPLEMENT_PACKAGES[_pkg] = {
        "version": f"TeX Live r{_rev}",
        "revision": _rev,
        "license": _license,
        "upstream_url": f"https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/{_pkg}.tar.xz",
        "upstream_sha256": _sha,
        "upstream_size_bytes": _size,
        "select": [(_prefix, MACRO_SUPPLEMENT_EXTS) for _prefix in MACRO_SUPPLEMENT_PREFIXES]
        + ([("scripts/" + _pkg, (".lua",))] if _pkg in MACRO_SUPPLEMENT_SCRIPT_PKGS else []),
        "map_files": [],
    }
# Supplement-wide license notice (legal directory name, shipped path).
SUPPLEMENT_EXTRA_NOTICES = [
    ("NOTICES-FONTS-XETEX.txt", "doc/fonts/NOTICES-FONTS-XETEX.txt"),
]
# Map files that TeX Live's updmap enables and whose fonts the main archive
# already ships, but which the baseline consolidation never merged.
SUPPLEMENT_MAP_ROOTS = [
    "fonts/map/dvips/avantgar/uag.map",
    "fonts/map/dvips/ebgaramond-maths/EBGaramond-Maths.map",
    "fonts/map/dvips/fourier/fourier.map",
    "fonts/map/dvips/gfsneohellenic/gfsneohellenic.map",
    "fonts/map/dvips/libertinegc/libertinegc.map",
    "fonts/map/dvips/mathdesign/mdbch.map",
    "fonts/map/dvips/mathdesign/mdici.map",
    "fonts/map/dvips/mathdesign/mdpgd.map",
    "fonts/map/dvips/mathdesign/mdpus.map",
    "fonts/map/dvips/mathdesign/mdput.map",
    "fonts/map/dvips/mathdesign/mdugm.map",
    "fonts/map/dvips/psnfss/fpls.map",
    "fonts/map/dvips/rsfso/rsfso.map",
    "fonts/map/dvips/srbtiks/srbtiks.map",
]
PDFTEX_MAP_REL = "fonts/map/pdftex/updmap/pdftex.map"

# Corresponding source archives for GPLv2 and open-source distribution compliance
SOURCE_ARCHIVES_INFO = {
    "base35": {
        "archive": "base35.zip",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/fonts/urw/base35.zip",
        "sha256": "07abaf13d6dbff68b884a877c91c9692283562273adc2d52a4bf9bfdd90668fd",
        "size_bytes": 1968003,
        "license": "GPL-2.0 with font exception",
        "provenance": "Upstream CTAN package release by URW++ containing PostScript Base 35 Type 1 PFB outlines, AFM metrics, PFM files, and author documentation",
    },
    "txfonts": {
        "archive": "txfonts.zip",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/fonts/txfonts.zip",
        "sha256": "26bea7fab28a21f21d26c36013938e52bc20ef1ccdad4a0e83b16243ffbcaece",
        "size_bytes": 1706528,
        "license": "GPL-2.0",
        "provenance": "Official CTAN package release by Young Ryu containing complete Type 1 outlines, METAFONT and TeX macro sources",
    },
    "mathdesign": {
        "archive": "mathdesign.zip",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/fonts/mathdesign.zip",
        "sha256": "9e54999967f2b7fefc6ffdb7fb48b6ef6ce3c8a4f1dd65b25e594596cb90be0a",
        "size_bytes": 6971155,
        "license": "GPL-2.0",
        "provenance": "Official CTAN package release by Paul Pichaureau containing Type 1 outlines, FontForge sources, and macro definitions",
    },
    "fpl": {
        "archive": "fpl.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/fpl.source.tar.xz",
        "sha256": "2c8e350bed490838429d6ade800d1d075dacfee017f67d8e86bebbfc470581fc",
        "size_bytes": 30656,
        "license": "GPL-2.0 / LPPL-1.0",
        "provenance": "Official TeX Live source archive containing FPL FontForge and metric sources",
    },
    "mathpazo": {
        "archive": "mathpazo.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/mathpazo.source.tar.xz",
        "sha256": "a0bc235b3f682cb65301ea2be4e599ceb5ddcaca559dd574adf717de1e7f7d93",
        "size_bytes": 17980,
        "license": "GPL-2.0 with font exception",
        "provenance": "Official TeX Live source archive containing Pazo Math fontinst, metrics, and build sources",
    },
    "yhmath": {
        "archive": "yhmath.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/yhmath.source.tar.xz",
        "sha256": "96872c0dc2d022a3f43ad2240bd16775b6ee4f6afe0ba2b6afa4a9903a92049f",
        "size_bytes": 16512,
        "license": "LPPL-1.3c",
        "provenance": "Official TeX Live source archive containing yhmath METAFONT sources, macros, and documentation",
    },
    "ae": {
        "archive": "ae.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ae.source.tar.xz",
        "sha256": "219d3e8b1c2d057d3583d3a99837744f52a6ce94cd2d845f1996a355dfda8dc7",
        "size_bytes": 19988,
        "license": "LPPL",
        "provenance": "Official TeX Live source archive containing virtual font source scripts for Almost European (AE) fonts",
    },
    "stix": {
        "archive": "stix.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/stix.source.tar.xz",
        "sha256": "28e71c9103859f50113b71c3fb788f09b9ea83d233d01c6475b923b048d242c0",
        "size_bytes": 27808,
        "license": "OFL-1.1 / LPPL-1.3",
        "provenance": "Official TeX Live source archive containing STIX fonts build and conversion sources",
    },
    "cm-super": {
        "archive": "cm-super.zip",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/fonts/ps-type1/cm-super.zip",
        "sha256": "f3f856a5006ae7d2bbc2d9a338ca5ae25b6fba19eebee3ba20892845aafa7987",
        "size_bytes": 67319801,
        "license": "GPL-2.0-or-later with font exception",
        "provenance": "Upstream CTAN package release by Vladimir Volovich containing Type 1 PFB outlines, AFM metrics, dvips encodings, and inf archives",
    },
    "unfonts-core": {
        "archive": "fonts-unfonts-core_1.0.2-080608.orig.tar.xz",
        "url": "https://deb.debian.org/debian/pool/main/f/fonts-unfonts-core/fonts-unfonts-core_1.0.2-080608.orig.tar.xz",
        "sha256": "14abb309f9d979cc20212fabfbd7f50b55c42183985ae507390c7461ce0b307c",
        "size_bytes": 14727532,
        "license": "GPL-2.0",
        "provenance": "Official upstream source tarball released by Won-kyu Park / KLDP (as packaged in Debian main), comprising the 12 TrueType font programs and author documentation",
    },
    "lh": {
        "archive": "lh.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/lh.source.tar.xz",
        "sha256": "e200068f4bf14c8cbb844030e49a73500f08e35906487a68cb73b8f51ea65122",
        "size_bytes": 41756,
        "license": "LPPL-1.3c",
        "provenance": "Official TeX Live source archive containing complete METAFONT source files for LH Cyrillic fonts (T2A, T2B, T2C, X2)",
    },
    "lhcyr": {
        "archive": "lhcyr.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/lhcyr.source.tar.xz",
        "sha256": "3b393e47f8da3b8e2d903863761ed796c1b64bcac39d4d94796d6637760c7df6",
        "size_bytes": 2356,
        "license": "LPPL / other-free",
        "provenance": "Official TeX Live source archive containing LaTeX Cyrillic style and font definition source scripts",
    },
    "stmaryrd": {
        "archive": "stmaryrd.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/stmaryrd.source.tar.xz",
        "sha256": "28d350526d65fed4b6e1e4ba8c58e88348ac5c5ed567ff217a4220447fde1d2d",
        "size_bytes": 6248,
        "license": "Public Domain",
        "provenance": "Official TeX Live source archive containing original METAFONT source files and stmaryrd.dtx source",
    },
    "bbding": {
        "archive": "bbding.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/bbding.source.tar.xz",
        "sha256": "f47be23cd57a035ff1d17c6b069040dc44bcdc8943b6a2f7880a1b3cf60f47e9",
        "size_bytes": 10156,
        "license": "LPPL-1.3c",
        "provenance": "Official TeX Live source archive containing bbding.dtx and bbding10.mf source files",
    },
    "babel-russian": {
        "archive": "babel-russian.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/babel-russian.source.tar.xz",
        "sha256": "ba61c255385376334213b208d6b9a54816f76d20502f5c612a6c9bb7e2cc03d8",
        "size_bytes": 18276,
        "license": "LPPL-1.3c",
        "provenance": "Official TeX Live source archive containing russianb.dtx and russianb.ins source files",
    },
    "babel-spanish": {
        "archive": "babel-spanish.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/babel-spanish.source.tar.xz",
        "sha256": "42d8bde6d46b711f2ffa315fab1f75e858bf9327a37a601a61d39cc08a0123b6",
        "size_bytes": 29796,
        "license": "LPPL-1.3c",
        "provenance": "Official TeX Live source archive containing spanish.dtx, spanish.ins, and documentation sources",
    },
    "babel-portuges": {
        "archive": "babel-portuges.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/babel-portuges.source.tar.xz",
        "sha256": "7ee261caa2606904e7b2cc1ce4f077c37145b60e5df1c2aa6f85fee90f1b088e",
        "size_bytes": 6332,
        "license": "LPPL-1.3c",
        "provenance": "Official TeX Live source archive containing portuges.dtx and portuges.ins source files",
    },
    "hyph-utf8": {
        "archive": "hyph-utf8.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/hyph-utf8.source.tar.xz",
        "sha256": "5ab1a0375270b69dca2c1e095e8e0631713a2b80ac9c5736475a9b3c36d51753",
        "size_bytes": 36044,
        "license": "LPPL / MIT / Public Domain",
        "provenance": "Official TeX Live source archive containing hyph-utf8 generator scripts, encoding definitions, and pattern conversion tools",
    },
    "ruhyphen": {
        "archive": "ruhyphen.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ruhyphen.source.tar.xz",
        "sha256": "2f229e3472560956365b4e1ac8ada8f5c1388cc8f95a4e5893eb4dad2b365150",
        "size_bytes": 13656,
        "license": "LPPL-1.2",
        "provenance": "Official TeX Live source archive containing ruhyphen source scripts and documentation",
    },
    "ec": {
        "archive": "ec.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ec.tar.xz",
        "sha256": "bb85425c214b1056b5f0b8f3bf1478b81e89bd7d290d61c7c73289b534786897",
        "size_bytes": 263716,
        "license": "LPPL-1.3c",
        "provenance": "Upstream CTAN package release by Jörg Knappen containing METAFONT source files for European Computer Modern (EC/TC) fonts",
    },
    "cm": {
        "archive": "cm.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/cm.tar.xz",
        "sha256": "ebedd3dc7ece433d366d848ea8bd9cd2642a0f49c000c46a2ed1dde5b1cebc1c",
        "size_bytes": 238064,
        "license": "Knuth",
        "provenance": "Official Knuth Computer Modern METAFONT source files including cmbase.mf and standard glyph definitions",
    },
    "rsfs": {
        "archive": "rsfs.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/rsfs.tar.xz",
        "sha256": "1afec0c5e9711f652675e38b7cd7e88101c44aa0d0ff317ad6ac06f1d2cc7043",
        "size_bytes": 56092,
        "license": "Public Domain",
        "provenance": "Official CTAN package release by Ralph Smith and Taco Hoekwater containing Type 1 outlines, metrics, and METAFONT sources",
    },

    'cjk-ko': {
        'archive': 'cjk-ko.zip',
        'license': 'LPPL-1.3c / GPL-2.0 / Public Domain',
        'provenance': 'Authoritative CTAN package distribution by ko.TeX team containing full LaTeX macros and documentation sources',
        'sha256': '28db8f29e112ff40047ad07f6052ae180f4192e780ed74b0db85e211c3a9f55a',
        'size_bytes': 175960,
        'url': 'https://mirror.aarnet.edu.au/pub/CTAN/language/korean/cjk-ko.zip',
    },
    'els-cas-templates': {
        'archive': 'els-cas-templates.zip',
        'license': 'LPPL-1.3c',
        'provenance': 'Official Elsevier STM Document Lab release containing complete class, style, thumbnail assets, and documentation',
        'sha256': '36d97da01c6bbd134f315bff6c3de553735e2550444a6ddd4f869ddc67a20757',
        'size_bytes': 3305507,
        'url': 'https://mirror.aarnet.edu.au/pub/CTAN/macros/latex/contrib/els-cas-templates.zip',
    },
    't2': {
        'archive': 't2.zip',
        'license': 'LPPL-1.3c',
        'provenance': 'Authoritative CTAN package distribution by Vladimir Volovich and Werner Lemberg containing DocStrip dtx/ins sources',
        'sha256': '29865cabc7e0bbbc8289144f2f1b7ad5a4d744ed179a9d87fdb82133e589d8a6',
        'size_bytes': 138360,
        'url': 'https://mirror.aarnet.edu.au/pub/CTAN/macros/latex/contrib/t2.zip',
    },
    'utfsym': {
        'archive': 'utfsym.zip',
        'license': 'CC0-1.0',
        'provenance': 'Authoritative CTAN distribution by Daniel Benjamin Stegemann containing complete TikZ sources and generator scripts',
        'sha256': '14c4aa17e60ec94f6cc0ff989e4946a88cdeac8753dff4148eed301f38c155f9',
        'size_bytes': 6530598,
        'url': 'https://mirror.aarnet.edu.au/pub/CTAN/graphics/pgf/contrib/utfsym.zip',
    },
    'bera': {'archive': 'bera.zip',
     'url': 'https://mirror.aarnet.edu.au/pub/CTAN/fonts/bera.zip',
     'sha256': '29735b1ca7281537a420b2e214b9fddfc27bcb478ad2bd5caf23a489ab1d8cd9',
     'size_bytes': 474202,
     'license': 'Bitstream-Vera',
     'provenance': 'Official upstream distribution preserving font or LaTeX package sources and licensing'},
    'ccicons': {'archive': 'ccicons.source.tar.xz',
     'url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/ccicons.source.tar.xz',
     'sha256': '1cc8f0e4d1c8e6ebaba613978355ae9bb7051995b60db54c59988ba9c8bc5b40',
     'size_bytes': 8832,
     'license': 'OFL-1.1 / LPPL-1.3c',
     'provenance': 'Official upstream distribution preserving font or LaTeX package sources and licensing'},
    'doclicense': {'archive': 'doclicense.source.tar.xz',
     'url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/doclicense.source.tar.xz',
     'sha256': '0775c271a95a388cde9b3df80133ba94efab6d6e18073c8e1e1ef6e28f471cc6',
     'size_bytes': 13592,
     'license': 'CC0-1.0 / LPPL-1.3c',
     'provenance': 'Official upstream distribution preserving font or LaTeX package sources and licensing'},
    'stix2-type1': {'archive': 'stix2-type1.source.tar.xz',
     'url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/stix2-type1.source.tar.xz',
     'sha256': '3527cd6040675385f2d36d6a79a493ef22b77b27a2f6d68527cb675c057ed704',
     'size_bytes': 28096,
     'license': 'OFL-1.1 / LPPL-1.3',
     'provenance': 'Official upstream distribution preserving font or LaTeX package sources and licensing'},
    'twemojis': {'archive': 'twemojis.source.tar.xz',
     'url': 'https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/twemojis.source.tar.xz',
     'sha256': 'cc65acb87b676d69bca7774c4ccc1a9f57be70caed619f6ee879f37494ab195c',
     'size_bytes': 70504,
     'license': 'CC-BY-4.0 / LPPL-1.3',
     'provenance': 'Official upstream distribution preserving font or LaTeX package sources and licensing'},
    "duckuments": {
        "archive": "duckuments.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/duckuments.source.tar.xz",
        "sha256": "c23c295c49490498b76fa07aa6de604202cafd03630e1d738363d947a2e2ce08",
        "size_bytes": 9112,
        "license": "LPPL-1.3c",
        "provenance": "Official upstream LaTeX and example graphic generation sources",
    },
    "fdsymbol": {
        "archive": "fdsymbol.source.tar.xz",
        "url": "https://mirror.aarnet.edu.au/pub/CTAN/systems/texlive/tlnet/archive/fdsymbol.source.tar.xz",
        "sha256": "44968e81d80de4c34e4b6d46d2bd36412796fc8b26dfa655c59f24739fadc596",
        "size_bytes": 17776,
        "license": "OFL-1.1 / LPPL-1.3c",
        "provenance": "Official TeX Live source archive containing fdsymbol.dtx and fdsymbol.ins source files",
    },
    "lualatex-math": {
        "archive": "lualatex-math.source.tar.xz",
        "url": "https://mirror.ctan.org/systems/texlive/tlnet/archive/lualatex-math.source.tar.xz",
        "sha256": "7a3af6f83bcca2c80e954a8d6899c7de9277a84ede6e436f22c3b950dc603af1",
        "size_bytes": 9532,
        "license": "LPPL-1.3c",
        "provenance": "Official TeX Live source archive containing the LuaLaTeX mathematics package's DocStrip and Lua sources",
    },
}


def sha256_file(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def resolve_baseline_path(user_path):
    if user_path and os.path.exists(user_path):
        return user_path
    for cand in ["/tmp/texres-baseline/packages.tar.zst", "/tmp/texres_baseline/packages.tar.zst"]:
        if os.path.exists(cand):
            return cand
    if user_path:
        return user_path
    return "/tmp/texres-baseline/packages.tar.zst"


def build_source_archive(output_path, cache_dir):
    """
    Stable callable source-archive packaging API.
    Packs verified corresponding source archives into a deterministic zstd-compressed tar archive.
    """
    if not cache_dir or not os.path.exists(cache_dir):
        raise FileNotFoundError(f"Source cache directory not found: {cache_dir}")

    print(f"Building deterministic source distribution archive: {output_path}...")
    source_records = {}

    # Verify all source archives exist and match pins
    for pkg_id, info in sorted(SOURCE_ARCHIVES_INFO.items()):
        arc_path = os.path.join(cache_dir, info["archive"])
        if not os.path.exists(arc_path):
            raise FileNotFoundError(f"Required source archive missing from cache: {info['archive']} ({arc_path})")
        actual_h = sha256_file(arc_path)
        expected_h = info["sha256"]
        if actual_h != expected_h:
            raise ValueError(
                f"REJECTED: Source archive hash mismatch on {pkg_id} ({info['archive']})!\n"
                f"  Expected: {expected_h}\n"
                f"  Actual:   {actual_h}"
            )
        actual_sz = os.path.getsize(arc_path)
        source_records[pkg_id] = {
            "archive": info["archive"],
            "url": info["url"],
            "sha256": actual_h,
            "size_bytes": actual_sz,
            "license": info["license"],
            "provenance": info.get("provenance", info.get("obligation", "")),
        }
        print(f"  Verified source archive {pkg_id}: {info['archive']} ({actual_sz} bytes, sha256={actual_h[:16]}...)")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    temp_tar = output_path + ".tmp"

    zstd_proc = subprocess.Popen(
        ["zstd", "-19", "-T0", "-o", temp_tar],
        stdin=subprocess.PIPE,
    )

    with tarfile.open(fileobj=zstd_proc.stdin, mode="w|") as tar:
        for pkg_id in sorted(SOURCE_ARCHIVES_INFO.keys()):
            info = SOURCE_ARCHIVES_INFO[pkg_id]
            full_path = os.path.join(cache_dir, info["archive"])
            arc_rel = f"sources/{info['archive']}"
            ti = tar.gettarinfo(full_path, arcname=arc_rel)
            ti.uid = 0
            ti.gid = 0
            ti.uname = "root"
            ti.gname = "root"
            ti.mtime = FIXED_MTIME
            ti.mode = 0o644
            with open(full_path, "rb") as f:
                tar.addfile(ti, f)

    zstd_proc.stdin.close()
    zstd_proc.wait()
    if zstd_proc.returncode != 0:
        raise RuntimeError("zstd compression of source archive failed.")

    os.replace(temp_tar, output_path)
    out_sz = os.path.getsize(output_path)
    out_h = sha256_file(output_path)
    print(f"Source distribution archive created successfully: {output_path}")
    print(f"  Total source archives: {len(source_records)}, Size: {out_sz / (1024*1024):.2f} MiB, SHA256: {out_h}")
    return {
        "logical_name": os.path.basename(output_path),
        "sha256": out_h,
        "size_bytes": out_sz,
        "source_archives": source_records,
    }


def generate_lh_metrics(combined_dir, cache_dir, scratch_dir):
    """
    Deterministic offline regeneration of complete LH Cyrillic font metrics (TFMs)
    using pinned METAFONT sources from lh.tar.xz, ec.tar.xz, and cm.tar.xz.
    Generates all 76 \\EC@family Cyrillic font families across all 14 standard sizes (1064 fonts)
    plus all extended design sizes supported by fikparm.mf (1580 fonts total).
    Operates with zero host font dependencies using METAFONT nullmode.
    """
    print(f"  Deterministically regenerating complete LH Cyrillic font metrics from pinned MF sources...")
    mf_scratch = os.path.join(scratch_dir, "mf_work")
    os.makedirs(mf_scratch, exist_ok=True)

    source_mappings = [
        (os.path.join(cache_dir, "lh.tar.xz"), "fonts/source/lh"),
        (os.path.join(cache_dir, "ec.tar.xz"), "fonts/source/jknappen/ec"),
        (os.path.join(cache_dir, "cm.tar.xz"), "fonts/source/public/cm"),
    ]
    for arc_path, sub in source_mappings:
        if not os.path.exists(arc_path):
            raise FileNotFoundError(f"Required source archive missing: {arc_path}")
        with tarfile.open(arc_path, "r:xz") as t:
            for m in t.getmembers():
                if m.name.startswith(sub):
                    t.extract(m, mf_scratch)

    input_dirs = [mf_scratch]
    for root, dirs, files in os.walk(mf_scratch):
        if any(f.endswith(".mf") for f in files):
            input_dirs.append(root)

    env = os.environ.copy()
    env["MFINPUTS"] = ":".join(input_dirs)

    candidate_fonts = set()
    for prefix in CYRILLIC_EC_FAMILIES:
        for sz in STANDARD_SIZES:
            candidate_fonts.add(f"{prefix}{sz}")

    cm_super_arc = os.path.join(cache_dir, "cm-super.tar.xz")
    with tarfile.open(cm_super_arc, "r:xz") as t:
        for m in t.getmembers():
            if m.name.endswith(".map") and ("cm-super-t2" in m.name or "cm-super-x2" in m.name):
                f = t.extractfile(m)
                for line in f:
                    s = line.decode('latin1').strip()
                    if s and not s.startswith("%"):
                        tfm = s.split()[0]
                        if tfm[:2] in ("la", "lb", "lc", "rx"):
                            candidate_fonts.add(tfm)

    font_list = sorted(candidate_fonts)
    for font in font_list:
        mf_file = os.path.join(mf_scratch, f"{font}.mf")
        with open(mf_file, "w") as f:
            f.write("input fikparm;\n")

    def run_mf_job(font):
        cmd = ["mf", rf"\mode:=nullmode; nonstopmode; input {font}"]
        subprocess.run(cmd, cwd=mf_scratch, env=env, capture_output=True, text=True)
        tfm_src = os.path.join(mf_scratch, f"{font}.tfm")
        if os.path.exists(tfm_src):
            return font, tfm_src
        return font, None

    t0 = time.time()
    generated_tfms = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
        results = list(executor.map(run_mf_job, font_list))
    t1 = time.time()

    for font, tfm_src in results:
        if tfm_src:
            prefix2 = font[:2]
            if prefix2 == "la":
                sub_rel = "fonts/tfm/lh/lh-t2a"
            elif prefix2 == "lb":
                sub_rel = "fonts/tfm/lh/lh-t2b"
            elif prefix2 == "lc":
                sub_rel = "fonts/tfm/lh/lh-t2c"
            else:
                sub_rel = "fonts/tfm/lh/lh-x2"

            target_rel = f"{sub_rel}/{font}.tfm"
            target_full = os.path.join(combined_dir, target_rel)
            os.makedirs(os.path.dirname(target_full), exist_ok=True)
            shutil.copy2(tfm_src, target_full)
            fhash = sha256_file(target_full)
            generated_tfms[target_rel] = fhash

    print(f"  Generated {len(generated_tfms)} complete LH Cyrillic TFMs in {t1 - t0:.1f}s (provenance: mf nullmode).")
    return {
        "compiler": "mf (METAFONT 2.71828182)",
        "mode": "nullmode",
        "total_generated": len(generated_tfms),
        "source_archives": ["lh.tar.xz", "ec.tar.xz", "cm.tar.xz"],
        "files": generated_tfms,
    }


# pdfTeX writes a TFM that has no pdftex.map entry as a Type 3 font of PK
# bitmaps, which kpathsea has mktexpk make on demand (texmf.cnf: mode ljfour;
# pdftexconfig.tex: \pdfpkresolution 600). TeX Live maps none of these
# METAFONT-only fonts, so they are shipped as the PK files mktexpk writes.
MF_PK_MODE = "ljfour"
MF_PK_BDPI = 600
# LaTeX's standard sizes: the size lists of the .fd files (ifsym's `<->`
# scales one design size to all of them).
MF_PK_SIZES = ("5", "6", "7", "8", "9", "10", "10.95", "12", "14.4", "17.28", "20.74", "24.88")
# (archive, TDS directory of the PK files, fonts: None = every TFM the archive ships)
MF_PK_FONTS = (
    ("bbm.tar.xz", "bbm", None),
    ("ifsym.tar.xz", "ifsym", None),
    ("cm-mf-extra-bold.tar.xz", "cm-mf-extra-bold", ["cmbcsc10"]),
    ("cmcyr.tar.xz", "cmcyr", ["cmcsc12"]),
)


def _f32(x):
    return struct.unpack("f", struct.pack("f", x))[0]


def _tex_pt_sp(text):
    """tex.web scan_dimen of `<text>pt`: the fraction through round_decimals."""
    whole, _, frac = text.partition(".")
    a = 0
    for digit in reversed(frac[:17]):
        a = (a + int(digit) * 0o400000) // 10
    return int(whole) * 65536 + (a + 1) // 2


def _divide_scaled_out(s, m, dd):
    """pdftex.web divide_scaled's `scaled_out` for s, m > 0."""
    q, r = divmod(s, m)
    for _ in range(dd):
        q, r = 10 * q + (10 * r) // m, (10 * r) % m
    if 2 * r >= m:
        r -= m
    p = 10 ** dd
    return s - (r // p if r >= 0 else -((-r) // p))


def _kpse_magstep(n, bdpi):
    """kpathsea magstep.c `magstep` (n in half steps)."""
    neg, n = n < 0, abs(n)
    t = 1.0
    if n & 1:
        n &= ~1
        t = 1.095445115
    while n > 8:
        n -= 8
        t *= 2.0736
    while n > 0:
        n -= 2
        t *= 1.2
    return int(0.5 + (bdpi / t if neg else bdpi * t))


def _kpse_magstep_fix(dpi, bdpi):
    """kpathsea `kpathsea_magstep_fix`: snap dpi to a magstep within 1."""
    sign = -1 if dpi < bdpi else 1
    real = 0
    for m in range(40):
        mdpi = _kpse_magstep(m * sign, bdpi)
        if abs(mdpi - dpi) <= 1:
            real = mdpi
        elif (mdpi - dpi) * sign > 0:
            real = dpi
        if real:
            break
    return real or dpi


def _pdftex_pk_dpi(at_sp, dsize_sp):
    """writet3.c `writepk`: the PK resolution pdfTeX asks kpathsea for."""
    size = _divide_scaled_out(at_sp, 6578176, 6)  # pdf_font_size
    val = _f32(MF_PK_BDPI * _f32(_f32(size) / _f32(dsize_sp)))
    return _kpse_magstep_fix(int(val + 0.5), MF_PK_BDPI)


def _tfm_design_size_sp(data):
    """tex.web: the TFM header's design size as scaled points."""
    return int.from_bytes(data[28:32], "big") >> 4


def generate_metafont_pk(combined_dir, cache_dir, scratch_dir):
    """
    The PK fonts pdfTeX embeds for the METAFONT-only fonts of MF_PK_FONTS,
    made the way mktexpk does (`mf-nowin -progname=mf \\mode:=ljfour;
    mag:=<dpi div 600>+<dpi mod 600>/600; nonstopmode; input <font>` and
    gftopk) from the pinned CTAN METAFONT sources, at every resolution a
    standard LaTeX size of half to 2.5 times the design size asks for.
    Also writes cmcsc12.tfm, which TeX Live's mktextfm makes on demand.
    """
    print("  [MF PK] Generating PK fonts from pinned METAFONT sources...")
    mf_exe = shutil.which("mf-nowin") or shutil.which("mf")
    if not mf_exe:
        raise RuntimeError("Required build tool 'mf' (METAFONT) not found via PATH.")
    gftopk_exe = shutil.which("gftopk")
    if not gftopk_exe:
        raise RuntimeError("Required build tool 'gftopk' not found via PATH.")

    work_dir = os.path.join(scratch_dir, "mf_pk_work")
    os.makedirs(work_dir, exist_ok=True)
    archives = [archive for archive, _, _ in MF_PK_FONTS] + ["cm.tar.xz"]
    mf_dirs = []
    tfms = {}
    for archive in archives:
        src = os.path.join(work_dir, "src", archive)
        with tarfile.open(os.path.join(cache_dir, archive), "r:xz") as tf:
            tf.extractall(src)
        for root, _, files in os.walk(src):
            if any(f.endswith(".mf") for f in files):
                mf_dirs.append(root)
            for f in files:
                if f.endswith(".tfm"):
                    tfms[(archive, f[:-4])] = os.path.join(root, f)
    env = os.environ.copy()
    env["MFINPUTS"] = ":".join(sorted(mf_dirs))
    # the GF/PK preamble comment carries METAFONT's run time
    env["SOURCE_DATE_EPOCH"] = str(FIXED_MTIME)
    env["FORCE_SOURCE_DATE"] = "1"

    def run_mf(font, dpi, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        mag = f"{dpi // MF_PK_BDPI}+{dpi % MF_PK_BDPI}/{MF_PK_BDPI}"
        # like mktexpk, accept METAFONT's recoverable errors (bbmssbx10's
        # "Strange path") as long as it wrote the GF file
        subprocess.run(
            [mf_exe, "-progname=mf", f"\\mode:={MF_PK_MODE}; mag:={mag}; nonstopmode; input {font}"],
            cwd=out_dir, env=env, capture_output=True,
        )
        if not os.path.exists(os.path.join(out_dir, f"{font}.{dpi}gf")):
            raise RuntimeError(f"METAFONT wrote no {font}.{dpi}gf")
        subprocess.run([gftopk_exe, f"{font}.{dpi}gf", f"{font}.{dpi}pk"],
                       cwd=out_dir, env=env, check=True, capture_output=True)
        return os.path.join(out_dir, f"{font}.{dpi}pk")

    generated_records = {}
    pkg_owners = {}
    jobs = []
    for archive, tds_dir, names in MF_PK_FONTS:
        pkg = archive[: -len(".tar.xz")]
        for font in names or sorted(name for owner, name in tfms if owner == archive):
            tfm = tfms.get((archive, font))
            if tfm is None:
                # not shipped as a TFM (cmcsc12): METAFONT writes it
                out_dir = os.path.join(work_dir, "tfm", font)
                run_mf(font, MF_PK_BDPI, out_dir)
                tfm = os.path.join(out_dir, f"{font}.tfm")
                rel = f"fonts/tfm/public/cmextra/{font}.tfm"
                os.makedirs(os.path.join(combined_dir, os.path.dirname(rel)), exist_ok=True)
                shutil.copyfile(tfm, os.path.join(combined_dir, rel))
                generated_records[rel] = sha256_file(tfm)
                pkg_owners[rel] = pkg
            with open(tfm, "rb") as fp:
                dsize = _tfm_design_size_sp(fp.read())
            sizes = [_tex_pt_sp(size) for size in MF_PK_SIZES]
            dpis = sorted({MF_PK_BDPI} | {_pdftex_pk_dpi(at, dsize) for at in sizes if dsize <= 2 * at <= 5 * dsize})
            for dpi in dpis:
                jobs.append((font, dpi, f"fonts/pk/{MF_PK_MODE}/public/{tds_dir}/{font}.{dpi}pk", pkg))

    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as executor:
        built = list(executor.map(
            lambda job: run_mf(job[0], job[1], os.path.join(work_dir, "pk", f"{job[0]}.{job[1]}")), jobs))
    for (font, dpi, rel, pkg), pk in zip(jobs, built):
        dest = os.path.join(combined_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(pk, dest)
        generated_records[rel] = sha256_file(dest)
        pkg_owners[rel] = pkg

    print(f"  [MF PK] Generated {len(generated_records)} files.")
    return {
        "generator": f"mf ({MF_PK_MODE}, {MF_PK_BDPI} dpi) + gftopk, as mktexpk runs them",
        "source_archives": archives,
        "fonts": sorted({job[0] for job in jobs}),
        "total_generated": len(generated_records),
        "files": generated_records,
        "pkg_owners": pkg_owners,
    }

LANGUAGE_DAT_ENTRIES = [
    # English (Knuth original, default language 0)
    ("english", "hyphen.tex", ["usenglish", "USenglish", "american"]),
    ("dumylang", "dumyhyph.tex", []),
    ("nohyphenation", "zerohyph.tex", []),
    # English extensions
    ("ukenglish", "loadhyph-en-gb.tex", ["british", "UKenglish"]),
    ("usenglishmax", "loadhyph-en-us.tex", []),
    # Basque
    ("basque", "loadhyph-eu.tex", []),
    # French
    ("french", "loadhyph-fr.tex", ["patois", "francais"]),
    # German
    ("german", "loadhyph-de-1901.tex", []),
    ("ngerman", "loadhyph-de-1996.tex", []),
    ("swissgerman", "loadhyph-de-ch-1901.tex", []),
    ("german-x-2024-02-28", "dehypht-x-2024-02-28.tex", ["german-x-latest"]),
    ("ngerman-x-2024-02-28", "dehyphn-x-2024-02-28.tex", ["ngerman-x-latest"]),
    # Greek
    ("greek", "loadhyph-el-polyton.tex", ["polygreek"]),
    ("monogreek", "loadhyph-el-monoton.tex", []),
    ("ancientgreek", "loadhyph-grc.tex", []),
    ("ibycus", "ibyhyph.tex", []),
    # Spanish (with mexican and mexicanspanish synonyms)
    ("spanish", "loadhyph-es.tex", ["espanol", "mexican", "mexicanspanish"]),
    # Portuguese (with brazilian and brazil synonyms)
    ("portuguese", "loadhyph-pt.tex", ["portuges", "brazilian", "brazil"]),
    # Russian (authentic 8-bit T2A via ruhyphen)
    ("russian", "loadhyph-ru.tex", []),
    # Pinyin
    ("pinyin", "loadhyph-zh-latn-pinyin.tex", []),
]

def tl_add_hyphen_entries(pkg, arc_path):
    """The `execute AddHyphen` entries of a package's tlpobj, parsed as
    TeXLive::TLUtils::parse_AddHyphen_line does."""
    with tarfile.open(arc_path, "r:xz") as tar:
        tlpobj = tar.extractfile(f"tlpkg/tlpobj/{pkg}.tlpobj").read().decode()
    entries = []
    for line in tlpobj.splitlines():
        if not line.startswith("execute AddHyphen"):
            continue
        entry = {"synonyms": [], "databases": ["dat", "def", "lua"]}
        for word in shlex.split(line[len("execute AddHyphen"):]):
            key, _, value = word.partition("=")
            entry[key] = value.split(",") if key in ("synonyms", "databases") else value
        entries.append(entry)
    return entries


def hyphenation_config_lines(db, entry):
    """TeXLive::TLPOBJ::make_{dat,def,lua}_lines for one AddHyphen entry."""
    name, file = entry["name"], entry["file"]
    lhm, rhm = entry["lefthyphenmin"], entry["righthyphenmin"]
    if db == "dat":
        return [f"{name} {file}\n"] + [f"={s}\n" for s in entry["synonyms"]]
    if db == "def":
        return [f"\\addlanguage{{{n}}}{{{file}}}{{}}{{{lhm}}}{{{rhm}}}\n"
                for n in [name] + entry["synonyms"]]
    synonyms = ", ".join(f"'{s}'" for s in entry["synonyms"])
    lines = [f"['{name}'] = {{", f"\tloader = '{file}',", f"\tlefthyphenmin = {lhm},",
             f"\trighthyphenmin = {rhm},", f"\tsynonyms = {{ {synonyms} }},"]
    for key, field in (("file_patterns", "patterns"), ("file_exceptions", "hyphenation"),
                       ("luaspecial", "special")):
        if key in entry:
            lines.append(f"\t{field} = '{entry[key]}',")
    lines.append("},")
    return [f"\t{line}\n" for line in lines]


def hyphenation_configs(cache_dir, available_basenames):
    """tex/generic/config/language.{dat,def,dat.lua} as tlmgr generates them
    (TeXLive::TLUtils::create_language_*): hyphen-base's language.us* head and
    the AddHyphen lines of every HYPHENATION_COLLECTIONS package in order.
    Every loader must be bundled: no language silently falls back."""
    with tarfile.open(os.path.join(cache_dir, "hyphen-base.tar.xz"), "r:xz") as tar:
        heads = {db: tar.extractfile(f"tex/generic/config/language.us{ext}").read().decode()
                 for db, ext in (("dat", ""), ("def", ".def"), ("lua", ".lua"))}
    body = {"dat": [], "def": [], "lua": []}
    languages = []
    for _, packages in HYPHENATION_COLLECTIONS:
        for pkg in packages:
            entries = tl_add_hyphen_entries(pkg, os.path.join(cache_dir, f"{pkg}.tar.xz"))
            if not entries:
                raise RuntimeError(f"REJECTED: {pkg} declares no AddHyphen entry")
            for entry in entries:
                if entry["file"] not in available_basenames:
                    raise RuntimeError(f"REJECTED: loader {entry['file']} of {entry['name']} is not bundled")
                languages.append(entry["name"])
            for db, cc in (("dat", "%"), ("def", "%"), ("lua", "--")):
                first = True
                for entry in entries:
                    if db not in entry["databases"]:
                        continue
                    if first:
                        body[db].append(f"{cc} from {pkg}:\n")
                        first = False
                    if entry.get("comment"):
                        body[db].append(f"{cc} {entry['comment']}\n")
                    body[db].extend(hyphenation_config_lines(db, entry))
    generated = "Generated by scripts/bundle_packages.py from TeX Live's AddHyphen entries\n"
    contents = {
        "tex/generic/config/language.dat": f"% {generated}" + heads["dat"] + "".join(body["dat"]),
        # language.def keeps its first line, which etex.src checks
        "tex/generic/config/language.def": heads["def"] + "".join(body["def"])
        + "%%% No changes may be made beyond this point.\n\n"
        + "\\uselanguage {USenglish}             %%% This MUST be the last line of the file.\n",
        "tex/generic/config/language.dat.lua": f"-- {generated}" + heads["lua"] + "".join(body["lua"]) + "}\n",
    }
    contents = {rel: text.encode() for rel, text in contents.items()}
    return contents, {
        "files": {rel: {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
                  for rel, data in contents.items()},
        "languages": languages,
    }

LOADER_DEPENDENCY_CLOSURE = {
    "hyphen.tex": [],
    "dumyhyph.tex": [],
    "zerohyph.tex": [],
    "loadhyph-en-gb.tex": ["hyph-en-gb.tex"],
    "loadhyph-en-us.tex": ["hyph-en-us.tex"],
    "loadhyph-eu.tex": ["conv-utf8-ec.tex", "hyph-eu.tex"],
    "loadhyph-fr.tex": ["conv-utf8-ec.tex", "hyph-fr.tex"],
    "loadhyph-de-1901.tex": ["dehypht.tex"],
    "loadhyph-de-1996.tex": ["dehyphn.tex"],
    "loadhyph-de-ch-1901.tex": ["conv-utf8-ec.tex", "hyph-de-ch-1901.tex"],
    "dehypht-x-2024-02-28.tex": ["dehypht-x-2024-02-28.pat"],
    "dehyphn-x-2024-02-28.tex": ["dehyphn-x-2024-02-28.pat"],
    "loadhyph-el-polyton.tex": ["grphyph5.tex"],
    "loadhyph-el-monoton.tex": ["grmhyph5.tex"],
    "loadhyph-grc.tex": ["grahyph5.tex"],
    "ibyhyph.tex": [],
    "loadhyph-es.tex": ["conv-utf8-ec.tex", "hyph-es.tex"],
    "loadhyph-pt.tex": ["conv-utf8-ec.tex", "hyph-pt.tex"],
    "loadhyph-ru.tex": [
        "ruhyphen.tex", "catkoi.tex", "koi2t2a.tex",
        "ruhyphal.tex", "cyryoal.tex", "hypht2.tex",
    ],
    "loadhyph-zh-latn-pinyin.tex": ["hyph-zh-latn-pinyin.ec.tex"],
}


def generate_language_dat(combined_dir):
    """
    Deterministically generates tex/generic/config/language.dat.
    Validates complete physical presence of every declared loader and secondary
    dependency in combined_dir. Strict verification: no silent English fallback.
    The supplement's TeX Live configuration (`hyphenation_configs`) shadows it.
    """
    print("  Validating hyphenation resource closure and generating tex/generic/config/language.dat...")
    available_basenames = set()
    for root, _, files in os.walk(combined_dir):
        for f in files:
            available_basenames.add(f)

    # Validate all loaders and secondary dependencies
    for lang, loader, aliases in LANGUAGE_DAT_ENTRIES:
        if loader not in available_basenames:
            raise RuntimeError(
                f"REJECTED: Missing primary hyphenation pattern loader '{loader}' for language '{lang}'!"
            )
        deps = LOADER_DEPENDENCY_CLOSURE.get(loader, [])
        for dep in deps:
            if dep not in available_basenames:
                raise RuntimeError(
                    f"REJECTED: Missing required dependency '{dep}' for loader '{loader}' (language '{lang}')!"
                )

    lines = [
        "% Deterministically generated language.dat for TeXres",
        "% English default (language 0), authentic 8-bit pattern loaders, zero silent fallbacks",
        "",
    ]
    for lang, loader, aliases in LANGUAGE_DAT_ENTRIES:
        lines.append(f"{lang} {loader}")
        for alias in aliases:
            lines.append(f"={alias}")

    content = "\n".join(lines) + "\n"
    target_rel = "tex/generic/config/language.dat"
    target_full = os.path.join(combined_dir, target_rel)
    os.makedirs(os.path.dirname(target_full), exist_ok=True)
    with open(target_full, "w", encoding="utf-8") as f:
        f.write(content)
    os.utime(target_full, (FIXED_MTIME, FIXED_MTIME))

    fhash = sha256_file(target_full)
    fsz = os.path.getsize(target_full)
    print(f"  Generated {target_rel}: {len(LANGUAGE_DAT_ENTRIES)} languages, {fsz} bytes, sha256={fhash[:16]}...")
    return {
        "output_file": target_rel,
        "sha256": fhash,
        "size_bytes": fsz,
        "total_languages": len(LANGUAGE_DAT_ENTRIES),
        "validated_loaders": sorted(LOADER_DEPENDENCY_CLOSURE.keys()),
        "entries": [
            {"language": lang, "loader": loader, "synonyms": aliases}
            for lang, loader, aliases in LANGUAGE_DAT_ENTRIES
        ],
    }


def normalize_map_line(line):
    """One pdftex.map record with collapsed quoted options, or None for a
    comment. A file token glued to a closing quote (`"... SlantFont"<x.pfb`,
    as in the mathdesign maps) is split off the way updmap writes it."""
    s = line.strip()
    if not s or s[0] in "%#*;":
        return None
    s = re.sub(r'"([^"]*)"', lambda m: '"' + ' '.join(m.group(1).split()) + '"', s)
    return re.sub(r'"<', '" <', s)


def parse_map_components(norm_line):
    parts = norm_line.split()
    if not parts:
        return None, None, None
    tfm = parts[0]
    enc = None
    fontfile = None
    i = 1
    while i < len(parts):
        p = parts[i]
        if p == "<[":
            if i + 1 < len(parts):
                enc = parts[i + 1]
                i += 2
                continue
        elif p == "<<" or p == "<":
            if i + 1 < len(parts):
                nxt = parts[i + 1]
                if nxt.endswith(".enc"):
                    enc = nxt
                else:
                    fontfile = nxt
                i += 2
                continue
        elif p.startswith("<["):
            enc = p[2:]
        elif p.startswith("<<"):
            fontfile = p[2:]
        elif p.startswith("<"):
            if p.endswith(".enc"):
                enc = p[1:]
            else:
                fontfile = p[1:]
        i += 1
    return tfm, enc, fontfile


def build_bundle(baseline_path, output_dir, lock_file_path, cache_dir, legal_dir, sources_out=None):
    """
    Builds packages.tar.zst and packages.lock.json using ONLY hash-pinned upstream archives
    and verified baseline archive. Zero host-derived payload.
    """
    baseline_path = resolve_baseline_path(baseline_path)
    if not os.path.exists(baseline_path):
        raise FileNotFoundError(f"Baseline archive not found at: {baseline_path}")

    print(f"[1/8] Verifying baseline archive: {baseline_path}...")
    baseline_hash = sha256_file(baseline_path)
    baseline_size = os.path.getsize(baseline_path)
    if baseline_hash != PINNED_BASELINE_SHA256:
        raise ValueError(
            f"Baseline archive hash mismatch!\nExpected: {PINNED_BASELINE_SHA256}\nActual:   {baseline_hash}"
        )
    print(f"  Baseline SHA256 verified: {baseline_hash[:16]}... ({baseline_size / (1024*1024):.1f} MiB)")

    if not cache_dir or not os.path.exists(cache_dir):
        raise FileNotFoundError(f"Upstream archive cache directory not found: {cache_dir}")

    print(f"  Verifying and locking upstream package archives in {cache_dir}...")
    for pkg_id, pkg_info in UPSTREAM_PACKAGES.items():
        if pkg_id == "latex-base-legal":
            continue
        arc_name = os.path.basename(pkg_info["upstream_url"])
        arc_path = os.path.join(cache_dir, arc_name)
        if not os.path.exists(arc_path):
            raise FileNotFoundError(f"Required upstream archive missing: {arc_name} for package {pkg_id}")
        actual_h = sha256_file(arc_path)
        expected_h = pkg_info["upstream_sha256"]
        if actual_h != expected_h:
            raise ValueError(
                f"REJECTED: Upstream archive hash mismatch for {pkg_id} ({arc_name})!\n"
                f"  Expected: {expected_h}\n"
                f"  Actual:   {actual_h}"
            )

    print(f"  All {len(UPSTREAM_PACKAGES)-1} upstream package archives verified against cryptographic pins.")

    scratch = tempfile.mkdtemp(prefix="texres_bundle_")
    baseline_dir = os.path.join(scratch, "baseline")
    combined_dir = os.path.join(scratch, "combined")
    os.makedirs(baseline_dir, exist_ok=True)
    os.makedirs(combined_dir, exist_ok=True)

    print(f"[2/8] Extracting baseline archive to scratch...")
    res = subprocess.run(["tar", "-I", "zstd", "-xf", baseline_path, "-C", baseline_dir])
    if res.returncode != 0:
        raise RuntimeError("Failed to extract baseline archive.")

    baseline_by_path = {}
    baseline_by_basename = {}
    baseline_file_count = 0
    for root, _, files in os.walk(baseline_dir):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, baseline_dir).replace("\\", "/")
            fhash = sha256_file(full)
            baseline_by_path[rel] = fhash
            if f not in baseline_by_basename:
                baseline_by_basename[f] = []
            baseline_by_basename[f].append((rel, fhash))
            baseline_file_count += 1

    for item in os.listdir(baseline_dir):
        s = os.path.join(baseline_dir, item)
        d = os.path.join(combined_dir, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)

    print(f"  Baseline tree contains {baseline_file_count} files ({len(baseline_by_basename)} unique basenames).")

    print(f"[3/8] Extracting and verifying payload ONLY from pinned upstream archives...")
    new_files_by_pkg = {}
    new_basenames = {}
    identical_overlays = 0

    for pkg_id, pkg_info in UPSTREAM_PACKAGES.items():
        new_files_by_pkg[pkg_id] = {}
        if pkg_id == "latex-base-legal":
            continue
        arc_name = os.path.basename(pkg_info["upstream_url"])
        arc_path = os.path.join(cache_dir, arc_name)

        with tarfile.open(arc_path, "r:xz") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            for src_dir, dst_dir in pkg_info["tds_dirs"]:
                for m in members:
                    norm_name = m.name
                    if norm_name.startswith("texmf-dist/"):
                        norm_name = norm_name[len("texmf-dist/"):]
                    if not norm_name.startswith(src_dir):
                        continue
                    if norm_name == "fonts/type1/public/mathdesign/mdpgd/md-utrma.pfb":
                        continue
                    if norm_name == src_dir:
                        target_rel = dst_dir
                    else:
                        rel_in = norm_name[len(src_dir):].lstrip("/")
                        target_rel = f"{dst_dir}/{rel_in}"

                    fname = os.path.basename(target_rel)
                    extracted_file = tar.extractfile(m)
                    data = extracted_file.read()
                    fhash = hashlib.sha256(data).hexdigest()

                    # Check for inter-package collisions
                    if fname in new_basenames:
                        other_pkg, other_rel, other_hash = new_basenames[fname]
                        if other_rel != target_rel:
                            raise RuntimeError(
                                f"REJECTED: Inter-package basename collision on '{fname}'!\n"
                                f"  Package {other_pkg}: {other_rel}\n"
                                f"  Package {pkg_id}: {target_rel}"
                            )

                    # Check against baseline
                    if fname in baseline_by_basename:
                        baseline_paths = [r for r, _ in baseline_by_basename[fname]]
                        if target_rel not in baseline_paths:
                            raise RuntimeError(
                                f"REJECTED: Basename path collision with baseline on '{fname}'!\n"
                                f"  Baseline TDS path(s): {baseline_paths}\n"
                                f"  Package {pkg_id} path: {target_rel}"
                            )
                        base_hash = baseline_by_path[target_rel]
                        if base_hash != fhash:
                            raise RuntimeError(
                                f"REJECTED: Conflicting content for existing baseline file '{fname}'!\n"
                                f"  Baseline SHA256: {base_hash}\n"
                                f"  Package SHA256:  {fhash}"
                            )
                        identical_overlays += 1

                    new_basenames[fname] = (pkg_id, target_rel, fhash)
                    new_files_by_pkg[pkg_id][target_rel] = fhash

                    dest_full = os.path.join(combined_dir, target_rel)
                    os.makedirs(os.path.dirname(dest_full), exist_ok=True)
                    with open(dest_full, "wb") as out_fp:
                        out_fp.write(data)

    # Bundle authentic upstream license files into doc/
    package_legal_mapping = {
        "lm": [
            ("GUST-FONT-LICENSE.TXT", "doc/fonts/lm/GUST-FONT-LICENSE.TXT"),
            ("MANIFEST-Latin-Modern.TXT", "doc/fonts/lm/MANIFEST-Latin-Modern.TXT"),
        ],
        "cm-super": [
            ("cm-super-COPYING.txt", "doc/fonts/cm-super/COPYING"),
            ("cm-super-README.txt", "doc/fonts/cm-super/README"),
        ],
        "cbfonts": [
            ("cbfonts-README.txt", "doc/fonts/cbfonts/README"),
        ],
        "greek-inputenc": [
            ("greek-inputenc-README.md", "doc/latex/greek-inputenc/README.md"),
        ],
        "wadalab": [
            ("wadalab-README.txt", "doc/fonts/wadalab/README"),
        ],
        "ipaex": [
            ("IPA_Font_License_Agreement_v1.0.txt", "doc/fonts/ipaex/IPA_Font_License_Agreement_v1.0.txt"),
        ],
        "haranoaji": [
            ("haranoaji-LICENSE.txt", "doc/fonts/haranoaji/LICENSE"),
        ],
        "arphic": [
            ("ARPHICPL.txt", "doc/fonts/arphic-ttf/ARPHICPL.txt"),
        ],
        "uhc": [
            ("uhc-README.txt", "doc/fonts/uhc/umj/README"),
        ],
        "unfonts-core": [
            ("unfonts-COPYING.txt", "doc/fonts/unfonts-core/COPYING"),
            ("unfonts-README.md", "doc/fonts/unfonts-core/README.md"),
        ],
        "nanumtype1": [
            ("nanum-COPYING.txt", "doc/fonts/nanumtype1/COPYING"),
        ],
        "stmaryrd": [
            ("stmaryrd-README.txt", "doc/fonts/stmaryrd/README"),
            ("stmaryrd-README.hoekwater.txt", "doc/fonts/stmaryrd/README.hoekwater"),
        ],
        "bbding": [
            ("bbding-README.txt", "doc/latex/bbding/README"),
        ],
        "babel-russian": [
            ("babel-russian-README.md", "doc/generic/babel-russian/README.md"),
        ],
        "babel-spanish": [
            ("babel-spanish-README.md", "doc/generic/babel-spanish/README.md"),
        ],
        "babel-portuges": [
            ("babel-portuges-README.md", "doc/generic/babel-portuges/README.md"),
        ],
        "rsfs": [
            ("rsfs-README.txt", "doc/fonts/rsfs/README"),
            ("rsfs-README.type1.txt", "doc/fonts/rsfs/README.type1"),
        ],
        "latex-base-legal": [
            ("manifest.txt", "doc/latex/base/manifest.txt"),
            ("legal.txt", "doc/latex/base/legal.txt"),
            ("lppl.txt", "doc/latex/base/lppl.txt"),
            ("README.md", "doc/latex/base/README.md"),
            ("NOTICES-FONTS.txt", "doc/fonts/NOTICES-FONTS.txt"),
        ],
        "times": [
            ("urw-base35-COPYING.txt", "doc/fonts/urw-base35/COPYING"),
            ("urw-base35-README.txt", "doc/fonts/urw-base35/README"),
        ],
        "txfonts": [
            ("txfonts-README.txt", "doc/fonts/txfonts/README"),
        ],
        "mathdesign": [
            ("mathdesign-README.txt", "doc/fonts/mathdesign/README"),
        ],
        "mathpazo": [
            ("mathpazo-README.txt", "doc/fonts/mathpazo/README"),
            ("fpl-COPYING.txt", "doc/fonts/mathpazo/gpl.txt"),
        ],
        'bera': [('bera-LICENSE.txt', 'doc/fonts/bera/LICENSE')],
        'ccicons': [('ccicons-OFL.txt', 'doc/fonts/ccicons/OFL.txt')],
        'lato': [('lato-README.txt', 'doc/fonts/lato/README'), ('lato-OFL.txt', 'doc/fonts/lato/OFL.txt')],
        'stix2-type1': [('stix2-README.txt', 'doc/fonts/stix2-type1/README.txt'),
         ('stix2-OFL.txt', 'doc/fonts/stix2-type1/OFL.txt')],
        'twemojis': [('twemojis-NOTICE.txt', 'doc/latex/twemojis/NOTICE.txt')],
        'xcharter': [('xcharter-README.txt', 'doc/fonts/xcharter/README')],
        'fdsymbol': [
            ('fdsymbol-OFL.txt', 'doc/fonts/fdsymbol/OFL.txt'),
            ('fdsymbol-README.txt', 'doc/latex/fdsymbol/README.txt'),
        ],
    }

    for pkg_id, files_list in package_legal_mapping.items():
        if pkg_id not in new_files_by_pkg:
            new_files_by_pkg[pkg_id] = {}
        for src_name, dst_rel in files_list:
            src_path = os.path.join(legal_dir, src_name)
            if not os.path.exists(src_path):
                raise FileNotFoundError(f"Required legal file not found: {src_path} for package {pkg_id}")
            fhash = sha256_file(src_path)
            new_files_by_pkg[pkg_id][dst_rel] = fhash
            dest_full = os.path.join(combined_dir, dst_rel)
            os.makedirs(os.path.dirname(dest_full), exist_ok=True)
            shutil.copy2(src_path, dest_full)

    print(f"  Pure archive extraction verified: {len(new_basenames)} unique basenames added.")
    print(f"  Verified clean overlays: {identical_overlays} identical files, 0 conflicting basenames.")

    print(f"[4/8] Generating complete LH Cyrillic metrics and consolidating default pdftex.map...")
    lh_provenance = generate_lh_metrics(combined_dir, cache_dir, scratch)
    for target_rel, fhash in lh_provenance["files"].items():
        fname = os.path.basename(target_rel)
        new_basenames[fname] = ("lh", target_rel, fhash)
        new_files_by_pkg["lh"][target_rel] = fhash

    mf_pk_provenance = generate_metafont_pk(combined_dir, cache_dir, scratch)
    for target_rel, fhash in mf_pk_provenance["files"].items():
        fname = os.path.basename(target_rel)
        pkg_owner = mf_pk_provenance["pkg_owners"][target_rel]
        new_basenames[fname] = (pkg_owner, target_rel, fhash)
        new_files_by_pkg.setdefault(pkg_owner, {})[target_rel] = fhash

    pdftex_map_rel = "fonts/map/pdftex/updmap/pdftex.map"
    pdftex_map_path = os.path.join(combined_dir, pdftex_map_rel)
    os.makedirs(os.path.dirname(pdftex_map_path), exist_ok=True)

    # Collect all bundled basenames to verify physical file presence
    bundled_basenames = set(baseline_by_basename.keys())
    for pkg_files in new_files_by_pkg.values():
        for rel in pkg_files.keys():
            bundled_basenames.add(os.path.basename(rel))

    map_lines = {}
    unavailable_baseline_entries = []
    baseline_root_records = {}

    # Canonical downloadable map for Base-35 PostScript fonts
    canonical_map_rel = "fonts/map/dvips/tetex/ps2pk35.map"
    canonical_map_path = os.path.join(baseline_dir, canonical_map_rel)
    canonical_downloadable_map = {}
    if os.path.exists(canonical_map_path):
        with open(canonical_map_path, "r", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("%"):
                    tfm_cand = s.split()[0]
                    canonical_downloadable_map[tfm_cand] = normalize_map_line(s)

    # 1. Recover complete bindings from baseline family roots
    for rel in BASELINE_FAMILY_MAP_ROOTS:
        map_full = os.path.join(baseline_dir, rel)
        if not os.path.exists(map_full):
            raise FileNotFoundError(f"Declared baseline family root missing from baseline: {rel}")
        with open(map_full, "r", errors="ignore") as f:
            content = f.read()
        fhash = sha256_file(map_full)
        baseline_root_records[rel] = {
            "sha256": fhash,
            "total_entries": 0,
            "retained_entries": 0,
            "unavailable_entries": 0,
        }
        for line in content.splitlines():
            norm = normalize_map_line(line)
            if norm:
                baseline_root_records[rel]["total_entries"] += 1
                tfm, enc, fontfile = parse_map_components(norm)
                # Audit no-file map records and resolve applicable ones to declared upstream resources
                if fontfile is None and tfm in canonical_downloadable_map:
                    resolved_norm = canonical_downloadable_map[tfm]
                    res_tfm, res_enc, res_fontfile = parse_map_components(resolved_norm)
                    res_enc_ok = (res_enc is None) or (res_enc in bundled_basenames)
                    res_font_ok = (res_fontfile is not None) and (res_fontfile in bundled_basenames)
                    if res_enc_ok and res_font_ok:
                        norm = resolved_norm
                        tfm, enc, fontfile = res_tfm, res_enc, res_fontfile

                enc_ok = (enc is None) or (enc in bundled_basenames)
                font_ok = (fontfile is not None) and (fontfile in bundled_basenames)
                if enc_ok and font_ok:
                    map_lines[tfm] = norm + "\n"
                    baseline_root_records[rel]["retained_entries"] += 1
                else:
                    missing = []
                    if not enc_ok:
                        missing.append(f"enc:{enc}")
                    if not font_ok:
                        if fontfile is None:
                            missing.append("font:None (unembedded standard font record)")
                        else:
                            missing.append(f"font:{fontfile}")
                    unavailable_baseline_entries.append({
                        "tfm": tfm,
                        "map": rel,
                        "line": norm,
                        "missing": missing,
                    })
                    baseline_root_records[rel]["unavailable_entries"] += 1

    # 2. Merge all declared upstream family roots
    upstream_root_records = {}
    upstream_added = 0
    for pkg_id, pkg_info in UPSTREAM_PACKAGES.items():
        for mf_rel in pkg_info["map_files"]:
            mf_path = os.path.join(combined_dir, mf_rel)
            if not os.path.exists(mf_path):
                raise FileNotFoundError(f"Declared upstream map file missing: {mf_rel} in {pkg_id}")
            fhash = sha256_file(mf_path)
            upstream_root_records[mf_rel] = {
                "package": pkg_id,
                "sha256": fhash,
                "entries": 0,
            }
            with open(mf_path, "r", errors="ignore") as f:
                for line in f:
                    norm = normalize_map_line(line)
                    if norm:
                        upstream_root_records[mf_rel]["entries"] += 1
                        tfm, enc, fontfile = parse_map_components(norm)
                        enc_ok = (enc is None) or (enc in bundled_basenames)
                        font_ok = (fontfile is not None) and (fontfile in bundled_basenames)
                        if not (enc_ok and font_ok):
                            missing = []
                            if not enc_ok:
                                missing.append(f"enc:{enc}")
                            if not font_ok:
                                if fontfile is None:
                                    missing.append("font:None (unembedded record)")
                                else:
                                    missing.append(f"font:{fontfile}")
                            unavailable_baseline_entries.append({
                                "tfm": tfm,
                                "map": mf_rel,
                                "line": norm,
                                "missing": missing,
                            })
                            continue
                        if tfm not in map_lines:
                            upstream_added += 1
                        map_lines[tfm] = norm + "\n"
    with open(pdftex_map_path, "w") as f:
        f.write("% Consolidated pdftex.map for texres distribution\n")
        f.write("% Generated deterministically from declared family map roots\n")
        for tfm in sorted(map_lines.keys()):
            f.write(map_lines[tfm])

    print(f"  Consolidated {len(map_lines)} total map entries ({upstream_added} from upstream roots, "
          f"{len(map_lines)-upstream_added} retained from baseline family roots, "
          f"{len(unavailable_baseline_entries)} unavailable baseline entries documented).")
    print(f"[5/8] Generating validated language.dat hyphenation closure...")
    lang_dat_provenance = generate_language_dat(combined_dir)

    print(f"[6/8] Writing deterministic sorted tar archive...")
    all_files = []
    total_uncompressed = 0
    unique_basenames = set()

    for root, dirs, files in os.walk(combined_dir):
        dirs.sort()
        for f in sorted(files):
            full = os.path.join(root, f)
            rel = os.path.relpath(full, combined_dir).replace("\\", "/")
            all_files.append((rel, full))
            total_uncompressed += os.path.getsize(full)
            unique_basenames.add(f)

    all_files.sort(key=lambda x: x[0])
    print(f"  Sorting and packing {len(all_files)} total files ({total_uncompressed / (1024*1024):.1f} MiB uncompressed)...")

    temp_full = os.path.join(scratch, "packages.tar.zst.full")
    start_time = time.time()
    zstd_proc = subprocess.Popen(
        ["zstd", "-19", "-T0", "-o", temp_full],
        stdin=subprocess.PIPE,
    )

    with tarfile.open(fileobj=zstd_proc.stdin, mode="w|") as tar:
        for rel, full in all_files:
            ti = tar.gettarinfo(full, arcname=rel)
            ti.uid = 0
            ti.gid = 0
            ti.uname = "root"
            ti.gname = "root"
            ti.mtime = FIXED_MTIME
            ti.mode = 0o644
            with open(full, "rb") as f:
                tar.addfile(ti, f)

    zstd_proc.stdin.close()
    zstd_proc.wait()
    if zstd_proc.returncode != 0:
        raise RuntimeError("zstd compression failed.")

    elapsed = time.time() - start_time
    output_size = os.path.getsize(temp_full)
    output_hash = sha256_file(temp_full)
    print(f"  Full archive created in {elapsed:.1f}s: {output_size} bytes ({output_size / (1024*1024):.2f} MiB, sha256={output_hash[:16]}...)")

    print(f"[7/8] Sharding into deterministic <=70 MiB parts...")
    os.makedirs(output_dir, exist_ok=True)
    for item in os.listdir(output_dir):
        if item == "packages.tar.zst" or item.startswith("packages.tar.zst."):
            os.remove(os.path.join(output_dir, item))

    parts = []
    idx = 0
    with open(temp_full, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE_LIMIT)
            if not chunk:
                break
            part_name = f"packages.tar.zst.{idx:02d}"
            part_path = os.path.join(output_dir, part_name)
            with open(part_path, "wb") as pf:
                pf.write(chunk)
            phash = sha256_file(part_path)
            parts.append({
                "name": part_name,
                "sha256": phash,
                "size_bytes": len(chunk),
            })
            print(f"  Part {part_name}: {len(chunk)} bytes ({len(chunk)/(1024*1024):.2f} MiB, sha256={phash[:16]}...)")
            idx += 1

    # Optional source distribution archive generation
    source_dist_data = None
    if sources_out:
        source_dist_data = build_source_archive(sources_out, cache_dir)
    else:
        # Compute source distribution records from cache if present
        source_records = {}
        for pkg_id, info in sorted(SOURCE_ARCHIVES_INFO.items()):
            arc_path = os.path.join(cache_dir, info["archive"])
            if os.path.exists(arc_path):
                source_records[pkg_id] = {
                    "archive": info["archive"],
                    "url": info["url"],
                    "sha256": sha256_file(arc_path),
                    "size_bytes": os.path.getsize(arc_path),
                    "license": info["license"],
                    "provenance": info.get("provenance", info.get("obligation", "")),
                }
        source_dist_data = {
            "logical_name": "sources.tar.zst",
            "description": "Corresponding source archives for copyleft and source redistribution compliance",
            "source_archives": source_records,
        }

    print(f"[8/8] Generating machine-readable asset lock: {lock_file_path}...")
    package_records = {}
    for pkg_id, pkg_info in UPSTREAM_PACKAGES.items():
        files_dict = new_files_by_pkg.get(pkg_id, {})
        package_records[pkg_id] = {
            "version": pkg_info["version"],
            "revision": pkg_info["revision"],
            "license": pkg_info["license"],
            "ctan_path": pkg_info["ctan_path"],
            "upstream_url": pkg_info["upstream_url"],
            "upstream_sha256": pkg_info.get("upstream_sha256", ""),
            "upstream_size_bytes": pkg_info.get("upstream_size_bytes", 0),
            "description": pkg_info["description"],
            "source_obligations": pkg_info["source_obligations"],
            "license_files": pkg_info["license_files"],
            "map_files": pkg_info["map_files"],
            "file_count": len(files_dict),
            "files": files_dict,
        }

    lock_data = {
        "version": 1,
        "format": "texres-packages-lock-v1",
        "generated_at": "2024-01-01T00:00:00Z",
        "baseline_archive": {
            "url": "https://github.com/leoliu0/texres/releases/download/v0.3.0/ratex-v0.3.0.bundle",
            "sha256": PINNED_BASELINE_SHA256,
            "size_bytes": baseline_size,
            "total_files": baseline_file_count,
            "verified": True,
        },
        "source_distribution": source_dist_data,
        "output_archive": {
            "logical_name": "packages.tar.zst",
            "sha256": output_hash,
            "size_bytes": output_size,
            "total_members": len(all_files),
            "unique_basenames": len(unique_basenames),
            "uncompressed_bytes": total_uncompressed,
            "sharding": {
                "shard_size_limit_bytes": CHUNK_SIZE_LIMIT,
                "total_parts": len(parts),
                "parts": parts,
            },
        },
        "packages": package_records,
        "map_roots": {
            "output_map": pdftex_map_rel,
            "total_entries": len(map_lines),
            "upstream_roots": upstream_root_records,
            "baseline_roots": baseline_root_records,
            "unavailable_baseline_entries": unavailable_baseline_entries,
        },
        "hyphenation_config": lang_dat_provenance,
        "metric_closures": {
            "lh_cyrillic": {
                "compiler": lh_provenance["compiler"],
                "mode": lh_provenance["mode"],
                "total_generated": lh_provenance["total_generated"],
                "source_archives": lh_provenance["source_archives"],
            },
        },
        "metafont_pk_closures": {
            "generator": mf_pk_provenance["generator"],
            "total_generated": mf_pk_provenance["total_generated"],
            "source_archives": mf_pk_provenance["source_archives"],
        },
        "basename_collision_policy": {
            "status": "verified_clean",
            "inter_package_collisions": 0,
            "conflicting_basenames_rejected": 0,
            "identical_baseline_overlays": identical_overlays,
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(lock_file_path)), exist_ok=True)
    with open(lock_file_path, "w") as f:
        json.dump(lock_data, f, indent=2)

    shutil.rmtree(scratch, ignore_errors=True)
    print("Archive generation and lock recording complete successfully!")
    return lock_data


def read_main_archive(assets_dir, lock_data, wanted):
    """Stream the locked main archive. Returns its member paths (archive
    order) and the bytes of the members named in `wanted`."""
    parts = [p["name"] for p in lock_data["output_archive"]["sharding"]["parts"]]
    cat = subprocess.Popen(["cat"] + [os.path.join(assets_dir, p) for p in parts], stdout=subprocess.PIPE)
    unzstd = subprocess.Popen(["zstd", "-dc"], stdin=cat.stdout, stdout=subprocess.PIPE)
    cat.stdout.close()
    members, contents = [], {}
    with tarfile.open(fileobj=unzstd.stdout, mode="r|") as tar:
        for m in tar:
            if not m.isfile():
                continue
            members.append(m.name)
            if m.name in wanted:
                contents[m.name] = tar.extractfile(m).read()
    if unzstd.wait() != 0 or cat.wait() != 0:
        raise RuntimeError("failed to decompress the locked main archive")
    return members, contents


def add_supplement_notice(added, main_members, legal_dir, legal_name, dest_rel, record):
    """Ship a license text from the legal directory at dest_rel. Notices are
    documentation, never looked up by basename, so only the full path must be new."""
    src = os.path.join(legal_dir, legal_name)
    if not os.path.exists(src):
        raise FileNotFoundError(f"Required legal file not found: {src}")
    if dest_rel in main_members or dest_rel in added:
        raise RuntimeError(f"REJECTED: notice path already shipped: {dest_rel}")
    with open(src, "rb") as f:
        data = f.read()
    added[dest_rel] = data
    record[dest_rel] = hashlib.sha256(data).hexdigest()


def build_supplement(assets_dir, lock_file_path, cache_dir, legal_dir):
    """Write the append-only overlay archive described at SUPPLEMENT_PACKAGES
    and record it in the lock, leaving the main archive parts untouched."""
    with open(lock_file_path, "r") as f:
        lock_data = json.load(f)
    check_lock(assets_dir, lock_file_path)
    root_maps = set(SUPPLEMENT_MAP_ROOTS) | {PDFTEX_MAP_REL}
    for info in SUPPLEMENT_PACKAGES.values():
        root_maps.update(info["map_files"])
    main_members, main_contents = read_main_archive(assets_dir, lock_data, root_maps)
    main_by_basename = {}
    for rel in main_members:
        main_by_basename.setdefault(os.path.basename(rel), rel)

    added = {}
    package_records = {}
    for pkg_id, info in SUPPLEMENT_PACKAGES.items():
        arc_path = os.path.join(cache_dir, os.path.basename(info["upstream_url"]))
        if not os.path.exists(arc_path):
            raise FileNotFoundError(f"Required upstream archive missing: {arc_path} for {pkg_id}")
        if sha256_file(arc_path) != info["upstream_sha256"]:
            raise ValueError(f"REJECTED: upstream archive hash mismatch for {pkg_id} ({arc_path})")
        files = {}
        with tarfile.open(arc_path, "r:xz") as tar:
            for m in sorted((m for m in tar.getmembers() if m.isfile()), key=lambda m: m.name):
                rel = m.name[len("texmf-dist/"):] if m.name.startswith("texmf-dist/") else m.name
                selected = any(
                    rel.startswith(prefix + "/")
                    and (exts is None or rel.lower().endswith(exts))
                    for prefix, exts in info["select"]
                )
                if not selected:
                    continue
                data = tar.extractfile(m).read()
                fname = os.path.basename(rel)
                shadow = main_by_basename.get(fname)
                if shadow == rel:
                    continue
                twin = next((r for r in added if os.path.basename(r) == fname), None)
                if twin is not None and shadow is None and added[twin] == data:
                    # Two packages ship the byte-identical file (polyglossia and
                    # arabxetex digit mappings); lookup by basename is unambiguous.
                    continue
                if shadow is not None or fname in {os.path.basename(r) for r in added}:
                    raise RuntimeError(
                        f"REJECTED: basename collision on '{fname}': {rel} vs {shadow or 'supplement'}"
                    )
                added[rel] = data
                files[rel] = hashlib.sha256(data).hexdigest()
        package_records[pkg_id] = {
            key: info[key]
            for key in ("version", "revision", "license", "upstream_url", "upstream_sha256",
                        "upstream_size_bytes", "map_files")
        }
        for key in ("description", "source_obligations"):
            if key in info:
                package_records[pkg_id][key] = info[key]
        package_records[pkg_id]["file_count"] = len(files)
        package_records[pkg_id]["files"] = files
        if info.get("notices"):
            package_records[pkg_id]["notices"] = {}
            for legal_name, dest_rel in info["notices"]:
                add_supplement_notice(added, main_members, legal_dir, legal_name, dest_rel,
                                      package_records[pkg_id]["notices"])

    # TeX Live maps no METAFONT-only font (bbm, ifsym, cmbcsc10, cmcsc12):
    # pdfTeX embeds their PK bitmaps. Their records in the main archive's
    # pdftex.map are dropped below, leaving its traced .pfb files unused.
    with tempfile.TemporaryDirectory() as scratch:
        combined = os.path.join(scratch, "combined")
        os.makedirs(combined)
        mf_pk = generate_metafont_pk(combined, cache_dir, scratch)
        mf_pk_files = {}
        for rel in sorted(mf_pk["files"]):
            if not rel.endswith("pk"):
                continue
            if os.path.basename(rel) in main_by_basename or rel in added:
                raise RuntimeError(f"REJECTED: basename collision on '{rel}'")
            with open(os.path.join(combined, rel), "rb") as fp:
                data = fp.read()
            added[rel] = data
            mf_pk_files[rel] = hashlib.sha256(data).hexdigest()
    mf_pk_record = {
        "generator": mf_pk["generator"],
        "source_archives": mf_pk["source_archives"],
        "fonts": mf_pk["fonts"],
        "file_count": len(mf_pk_files),
        "files": mf_pk_files,
    }

    notice_records = {}
    for legal_name, dest_rel in SUPPLEMENT_EXTRA_NOTICES:
        add_supplement_notice(added, main_members, legal_dir, legal_name, dest_rel, notice_records)

    # Consolidate pdftex.map: main records stay byte-identical; the declared
    # roots contribute records for still-unmapped TFMs whose files exist.
    available = set(main_by_basename) | {os.path.basename(r) for r in added}
    hyphenation_files, hyphenation_provenance = hyphenation_configs(cache_dir, available)
    added.update(hyphenation_files)
    main_map = main_contents[PDFTEX_MAP_REL].decode()
    header = [line for line in main_map.splitlines(keepends=True) if line.startswith("%")]
    map_lines = {}
    for line in main_map.splitlines(keepends=True):
        norm = normalize_map_line(line)
        if norm:
            map_lines[norm.split()[0]] = line
    map_roots = {}
    unavailable = []
    for rel in SUPPLEMENT_MAP_ROOTS + [m for info in SUPPLEMENT_PACKAGES.values() for m in info["map_files"]]:
        text = main_contents.get(rel) or added.get(rel)
        if text is None:
            raise FileNotFoundError(f"Declared supplement map root missing: {rel}")
        record = {"sha256": hashlib.sha256(text).hexdigest(), "added_entries": 0, "unavailable_entries": 0}
        for line in text.decode(errors="ignore").splitlines():
            norm = normalize_map_line(line)
            if not norm:
                continue
            tfm, enc, fontfile = parse_map_components(norm)
            if tfm in map_lines:
                continue
            missing = [f for f in (enc, fontfile) if f is not None and f not in available]
            if fontfile is None or missing:
                record["unavailable_entries"] += 1
                unavailable.append({"tfm": tfm, "map": rel, "line": norm,
                                    "missing": missing or ["font:None (unembedded record)"]})
                continue
            map_lines[tfm] = norm + "\n"
            record["added_entries"] += 1
        map_roots[rel] = record
    for tfm in mf_pk["fonts"]:
        map_lines.pop(tfm, None)
    pdftex_map = "".join(header) + "".join(map_lines[t] for t in sorted(map_lines))
    added[PDFTEX_MAP_REL] = pdftex_map.encode()

    out_path = os.path.join(assets_dir, SUPPLEMENT_ARCHIVE_NAME)
    zstd_proc = subprocess.Popen(["zstd", "-19", "-q", "-f", "-o", out_path], stdin=subprocess.PIPE)
    with tarfile.open(fileobj=zstd_proc.stdin, mode="w|", format=tarfile.USTAR_FORMAT) as tar:
        for rel in sorted(added):
            ti = tarfile.TarInfo(rel)
            ti.size = len(added[rel])
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = FIXED_MTIME
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(added[rel]))
    zstd_proc.stdin.close()
    if zstd_proc.wait() != 0:
        raise RuntimeError("zstd compression failed.")

    lock_data["supplement_archive"] = {
        "name": SUPPLEMENT_ARCHIVE_NAME,
        "sha256": sha256_file(out_path),
        "size_bytes": os.path.getsize(out_path),
        "total_members": len(added),
        "precedence": "read before the main archive; its members shadow same-named main members",
        "packages": package_records,
        "mf_pk": mf_pk_record,
        "notices": notice_records,
        "hyphenation_config": hyphenation_provenance,
        "map_roots": {
            "output_map": PDFTEX_MAP_REL,
            "total_entries": len(map_lines),
            "roots": map_roots,
            "unavailable_entries": unavailable,
        },
    }
    with open(lock_file_path, "w") as f:
        json.dump(lock_data, f, indent=2)
    print(f"Wrote {out_path}: {len(added)} members, {os.path.getsize(out_path)} bytes; "
          f"pdftex.map {len(map_lines)} entries")


def check_lock(assets_dir, lock_file_path):
    if not os.path.exists(lock_file_path):
        raise FileNotFoundError(f"Lock file not found: {lock_file_path}")
    with open(lock_file_path, "r") as f:
        data = json.load(f)

    parts_info = list(data["output_archive"]["sharding"]["parts"])
    if "supplement_archive" in data:
        parts_info.append(data["supplement_archive"])
    print(f"Checking {len(parts_info)} asset files...")
    for part in parts_info:
        name = part["name"]
        expected_hash = part["sha256"]
        expected_size = part["size_bytes"]
        p = os.path.join(assets_dir, name)
        if not os.path.exists(p):
            raise FileNotFoundError(f"Missing part file: {p}")
        actual_size = os.path.getsize(p)
        if actual_size != expected_size:
            raise ValueError(f"Size mismatch on {name}: expected {expected_size}, got {actual_size}")
        actual_hash = sha256_file(p)
        if actual_hash != expected_hash:
            raise ValueError(f"Hash mismatch on {name}:\nExpected: {expected_hash}\nActual:   {actual_hash}")
        print(f"  OK: {name} (SHA256: {actual_hash[:16]}...)")
    print("All asset files match lockfile perfectly!")


def reconstruct_archive(assets_dir, output_path, lock_file_path=None):
    if lock_file_path and os.path.exists(lock_file_path):
        with open(lock_file_path, "r") as f:
            data = json.load(f)
        part_names = [p["name"] for p in data["output_archive"]["sharding"]["parts"]]
    else:
        part_names = sorted([p for p in os.listdir(assets_dir) if p.startswith("packages.tar.zst.")])

    if not part_names:
        raise FileNotFoundError(f"No packages.tar.zst.* parts found in {assets_dir}")

    with open(output_path, "wb") as out:
        for p in part_names:
            full = os.path.join(assets_dir, p)
            with open(full, "rb") as inp:
                shutil.copyfileobj(inp, out)

    reconstructed_hash = sha256_file(output_path)
    reconstructed_size = os.path.getsize(output_path)
    print(f"Reconstructed {output_path}: {reconstructed_size} bytes, SHA256: {reconstructed_hash}")


def main():
    parser = argparse.ArgumentParser(description="Deterministic generator for TeXres packages archive, sources, and lock")
    parser.add_argument(
        "--baseline",
        default="/tmp/texres-baseline/packages.tar.zst",
        help="Path to baseline packages.tar.zst",
    )
    parser.add_argument(
        "--assets-dir",
        default="crates/tex-kpse/assets",
        help="Target assets directory",
    )
    parser.add_argument(
        "--lock",
        default="crates/tex-kpse/assets/packages.lock.json",
        help="Path to output packages.lock.json",
    )
    parser.add_argument(
        "--legal-dir",
        default="crates/tex-kpse/assets/legal",
        help="Path to legal/notices directory",
    )
    parser.add_argument(
        "--cache-dir",
        default="/tmp/texres-upstream-cache",
        help="Cache directory for upstream archives",
    )
    parser.add_argument(
        "--sources-out",
        default="crates/tex-kpse/assets/sources.tar.zst",
        help="Path to output sources.tar.zst archive",
    )
    parser.add_argument(
        "--build-sources",
        action="store_true",
        help="Build source distribution archive only",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify existing asset parts against packages.lock.json",
    )
    parser.add_argument(
        "--reconstruct",
        metavar="OUTPUT_PATH",
        help="Reconstruct full single packages.tar.zst from assets/ parts into OUTPUT_PATH",
    )
    parser.add_argument(
        "--supplement",
        action="store_true",
        help="Build the append-only packages-supplement.tar.zst overlay from SUPPLEMENT_PACKAGES "
        "and record it in the lock (rerun after a full rebuild, which rewrites the lock)",
    )

    args = parser.parse_args()

    if args.check:
        check_lock(args.assets_dir, args.lock)
        return

    if args.reconstruct:
        reconstruct_archive(args.assets_dir, args.reconstruct, args.lock)
        return

    if args.supplement:
        build_supplement(args.assets_dir, args.lock, args.cache_dir, args.legal_dir)
        return

    if args.build_sources:
        build_source_archive(args.sources_out, args.cache_dir)
        return

    build_bundle(
        baseline_path=args.baseline,
        output_dir=args.assets_dir,
        lock_file_path=args.lock,
        cache_dir=args.cache_dir,
        legal_dir=args.legal_dir,
        sources_out=args.sources_out,
    )


if __name__ == "__main__":
    main()
