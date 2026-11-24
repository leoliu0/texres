//! Native `fio` (binary readers on `io.open` handles — both `EmbeddedFile`
//! virtual handles and real OS `LuaFile` streams) and `sio` (the same on Lua
//! strings), matching LuaTeX's `liolibext.c` without Lua wrapper calls,
//! intermediate `LuaString` allocations, or `string.unpack` format parsing.

use std::io::SeekFrom;

use tex_lua::{CallbackLua, Lua, LuaApi, LuaError, LuaFile, LuaTable, LuaValueKind};

use crate::lua_sys_embedded::{EmbeddedFile, EmbeddedState};

pub(crate) const PRELUDE: &str = include_str!("lua_sys_fio.lua");

fn arg_type_name(kind: Option<LuaValueKind>) -> &'static str {
    match kind {
        None | Some(LuaValueKind::Nil) => "no value",
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

#[inline]
fn is_string_or_number(kind: Option<LuaValueKind>) -> bool {
    matches!(
        kind,
        Some(LuaValueKind::String | LuaValueKind::Integer | LuaValueKind::Float)
    )
}

fn bad_fio_handle(cb: &mut CallbackLua<'_>, fname: &str) -> LuaError {
    let got = arg_type_name(cb.arg_kind(1));
    cb.error(format!(
        "bad argument #1 to 'fio.{fname}' (FILE* expected, got {got})"
    ))
}

#[inline]
fn decode_int(bytes: &[u8], width: usize, signed: bool, little: bool) -> i64 {
    match (width, signed, little) {
        (1, false, _) => bytes[0] as i64,
        (1, true, _) => (bytes[0] as i8) as i64,
        (2, false, false) => u16::from_be_bytes([bytes[0], bytes[1]]) as i64,
        (2, false, true) => u16::from_le_bytes([bytes[0], bytes[1]]) as i64,
        (2, true, false) => i16::from_be_bytes([bytes[0], bytes[1]]) as i64,
        (2, true, true) => i16::from_le_bytes([bytes[0], bytes[1]]) as i64,
        (3, false, false) => {
            ((bytes[0] as i64) << 16) | ((bytes[1] as i64) << 8) | (bytes[2] as i64)
        }
        (3, false, true) => {
            (bytes[0] as i64) | ((bytes[1] as i64) << 8) | ((bytes[2] as i64) << 16)
        }
        (3, true, false) => {
            let u = ((bytes[0] as u32) << 16) | ((bytes[1] as u32) << 8) | (bytes[2] as u32);
            (((u as i32) << 8) >> 8) as i64
        }
        (3, true, true) => {
            let u = (bytes[0] as u32) | ((bytes[1] as u32) << 8) | ((bytes[2] as u32) << 16);
            (((u as i32) << 8) >> 8) as i64
        }
        // LuaTeX's liolibext.c computes 4-byte cardinals in a signed 32-bit C `int`.
        (4, _, false) => i32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]) as i64,
        (4, _, true) => i32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]) as i64,
        _ => unreachable!(),
    }
}

/// Read exact `N` bytes (`1..=4`) from `arg 1` (`EmbeddedFile` or `LuaFile`),
/// advancing file position to EOF on partial reads just like `f:read(N)`.
#[inline]
fn fio_read_fixed<const N: usize>(
    cb: &mut CallbackLua<'_>,
    fname: &str,
) -> Result<Option<[u8; N]>, LuaError> {
    if let Some(res) =
        cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| -> Result<Option<[u8; N]>, &'static str> {
            let pos = ef.pos;
            let size = ef.size;
            match &mut ef.state {
                EmbeddedState::Closed => Err("attempt to use a closed file"),
                EmbeddedState::Directory => Ok(None),
                EmbeddedState::File(bytes) => {
                    if pos >= size {
                        return Ok(None);
                    }
                    if pos + N > size {
                        ef.pos = size;
                        return Ok(None);
                    }
                    ef.pos = pos + N;
                    let slice = bytes
                        .slice(pos, pos + N)
                        .map_err(|_| "embedded file could not be decoded")?;
                    let mut out = [0u8; N];
                    out.copy_from_slice(slice);
                    Ok(Some(out))
                }
            }
        })
    {
        return res.map_err(|msg| cb.error(msg));
    }

    if let Some(res) =
        cb.arg_userdata_mut::<LuaFile, _>(1, |lf| -> Result<Option<[u8; N]>, &'static str> {
            if lf.is_closed() {
                return Err("attempt to use a closed file");
            }
            let mut out = [0u8; N];
            match lf.read_exact_or_eof(&mut out) {
                Ok(got) if got == N => Ok(Some(out)),
                Ok(_) | Err(_) => Ok(None),
            }
        })
    {
        return res.map_err(|msg| cb.error(msg));
    }

    Err(bad_fio_handle(cb, fname))
}

/// Permissive integer conversion used by `fio`'s count/offset arguments (`integer(v)`).
#[inline]
fn opt_integer(cb: &CallbackLua<'_>, index: usize) -> i64 {
    match cb.arg_kind(index) {
        Some(LuaValueKind::Integer | LuaValueKind::Float) => cb
            .arg_integer(index)
            .unwrap_or_else(|| cb.arg_number(index).map_or(0, |n| n.floor() as i64)),
        Some(LuaValueKind::String) => cb
            .arg_bytes(index, |b| {
                let s = std::str::from_utf8(b).ok()?.trim();
                s.parse::<i64>()
                    .ok()
                    .or_else(|| s.parse::<f64>().ok().map(|f| f.floor() as i64))
            })
            .flatten()
            .unwrap_or(0),
        _ => 0,
    }
}

/// Strict integer check used by `sio` position arguments (`checkinteger(v, n, fname)`).
#[inline]
fn check_sio_integer(cb: &mut CallbackLua<'_>, index: usize, fname: &str) -> Result<i64, LuaError> {
    if let Some(i) = cb.arg_integer(index) {
        return Ok(i);
    }
    if cb.arg_kind(index) == Some(LuaValueKind::String) {
        let parsed = cb.arg_bytes(index, |b| {
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
        if let Some(Some(i)) = parsed {
            return Ok(i);
        }
    }
    let got = arg_type_name(cb.arg_kind(index));
    Err(cb.error(format!(
        "bad argument #{index} to '{fname}' (number expected, got {got})"
    )))
}

#[inline]
fn run_sio_bytes<R>(
    cb: &mut CallbackLua<'_>,
    fname: &str,
    f: impl FnOnce(&[u8]) -> R,
) -> Result<R, LuaError> {
    match cb.arg_kind(1) {
        Some(LuaValueKind::String) => Ok(cb.arg_bytes(1, f).expect("checked string")),
        Some(LuaValueKind::Integer | LuaValueKind::Float) => {
            let text = if let Some(i) = cb.arg_integer(1) {
                i.to_string()
            } else {
                cb.arg_number(1).unwrap_or(0.0).to_string()
            };
            Ok(f(text.as_bytes()))
        }
        kind => {
            let got = arg_type_name(kind);
            Err(cb.error(format!(
                "bad argument #1 to '{fname}' (string expected, got {got})"
            )))
        }
    }
}

#[inline]
fn dot14(n: u16) -> f64 {
    let mut top = (n >> 14) as i32;
    if top >= 2 {
        top -= 4;
    }
    (top as f64) + ((n & 0x3fff) as f64) / 16384.0
}

fn read_fio_var_bytes(
    cb: &mut CallbackLua<'_>,
    fname: &str,
    n: usize,
) -> Result<Option<Vec<u8>>, LuaError> {
    if let Some(res) =
        cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| -> Result<Option<Vec<u8>>, &'static str> {
            let pos = ef.pos;
            let size = ef.size;
            match &mut ef.state {
                EmbeddedState::Closed => Err("attempt to use a closed file"),
                EmbeddedState::Directory => Ok(None),
                EmbeddedState::File(bytes) => {
                    if pos >= size {
                        return Ok(None);
                    }
                    let end = pos.saturating_add(n).min(size);
                    ef.pos = end;
                    let slice = bytes
                        .slice(pos, end)
                        .map_err(|_| "embedded file could not be decoded")?;
                    Ok(Some(slice.to_vec()))
                }
            }
        })
    {
        return res.map_err(|msg| cb.error(msg));
    }

    if let Some(res) =
        cb.arg_userdata_mut::<LuaFile, _>(1, |lf| -> Result<Option<Vec<u8>>, &'static str> {
            if lf.is_closed() {
                return Err("attempt to use a closed file");
            }
            let mut out = Vec::with_capacity(n.min(65536));
            if lf.read_chars(&mut out, n).is_err() || out.is_empty() {
                return Ok(None);
            }
            Ok(Some(out))
        })
    {
        return res.map_err(|msg| cb.error(msg));
    }

    Err(bad_fio_handle(cb, fname))
}

fn register_int_reader(
    lua: &mut Lua,
    fio: &LuaTable,
    sio: &LuaTable,
    name: &'static str,
    width: usize,
    signed: bool,
    little: bool,
) -> Result<(), String> {
    let fio_fn = lua
        .create_callback(move |cb| {
            let val = match width {
                1 => fio_read_fixed::<1>(cb, name)?.map(|b| decode_int(&b, 1, signed, little)),
                2 => fio_read_fixed::<2>(cb, name)?.map(|b| decode_int(&b, 2, signed, little)),
                3 => fio_read_fixed::<3>(cb, name)?.map(|b| decode_int(&b, 3, signed, little)),
                4 => fio_read_fixed::<4>(cb, name)?.map(|b| decode_int(&b, 4, signed, little)),
                _ => unreachable!(),
            };
            match val {
                Some(v) => cb.push_int(v),
                None => cb.push_nil(),
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    fio.set(name, fio_fn.clone())
        .map_err(|e| format!("{e:?}"))?;

    let sio_fn = lua
        .create_callback(move |cb| {
            // Check string first (`arg #1`), then integer (`arg #2`), matching Lua order.
            if !is_string_or_number(cb.arg_kind(1)) {
                let got = arg_type_name(cb.arg_kind(1));
                return Err(cb.error(format!(
                    "bad argument #1 to '{name}' (string expected, got {got})"
                )));
            }
            let pos = check_sio_integer(cb, 2, name)?;
            let val = run_sio_bytes(cb, name, |s| {
                if pos < 1 {
                    return None;
                }
                let start = (pos - 1) as usize;
                let end = start.checked_add(width)?;
                if end > s.len() {
                    return None;
                }
                Some(decode_int(&s[start..end], width, signed, little))
            })?;
            match val {
                Some(v) => cb.push_int(v),
                None => cb.push_nil(),
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    sio.set(name, sio_fn.clone())
        .map_err(|e| format!("{e:?}"))?;

    if width == 1 {
        let le_name = if signed {
            "readinteger1le"
        } else {
            "readcardinal1le"
        };
        fio.set(le_name, fio_fn).map_err(|e| format!("{e:?}"))?;
        sio.set(le_name, sio_fn).map_err(|e| format!("{e:?}"))?;
    }
    Ok(())
}

fn register_table_reader(
    lua: &mut Lua,
    fio: &LuaTable,
    sio: &LuaTable,
    signed: bool,
) -> Result<(), String> {
    let fname: &'static str = if signed {
        "readintegertable"
    } else {
        "readcardinaltable"
    };
    let fio_fn = lua
        .create_callback(move |cb| {
            // Validate handle first (`tofile(f, fname)`), even when `n <= 0`.
            let is_embedded = cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| ef.is_closed());
            if let Some(true) = is_embedded {
                return Err(cb.error("attempt to use a closed file"));
            }
            if is_embedded.is_none() {
                match cb.arg_userdata_mut::<LuaFile, _>(1, |lf| lf.is_closed()) {
                    Some(true) => return Err(cb.error("attempt to use a closed file")),
                    None => return Err(bad_fio_handle(cb, fname)),
                    Some(false) => {}
                }
            }
            let n = opt_integer(cb, 2);
            let b = opt_integer(cb, 3);
            if n <= 0 || !(1..=4).contains(&b) {
                return cb.push_int_array(std::iter::empty());
            }
            let width = b as usize;
            let req_bytes = (n as usize).saturating_mul(width);

            // Fast zero-copy path for `EmbeddedFile`: decode straight from slice into Lua array!
            if is_embedded.is_some() {
                enum FastTable {
                    Empty,
                    Decoded(Vec<i64>),
                }
                let decoded = cb
                    .arg_userdata_mut::<EmbeddedFile, _>(
                        1,
                        |ef| -> Result<FastTable, &'static str> {
                            let pos = ef.pos;
                            let size = ef.size;
                            let EmbeddedState::File(bytes) = &mut ef.state else {
                                return Ok(FastTable::Empty);
                            };
                            if pos >= size {
                                return Ok(FastTable::Empty);
                            }
                            let end = pos.saturating_add(req_bytes).min(size);
                            ef.pos = end;
                            let slice = bytes
                                .slice(pos, end)
                                .map_err(|_| "embedded file could not be decoded")?;
                            let count = slice.len() / width;
                            let mut out = Vec::with_capacity(count);
                            for chunk in slice.chunks_exact(width) {
                                out.push(decode_int(chunk, width, signed, false));
                            }
                            Ok(FastTable::Decoded(out))
                        },
                    )
                    .expect("checked EmbeddedFile")
                    .map_err(|msg| cb.error(msg))?;
                return match decoded {
                    FastTable::Empty => cb.push_int_array(std::iter::empty()),
                    FastTable::Decoded(vals) => cb.push_int_array(vals.into_iter()),
                };
            }

            let Some(raw) = read_fio_var_bytes(cb, fname, req_bytes)? else {
                return cb.push_int_array(std::iter::empty());
            };
            let count = (raw.len() / width).min(n as usize);
            cb.push_int_array(
                raw[..count * width]
                    .chunks_exact(width)
                    .map(|chunk| decode_int(chunk, width, signed, false)),
            )
        })
        .map_err(|e| format!("{e:?}"))?;
    fio.set(fname, fio_fn).map_err(|e| format!("{e:?}"))?;

    let sio_fn = lua
        .create_callback(move |cb| {
            if !is_string_or_number(cb.arg_kind(1)) {
                let got = arg_type_name(cb.arg_kind(1));
                return Err(cb.error(format!(
                    "bad argument #1 to '{fname}' (string expected, got {got})"
                )));
            }
            let pos = check_sio_integer(cb, 2, fname)?;
            let n = opt_integer(cb, 3);
            let b = opt_integer(cb, 4);
            if pos < 1 || n <= 0 || !(1..=4).contains(&b) {
                return cb.push_int_array(std::iter::empty());
            }
            let width = b as usize;
            let start = (pos - 1) as usize;
            let vals = run_sio_bytes(cb, fname, |s| {
                if start >= s.len() {
                    return Vec::new();
                }
                let avail = (s.len() - start) / width;
                let count = avail.min(n as usize);
                s[start..start + count * width]
                    .chunks_exact(width)
                    .map(|chunk| decode_int(chunk, width, signed, false))
                    .collect::<Vec<i64>>()
            })?;
            cb.push_int_array(vals.into_iter())
        })
        .map_err(|e| format!("{e:?}"))?;
    sio.set(fname, sio_fn).map_err(|e| format!("{e:?}"))?;
    Ok(())
}

pub(crate) fn register(lua: &mut Lua, s: &LuaTable) -> Result<(), String> {
    let fio = lua.create_table().map_err(|e| format!("{e:?}"))?;
    let sio = lua.create_table().map_err(|e| format!("{e:?}"))?;

    register_int_reader(lua, &fio, &sio, "readcardinal1", 1, false, false)?;
    register_int_reader(lua, &fio, &sio, "readinteger1", 1, true, false)?;
    register_int_reader(lua, &fio, &sio, "readcardinal2", 2, false, false)?;
    register_int_reader(lua, &fio, &sio, "readcardinal2le", 2, false, true)?;
    register_int_reader(lua, &fio, &sio, "readinteger2", 2, true, false)?;
    register_int_reader(lua, &fio, &sio, "readinteger2le", 2, true, true)?;
    register_int_reader(lua, &fio, &sio, "readcardinal3", 3, false, false)?;
    register_int_reader(lua, &fio, &sio, "readcardinal3le", 3, false, true)?;
    register_int_reader(lua, &fio, &sio, "readinteger3", 3, true, false)?;
    register_int_reader(lua, &fio, &sio, "readinteger3le", 3, true, true)?;
    register_int_reader(lua, &fio, &sio, "readcardinal4", 4, false, false)?;
    register_int_reader(lua, &fio, &sio, "readcardinal4le", 4, false, true)?;
    register_int_reader(lua, &fio, &sio, "readinteger4", 4, true, false)?;
    register_int_reader(lua, &fio, &sio, "readinteger4le", 4, true, true)?;

    register_table_reader(lua, &fio, &sio, false)?;
    register_table_reader(lua, &fio, &sio, true)?;

    // Fixed-point readers: readfixed2, readfixed4, read2dot14
    let fio_fixed2 = lua
        .create_callback(|cb| match fio_read_fixed::<2>(cb, "readfixed2")? {
            Some(b) => {
                let n = u16::from_be_bytes(b);
                cb.push_float(((n >> 8) as f64) + ((n & 0xff) as f64) / 256.0)
            }
            None => cb.push_nil(),
        })
        .map_err(|e| format!("{e:?}"))?;
    fio.set("readfixed2", fio_fixed2)
        .map_err(|e| format!("{e:?}"))?;

    let sio_fixed2 = lua
        .create_callback(|cb| {
            if !is_string_or_number(cb.arg_kind(1)) {
                let got = arg_type_name(cb.arg_kind(1));
                return Err(cb.error(format!(
                    "bad argument #1 to 'readfixed2' (string expected, got {got})"
                )));
            }
            let pos = check_sio_integer(cb, 2, "readfixed2")?;
            // Match LuaTeX's `p + 3 >= #s` check in liolibext.c.
            let val = run_sio_bytes(cb, "readfixed2", |s| {
                if pos < 1 {
                    return None;
                }
                let start = (pos - 1) as usize;
                if start.checked_add(3)? >= s.len() {
                    return None;
                }
                let n = u16::from_be_bytes([s[start], s[start + 1]]);
                Some(((n >> 8) as f64) + ((n & 0xff) as f64) / 256.0)
            })?;
            match val {
                Some(v) => cb.push_float(v),
                None => cb.push_nil(),
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    sio.set("readfixed2", sio_fixed2)
        .map_err(|e| format!("{e:?}"))?;

    let fio_fixed4 = lua
        .create_callback(|cb| match fio_read_fixed::<4>(cb, "readfixed4")? {
            Some(b) => {
                let n = i32::from_be_bytes(b) as i64;
                cb.push_float(((n >> 16) as f64) + ((n & 0xffff) as f64) / 65536.0)
            }
            None => cb.push_nil(),
        })
        .map_err(|e| format!("{e:?}"))?;
    fio.set("readfixed4", fio_fixed4)
        .map_err(|e| format!("{e:?}"))?;

    let sio_fixed4 = lua
        .create_callback(|cb| {
            if !is_string_or_number(cb.arg_kind(1)) {
                let got = arg_type_name(cb.arg_kind(1));
                return Err(cb.error(format!(
                    "bad argument #1 to 'readfixed4' (string expected, got {got})"
                )));
            }
            let pos = check_sio_integer(cb, 2, "readfixed4")?;
            let val = run_sio_bytes(cb, "readfixed4", |s| {
                if pos < 1 {
                    return None;
                }
                let start = (pos - 1) as usize;
                if start.checked_add(3)? >= s.len() {
                    return None;
                }
                let n =
                    i32::from_be_bytes([s[start], s[start + 1], s[start + 2], s[start + 3]]) as i64;
                Some(((n >> 16) as f64) + ((n & 0xffff) as f64) / 65536.0)
            })?;
            match val {
                Some(v) => cb.push_float(v),
                None => cb.push_nil(),
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    sio.set("readfixed4", sio_fixed4)
        .map_err(|e| format!("{e:?}"))?;

    let fio_2dot14 = lua
        .create_callback(|cb| match fio_read_fixed::<2>(cb, "read2dot14")? {
            Some(b) => cb.push_float(dot14(u16::from_be_bytes(b))),
            None => cb.push_nil(),
        })
        .map_err(|e| format!("{e:?}"))?;
    fio.set("read2dot14", fio_2dot14)
        .map_err(|e| format!("{e:?}"))?;

    let sio_2dot14 = lua
        .create_callback(|cb| {
            if !is_string_or_number(cb.arg_kind(1)) {
                let got = arg_type_name(cb.arg_kind(1));
                return Err(cb.error(format!(
                    "bad argument #1 to 'read2dot14' (string expected, got {got})"
                )));
            }
            let pos = check_sio_integer(cb, 2, "read2dot14")?;
            let val = run_sio_bytes(cb, "read2dot14", |s| {
                if pos < 1 {
                    return None;
                }
                let start = (pos - 1) as usize;
                if start.checked_add(1)? >= s.len() {
                    return None;
                }
                Some(dot14(u16::from_be_bytes([s[start], s[start + 1]])))
            })?;
            match val {
                Some(v) => cb.push_float(v),
                None => cb.push_nil(),
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    sio.set("read2dot14", sio_2dot14)
        .map_err(|e| format!("{e:?}"))?;

    // Position helpers: getposition, setposition, skipposition
    let getpos = lua
        .create_callback(|cb| {
            if let Some(res) = cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| {
                if ef.is_closed() {
                    Err("attempt to use a closed file")
                } else {
                    Ok(ef.pos as i64)
                }
            }) {
                let pos = res.map_err(|m| cb.error(m))?;
                return cb.push_int(pos);
            }
            if let Some(res) = cb.arg_userdata_mut::<LuaFile, _>(1, |lf| {
                if lf.is_closed() {
                    Err("attempt to use a closed file")
                } else {
                    Ok(lf.seek(SeekFrom::Current(0)).ok().map(|p| p as i64))
                }
            }) {
                return match res.map_err(|m| cb.error(m))? {
                    Some(pos) => cb.push_int(pos),
                    None => cb.push_nil(),
                };
            }
            Err(bad_fio_handle(cb, "getposition"))
        })
        .map_err(|e| format!("{e:?}"))?;
    fio.set("getposition", getpos)
        .map_err(|e| format!("{e:?}"))?;

    let setpos = lua
        .create_callback(|cb| {
            let p = opt_integer(cb, 2);
            if let Some(res) = cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| {
                if ef.is_closed() {
                    Err("attempt to use a closed file")
                } else if p < 0 {
                    Ok(false)
                } else {
                    ef.pos = p as usize;
                    Ok(true)
                }
            }) {
                return if res.map_err(|m| cb.error(m))? {
                    cb.push_int(0)
                } else {
                    cb.push_nil()
                };
            }
            if let Some(res) = cb.arg_userdata_mut::<LuaFile, _>(1, |lf| {
                if lf.is_closed() {
                    Err("attempt to use a closed file")
                } else if p < 0 {
                    Ok(false)
                } else {
                    Ok(lf.seek(SeekFrom::Start(p as u64)).is_ok())
                }
            }) {
                return if res.map_err(|m| cb.error(m))? {
                    cb.push_int(0)
                } else {
                    cb.push_nil()
                };
            }
            Err(bad_fio_handle(cb, "setposition"))
        })
        .map_err(|e| format!("{e:?}"))?;
    fio.set("setposition", setpos)
        .map_err(|e| format!("{e:?}"))?;

    let skippos = lua
        .create_callback(|cb| {
            let delta = opt_integer(cb, 2);
            if let Some(res) = cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| {
                if ef.is_closed() {
                    return Err("attempt to use a closed file");
                }
                let Some(target) = (ef.pos as i64).checked_add(delta) else {
                    return Ok(false);
                };
                if target < 0 {
                    Ok(false)
                } else {
                    ef.pos = target as usize;
                    Ok(true)
                }
            }) {
                return if res.map_err(|m| cb.error(m))? {
                    cb.push_int(0)
                } else {
                    cb.push_nil()
                };
            }
            if let Some(res) = cb.arg_userdata_mut::<LuaFile, _>(1, |lf| {
                if lf.is_closed() {
                    return Err("attempt to use a closed file");
                }
                let Ok(here) = lf.seek(SeekFrom::Current(0)) else {
                    return Ok(false);
                };
                let Some(target) = (here as i64).checked_add(delta) else {
                    return Ok(false);
                };
                if target < 0 {
                    Ok(false)
                } else {
                    Ok(lf.seek(SeekFrom::Start(target as u64)).is_ok())
                }
            }) {
                return if res.map_err(|m| cb.error(m))? {
                    cb.push_int(0)
                } else {
                    cb.push_nil()
                };
            }
            Err(bad_fio_handle(cb, "skipposition"))
        })
        .map_err(|e| format!("{e:?}"))?;
    fio.set("skipposition", skippos)
        .map_err(|e| format!("{e:?}"))?;

    // Byte readers: readbytes, readbytetable, readline
    let fio_readbytes = lua
        .create_callback(|cb| {
            let is_embedded = cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| ef.is_closed());
            if let Some(true) = is_embedded {
                return Err(cb.error("attempt to use a closed file"));
            }
            if is_embedded.is_none() {
                match cb.arg_userdata_mut::<LuaFile, _>(1, |lf| lf.is_closed()) {
                    Some(true) => return Err(cb.error("attempt to use a closed file")),
                    None => return Err(bad_fio_handle(cb, "readbytes")),
                    Some(false) => {}
                }
            }
            let n = opt_integer(cb, 2);
            if n <= 0 {
                return Ok(0);
            }
            let Some(bytes) = read_fio_var_bytes(cb, "readbytes", n as usize)? else {
                return Ok(0);
            };
            let mut pushed = 0usize;
            for b in bytes {
                pushed += cb.push_int(b as i64)?;
            }
            Ok(pushed)
        })
        .map_err(|e| format!("{e:?}"))?;
    fio.set("readbytes", fio_readbytes)
        .map_err(|e| format!("{e:?}"))?;

    let sio_readbytes = lua
        .create_callback(|cb| {
            if !is_string_or_number(cb.arg_kind(1)) {
                let got = arg_type_name(cb.arg_kind(1));
                return Err(cb.error(format!(
                    "bad argument #1 to 'readbytes' (string expected, got {got})"
                )));
            }
            let pos = check_sio_integer(cb, 2, "readbytes")?;
            let n = opt_integer(cb, 3);
            if pos < 1 || n <= 0 {
                return Ok(0);
            }
            let start = (pos - 1) as usize;
            let bytes = run_sio_bytes(cb, "readbytes", |s| {
                if start >= s.len() {
                    return Vec::new();
                }
                let end = start.saturating_add(n as usize).min(s.len());
                s[start..end].to_vec()
            })?;
            let mut pushed = 0usize;
            for b in bytes {
                pushed += cb.push_int(b as i64)?;
            }
            Ok(pushed)
        })
        .map_err(|e| format!("{e:?}"))?;
    sio.set("readbytes", sio_readbytes)
        .map_err(|e| format!("{e:?}"))?;

    let fio_bytetable = lua
        .create_callback(|cb| {
            let is_embedded = cb.arg_userdata_mut::<EmbeddedFile, _>(1, |ef| ef.is_closed());
            if let Some(true) = is_embedded {
                return Err(cb.error("attempt to use a closed file"));
            }
            if is_embedded.is_none() {
                match cb.arg_userdata_mut::<LuaFile, _>(1, |lf| lf.is_closed()) {
                    Some(true) => return Err(cb.error("attempt to use a closed file")),
                    None => return Err(bad_fio_handle(cb, "readbytetable")),
                    Some(false) => {}
                }
            }
            let n = opt_integer(cb, 2);
            if n <= 0 {
                return cb.push_int_array(std::iter::empty());
            }
            let Some(bytes) = read_fio_var_bytes(cb, "readbytetable", n as usize)? else {
                return cb.push_int_array(std::iter::empty());
            };
            cb.push_int_array(bytes.into_iter().map(|b| b as i64))
        })
        .map_err(|e| format!("{e:?}"))?;
    fio.set("readbytetable", fio_bytetable)
        .map_err(|e| format!("{e:?}"))?;

    let sio_bytetable = lua
        .create_callback(|cb| {
            if !is_string_or_number(cb.arg_kind(1)) {
                let got = arg_type_name(cb.arg_kind(1));
                return Err(cb.error(format!(
                    "bad argument #1 to 'readbytetable' (string expected, got {got})"
                )));
            }
            let pos = check_sio_integer(cb, 2, "readbytetable")?;
            let n = opt_integer(cb, 3);
            if pos < 1 {
                return cb.push_nil();
            }
            let start = (pos - 1) as usize;
            let maybe = run_sio_bytes(cb, "readbytetable", |s| {
                if start >= s.len() {
                    return None;
                }
                if n <= 0 {
                    return Some(Vec::new());
                }
                let end = start.saturating_add(n as usize).min(s.len());
                Some(s[start..end].to_vec())
            })?;
            match maybe {
                None => cb.push_nil(),
                Some(bytes) => cb.push_int_array(bytes.into_iter().map(|b| b as i64)),
            }
        })
        .map_err(|e| format!("{e:?}"))?;
    sio.set("readbytetable", sio_bytetable)
        .map_err(|e| format!("{e:?}"))?;

    // fio.readline: ends at `\n`, `\r`, or `\r\n`
    let readline = lua
        .create_callback(|cb| {
            if let Some(res) = cb.arg_userdata_mut::<EmbeddedFile, _>(
                1,
                |ef| -> Result<Option<Vec<u8>>, &'static str> {
                    let pos = ef.pos;
                    let size = ef.size;
                    match &mut ef.state {
                        EmbeddedState::Closed => Err("attempt to use a closed file"),
                        EmbeddedState::Directory => Ok(None),
                        EmbeddedState::File(bytes) => {
                            if pos >= size {
                                return Ok(None);
                            }
                            let data = bytes
                                .full()
                                .map_err(|_| "embedded file could not be decoded")?;
                            let tail = &data[pos..size];
                            match tail.iter().position(|&b| b == b'\n' || b == b'\r') {
                                None => {
                                    ef.pos = size;
                                    Ok(Some(tail.to_vec()))
                                }
                                Some(rel) => {
                                    let line = tail[..rel].to_vec();
                                    let mut next = pos + rel + 1;
                                    if tail[rel] == b'\r' && tail.get(rel + 1) == Some(&b'\n') {
                                        next += 1;
                                    }
                                    ef.pos = next;
                                    Ok(Some(line))
                                }
                            }
                        }
                    }
                },
            ) {
                return match res.map_err(|m| cb.error(m))? {
                    Some(line) => cb.push_bytes(&line),
                    None => cb.push_nil(),
                };
            }

            if let Some(res) = cb.arg_userdata_mut::<LuaFile, _>(
                1,
                |lf| -> Result<Option<Vec<u8>>, &'static str> {
                    if lf.is_closed() {
                        return Err("attempt to use a closed file");
                    }
                    let mut line = lf.take_scratch();
                    line.clear();
                    let had_nl = match lf.read_line(&mut line, true) {
                        Ok(nl) => nl,
                        Err(_) => {
                            lf.put_scratch(line);
                            return Ok(None);
                        }
                    };
                    if !had_nl && line.is_empty() {
                        lf.put_scratch(line);
                        return Ok(None);
                    }
                    if let Some(cr) = line.iter().position(|&b| b == b'\r') {
                        let rest = line.len() - 1 - cr;
                        if !(rest == 1 && line.last() == Some(&b'\n')) && rest > 0 {
                            let _ = lf.seek(SeekFrom::Current(-(rest as i64)));
                        }
                        line.truncate(cr);
                    } else if line.last() == Some(&b'\n') {
                        line.pop();
                    }
                    let out = line.clone();
                    lf.put_scratch(line);
                    Ok(Some(out))
                },
            ) {
                return match res.map_err(|m| cb.error(m))? {
                    Some(line) => cb.push_bytes(&line),
                    None => cb.push_nil(),
                };
            }

            Err(bad_fio_handle(cb, "readline"))
        })
        .map_err(|e| format!("{e:?}"))?;
    fio.set("readline", readline)
        .map_err(|e| format!("{e:?}"))?;

    s.set("fio", fio).map_err(|e| format!("{e:?}"))?;
    s.set("sio", sio).map_err(|e| format!("{e:?}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::register;
    use tex_lua::{Lua, LuaApi, SafeOption, Stdlib};

    #[test]
    fn sio_mininteger_position_does_not_underflow() {
        let mut lua = Lua::new_lua53(SafeOption::default());
        lua.open_stdlib(Stdlib::All).unwrap();
        let sys = lua.create_table().unwrap();
        register(&mut lua, &sys).unwrap();
        lua.set_global("sys", sys).unwrap();
        let ok: bool = lua
            .load(
                r##"
                local sio = sys.sio
                local m = math.mininteger
                local s = "abcdefgh"
                assert(sio.readcardinal1(s, m) == nil)
                assert(sio.readinteger2(s, m) == nil)
                assert(#sio.readcardinaltable(s, m, 2, 1) == 0)
                assert(#sio.readintegertable(s, m, 2, 1) == 0)
                assert(sio.readfixed2(s, m) == nil)
                assert(sio.readfixed4(s, m) == nil)
                assert(sio.read2dot14(s, m) == nil)
                assert(select("#", sio.readbytes(s, m, 2)) == 0)
                assert(sio.readbytetable(s, m, 2) == nil)
                return true
                "##,
            )
            .eval()
            .unwrap();
        assert!(ok);
    }
}
