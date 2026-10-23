//! pdfTeX/e-TeX engine primitives; expected values come from
//! `pdftex -ini -etex` (TeX Live 2026) on the same input.

use tex_core::engine::Engine;

fn run(src: &str) -> Engine {
    let mut engine = Engine::new(true);
    engine.init_primitives();
    engine.add_nullfont();
    engine.input.push_file("t.tex".into(), src.as_bytes().to_vec());
    engine.run();
    engine
}

#[test]
fn parshape_queries_and_glue_conversions_match_pdftex() {
    let e = run(concat!(
        "\\catcode`\\{=1 \\catcode`\\}=2 \\parshape 3 1pt 2pt 3pt 4pt 5pt 6pt\n",
        "\\message{[\\the\\parshapelength1,\\the\\parshapeindent2,\\the\\parshapedimen1,",
        "\\the\\parshapedimen4,\\the\\parshapedimen5,\\the\\parshapelength9,",
        "\\the\\parshapeindent0,\\the\\parshapedimen-1]}\n",
        "\\skip0=1pt plus 2fil minus 3pt \\muskip0=4mu plus 5fill\n",
        "\\message{[\\the\\gluetomu\\skip0|\\the\\mutoglue\\muskip0|",
        "\\the\\numexpr\\mutoglue\\muskip0\\relax]}\n",
        "\\message{[\\the\\pdfretval|\\the\\pdfeachlineheight|\\the\\pdfignoreddimen|",
        "\\the\\lastlinefit|\\pdfinsertht0]}\\end\n"
    ));
    assert!(e.term.contains("[2.0pt,3.0pt,1.0pt,4.0pt,5.0pt,6.0pt,0.0pt,0.0pt]"), "{}", e.term);
    assert!(e.term.contains("[1.0mu plus 2.0fil minus 3.0mu|4.0pt plus 5.0fill|262144]"), "{}", e.term);
    assert!(e.term.contains("[0|-1000.0pt|-1000.0pt|0|0pt]"), "{}", e.term);
}

#[test]
fn pdfprimitive_reaches_the_initex_meaning_like_pdftex() {
    let e = run(concat!(
        "\\catcode`\\{=1 \\catcode`\\}=2 \\def\\relax{XX}\n",
        "\\edef\\x{\\pdfprimitive\\relax\\pdfprimitive\\number 5\\pdfprimitive\\foo A}\n",
        "\\message{[\\meaning\\x]}\\count3=7 \\count2=\\pdfprimitive\\count3 \n",
        "\\message{[\\the\\count2|\\the\\pdfprimitive\\count3]}\n",
        "\\let\\c\\count\\message{[\\ifpdfprimitive\\relax Y\\else N\\fi",
        "\\ifpdfprimitive\\count Y\\else N\\fi\\ifpdfprimitive\\c Y\\else N\\fi]}\\end\n"
    ));
    assert!(e.term.contains("[macro:->\\pdfprimitive XX5A]"), "{}", e.term);
    assert!(e.term.contains("[7|7]"), "{}", e.term);
    assert!(e.term.contains("[NYN]"), "{}", e.term);
}

/// Plain-like setup with box displays in the log (`\showbox`/`\showlists`).
fn run_boxes(body: &str) -> Engine {
    let mut engine = Engine::new(true);
    engine.init_primitives();
    engine.add_nullfont();
    engine.set_interaction_mode(tex_core::engine::InteractionMode::Nonstop);
    let src = format!(
        concat!(
            "\\catcode`\\{{=1 \\catcode`\\}}=2 \\catcode`\\$=3 \\catcode`\\#=6\n",
            "\\font\\tenrm=cmr10 \\tenrm \\font\\teni=cmmi10 \\textfont1=\\teni ",
            "\\scriptfont1=\\teni \\scriptscriptfont1=\\teni\n",
            "\\font\\tensy=cmsy10 \\textfont2=\\tensy \\scriptfont2=\\tensy ",
            "\\scriptscriptfont2=\\tensy\n",
            "\\font\\tenex=cmex10 \\textfont3=\\tenex \\scriptfont3=\\tenex ",
            "\\scriptscriptfont3=\\tenex\n",
            "\\showboxdepth=100 \\showboxbreadth=10000 \\tracingonline=1\n{}\n\\end\n"
        ),
        body
    );
    engine.input.push_file("t.tex".into(), src.into_bytes());
    engine.run();
    engine
}

/// pdftex -ini: `\-` in math mode appends the discretionary to the mlist, and
/// a scanned `\discretionary` shows the replacement text as `replacing n` plus
/// the replaced nodes after the pre-/post-break lists.
#[test]
fn hyphen_discretionary_in_math_and_replacement_display_match_pdftex() {
    let e = run_boxes(concat!(
        "\\hyphenchar\\tenrm=`-\n",
        "\\setbox0\\hbox{$a\\-b$}\\showbox0\n",
        "\\setbox0\\hbox{a\\discretionary{x}{y}{zz}c}\\showbox0\n"
    ));
    assert!(
        e.log.contains("\n.\\teni a\n.\\discretionary\n..\\tenrm -\n.\\teni b\n.\\mathoff"),
        "{}",
        e.log
    );
    assert!(
        e.log.contains(concat!(
            "\n.\\discretionary replacing 2\n..\\tenrm x\n.|\\tenrm y\n",
            ".\\tenrm z\n.\\tenrm z\n.\\tenrm c\n"
        )),
        "{}",
        e.log
    );
}

/// tex.web §560: a font takes `\defaulthyphenchar` and `\defaultskewchar` as
/// they are when it is first loaded (pdftex -ini: [65,66][-1,67]).
#[test]
fn new_fonts_take_the_default_hyphen_and_skew_chars() {
    let e = run(concat!(
        "\\catcode`\\{=1 \\catcode`\\}=2\n",
        "\\defaultskewchar=65 \\defaulthyphenchar=66\n",
        "\\font\\z=cmr10 at 8pt \\message{[\\the\\skewchar\\z,\\the\\hyphenchar\\z]}\n",
        "\\defaultskewchar=-1 \\defaulthyphenchar=67 \\font\\x=cmr10 at 9pt\n",
        "\\message{[\\the\\skewchar\\x,\\the\\hyphenchar\\x]}\\end\n"
    ));
    let term: String = e.term.split_whitespace().collect();
    assert!(term.contains("[65,66][-1,67]"), "{}", e.term);
}

/// tex.web §218 / §1376: `\showlists` prints a paragraph's starting language
/// state and its current `clang`, and a paragraph resumed after a display
/// starts from `\language`, so no language whatsit precedes its first word.
#[test]
fn show_lists_and_display_resumption_follow_tex_languages() {
    let e = run_boxes(concat!(
        "\\lefthyphenmin=1 \\righthyphenmin=1 \\setbox0\\vbox{\\hsize=100pt \\language=3 \\noindent a\\language=4 b\\showlists}\n",
        "\\setbox0\\vbox{\\hsize=100pt \\parindent=0pt \\language=6 x$$a$$ z\\par}\n"
    ));
    assert!(
        e.log.contains(concat!(
            "### horizontal mode entered at line 6 (language3:hyphenmin1,1)\n",
            "\\tenrm a\n\\setlanguage4 (hyphenmin 1,1)\n\\tenrm b\n",
            "spacefactor 1000, current language 4\n"
        )),
        "{}",
        e.log
    );
    assert!(!e.log.contains("setlanguage6"), "{}", e.log);
}

/// pdftex.web: `\pdfignoreddimen` is the value at or below which `\prevdepth`
/// is ignored, both for interline glue and in `\showlists`.
#[test]
fn prevdepth_ignored_follows_pdfignoreddimen() {
    let e = run_boxes(concat!(
        "\\pdfignoreddimen=-5pt\n",
        "\\setbox0\\vbox{\\hbox{a}\\prevdepth=-5pt \\showlists}\n",
        "\\pdfignoreddimen=100pt\n",
        "\\setbox0\\vbox{\\showlists}\n"
    ));
    assert!(e.log.contains("\n.\\tenrm a\nprevdepth ignored\n"), "{}", e.log);
    assert!(
        e.log.contains("entered at line 9\nprevdepth ignored\n"),
        "{}",
        e.log
    );
}

/// tex.web §1101/§188: an insert node shows `natural size` = height + depth
/// of its box and the `\splitmaxdepth` it was created with.
#[test]
fn insert_nodes_show_natural_size_and_split_depth() {
    let e = run_boxes(concat!(
        "\\splitmaxdepth=1pt\n",
        "\\setbox0\\vbox{\\insert100{\\hbox{\\vrule height 2pt depth 3pt}}\\showlists}\n"
    ));
    assert!(
        e.log.contains("\\insert100, natural size 5.0; split(0.0,1.0); float cost 0\n"),
        "{}",
        e.log
    );
}

/// tex.web §881-§882: the node a line breaks at stays in the line (a penalty
/// as is, an explicit kern with width 0), and a discretionary break leaves the
/// emptied disc node before the transplanted pre-break text.
#[test]
fn lines_keep_their_break_nodes_like_tex() {
    let e = run_boxes(concat!(
        "\\hsize=10pt \\parindent=0pt \\tolerance=10000 \\pretolerance=-1\n",
        "\\setbox0\\vbox{\\hskip0pt aaa\\penalty7 bbb\\par}\\showbox0\n",
        "\\setbox0\\vbox{\\hskip0pt aaa\\kern3pt\\hskip3pt bbb\\par}\\showbox0\n",
        "\\hyphenchar\\tenrm=`- \\hsize=14pt\n",
        "\\setbox0\\vbox{\\hskip0pt aa\\-aa\\par}\\showbox0\n"
    ));
    assert!(
        e.log.contains("\n..\\tenrm a\n..\\penalty 7\n..\\glue(\\rightskip) 0.0\n"),
        "{}",
        e.log
    );
    assert!(
        e.log.contains("\n..\\tenrm a\n..\\kern 0.0\n..\\glue(\\rightskip) 0.0\n"),
        "{}",
        e.log
    );
    assert!(
        e.log.contains(concat!(
            "\n..\\tenrm a\n..\\discretionary\n..\\tenrm -\n",
            "..\\glue(\\rightskip) 0.0\n"
        )),
        "{}",
        e.log
    );
}

#[test]
fn moveleft_raise_aliases_keep_their_direction() {
    let e = run(concat!(
        "\\catcode`\\{=1 \\catcode`\\}=2 \\let\\x\\moveright \\let\\y\\raise \\let\\z\\moveleft \\let\\w\\lower\n",
        "\\setbox1\\vbox{\\x 5pt\\hbox{}\\z 5pt\\hbox{}}\\setbox2\\hbox{\\y 5pt\\hbox{}\\w 5pt\\hbox{}}\n",
        "\\message{[\\the\\wd1,\\the\\ht2,\\the\\dp2|\\meaning\\x\\meaning\\z\\meaning\\y\\meaning\\w]}\\end\n"
    ));
    assert!(e.term.contains("[5.0pt,5.0pt,5.0pt|\\moveright\\moveleft\\raise\\lower]"), "{}", e.term);
}

/// tex.web §1269: every assignment, including those main control runs
/// itself (`\font`, `\catcode`, `\textfont`, `\hyphenation`, ...), inserts
/// the `\afterassignment` token right after it. LuaTeX-ja's `\jfont` relies
/// on this for `\font`. Expected output is `pdftex -ini` (TeX Live 2026).
#[test]
fn afterassignment_fires_after_main_control_assignments() {
    let e = run(concat!(
        "\\catcode`\\{=1 \\catcode`\\}=2 \\catcode`\\#=6\n",
        "\\def\\m#1{\\immediate\\write16{[#1]}}\\def\\a{\\m{a}}\n",
        "\\afterassignment\\a\\font\\x=cmr10 \\m{font}\n",
        "\\afterassignment\\a\\catcode`\\@=11 \\m{catcode}\n",
        "\\afterassignment\\a\\mathcode`\\x=\"0178 \\m{mathcode}\n",
        "\\afterassignment\\a\\delcode`\\x=\"161300 \\m{delcode}\n",
        "\\afterassignment\\a\\lccode`\\x=`\\x \\m{lccode}\n",
        "\\afterassignment\\a\\uccode`\\x=`\\X \\m{uccode}\n",
        "\\afterassignment\\a\\sfcode`\\x=999 \\m{sfcode}\n",
        "\\afterassignment\\a\\textfont0=\\x \\m{textfont}\n",
        "\\afterassignment\\a\\scriptfont0=\\x \\m{scriptfont}\n",
        "\\afterassignment\\a\\hyphenation{ab-c} \\m{hyphenation}\n",
        "\\afterassignment\\a\\x \\m{fontselect}\n",
        "\\afterassignment\\a\\count0=1 \\m{count}\n",
        "\\afterassignment\\a\\letterspacefont\\y\\x 100 \\m{letterspacefont}\n",
        "\\afterassignment\\a\\pdfcopyfont\\z\\x \\m{pdfcopyfont}\n",
        "\\afterassignment\\a\\pdfnoligatures\\x \\m{pdfnoligatures}\n",
        "\\afterassignment\\a\\patterns{a1b} \\m{patterns}\n",
        "\\afterassignment\\a\\global\\font\\w=cmr10 \\m{globalfont}\n",
        "\\afterassignment\\a\\global\\catcode`\\!=12 \\m{globalcatcode}\n",
        "\\afterassignment\\a\\global\\x \\m{globalselect}\n",
        "\\end\n"
    ));
    let lines: Vec<&str> = e.term.lines().filter(|l| l.starts_with('[')).collect();
    assert_eq!(
        lines.join(" "),
        "[a] [font] [a] [catcode] [a] [mathcode] [a] [delcode] [a] [lccode] [a] [uccode] \
         [a] [sfcode] [a] [textfont] [a] [scriptfont] [a] [hyphenation] [a] [fontselect] [a] [count] \
         [a] [letterspacefont] [a] [pdfcopyfont] [a] [pdfnoligatures] [a] [patterns] \
         [a] [globalfont] [a] [globalcatcode] [a] [globalselect]"
    );
}

/// tex.web §476: `##` in a parameter text is no literal `#`: the second `#`
/// is not a parameter number, so it is an error and is read again. Expected
/// output is `pdftex -ini` (TeX Live 2026).
#[test]
fn doubled_hash_in_a_parameter_text_is_an_error() {
    let e = run(concat!(
        "\\scrollmode\\catcode`\\{=1 \\catcode`\\}=2 \\catcode`\\#=6\n",
        "\\def\\m#1{\\immediate\\write16{[#1]}}\n",
        "\\def\\a##1x{A}\\m{\\meaning\\a}\\end\n"
    ));
    assert!(e.term.contains("[macro:#1#21x->A]"), "{}", e.term);
    assert_eq!(e.error_count, 2, "{}", e.term);
}
