//! The `node` and `node.direct` libraries (lnodelib.c). The natives below
//! work on integer node handles (the `node.direct` semantics); `lua_node.lua`
//! builds the public tables from them: `node.direct` is the natives under
//! their LuaTeX names, `node` wraps them for userdata nodes.

use std::rc::Rc;

use tex_lua::{CallbackLua, Lua, LuaApi, LuaResult, LuaTable, LuaValueKind, UdValue, UserDataTrait, Value, Variadic};

use crate::engine::Engine;
use crate::lua_bridge::with_engine;
use crate::lua_node::*;
use crate::lua_node_conv::sl;
use crate::tfm::Font;

/// The userdata form of a node: LuaTeX's `luatex.node` objects.
#[derive(Clone)]
pub struct NodeUd {
    pub h: u32,
}

pub(crate) fn glyph_dimensions(fonts: &[Rc<Font>], font: i32, c: i32) -> (i32, i32, i32) {
    let Some(f) = fonts.get(font as usize) else { return (0, 0, 0) };
    if let Some(ci) = f.lua_char(c as u32) {
        return (ci.width, ci.height, ci.depth);
    }
    if f.lua.is_none() && (0..256).contains(&c) {
        let c = c as u8;
        return (f.char_width(c), f.char_height(c), f.char_depth(c));
    }
    (0, 0, 0)
}

/// The value of a field as a Lua value; node values are handles (direct) or
/// userdata.
fn to_ud(v: Val, direct: bool) -> UdValue {
    match v {
        Val::Nil => UdValue::Nil,
        Val::Int(i) => UdValue::Integer(i),
        Val::Num(n) => UdValue::Number(n),
        Val::Node(h) => {
            if direct {
                UdValue::Integer(i64::from(h))
            } else {
                UdValue::from_userdata(NodeUd { h })
            }
        }
        Val::Bytes(b) => UdValue::Bytes(b),
        Val::Str(s) => UdValue::Str(s.to_string()),
        Val::Toks => UdValue::Nil,
    }
}

fn node_ud(h: u32) -> UdValue {
    if h == 0 { UdValue::Nil } else { UdValue::from_userdata(NodeUd { h }) }
}

impl Engine {
    fn lua_node_field(&self, n: u32, name: &str) -> Val {
        let s = &self.lua_nodes;
        if !s.valid(n) {
            return Val::Nil;
        }
        let fonts = &self.eqtb.fonts;
        let v = s.get_field(n, name, &|f, c| glyph_dimensions(fonts, f, c));
        if matches!(v, Val::Toks) {
            return Val::Bytes(self.lua_node_tokens(n));
        }
        v
    }

    /// The tokens of a mark / write node as text.
    fn lua_node_tokens(&self, n: u32) -> Vec<u8> {
        match &self.lua_nodes.node(n).ext {
            Some(e) => self.tokens_to_bytes(&e.toks),
            None => Vec::new(),
        }
    }

    /// The `mark` table of a mark node as {cmd, chr, cs} triples: the head of
    /// the token list ({0,0,0}) and every token but the last one, as LuaTeX's
    /// loop over the list does; `None` for other nodes.
    fn lua_mark_triples(&self, h: u32) -> Option<Vec<[i64; 3]>> {
        if !self.lua_nodes.valid(h) || self.lua_nodes.id(h) != MARK {
            return None;
        }
        let toks = match &self.lua_nodes.node(h).ext {
            Some(x) => &x.toks[..],
            None => &[],
        };
        let Some((_, init)) = toks.split_last() else { return Some(Vec::new()) };
        let mut triples = vec![[0, 0, 0]];
        for t in init {
            let (cmd, chr) = self.lua_cmd_mode(*t);
            let cs = if t.unfreeze().is_cs() { Engine::lua_tok_value(*t) - 0x1FFF_FFFF } else { 0 };
            triples.push([i64::from(cmd), chr, cs]);
        }
        Some(triples)
    }
}

fn is_mark(h: u32) -> bool {
    with_engine(|e| e.lua_nodes.valid(h) && e.lua_nodes.id(h) == MARK).unwrap_or(false)
}

/// `n.<name>` for a userdata node.
fn ud_get(n: u32, key: &str) -> Option<UdValue> {
    with_engine(|e| {
        let v = e.lua_node_field(n, key);
        Some(to_ud(v, false))
    })
    .ok()
    .flatten()
}

impl UserDataTrait for NodeUd {
    fn type_name(&self) -> &'static str {
        "luatex.node"
    }

    fn metatable_name(&self) -> Option<&'static str> {
        Some("luatex.node")
    }

    fn get_field(&self, key: &str) -> Option<UdValue> {
        // the `mark` table comes from the metatable's __index (node.getfield)
        if key == "mark" && is_mark(self.h) {
            return None;
        }
        ud_get(self.h, key)
    }

    fn get_int_field(&self, key: i64) -> Option<UdValue> {
        with_engine(|e| {
            let s = &e.lua_nodes;
            if !s.valid(self.h) {
                return UdValue::Nil;
            }
            let id = s.id(self.h);
            let sub = s.subtype(self.h);
            if !has_attr_type(id, sub) {
                return UdValue::Nil;
            }
            let v = s.has_attribute(self.h, key as i32, UNUSED_ATTRIBUTE);
            if v > UNUSED_ATTRIBUTE { UdValue::Integer(i64::from(v)) } else { UdValue::Nil }
        })
        .ok()
    }

    fn set_field(&mut self, key: &str, value: UdValue) -> Option<Result<(), String>> {
        if key == "mark" && is_mark(self.h) {
            return None;
        }
        let v = match value {
            UdValue::Nil => SetVal::Nil,
            UdValue::Integer(i) => SetVal::Int(i),
            UdValue::Number(n) => SetVal::Num(n),
            UdValue::Boolean(_) => SetVal::Other,
            UdValue::Str(s) => SetVal::Bytes(s.into_bytes()),
            UdValue::Bytes(b) => SetVal::Bytes(b),
            UdValue::Handle(h) => SetVal::Node(h as u32),
            _ => SetVal::Other,
        };
        let h = self.h;
        Some(match with_engine(|e| e.lua_set_node_field(h, key, v, false)) {
            Ok(r) => r,
            Err(e) => Err(e),
        })
    }

    fn set_int_field(&mut self, key: i64, value: UdValue) -> Option<Result<(), String>> {
        let val = match value {
            UdValue::Integer(i) => i,
            UdValue::Number(n) => n as i64,
            _ => 0,
        };
        let h = self.h;
        with_engine(|e| {
            if val as i32 == UNUSED_ATTRIBUTE {
                e.lua_nodes.unset_attribute(h, key as i32, UNUSED_ATTRIBUTE);
            } else {
                e.lua_nodes.set_attribute(h, key as i32, val as i32);
            }
        })
        .ok();
        Some(Ok(()))
    }

    fn lua_tostring(&self) -> Option<String> {
        Some(with_engine(|e| e.lua_nodes_tostring(self.h, "node")).unwrap_or_default())
    }

    fn lua_eq(&self, other: &dyn UserDataTrait) -> Option<bool> {
        other.as_any().downcast_ref::<NodeUd>().map(|o| o.h == self.h)
    }

    fn handle_id(&self) -> Option<i64> {
        Some(i64::from(self.h))
    }

    fn as_any(&self) -> &dyn std::any::Any {
        self
    }

    fn as_any_mut(&mut self) -> &mut dyn std::any::Any {
        self
    }
}

impl Engine {
    /// `setfield`; `direct` selects the integer form of node and attribute
    /// values.
    fn lua_set_node_field(&mut self, n: u32, name: &str, v: SetVal, direct: bool) -> Result<(), String> {
        if !self.lua_nodes.valid(n) {
            return Ok(());
        }
        // direct nodes are integers: a field that holds a node (list) takes
        // the handle, and keeps the list it names alive
        let v = match v {
            SetVal::Int(i) if direct && self.lua_nodes.is_node_field(n, name) => SetVal::Node(i as u32),
            v => v,
        };
        // `mark` and `write` take token lists
        let (id, sub) = (self.lua_nodes.id(n), self.lua_nodes.subtype(n));
        if (id == MARK && name == "mark") || (id == WHATSIT && sub == ws::WRITE && matches!(name, "data" | "value")) {
            if let SetVal::Bytes(b) = &v {
                let toks = self.lua_text_tokens(b);
                self.lua_nodes.node_mut(n).ext.get_or_insert_with(Default::default).toks = toks;
            }
            return Ok(());
        }
        self.lua_nodes.set_field(n, name, v)
    }

    /// A character-string as a token list (catcode 12, spaces 10).
    fn lua_text_tokens(&self, text: &[u8]) -> Vec<crate::token::Token> {
        text.iter().map(|&b| if b == b' ' { crate::token::Token::space() } else { crate::token::Token::other(b) }).collect()
    }

    pub(crate) fn lua_nodes_tostring(&self, n: u32, tag: &str) -> String {
        let s = &self.lua_nodes;
        if !s.valid(n) {
            return String::new();
        }
        let node = s.node(n);
        let a = if node.prev != 0 && node.id != ATTRIBUTE { format!("{:>6}", node.prev) } else { "   nil".to_string() };
        let v = if node.next != 0 { format!("{:>6}", node.next) } else { "   nil".to_string() };
        format!(
            "<{} {} < {:>6} > {} : {} {}>",
            tag,
            a,
            n,
            v,
            type_info(node.id).map_or("unknown", |t| t.name),
            node.subtype
        )
    }
}

macro_rules! nat {
    ($lua:expr, $tbl:expr, $name:literal, $f:expr) => {
        $tbl.set($name, $lua.create_function($f).map_err(|e| format!("{}: {e:?}", $name))?)
            .map_err(|e| format!("{}: {e:?}", $name))?
    };
}

macro_rules! cnat {
    ($lua:expr, $tbl:expr, $name:literal, $f:expr) => {
        $tbl.set($name, $lua.create_c_callback($f).map_err(|e| format!("{}: {e:?}", $name))?)
            .map_err(|e| format!("{}: {e:?}", $name))?
    };
}

#[inline(always)]
fn opt_int_arg(cx: &mut CallbackLua<'_>, idx: usize) -> LuaResult<Option<i64>> {
    if let Some(i) = cx.arg_integer(idx) {
        return Ok(Some(i));
    }
    if cx.arg_is_nil(idx) {
        return Ok(None);
    }
    cx.arg::<Option<i64>>(idx)
}

#[inline(always)]
fn handle_arg(cx: &mut CallbackLua<'_>, idx: usize) -> LuaResult<u32> {
    Ok(opt_int_arg(cx, idx)?.unwrap_or(0) as u32)
}

#[inline(always)]
fn opt_num_arg(cx: &mut CallbackLua<'_>, idx: usize) -> LuaResult<Option<f64>> {
    if let Some(n) = cx.arg_number(idx) {
        return Ok(Some(n));
    }
    if cx.arg_is_nil(idx) {
        return Ok(None);
    }
    cx.arg::<Option<f64>>(idx)
}

#[inline(always)]
fn arg_is_num(cx: &CallbackLua<'_>, idx: usize) -> bool {
    cx.arg_number(idx).is_some()
}

#[inline(always)]
fn arg_vint(cx: &CallbackLua<'_>, idx: usize) -> i64 {
    if let Some(i) = cx.arg_integer(idx) {
        i
    } else if let Some(n) = cx.arg_number(idx) {
        if n.fract() == 0.0 { n as i64 } else { 0 }
    } else if let Some(i) = cx
        .arg_bytes(idx, |b| std::str::from_utf8(b).ok().and_then(|s| s.trim().parse::<i64>().ok()))
        .flatten()
    {
        i
    } else {
        0
    }
}

#[inline(always)]
fn arg_vnum(cx: &CallbackLua<'_>, idx: usize) -> f64 {
    if let Some(n) = cx.arg_number(idx) {
        n
    } else if let Some(n) = cx
        .arg_bytes(idx, |b| std::str::from_utf8(b).ok().and_then(|s| s.trim().parse::<f64>().ok()))
        .flatten()
    {
        n
    } else {
        0.0
    }
}

#[inline(always)]
fn arg_vround(cx: &CallbackLua<'_>, idx: usize) -> i64 {
    (arg_vnum(cx, idx) + 0.5).floor() as i64
}

#[inline(always)]
fn push_opt_node(cx: &mut CallbackLua<'_>, h: u32) -> LuaResult<usize> {
    if h == 0 { cx.push_nil() } else { cx.push_int(i64::from(h)) }
}

#[inline(always)]
fn push_val(cx: &mut CallbackLua<'_>, v: Val, direct: bool) -> LuaResult<usize> {
    match v {
        Val::Nil | Val::Toks => cx.push_nil(),
        Val::Int(i) => cx.push_int(i),
        Val::Num(n) => cx.push_float(n),
        Val::Node(h) => {
            if direct {
                cx.push_int(i64::from(h))
            } else {
                cx.push_userdata(NodeUd { h })
            }
        }
        Val::Bytes(b) => cx.push_bytes(&b),
        Val::Str(s) => cx.push_bytes(s.as_bytes()),
    }
}

pub(crate) fn handle32(v: Option<i64>) -> u32 {
    v.unwrap_or(0) as u32
}

fn rnd(v: Option<f64>) -> i64 {
    v.map_or(0, |n| (n + 0.5).floor() as i64)
}

/// a value usable as `lua_tointeger` argument
pub(crate) fn value_int(v: &Value) -> i64 {
    if let Some(i) = v.as_integer() {
        i
    } else if let Some(n) = v.as_number() {
        if n.fract() == 0.0 { n as i64 } else { 0 }
    } else if let Some(s) = v.as_string() {
        s.trim().parse::<i64>().unwrap_or(0)
    } else {
        0
    }
}

pub(crate) fn value_num(v: &Value) -> f64 {
    if let Some(n) = v.as_number() {
        n
    } else if let Some(i) = v.as_integer() {
        i as f64
    } else if let Some(s) = v.as_string() {
        s.trim().parse::<f64>().unwrap_or(0.0)
    } else {
        0.0
    }
}

pub(crate) fn value_bytes(v: &Value) -> Option<Vec<u8>> {
    v.as_string_handle().map(|s| s.to_bytes())
}

fn is_truthy(v: Option<&Value>) -> bool {
    v.is_some_and(|v| !v.is_nil() && v.as_boolean() != Some(false))
}

#[inline(always)]
fn push_dir_string(cx: &mut CallbackLua<'_>, d: i32) -> LuaResult<usize> {
    if (0..4).contains(&d) { cx.push_bytes(DIR_NAMES[d as usize].as_bytes()) } else { cx.push_nil() }
}

/// Register the natives; returns the table the Lua side consumes.
pub(crate) fn install(lua: &mut Lua) -> Result<LuaTable, String> {
    let n: LuaTable = lua.create_table().map_err(|e| format!("{e:?}"))?;
    crate::lua_ud::install_tokens(lua, &n)?;

    // ---- identity, links ----
    cnat!(lua, n, "getid", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = with_engine(|e| e.lua_nodes.valid(h).then(|| i64::from(e.lua_nodes.id(h))))
            .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(v)
    });
    cnat!(lua, n, "getsubtype", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = with_engine(|e| e.lua_nodes.valid(h).then(|| i64::from(e.lua_nodes.subtype(h))))
            .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(v)
    });
    cnat!(lua, n, "setsubtype", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = opt_int_arg(cx, 2)?;
        with_engine(|e| {
            if let (true, Some(v)) = (e.lua_nodes.valid(h), v) {
                e.lua_nodes.node_mut(h).subtype = v as u16;
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "getnext", |cx| {
        let h = handle_arg(cx, 1)?;
        let x = with_engine(|e| if e.lua_nodes.valid(h) { e.lua_nodes.next(h) } else { 0 })
            .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, x)
    });
    cnat!(lua, n, "getprev", |cx| {
        let h = handle_arg(cx, 1)?;
        let x = with_engine(|e| if e.lua_nodes.valid(h) { e.lua_nodes.prev(h) } else { 0 })
            .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, x)
    });
    cnat!(lua, n, "getboth", |cx| {
        let h = handle_arg(cx, 1)?;
        let (p, x) = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return (0, 0);
            }
            (e.lua_nodes.prev(h), e.lua_nodes.next(h))
        })
        .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, p)?;
        push_opt_node(cx, x)?;
        Ok(2)
    });
    cnat!(lua, n, "setnext", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = opt_num_arg(cx, 2)?;
        with_engine(|e| {
            if e.lua_nodes.valid(h) {
                e.lua_nodes.node_mut(h).next = v.map_or(0, |v| v as u32);
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "setprev", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = opt_num_arg(cx, 2)?;
        with_engine(|e| {
            if e.lua_nodes.valid(h) {
                e.lua_nodes.node_mut(h).prev = v.map_or(0, |v| v as u32);
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "setboth", |cx| {
        let h = handle_arg(cx, 1)?;
        let p = opt_num_arg(cx, 2)?;
        let x = opt_num_arg(cx, 3)?;
        with_engine(|e| {
            if e.lua_nodes.valid(h) {
                let nd = e.lua_nodes.node_mut(h);
                nd.prev = p.map_or(0, |v| v as u32);
                nd.next = x.map_or(0, |v| v as u32);
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "setlink", |cx| {
        let top = cx.arg_count();
        let head = with_engine(|e| {
            let s = &mut e.lua_nodes;
            let (mut head, mut tail) = (0u32, 0u32);
            for idx in 1..=top {
                if arg_is_num(cx, idx) {
                    let c = arg_vint(cx, idx) as u32;
                    if c != tail {
                        if tail != 0 {
                            s.node_mut(tail).next = c;
                            s.node_mut(c).prev = tail;
                        } else if idx > 1 {
                            s.node_mut(c).prev = 0;
                        }
                        tail = c;
                        if head == 0 {
                            head = tail;
                        }
                    }
                } else if tail != 0 {
                    s.node_mut(tail).next = 0;
                }
            }
            head
        })
        .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, head)
    });
    cnat!(lua, n, "setsplit", |cx| {
        let l = handle_arg(cx, 1)?;
        let r = handle_arg(cx, 2)?;
        with_engine(|e| {
            if l == 0 || r == 0 {
                return;
            }
            let s = &mut e.lua_nodes;
            if l != r {
                let ln = s.next(l);
                if ln != 0 {
                    s.node_mut(ln).prev = 0;
                }
                let rp = s.prev(r);
                if rp != 0 {
                    s.node_mut(rp).next = 0;
                }
            }
            s.node_mut(l).next = 0;
            s.node_mut(r).prev = 0;
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });

    // ---- creation, release, copies ----
    cnat!(lua, n, "new", |cx| {
        let idv: u8 = if let Some(i) = cx.arg_integer(1) {
            if (0..=255).contains(&i) && type_info(i as u8).is_some() {
                i as u8
            } else {
                return Err(cx.raw_error("invalid node id for creating new node"));
            }
        } else if let Some(Some(id)) = cx.arg_bytes(1, |b| type_by_name(b).map(|t| t.id)) {
            id
        } else {
            return Err(cx.raw_error("invalid node id for creating new node"));
        };
        let subv: u16 = if idv == WHATSIT {
            let r = if let Some(i) = cx.arg_integer(2) {
                if (0..=255).contains(&i) {
                    whatsit_info(i as u16).map(|w| u16::from(w.id))
                } else {
                    None
                }
            } else {
                cx.arg_bytes(2, whatsit_by_name).flatten()
            };
            match r {
                Some(v) => v,
                None => return Err(cx.raw_error("creating a whatsit requires the subtype number as a second argument")),
            }
        } else if let Some(i) = cx.arg_integer(2) {
            i as u16
        } else if let Some(sub) = cx.arg_bytes(2, |b| {
            type_info(idv)
                .and_then(|t| t.subtypes.iter().find(|(_, n)| n.as_bytes() == b).map(|(i, _)| *i))
                .unwrap_or(0)
        }) {
            sub
        } else {
            0
        };
        let h = with_engine(|e| i64::from(e.lua_new_node(idv, subv))).map_err(|msg| cx.raw_error(msg))?;
        cx.push_int(h)
    });
    cnat!(lua, n, "free", |cx| {
        let h = handle_arg(cx, 1)?;
        let nx = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return 0;
            }
            let nx = e.lua_nodes.next(h);
            e.lua_flush_node(h);
            nx
        })
        .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, nx)
    });
    cnat!(lua, n, "flush_node", |cx| {
        let h = handle_arg(cx, 1)?;
        with_engine(|e| {
            if e.lua_nodes.valid(h) {
                e.lua_flush_node(h);
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "flush_list", |cx| {
        let mut h = handle_arg(cx, 1)?;
        with_engine(|e| {
            while e.lua_nodes.valid(h) {
                let nx = e.lua_nodes.next(h);
                e.lua_flush_node(h);
                h = nx;
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "copy", |cx| {
        let h = handle_arg(cx, 1)?;
        let c = with_engine(|e| if e.lua_nodes.valid(h) { Some(i64::from(e.lua_copy_node(h))) } else { None })
            .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(c)
    });
    cnat!(lua, n, "copy_list", |cx| {
        let h = handle_arg(cx, 1)?;
        let stop = handle_arg(cx, 2)?;
        let c = with_engine(|e| if e.lua_nodes.valid(h) { e.lua_copy_range(h, stop) } else { 0 })
            .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, c)
    });
    cnat!(lua, n, "remove", |cx| {
        let head = handle_arg(cx, 1)?;
        let cur = handle_arg(cx, 2)?;
        let (h, c) = with_engine(|e| {
            if head != 0 && !e.lua_nodes.valid(head) {
                return Ok((0, 0));
            }
            e.lua_nodes.remove(head, cur, true)
        })
        .and_then(|r| r)
        .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, h)?;
        push_opt_node(cx, c)?;
        Ok(2)
    });
    cnat!(lua, n, "insert_before", |cx| {
        let head = handle_arg(cx, 1)?;
        let cur = handle_arg(cx, 2)?;
        let nn = handle_arg(cx, 3)?;
        let (h, n) = with_engine(|e| {
            if nn == 0 {
                return Ok((head, cur));
            }
            e.lua_nodes.insert_before(head, cur, nn)
        })
        .and_then(|r| r)
        .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, h)?;
        push_opt_node(cx, n)?;
        Ok(2)
    });
    cnat!(lua, n, "insert_after", |cx| {
        let head = handle_arg(cx, 1)?;
        let cur = handle_arg(cx, 2)?;
        let nn = handle_arg(cx, 3)?;
        let (h, n) = with_engine(|e| {
            if nn == 0 {
                return (head, cur);
            }
            e.lua_nodes.insert_after(head, cur, nn)
        })
        .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, h)?;
        push_opt_node(cx, n)?;
        Ok(2)
    });
    cnat!(lua, n, "slide", |cx| {
        let h = handle_arg(cx, 1)?;
        let r = with_engine(|e| if e.lua_nodes.valid(h) { Some(i64::from(e.lua_nodes.slide(h))) } else { None })
            .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(r)
    });
    cnat!(lua, n, "tail", |cx| {
        let h = handle_arg(cx, 1)?;
        let r = with_engine(|e| if e.lua_nodes.valid(h) { Some(i64::from(e.lua_nodes.tail_of(h))) } else { None })
            .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(r)
    });
    cnat!(lua, n, "end_of_math", |cx| {
        let h = handle_arg(cx, 1)?;
        let m = with_engine(|e| if e.lua_nodes.valid(h) { e.lua_nodes.end_of_math(h) } else { 0 })
            .map_err(|msg| cx.raw_error(msg))?;
        if m != 0 { cx.push_int(i64::from(m)) } else { Ok(0) }
    });
    cnat!(lua, n, "length", |cx| {
        let h = handle_arg(cx, 1)?;
        let stop = handle_arg(cx, 2)?;
        let c = with_engine(|e| if e.lua_nodes.valid(h) { e.lua_nodes.count(-1, h, stop) } else { 0 })
            .map_err(|msg| cx.raw_error(msg))?;
        cx.push_int(c)
    });
    cnat!(lua, n, "count", |cx| {
        let id = opt_int_arg(cx, 1)?.unwrap_or(0);
        let h = handle_arg(cx, 2)?;
        let stop = handle_arg(cx, 3)?;
        let c = with_engine(|e| if e.lua_nodes.valid(h) { e.lua_nodes.count(id, h, stop) } else { 0 })
            .map_err(|msg| cx.raw_error(msg))?;
        cx.push_int(c)
    });

    // ---- mark nodes: `mark` is a Lua table of {cmd, chr, cs} (lnodelib.c),
    // see `getfield` ----
    cnat!(lua, n, "is_mark", |cx| {
        let h = handle_arg(cx, 1)?;
        cx.push_bool(is_mark(h))
    });
    // `kinds` holds quadruples: (0, packed token, 0, 0), (1, cmd, chr, 0) for a
    // character token, (2, byte, 0, 0) for one byte of a string
    nat!(lua, n, "mark_set", |h: Option<i64>, quads: LuaTable| -> Result<(), String> {
        let quads: Vec<i64> = quads.sequence_values().map_err(|e| format!("{e:?}"))?;
        with_engine(|e| {
            let h = handle32(h);
            if !e.lua_nodes.valid(h) || e.lua_nodes.id(h) != MARK {
                return;
            }
            let mut toks = Vec::new();
            for q in quads.chunks(4) {
                match q {
                    [0, packed, ..] => {
                        if let Ok(v) = u32::try_from(*packed) {
                            toks.push(crate::token::Token(v));
                        }
                    }
                    [1, cmd, chr, ..] if (1..=12).contains(cmd) && *cmd != 5 => {
                        toks.push(crate::token::Token::unicode_char(*cmd as u8, *chr as u32));
                    }
                    [2, byte, ..] => toks.extend(Engine::lua_str_toks(&[*byte as u8])),
                    _ => {}
                }
            }
            e.lua_nodes.node_mut(h).ext.get_or_insert_with(Default::default).toks = toks;
        })
    });
    // ---- fields ----
    cnat!(lua, n, "getfield", |cx| getfield(cx, true));
    cnat!(lua, n, "getfield_ud", |cx| getfield(cx, false));
    cnat!(lua, n, "setfield", |cx| set_field_cb(cx, true));
    cnat!(lua, n, "setfield_ud", |cx| set_field_cb(cx, false));
    cnat!(lua, n, "has_field", |cx| {
        let h = handle_arg(cx, 1)?;
        let check = |k: &str| {
            with_engine(|e| {
                if !e.lua_nodes.valid(h) {
                    return false;
                }
                let (id, sub) = (e.lua_nodes.id(h), e.lua_nodes.subtype(h));
                match k {
                    "next" | "id" => true,
                    "subtype" => id != ATTRIBUTE && id != GLUE_SPEC && id != ATTRIBUTE_LIST,
                    "attr" => has_attr_type(id, sub),
                    "prev" => id != ATTRIBUTE && id != GLUE_SPEC && id != ATTRIBUTE_LIST,
                    other => {
                        let k = if other == "list" { "head" } else { other };
                        fields_of(id, sub).iter().any(|(f, _)| *f == k)
                    }
                }
            })
        };
        let res = if cx.arg_is_nil(2) {
            false
        } else if let Some(Some(r)) = cx.arg_bytes(2, |b| std::str::from_utf8(b).ok().map(check)) {
            r.map_err(|msg| cx.raw_error(msg))?
        } else {
            match cx.arg::<Option<String>>(2)? {
                Some(k) => check(&k).map_err(|msg| cx.raw_error(msg))?,
                None => false,
            }
        };
        cx.push_bool(res)
    });

    // ---- attributes ----
    cnat!(lua, n, "has_attribute", |cx| {
        let h = handle_arg(cx, 1)?;
        let id = opt_int_arg(cx, 2)?.unwrap_or(0) as i32;
        let val = opt_int_arg(cx, 3)?.map_or(UNUSED_ATTRIBUTE, |v| v as i32);
        let v = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return None;
            }
            let v = e.lua_nodes.has_attribute(h, id, val);
            (v > UNUSED_ATTRIBUTE).then_some(i64::from(v))
        })
        .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(v)
    });
    cnat!(lua, n, "get_attribute", |cx| {
        let h = handle_arg(cx, 1)?;
        let i = opt_int_arg(cx, 2)?.unwrap_or(0) as i32;
        let v = with_engine(|e| {
            if !e.lua_nodes.valid(h) || !has_attr_type(e.lua_nodes.id(h), e.lua_nodes.subtype(h)) {
                return None;
            }
            let mut p = e.lua_nodes.attr_first(e.lua_nodes.node(h).attr);
            while p != 0 {
                let a = e.lua_nodes.node(p);
                if a.f[0] == i {
                    return (a.f[1] != UNUSED_ATTRIBUTE).then_some(i64::from(a.f[1]));
                } else if a.f[0] > i {
                    return None;
                }
                p = a.next;
            }
            None
        })
        .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(v)
    });
    cnat!(lua, n, "find_attribute", |cx| {
        let mut c = handle_arg(cx, 1)?;
        let i = opt_int_arg(cx, 2)?.unwrap_or(0) as i32;
        let found = with_engine(|e| {
            while e.lua_nodes.valid(c) {
                if has_attr_type(e.lua_nodes.id(c), e.lua_nodes.subtype(c)) {
                    let mut p = e.lua_nodes.attr_first(e.lua_nodes.node(c).attr);
                    while p != 0 {
                        let a = e.lua_nodes.node(p);
                        if a.f[0] == i {
                            if a.f[1] != UNUSED_ATTRIBUTE {
                                return Some((i64::from(a.f[1]), i64::from(c)));
                            }
                            break;
                        } else if a.f[0] > i {
                            break;
                        }
                        p = a.next;
                    }
                }
                c = e.lua_nodes.next(c);
            }
            None
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match found {
            Some((v, node)) => {
                cx.push_int(v)?;
                cx.push_int(node)?;
                Ok(2)
            }
            None => Ok(0),
        }
    });
    cnat!(lua, n, "set_attribute", |cx| {
        let top = cx.arg_count();
        let h = if top >= 1 { arg_vint(cx, 1) as u32 } else { 0 };
        if h == 0 {
            return Ok(0);
        }
        if top != 3 {
            return Err(cx.raw_error("incorrect number of arguments"));
        }
        let (i, val) = (arg_vint(cx, 2) as i32, arg_vint(cx, 3) as i32);
        with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return;
            }
            if val == UNUSED_ATTRIBUTE {
                e.lua_nodes.unset_attribute(h, i, val);
            } else {
                e.lua_nodes.set_attribute(h, i, val);
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "unset_attribute", |cx| {
        let h = handle_arg(cx, 1)?;
        let id = match cx.arg_number(2) {
            Some(n) => n,
            None => cx.arg::<f64>(2)?,
        };
        let val = opt_num_arg(cx, 3)?;
        let r = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return None;
            }
            let r = e.lua_nodes.unset_attribute(h, id as i32, val.map_or(UNUSED_ATTRIBUTE, |v| v as i32));
            (r > UNUSED_ATTRIBUTE).then_some(i64::from(r))
        })
        .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(r)
    });
    cnat!(lua, n, "current_attr", |cx| {
        let l = with_engine(|e| e.lua_current_attr_handle()).map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, l)
    });
    cnat!(lua, n, "getattributelist", |cx| {
        let h = handle_arg(cx, 1)?;
        let a = with_engine(|e| {
            if !e.lua_nodes.valid(h) || !has_attr_type(e.lua_nodes.id(h), e.lua_nodes.subtype(h)) {
                return 0;
            }
            e.lua_nodes.node(h).attr
        })
        .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, a)
    });
    cnat!(lua, n, "setattributelist", |cx| {
        let h = handle_arg(cx, 1)?;
        let kind = if let Some(i) = cx.arg_integer(2) {
            Some(i as u32)
        } else if cx.arg_bool(2) == Some(true) {
            Some(u32::MAX)
        } else {
            None
        };
        with_engine(|e| {
            if !e.lua_nodes.valid(h) || !has_attr_type(e.lua_nodes.id(h), e.lua_nodes.subtype(h)) {
                return;
            }
            let new = match kind {
                Some(u32::MAX) => e.lua_current_attr_handle(),
                Some(a) if e.lua_nodes.valid(a) => {
                    if e.lua_nodes.id(a) == ATTRIBUTE_LIST {
                        a
                    } else if has_attr_type(e.lua_nodes.id(a), e.lua_nodes.subtype(a)) {
                        e.lua_nodes.node(a).attr
                    } else {
                        0
                    }
                }
                _ => 0,
            };
            e.lua_nodes.reassign_attr(h, new);
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });

    // ---- scalar accessors ----
    install_accessors(lua, &n)?;
    install_lists(lua, &n)?;
    crate::lua_node_iter::install(lua, &n)?;
    Ok(n)
}

/// `getfield(n, key)` of `node.direct` (`direct`) and `node`: a number key
/// reads an attribute, a string key a field; a mark node's `mark` is a table
/// of {cmd, chr, cs} triples.
fn getfield(cx: &mut CallbackLua<'_>, direct: bool) -> LuaResult<usize> {
    let h = if direct {
        cx.arg_tointeger(1).unwrap_or(0) as u32
    } else {
        cx.arg_userdata_ref::<NodeUd, _>(1, |n| n.h).unwrap_or(0)
    };
    match cx.arg_kind(2) {
        Some(LuaValueKind::Integer | LuaValueKind::Float) => {
            let i = cx.arg_tointeger(2).unwrap_or(0) as i32;
            let v = with_engine(|e| {
                let s = &e.lua_nodes;
                if !s.valid(h) || !has_attr_type(s.id(h), s.subtype(h)) {
                    return None;
                }
                let v = s.has_attribute(h, i, UNUSED_ATTRIBUTE);
                (v > UNUSED_ATTRIBUTE).then_some(i64::from(v))
            })
            .map_err(|msg| cx.raw_error(msg))?;
            cx.push_opt_int(v)
        }
        Some(LuaValueKind::String) => {
            let field = cx
                .arg_bytes(2, |name| {
                    with_engine(|e| {
                        let Ok(name) = std::str::from_utf8(name) else { return Ok(Val::Nil) };
                        if name == "mark" && e.lua_nodes.valid(h) && e.lua_nodes.id(h) == MARK {
                            return Err(e.lua_mark_triples(h).unwrap_or_default());
                        }
                        // node 0 is the zero glue spec of LuaTeX's memory
                        if direct && h == 0 && name == "id" {
                            return Ok(Val::Int(i64::from(GLUE_SPEC)));
                        }
                        if direct && name == "subtype" && e.lua_nodes.valid(h) && e.lua_nodes.id(h) == GLUE_SPEC {
                            return Ok(Val::Int(0));
                        }
                        Ok(e.lua_node_field(h, name))
                    })
                })
                .unwrap_or(Ok(Ok(Val::Nil)));
            match field {
                Ok(Ok(v)) => push_val(cx, v, direct),
                Ok(Err(triples)) => {
                    let t = cx.create_table_with_capacity(triples.len(), 0)?;
                    for (i, triple) in triples.into_iter().enumerate() {
                        let entry = cx.create_table_with_capacity(3, 0)?;
                        for (j, x) in triple.into_iter().enumerate() {
                            entry.raw_seti(j as i64 + 1, x)?;
                        }
                        t.raw_seti(i as i64 + 1, entry)?;
                    }
                    cx.push(t)
                }
                Err(msg) => Err(cx.raw_error(msg)),
            }
        }
        _ => cx.push_nil(),
    }
}

fn set_field_cb(cx: &mut CallbackLua<'_>, direct: bool) -> LuaResult<usize> {
    let top = cx.arg_count();
    let h = if top == 0 {
        0
    } else if direct {
        arg_vint(cx, 1) as u32
    } else {
        cx.arg_userdata_ref::<NodeUd, _>(1, |u| u.h).unwrap_or(0)
    };
    if top < 2 {
        return Ok(0);
    }
    if let Some(i) = cx.arg_integer(2) {
        if top != 3 {
            return Err(cx.raw_error("incorrect number of arguments"));
        }
        let val = arg_vint(cx, 3) as i32;
        with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return;
            }
            if val == UNUSED_ATTRIBUTE {
                e.lua_nodes.unset_attribute(h, i as i32, val);
            } else {
                e.lua_nodes.set_attribute(h, i as i32, val);
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        return Ok(0);
    }
    if cx.arg_kind(2) != Some(LuaValueKind::String) {
        return Ok(0);
    }
    let v = if top < 3 || cx.arg_is_nil(3) {
        SetVal::Nil
    } else if let Some(i) = cx.arg_integer(3) {
        SetVal::Int(i)
    } else if let Some(n) = cx.arg_number(3) {
        SetVal::Num(n)
    } else if let Some(b) = cx.arg_bytes(3, |b| b.to_vec()) {
        SetVal::Bytes(b)
    } else if !direct {
        match cx.arg_userdata_ref::<NodeUd, _>(3, |u| u.h) {
            Some(nh) if nh != 0 => SetVal::Node(nh),
            _ => SetVal::Other,
        }
    } else {
        SetVal::Other
    };
    let res = cx.arg_bytes(2, |b| {
        std::str::from_utf8(b)
            .ok()
            .map(|name| with_engine(|e| e.lua_set_node_field(h, name, v, direct)).and_then(|r| r))
    });
    if let Some(Some(Err(msg))) = res {
        return Err(cx.raw_error(msg));
    }
    Ok(0)
}

fn install_accessors(lua: &mut Lua, n: &LuaTable) -> Result<(), String> {
    macro_rules! get_set {
        ($get:literal, $set:literal, |$s:ident, $h:ident| $getter:expr, |$s2:ident, $h2:ident, $v:ident| $setter:expr) => {
            cnat!(lua, n, $get, |cx| {
                let $h = handle_arg(cx, 1)?;
                let res = with_engine(|e| {
                    let $s = &e.lua_nodes;
                    if !$s.valid($h) {
                        return None;
                    }
                    $getter
                })
                .map_err(|msg| cx.raw_error(msg))?;
                cx.push_opt_int(res)
            });
            cnat!(lua, n, $set, |cx| {
                let $h2 = handle_arg(cx, 1)?;
                let $v: Option<f64> = opt_num_arg(cx, 2)?;
                with_engine(|e| {
                    let $s2 = &mut e.lua_nodes;
                    if !$s2.valid($h2) {
                        return;
                    }
                    $setter
                })
                .map_err(|msg| cx.raw_error(msg))?;
                Ok(0)
            });
        };
    }

    get_set!("getchar", "setchar",
        |s, h| match s.id(h) {
            GLYPH | MATH_CHAR | MATH_TEXT_CHAR => Some(i64::from(s.node(h).f[if s.id(h) == GLYPH { 0 } else { 1 }])),
            DELIM => Some(i64::from(s.node(h).f[1])),
            _ => None,
        },
        |s, h, v| {
            let Some(v) = v else { return };
            let id = s.id(h);
            let slot = match id {
                GLYPH => 0,
                MATH_CHAR | MATH_TEXT_CHAR | DELIM => 1,
                _ => return,
            };
            s.node_mut(h).f[slot] = v as i32;
        });
    get_set!("getlang", "setlang",
        |s, h| (s.id(h) == GLYPH).then(|| i64::from(s.node(h).f[2])),
        |s, h, v| if let (GLYPH, Some(v)) = (s.id(h), v) { s.node_mut(h).f[2] = v as i32 });
    get_set!("getexpansion", "setexpansion",
        |s, h| match s.id(h) {
            GLYPH => Some(i64::from(s.node(h).f[12])),
            KERN => Some(i64::from(s.node(h).f[1])),
            _ => None,
        },
        |s, h, v| {
            let e = v.map_or(0, |v| v as i32);
            match s.id(h) {
                GLYPH => s.node_mut(h).f[12] = e,
                KERN => s.node_mut(h).f[1] = e,
                _ => {}
            }
        });
    get_set!("getpenalty", "setpenalty",
        |s, h| match s.id(h) {
            PENALTY => Some(i64::from(s.node(h).f[0])),
            DISC => Some(i64::from(s.node(h).f[3])),
            _ => None,
        },
        |s, h, v| match s.id(h) {
            PENALTY => s.node_mut(h).f[0] = v.map_or(0, |v| v as i32),
            DISC => {
                if let Some(v) = v {
                    s.node_mut(h).f[3] = v as i32;
                }
            }
            _ => {}
        });
    get_set!("getnucleus", "setnucleus",
        |s, h| matches!(s.id(h), NOAD | ACCENT | RADICAL).then(|| i64::from(s.node(h).f[0])).filter(|x| *x != 0),
        |s, h, v| if matches!(s.id(h), NOAD | ACCENT | RADICAL) { s.node_mut(h).f[0] = v.map_or(0, |v| v as i32) });
    get_set!("getsub", "setsub",
        |s, h| matches!(s.id(h), NOAD | ACCENT | RADICAL).then(|| i64::from(s.node(h).f[1])).filter(|x| *x != 0),
        |s, h, v| if matches!(s.id(h), NOAD | ACCENT | RADICAL) { s.node_mut(h).f[1] = v.map_or(0, |v| v as i32) });
    get_set!("getsup", "setsup",
        |s, h| matches!(s.id(h), NOAD | ACCENT | RADICAL).then(|| i64::from(s.node(h).f[2])).filter(|x| *x != 0),
        |s, h, v| if matches!(s.id(h), NOAD | ACCENT | RADICAL) { s.node_mut(h).f[2] = v.map_or(0, |v| v as i32) });
    get_set!("getshift", "setshift",
        |s, h| matches!(s.id(h), HLIST | VLIST).then(|| i64::from(s.node(h).f[4])),
        |s, h, v| if matches!(s.id(h), HLIST | VLIST) { s.node_mut(h).f[4] = v.map_or(0, |v| (v + 0.5).floor() as i32) });
    get_set!("getleader", "setleader",
        |s, h| (s.id(h) == GLUE).then(|| i64::from(s.node(h).f[0])).filter(|x| *x != 0),
        |s, h, v| if s.id(h) == GLUE { s.node_mut(h).f[0] = v.map_or(0, |v| v as i32) });
    get_set!("getcomponents", "setcomponents",
        |s, h| (s.id(h) == GLYPH).then(|| i64::from(s.node(h).f[6])).filter(|x| *x != 0),
        |s, h, v| if s.id(h) == GLYPH { s.node_mut(h).f[6] = v.map_or(0, |v| v as i32) });

    // font and family
    cnat!(lua, n, "getfont", |cx| {
        let h = handle_arg(cx, 1)?;
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return None;
            }
            let node = e.lua_nodes.node(h);
            match node.id {
                GLYPH => Some(i64::from(node.f[1])),
                MATH_CHAR | MATH_TEXT_CHAR => Some(e.lua_family_font(node.f[0], 0)),
                DELIM => Some(e.lua_family_font(node.f[0], 0)),
                _ => None,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(res)
    });
    cnat!(lua, n, "setfont", |cx| {
        let h = handle_arg(cx, 1)?;
        let f = opt_int_arg(cx, 2)?;
        let c = opt_int_arg(cx, 3)?;
        with_engine(|e| {
            if e.lua_nodes.valid(h) && e.lua_nodes.id(h) == GLYPH {
                let nd = e.lua_nodes.node_mut(h);
                nd.f[1] = f.unwrap_or(0) as i32;
                if let Some(c) = c {
                    nd.f[0] = c as i32;
                }
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "getfam", |cx| {
        let h = handle_arg(cx, 1)?;
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return None;
            }
            let node = e.lua_nodes.node(h);
            match node.id {
                MATH_CHAR | MATH_TEXT_CHAR => Some(i64::from(node.f[0])),
                DELIM => Some(i64::from(node.f[0])),
                FRACTION => Some(i64::from(node.f[6])),
                NOAD => Some(i64::from(node.f[3])),
                _ => None,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(res)
    });
    cnat!(lua, n, "setfam", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = opt_int_arg(cx, 2)?;
        with_engine(|e| {
            let Some(v) = v else { return };
            if !e.lua_nodes.valid(h) {
                return;
            }
            let nd = e.lua_nodes.node_mut(h);
            match nd.id {
                MATH_CHAR | MATH_TEXT_CHAR | DELIM => nd.f[0] = v as i32,
                FRACTION => nd.f[6] = v as i32,
                NOAD => nd.f[3] = v as i32,
                _ => {}
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });

    // kern
    cnat!(lua, n, "getkern", |cx| {
        let h = handle_arg(cx, 1)?;
        // the C code tests `lua_toboolean(L, 2)` after pushing its result, so
        // without a second argument (or nil) it sees the pushed number: always true
        let want_exp = cx.arg_is_nil(2) || cx.arg_is_truthy(2);
        enum KernRet {
            Nil,
            Int(i64),
            Kern(f64, Option<i64>),
        }
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return KernRet::Nil;
            }
            let node = e.lua_nodes.node(h);
            match node.id {
                KERN => KernRet::Kern(f64::from(node.f[0]), want_exp.then(|| i64::from(node.f[1]))),
                MARGIN_KERN | MATH => KernRet::Int(i64::from(node.f[0])),
                _ => KernRet::Nil,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            KernRet::Nil => cx.push_nil(),
            KernRet::Int(v) => cx.push_int(v),
            KernRet::Kern(k, Some(e)) => {
                cx.push_float(k)?;
                cx.push_int(e)?;
                Ok(2)
            }
            KernRet::Kern(k, None) => cx.push_float(k),
        }
    });
    cnat!(lua, n, "setkern", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = opt_num_arg(cx, 2)?;
        let sub = opt_int_arg(cx, 3)?;
        with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return;
            }
            let nd = e.lua_nodes.node_mut(h);
            match nd.id {
                KERN | MARGIN_KERN => {
                    nd.f[0] = rnd(v) as i32;
                    if let Some(s) = sub {
                        nd.subtype = s as u16;
                    }
                }
                MATH => nd.f[0] = rnd(v) as i32,
                _ => {}
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });

    // width / height / depth
    cnat!(lua, n, "getwidth", |cx| {
        let h = handle_arg(cx, 1)?;
        let want_exp = cx.arg_is_nil(2) || cx.arg_is_truthy(2);
        enum WidthRet {
            Nil,
            Int(i64),
            IntExp(i64, i64),
            Float(f64),
            FloatExp(f64, i64),
        }
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return WidthRet::Nil;
            }
            let node = e.lua_nodes.node(h);
            match node.id {
                HLIST | VLIST | RULE | UNSET | GLUE_SPEC | MARGIN_KERN => WidthRet::Int(i64::from(node.f[0])),
                GLYPH => {
                    let (w, _, _) = glyph_dimensions(&e.eqtb.fonts, node.f[1], node.f[0]);
                    if want_exp {
                        WidthRet::FloatExp(f64::from(w), i64::from(node.f[12]))
                    } else {
                        WidthRet::Float(f64::from(w))
                    }
                }
                GLUE | MATH => WidthRet::Int(i64::from(node.f[1])),
                INS => WidthRet::Int(i64::from(node.f[INS_GLUE])),
                KERN => {
                    if want_exp {
                        WidthRet::IntExp(i64::from(node.f[0]), i64::from(node.f[1]))
                    } else {
                        WidthRet::Int(i64::from(node.f[0]))
                    }
                }
                _ => WidthRet::Nil,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            WidthRet::Nil => cx.push_nil(),
            WidthRet::Int(w) => cx.push_int(w),
            WidthRet::IntExp(w, e) => {
                cx.push_int(w)?;
                cx.push_int(e)?;
                Ok(2)
            }
            WidthRet::Float(w) => cx.push_float(w),
            WidthRet::FloatExp(w, e) => {
                cx.push_float(w)?;
                cx.push_int(e)?;
                Ok(2)
            }
        }
    });
    cnat!(lua, n, "setwidth", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = opt_num_arg(cx, 2)?;
        with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return;
            }
            let nd = e.lua_nodes.node_mut(h);
            let slot = match nd.id {
                HLIST | VLIST | RULE | UNSET | GLUE_SPEC | MARGIN_KERN | FRACTION | KERN => 0,
                INS => INS_GLUE,
                GLUE | MATH => 1,
                RADICAL => 5,
                _ => return,
            };
            nd.f[slot] = rnd(v) as i32;
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "getheight", |cx| {
        let h = handle_arg(cx, 1)?;
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return None;
            }
            let node = e.lua_nodes.node(h);
            match node.id {
                HLIST | VLIST | RULE | UNSET | INS | FENCE => Some(i64::from(node.f[2])),
                GLYPH => Some(i64::from(glyph_dimensions(&e.eqtb.fonts, node.f[1], node.f[0]).1)),
                _ => None,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(res)
    });
    cnat!(lua, n, "setheight", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = opt_num_arg(cx, 2)?;
        with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return;
            }
            let nd = e.lua_nodes.node_mut(h);
            match nd.id {
                HLIST | VLIST | RULE | UNSET | FENCE => nd.f[2] = rnd(v) as i32,
                _ => {}
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "getdepth", |cx| {
        let h = handle_arg(cx, 1)?;
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return None;
            }
            let node = e.lua_nodes.node(h);
            match node.id {
                HLIST | VLIST | RULE | UNSET | INS => Some(i64::from(node.f[1])),
                GLYPH => Some(i64::from(glyph_dimensions(&e.eqtb.fonts, node.f[1], node.f[0]).2)),
                FENCE => Some(i64::from(node.f[3])),
                _ => None,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(res)
    });
    cnat!(lua, n, "setdepth", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = opt_num_arg(cx, 2)?;
        with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return;
            }
            let nd = e.lua_nodes.node_mut(h);
            match nd.id {
                HLIST | VLIST | RULE | UNSET => nd.f[1] = rnd(v) as i32,
                FENCE => nd.f[3] = rnd(v) as i32,
                _ => {}
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "getwhd", |cx| {
        let h = handle_arg(cx, 1)?;
        let want_exp = cx.arg_is_truthy(2);
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return None;
            }
            let s = &e.lua_nodes;
            let t = s.node(h);
            match t.id {
                HLIST | VLIST | RULE | UNSET => Some((i64::from(t.f[0]), i64::from(t.f[2]), i64::from(t.f[1]), None)),
                GLYPH => {
                    let (w, ht, d) = glyph_dimensions(&e.eqtb.fonts, t.f[1], t.f[0]);
                    Some((i64::from(w), i64::from(ht), i64::from(d), want_exp.then(|| i64::from(t.f[12]))))
                }
                GLUE => {
                    let l = t.f[0] as u32;
                    if s.valid(l) && matches!(s.id(l), HLIST | VLIST | RULE) {
                        let x = s.node(l);
                        Some((i64::from(x.f[0]), i64::from(x.f[2]), i64::from(x.f[1]), None))
                    } else {
                        None
                    }
                }
                _ => None,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((w, ht, d, exp)) => {
                cx.push_int(w)?;
                cx.push_int(ht)?;
                cx.push_int(d)?;
                if let Some(exp) = exp {
                    cx.push_int(exp)?;
                    Ok(4)
                } else {
                    Ok(3)
                }
            }
            None => Ok(0),
        }
    });
    cnat!(lua, n, "setwhd", |cx| {
        let top = cx.arg_count();
        let h = if top >= 1 { arg_vint(cx, 1) as u32 } else { 0 };
        with_engine(|e| {
            let s = &mut e.lua_nodes;
            if !s.valid(h) {
                return;
            }
            let mut target = h;
            if s.id(target) == GLUE {
                target = s.node(target).f[0] as u32;
                if !s.valid(target) {
                    return;
                }
            }
            if !matches!(s.id(target), HLIST | VLIST | RULE | UNSET) {
                return;
            }
            for (idx, slot) in [(2usize, 0usize), (3, 2), (4, 1)] {
                if top >= idx && arg_is_num(cx, idx) {
                    s.node_mut(target).f[slot] = arg_vround(cx, idx) as i32;
                }
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });

    // glue
    cnat!(lua, n, "getglue", |cx| {
        let h = handle_arg(cx, 1)?;
        enum GlueRet {
            Empty,
            Five([i64; 5]),
            Box(f64, i64, i64),
        }
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return GlueRet::Empty;
            }
            let t = e.lua_nodes.node(h);
            let five = |first: usize| {
                GlueRet::Five([
                    i64::from(t.f[first]),
                    i64::from(t.f[first + 1]),
                    i64::from(t.f[first + 2]),
                    i64::from(t.f[first + 3]),
                    i64::from(t.f[first + 4]),
                ])
            };
            match t.id {
                // an insert's glue fields are not backed by data (zeros)
                INS => five(INS_GLUE),
                GLUE | MATH => five(1),
                GLUE_SPEC => five(0),
                HLIST | VLIST => GlueRet::Box(t.fl, i64::from(t.f[5]), i64::from(t.f[6])),
                _ => GlueRet::Empty,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            GlueRet::Empty => Ok(0),
            GlueRet::Five(a) => {
                for v in a {
                    cx.push_int(v)?;
                }
                Ok(5)
            }
            GlueRet::Box(fl, o, s) => {
                cx.push_float(fl)?;
                cx.push_int(o)?;
                cx.push_int(s)?;
                Ok(3)
            }
        }
    });
    cnat!(lua, n, "setglue", |cx| {
        let top = cx.arg_count();
        let h = if top >= 1 { arg_vint(cx, 1) as u32 } else { 0 };
        with_engine(|e| {
            let s = &mut e.lua_nodes;
            if !s.valid(h) {
                return;
            }
            let t = s.id(h);
            let num = |idx: usize| top >= idx && arg_is_num(cx, idx);
            match t {
                GLUE | GLUE_SPEC | MATH => {
                    let base = if t == GLUE_SPEC { 0 } else { 1 };
                    let nd = s.node_mut(h);
                    nd.f[base] = if num(2) { arg_vround(cx, 2) as i32 } else { 0 };
                    nd.f[base + 1] = if num(3) { arg_vround(cx, 3) as i32 } else { 0 };
                    nd.f[base + 2] = if num(4) { arg_vround(cx, 4) as i32 } else { 0 };
                    nd.f[base + 3] = if num(5) { arg_vint(cx, 5) as i32 } else { 0 };
                    nd.f[base + 4] = if num(6) { arg_vint(cx, 6) as i32 } else { 0 };
                }
                HLIST | VLIST => {
                    let nd = s.node_mut(h);
                    nd.fl = if num(2) { arg_vnum(cx, 2) } else { 0.0 };
                    nd.f[5] = if num(3) { arg_vint(cx, 3) as i32 } else { 0 };
                    nd.f[6] = if num(4) { arg_vint(cx, 4) as i32 } else { 0 };
                }
                _ => {}
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "is_zero_glue", |cx| {
        let h = handle_arg(cx, 1)?;
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return None;
            }
            let t = e.lua_nodes.node(h);
            match t.id {
                GLUE | MATH => Some(t.f[1] == 0 && t.f[2] == 0 && t.f[3] == 0),
                GLUE_SPEC => Some(t.f[0] == 0 && t.f[1] == 0 && t.f[2] == 0),
                INS => Some(t.f[INS_GLUE] == 0 && t.f[INS_GLUE + 1] == 0 && t.f[INS_GLUE + 2] == 0),
                HLIST | VLIST => Some(t.fl == 0.0 && t.f[5] == 0 && t.f[6] == 0),
                _ => None,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some(b) => cx.push_bool(b),
            None => Ok(0),
        }
    });
    cnat!(lua, n, "effective_glue", |cx| {
        let g = handle_arg(cx, 1)?;
        let p = handle_arg(cx, 2)?;
        let round = cx.arg_is_truthy(3);
        enum EffRet {
            Nil,
            Int(i64),
            Float(f64),
        }
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(g) || e.lua_nodes.id(g) != GLUE {
                return EffRet::Nil;
            }
            let pv = e.lua_nodes.valid(p) && matches!(e.lua_nodes.id(p), HLIST | VLIST);
            if !pv {
                return EffRet::Int(i64::from(e.lua_nodes.node(g).f[1]));
            }
            match e.lua_nodes.effective_glue(g, p) {
                Some(w) if round => EffRet::Int(w.round() as i64),
                Some(w) => EffRet::Float(w),
                None => EffRet::Nil,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            EffRet::Nil => cx.push_nil(),
            EffRet::Int(v) => cx.push_int(v),
            EffRet::Float(v) => cx.push_float(v),
        }
    });

    // dir
    cnat!(lua, n, "getdir", |cx| {
        let h = handle_arg(cx, 1)?;
        enum DirRet {
            Nil,
            Dir(i32),
            Signed(u16, i32),
        }
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return DirRet::Nil;
            }
            let t = e.lua_nodes.node(h);
            match t.id {
                DIR => DirRet::Signed(t.subtype, t.f[0]),
                HLIST | VLIST | RULE => DirRet::Dir(t.f[3]),
                LOCAL_PAR => DirRet::Dir(t.f[2]),
                _ => DirRet::Nil,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            DirRet::Nil => cx.push_nil(),
            DirRet::Dir(d) => push_dir_string(cx, d),
            DirRet::Signed(sub, d) => {
                if (0..4).contains(&d) {
                    let name = DIR_NAMES[d as usize].as_bytes();
                    let buf = [if sub == 1 { b'-' } else { b'+' }, name[0], name[1], name[2]];
                    cx.push_bytes(&buf)
                } else {
                    cx.push_nil()
                }
            }
        }
    });
    cnat!(lua, n, "getdirection", |cx| {
        let h = handle_arg(cx, 1)?;
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return (None, None);
            }
            let t = e.lua_nodes.node(h);
            let d = |x: i32| (x >= 0).then(|| i64::from(x));
            match t.id {
                DIR => (d(t.f[0]), Some(t.subtype != 0)),
                HLIST | VLIST | RULE => (d(t.f[3]), None),
                LOCAL_PAR => (d(t.f[2]), None),
                _ => (None, None),
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        cx.push_opt_int(res.0)?;
        if let Some(cancel) = res.1 {
            cx.push_bool(cancel)?;
            Ok(2)
        } else {
            Ok(1)
        }
    });
    cnat!(lua, n, "setdir", |cx| {
        let h = handle_arg(cx, 1)?;
        let v: Option<Vec<u8>> = if cx.arg_is_nil(2) {
            None
        } else if let Some(Some(bytes)) = cx.arg_bytes(2, |b| std::str::from_utf8(b).ok().map(|s| s.as_bytes().to_vec())) {
            Some(bytes)
        } else {
            cx.arg::<Option<String>>(2)?.map(String::into_bytes)
        };
        with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return Ok(());
            }
            let id = e.lua_nodes.id(h);
            let Some(v) = v else { return Err("Direction specifiers have to be strings".to_string()) };
            match id {
                DIR | HLIST | VLIST | RULE | LOCAL_PAR => e.lua_nodes.set_field(h, "dir", SetVal::Bytes(v)),
                _ => Ok(()),
            }
        })
        .and_then(|r| r)
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "setdirection", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = cx.arg_integer(2);
        let cancel = cx.arg_bool(3);
        with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return Ok(());
            }
            if !matches!(e.lua_nodes.id(h), DIR | HLIST | VLIST | RULE | LOCAL_PAR) {
                return Ok(());
            }
            let Some(v) = v else {
                return Err("Direction specifiers have to be numbers".to_string());
            };
            if !(0..4).contains(&v) {
                return Err(format!("Invalid direction value {v}"));
            }
            let nd = e.lua_nodes.node_mut(h);
            match nd.id {
                DIR => {
                    nd.f[0] = v as i32;
                    if let Some(c) = cancel {
                        nd.subtype = u16::from(c);
                    }
                }
                HLIST | VLIST | RULE => nd.f[3] = v as i32,
                LOCAL_PAR => nd.f[2] = v as i32,
                _ => {}
            }
            Ok(())
        })
        .and_then(|r| r)
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });

    // offsets
    cnat!(lua, n, "getoffsets", |cx| {
        let h = handle_arg(cx, 1)?;
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return None;
            }
            let t = e.lua_nodes.node(h);
            match t.id {
                GLYPH => Some((i64::from(t.f[7]), i64::from(t.f[8]))),
                RULE => Some((i64::from(t.f[5]), i64::from(t.f[6]))),
                _ => None,
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((x, y)) => {
                cx.push_int(x)?;
                cx.push_int(y)?;
                Ok(2)
            }
            None => Ok(0),
        }
    });
    cnat!(lua, n, "setoffsets", |cx| {
        let top = cx.arg_count();
        let h = if top >= 1 { arg_vint(cx, 1) as u32 } else { 0 };
        with_engine(|e| {
            let s = &mut e.lua_nodes;
            if !s.valid(h) {
                return;
            }
            let base = match s.id(h) {
                GLYPH => 7,
                RULE => 5,
                _ => return,
            };
            for i in 0..2 {
                if arg_is_num(cx, 2 + i) {
                    s.node_mut(h).f[base + i] = arg_vround(cx, 2 + i) as i32;
                }
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });

    // data
    cnat!(lua, n, "getdata", |cx| {
        let h = handle_arg(cx, 1)?;
        enum DataRet {
            Val(Val),
            Literal(Vec<u8>, i64),
        }
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return DataRet::Val(Val::Nil);
            }
            let t = e.lua_nodes.node(h);
            match t.id {
                GLYPH => DataRet::Val(Val::Int(i64::from(t.f[13]))),
                BOUNDARY => DataRet::Val(Val::Int(i64::from(t.f[0]))),
                WHATSIT => match t.subtype {
                    ws::USER_DEFINED => DataRet::Val(e.lua_nodes.get_field(h, "value", &|_, _| (0, 0, 0))),
                    ws::PDF_LITERAL | ws::PDF_LATE_LITERAL => {
                        DataRet::Literal(e.lua_nodes.ext_str(h, "data"), i64::from(t.f[0]))
                    }
                    ws::LATE_LUA | ws::PDF_SETMATRIX | ws::SPECIAL | ws::LATE_SPECIAL => {
                        DataRet::Val(Val::Bytes(e.lua_nodes.ext_str(h, "data")))
                    }
                    ws::WRITE => DataRet::Val(Val::Bytes(e.lua_node_tokens(h))),
                    _ => DataRet::Val(Val::Nil),
                },
                _ => DataRet::Val(Val::Nil),
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            DataRet::Val(v) => push_val(cx, v, true),
            DataRet::Literal(b, mode) => {
                cx.push_bytes(&b)?;
                cx.push_int(mode)?;
                Ok(2)
            }
        }
    });
    cnat!(lua, n, "setdata", |cx| {
        let h = handle_arg(cx, 1)?;
        let (iv, bv) = if cx.arg_is_nil(2) {
            (0, None)
        } else {
            (arg_vround(cx, 2), cx.arg_bytes(2, |b| b.to_vec()))
        };
        with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return;
            }
            let (id, sub) = (e.lua_nodes.id(h), e.lua_nodes.subtype(h));
            match id {
                GLYPH => e.lua_nodes.node_mut(h).f[13] = iv as i32,
                BOUNDARY => e.lua_nodes.node_mut(h).f[0] = iv as i32,
                WHATSIT => {
                    let _ = e.lua_set_node_field(
                        h,
                        if sub == ws::USER_DEFINED { "value" } else { "data" },
                        match (sub, bv) {
                            (ws::USER_DEFINED, Some(b)) => SetVal::Bytes(b),
                            (ws::USER_DEFINED, None) => SetVal::Int(iv),
                            (_, Some(b)) => SetVal::Bytes(b),
                            (_, None) => SetVal::Nil,
                        },
                        true,
                    );
                }
                _ => {}
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    Ok(())
}

impl Engine {
    /// `fam_fnt(fam, size)`: the font of family `fam` in the text size
    /// (`size` = 0), script (256) or scriptscript (512) size.
    pub(crate) fn lua_family_font(&self, fam: i32, size: i32) -> i64 {
        let sz = (size / 256).clamp(0, 2) as usize;
        self.eqtb.style_fonts[sz][fam.clamp(0, 255) as usize] as i64
    }

    pub(crate) fn lua_flush_node(&mut self, n: u32) {
        self.lua_nodes.flush_node(n);
    }

    pub(crate) fn lua_copy_node(&mut self, n: u32) -> u32 {
        self.lua_nodes.copy_node(n)
    }

    pub(crate) fn lua_copy_range(&mut self, p: u32, stop: u32) -> u32 {
        let mut head = 0;
        let mut q = 0;
        let mut p = p;
        while p != stop && p != 0 {
            let s = self.lua_copy_node(p);
            if head == 0 {
                head = s;
            } else {
                self.lua_nodes.couple(q, s);
            }
            q = s;
            p = self.lua_nodes.next(p);
        }
        head
    }

}

fn install_lists(lua: &mut Lua, n: &LuaTable) -> Result<(), String> {
    // lists and discretionaries
    cnat!(lua, n, "getlist", |cx| {
        let h = handle_arg(cx, 1)?;
        let head = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return 0;
            }
            let slot = match e.lua_nodes.id(h) {
                HLIST | VLIST => sl::B_HEAD,
                SUB_BOX | SUB_MLIST | ADJUST => 0,
                INS => sl::I_HEAD,
                _ => return 0,
            };
            let head = e.lua_nodes.node(h).f[slot] as u32;
            if head != 0 {
                e.lua_nodes.node_mut(head).prev = 0;
            }
            head
        })
        .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, head)
    });
    cnat!(lua, n, "setlist", |cx| {
        let h = handle_arg(cx, 1)?;
        let v = opt_num_arg(cx, 2)?;
        with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return;
            }
            let slot = match e.lua_nodes.id(h) {
                HLIST | VLIST => sl::B_HEAD,
                SUB_BOX | SUB_MLIST | ADJUST => 0,
                INS => sl::I_HEAD,
                _ => return,
            };
            e.lua_nodes.node_mut(h).f[slot] = v.map_or(0, |v| v as i32);
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "getdisc", |cx| {
        let h = handle_arg(cx, 1)?;
        // lua_isboolean(L, 2) && lua_toboolean(L, 2)
        let tails = cx.arg_bool(2) == Some(true);
        let res = with_engine(|e| {
            let s = &mut e.lua_nodes;
            if !s.valid(h) || s.id(h) != DISC {
                return None;
            }
            let f = s.node(h).f;
            let heads = [f[0] as u32, f[1] as u32, f[2] as u32];
            for &head in &heads {
                if head != 0 {
                    s.node_mut(head).prev = 0;
                }
            }
            let tail_arr = if tails {
                Some([s.tail_of(heads[0]), s.tail_of(heads[1]), s.tail_of(heads[2])])
            } else {
                None
            };
            Some((heads, tail_arr))
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((heads, tail_arr)) => {
                for head in heads {
                    push_opt_node(cx, head)?;
                }
                if let Some(tail_arr) = tail_arr {
                    for tail in tail_arr {
                        push_opt_node(cx, tail)?;
                    }
                    Ok(6)
                } else {
                    Ok(3)
                }
            }
            None => Ok(0),
        }
    });
    cnat!(lua, n, "setdisc", |cx| {
        let top = cx.arg_count();
        let h = if top >= 1 { arg_vint(cx, 1) as u32 } else { 0 };
        with_engine(|e| {
            let s = &mut e.lua_nodes;
            if !s.valid(h) || s.id(h) != DISC {
                return;
            }
            let get = |idx: usize| if top >= idx { arg_vint(cx, idx) as i32 } else { 0 };
            let nd = s.node_mut(h);
            // LuaTeX clears missing list arguments; subtype and penalty
            // below change only when those optional arguments are present.
            nd.f[0] = get(2);
            nd.f[1] = get(3);
            nd.f[2] = get(4);
            if top >= 5 {
                nd.subtype = arg_vint(cx, 5) as u16;
            }
            if top >= 6 {
                nd.f[3] = arg_vint(cx, 6) as i32;
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "flatten_discretionaries", |cx| {
        let h = handle_arg(cx, 1)?;
        let (head, c) = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return (0, 0);
            }
            e.lua_nodes.flatten_discretionaries(h)
        })
        .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, head)?;
        cx.push_int(c)?;
        Ok(2)
    });
    cnat!(lua, n, "first_glyph", |cx| {
        let h = handle_arg(cx, 1)?;
        let t = handle_arg(cx, 2)?;
        let g = with_engine(|e| if e.lua_nodes.valid(h) { e.lua_nodes.first_glyph(h, t) } else { 0 })
            .map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, g)
    });
    cnat!(lua, n, "has_glyph", |cx| {
        let h = handle_arg(cx, 1)?;
        let g = with_engine(|e| e.lua_nodes.has_glyph(h)).map_err(|msg| cx.raw_error(msg))?;
        push_opt_node(cx, g)
    });
    cnat!(lua, n, "is_char", |cx| {
        let h = handle_arg(cx, 1)?;
        let font_arg = if arg_is_num(cx, 2) { Some(arg_vint(cx, 2)) } else { None };
        enum IsCharRet {
            NotGlyph(i64),
            False,
            Char(i64),
        }
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return IsCharRet::NotGlyph(0);
            }
            let t = e.lua_nodes.node(h);
            if t.id != GLYPH {
                return IsCharRet::NotGlyph(i64::from(t.id));
            }
            if t.subtype & 0xFF00 != 0 {
                return IsCharRet::False;
            }
            match font_arg {
                Some(f) if f == 0 || f != i64::from(t.f[1]) => IsCharRet::False,
                _ => IsCharRet::Char(i64::from(t.f[0])),
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            IsCharRet::NotGlyph(id) => {
                cx.push_nil()?;
                cx.push_int(id)?;
                Ok(2)
            }
            IsCharRet::False => cx.push_bool(false),
            IsCharRet::Char(c) => cx.push_int(c),
        }
    });
    cnat!(lua, n, "is_glyph", |cx| {
        let h = handle_arg(cx, 1)?;
        let res = with_engine(|e| {
            if !e.lua_nodes.valid(h) {
                return (None, 0);
            }
            let t = e.lua_nodes.node(h);
            if t.id != GLYPH {
                (None, i64::from(t.id))
            } else {
                (Some(i64::from(t.f[0])), i64::from(t.f[1]))
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res.0 {
            Some(c) => cx.push_int(c)?,
            None => cx.push_bool(false)?,
        };
        cx.push_int(res.1)?;
        Ok(2)
    });
    cnat!(lua, n, "uses_font", |cx| {
        let h = handle_arg(cx, 1)?;
        let f = opt_int_arg(cx, 2)?.unwrap_or(0);
        let b = with_engine(|e| e.lua_nodes.valid(h) && e.lua_nodes.uses_font(h, f))
            .map_err(|msg| cx.raw_error(msg))?;
        cx.push_bool(b)
    });
    cnat!(lua, n, "protect_glyph", |cx| {
        let h = handle_arg(cx, 1)?;
        with_engine(|e| {
            if e.lua_nodes.valid(h) {
                e.lua_nodes.protect(h, true);
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "unprotect_glyph", |cx| {
        let h = handle_arg(cx, 1)?;
        with_engine(|e| {
            if e.lua_nodes.valid(h) {
                e.lua_nodes.protect(h, false);
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "protect_glyphs", |cx| {
        let mut head = handle_arg(cx, 1)?;
        let tail = handle_arg(cx, 2)?;
        with_engine(|e| {
            while e.lua_nodes.valid(head) {
                e.lua_nodes.protect(head, true);
                if head == tail {
                    break;
                }
                head = e.lua_nodes.next(head);
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "unprotect_glyphs", |cx| {
        let mut head = handle_arg(cx, 1)?;
        let tail = handle_arg(cx, 2)?;
        with_engine(|e| {
            while e.lua_nodes.valid(head) {
                e.lua_nodes.protect(head, false);
                if head == tail {
                    break;
                }
                head = e.lua_nodes.next(head);
            }
        })
        .map_err(|msg| cx.raw_error(msg))?;
        Ok(0)
    });
    cnat!(lua, n, "protrusion_skippable", |cx| {
        let h = handle_arg(cx, 1)?;
        let res = with_engine(|e| e.lua_nodes.valid(h).then(|| e.lua_nodes.cp_skippable(h)))
            .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some(b) => cx.push_bool(b),
            None => cx.push_nil(),
        }
    });
    cnat!(lua, n, "check_discretionary", |cx| {
        let _ = opt_int_arg(cx, 1)?;
        Ok(0)
    });
    cnat!(lua, n, "check_discretionaries", |cx| {
        let _ = opt_int_arg(cx, 1)?;
        Ok(0)
    });
    nat!(lua, n, "usedlist", || -> Result<i64, String> {
        with_engine(|e| {
            // copies of all nodes in use, chained (texnodes.c list_node_mem_usage)
            let ids: Vec<u32> = (1..e.lua_nodes.nodes.len() as u32).filter(|&i| e.lua_nodes.valid(i)).collect();
            let mut head = 0;
            let mut prev = 0;
            for i in ids {
                let c = e.lua_copy_node(i);
                if prev == 0 {
                    head = c;
                } else {
                    e.lua_nodes.couple(prev, c);
                }
                prev = c;
            }
            i64::from(head)
        })
    });
    nat!(lua, n, "tostring", |h: Option<i64>| -> Result<Option<String>, String> {
        with_engine(|e| {
            let h = handle32(h);
            (e.lua_nodes.valid(h)).then(|| e.lua_nodes_tostring(h, "direct"))
        })
    });
    nat!(lua, n, "tostring_node", |h: Option<i64>| -> Result<Option<String>, String> {
        with_engine(|e| {
            let h = handle32(h);
            (e.lua_nodes.valid(h)).then(|| e.lua_nodes_tostring(h, "node"))
        })
    });
    cnat!(lua, n, "todirect_ud", |cx| {
        let h = cx.arg_userdata_ref::<NodeUd, _>(1, |u| u.h).unwrap_or(0);
        push_opt_node(cx, h)
    });
    cnat!(lua, n, "is_node_ud", |cx| {
        let h = cx.arg_userdata_ref::<NodeUd, _>(1, |u| u.h).unwrap_or(0);
        push_opt_node(cx, h)
    });
    cnat!(lua, n, "tointeger", |cx| {
        cx.push_int(cx.arg_tointeger(1).unwrap_or(0))
    });
    cnat!(lua, n, "tonode", |cx| {
        let h = handle_arg(cx, 1)?;
        if h == 0 { cx.push_nil() } else { cx.push_userdata(NodeUd { h }) }
    });
    nat!(lua, n, "fix_node_lists", |v: Option<Value>| -> Result<(), String> {
        let b = v.as_ref().and_then(|v| v.as_boolean()).or_else(|| v.as_ref().and_then(|v| v.as_integer()).map(|i| i != 0));
        with_engine(|e| {
            if let Some(b) = b {
                e.lua_nodes.fix_node_lists = b;
            }
        })
    });
    nat!(lua, n, "set_properties_mode", |a: Option<Value>, b: Option<Value>| -> Result<(), String> {
        let (a, b) = (
            a.and_then(|v| v.as_boolean()),
            b.and_then(|v| v.as_boolean()),
        );
        with_engine(|e| {
            if let Some(a) = a {
                e.lua_nodes.props_mode.0 = a;
            }
            if let Some(b) = b {
                e.lua_nodes.props_mode.1 = b;
            }
        })
    });
    nat!(lua, n, "set_properties_table", |t: LuaTable| -> Result<(), String> {
        with_engine(|e| e.lua_nodes.props = Some(t))
    });
    nat!(lua, n, "family_font", |fam: i64, size: Option<i64>| -> Result<i64, String> {
        with_engine(|e| e.lua_family_font(fam as i32, size.unwrap_or(0) as i32))
    });
    nat!(lua, n, "last_node", || -> Result<i64, String> {
        with_engine(|e| i64::from(e.lua_last_node()))
    });
    // the nodes stay Lua's handles to the list they joined (see
    // `lua_texnodes`): LuaTeX links them into the current list itself
    nat!(lua, n, "write", |h: Option<i64>| -> Result<(), String> {
        with_engine(|e| e.lua_node_write(handle32(h)))
    });
    nat!(lua, n, "prepend_prevdepth", |h: Option<i64>, prev: Option<i64>, ud: bool| -> Result<Variadic<UdValue>, String> {
        with_engine(|e| e.lua_prepend_prevdepth(handle32(h), prev.unwrap_or(0) as i32, ud))
    });
    nat!(lua, n, "set_synctex_fields", |h: Option<i64>, tag: Option<i64>, line: Option<i64>| -> Result<(), String> {
        with_engine(|e| {
            let h = handle32(h);
            if e.lua_nodes.valid(h) && has_synctex(e.lua_nodes.id(h)) {
                let f = &mut e.lua_nodes.node_mut(h).f;
                if let Some(t) = tag.filter(|t| *t != 0) {
                    f[NF - 2] = t as i32;
                }
                if let Some(l) = line.filter(|l| *l != 0) {
                    f[NF - 1] = l as i32;
                }
            }
        })
    });
    nat!(lua, n, "get_synctex_fields", |h: Option<i64>| -> Result<Variadic<UdValue>, String> {
        with_engine(|e| {
            let h = handle32(h);
            if e.lua_nodes.valid(h) && has_synctex(e.lua_nodes.id(h)) {
                let f = &e.lua_nodes.node(h).f;
                Variadic(vec![UdValue::Integer(i64::from(f[NF - 2])), UdValue::Integer(i64::from(f[NF - 1]))])
            } else {
                Variadic(vec![])
            }
        })
    });
    nat!(lua, n, "ligaturing", |h: Option<i64>, t: Option<i64>| -> Result<Variadic<UdValue>, String> {
        with_engine(|e| e.lua_ligkern_native(LigKern::Ligaturing, h, t))
    });
    nat!(lua, n, "kerning", |h: Option<i64>, t: Option<i64>| -> Result<Variadic<UdValue>, String> {
        with_engine(|e| e.lua_ligkern_native(LigKern::Kerning, h, t))
    });
    nat!(lua, n, "hyphenating", |h: Option<i64>, t: Option<i64>, ud: bool| -> Result<Variadic<UdValue>, String> {
        with_engine(|e| {
            let h = handle32(h);
            // lang_tex_direct_hyphenating always walks to the tail; the
            // userdata form honours a given one
            let t = match handle32(t) {
                t if ud && t != 0 => t,
                _ => e.lua_nodes.tail_of(h),
            };
            e.lk_hyphenation(h, t);
            Variadic(vec![
                UdValue::Integer(i64::from(h)),
                UdValue::Integer(i64::from(t)),
                UdValue::Boolean(true),
            ])
        })
    });
    nat!(lua, n, "mlist_to_hlist", |h: Option<i64>, style: Option<Value>, pen: Option<Value>| -> Result<Option<i64>, String> {
        // assign_math_style: numbers as they are, names by the math style list
        let style = match &style {
            Some(v) if v.as_integer().is_some() => value_int(v) as u8,
            Some(v) => match value_bytes(v) {
                Some(b) => math_style_from_name(&b).map_or(2, |s| s as u8),
                None => 0,
            },
            None => 0,
        };
        let pen = is_truthy(pen.as_ref());
        with_engine(|e| {
            let list = e.lua_nodes_to_engine(i64::from(handle32(h)));
            let out = e.lua_mlist_to_hlist(&list, style, pen);
            let head = e.lua_nodes_from_engine(out);
            (head != 0).then_some(head)
        })
    });
    // node.make_extensible(font, char, size [, overlap [, horizontal [, attr]]])
    nat!(lua, n, "make_extensible", |fnt: Value, chr: Value, size: Value, overlap: Option<Value>, horizontal: Option<Value>, attlist: Option<Value>| -> Result<Option<i64>, String> {
        let fnt = value_int(&fnt);
        let chr = value_int(&chr);
        let size = value_int(&size);
        let overlap = overlap.as_ref().map_or(65536, value_int);
        let horizontal = is_truthy(horizontal.as_ref());
        let attlist = attlist.as_ref().filter(|v| !v.is_nil()).map(value_int);
        with_engine(|e| {
            let att = match attlist {
                Some(h) if h > 0 && e.lua_nodes.valid(h as u32) && e.lua_nodes.id(h as u32) == crate::lua_node::ATTRIBUTE_LIST => {
                    e.engine_attr_of_list(h as u32)
                }
                _ => crate::boxes::Attr::NONE,
            };
            let b = e.make_extensible(fnt as crate::tfm::FontId, chr as u32, size as i32, overlap as i32, horizontal, att);
            let head = e.lua_nodes_from_engine(vec![b]);
            (head != 0).then_some(head)
        })
    });
    // direct.getbox / direct.setbox: a box register as a node. The node is
    // tied to the register (see `lua_texnodes`): edits reach the register
    // when the Lua call ends.
    nat!(lua, n, "getbox", |k: Value| -> Result<Option<i64>, String> {
        let k = box_register(&k, "getbox")?;
        with_engine(|e| {
            let h = e.lua_box_handle(k);
            (h != 0).then_some(i64::from(h))
        })
    });
    nat!(lua, n, "setbox", |args: Variadic<Value>| -> Result<(), String> {
        let top = args.len();
        if top < 2 {
            return Err("argument must be a string or a number".to_string());
        }
        let global = top == 3 && value_bytes(&args[0]).as_deref() == Some(b"global");
        let value = &args[top - 1];
        let k = box_register(&args[top - 2], "setbox")?;
        if let Some(b) = value.as_boolean() {
            if b {
                return Ok(());
            }
            return with_engine(|e| e.lua_set_box(k, 0, global));
        }
        if value.is_nil() {
            return with_engine(|e| e.lua_set_box(k, 0, global));
        }
        let h = handle32(value.as_integer());
        with_engine(|e| {
            if h != 0 && e.lua_nodes.valid(h) && !matches!(e.lua_nodes.id(h), HLIST | VLIST) {
                let id = e.lua_nodes.id(h);
                let name = type_info(id).map_or("unknown", |t| t.name);
                return Err(format!("setbox: incompatible node type ({name})\n"));
            }
            e.lua_set_box(k, h, global);
            Ok(())
        })?
    });
    nat!(lua, n, "wrap_hpack", |args: Variadic<Value>| -> Result<Variadic<UdValue>, String> {
        crate::lua_node_pack::lua_pack(&args, true)
    });
    nat!(lua, n, "wrap_vpack", |args: Variadic<Value>| -> Result<Variadic<UdValue>, String> {
        crate::lua_node_pack::lua_pack(&args, false)
    });
    nat!(lua, n, "dimensions", |args: Variadic<Value>| -> Result<Variadic<UdValue>, String> {
        crate::lua_node_pack::lua_dimensions(&args, false)
    });
    nat!(lua, n, "rangedimensions", |args: Variadic<Value>| -> Result<Variadic<UdValue>, String> {
        crate::lua_node_pack::lua_dimensions(&args, true)
    });
    Ok(())
}



/// Install the global `node` table: the natives of [`install`] under the
/// public names of `lua_node.lua`.
pub(crate) fn install_library(lua: &mut Lua) -> Result<(), String> {
    let natives = install(lua)?;
    let data: LuaTable = lua
        .load(include_str!("lua_node_data.lua"))
        .set_name("=[texres node data]")
        .eval()
        .map_err(|e| format!("node data: {}", lua.get_error_message(e).message()))?;
    lua.set_global("__texres_nodelib", natives).map_err(|e| format!("{e:?}"))?;
    lua.set_global("__texres_nodedata", data).map_err(|e| format!("{e:?}"))?;
    let node: LuaTable = lua
        .load(include_str!("lua_node.lua"))
        .set_name("=[texres node]")
        .eval()
        .map_err(|e| format!("node library: {}", lua.get_error_message(e).message()))?;
    lua.set_global("node", node).map_err(|e| format!("{e:?}"))
}

/// Node types that carry synctex tag and line.
fn has_synctex(id: u8) -> bool {
    matches!(id, GLYPH | GLUE | KERN | HLIST | VLIST | UNSET | RULE | MATH)
}

impl Engine {
    /// `node.prepend_prevdepth` (`ud`) / `node.direct.prepend_prevdepth`:
    /// the interline glue that goes in front of box `n`. The direct form
    /// keeps LuaTeX's inverted box test.
    fn lua_prepend_prevdepth(&mut self, n: u32, prev: i32, ud: bool) -> Variadic<UdValue> {
        use crate::prim::{DimParam, GlueParam};
        if !self.lua_nodes.valid(n) {
            return Variadic(vec![UdValue::Nil]);
        }
        let id = self.lua_nodes.id(n);
        let is_box = id == HLIST || id == VLIST;
        if is_box != ud {
            return Variadic(vec![UdValue::Nil]);
        }
        let f = self.lua_nodes.node(n).f;
        let mirrored = id == HLIST && matches!(f[sl::B_DIR], 1 | 3);
        let (height, depth) = if is_box { (f[sl::B_HEIGHT], f[sl::B_DEPTH]) } else { (0, 0) };
        let result = if prev > self.ignore_depth() {
            let bs = self.eqtb.glue_params[GlueParam::BaselineSkip.idx() as usize];
            let ls = self.eqtb.glue_params[GlueParam::LineSkip.idx() as usize];
            let limit = self.eqtb.dim_params[DimParam::LineSkipLimit.idx() as usize];
            let d = if mirrored { bs.width - prev - depth } else { bs.width - prev - height };
            let p = if d < limit {
                self.import_glue(&ls, 1)
            } else {
                let g = crate::boxes::Glue { width: d, ..bs.fresh() };
                self.import_glue(&g, 2)
            };
            self.lua_nodes.node_mut(p).next = n;
            self.lua_nodes.node_mut(n).prev = p;
            p
        } else {
            n
        };
        let new_prev = if mirrored { height } else { depth };
        let first = if ud { node_ud(result) } else { UdValue::Integer(i64::from(result)) };
        Variadic(vec![first, UdValue::Integer(i64::from(new_prev))])
    }
}

#[derive(Clone, Copy)]
enum LigKern {
    Ligaturing,
    Kerning,
}

impl Engine {
    /// `font_tex_direct_ligaturing` / `font_tex_direct_kerning`: run the
    /// pass on the range `h`..`t` (the whole list from `h` when `t` is
    /// absent) behind a temporary head.
    fn lua_ligkern_native(&mut self, kind: LigKern, h: Option<i64>, t: Option<i64>) -> Variadic<UdValue> {
        let h = handle32(h);
        if !self.lua_nodes.valid(h) {
            return Variadic(vec![UdValue::Nil, UdValue::Boolean(false)]);
        }
        let t = handle32(t);
        let tmp = self.lua_new_node(TEMP, 1);
        let p = self.lua_nodes.prev(h);
        self.lua_nodes.couple(tmp, h);
        let t = match kind {
            LigKern::Ligaturing => self.lk_handle_ligaturing(tmp, t),
            LigKern::Kerning => self.lk_handle_kerning(tmp, t),
        };
        let first = self.lua_nodes.next(tmp);
        if p != 0 {
            self.lua_nodes.node_mut(p).next = first;
        }
        if first != 0 {
            self.lua_nodes.node_mut(first).prev = p;
        }
        self.lua_nodes.node_mut(tmp).next = 0;
        self.lua_nodes.flush_node(tmp);
        Variadic(vec![
            UdValue::Integer(i64::from(first)),
            if t == 0 { UdValue::Nil } else { UdValue::Integer(i64::from(t)) },
            UdValue::Boolean(true),
        ])
    }
}

/// `direct_get_box_id` + `direct_check_index_range`: a box register number.
fn box_register(v: &Value, what: &str) -> Result<u16, String> {
    let k = if let Some(k) = v.as_integer() {
        k
    } else if let Some(name) = value_bytes(v) {
        with_engine(|e| {
            let id = e.cs.lookup(&name)?;
            match e.eqtb.resolve(id)? {
                crate::eqtb::Equiv::BoxReg(i) => Some(i64::from(*i)),
                crate::eqtb::Equiv::CharDef(c) => Some(i64::from(*c)),
                crate::eqtb::Equiv::MathCharDef(c) => Some(i64::from(*c)),
                _ => None,
            }
        })?
        .unwrap_or(-1)
    } else {
        return Err("argument must be a string or a number".to_string());
    };
    u16::try_from(k).map_err(|_| format!("incorrect index in {what}"))
}
