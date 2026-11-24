//! The iterator functions of `node.traverse*` and `node.direct.traverse*`
//! (lnodelib.c `nodelib_aux_next*`, `nodelib_direct_aux_next*`), which
//! `lua_node.lua` hands to the generic `for`.
//!
//! As in LuaTeX, an iterator called with a nil control value starts at its
//! state (the head) and otherwise at the node after the control value, read
//! when the step runs: a loop body may relink or replace the node it is at.
//! The first node from there that the iterator accepts is returned with its
//! data; past the end a single nil is returned.

use tex_lua::{CallbackLua, Lua, LuaApi, LuaResult, LuaTable};

use crate::lua_bridge::with_engine;
use crate::lua_node::*;
use crate::lua_node_conv::sl;
use crate::lua_node_lib::NodeUd;

/// Node ids below this get a prebuilt `traverse_id` iterator each; other
/// filter values use one that takes the id as an argument.
const PREBUILT_IDS: u8 = 64;

macro_rules! cnat {
    ($lua:expr, $tbl:expr, $name:expr, $f:expr) => {
        $tbl.set($name, $lua.create_c_callback($f).map_err(|e| format!("{}: {e:?}", $name))?)
            .map_err(|e| format!("{}: {e:?}", $name))?
    };
}

/// The node a step stops at: from `first` (no control value) or from the
/// node after `cur`, the first node `keep` accepts; 0 past the end. A node
/// Lua has freed ends the walk.
#[inline(always)]
fn step(s: &NodeStore, first: u32, cur: Option<u32>, keep: impl Fn(&LNode) -> bool) -> u32 {
    let mut t = match cur {
        None => first,
        Some(c) if s.valid(c) => s.node(c).next,
        Some(_) => 0,
    };
    while s.valid(t) {
        let nd = s.node(t);
        if keep(nd) {
            return t;
        }
        t = nd.next;
    }
    0
}

#[inline(always)]
fn any(_: &LNode) -> bool {
    true
}

/// `traverse_char`: glyphs not yet processed (subtype below 256)
#[inline(always)]
fn char_glyph(nd: &LNode) -> bool {
    nd.id == GLYPH && nd.subtype < 256
}

#[inline(always)]
fn glyph(nd: &LNode) -> bool {
    nd.id == GLYPH
}

#[inline(always)]
fn list(nd: &LNode) -> bool {
    nd.id == HLIST || nd.id == VLIST
}

#[inline(always)]
fn direct_args(cx: &CallbackLua<'_>, s_idx: usize, c_idx: usize) -> (u32, Option<u32>) {
    let s = cx.arg_integer(s_idx).or_else(|| cx.arg_tointeger(s_idx)).unwrap_or(0) as u32;
    let c = if cx.arg_is_nil(c_idx) {
        None
    } else {
        Some(cx.arg_integer(c_idx).or_else(|| cx.arg_tointeger(c_idx)).unwrap_or(0) as u32)
    };
    (s, c)
}

#[inline(always)]
fn ud_args(cx: &mut CallbackLua<'_>, s_idx: usize, c_idx: usize) -> LuaResult<(u32, Option<u32>)> {
    let Some(s) = cx.arg_userdata_ref::<NodeUd, _>(s_idx, |u| u.h) else {
        return Err(cx.type_error(s_idx, "luatex.node"));
    };
    let c = if cx.arg_is_nil(c_idx) {
        None
    } else {
        let Some(h) = cx.arg_userdata_ref::<NodeUd, _>(c_idx, |u| u.h) else {
            return Err(cx.type_error(c_idx, "luatex.node"));
        };
        Some(h)
    };
    Ok((s, c))
}

#[inline(always)]
fn list_head(nd: &LNode) -> u32 {
    nd.f[sl::B_HEAD] as u32
}

#[inline(always)]
fn push_ud_or_nil(cx: &mut CallbackLua<'_>, h: u32) -> LuaResult<usize> {
    if h == 0 {
        cx.push_nil()
    } else {
        cx.push_userdata(NodeUd { h })
    }
}

fn trav_id_const<const ID: u8>(cx: &mut CallbackLua<'_>) -> LuaResult<usize> {
    let (s, c) = direct_args(cx, 1, 2);
    let res = with_engine(|e| {
        let st = &e.lua_nodes;
        let t = step(st, s, c, |nd| nd.id == ID);
        (t != 0).then(|| (i64::from(t), i64::from(st.subtype(t))))
    })
    .map_err(|msg| cx.raw_error(msg))?;
    match res {
        Some((t, sub)) => {
            cx.push_int(t)?;
            cx.push_int(sub)?;
            Ok(2)
        }
        None => cx.push_nil(),
    }
}

fn trav_id_ud_const<const ID: u8>(cx: &mut CallbackLua<'_>) -> LuaResult<usize> {
    let (s, c) = ud_args(cx, 1, 2)?;
    let res = with_engine(|e| {
        let st = &e.lua_nodes;
        let t = step(st, s, c, |nd| nd.id == ID);
        (t != 0).then(|| (t, i64::from(st.subtype(t))))
    })
    .map_err(|msg| cx.raw_error(msg))?;
    match res {
        Some((t, sub)) => {
            cx.push_userdata(NodeUd { h: t })?;
            cx.push_int(sub)?;
            Ok(2)
        }
        None => cx.push_nil(),
    }
}

macro_rules! install_prebuilt_ids {
    ($lua:expr, $tbl:expr, $fn:ident, [$($id:literal),* $(,)?]) => {{
        $(
            cnat!($lua, $tbl, i64::from($id), |cx| $fn::<$id>(cx));
        )*
    }};
}

/// Register the iterators on the natives table `n`: `trav_next`,
/// `trav_char`, `trav_glyph`, `trav_list`, `trav_id` (id, state, control)
/// and the table `trav_ids` of per-id iterators, and the same with a `_ud`
/// suffix for userdata nodes.
pub(crate) fn install(lua: &mut Lua, n: &LuaTable) -> Result<(), String> {
    // ---- node.direct ----
    cnat!(lua, n, "trav_next", |cx| {
        let (s, c) = direct_args(cx, 1, 2);
        let res = with_engine(|e| {
            let st = &e.lua_nodes;
            let t = step(st, s, c, any);
            (t != 0).then(|| {
                let nd = st.node(t);
                (i64::from(t), i64::from(nd.id), i64::from(nd.subtype))
            })
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((t, id, sub)) => {
                cx.push_int(t)?;
                cx.push_int(id)?;
                cx.push_int(sub)?;
                Ok(3)
            }
            None => cx.push_nil(),
        }
    });
    cnat!(lua, n, "trav_char", |cx| {
        let (s, c) = direct_args(cx, 1, 2);
        let res = with_engine(|e| {
            let st = &e.lua_nodes;
            let t = step(st, s, c, char_glyph);
            (t != 0).then(|| {
                let nd = st.node(t);
                (i64::from(t), i64::from(nd.f[0]), i64::from(nd.f[1]))
            })
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((t, ch, font)) => {
                cx.push_int(t)?;
                cx.push_int(ch)?;
                cx.push_int(font)?;
                Ok(3)
            }
            None => cx.push_nil(),
        }
    });
    cnat!(lua, n, "trav_glyph", |cx| {
        let (s, c) = direct_args(cx, 1, 2);
        let res = with_engine(|e| {
            let st = &e.lua_nodes;
            let t = step(st, s, c, glyph);
            (t != 0).then(|| {
                let nd = st.node(t);
                (i64::from(t), i64::from(nd.f[0]), i64::from(nd.f[1]))
            })
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((t, ch, font)) => {
                cx.push_int(t)?;
                cx.push_int(ch)?;
                cx.push_int(font)?;
                Ok(3)
            }
            None => cx.push_nil(),
        }
    });
    cnat!(lua, n, "trav_list", |cx| {
        let (s, c) = direct_args(cx, 1, 2);
        let res = with_engine(|e| {
            let st = &e.lua_nodes;
            let t = step(st, s, c, list);
            (t != 0).then(|| {
                let nd = st.node(t);
                let head = list_head(nd);
                (
                    i64::from(t),
                    i64::from(nd.id),
                    i64::from(nd.subtype),
                    (head != 0).then_some(i64::from(head)),
                )
            })
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((t, id, sub, head)) => {
                cx.push_int(t)?;
                cx.push_int(id)?;
                cx.push_int(sub)?;
                cx.push_opt_int(head)?;
                Ok(4)
            }
            None => cx.push_nil(),
        }
    });
    cnat!(lua, n, "trav_id", |cx| {
        // the C int of lua_tointeger: no node id matches outside 0..=255
        let id = cx.arg_integer(1).or_else(|| cx.arg_tointeger(1)).unwrap_or(0) as i32;
        let (s, c) = direct_args(cx, 2, 3);
        let res = with_engine(|e| {
            let st = &e.lua_nodes;
            let t = step(st, s, c, |nd| i32::from(nd.id) == id);
            (t != 0).then(|| (i64::from(t), i64::from(st.subtype(t))))
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((t, sub)) => {
                cx.push_int(t)?;
                cx.push_int(sub)?;
                Ok(2)
            }
            None => cx.push_nil(),
        }
    });
    let ids = lua.create_table().map_err(|e| format!("{e:?}"))?;
    const _: () = assert!(PREBUILT_IDS == 64);
    install_prebuilt_ids!(
        lua,
        ids,
        trav_id_const,
        [
            0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23,
            24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45,
            46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63
        ]
    );
    n.set("trav_ids", ids).map_err(|e| format!("trav_ids: {e:?}"))?;

    // ---- node (userdata) ----
    cnat!(lua, n, "trav_next_ud", |cx| {
        let (s, c) = ud_args(cx, 1, 2)?;
        let res = with_engine(|e| {
            let st = &e.lua_nodes;
            let t = step(st, s, c, any);
            (t != 0).then(|| {
                let nd = st.node(t);
                (t, i64::from(nd.id), i64::from(nd.subtype))
            })
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((t, id, sub)) => {
                cx.push_userdata(NodeUd { h: t })?;
                cx.push_int(id)?;
                cx.push_int(sub)?;
                Ok(3)
            }
            None => cx.push_nil(),
        }
    });
    cnat!(lua, n, "trav_char_ud", |cx| {
        let (s, c) = ud_args(cx, 1, 2)?;
        let res = with_engine(|e| {
            let st = &e.lua_nodes;
            let t = step(st, s, c, char_glyph);
            (t != 0).then(|| {
                let nd = st.node(t);
                (t, i64::from(nd.f[0]), i64::from(nd.f[1]))
            })
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((t, ch, font)) => {
                cx.push_userdata(NodeUd { h: t })?;
                cx.push_int(ch)?;
                cx.push_int(font)?;
                Ok(3)
            }
            None => cx.push_nil(),
        }
    });
    cnat!(lua, n, "trav_glyph_ud", |cx| {
        let (s, c) = ud_args(cx, 1, 2)?;
        let res = with_engine(|e| {
            let st = &e.lua_nodes;
            let t = step(st, s, c, glyph);
            (t != 0).then(|| {
                let nd = st.node(t);
                (t, i64::from(nd.f[0]), i64::from(nd.f[1]))
            })
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((t, ch, font)) => {
                cx.push_userdata(NodeUd { h: t })?;
                cx.push_int(ch)?;
                cx.push_int(font)?;
                Ok(3)
            }
            None => cx.push_nil(),
        }
    });
    cnat!(lua, n, "trav_list_ud", |cx| {
        let (s, c) = ud_args(cx, 1, 2)?;
        let res = with_engine(|e| {
            let st = &e.lua_nodes;
            let t = step(st, s, c, list);
            (t != 0).then(|| {
                let nd = st.node(t);
                (t, i64::from(nd.id), i64::from(nd.subtype), list_head(nd))
            })
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((t, id, sub, head)) => {
                cx.push_userdata(NodeUd { h: t })?;
                cx.push_int(id)?;
                cx.push_int(sub)?;
                push_ud_or_nil(cx, head)?;
                Ok(4)
            }
            None => cx.push_nil(),
        }
    });
    cnat!(lua, n, "trav_id_ud", |cx| {
        let id = cx.arg_integer(1).or_else(|| cx.arg_tointeger(1)).unwrap_or(0) as i32;
        let (s, c) = ud_args(cx, 2, 3)?;
        let res = with_engine(|e| {
            let st = &e.lua_nodes;
            let t = step(st, s, c, |nd| i32::from(nd.id) == id);
            (t != 0).then(|| (t, i64::from(st.subtype(t))))
        })
        .map_err(|msg| cx.raw_error(msg))?;
        match res {
            Some((t, sub)) => {
                cx.push_userdata(NodeUd { h: t })?;
                cx.push_int(sub)?;
                Ok(2)
            }
            None => cx.push_nil(),
        }
    });
    let ids = lua.create_table().map_err(|e| format!("{e:?}"))?;
    install_prebuilt_ids!(
        lua,
        ids,
        trav_id_ud_const,
        [
            0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23,
            24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45,
            46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63
        ]
    );
    n.set("trav_ids_ud", ids).map_err(|e| format!("trav_ids_ud: {e:?}"))?;
    Ok(())
}
