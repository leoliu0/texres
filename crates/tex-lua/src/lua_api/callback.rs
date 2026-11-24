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
    #[inline(always)]
    pub(crate) fn state(&mut self) -> &mut LuaState {
        self.state
    }

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

    /// Borrow typed userdata at argument `index` (1-based) immutably on the VM
    /// stack without creating or dropping a registry handle.
    #[inline]
    pub fn arg_userdata_ref<T: UserDataTrait + 'static, R>(
        &self,
        index: usize,
        f: impl FnOnce(&T) -> R,
    ) -> Option<R> {
        let value = self.state.get_arg(index)?;
        let ud_ptr = value.as_userdata_ptr()?;
        let ud = &ud_ptr.as_ref().data;
        if !ud.is_type::<T>() {
            return None;
        }
        if !ud.try_host_borrow() {
            ud.borrowed_by_host();
        }
        struct BorrowGuard<'a>(&'a crate::lua_value::LuaUserdata);
        impl Drop for BorrowGuard<'_> {
            #[inline]
            fn drop(&mut self) {
                self.0.release_host_borrow();
            }
        }
        let _guard = BorrowGuard(ud);
        // SAFETY: `is_type::<T>()` matched and `try_host_borrow()` holds
        // shared access for the lifetime of `_guard`.
        let data = unsafe { &*ud.data_ptr::<T>() };
        Some(f(data))
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
        let ud_ptr = value.as_userdata_ptr()?;
        let ud = &ud_ptr.as_ref().data;
        if !ud.is_type::<T>() {
            return None;
        }
        if !ud.try_host_borrow_mut() {
            ud.borrowed_by_host();
        }
        struct BorrowGuard<'a>(&'a crate::lua_value::LuaUserdata);
        impl Drop for BorrowGuard<'_> {
            #[inline]
            fn drop(&mut self) {
                self.0.release_host_borrow_mut();
            }
        }
        let _guard = BorrowGuard(ud);
        // SAFETY: `is_type::<T>()` matched and `try_host_borrow_mut()` holds
        // exclusive access for the lifetime of `_guard`; `f` has no VM handle
        // so GC cannot collect or move `ud_ptr`.
        let data = unsafe { &mut *ud.data_ptr::<T>() };
        Some(f(data))
    }

    /// Exact integer value of argument `index` (Lua `math.tointeger` semantics:
    /// integers or floats with an exact integer value; strings are not coerced).
    #[inline]
    pub fn arg_integer(&self, index: usize) -> Option<i64> {
        self.state.get_arg(index)?.as_integer()
    }

    /// Lua `lua_tointeger` on argument `index` (coercing numeric strings);
    /// returns `None` if absent/nil or not convertible to an exact integer.
    #[inline]
    pub fn arg_tointeger(&self, index: usize) -> Option<i64> {
        let value = self.state.get_arg(index)?;
        if let Some(i) = value.as_integer() {
            return Some(i);
        }
        if let Some(s) = value.as_str() {
            let parsed = crate::stdlib::basic::parse_number::parse_lua_number(s);
            return parsed.as_integer();
        }
        None
    }

    /// Numeric value of argument `index` (integer or float; strings not coerced).
    #[inline]
    pub fn arg_number(&self, index: usize) -> Option<f64> {
        self.state.get_arg(index)?.as_number()
    }

    /// Lua `lua_tonumber` on argument `index` (coercing numeric strings).
    #[inline]
    pub fn arg_tonumber(&self, index: usize) -> Option<f64> {
        let value = self.state.get_arg(index)?;
        if let Some(n) = value.as_number() {
            return Some(n);
        }
        if let Some(s) = value.as_str() {
            let parsed = crate::stdlib::basic::parse_number::parse_lua_number(s);
            return parsed.as_number();
        }
        None
    }

    /// Boolean value of argument `index` (`None` if absent or not a boolean).
    #[inline]
    pub fn arg_bool(&self, index: usize) -> Option<bool> {
        self.state.get_arg(index)?.as_boolean()
    }

    /// Lua truthiness (`lua_toboolean`) of argument `index` (false when absent, nil, or false).
    #[inline]
    pub fn arg_is_truthy(&self, index: usize) -> bool {
        self.state
            .get_arg(index)
            .is_some_and(|v| !v.is_nil() && !v.ttisfalse())
    }

    /// Whether argument `index` is absent or `nil`.
    #[inline]
    pub fn arg_is_nil(&self, index: usize) -> bool {
        self.state.get_arg(index).is_none_or(|v| v.is_nil())
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

    /// Raise a plain string error without a caller-position prefix (matches
    /// `Err(String)` returned from [`Lua::create_function`]).
    pub fn raw_error(&mut self, message: impl AsRef<str>) -> LuaError {
        self.state.error(message.as_ref().to_owned())
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

    /// Push an optional integer (`Some(v)` -> int, `None` -> nil).
    #[inline]
    pub fn push_opt_int(&mut self, value: Option<i64>) -> LuaResult<usize> {
        match value {
            Some(v) => self.push_int(v),
            None => self.push_nil(),
        }
    }

    /// Push a newly created userdata directly onto the VM stack (attaching its
    /// registered metatable if `metatable_name()` is present in the registry)
    /// without creating or dropping a registry handle.
    #[inline]
    pub fn push_userdata<T: UserDataTrait + 'static>(&mut self, data: T) -> LuaResult<usize> {
        let meta_name = data.metatable_name();
        let value = self.state.create_userdata(crate::LuaUserdata::new(data))?;
        self.state.push_value(value)?;
        if let Some(name) = meta_name
            && let Some(meta) = self.state.global_state_mut().registry_get(name)?
            && meta.as_table_ptr().is_some()
            && let Some(ptr) = value.as_userdata_ptr()
        {
            ptr.as_mut_ref().data.set_metatable(meta);
            if let Some(meta_ptr) = meta.as_gc_ptr() {
                self.state
                    .gc_barrier_obj(crate::gc::GcObjectPtr::from(ptr), meta_ptr);
            }
        }
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
        let mut iter = values.enumerate();
        let mut pending = None;
        if let Some(table) = tbl_val.as_table_mut() {
            while let Some((idx, v)) = iter.next() {
                let key = (idx + 1) as i64;
                let lv = crate::LuaValue::integer(v);
                if !table.impl_table.fast_seti(key, lv) {
                    pending = Some((key, lv));
                    break;
                }
            }
        }
        if let Some((key, lv)) = pending {
            self.state.raw_seti(&tbl_val, key, lv);
            for (idx, v) in iter {
                self.state
                    .raw_seti(&tbl_val, (idx + 1) as i64, crate::LuaValue::integer(v));
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

    /// Create a light C function (`LUA_VLCF`) from a stateless `Copy` callback
    /// closure, avoiding `RClosure` GC allocation and virtual dispatch. Falls
    /// back to a regular callback if the closure captures state.
    pub fn create_c_callback<F>(&mut self, f: F) -> LuaResult<LuaFunction>
    where
        F: Fn(&mut CallbackLua<'_>) -> LuaResult<usize> + Copy + 'static,
    {
        if std::mem::size_of::<F>() == 0 && !std::mem::needs_drop::<F>() {
            fn trampoline<F: Fn(&mut CallbackLua<'_>) -> LuaResult<usize> + Copy + 'static>(
                state: &mut LuaState,
            ) -> LuaResult<usize> {
                // SAFETY: `F` is a zero-sized `Copy` type without drop glue,
                // and an inhabited valid value `f` was witnessed by the caller.
                let f: F = unsafe { std::ptr::NonNull::<F>::dangling().read() };
                f(&mut CallbackLua { state })
            }
            let _ = f;
            return self.value_to_function(crate::LuaValue::cfunction(trampoline::<F>));
        }
        self.create_callback(f)
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

    #[test]
    fn lua53_uses_standard_pause_and_bounds_large_one_shot_allocations() {
        let mut lua53 = lua();
        let pause53: i64 = lua53
            .load("return collectgarbage('setpause', 200)")
            .eval()
            .unwrap();
        assert_eq!(pause53, 200);

        let mut lua55 = Lua::new(SafeOption::default());
        lua55.open_stdlib(Stdlib::All).unwrap();
        let pause55: i64 = lua55
            .load("return collectgarbage('setpause', 250)")
            .eval()
            .unwrap();
        assert_eq!(pause55, 250);

        let make_arr = lua53
            .create_callback(|cx| cx.push_int_array((0..100_000i32).map(i64::from)))
            .unwrap();
        lua53.set_global("make_arr", make_arr).unwrap();

        // Repeated ~1.7 MiB array allocations and 1 MiB strings must trigger
        // complete GC cycles instead of letting garbage accumulate unboundedly.
        let peak_kb: f64 = lua53
            .load(
                r#"
                local peak = 0
                local big = string.rep("x", 1024)
                for i = 1, 40 do
                    local _t = make_arr()
                    local _s = string.rep(big, 1024)
                    local cur = collectgarbage("count")
                    if cur > peak then peak = cur end
                end
                return peak
                "#,
            )
            .eval()
            .unwrap();
        // Without paying overdue GC debt, 40 iterations allocate ~110 MiB before sweeping.
        assert!(
            peak_kb < 32_768.0,
            "expected GC to bound peak memory below 32 MiB, got {peak_kb:.1} KiB"
        );
    }

    struct OtherUd;
    impl UserDataTrait for OtherUd {
        fn type_name(&self) -> &'static str {
            "other"
        }
        fn as_any(&self) -> &dyn std::any::Any {
            self
        }
        fn as_any_mut(&mut self) -> &mut dyn std::any::Any {
            self
        }
    }

    #[test]
    fn arg_userdata_mut_rejects_aliased_host_borrow() {
        let mut lua = lua();
        let counter = lua.create_userdata(Counter(10)).unwrap();
        let alias = counter.clone();
        let f = lua
            .create_callback(move |cx| {
                let rejected = cx
                    .arg_userdata_mut::<Counter, _>(1, |c| {
                        c.0 += 1;
                        // Both shared and mutable host borrows must fail while arg_userdata_mut is live.
                        alias.borrow().is_err() && alias.borrow_mut().is_err()
                    })
                    .unwrap();
                cx.push_bool(rejected)
            })
            .unwrap();
        lua.set_global("c", counter.clone()).unwrap();
        lua.set_global("f", f).unwrap();
        let rejected: bool = lua.load("return f(c)").eval().unwrap();
        assert!(rejected, "expected aliased host borrow to be rejected");
        // Drop guard must have released host_borrow after callback returns.
        assert_eq!(counter.borrow().unwrap().0, 11);

        // Verify drop guard also releases host_borrow on unwind (panic inside callback).
        let panicky = lua
            .create_callback(|cx| {
                let _ = cx.arg_userdata_mut::<Counter, _>(1, |c| {
                    c.0 += 1;
                    panic!("intentional unwind");
                });
                Ok(0)
            })
            .unwrap();
        lua.set_global("panicky", panicky).unwrap();
        let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = lua.load("panicky(c)").exec();
        }));
        assert!(res.is_err());
        assert_eq!(counter.borrow().unwrap().0, 12);
    }

    #[test]
    fn arg_userdata_mut_wrong_type_does_not_panic_on_host_borrowed_userdata() {
        let mut lua = lua();
        let other = lua.create_userdata(OtherUd).unwrap();
        let f = lua
            .create_callback(|cx| {
                let matched = cx.arg_userdata_mut::<Counter, _>(1, |c| c.0).is_some();
                cx.push_bool(matched)
            })
            .unwrap();
        lua.set_global("u", other.clone()).unwrap();
        lua.set_global("f", f).unwrap();
        let _guard = other.borrow_mut().unwrap();
        let matched: bool = lua.load("return f(u)").eval().unwrap();
        assert!(!matched);
    }

    #[test]
    fn push_int_array_handles_exact_size_iterator_overrun() {
        struct LyingIter {
            next: i64,
            end: i64,
        }
        impl Iterator for LyingIter {
            type Item = i64;
            fn next(&mut self) -> Option<i64> {
                if self.next <= self.end {
                    let v = self.next;
                    self.next += 1;
                    Some(v)
                } else {
                    None
                }
            }
        }
        impl ExactSizeIterator for LyingIter {
            fn len(&self) -> usize {
                // Intentionally under-report length to force fallback to `raw_seti`.
                2
            }
        }

        let mut lua = lua();
        let make = lua
            .create_callback(|cx| cx.push_int_array(LyingIter { next: 1, end: 12 }))
            .unwrap();
        lua.set_global("make", make).unwrap();
        let sum: i64 = lua
            .load(
                "local t = make(); local s = 0; for i = 1, 12 do s = s + t[i] end; return s",
            )
            .eval()
            .unwrap();
        assert_eq!(sum, 78);
    }

    #[test]
    fn small_stepsize_does_not_force_fast_gc_mode() {
        let mut lua = Lua::new(SafeOption::default());
        lua.open_stdlib(Stdlib::All).unwrap();
        // Configure stepsize < sizeof(*const ()) with non-zero stepmul; GC must remain incremental.
        let ok: bool = lua
            .load(
                r#"
                collectgarbage("incremental")
                collectgarbage("param", "stepsize", 1)
                collectgarbage("param", "stepmul", 200)
                local t = {}
                for i = 1, 2000 do t[i] = { i, i + 1 } end
                return #t == 2000
                "#,
            )
            .eval()
            .unwrap();
        assert!(ok);
    }
}
