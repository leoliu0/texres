# TeXres vs TeX Live: 100-document benchmark

Run 2026-11-25T08:25:22+08:00; 5 timed runs per cell (median), tools alternating; jobs=6, CPUs/job=4.

Ratio = TeXres wall time / TeX Live wall time (below 1: TeXres faster).

## Environment

| | |
| --- | --- |
| CPU | AMD Ryzen 9 9950X3D2 16-Core Processor (32 logical) |
| RAM / OS | 186.4 GiB; Arch Linux, kernel 7.3.5-arch1-1; governor `powersave` |
| TeXres | texres 0.7.11 (Rust TeX engine) (sha256 `0440f5328c3796bd…`); eb6fdce, scripts/build_pgo.sh (PGO, fat LTO, as release.yml ships); reference TeX Live aligned with locked TeX Live 2026 TEXMFLOCAL overlay |
| TeX Live | pdfTeX 3.141592653-2.6-1.40.29 (TeX Live 2026/Arch Linux); XeTeX 3.141592653-2.6-0.999998 (TeX Live 2026/Arch Linux); This is LuaHBTeX, Version 1.24.0 (TeX Live 2026/Arch Linux); Latexmk, John Collins, 15 June 2025. Version 4.87; texres 0.7.11 (Rust TeX engine) |
| Load average | start 3.4, end 5.7 |

## Corpus composition

| Group | Documents |
| --- | ---: |
| arxiv, pdf | 24 |
| generated, lua | 15 |
| generated, pdf | 31 |
| generated, xe | 24 |
| github, pdf | 5 |
| github, xe | 1 |
| **total** | 100 |

## Status

| Status | Documents |
| --- | ---: |
| timed | 100 |

## TeX Live Parity & Similarity

| Metric | Value |
| --- | ---: |
| Page-count parity | 100 / 100 |
| Exact text parity (`pdftotext -layout`) | 100 / 100 |
| Text similarity (min / median / mean) | 1.0000 / 1.0000 / 1.0000 |
| Composite similarity (min / median / mean) | 1.0000 / 1.0000 / 1.0000 |
| Release gate | **PASS** (min_similarity >= 0.980, min_text_similarity >= 1.000, max_median_wall_ratio <= 1.00) |

## Summary: documents where TeXres is faster / slower

| Scenario | TeXres faster | TeXres slower | Median ratio | Worst ratio |
| --- | ---: | ---: | ---: | ---: |
| (a) cold build | 100 | 0 | 0.51 | 0.90 (pdf_siunitx_tables) |
| (b) no-change rebuild | 100 | 0 | 0.08 | 0.27 (arxiv_vae) |
| (c) one-line edit rebuild | 100 | 0 | 0.49 | 0.86 (pdf_siunitx_tables) |

## All timed documents, slowest first (by worst ratio over scenarios)

| Document | Engine | Pages | (a) TeXres / TL s | ratio | (b) TeXres / TL s | ratio | (c) TeXres / TL s | ratio |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| pdf_siunitx_tables | pdf | 19 | 1.94 / 2.15 | 0.90 | 0.003 / 0.044 | 0.07 | 0.652 / 0.757 | 0.86 |
| gh_lkmpg_book | pdf | 193 | 9.91 / 13.07 | 0.76 | 0.012 / 0.064 | 0.18 | 3.47 / 4.60 | 0.75 |
| lua_beamer | lua | 21 | 1.47 / 2.00 | 0.73 | 0.005 / 0.053 | 0.09 | 0.741 / 1.01 | 0.73 |
| pdf_beamer_metropolis | pdf | 73 | 1.38 / 1.89 | 0.73 | 0.003 / 0.045 | 0.08 | 0.697 / 0.973 | 0.72 |
| lua_siunitx | lua | 5 | 1.40 / 1.95 | 0.72 | 0.004 / 0.049 | 0.08 | 0.465 / 0.672 | 0.69 |
| pdf_scrbook_large | pdf | 255 | 2.18 / 3.24 | 0.67 | 0.006 / 0.045 | 0.14 | 0.757 / 1.09 | 0.70 |
| pdf_pgfplots_gallery | pdf | 19 | 4.48 / 6.77 | 0.66 | 0.003 / 0.045 | 0.08 | 1.50 / 2.30 | 0.65 |
| lua_tikz_pgfplots | lua | 6 | 1.85 / 2.82 | 0.66 | 0.004 / 0.051 | 0.08 | 0.626 / 0.971 | 0.64 |
| pdf_tikz_diagrams | pdf | 22 | 2.46 / 3.75 | 0.66 | 0.004 / 0.046 | 0.08 | 0.828 / 1.31 | 0.63 |
| lua_microtype | lua | 21 | 1.48 / 2.31 | 0.64 | 0.004 / 0.050 | 0.09 | 0.507 / 0.786 | 0.65 |
| lua_polyglossia_french | lua | 3 | 0.980 / 1.53 | 0.64 | 0.004 / 0.050 | 0.09 | 0.329 / 0.544 | 0.61 |
| xe_beamer | xe | 21 | 0.981 / 1.54 | 0.64 | 0.004 / 0.048 | 0.07 | 0.500 / 0.827 | 0.60 |
| pdf_beamer_boadilla | pdf | 13 | 1.32 / 2.08 | 0.63 | 0.004 / 0.048 | 0.09 | 0.658 / 1.04 | 0.63 |
| lua_book | lua | 40 | 0.945 / 1.49 | 0.63 | 0.004 / 0.049 | 0.09 | 0.473 / 0.782 | 0.60 |
| arxiv_lipics_paper | pdf | 30 | 2.15 / 3.45 | 0.62 | 0.006 / 0.050 | 0.12 | 0.733 / 1.18 | 0.62 |
| xe_tikz_pgfplots | xe | 7 | 1.92 / 3.09 | 0.62 | 0.003 / 0.045 | 0.07 | 0.639 / 1.08 | 0.59 |
| pdf_memoir_book | pdf | 118 | 1.06 / 1.71 | 0.62 | 0.004 / 0.043 | 0.10 | 0.360 / 0.583 | 0.62 |
| gh_mtheme_demo | pdf | 33 | 2.13 / 3.50 | 0.61 | 0.005 / 0.054 | 0.10 | 0.731 / 1.19 | 0.62 |
| gh_mtheme_demo_xelatex | xe | 33 | 2.40 / 3.91 | 0.62 | 0.005 / 0.051 | 0.09 | 0.816 / 1.38 | 0.59 |
| arxiv_jhep_6d_scft | pdf | 28 | 1.74 / 2.83 | 0.61 | 0.005 / 0.049 | 0.11 | 0.594 / 0.970 | 0.61 |
| pdf_listings_algorithm2e | pdf | 31 | 0.585 / 0.961 | 0.61 | 0.003 / 0.044 | 0.08 | 0.202 / 0.343 | 0.59 |
| pdf_hyperref_toc | pdf | 68 | 0.728 / 1.20 | 0.61 | 0.004 / 0.045 | 0.09 | 0.256 / 0.437 | 0.59 |
| lua_luacode_env | lua | 3 | 0.801 / 1.33 | 0.60 | 0.004 / 0.049 | 0.08 | 0.273 / 0.478 | 0.57 |
| xe_siunitx | xe | 5 | 0.849 / 1.42 | 0.60 | 0.003 / 0.046 | 0.06 | 0.286 / 0.540 | 0.53 |
| lua_biblatex | lua | 5 | 2.12 / 3.56 | 0.60 | 0.006 / 0.052 | 0.12 | 0.553 / 0.955 | 0.58 |
| lua_directlua | lua | 3 | 0.807 / 1.35 | 0.60 | 0.004 / 0.048 | 0.08 | 0.270 / 0.476 | 0.57 |
| arxiv_math_nt_amsart | pdf | 32 | 0.660 / 1.14 | 0.58 | 0.004 / 0.045 | 0.09 | 0.233 / 0.395 | 0.59 |
| arxiv_graded_xy | pdf | 22 | 0.705 / 1.21 | 0.58 | 0.005 / 0.047 | 0.10 | 0.248 / 0.436 | 0.57 |
| arxiv_lmcs_eating | pdf | 30 | 1.05 / 1.84 | 0.57 | 0.005 / 0.049 | 0.11 | 0.522 / 0.903 | 0.58 |
| lua_fontspec_pagella | lua | 4 | 1.49 / 2.57 | 0.58 | 0.004 / 0.051 | 0.08 | 0.508 / 0.914 | 0.56 |
| lua_fontspec_termes | lua | 12 | 1.60 / 2.77 | 0.58 | 0.004 / 0.050 | 0.09 | 0.542 / 0.964 | 0.56 |
| lua_polyglossia_russian | lua | 3 | 0.955 / 1.66 | 0.58 | 0.004 / 0.052 | 0.07 | 0.320 / 0.582 | 0.55 |
| lua_lm_fontspec | lua | 5 | 0.756 / 1.31 | 0.58 | 0.004 / 0.050 | 0.08 | 0.254 / 0.460 | 0.55 |
| xe_ctex_article | xe | 24 | 0.864 / 1.51 | 0.57 | 0.003 / 0.045 | 0.07 | 0.303 / 0.578 | 0.52 |
| xe_xecjk_chinese | xe | 20 | 0.732 / 1.29 | 0.57 | 0.003 / 0.044 | 0.07 | 0.250 / 0.498 | 0.50 |
| arxiv_ecta_paper | pdf | 86 | 5.01 / 8.95 | 0.56 | 0.006 / 0.046 | 0.14 | 1.69 / 3.05 | 0.55 |
| pdf_cleveref_hyperref | pdf | 25 | 0.474 / 0.858 | 0.55 | 0.004 / 0.043 | 0.08 | 0.162 / 0.313 | 0.52 |
| arxiv_stat_me_39pp | pdf | 39 | 1.43 / 2.82 | 0.51 | 0.006 / 0.048 | 0.12 | 0.491 / 0.890 | 0.55 |
| xe_ctex_book | xe | 27 | 0.543 / 0.986 | 0.55 | 0.003 / 0.048 | 0.07 | 0.271 / 0.564 | 0.48 |
| arxiv_ehmp_econ | pdf | 54 | 1.19 / 2.20 | 0.54 | 0.007 / 0.050 | 0.14 | 0.419 / 0.764 | 0.55 |
| lua_scrartcl_unicode | lua | 7 | 1.83 / 3.37 | 0.54 | 0.004 / 0.049 | 0.07 | 0.593 / 1.09 | 0.54 |
| arxiv_vae | pdf | 14 | 0.593 / 1.10 | 0.54 | 0.013 / 0.047 | 0.27 | 0.334 / 0.633 | 0.53 |
| pdf_float_heavy | pdf | 41 | 0.361 / 0.674 | 0.54 | 0.003 / 0.042 | 0.07 | 0.125 / 0.254 | 0.49 |
| arxiv_bert | pdf | 16 | 0.929 / 1.83 | 0.51 | 0.005 / 0.048 | 0.11 | 0.356 / 0.665 | 0.53 |
| arxiv_sex_chr_bio | pdf | 20 | 0.327 / 0.618 | 0.53 | 0.007 / 0.044 | 0.16 | 0.174 / 0.330 | 0.53 |
| pdf_scrartcl_floats | pdf | 50 | 0.837 / 1.58 | 0.53 | 0.003 / 0.042 | 0.07 | 0.290 / 0.555 | 0.52 |
| lua_polyglossia_german | lua | 3 | 0.907 / 1.72 | 0.53 | 0.004 / 0.049 | 0.08 | 0.304 / 0.601 | 0.51 |
| pdf_tcolorbox | pdf | 13 | 0.466 / 0.903 | 0.52 | 0.003 / 0.043 | 0.07 | 0.158 / 0.331 | 0.48 |
| pdf_microtype | pdf | 22 | 0.396 / 0.772 | 0.51 | 0.003 / 0.043 | 0.07 | 0.138 / 0.285 | 0.48 |
| arxiv_recursive_partitioning | pdf | 48 | 0.714 / 1.44 | 0.49 | 0.006 / 0.047 | 0.13 | 0.261 / 0.509 | 0.51 |
| pdf_amsart | pdf | 40 | 0.325 / 0.637 | 0.51 | 0.003 / 0.042 | 0.08 | 0.112 / 0.231 | 0.49 |
| pdf_footnote_heavy | pdf | 38 | 0.293 / 0.576 | 0.51 | 0.003 / 0.042 | 0.08 | 0.104 / 0.220 | 0.47 |
| arxiv_sparse_cb_tikz | pdf | 13 | 0.577 / 1.14 | 0.51 | 0.004 / 0.048 | 0.09 | 0.200 / 0.411 | 0.49 |
| xe_polyglossia_german | xe | 20 | 0.628 / 1.25 | 0.50 | 0.003 / 0.046 | 0.07 | 0.212 / 0.488 | 0.43 |
| arxiv_jcap_cmb | pdf | 14 | 0.383 / 0.787 | 0.49 | 0.004 / 0.045 | 0.08 | 0.139 / 0.283 | 0.49 |
| pdf_glossaries | pdf | 4 | 0.351 / 0.716 | 0.49 | 0.003 / 0.042 | 0.07 | 0.126 / 0.271 | 0.47 |
| arxiv_ieeetran_kozen | pdf | 17 | 0.548 / 1.13 | 0.48 | 0.005 / 0.047 | 0.11 | 0.282 / 0.577 | 0.49 |
| xe_biblatex | xe | 6 | 1.47 / 3.02 | 0.49 | 0.005 / 0.045 | 0.11 | 0.390 / 0.867 | 0.45 |
| xe_polyglossia_polish | xe | 4 | 0.588 / 1.22 | 0.48 | 0.003 / 0.044 | 0.06 | 0.199 / 0.469 | 0.42 |
| arxiv_resnet | pdf | 12 | 0.576 / 1.21 | 0.47 | 0.005 / 0.045 | 0.12 | 0.200 / 0.426 | 0.47 |
| arxiv_dml_econometrics_journal | pdf | 71 | 0.583 / 1.27 | 0.46 | 0.005 / 0.045 | 0.11 | 0.207 / 0.437 | 0.47 |
| xe_book_memoir | xe | 25 | 0.412 / 0.876 | 0.47 | 0.003 / 0.046 | 0.07 | 0.209 / 0.504 | 0.41 |
| xe_scrbook_fontspec | xe | 36 | 0.942 / 2.00 | 0.47 | 0.004 / 0.046 | 0.08 | 0.323 / 0.735 | 0.44 |
| gh_tufte_book | pdf | 42 | 0.962 / 2.09 | 0.46 | 0.005 / 0.048 | 0.10 | 0.332 / 0.735 | 0.45 |
| pdf_biblatex_numeric | pdf | 15 | 1.47 / 3.21 | 0.46 | 0.005 / 0.044 | 0.11 | 0.394 / 0.876 | 0.45 |
| pdf_biblatex_ieee | pdf | 9 | 1.39 / 3.06 | 0.45 | 0.005 / 0.043 | 0.12 | 0.390 / 0.865 | 0.45 |
| xe_float_heavy | xe | 22 | 0.509 / 1.13 | 0.45 | 0.003 / 0.046 | 0.07 | 0.174 / 0.458 | 0.38 |
| arxiv_revtex41_neuro | pdf | 17 | 0.428 / 0.959 | 0.45 | 0.007 / 0.046 | 0.15 | 0.223 / 0.500 | 0.45 |
| pdf_report_longtable | pdf | 30 | 0.169 / 0.380 | 0.44 | 0.003 / 0.043 | 0.07 | 0.084 / 0.210 | 0.40 |
| pdf_standalone_tikz | pdf | 1 | 0.135 / 0.312 | 0.43 | 0.004 / 0.045 | 0.08 | 0.134 / 0.303 | 0.44 |
| arxiv_svjour3_paper | pdf | 38 | 0.512 / 1.20 | 0.43 | 0.004 / 0.044 | 0.10 | 0.182 / 0.420 | 0.43 |
| xe_fontspec_schola | xe | 19 | 0.751 / 1.74 | 0.43 | 0.003 / 0.047 | 0.07 | 0.254 / 0.649 | 0.39 |
| pdf_biblatex_authortitle | pdf | 5 | 0.753 / 1.74 | 0.43 | 0.004 / 0.043 | 0.10 | 0.200 / 0.488 | 0.41 |
| arxiv_perelman_ricci_flow | pdf | 39 | 0.183 / 0.425 | 0.43 | 0.004 / 0.043 | 0.08 | 0.096 / 0.236 | 0.41 |
| arxiv_econ_gn_bibtex | pdf | 34 | 0.673 / 2.98 | 0.23 | 0.012 / 0.047 | 0.25 | 0.232 / 0.539 | 0.43 |
| pdf_biblatex_chicago | pdf | 13 | 1.52 / 3.60 | 0.42 | 0.006 / 0.044 | 0.14 | 0.421 / 0.983 | 0.43 |
| gh_phd_thesis_template | pdf | 39 | 1.10 / 2.60 | 0.42 | 0.006 / 0.047 | 0.13 | 0.363 / 0.861 | 0.42 |
| xe_fontspec_pagella | xe | 8 | 0.692 / 1.64 | 0.42 | 0.003 / 0.048 | 0.07 | 0.234 / 0.624 | 0.38 |
| xe_fontspec_termes | xe | 4 | 0.646 / 1.54 | 0.42 | 0.003 / 0.046 | 0.07 | 0.215 / 0.583 | 0.37 |
| gh_tufte_handout | pdf | 5 | 0.588 / 1.50 | 0.39 | 0.004 / 0.046 | 0.09 | 0.210 / 0.515 | 0.41 |
| xe_polyglossia_russian | xe | 4 | 0.429 / 1.05 | 0.41 | 0.003 / 0.046 | 0.07 | 0.141 / 0.422 | 0.33 |
| xe_report_tables | xe | 26 | 0.322 / 0.793 | 0.41 | 0.003 / 0.046 | 0.07 | 0.163 / 0.442 | 0.37 |
| xe_fontspec_bonum | xe | 5 | 0.647 / 1.60 | 0.40 | 0.003 / 0.045 | 0.07 | 0.221 / 0.586 | 0.38 |
| xe_polyglossia_multilingual | xe | 3 | 0.430 / 1.07 | 0.40 | 0.003 / 0.047 | 0.07 | 0.150 / 0.421 | 0.36 |
| xe_polyglossia_french | xe | 4 | 0.414 / 1.04 | 0.40 | 0.003 / 0.045 | 0.07 | 0.144 / 0.414 | 0.35 |
| xe_polyglossia_greek | xe | 10 | 0.417 / 1.07 | 0.39 | 0.003 / 0.046 | 0.07 | 0.147 / 0.446 | 0.33 |
| pdf_makeindex | pdf | 30 | 0.210 / 0.542 | 0.39 | 0.003 / 0.042 | 0.07 | 0.076 / 0.196 | 0.39 |
| xe_polyglossia_spanish | xe | 4 | 0.402 / 1.07 | 0.38 | 0.003 / 0.045 | 0.06 | 0.139 / 0.398 | 0.35 |
| arxiv_attention | pdf | 15 | 0.403 / 1.22 | 0.33 | 0.008 / 0.045 | 0.18 | 0.222 / 0.630 | 0.35 |
| xe_lm_fontspec | xe | 4 | 0.299 / 0.887 | 0.34 | 0.003 / 0.045 | 0.07 | 0.104 / 0.365 | 0.28 |
| pdf_revtex4_2 | pdf | 7 | 0.193 / 0.635 | 0.30 | 0.003 / 0.044 | 0.07 | 0.098 / 0.306 | 0.32 |
| arxiv_divisor2squares | pdf | 14 | 0.185 / 0.583 | 0.32 | 0.004 / 0.045 | 0.09 | 0.096 / 0.320 | 0.30 |
| pdf_elsarticle | pdf | 26 | 0.354 / 1.24 | 0.28 | 0.003 / 0.043 | 0.08 | 0.094 / 0.299 | 0.32 |
| pdf_ieeetran_conf | pdf | 6 | 0.232 / 0.850 | 0.27 | 0.003 / 0.043 | 0.07 | 0.082 / 0.281 | 0.29 |
| pdf_natbib_numbers | pdf | 24 | 0.317 / 1.24 | 0.26 | 0.003 / 0.042 | 0.08 | 0.085 / 0.294 | 0.29 |
| xe_natbib_bibtex | xe | 9 | 0.493 / 1.79 | 0.28 | 0.003 / 0.044 | 0.07 | 0.130 / 0.506 | 0.26 |
| pdf_letter | pdf | 1 | 0.064 / 0.240 | 0.27 | 0.003 / 0.042 | 0.06 | 0.032 / 0.141 | 0.23 |
| pdf_imakeidx | pdf | 15 | 0.154 / 0.624 | 0.25 | 0.003 / 0.042 | 0.07 | 0.055 / 0.213 | 0.26 |
| arxiv_revtex42_unruh | pdf | 33 | 0.304 / 1.43 | 0.21 | 0.007 / 0.044 | 0.15 | 0.177 / 0.709 | 0.25 |
| pdf_llncs | pdf | 8 | 0.258 / 1.10 | 0.23 | 0.003 / 0.043 | 0.07 | 0.069 / 0.276 | 0.25 |

## Documents where TeXres is slower in at least one scenario

None.

## Documents that failed, differ, or could not be timed

None.

