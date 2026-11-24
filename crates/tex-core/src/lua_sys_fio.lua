-- Install the native Rust `fio` and `sio` tables registered by `lua_sys_fio.rs`.
local S = __texres_sys
_G.fio = S.fio
_G.sio = S.sio
package.loaded.fio = S.fio
package.loaded.sio = S.sio
