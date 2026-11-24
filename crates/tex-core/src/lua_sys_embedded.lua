-- Reading files of the embedded archive through the standard `io` and
-- `loadfile` functions. Paths below `/<embedded>/` (what `kpse.find_file`
-- reports for bundled files) denote a read-only virtual TDS tree; the
-- handles returned for them are native `EmbeddedFile` userdata with the
-- `FILE*` method surface so `fio`, `lfs`, and `io` work without Lua-table overhead.
local S = __texres_sys
local type, error, load = type, error, load
local sub, find, match, byte = string.sub, string.find, string.match, string.byte

local is_path, read_file = S.embedded_is_path, S.embedded_read
local open_embedded = S.embedded_open
local vfile_type, vfile_is_closed = S.vfile_type, S.vfile_is_closed
local vfile_read, vfile_seek, vfile_close = S.vfile_read, S.vfile_seek, S.vfile_close
local real_open, real_lines, real_type, real_close = io.open, io.lines, io.type, io.close
local real_loadfile, real_dofile = loadfile, dofile

local ENOENT, EROFS = 2, 30
local function failure(message, errno) return nil, message, errno end

local vfile = { __name = "FILE*" }
local methods = {
  read = vfile_read,
  seek = vfile_seek,
  close = vfile_close,
  write = S.vfile_write,
  flush = S.vfile_flush,
  setvbuf = S.vfile_setvbuf,
}
vfile.__index = methods
vfile.__close = S.vfile_close_silent

function methods.lines(f, ...)
  if vfile_is_closed(f) then error("attempt to use a closed file", 2) end
  local formats = table.pack(...)
  return function()
    if vfile_is_closed(f) then error("file is already closed", 2) end
    return vfile_read(f, table.unpack(formats, 1, formats.n))
  end
end

local function open_virtual(name, mode)
  mode = mode == nil and "r" or mode
  if type(mode) ~= "string" or not match(mode, "^[rwa]%+?b?$") and not match(mode, "^[rwa]b?%+?$") then
    error("bad argument #2 to 'open' (invalid mode)", 3)
  end
  if find(mode, "[wa+]") then return failure(name .. ": Read-only file system", EROFS) end
  local f = open_embedded(name, vfile, false)
  if f then return f end
  if lfs.attributes(name, "mode") == "directory" then
    return open_embedded(name, vfile, true)
  end
  return failure(name .. ": No such file or directory", ENOENT)
end

function io.open(name, mode)
  if type(name) == "string" and is_path(name) then return open_virtual(name, mode) end
  return real_open(name, mode)
end

function io.type(f)
  return vfile_type(f) or real_type(f)
end

function io.close(f)
  if vfile_type(f) ~= nil then return vfile_close(f) end
  return real_close(f)
end

function io.lines(name, ...)
  if type(name) == "string" and is_path(name) then
    local f, message = open_virtual(name)
    if not f then error(message, 2) end
    local formats = table.pack(...)
    return function()
      if vfile_is_closed(f) then error("file is already closed", 2) end
      local results = table.pack(vfile_read(f, table.unpack(formats, 1, formats.n)))
      if results[1] == nil then vfile_close(f) end
      return table.unpack(results, 1, results.n)
    end
  end
  return real_lines(name, ...)
end

-- loadfile/dofile skip a leading `#` line and a UTF-8 byte order mark
local function chunk_of(name)
  local data = read_file(name)
  if data == nil then return nil end
  if sub(data, 1, 3) == "\239\187\191" then data = sub(data, 4) end
  if byte(data, 1) == 35 then data = (data:gsub("^#[^\n]*", "", 1)) end
  return data
end

function loadfile(name, ...)
  if type(name) == "string" and is_path(name) then
    local data = chunk_of(name)
    if data == nil then return nil, "cannot open " .. name end
    return load(data, "@" .. name, ...)
  end
  return real_loadfile(name, ...)
end

function dofile(name)
  if type(name) == "string" and is_path(name) then
    local f, message = loadfile(name)
    if not f then error(message, 0) end
    return f()
  end
  return real_dofile(name)
end
