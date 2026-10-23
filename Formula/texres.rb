class Texres < Formula
  desc "Fast self-contained TeX engine written in Rust"
  homepage "https://github.com/leoliu0/texres"
  version "0.7.8"
  license any_of: ["MIT", "Apache-2.0"]

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/leoliu0/texres/releases/download/v0.7.8/tex-suite-v0.7.8-macos-aarch64.tar.gz"
      sha256 "73d7a1095ab12097af6cbf9323e9603816c8d11ca7102994b46f96d28d07b6b6"
    end
    if Hardware::CPU.intel?
      url "https://github.com/leoliu0/texres/releases/download/v0.7.8/tex-suite-v0.7.8-macos-x86_64.tar.gz"
      sha256 "68d252d8e3550899ab6cbcb47373d20b51676be15204ce536770073ad65722ed"
    end
  end

  depends_on :macos

  def install
    bin.install "bin/texres"
    (share/"tex-suite").install Dir["share/tex-suite/*"]
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/texres --version")
    (testpath/"sample.tex").write <<~'LATEX'
      \documentclass{article}
      \usepackage{hyperref}
      \begin{document}
      \section{Homebrew}\label{home}
      See \ref{home}.
      \end{document}
    LATEX
    system bin/"texres", testpath/"sample.tex"
    assert_path_exists testpath/"sample.pdf"
    assert_path_exists testpath/"sample.synctex.gz"
  end
end
