//! Input sources: files (line-based) and token lists (macros, args, registers).

use crate::token::{CsId, Token};
use std::collections::HashMap;
use std::rc::Rc;

pub const EOF_MARKER: Token = Token(0xFFFF_FFFF);
pub const PAR_END: Token = Token(0xFFFF_FFFE); // blank line -> \par
/// TeX82 bounds nested input sources (`stack_size`) instead of allowing a
/// recursive macro/input cycle to consume the host. Keep the same hard limit.
pub const MAX_INPUT_STACK: usize = 5_000;
/// Upper bound for one scanned macro argument or definition body. This is
/// deliberately comparable to modern TeX's main-memory capacity, while
/// preventing a malformed expansion cycle from growing a Vec without bound.
pub const MAX_TOKEN_LIST_TOKENS: usize = 5_000_000;
/// A diagnostic keeps only a local window of a physical source line. This
/// prevents one malformed megabyte-long line from being copied several times
/// while still leaving ample context for the 120-column renderer.
const MAX_DIAGNOSTIC_SOURCE_BYTES: usize = 4 * 1024;

/// A source position captured for a user-facing diagnostic. Columns are
/// one-based byte positions, matching TeX's byte-oriented input scanner.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SourceContext {
    pub name: String,
    pub line: u32,
    pub column: usize,
    pub text: String,
    /// One-based byte position within the captured `text` window.
    pub(crate) display_column: usize,
    /// Immutable include chain. Source text for parents remains lazy until a
    /// diagnostic is rendered.
    included_from: Option<Rc<SourceMark>>,
}

/// Cheap source bookmark used on successful hot paths. The source excerpt is
/// materialized only if an error later needs it.
#[derive(Clone, Debug, PartialEq, Eq)]
#[doc(hidden)]
pub struct SourceMark {
    /// The file the position lies in, shared by every bookmark of it so that
    /// capturing one costs a single reference count.
    origin: Rc<MarkOrigin>,
    line: u32,
    /// Byte offset of `line` in `data`. Keeping this in the bookmark avoids
    /// rescanning the file from byte zero whenever a diagnostic is rendered.
    line_start: usize,
    byte_column: usize,
}

/// What a [`SourceMark`] needs to know about its file.
#[derive(Debug, PartialEq, Eq)]
#[doc(hidden)]
pub struct MarkOrigin {
    name: Rc<str>,
    data: Rc<[u8]>,
    /// Location in the parent source that opened this file.
    included_from: Option<Rc<SourceMark>>,
}

impl MarkOrigin {
    fn new(name: Rc<str>, data: Rc<[u8]>, included_from: Option<SourceMark>) -> Rc<MarkOrigin> {
        Rc::new(MarkOrigin {
            name,
            data,
            included_from: included_from.map(Rc::new),
        })
    }
}

/// The include chain is as deep as the files nest (up to [`MAX_INPUT_STACK`]).
/// Dropped by recursion it takes several frames per level, which overflowed
/// the 2 MiB test-thread stack of a debug build on Windows at 4000 levels;
/// unlink the levels this origin owns alone one at a time instead.
impl Drop for MarkOrigin {
    fn drop(&mut self) {
        let mut next = self.included_from.take();
        while let Some(mut mark) = next {
            next = Rc::get_mut(&mut mark)
                .and_then(|mark| Rc::get_mut(&mut mark.origin))
                .and_then(|origin| origin.included_from.take());
        }
    }
}

impl SourceMark {
    pub(crate) fn to_context(&self) -> SourceContext {
        let data = &self.origin.data;
        let bytes = if self.line == 0 {
            &[][..]
        } else {
            let (end, _) = physical_line_bounds(data, self.line_start);
            &data[self.line_start.min(end)..end]
        };
        let mut context = InputStack::context_from_line(
            self.origin.name.to_string(),
            self.line,
            bytes,
            self.byte_column.min(bytes.len()),
        );
        context.included_from = self.origin.included_from.clone();
        context
    }

    pub(crate) fn rewind(&mut self, bytes: usize) {
        self.byte_column = self.byte_column.saturating_sub(bytes);
    }

    /// The name of the file the position lies in.
    pub(crate) fn file_name(&self) -> &Rc<str> {
        &self.origin.name
    }

    /// The position lies in the file `origin` stands for.
    pub(crate) fn in_origin(&self, origin: &Rc<MarkOrigin>) -> bool {
        Rc::ptr_eq(&self.origin, origin)
    }
}

/// Sentinel `owner` value for a token list or macro frame with no owning CS.
pub const NO_OWNER: CsId = u32::MAX;
/// Number of leading `Token` slots in a `MacroArgs` buffer reserved for
/// per-argument end offsets (`ends[0..9]`).
const ARG_HEADER_LEN: usize = 9;

/// Owned `Vec<Token>` packed into 16 bytes so `Source` fits in one 64-byte
/// cache line. Every token list and macro argument buffer is bounded by
/// `9 + 9 * MAX_TOKEN_LIST_TOKENS << u32::MAX`.
#[repr(C)]
pub struct CompactTokenVec {
    ptr: *mut Token,
    len: u32,
    cap: u32,
}

impl CompactTokenVec {
    #[inline]
    pub(crate) fn from_vec(mut v: Vec<Token>) -> Self {
        assert!(
            v.capacity() <= u32::MAX as usize,
            "token vector capacity exceeds u32::MAX"
        );
        let ptr = v.as_mut_ptr();
        let len = v.len() as u32;
        let cap = v.capacity() as u32;
        std::mem::forget(v);
        CompactTokenVec { ptr, len, cap }
    }

    #[inline]
    pub(crate) fn into_vec(self) -> Vec<Token> {
        let this = std::mem::ManuallyDrop::new(self);
        // SAFETY: `ptr`, `len`, and `cap` came directly from `Vec::as_mut_ptr`
        // (or `Vec::new()`'s dangling pointer when `cap == 0`).
        unsafe { Vec::from_raw_parts(this.ptr, this.len as usize, this.cap as usize) }
    }

    #[inline(always)]
    pub(crate) fn as_ptr(&self) -> *const Token {
        self.ptr
    }

    #[inline(always)]
    pub(crate) fn len(&self) -> usize {
        self.len as usize
    }

    #[inline(always)]
    pub(crate) fn as_slice(&self) -> &[Token] {
        if self.len == 0 {
            &[]
        } else {
            // SAFETY: `ptr` points to `len` initialized `Token`s.
            unsafe { std::slice::from_raw_parts(self.ptr, self.len as usize) }
        }
    }
}

impl Drop for CompactTokenVec {
    #[inline]
    fn drop(&mut self) {
        if self.cap > 0 {
            // SAFETY: `ptr`, `len`, and `cap` were unpacked from a `Vec<Token>`
            // in `from_vec` and not yet reclaimed by `into_vec`.
            unsafe {
                drop(Vec::from_raw_parts(
                    self.ptr,
                    self.len as usize,
                    self.cap as usize,
                ));
            }
        }
    }
}

impl std::fmt::Debug for CompactTokenVec {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.as_slice().fmt(f)
    }
}

/// Cold file-scanner state boxed behind `Source::File::inner` so `Source`
/// stays 64 bytes (one cache line) for token lists and macro frames.
#[derive(Debug)]
pub struct FileBody {
    /// Shared by the bookmarks taken in this file: its diagnostic name, a
    /// handle on `data` and the location in the parent source that opened it.
    pub origin: Rc<MarkOrigin>,
    pub data: Rc<[u8]>,
    pub pos: usize,
    pub line_no: u32,
    /// Byte offset of the current physical line in `data`.
    pub line_start: usize,
    /// tokenizer state: 0 = new line, 1 = mid line, 2 = skip spaces
    pub state: u8,
    /// set by \endinput: stop at end of current line
    pub ending: bool,
    /// true when the source has returned EOF already
    pub done: bool,
    /// The current line as TeX's `buffer` sees it: the physical line
    /// without trailing spaces, followed by `\endlinechar` (as of the time
    /// the line was read) unless that parameter was inactive.
    pub line_buf: Option<Vec<u8>>,
    /// Bytes at the end of `line_buf` that encode the end-of-line
    /// character. Diagnostics hide them, as TeX's `show_context` does.
    pub line_end_len: u8,
    pub line_pos: usize,
    /// Catcode regime of the current line (luatex `line_catcode_table`):
    /// -1 the current table, -2 "string" catcodes, >= 0 a catcode table,
    /// <= -0xFF the fixed catcode `-regime - 0xFF`.
    pub cat_regime: i32,
    /// True for a real input file (or `\scantokens` pseudo-file) whose
    /// group and conditional nesting at open is on the engine's
    /// `file_nests` stack (e-TeX's `grp_stack`/`if_stack`).
    pub tracked: bool,
    /// XeTeX: how the bytes of this file are decoded (`\XeTeXinputencoding`).
    pub xetex_enc: crate::xetex_input::Enc,
    /// A `\scantokens` pseudo-file's line ends: the offsets of its
    /// `\newlinechar` bytes. e-TeX's `pseudo_start` breaks lines there
    /// only, so a carriage return or line feed inside the text stays in
    /// the line (`\meaning` of LaTeX's `\@outputpage` holds `\^^M`).
    pub pseudo_breaks: Option<Rc<[usize]>>,
}

/// Common prefix of every token-list / macro-frame input source.
#[derive(Debug)]
#[repr(C)]
pub(crate) struct ListCursor {
    pub(crate) seg: *const Token,
    pub(crate) pos: u32,
    pub(crate) end: u32,
    pub(crate) trace_depth: u8,
}

/// An input source. `repr(C, u8)` keeps `Source` to 64 bytes (one cache line)
/// and places the token cursor (`seg`, `pos`, `end`, `trace_depth`) at byte
/// offset 8 in `TokRc`, `TokVec`, and `MacroFrame`, so `Source::cursor` /
/// `Source::cursor_mut` checks only `discriminant != 0` without a jump table.
#[derive(Debug)]
#[repr(C, u8)]
pub enum Source {
    File {
        name: String,
        /// Lines queued by LuaTeX's `tex.print` family (luatex
        /// `luacstring_input`); None for every real file.
        lua_lines: Option<Box<crate::engine_lua::LuaLines>>,
        inner: Box<FileBody>,
        /// The `open_read_file` object (see `lua_cb_files`) lines are read
        /// from instead of `data`; 0 for a file read from memory.
        lua_reader: u32,
        /// The file's start was shown (`(name`, tex.web §537 `start_input`,
        /// which counts it in `open_parens`), so its end shows `)`.
        announced: bool,
    },
    TokRc {
        /// `toks.as_ptr()`: the cursor's view of the list.
        seg: *const Token,
        pos: u32,
        /// `toks.len()`.
        end: u32,
        /// Number of macro ancestry entries that belong to this list.
        trace_depth: u8,
        owner: CsId,
        toks: Rc<[Token]>,
        name: &'static str,
    },
    TokVec {
        /// `toks.as_ptr()`: the cursor's view of the list.
        seg: *const Token,
        pos: u32,
        /// `toks.len()`.
        end: u32,
        /// Number of macro ancestry entries that belong to this list.
        trace_depth: u8,
        owner: CsId,
        toks: CompactTokenVec,
        name: &'static str,
    },
    MacroFrame(MacroFrame),
}

const _: () = assert!(std::mem::size_of::<Source>() == 64);

impl Source {
    /// Borrow the shared token cursor for any non-`File` variant in a single
    /// tag comparison (`discriminant != File`), avoiding a 4-way jump table.
    #[inline(always)]
    pub(crate) fn cursor(&self) -> Option<&ListCursor> {
        if matches!(self, Source::File { .. }) {
            return None;
        }
        // SAFETY: `Source` is `#[repr(C, u8)]` with 8-byte payload alignment,
        // so every variant payload starts at byte offset 8, and `TokRc`,
        // `TokVec`, and `MacroFrame` all begin with `(seg, pos, end, trace_depth)`
        // matching `ListCursor` (verified by unit test `source_cursor_layout`).
        Some(unsafe { &*(self as *const Source as *const u8).add(8).cast::<ListCursor>() })
    }

    #[inline(always)]
    pub(crate) fn cursor_mut(&mut self) -> Option<&mut ListCursor> {
        if matches!(self, Source::File { .. }) {
            return None;
        }
        // SAFETY: see `Source::cursor`.
        Some(unsafe { &mut *(self as *mut Source as *mut u8).add(8).cast::<ListCursor>() })
    }
}

/// Arguments of one macro call stored contiguously in a single pooled buffer:
/// `toks[0..9]` holds the end offset of each collected argument, and argument
/// `i` occupies `toks[start(i)..toks[i].0 as usize]` (starting at `ARG_HEADER_LEN`).
#[derive(Clone, Debug, Default)]
pub struct MacroArgs {
    toks: Vec<Token>,
    count: u8,
}

impl MacroArgs {
    /// Start collecting into a (cleared) recycled buffer.
    #[inline(always)]
    pub(crate) fn with_buffer(mut toks: Vec<Token>) -> Self {
        toks.clear();
        if toks.capacity() < ARG_HEADER_LEN {
            toks.reserve(ARG_HEADER_LEN);
        }
        // SAFETY: `toks.capacity() >= ARG_HEADER_LEN` and `Token` is a POD `u32`
        // wrapper, so zero-initializing the 9-slot header leaves all elements
        // up to `ARG_HEADER_LEN` valid.
        unsafe {
            std::ptr::write_bytes(toks.as_mut_ptr(), 0, ARG_HEADER_LEN);
            toks.set_len(ARG_HEADER_LEN);
        }
        MacroArgs { toks, count: 0 }
    }

    #[inline(always)]
    fn start(&self, index: usize) -> usize {
        if index == 0 {
            ARG_HEADER_LEN
        } else {
            debug_assert!(index <= self.count as usize && self.toks.len() >= ARG_HEADER_LEN);
            // SAFETY: `1 <= index <= count <= 9 <= toks.len()`.
            unsafe { self.toks.get_unchecked(index - 1).0 as usize }
        }
    }

    /// Start of the argument currently being collected.
    #[inline(always)]
    pub(crate) fn open_start(&self) -> usize {
        self.start(self.count as usize)
    }

    /// The shared buffer; the open argument is its tail from `open_start`.
    #[inline(always)]
    pub(crate) fn buffer(&mut self) -> &mut Vec<Token> {
        &mut self.toks
    }

    /// Close the argument being collected. A macro has at most nine.
    #[inline(always)]
    pub(crate) fn finish_arg(&mut self) {
        let idx = self.count as usize;
        debug_assert!(idx < ARG_HEADER_LEN && self.toks.len() >= ARG_HEADER_LEN);
        let end = self.toks.len() as u32;
        // SAFETY: `idx < ARG_HEADER_LEN <= self.toks.len()`.
        unsafe {
            *self.toks.get_unchecked_mut(idx) = Token(end);
        }
        self.count += 1;
    }

    #[inline]
    pub(crate) fn get(&self, index: usize) -> Option<&[Token]> {
        if index < self.count as usize {
            let start = self.start(index);
            // SAFETY: `index < count <= 9 <= toks.len()`.
            let end = unsafe { self.toks.get_unchecked(index).0 as usize };
            Some(&self.toks[start..end])
        } else {
            None
        }
    }

    /// The lengths of the collected arguments, in order.
    #[inline(always)]
    pub(crate) fn arg_lengths(&self) -> impl Iterator<Item = usize> + '_ {
        let mut start = ARG_HEADER_LEN as u32;
        let count = self.count as usize;
        debug_assert!(count <= ARG_HEADER_LEN && self.toks.len() >= ARG_HEADER_LEN);
        // SAFETY: `count <= ARG_HEADER_LEN <= self.toks.len()`.
        let header = unsafe { self.toks.get_unchecked(..count) };
        header.iter().map(move |&end_tok| {
            let end = end_tok.0;
            let length = end - start;
            start = end;
            length as usize
        })
    }

    #[inline(always)]
    pub(crate) fn into_buffer(self) -> Vec<Token> {
        self.toks
    }
}

/// A macro replacement read lazily: body tokens interleaved with the
/// arguments their parameter references name, without materializing the
/// substituted list.
///
/// `repr(C)`: the cursor comes first, in the exact layout of `Source::TokRc`
/// and `Source::TokVec` (see `Source`), and the whole struct fits in 56 bytes
/// so `Source` is 64 bytes.
#[derive(Debug)]
#[repr(C)]
pub struct MacroFrame {
    /// Cursor into the current segment: `body` or, when `in_arg`, `args_toks`.
    /// `seg` is the start of that segment's buffer, which neither moves nor
    /// changes while the frame exists.
    pub(crate) seg: *const Token,
    pub(crate) pos: u32,
    pub(crate) end: u32,
    pub trace_depth: u8,
    in_arg: bool,
    arg_count: u8,
    /// Body position after the reference whose argument is being read.
    resume: u32,
    next_ref: u32,
    pub owner: CsId,
    /// The body and its parameter references.
    plan: Rc<crate::eqtb::MacroReplacement>,
    args_toks: CompactTokenVec,
}

impl MacroFrame {
    #[inline]
    pub(crate) fn new(
        plan: Rc<crate::eqtb::MacroReplacement>,
        args: MacroArgs,
        owner: Option<CsId>,
        trace_depth: u8,
    ) -> Self {
        let end = plan
            .references
            .first()
            .map_or(plan.body.len(), |&(position, _)| position as usize) as u32;
        MacroFrame {
            seg: plan.body.as_ptr(),
            pos: 0,
            end,
            trace_depth,
            in_arg: false,
            arg_count: args.count,
            resume: 0,
            next_ref: 0,
            owner: owner.unwrap_or(NO_OWNER),
            plan,
            args_toks: CompactTokenVec::from_vec(args.toks),
        }
    }

    #[inline(always)]
    pub(crate) fn owner_opt(&self) -> Option<CsId> {
        (self.owner != NO_OWNER).then_some(self.owner)
    }

    /// True once no body or remaining argument tokens remain. A trailing
    /// parameter that has delivered its last token (`in_arg == true`,
    /// `pos >= end`, `resume >= body.len()`, `next_ref >= references.len()`)
    /// or is followed only by empty arguments is also exhausted so tail calls
    /// through `#k` pop in O(1) stack space without mutating the frame.
    #[inline(always)]
    pub fn is_exhausted(&self) -> bool {
        if self.pos < self.end {
            return false;
        }
        let refs = &*self.plan.references;
        let next_ref = self.next_ref as usize;
        if !self.in_arg {
            let Some(&(_, index)) = refs.get(next_ref) else {
                return true;
            };
            if index < self.arg_count {
                let i = index as usize;
                debug_assert!(i < ARG_HEADER_LEN && self.args_toks.len() >= ARG_HEADER_LEN);
                let ptr = self.args_toks.as_ptr();
                // SAFETY: `i < arg_count <= 9 <= args_toks.len()`.
                let start = if i == 0 {
                    ARG_HEADER_LEN as u32
                } else {
                    unsafe { (*ptr.add(i - 1)).0 }
                };
                if start < unsafe { (*ptr.add(i)).0 } {
                    return false;
                }
            }
        } else {
            let body_len = self.plan.body.len() as u32;
            if self.resume >= body_len && next_ref >= refs.len() {
                return true;
            }
            let body_end = refs
                .get(next_ref)
                .map_or(body_len, |&(position, _)| position);
            if self.resume < body_end {
                return false;
            }
        }
        self.is_exhausted_slow()
    }

    #[cold]
    #[inline(never)]
    fn is_exhausted_slow(&self) -> bool {
        let refs = &*self.plan.references;
        let body_len = self.plan.body.len() as u32;
        let arg_count = self.arg_count;
        let args_ptr = self.args_toks.as_ptr();
        let mut next_ref = self.next_ref as usize;
        let mut in_arg = self.in_arg;
        let mut resume = self.resume;
        loop {
            let body_from = if in_arg {
                in_arg = false;
                resume
            } else if let Some(&(position, index)) = refs.get(next_ref) {
                next_ref += 1;
                resume = position + 1;
                if index < arg_count {
                    let i = index as usize;
                    debug_assert!(i < ARG_HEADER_LEN && self.args_toks.len() >= ARG_HEADER_LEN);
                    // SAFETY: `i < arg_count <= 9 <= args_toks.len()`.
                    let start = if i == 0 {
                        ARG_HEADER_LEN as u32
                    } else {
                        unsafe { (*args_ptr.add(i - 1)).0 }
                    };
                    if start < unsafe { (*args_ptr.add(i)).0 } {
                        return false;
                    }
                }
                position + 1
            } else {
                return true;
            };
            let body_end = refs
                .get(next_ref)
                .map_or(body_len, |&(position, _)| position);
            if body_from < body_end {
                return false;
            }
        }
    }

    #[inline(always)]
    pub fn next_token(&mut self) -> Option<Token> {
        if self.pos >= self.end && !self.advance_segment() {
            return None;
        }
        // SAFETY: `advance_segment` establishes `pos < end` within `seg`.
        let t = unsafe { *self.seg.add(self.pos as usize) };
        self.pos += 1;
        Some(t)
    }

    /// The next token of the current segment, without consuming it; `None`
    /// at a segment boundary.
    #[inline(always)]
    pub(crate) fn peek_token(&self) -> Option<Token> {
        self.segment().first().copied()
    }

    /// Move on to the next segment that has a token left; false once the
    /// frame is exhausted.
    #[inline]
    pub(crate) fn advance_segment(&mut self) -> bool {
        if self.pos < self.end {
            return true;
        }
        self.advance_segment_slow()
    }

    /// Step across empty body slices and empty/omitted arguments in registers
    /// until a non-empty segment is reached, or mark the frame exhausted.
    fn advance_segment_slow(&mut self) -> bool {
        let refs = &*self.plan.references;
        let body_len = self.plan.body.len() as u32;
        let arg_count = self.arg_count;
        let args_ptr = self.args_toks.as_ptr();
        let mut next_ref = self.next_ref as usize;
        let mut in_arg = self.in_arg;
        let mut resume = self.resume;
        loop {
            let body_from = if in_arg {
                in_arg = false;
                resume
            } else if let Some(&(position, index)) = refs.get(next_ref) {
                next_ref += 1;
                resume = position + 1;
                if index < arg_count {
                    let i = index as usize;
                    debug_assert!(i < ARG_HEADER_LEN && self.args_toks.len() >= ARG_HEADER_LEN);
                    // SAFETY: `i < arg_count <= 9 <= args_toks.len()`.
                    let start = if i == 0 {
                        ARG_HEADER_LEN as u32
                    } else {
                        unsafe { (*args_ptr.add(i - 1)).0 }
                    };
                    let end = unsafe { (*args_ptr.add(i)).0 };
                    if start < end {
                        self.next_ref = next_ref as u32;
                        self.resume = resume;
                        self.in_arg = true;
                        self.seg = args_ptr;
                        self.pos = start;
                        self.end = end;
                        return true;
                    }
                }
                position + 1
            } else {
                self.next_ref = next_ref as u32;
                self.resume = resume;
                self.in_arg = false;
                self.pos = body_len;
                self.end = body_len;
                return false;
            };
            let body_end = refs
                .get(next_ref)
                .map_or(body_len, |&(position, _)| position);
            if body_from < body_end {
                self.next_ref = next_ref as u32;
                self.resume = resume;
                self.in_arg = false;
                self.seg = self.plan.body.as_ptr();
                self.pos = body_from;
                self.end = body_end;
                return true;
            }
        }
    }

    /// The undelivered rest of the current body or argument segment.
    #[inline(always)]
    pub(crate) fn segment(&self) -> &[Token] {
        if self.pos >= self.end {
            return &[];
        }
        // SAFETY: `seg` is the start of `body` or `args_toks` (see
        // `advance_segment_slow`), both owned by the frame and never changed
        // while it exists, and `pos < end <=` that buffer's length.
        unsafe {
            std::slice::from_raw_parts(
                self.seg.add(self.pos as usize),
                (self.end - self.pos) as usize,
            )
        }
    }

    /// Consume `count` tokens of `segment()`.
    #[inline]
    pub(crate) fn skip(&mut self, count: usize) {
        debug_assert!((self.pos as usize) + count <= self.end as usize);
        self.pos += count as u32;
    }

    /// A number that changes whenever a token is delivered.
    pub(crate) fn signature(&self) -> usize {
        (self.pos as usize) << 8 ^ (self.next_ref as usize) << 1 ^ usize::from(self.in_arg)
    }

    /// Where `show_context` finds this call: the replacement text with the
    /// position of its next token, and, while the tokens of a parameter are
    /// being delivered, those tokens with their next position (TeX's
    /// `parameter` level above the macro).
    pub(crate) fn context_view(&self) -> (&Rc<[Token]>, usize, Option<(&[Token], usize)>) {
        if !self.in_arg {
            return (&self.plan.body, self.pos as usize, None);
        }
        let index = self.plan.references[self.next_ref as usize - 1].1 as usize;
        let args_slice = self.args_toks.as_slice();
        let start = if index == 0 {
            ARG_HEADER_LEN
        } else {
            args_slice[index - 1].0 as usize
        };
        let arg = &args_slice[start..self.end as usize];
        (
            &self.plan.body,
            self.resume as usize,
            Some((arg, self.pos as usize - start)),
        )
    }

    #[inline]
    pub(crate) fn take_arg_buffer(&mut self) -> Vec<Token> {
        self.pos = 0;
        self.end = 0;
        std::mem::replace(&mut self.args_toks, CompactTokenVec::from_vec(Vec::new())).into_vec()
    }

    /// Consume a `MacroFrame` in place (used by `end_token_list`), dropping its
    /// `plan` reference and returning the argument buffer without copying the
    /// frame or running `CompactTokenVec::drop`.
    ///
    /// # Safety
    /// The caller must never use or drop `self` again after this returns.
    #[inline(always)]
    pub(crate) unsafe fn drop_in_place_take_buffer(&mut self) -> Vec<Token> {
        let buffer = std::ptr::read(&self.args_toks).into_vec();
        std::ptr::drop_in_place(&mut self.plan);
        buffer
    }

    #[inline]
    pub(crate) fn into_parts(self) -> (Rc<crate::eqtb::MacroReplacement>, Vec<Token>) {
        (self.plan, self.args_toks.into_vec())
    }

    #[inline]
    pub(crate) fn into_arg_buffer(self) -> Vec<Token> {
        self.args_toks.into_vec()
    }
}

#[derive(Clone, Debug)]
pub enum TokTokens {
    Rc(std::rc::Rc<[Token]>),
    Vec(Vec<Token>),
}

impl std::ops::Deref for TokTokens {
    type Target = [Token];
    #[inline(always)]
    fn deref(&self) -> &[Token] {
        match self {
            TokTokens::Rc(r) => r,
            TokTokens::Vec(v) => v.as_slice(),
        }
    }
}

impl From<std::rc::Rc<[Token]>> for TokTokens {
    #[inline(always)]
    fn from(r: std::rc::Rc<[Token]>) -> Self {
        TokTokens::Rc(r)
    }
}

impl From<Vec<Token>> for TokTokens {
    #[inline(always)]
    fn from(v: Vec<Token>) -> Self {
        TokTokens::Vec(v)
    }
}

impl From<&[Token]> for TokTokens {
    #[inline(always)]
    fn from(s: &[Token]) -> Self {
        TokTokens::Vec(s.to_vec())
    }
}

pub struct InputStack {
    pub stack: Vec<Source>,
    file_bytes: HashMap<String, Rc<[u8]>>,
    disk_stamps: HashMap<String, (std::time::SystemTime, u64)>,
    /// The scanner normally pops a file before an EOF error is diagnosed.
    /// Retain its final position so runaway definitions and arguments do not
    /// degrade to the unhelpful "line 0" location.
    last_finished_file: Option<SourceContext>,
    /// Index of the topmost `Source::File`, a hint validated on every use:
    /// `current_file_line` runs on hot paths (every skipped conditional)
    /// below stacks of macro frames.
    top_file: std::cell::Cell<usize>,
}

/// The content end and next-line offset of a pseudo-file line that starts
/// at `start`: the line ends at the next recorded line break.
pub(crate) fn pseudo_line_bounds(data: &[u8], breaks: &[usize], start: usize) -> (usize, usize) {
    let index = breaks.partition_point(|&end| end < start);
    match breaks.get(index) {
        Some(&end) => (end, end + 1),
        None => (data.len(), data.len()),
    }
}

/// Return the content end and next-line offset for a physical line.
/// TeX accepts LF, CRLF, and legacy CR-only files.
pub(crate) fn physical_line_bounds(data: &[u8], start: usize) -> (usize, usize) {
    let start = start.min(data.len());
    let Some(offset) = data[start..]
        .iter()
        .position(|&byte| byte == b'\n' || byte == b'\r')
    else {
        return (data.len(), data.len());
    };
    let end = start + offset;
    let next = if data[end] == b'\r' && data.get(end + 1) == Some(&b'\n') {
        end + 2
    } else {
        end + 1
    };
    (end, next)
}
impl InputStack {
    pub fn new() -> Self {
        InputStack {
            stack: Vec::new(),
            file_bytes: HashMap::new(),
            disk_stamps: HashMap::new(),
            last_finished_file: None,
            top_file: std::cell::Cell::new(usize::MAX),
        }
    }

    /// The stack depth and reading position of the top source: it differs
    /// after any token has been read from the stack.
    pub(crate) fn signature(&self) -> (usize, usize) {
        let position = match self.stack.last() {
            Some(Source::File { inner, .. }) => {
                (inner.line_no as usize).wrapping_mul(1 << 20) ^ inner.line_pos
            }
            Some(Source::TokRc { pos, .. } | Source::TokVec { pos, .. }) => *pos as usize,
            Some(Source::MacroFrame(frame)) => frame.signature(),
            None => 0,
        };
        (self.stack.len(), position)
    }

    /// Clear active sources and any EOF location retained for diagnostics.
    pub fn clear_sources(&mut self) {
        self.stack.clear();
        self.last_finished_file = None;
    }

    fn raw_line_at(data: &[u8], line_no: u32) -> (usize, &[u8]) {
        if line_no == 0 {
            return (0, &[]);
        }
        let mut start = 0usize;
        let mut current = 1u32;
        while current < line_no && start < data.len() {
            let (end, next) = physical_line_bounds(data, start);
            if end == next {
                return (data.len(), &[]);
            }
            start = next;
            current += 1;
        }
        let (end, _) = physical_line_bounds(data, start);
        (start, &data[start..end])
    }

    fn context_for_at(source: &Source, byte_column: Option<usize>) -> Option<SourceContext> {
        let Source::File { name, inner, .. } = source else {
            return None;
        };
        let FileBody {
            origin,
            data,
            line_no,
            line_start,
            line_buf,
            line_end_len,
            line_pos,
            ..
        } = &**inner;
        let bytes = line_buf.as_deref().map_or_else(
            || {
                if *line_no == 0 {
                    &[][..]
                } else {
                    let (end, _) = physical_line_bounds(data, *line_start);
                    &data[(*line_start).min(end)..end]
                }
            },
            |buf| &buf[..buf.len().saturating_sub(usize::from(*line_end_len))],
        );
        let byte_column = byte_column
            .unwrap_or_else(|| {
                if line_buf.is_some() {
                    *line_pos
                } else {
                    bytes.len()
                }
            })
            .min(bytes.len());
        let mut context = Self::context_from_line(name.clone(), *line_no, bytes, byte_column);
        context.included_from = origin.included_from.clone();
        Some(context)
    }

    fn context_from_line(
        name: String,
        line: u32,
        bytes: &[u8],
        byte_column: usize,
    ) -> SourceContext {
        let mut start = byte_column.saturating_sub(MAX_DIAGNOSTIC_SOURCE_BYTES / 2);
        let end = (start + MAX_DIAGNOSTIC_SOURCE_BYTES).min(bytes.len());
        start = end.saturating_sub(MAX_DIAGNOSTIC_SOURCE_BYTES);
        let display_column = String::from_utf8_lossy(&bytes[start..byte_column]).len() + 1;
        SourceContext {
            name,
            line,
            column: byte_column + 1,
            text: String::from_utf8_lossy(&bytes[start..end]).into_owned(),
            display_column,
            included_from: None,
        }
    }

    fn context_for(source: &Source) -> Option<SourceContext> {
        Self::context_for_at(source, None)
    }

    pub(crate) fn source_context_at(
        &self,
        index: usize,
        byte_column: usize,
    ) -> Option<SourceContext> {
        self.stack
            .get(index)
            .and_then(|source| Self::context_for_at(source, Some(byte_column)))
    }

    /// Materialize an exact physical scanner coordinate only when a
    /// diagnostic needs it. Unlike `current_source_mark`, this remains
    /// correct after the scanner has consumed the rest of the token or moved
    /// to another line in the same still-open source.
    pub(crate) fn source_mark_at(
        &self,
        index: usize,
        line: u32,
        byte_column: usize,
    ) -> Option<SourceMark> {
        let Source::File { inner, .. } = self.stack.get(index)? else {
            return None;
        };
        let FileBody {
            origin,
            data,
            line_no,
            line_start,
            ..
        } = &**inner;
        let line_start = if line == *line_no {
            *line_start
        } else {
            Self::raw_line_at(data, line).0
        };
        Some(SourceMark {
            origin: origin.clone(),
            line,
            line_start,
            byte_column,
        })
    }

    pub(crate) fn source_line_at(&self, index: usize) -> Option<u32> {
        match self.stack.get(index)? {
            Source::File { inner, .. } => Some(inner.line_no),
            Source::TokRc { .. } | Source::TokVec { .. } | Source::MacroFrame(_) => None,
        }
    }

    pub(crate) fn current_source_mark(&self) -> Option<SourceMark> {
        match self.top_file()? {
            Source::File { inner, .. } => {
                let FileBody {
                    origin,
                    data,
                    line_no,
                    line_start,
                    line_buf,
                    line_pos,
                    ..
                } = &**inner;
                Some(SourceMark {
                    origin: origin.clone(),
                    line: *line_no,
                    line_start: *line_start,
                    byte_column: if line_buf.is_some() {
                        *line_pos
                    } else if *line_no == 0 {
                        0
                    } else {
                        let (end, _) = physical_line_bounds(data, *line_start);
                        end.saturating_sub(*line_start)
                    },
                })
            }
            _ => None,
        }
    }

    /// Find the most recent physical spelling of text in an active source.
    /// This is used for errors whose TeX macro protocol reports the failure
    /// only after it has read ahead (notably LaTeX's missing-file prompt).
    /// Searching happens only on that error path, outside the scanner's hot
    /// loop, and returns a precise byte-oriented location.
    pub(crate) fn find_recent_text(&self, needle: &[u8]) -> Option<SourceMark> {
        if needle.is_empty() {
            return None;
        }
        self.stack.iter().rev().find_map(|source| {
            let Source::File { inner, .. } = source else {
                return None;
            };
            let FileBody {
                origin,
                data,
                pos,
                line_no,
                line_buf,
                line_pos,
                ..
            } = &**inner;
            let consumed_end = if line_buf.is_some() && *line_no > 0 {
                let mut line_start = 0usize;
                for _ in 1..*line_no {
                    let (end, next) = physical_line_bounds(data, line_start);
                    if end == next {
                        break;
                    }
                    line_start = next;
                }
                line_start.saturating_add(*line_pos).min(data.len())
            } else {
                (*pos).min(data.len())
            };
            let consumed = &data[..consumed_end];
            let index = consumed
                .windows(needle.len())
                .rposition(|bytes| bytes == needle)?;
            let mut line_start = 0usize;
            let mut line = 1u32;
            while line_start < index {
                let (end, next) = physical_line_bounds(data, line_start);
                if index <= end || end == next {
                    break;
                }
                line_start = next;
                line = line.saturating_add(1);
            }
            Some(SourceMark {
                origin: origin.clone(),
                line,
                line_start,
                byte_column: index - line_start,
            })
        })
    }

    /// Current file followed by the exact locations that included it. If
    /// scanning reached global EOF, retain the last file's final position.
    pub fn source_contexts(&self) -> Vec<SourceContext> {
        self.current_source_context()
            .map_or_else(Vec::new, |source| Self::source_context_chain(source, 21))
    }

    /// Materialize only the current source. Hot-path provenance capture for
    /// deferred writes does not need to walk the include chain.
    pub fn current_source_context(&self) -> Option<SourceContext> {
        self.stack
            .iter()
            .rev()
            .find_map(Self::context_for)
            .or_else(|| self.last_finished_file.clone())
    }

    pub(crate) fn source_context_chain(
        mut source: SourceContext,
        limit: usize,
    ) -> Vec<SourceContext> {
        let mut contexts = Vec::with_capacity(limit.min(8));
        for _ in 0..limit {
            let parent = source.included_from.clone();
            contexts.push(source);
            let Some(parent) = parent else {
                break;
            };
            source = parent.to_context();
        }
        contexts
    }

    /// Remove an exhausted file while retaining its final source position.
    pub(crate) fn finish_file(&mut self, index: usize) {
        let source = if index + 1 == self.stack.len() {
            self.stack.pop()
        } else {
            Some(self.stack.remove(index))
        };
        if let Some(source) = source {
            if let Some(context) = Self::context_for(&source) {
                self.last_finished_file = Some(context);
            }
        }
    }

    #[inline]
    fn ensure_stack_room(&self) {
        assert!(
            self.stack.len() < MAX_INPUT_STACK,
            "TeX capacity exceeded, sorry [input stack size={MAX_INPUT_STACK}]"
        );
    }

    /// Test a compound input-stack push before mutating any of its sources.
    /// Engine entry points use this to turn user-triggered exhaustion into a
    /// normal fatal diagnostic; the assertion in the low-level push remains
    /// an internal invariant for direct callers.
    #[inline]
    pub(crate) fn has_stack_room(&self, needed: usize) -> bool {
        self.stack.len().saturating_add(needed) <= MAX_INPUT_STACK
    }

    pub fn cached_file(&self, key: &str) -> Option<Rc<[u8]>> {
        self.file_bytes.get(key).cloned()
    }

    pub fn read_file(&mut self, path: &std::path::Path) -> std::io::Result<Rc<[u8]>> {
        let key = path.to_string_lossy().into_owned();
        let meta = tex_kpse::fs::metadata(path)?;
        let stamp = meta.modified().ok().map(|mtime| (mtime, meta.len()));
        if stamp.is_some() && self.disk_stamps.get(&key).copied() == stamp {
            if let Some(bytes) = self.file_bytes.get(&key) {
                return Ok(bytes.clone());
            }
        }
        let bytes: Rc<[u8]> = tex_kpse::fs::read(path)?.into();
        self.file_bytes.insert(key.clone(), bytes.clone());
        if let Some(stamp) = stamp {
            self.disk_stamps.insert(key, stamp);
        }
        Ok(bytes)
    }

    /// A TeX output can alias an input under a different spelling. Drop all
    /// disk entries at openout rather than retaining stale aliases; active
    /// input sources retain their own Rc and embedded packages stay cached.
    pub fn invalidate_disk_files(&mut self) {
        for (key, _) in self.disk_stamps.drain() {
            self.file_bytes.remove(&key);
        }
    }

    pub fn intern_file(&mut self, key: String, data: Vec<u8>) -> Rc<[u8]> {
        if let Some(rc) = self.file_bytes.get(&key) {
            return rc.clone();
        }
        let rc: Rc<[u8]> = Rc::from(data);
        self.file_bytes.insert(key, rc.clone());
        rc
    }

    pub fn push_file(&mut self, name: String, data: impl Into<Rc<[u8]>>) {
        let included_from = self.current_source_mark();
        self.push_file_from(name, data, included_from);
    }

    pub(crate) fn push_file_from(
        &mut self,
        name: String,
        data: impl Into<Rc<[u8]>>,
        included_from: Option<SourceMark>,
    ) {
        self.ensure_stack_room();
        let data: Rc<[u8]> = data.into();
        let origin = MarkOrigin::new(Rc::from(name.as_str()), data.clone(), included_from);
        self.stack.push(Source::File {
            name,
            lua_lines: None,
            inner: Box::new(FileBody {
                origin,
                data,
                pos: 0,
                line_no: 0,
                line_start: 0,
                state: 0,
                ending: false,
                done: false,
                line_buf: None,
                line_end_len: 0,
                line_pos: 0,
                cat_regime: -1,
                tracked: false,
                xetex_enc: crate::xetex_input::Enc::Default,
                pseudo_breaks: None,
            }),
            lua_reader: 0,
            announced: false,
        });
        self.top_file.set(self.stack.len() - 1);
    }

    /// Push a `\scantokens` pseudo-file whose lines end at `breaks`.
    pub(crate) fn push_pseudo_file(&mut self, name: String, data: Vec<u8>, breaks: Vec<usize>) {
        self.push_file(name, data);
        if let Some(Source::File { inner, .. }) = self.stack.last_mut() {
            inner.pseudo_breaks = Some(breaks.into());
        }
    }

    /// Push a file whose lines come from an `open_read_file` object.
    pub(crate) fn push_reader_file(
        &mut self,
        name: String,
        reader: u32,
        included_from: Option<SourceMark>,
    ) {
        self.push_file_from(name, Vec::<u8>::new(), included_from);
        if let Some(Source::File { lua_reader, .. }) = self.stack.last_mut() {
            *lua_reader = reader;
        }
    }

    /// The file just pushed was opened by `start_input`, which showed it.
    pub(crate) fn announce_top_file(&mut self) {
        if let Some(Source::File { announced, .. }) = self.stack.last_mut() {
            *announced = true;
        }
    }

    /// Push LuaTeX `tex.print` output as a pseudo file. tex.web §328
    /// begin_file_reading starts it in state mid_line.
    pub(crate) fn push_lua_lines(&mut self, lines: crate::engine_lua::LuaLines) {
        let included_from = self.current_source_mark();
        self.ensure_stack_room();
        let data: Rc<[u8]> = Rc::from(&b""[..]);
        let origin = MarkOrigin::new(Rc::from("<directlua>"), data.clone(), included_from);
        self.stack.push(Source::File {
            name: "<directlua>".to_string(),
            lua_lines: Some(Box::new(lines)),
            inner: Box::new(FileBody {
                origin,
                data,
                pos: 0,
                line_no: 0,
                line_start: 0,
                state: 1,
                ending: false,
                done: false,
                line_buf: None,
                line_end_len: 0,
                line_pos: 0,
                cat_regime: -1,
                tracked: false,
                xetex_enc: crate::xetex_input::Enc::Utf8,
                pseudo_breaks: None,
            }),
            lua_reader: 0,
            announced: false,
        });
        self.top_file.set(self.stack.len() - 1);
    }
    pub fn push_toks(&mut self, toks: impl Into<TokTokens>, name: &'static str) {
        self.push_toks_owned(toks, name, None, 0);
    }

    pub(crate) fn push_toks_rc(
        &mut self,
        toks: Rc<[Token]>,
        name: &'static str,
        owner: Option<CsId>,
        trace_depth: u8,
    ) {
        self.ensure_stack_room();
        assert!(
            toks.len() <= MAX_TOKEN_LIST_TOKENS,
            "TeX capacity exceeded, sorry [token list size={MAX_TOKEN_LIST_TOKENS}]"
        );
        self.stack.push(Source::TokRc {
            seg: toks.as_ptr(),
            pos: 0,
            end: toks.len() as u32,
            trace_depth,
            owner: owner.unwrap_or(NO_OWNER),
            toks,
            name,
        });
    }

    pub(crate) fn push_toks_vec(
        &mut self,
        toks: Vec<Token>,
        name: &'static str,
        owner: Option<CsId>,
        trace_depth: u8,
    ) {
        self.ensure_stack_room();
        assert!(
            toks.len() <= MAX_TOKEN_LIST_TOKENS,
            "TeX capacity exceeded, sorry [token list size={MAX_TOKEN_LIST_TOKENS}]"
        );
        let seg = toks.as_ptr();
        let end = toks.len() as u32;
        self.stack.push(Source::TokVec {
            seg,
            pos: 0,
            end,
            trace_depth,
            owner: owner.unwrap_or(NO_OWNER),
            toks: CompactTokenVec::from_vec(toks),
            name,
        });
    }

    pub(crate) fn push_toks_owned(
        &mut self,
        toks: impl Into<TokTokens>,
        name: &'static str,
        owner: Option<CsId>,
        trace_depth: u8,
    ) {
        match toks.into() {
            TokTokens::Rc(r) => self.push_toks_rc(r, name, owner, trace_depth),
            TokTokens::Vec(v) => self.push_toks_vec(v, name, owner, trace_depth),
        }
    }

    /// Borrow the physical source coordinate without cloning its path for
    /// every typeset word. Token-list expansions retain their parent file.
    pub fn current_file_position(&self) -> Option<(&str, u32)> {
        for source in self.stack.iter().rev() {
            if let Source::File { name, inner, .. } = source {
                return Some((name, inner.line_no));
            }
        }
        self.last_finished_file
            .as_ref()
            .map(|context| (context.name.as_str(), context.line))
    }

    pub fn current_file_name(&self) -> String {
        for s in self.stack.iter().rev() {
            if let Source::File { name, .. } = s {
                return name.clone();
            }
        }
        self.last_finished_file
            .as_ref()
            .map(|context| context.name.clone())
            .unwrap_or_default()
    }

    /// The innermost open file. Pushing a file records its index, so the
    /// stack is only searched after that file was closed.
    #[inline]
    fn top_file(&self) -> Option<&Source> {
        if let Some(source @ Source::File { .. }) = self.stack.get(self.top_file.get()) {
            return Some(source);
        }
        let (index, source) = self
            .stack
            .iter()
            .enumerate()
            .rev()
            .find(|(_, s)| matches!(s, Source::File { .. }))?;
        self.top_file.set(index);
        Some(source)
    }

    pub fn current_file_line(&self) -> u32 {
        if let Some(Source::File { inner, .. }) = self.top_file() {
            return inner.line_no;
        }
        self.last_finished_file
            .as_ref()
            .map_or(0, |context| context.line)
    }

    /// `current_file_name` and `current_file_line` in one pass, sharing the
    /// name of an open file instead of copying it.
    pub(crate) fn current_file_location(&self) -> (Rc<str>, u32) {
        if let Some(Source::File { inner, .. }) = self.top_file() {
            return (inner.origin.name.clone(), inner.line_no);
        }
        self.last_finished_file.as_ref().map_or_else(
            || (Rc::from(""), 0),
            |context| (Rc::from(context.name.as_str()), context.line),
        )
    }

    /// The innermost open file's bookmark origin and current line, without
    /// touching its name (see `current_file_location`).
    #[inline]
    pub(crate) fn top_file_origin(&self) -> Option<(&Rc<MarkOrigin>, u32)> {
        match self.top_file()? {
            Source::File { inner, .. } => Some((&inner.origin, inner.line_no)),
            _ => None,
        }
    }

    /// web2c `print_file_line`: the innermost file and its current line
    /// for a `-file-line-error` message. A pseudo file (`\scantokens`, a
    /// Lua chunk) has no name of its own and defers to the file below it.
    pub(crate) fn tex_error_file(&self) -> Option<(Rc<str>, u32)> {
        self.stack.iter().rev().find_map(|source| match source {
            Source::File { name, inner, .. }
                if !matches!(
                    name.as_str(),
                    "<scantokens>" | "<scantextokens>" | "<directlua>"
                ) =>
            {
                Some((inner.origin.name.clone(), inner.line_no))
            }
            _ => None,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nested_input_shares_bytes() {
        let blob = vec![b'a'; 1_000_000];
        let mut s = InputStack::new();
        let key = "big.tex".to_string();
        let rc = s.intern_file(key.clone(), blob);
        for _ in 0..4_000 {
            s.push_file(key.clone(), rc.clone());
        }
        // Each source owns the shared bytes twice (the scanner's handle and
        // its bookmark origin); the include bookmarks share the parent's origin.
        assert_eq!(Rc::strong_count(&rc), 2 + 2 * 4_000);
        assert_eq!(s.file_bytes.len(), 1);
    }

    #[test]
    fn disk_cache_shares_unchanged_reads_and_refreshes_after_writes() {
        let path = std::env::temp_dir().join(format!("tex-input-cache-{}.tex", std::process::id()));
        std::fs::write(&path, b"old").unwrap();
        let mut input = InputStack::new();
        let old = input.read_file(&path).unwrap();
        assert!(Rc::ptr_eq(&old, &input.read_file(&path).unwrap()));
        std::fs::write(&path, b"new contents").unwrap();
        assert_eq!(&*input.read_file(&path).unwrap(), b"new contents");
        input.invalidate_disk_files();
        std::fs::write(&path, b"same length!").unwrap();
        assert_eq!(&*input.read_file(&path).unwrap(), b"same length!");
        assert_eq!(&*old, b"old", "active input retains its original bytes");
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn recent_text_uses_tex_line_endings_for_the_reported_location() {
        let bytes: Rc<[u8]> = Rc::from(&b"first\rneedle rest\rlast"[..]);
        let mut input = InputStack::new();
        input.stack.push(Source::File {
            name: "legacy-cr.tex".into(),
            lua_lines: None,
            inner: Box::new(FileBody {
                origin: MarkOrigin::new(Rc::from("legacy-cr.tex"), bytes.clone(), None),
                data: bytes,
                pos: 0,
                line_no: 2,
                line_start: b"first\r".len(),
                state: 1,
                ending: false,
                done: false,
                line_buf: Some(b"needle rest".to_vec()),
                line_end_len: 0,
                line_pos: b"needle".len(),
                cat_regime: -1,
                tracked: false,
                xetex_enc: crate::xetex_input::Enc::Default,
                pseudo_breaks: None,
            }),
            lua_reader: 0,
            announced: false,
        });

        let context = input
            .find_recent_text(b"needle")
            .expect("consumed text")
            .to_context();
        assert_eq!(context.name, "legacy-cr.tex");
        assert_eq!(context.line, 2);
        assert_eq!(context.column, 1);
        assert_eq!(context.text, "needle rest");
    }

    #[test]
    fn source_cursor_layout_matches_all_list_variants() {
        let rc: Rc<[Token]> = Rc::from(&[Token::letter(b'a')][..]);
        let s_rc = Source::TokRc {
            seg: rc.as_ptr(),
            pos: 1,
            end: 2,
            trace_depth: 3,
            owner: NO_OWNER,
            toks: rc,
            name: "<rc>",
        };
        let c = s_rc.cursor().expect("TokRc cursor");
        assert_eq!((c.pos, c.end, c.trace_depth), (1, 2, 3));

        let v = vec![Token::letter(b'b')];
        let s_vec = Source::TokVec {
            seg: v.as_ptr(),
            pos: 4,
            end: 5,
            trace_depth: 6,
            owner: NO_OWNER,
            toks: CompactTokenVec::from_vec(v),
            name: "<vec>",
        };
        let c = s_vec.cursor().expect("TokVec cursor");
        assert_eq!((c.pos, c.end, c.trace_depth), (4, 5, 6));

        let m = crate::eqtb::Macro {
            num_params: 1,
            has_param_refs: true,
            params: vec![Vec::new()],
            prefix: Vec::new(),
            body: Rc::from(&[Token::letter(b'c'), Token(crate::expand::PAR_REF_FLAG | 1)][..]),
            long: false,
            outer: false,
            protected: false,
            replacement: Default::default(),
        };
        let plan = m.cached_plan();
        let mut args = MacroArgs::with_buffer(Vec::with_capacity(16));
        args.buffer().push(Token::letter(b'd'));
        args.finish_arg();
        let mut frame = MacroFrame::new(plan, args, None, 7);
        frame.pos = 8;
        frame.end = 9;
        let s_frame = Source::MacroFrame(frame);
        let c = s_frame.cursor().expect("MacroFrame cursor");
        assert_eq!((c.pos, c.end, c.trace_depth), (8, 9, 7));
    }

    #[test]
    fn unopened_file_mark_does_not_claim_the_first_line() {
        let mut input = InputStack::new();
        input.push_file("pending.tex".to_string(), b"first line\nsecond".to_vec());

        let context = input.current_source_mark().expect("file mark").to_context();

        assert_eq!(context.line, 0);
        assert_eq!(context.column, 1);
        assert!(context.text.is_empty());
    }
}
