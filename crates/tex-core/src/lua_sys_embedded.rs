//! Reading the embedded archive's virtual tree (`/<embedded>/…`, see
//! `tex_kpse::embedded_tree`) from Lua: the zero-copy `EmbeddedFile` userdata
//! and native `FILE*` methods behind `lua_sys_embedded.lua`.

use std::any::Any;
use std::cell::Cell;

use tex_lua::{
    CallbackLua, Lua, LuaApi, LuaBytes, LuaError, LuaResult, LuaString, LuaTable, LuaValueKind,
    UserDataTrait,
};

use crate::lua_sys::{bytes_of, sys_reg};

pub(crate) const PRELUDE: &str = include_str!("lua_sys_embedded.lua");

thread_local! {
    static NEXT_VFILE_ID: Cell<u32> = const { Cell::new(0) };
}

fn next_vfile_id() -> u32 {
    NEXT_VFILE_ID.with(|cell| {
        let next = cell.get().wrapping_add(1);
        cell.set(next);
        next
    })
}

pub(crate) enum ReadBytes {
    Decoded(Vec<u8>),
    Font(tex_kpse::EmbeddedFontFile),
}

impl ReadBytes {
    #[inline]
    pub(crate) fn len(&self) -> usize {
        match self {
            Self::Decoded(data) => data.len(),
            Self::Font(file) => file.len(),
        }
    }

    pub(crate) fn full(&mut self) -> Result<&[u8], String> {
        if let Self::Font(file) = self {
            let data = file
                .read_all()
                .ok_or_else(|| "embedded file could not be decoded".to_string())?;
            *self = Self::Decoded(data);
        }
        match self {
            Self::Decoded(data) => Ok(data),
            Self::Font(_) => unreachable!(),
        }
    }

    /// Borrow `start..end` directly without allocating a `Vec<u8>`.
    #[inline]
    pub(crate) fn slice(&mut self, start: usize, end: usize) -> Result<&[u8], String> {
        let need_decode = match self {
            Self::Font(file) => file.metadata_slice(start, end).is_none(),
            Self::Decoded(_) => false,
        };
        if need_decode {
            self.full()?;
        }
        match self {
            Self::Font(file) => Ok(file.metadata_slice(start, end).expect("checked slice")),
            Self::Decoded(data) => Ok(&data[start..end]),
        }
    }
}

pub(crate) enum EmbeddedState {
    File(ReadBytes),
    Directory,
    Closed,
}

/// Open virtual file handle userdata (`io.open` on `/<embedded>/…`).
/// Explicit close drops any decoded buffer even while Lua references remain.
pub(crate) struct EmbeddedFile {
    pub(crate) state: EmbeddedState,
    pub(crate) pos: usize,
    pub(crate) size: usize,
    id: u32,
}

impl EmbeddedFile {
    fn new_file(bytes: ReadBytes) -> Self {
        let size = bytes.len();
        EmbeddedFile {
            state: EmbeddedState::File(bytes),
            pos: 0,
            size,
            id: next_vfile_id(),
        }
    }

    fn new_directory() -> Self {
        EmbeddedFile {
            state: EmbeddedState::Directory,
            pos: 0,
            size: 0,
            id: next_vfile_id(),
        }
    }

    #[inline]
    pub(crate) fn is_closed(&self) -> bool {
        matches!(self.state, EmbeddedState::Closed)
    }
}

impl UserDataTrait for EmbeddedFile {
    fn type_name(&self) -> &'static str {
        "FILE*"
    }

    fn lua_tostring(&self) -> Option<String> {
        if self.is_closed() {
            Some("file (closed)".to_string())
        } else {
            Some(format!("file (0x{:08x})", self.id))
        }
    }

    fn lua_close(&mut self) {
        self.state = EmbeddedState::Closed;
    }

    fn as_any(&self) -> &dyn Any {
        self
    }

    fn as_any_mut(&mut self) -> &mut dyn Any {
        self
    }
}

enum SmallBytes {
    Inline([u8; 512], usize),
    Heap(Vec<u8>),
}

impl SmallBytes {
    #[inline]
    fn from_slice(slice: &[u8]) -> Self {
        if slice.len() <= 512 {
            let mut buf = [0u8; 512];
            buf[..slice.len()].copy_from_slice(slice);
            Self::Inline(buf, slice.len())
        } else {
            Self::Heap(slice.to_vec())
        }
    }

    #[inline]
    fn as_slice(&self) -> &[u8] {
        match self {
            Self::Inline(buf, len) => &buf[..*len],
            Self::Heap(vec) => vec.as_slice(),
        }
    }
}

enum ReadOneResult {
    Eof,
    Bytes(SmallBytes),
    Int(i64),
    Float(f64),
}

enum ReadFormat {
    Count(i64),
    All,
    Line(bool),
    Number,
}

fn lua_type_name(kind: Option<LuaValueKind>) -> &'static str {
    match kind {
        None => "no value",
        Some(LuaValueKind::Nil) => "nil",
        Some(LuaValueKind::Boolean) => "boolean",
        Some(LuaValueKind::Userdata) => "userdata",
        Some(LuaValueKind::Integer | LuaValueKind::Float) => "number",
        Some(LuaValueKind::String) => "string",
        Some(LuaValueKind::Table) => "table",
        Some(
            LuaValueKind::Function
            | LuaValueKind::CFunction
            | LuaValueKind::CClosure
            | LuaValueKind::RClosure,
        ) => "function",
        Some(LuaValueKind::Thread) => "thread",
    }
}

fn bad_vfile_self(cb: &mut CallbackLua<'_>, method: &str) -> LuaError {
    let got = lua_type_name(cb.arg_kind(1));
    cb.error(format!(
        "bad argument #1 to '{method}' (FILE* expected, got {got})"
    ))
}

fn push_failure(cb: &mut CallbackLua<'_>, message: &str, errno: i64) -> LuaResult<usize> {
    cb.push_nil()?;
    cb.push_bytes(message.as_bytes())?;
    cb.push_int(errno)?;
    Ok(3)
}

fn parse_read_format(cb: &mut CallbackLua<'_>, arg_idx: usize) -> Result<ReadFormat, LuaError> {
    match cb.arg_kind(arg_idx) {
        Some(LuaValueKind::Integer | LuaValueKind::Float) => {
            let Some(n) = cb.arg_integer(arg_idx) else {
                return Err(
                    cb.error("bad argument to 'read' (number has no integer representation)")
                );
            };
            Ok(ReadFormat::Count(n))
        }
        Some(LuaValueKind::String) => {
            let parsed = cb.arg_bytes(arg_idx, |bytes| {
                let rest = if bytes.first() == Some(&b'*') {
                    &bytes[1..]
                } else {
                    bytes
                };
                match rest.first().copied() {
                    Some(b'n') => Some(ReadFormat::Number),
                    Some(b'a') => Some(ReadFormat::All),
                    Some(b'l') => Some(ReadFormat::Line(false)),
                    Some(b'L') => Some(ReadFormat::Line(true)),
                    _ => None,
                }
            });
            match parsed.flatten() {
                Some(fmt) => Ok(fmt),
                None => Err(cb.error("bad argument to 'read' (invalid format)")),
            }
        }
        _ => Err(cb.error("bad argument to 'read' (invalid format)")),
    }
}

fn parse_lua_numeral(text: &str) -> Option<ReadOneResult> {
    let bytes = text.as_bytes();
    if bytes.is_empty() {
        return None;
    }
    let has_hex = bytes.starts_with(b"0x")
        || bytes.starts_with(b"0X")
        || bytes.starts_with(b"+0x")
        || bytes.starts_with(b"+0X")
        || bytes.starts_with(b"-0x")
        || bytes.starts_with(b"-0X");
    if !has_hex {
        if !bytes.contains(&b'.') && !bytes.contains(&b'e') && !bytes.contains(&b'E') {
            if let Ok(i) = text.parse::<i64>() {
                return Some(ReadOneResult::Int(i));
            }
        }
        return text.parse::<f64>().ok().map(ReadOneResult::Float);
    }
    let (neg, rest) = match bytes[0] {
        b'-' => (true, &text[3..]),
        b'+' => (false, &text[3..]),
        _ => (false, &text[2..]),
    };
    let (mantissa, exp_part) = match rest.find(['p', 'P']) {
        Some(idx) => (&rest[..idx], Some(&rest[idx + 1..])),
        None => (rest, None),
    };
    if exp_part.is_none() && !mantissa.contains('.') {
        if mantissa.is_empty() {
            return None;
        }
        let u = u64::from_str_radix(mantissa, 16).ok()?;
        let val = if neg {
            (u as i64).wrapping_neg()
        } else {
            u as i64
        };
        return Some(ReadOneResult::Int(val));
    }
    let exp: i32 = match exp_part {
        Some(s) => s.parse().ok()?,
        None => 0,
    };
    let (int_str, frac_str) = match mantissa.find('.') {
        Some(dot) => (&mantissa[..dot], &mantissa[dot + 1..]),
        None => (mantissa, ""),
    };
    if int_str.is_empty() && frac_str.is_empty() {
        return None;
    }
    let mut acc = 0.0f64;
    for ch in int_str.chars() {
        acc = acc * 16.0 + (ch.to_digit(16)? as f64);
    }
    let mut scale = 1.0 / 16.0;
    for ch in frac_str.chars() {
        acc += (ch.to_digit(16)? as f64) * scale;
        scale /= 16.0;
    }
    let val = acc * 2.0f64.powi(exp);
    Some(ReadOneResult::Float(if neg { -val } else { val }))
}

fn read_number_from(data: &[u8], pos: &mut usize) -> ReadOneResult {
    let start = *pos;
    let end = data.len();
    let mut cur = start;
    while cur < end && matches!(data[cur], b' ' | b'\t'..=b'\r') {
        cur += 1;
    }
    let num_start = cur;
    if cur < end && matches!(data[cur], b'+' | b'-') {
        cur += 1;
    }
    let is_hex = cur + 1 < end && data[cur] == b'0' && matches!(data[cur + 1], b'x' | b'X');
    if is_hex {
        cur += 2;
        let mut digits = 0usize;
        while cur < end && data[cur].is_ascii_hexdigit() {
            cur += 1;
            digits += 1;
        }
        if cur < end && data[cur] == b'.' {
            cur += 1;
            while cur < end && data[cur].is_ascii_hexdigit() {
                cur += 1;
                digits += 1;
            }
        }
        if digits == 0 {
            *pos = cur;
            return ReadOneResult::Eof;
        }
        if cur < end && matches!(data[cur], b'p' | b'P') {
            let save = cur;
            cur += 1;
            if cur < end && matches!(data[cur], b'+' | b'-') {
                cur += 1;
            }
            let exp_start = cur;
            while cur < end && data[cur].is_ascii_digit() {
                cur += 1;
            }
            if cur == exp_start {
                cur = save;
            }
        }
    } else {
        let mut digits = 0usize;
        while cur < end && data[cur].is_ascii_digit() {
            cur += 1;
            digits += 1;
        }
        if cur < end && data[cur] == b'.' {
            cur += 1;
            while cur < end && data[cur].is_ascii_digit() {
                cur += 1;
                digits += 1;
            }
        }
        if digits == 0 {
            *pos = cur;
            return ReadOneResult::Eof;
        }
        if cur < end && matches!(data[cur], b'e' | b'E') {
            let save = cur;
            cur += 1;
            if cur < end && matches!(data[cur], b'+' | b'-') {
                cur += 1;
            }
            let exp_start = cur;
            while cur < end && data[cur].is_ascii_digit() {
                cur += 1;
            }
            if cur == exp_start {
                cur = save;
            }
        }
    }
    *pos = cur;
    let text = std::str::from_utf8(&data[num_start..cur]).unwrap_or("");
    parse_lua_numeral(text).unwrap_or(ReadOneResult::Eof)
}

fn read_one(ef: &mut EmbeddedFile, fmt: ReadFormat) -> Result<ReadOneResult, String> {
    let size = ef.size;
    let pos = ef.pos;
    let EmbeddedState::File(bytes) = &mut ef.state else {
        return Err("attempt to use a closed file".to_string());
    };
    match fmt {
        ReadFormat::Count(n) => {
            if pos >= size {
                return Ok(ReadOneResult::Eof);
            }
            if n <= 0 {
                return Ok(ReadOneResult::Bytes(SmallBytes::Inline([0u8; 512], 0)));
            }
            let end = pos.saturating_add(n as usize).min(size);
            ef.pos = end;
            let slice = bytes.slice(pos, end)?;
            Ok(ReadOneResult::Bytes(SmallBytes::from_slice(slice)))
        }
        ReadFormat::All => {
            if pos >= size {
                return Ok(ReadOneResult::Bytes(SmallBytes::Inline([0u8; 512], 0)));
            }
            ef.pos = size;
            let slice = bytes.slice(pos, size)?;
            Ok(ReadOneResult::Bytes(SmallBytes::from_slice(slice)))
        }
        ReadFormat::Line(keep_nl) => {
            if pos >= size {
                return Ok(ReadOneResult::Eof);
            }
            let data = bytes.full()?;
            match data[pos..size].iter().position(|&b| b == b'\n') {
                None => {
                    ef.pos = size;
                    Ok(ReadOneResult::Bytes(SmallBytes::from_slice(
                        &data[pos..size],
                    )))
                }
                Some(rel) => {
                    let nl_end = pos + rel + 1;
                    ef.pos = nl_end;
                    let slice_end = if keep_nl { nl_end } else { nl_end - 1 };
                    Ok(ReadOneResult::Bytes(SmallBytes::from_slice(
                        &data[pos..slice_end],
                    )))
                }
            }
        }
        ReadFormat::Number => {
            let data = bytes.full()?;
            let mut cur = pos.min(size);
            let res = read_number_from(data, &mut cur);
            if pos <= size {
                ef.pos = cur;
            }
            Ok(res)
        }
    }
}

fn push_read_result(cb: &mut CallbackLua<'_>, res: ReadOneResult) -> LuaResult<usize> {
    match res {
        ReadOneResult::Eof => cb.push_nil(),
        ReadOneResult::Bytes(b) => cb.push_bytes(b.as_slice()),
        ReadOneResult::Int(i) => cb.push_int(i),
        ReadOneResult::Float(f) => cb.push_float(f),
    }
}

fn parse_seek_offset(cb: &mut CallbackLua<'_>) -> Result<i64, LuaError> {
    match cb.arg_kind(3) {
        None | Some(LuaValueKind::Nil) => Ok(0),
        Some(LuaValueKind::Integer | LuaValueKind::Float) => cb.arg_integer(3).ok_or_else(|| {
            cb.error("bad argument #2 to 'seek' (number has no integer representation)")
        }),
        Some(LuaValueKind::String) => {
            let parsed = cb.arg_bytes(3, |b| {
                let s = std::str::from_utf8(b).ok()?.trim();
                s.parse::<i64>().ok().or_else(|| {
                    let f = s.parse::<f64>().ok()?;
                    if f.is_finite()
                        && f.fract() == 0.0
                        && f >= i64::MIN as f64
                        && f < 9223372036854775808.0
                    {
                        Some(f as i64)
                    } else {
                        None
                    }
                })
            });
            parsed.flatten().ok_or_else(|| {
                cb.error("bad argument #2 to 'seek' (number has no integer representation)")
            })
        }
        _ => Err(cb.error("bad argument #2 to 'seek' (number has no integer representation)")),
    }
}

pub(crate) fn register(lua: &mut Lua, s: &LuaTable) -> Result<(), String> {
    sys_reg!(lua, s, "embedded_is_path", |path: LuaString| -> bool {
        std::str::from_utf8(&bytes_of(&path)).is_ok_and(tex_kpse::embedded_tree::is_embedded_path)
    });
    sys_reg!(
        lua,
        s,
        "embedded_read",
        |path: LuaString| -> Option<LuaBytes> {
            let bytes = bytes_of(&path);
            let text = std::str::from_utf8(&bytes).ok()?;
            tex_kpse::embedded_tree::read(text).map(LuaBytes)
        }
    );

    let open = lua
        .create_callback(|cb| {
            let path: LuaString = cb.arg(1)?;
            let meta: LuaTable = cb.arg(2)?;
            let is_dir_check: bool = cb.arg(3)?;
            let bytes = bytes_of(&path);
            let handle_data = if is_dir_check {
                Some(EmbeddedFile::new_directory())
            } else {
                std::str::from_utf8(&bytes)
                    .ok()
                    .and_then(|p| {
                        tex_kpse::EmbeddedFontFile::open(p)
                            .map(ReadBytes::Font)
                            .or_else(|| tex_kpse::embedded_tree::read(p).map(ReadBytes::Decoded))
                    })
                    .map(EmbeddedFile::new_file)
            };
            let Some(ef) = handle_data else {
                return cb.push_nil();
            };
            let ud = cb.create_userdata(ef)?;
            let val = cb.pack(&ud)?;
            val.set_metatable(Some(&meta))?;
            cb.push(val)
        })
        .map_err(|e| format!("{e:?}"))?;
    s.set("embedded_open", open).map_err(|e| format!("{e:?}"))?;

    let vfile_type = lua
        .create_callback(|cb| {
            match cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| ef.is_closed()) {
                Some(true) => cb.push_bytes(b"closed file"),
                Some(false) => cb.push_bytes(b"file"),
                None => cb.push_nil(),
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    s.set("vfile_type", vfile_type)
        .map_err(|e| format!("{e:?}"))?;

    let vfile_is_closed = lua
        .create_callback(|cb| {
            match cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| ef.is_closed()) {
                Some(closed) => cb.push_bool(closed),
                None => Err(bad_vfile_self(cb, "lines")),
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    s.set("vfile_is_closed", vfile_is_closed)
        .map_err(|e| format!("{e:?}"))?;

    let vfile_read = lua
        .create_callback(|cb| {
            let state_check = cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| match ef.state {
                EmbeddedState::Closed => 1u8,
                EmbeddedState::Directory => 2u8,
                EmbeddedState::File(_) => 0u8,
            });
            match state_check {
                None => return Err(bad_vfile_self(cb, "read")),
                Some(1) => return Err(cb.error("attempt to use a closed file")),
                Some(2) => return push_failure(cb, "Is a directory", 21),
                Some(_) => {}
            }
            let nargs = cb.arg_count();
            if nargs <= 1 {
                let res = cb
                    .arg_userdata_mut::<EmbeddedFile, _>(1, |ef| {
                        read_one(ef, ReadFormat::Line(false))
                    })
                    .expect("checked EmbeddedFile")
                    .map_err(|e| cb.error(e))?;
                return push_read_result(cb, res);
            }
            if nargs == 2 {
                let fmt = parse_read_format(cb, 2)?;
                let res = cb
                    .arg_userdata_mut::<EmbeddedFile, _>(1, |ef| read_one(ef, fmt))
                    .expect("checked EmbeddedFile")
                    .map_err(|e| cb.error(e))?;
                return push_read_result(cb, res);
            }
            let mut pushed = 0usize;
            for idx in 2..=nargs {
                let fmt = parse_read_format(cb, idx)?;
                let res = cb
                    .arg_userdata_mut::<EmbeddedFile, _>(1, |ef| read_one(ef, fmt))
                    .expect("checked EmbeddedFile")
                    .map_err(|e| cb.error(e))?;
                let is_eof = matches!(res, ReadOneResult::Eof);
                pushed += push_read_result(cb, res)?;
                if is_eof {
                    break;
                }
            }
            Ok(pushed)
        })
        .map_err(|e| format!("{e:?}"))?;
    s.set("vfile_read", vfile_read)
        .map_err(|e| format!("{e:?}"))?;

    let vfile_seek = lua
        .create_callback(|cb| {
            let info = cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| {
                (ef.is_closed(), ef.pos as i64, ef.size as i64)
            });
            let Some((closed, cur_pos, size)) = info else {
                return Err(bad_vfile_self(cb, "seek"));
            };
            if closed {
                return Err(cb.error("attempt to use a closed file"));
            }
            let base = match cb.arg_kind(2) {
                None | Some(LuaValueKind::Nil) => cur_pos,
                Some(LuaValueKind::String) => {
                    let b = cb.arg_bytes(2, |w| match w {
                        b"set" => Ok(0i64),
                        b"cur" => Ok(cur_pos),
                        b"end" => Ok(size),
                        other => Err(String::from_utf8_lossy(other).into_owned()),
                    });
                    match b.expect("string arg") {
                        Ok(v) => v,
                        Err(opt) => {
                            return Err(cb.error(format!(
                                "bad argument #1 to 'seek' (invalid option '{opt}')"
                            )));
                        }
                    }
                }
                kind => {
                    let shown = match kind {
                        Some(LuaValueKind::Boolean) => {
                            cb.arg::<bool>(2).unwrap_or(false).to_string()
                        }
                        Some(LuaValueKind::Integer) => cb.arg_integer(2).unwrap_or(0).to_string(),
                        Some(LuaValueKind::Float) => cb.arg_number(2).unwrap_or(0.0).to_string(),
                        _ => lua_type_name(kind).to_string(),
                    };
                    return Err(cb.error(format!(
                        "bad argument #1 to 'seek' (invalid option '{shown}')"
                    )));
                }
            };
            let offset = parse_seek_offset(cb)?;
            let Some(target) = base.checked_add(offset) else {
                return push_failure(cb, "Invalid argument", 22);
            };
            if target < 0 {
                return push_failure(cb, "Invalid argument", 22);
            }
            cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| {
                ef.pos = target as usize;
            });
            cb.push_int(target)
        })
        .map_err(|e| format!("{e:?}"))?;
    s.set("vfile_seek", vfile_seek)
        .map_err(|e| format!("{e:?}"))?;

    let vfile_close = lua
        .create_callback(|cb| {
            let state = cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| {
                let was_closed = ef.is_closed();
                ef.state = EmbeddedState::Closed;
                was_closed
            });
            match state {
                None => Err(bad_vfile_self(cb, "close")),
                Some(true) => Err(cb.error("attempt to use a closed file")),
                Some(false) => cb.push_bool(true),
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    s.set("vfile_close", vfile_close)
        .map_err(|e| format!("{e:?}"))?;

    let vfile_close_silent = lua
        .create_callback(|cb| {
            cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| {
                ef.state = EmbeddedState::Closed;
            });
            cb.push_bool(true)
        })
        .map_err(|e| format!("{e:?}"))?;
    s.set("vfile_close_silent", vfile_close_silent)
        .map_err(|e| format!("{e:?}"))?;

    let vfile_write = lua
        .create_callback(|cb| {
            match cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| ef.is_closed()) {
                None => Err(bad_vfile_self(cb, "write")),
                Some(true) => Err(cb.error("attempt to use a closed file")),
                Some(false) => push_failure(cb, "Bad file descriptor", 9),
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    s.set("vfile_write", vfile_write)
        .map_err(|e| format!("{e:?}"))?;

    let vfile_flush = lua
        .create_callback(|cb| {
            match cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| ef.is_closed()) {
                None => Err(bad_vfile_self(cb, "flush")),
                Some(true) => Err(cb.error("attempt to use a closed file")),
                Some(false) => {
                    let f: tex_lua::Value = cb.arg(1)?;
                    cb.push(f)
                }
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    s.set("vfile_flush", vfile_flush)
        .map_err(|e| format!("{e:?}"))?;

    let vfile_setvbuf = lua
        .create_callback(|cb| {
            match cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| ef.is_closed()) {
                None => Err(bad_vfile_self(cb, "setvbuf")),
                Some(true) => Err(cb.error("attempt to use a closed file")),
                Some(false) => cb.push_bool(true),
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    s.set("vfile_setvbuf", vfile_setvbuf)
        .map_err(|e| format!("{e:?}"))?;

    Ok(())
}
