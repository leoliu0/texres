use std::marker::PhantomData;

use crate::gc::StringInterner;
use crate::lua_value::NativeTable;
use crate::{FromLua, FromLuaMulti, IntoLua, LuaFunction, LuaResult, LuaTableRef, LuaValue};

/// Pre-resolved short-string key scoped to a [`LuaTable::with_raw`] borrow.
/// Avoids repeating string hashing and interner lookup across many tables.
#[derive(Clone, Copy)]
pub struct RawStrKey<'a> {
    value: Option<LuaValue>,
    _marker: PhantomData<&'a ()>,
}

/// Borrowed, read-only view of a Lua value inside [`LuaTable::with_raw`].
/// Zero registry handles, zero GC allocations.
#[derive(Clone, Copy)]
pub struct RawValue<'a> {
    value: LuaValue,
    strings: &'a StringInterner,
}

impl<'a> RawValue<'a> {
    #[inline(always)]
    pub fn is_nil(self) -> bool {
        self.value.is_nil()
    }

    #[inline(always)]
    pub fn as_number(self) -> Option<f64> {
        self.value.as_number()
    }

    #[inline(always)]
    pub fn as_integer(self) -> Option<i64> {
        self.value.as_integer()
    }

    #[inline(always)]
    pub fn as_boolean(self) -> Option<bool> {
        self.value.as_boolean()
    }

    /// Zero-copy borrow of string bytes directly from the VM string object.
    #[inline(always)]
    pub fn as_bytes(self) -> Option<&'a [u8]> {
        self.value.as_string_ptr().map(|ptr| {
            // SAFETY: `RawValue<'a>` is bound to an immutable borrow of the VM
            // (`LuaTable::with_raw`), during which no GC or mutation can occur.
            unsafe { &*ptr.as_raw_ptr() }.data.as_bytes()
        })
    }

    /// Borrow a nested table without creating a registry handle.
    #[inline(always)]
    pub fn as_table(self) -> Option<RawTable<'a>> {
        self.value.as_table_ptr().map(|ptr| RawTable {
            // SAFETY: `RawValue<'a>` is bound to an immutable borrow of the VM
            // (`LuaTable::with_raw`), during which no GC or mutation can occur.
            table: &unsafe { &*ptr.as_raw_ptr() }.data.impl_table,
            strings: self.strings,
        })
    }
}

/// Borrowed, read-only view of a Lua table inside [`LuaTable::with_raw`].
/// Zero registry handles, zero GC allocations, and no VM mutation is possible
/// for the lifetime `'a`.
#[derive(Clone, Copy)]
pub struct RawTable<'a> {
    table: &'a NativeTable,
    strings: &'a StringInterner,
}

impl<'a> RawTable<'a> {
    /// Pre-resolve a short string key (<= 40 bytes) once against the VM's
    /// string interner without allocating or mutating GC state.
    #[inline]
    pub fn resolve_key(self, key: &str) -> RawStrKey<'a> {
        RawStrKey {
            value: self.strings.lookup_short(key.as_bytes()),
            _marker: PhantomData,
        }
    }

    /// Look up a pre-resolved short string key without metamethods or allocation.
    #[inline]
    pub fn get_key(self, key: RawStrKey<'a>) -> Option<RawValue<'a>> {
        let key_val = key.value?;
        self.table.get_shortstr(&key_val).map(|value| RawValue {
            value,
            strings: self.strings,
        })
    }

    /// Raw lookup by string key without allocating a GC string or registry handle.
    #[inline]
    pub fn get_str(self, key: &str) -> Option<RawValue<'a>> {
        let bytes = key.as_bytes();
        if let Some(key_val) = self.strings.lookup_short(bytes) {
            return self.table.get_shortstr(&key_val).map(|value| RawValue {
                value,
                strings: self.strings,
            });
        }
        #[cfg(not(feature = "shared-proto"))]
        if bytes.len() <= StringInterner::SHORT_STRING_LIMIT {
            return None;
        }
        self.table.get_str_bytes_fallback(bytes).map(|value| RawValue {
            value,
            strings: self.strings,
        })
    }

    /// Raw lookup by integer key without creating a registry handle.
    #[inline]
    pub fn get_int(self, key: i64) -> Option<RawValue<'a>> {
        self.table.get_int(key).map(|value| RawValue {
            value,
            strings: self.strings,
        })
    }

    /// Raw array length (`#t`).
    #[inline]
    pub fn raw_len(self) -> usize {
        self.table.len()
    }

    /// Returns true if the table has no live entries (array or hash).
    #[inline]
    pub fn is_empty_entries(self) -> bool {
        self.table.is_empty_entries()
    }

    /// Iterate over all live `(key, value)` pairs without allocating a `Vec`
    /// or creating registry handles.
    #[inline]
    pub fn try_for_each<E>(
        self,
        mut f: impl FnMut(RawValue<'a>, RawValue<'a>) -> Result<(), E>,
    ) -> Result<(), E> {
        let strings = self.strings;
        self.table.try_for_each_entry(|k, v| {
            f(
                RawValue { value: k, strings },
                RawValue { value: v, strings },
            )
        })
    }
}

/// Safe wrapper around a Lua table handle.
#[derive(Clone, Debug)]
pub struct LuaTable {
    pub(crate) inner: LuaTableRef,
}

impl LuaTable {
    pub(crate) fn new(inner: LuaTableRef) -> Self {
        LuaTable { inner }
    }

    /// Get and convert a field.
    #[inline]
    pub fn get<T: FromLua>(&self, key: impl IntoLua) -> LuaResult<T> {
        self.inner.get_typed(key)
    }

    /// Look up a nested path of string keys.
    pub fn get_path<T: FromLua>(&self, path: &[&str]) -> LuaResult<T> {
        let mut current = self.clone();
        let Some((last, prefix)) = path.split_last() else {
            return Err(crate::LuaError::RuntimeError);
        };

        for key in prefix {
            current = current.get::<LuaTable>(*key)?;
        }

        current.get(*last)
    }

    /// Returns true if the table contains a non-nil value for `key`.
    #[inline]
    pub fn contains_key(&self, key: impl IntoLua) -> LuaResult<bool> {
        self.inner.contains_key(key)
    }

    /// Returns true if this table currently has a metatable.
    #[inline]
    pub fn has_metatable(&self) -> bool {
        self.inner.has_metatable()
    }

    /// Get the table's metatable, if present.
    #[inline]
    pub fn get_metatable(&self) -> Option<LuaTable> {
        self.inner.get_metatable().map(LuaTable::new)
    }

    /// Set or clear the table's metatable.
    #[inline]
    pub fn set_metatable(&self, metatable: Option<&LuaTable>) -> LuaResult<()> {
        self.inner
            .set_metatable(metatable.map(|table| &table.inner))
    }

    /// Create an empty table that inherits fields from this table.
    #[inline]
    pub fn inherit(&self) -> LuaResult<LuaTable> {
        self.inner.inherit().map(LuaTable::new)
    }

    /// Set a field.
    #[inline]
    pub fn set(&self, key: impl IntoLua, value: impl IntoLua) -> LuaResult<()> {
        self.inner.set_typed(key, value)
    }

    /// Get a named function field and call it with `args`.
    #[inline]
    pub fn call_function<A: IntoLua, R: FromLuaMulti>(&self, name: &str, args: A) -> LuaResult<R> {
        self.get::<LuaFunction>(name)?.call(args)
    }

    /// Get a named method field and call it with `self` as the first argument.
    #[inline]
    pub fn call_method<A: IntoLua, R: FromLuaMulti>(&self, name: &str, args: A) -> LuaResult<R> {
        self.get::<LuaFunction>(name)?.call((self.clone(), args))
    }

    /// Get a named method field and call it, converting only the first result.
    #[inline]
    pub fn call_method1<A: IntoLua, R: FromLua>(&self, name: &str, args: A) -> LuaResult<R> {
        self.get::<LuaFunction>(name)?.call1((self.clone(), args))
    }

    /// Get a field without metamethods.
    #[inline]
    pub fn raw_get<T: FromLua>(&self, key: impl IntoLua) -> LuaResult<T> {
        self.inner.rawget_typed(key)
    }

    /// Set a field without metamethods.
    #[inline]
    pub fn raw_set(&self, key: impl IntoLua, value: impl IntoLua) -> LuaResult<()> {
        self.inner.rawset_typed(key, value)
    }

    #[inline]
    pub fn raw_seti(&self, idx: i64, value: impl IntoLua) -> LuaResult<()> {
        self.inner.rawseti_typed(idx, value)
    }

    #[inline]
    pub fn raw_geti<T: FromLua>(&self, idx: i64) -> LuaResult<T> {
        self.inner.rawgeti_typed(idx)
    }

    /// Return the array length (`#t`).
    #[inline]
    pub fn len(&self) -> LuaResult<usize> {
        self.inner.len()
    }

    /// Return the raw array length.
    #[inline]
    pub fn raw_len(&self) -> LuaResult<usize> {
        self.inner.len()
    }

    /// Returns true if the table length is zero.
    #[inline]
    pub fn is_empty(&self) -> LuaResult<bool> {
        self.len().map(|len| len == 0)
    }

    /// Return all pairs converted to Rust types.
    #[inline]
    pub fn pairs<K: FromLua, V: FromLua>(&self) -> LuaResult<Vec<(K, V)>> {
        self.inner.pairs_typed()
    }

    /// Return the contiguous sequence values from `1..` until nil.
    #[inline]
    pub fn sequence_values<T: FromLua>(&self) -> LuaResult<Vec<T>> {
        self.inner.sequence_values()
    }

    /// Borrow this table for read-only raw inspection without allocating
    /// registry handles or GC strings.
    #[inline]
    pub fn with_raw<R>(&self, f: impl FnOnce(RawTable<'_>) -> R) -> LuaResult<R> {
        self.inner
            .with_raw(|table, strings| f(RawTable { table, strings }))
    }

    /// Append a value to the array portion of the table.
    #[inline]
    pub fn push(&self, value: impl IntoLua) -> LuaResult<()> {
        self.inner.push_typed(value)
    }

    pub(crate) fn value(&self) -> crate::LuaValue {
        self.inner.to_value()
    }

    /// Whether `self` belongs to the same Lua state as `state`.
    pub(crate) fn same_state_as(&self, state: &crate::GlobalState) -> bool {
        self.inner.belongs_to(state)
    }
}

impl IntoLua for LuaTable {
    #[inline]
    fn into_lua(self, state: &mut crate::LuaState) -> Result<usize, String> {
        self.inner.push_into(state)
    }
}

impl IntoLua for &LuaTable {
    #[inline]
    fn into_lua(self, state: &mut crate::LuaState) -> Result<usize, String> {
        self.inner.push_into(state)
    }
}

impl FromLua for LuaTable {
    fn from_lua(value: LuaValue, state: &mut crate::LuaState) -> Result<Self, String> {
        let actual = value.type_name();
        let table = state
            .to_table_ref(value)
            .ok_or_else(|| format!("expected table, got {}", actual))?;
        Ok(LuaTable::new(table))
    }
}
