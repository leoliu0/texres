//! Callbacks that can allocate: [`Lua::create_callback`] runs a Rust closure
//! with a [`CallbackLua`], the running call's view of the VM. Unlike typed
//! callbacks (`create_function`), such a closure can create tables, byte
//! strings and userdata, and it reads its arguments and pushes its results
//! one by one, with Lua's own argument errors.
//!
//! `CallbackLua` borrows the call's state for the duration of the call, so it
//! cannot escape the closure or be used on another VM. Everything it creates
//! is handed out as a rooted handle ([`LuaTable`], [`LuaString`],
//! [`UserDataRef`], [`Value`]) that the existing liveness checks police, and
//! results are anchored on the call's stack as they are pushed.

use crate::lua_api::{Lua, LuaApi, LuaFunction, LuaString, LuaTable, Value};
use crate::stdlib::lauxlib;
use crate::{
    FromLua, IntoLua, LuaError, LuaResult, LuaState, LuaValueKind, UserDataRef, UserDataTrait,
};

/// The running native call: its arguments, its result stack and allocation.
pub struct CallbackLua<'a> {
    state: &'a mut LuaState,
}

impl CallbackLua<'_> {
    /// Number of arguments passed to the call.
    #[inline]
    pub fn arg_count(&self) -> usize {
        self.state.arg_count()
    }

    /// Kind of argument `index` (1-based); `None` when it was not passed.
    #[inline]
    pub fn arg_kind(&self, index: usize) -> Option<LuaValueKind> {
        self.state.get_arg(index).map(|value| value.kind())
    }

    /// Run `f` on the bytes of argument `index` (1-based) if it is a string
    /// (numbers are not converted), without copying them; `None` otherwise.
    #[inline]
    pub fn arg_bytes<R>(&self, index: usize, f: impl FnOnce(&[u8]) -> R) -> Option<R> {
        let value = self.state.get_arg(index)?;
        value.as_bytes().map(f)
    }

    /// Borrow typed userdata at argument `index` (1-based) directly on the VM
    /// stack without creating or dropping a registry handle.
    #[inline]
    pub fn arg_userdata_mut<T: UserDataTrait + 'static, R>(
        &mut self,
        index: usize,
        f: impl FnOnce(&mut T) -> R,
    ) -> Option<R> {
        let value = self.state.get_arg(index)?;
        let ud = value.as_userdata_mut()?.downcast_mut::<T>()?;
        Some(f(ud))
    }

    /// Exact integer value of argument `index` (Lua `math.tointeger` semantics:
    /// integers or floats with an exact integer value; strings are not coerced).
    #[inline]
    pub fn arg_integer(&self, index: usize) -> Option<i64> {
        self.state.get_arg(index)?.as_integer()
    }

    /// Numeric value of argument `index` (integer or float; strings not coerced).
    #[inline]
    pub fn arg_number(&self, index: usize) -> Option<f64> {
        self.state.get_arg(index)?.as_number()
    }

    /// Argument `index` (1-based) converted to `T`; an absent argument is nil.
    /// A failed conversion raises Lua's "bad argument #n to 'f' (...)" error.
    pub fn arg<T: FromLua>(&mut self, index: usize) -> LuaResult<T> {
        let value = self.state.get_arg(index).unwrap_or_default();
        T::from_lua(value, self.state)
            .map_err(|message| lauxlib::argerror(self.state, index, &message))
    }

    /// `luaL_argerror`.
    pub fn arg_error(&mut self, index: usize, message: &str) -> LuaError {
        lauxlib::argerror(self.state, index, message)
    }

    /// `luaL_argerror` with a signed index (negative: relative to the top
    /// of the stack, which LuaTeX's `mplib.new` reports as `#-1`).
    pub fn arg_error_at(&mut self, index: i64, message: &str) -> LuaError {
        lauxlib::argerror_at(self.state, index, message)
    }

    /// `luaL_typeerror`: "`expected` expected, got <type of argument>".
    pub fn type_error(&mut self, index: usize, expected: &str) -> LuaError {
        lauxlib::typeerror(self.state, index, expected)
    }

    /// `luaL_error`: an error message prefixed with the caller's position.
    pub fn error(&mut self, message: impl AsRef<str>) -> LuaError {
        lauxlib::lual_error(self.state, message)
    }

    /// Push `value` as a result of the call; returns the number of values
    /// pushed, to be added to the closure's result count.
    pub fn push<T: IntoLua>(&mut self, value: T) -> LuaResult<usize> {
        self.state.push_multi(value)
    }

    /// Push `nil` directly onto the VM stack without `IntoLua` dispatch.
    #[inline]
    pub fn push_nil(&mut self) -> LuaResult<usize> {
        self.state.push_value(crate::LuaValue::nil())?;
        Ok(1)
    }

    /// Push a boolean directly onto the VM stack.
    #[inline]
    pub fn push_bool(&mut self, value: bool) -> LuaResult<usize> {
        self.state.push_value(crate::LuaValue::boolean(value))?;
        Ok(1)
    }

    /// Push an integer directly onto the VM stack.
    #[inline]
    pub fn push_int(&mut self, value: i64) -> LuaResult<usize> {
        self.state.push_value(crate::LuaValue::integer(value))?;
        Ok(1)
    }

    /// Push a float directly onto the VM stack.
    #[inline]
    pub fn push_float(&mut self, value: f64) -> LuaResult<usize> {
        self.state.push_value(crate::LuaValue::float(value))?;
        Ok(1)
    }

    /// Create a Lua byte string and push it directly onto the VM stack without
    /// allocating an intermediate `Vec<u8>` or registry `LuaString` handle.
    #[inline]
    pub fn push_bytes(&mut self, bytes: &[u8]) -> LuaResult<usize> {
        let val = self.state.create_bytes(bytes)?;
        self.state.push_value(val)?;
        Ok(1)
    }

    /// Create a pre-sized Lua array table `[1..=len]` of integers and push it
    /// directly onto the VM stack in a single allocation.
    pub fn push_int_array(
        &mut self,
        values: impl ExactSizeIterator<Item = i64>,
    ) -> LuaResult<usize> {
        let len = values.len();
        let tbl_val = self.state.create_table(len, 0)?;
        self.state.push_value(tbl_val)?;
        if let Some(table) = tbl_val.as_table_mut() {
            for (idx, v) in values.enumerate() {
                let key = (idx + 1) as i64;
                let lv = crate::LuaValue::integer(v);
                if !table.impl_table.fast_seti(key, lv) {
                    self.state.raw_seti(&tbl_val, key, lv);
                }
            }
        }
        Ok(1)
    }

    pub fn create_table(&mut self) -> LuaResult<LuaTable> {
        LuaApi::create_table(self.state)
    }

    pub fn create_table_with_capacity(&mut self, narr: usize, nrec: usize) -> LuaResult<LuaTable> {
        LuaApi::create_table_with_capacity(self.state, narr, nrec)
    }

    pub fn create_bytes(&mut self, bytes: &[u8]) -> LuaResult<LuaString> {
        LuaApi::create_bytes(self.state, bytes)
    }

    pub fn create_userdata<T: UserDataTrait + 'static>(
        &mut self,
        data: T,
    ) -> LuaResult<UserDataRef<T>> {
        LuaApi::create_userdata(self.state, data)
    }

    /// Root any convertible Rust value as a handle.
    pub fn pack<T: IntoLua>(&mut self, value: T) -> LuaResult<Value> {
        LuaApi::pack(self.state, value)
    }
}

impl Lua {
    /// Create a Lua function from a closure that reads its arguments from
    /// and pushes its results through a [`CallbackLua`]. The closure returns
    /// the number of results it pushed.
    pub fn create_callback<F>(&mut self, f: F) -> LuaResult<LuaFunction>
    where
        F: Fn(&mut CallbackLua<'_>) -> LuaResult<usize> + 'static,
    {
        self.create_raw_function(move |state| f(&mut CallbackLua { state }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{LuaBytes, SafeOption, Stdlib, UserDataTrait};

    struct Counter(i64);

    impl UserDataTrait for Counter {
        fn type_name(&self) -> &'static str {
            "counter"
        }
        fn lua_tostring(&self) -> Option<String> {
            Some(format!("counter {}", self.0))
        }
        fn as_any(&self) -> &dyn std::any::Any {
            self
        }
        fn as_any_mut(&mut self) -> &mut dyn std::any::Any {
            self
        }
    }

    fn lua() -> Lua {
        let mut lua = Lua::new_lua53(SafeOption::default());
        lua.open_stdlib(Stdlib::All).unwrap();
        lua
    }

    // A callback allocates a table, a binary string and a userdata, and
    // returns them; they survive a full collection and are usable after it.
    #[test]
    fn callback_created_values_survive_collection() {
        let mut lua = lua();
        let make = lua
            .create_callback(|cx| {
                let n: i64 = cx.arg(1)?;
                let table = cx.create_table()?;
                table.raw_seti(1, LuaBytes(vec![0xFF, 0x00, b'a']))?;
                let counter = cx.create_userdata(Counter(n))?;
                let bytes = cx.create_bytes(&[0x80, 0x81])?;
                Ok(cx.push(table)? + cx.push(counter)? + cx.push(bytes)?)
            })
            .unwrap();
        lua.set_global("make", make).unwrap();
        let ok: bool = lua
            .load(
                "local t, u, s = make(7)
                 collectgarbage() collectgarbage()
                 return type(t) == 'table' and #t[1] == 3 and t[1]:byte(1) == 255
                   and type(u) == 'userdata' and s == '\\x80\\x81'",
            )
            .eval()
            .unwrap();
        assert!(ok);
    }

    // A handle made on one VM is rejected when pushed from a callback on another.
    #[test]
    fn callback_rejects_values_of_another_vm() {
        let mut other = lua();
        let foreign = other.create_table().unwrap();
        let mut lua = lua();
        let f = lua
            .create_callback(move |cx| {
                cx.push(foreign.clone())?;
                Ok(1)
            })
            .unwrap();
        lua.set_global("f", f).unwrap();
        let rejected: bool = lua.load("return not pcall(f)").eval().unwrap();
        assert!(rejected);
    }
}
