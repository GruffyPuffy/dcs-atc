-- Saved Games hook: local JSON-line bridge. DCS installation files are untouched.
local socket = require('socket')
local listener, bind_error = socket.bind('0.0.0.0', 10309, 1)
if not listener then
    log.write('ATC-State', log.ERROR, 'socket bind failed: ' .. tostring(bind_error))
    return
end
listener:settimeout(0)

local client, input, output, sent = nil, '', nil, 1
local function close_client()
    if client then client:close() end
    client, input, output, sent = nil, '', nil, 1
end

local function error_reply(id, reason)
    return '{"v":1,"id":"' .. id .. '","ok":false,"error":"' .. reason .. '"}'
end

local function json_escape(s)
    s = tostring(s)
    s = s:gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t')
    return s
end

-- One-shot diagnostic: probe which mission-env mechanisms actually work.
-- Returns a hand-built JSON object (no net.lua2json dependency).
local function diag(id)
    local function test(state, code)
        local ok, val = pcall(net.dostring_in, state, code)
        return { ok = ok, type = type(val), value = tostring(val):sub(1, 300) }
    end
    local function ados(code)
        local wrapped = 'local a,b,c = a_do_script(' .. string.format('%q', code) .. '); '
            .. 'return tostring(a) .. "|" .. tostring(b) .. "|" .. tostring(c)'
        return test('mission', wrapped)
    end
    local tests = {
        ados_returns = ados('return "A", "B"'),
        ados_single = ados('return "ONLY"'),
        ados_err = ados('error("boom")'),
        ados_globals = ados('local t={} for k in pairs(_G) do t[#t+1]=k end table.sort(t) return table.concat(t,",")'),
        ados_coalition_raw = ados('return tostring(rawget(_G,"coalition") ~= nil)'),
        ados_net_raw = ados('return tostring(rawget(_G,"net") ~= nil)'),
        ados_coalition_type = ados('return type(coalition)'),
        ados_body = test('mission',
            'local _, v = a_do_script(\'local function run() return "BODYOK" end return run(), "SENTINEL"\'); return v'),
        m_coalition = test('mission', 'return tostring(rawget(_G,"coalition") ~= nil)'),
        m_net = test('mission', 'return tostring(rawget(_G,"net") ~= nil)'),
        e_coalition = test('export', 'return tostring(rawget(_G,"coalition") ~= nil)'),
        e_net = test('export', 'return tostring(rawget(_G,"net") ~= nil)'),
        g_coalition = test('gui', 'return tostring(rawget(_G,"coalition") ~= nil)'),
        s_coalition = test('server', 'return tostring(rawget(_G,"coalition") ~= nil)'),
    }
    local parts = {}
    for name, r in pairs(tests) do
        parts[#parts + 1] = '"' .. name .. '":{"ok":' .. tostring(r.ok)
            .. ',"type":"' .. json_escape(r.type) .. '","value":"' .. json_escape(r.value) .. '"}'
    end
    return '{"v":1,"id":"' .. id .. '","ok":true,"result":{' .. table.concat(parts, ',') .. '}}'
end

-- Mission-side logic is maintained only in dcs_state_body.lua. The file is
-- re-read on every request so edits take effect without restarting DCS.
-- Missing, unreadable, or invalid files fail closed; never run a stale copy.
local BODY_CANDIDATES = {}
do
    -- Derive the hook's own directory from its source path (Wine: @C:\...\x.lua).
    local ok, info = pcall(debug.getinfo, 1, 'S')
    local src = ok and info and info.source
    if type(src) == 'string' and src:sub(1, 1) == '@' then
        local dir = src:sub(2):gsub('[^/\\]+$', '')
        BODY_CANDIDATES[#BODY_CANDIDATES + 1] = dir .. 'dcs_state_body.lua'
    end
    -- Alternate path: derive the Saved Games root from package.path's first entry.
    if type(package) == 'table' and type(package.path) == 'string' then
        local root = package.path:match('^(.-)Scripts[/\\]%?%.lua')
        if root then
            BODY_CANDIDATES[#BODY_CANDIDATES + 1] = root .. 'Scripts\\Hooks\\dcs_state_body.lua'
        end
    end
end

local function read_body()
    if not io then return nil, 'BODY_IO_UNAVAILABLE' end
    if #BODY_CANDIDATES == 0 then return nil, 'BODY_PATH_UNAVAILABLE' end

    local failures = {}
    for _, path in ipairs(BODY_CANDIDATES) do
        local ok_open, f, open_err = pcall(io.open, path, 'r')
        if ok_open and f then
            local ok_read, content = pcall(function()
                local value = f:read('*a')
                f:close()
                return value
            end)
            if ok_read and type(content) == 'string' and #content > 0 then
                return content
            end
            failures[#failures + 1] = path .. ': ' .. tostring(ok_read and 'empty body' or content)
        else
            failures[#failures + 1] = path .. ': ' .. tostring(ok_open and open_err or f)
        end
    end
    return nil, 'BODY_UNREADABLE', table.concat(failures, '; ')
end

local function mission_call(id, request)
    local safe, encoded = pcall(literal, request, 0, {count = 0})
    if not safe then return error_reply(id, 'INVALID_REQUEST_DATA') end
    local body, body_error, body_detail = read_body()
    if not body then
        log.write('ATC-State', log.ERROR, tostring(body_error) .. ': ' .. tostring(body_detail or ''))
        return error_reply(id, body_error or 'BODY_UNAVAILABLE')
    end
    local compiled, compile_error = loadstring(
        'local req = {}\nlocal function run()\n' .. body .. '\nend\nreturn run')
    if not compiled then
        log.write('ATC-State', log.ERROR, 'BODY_SYNTAX_ERROR: ' .. tostring(compile_error))
        return error_reply(id, 'BODY_SYNTAX_ERROR')
    end
    -- The SSE API (coalition/Group/coord/net) only works in the mission state.
    -- The hook's own "gui" state has a stub coalition (no getGroups), so we must
    -- inject into the mission env with a_do_script. Quirk: a_do_script drops a
    -- single return value, so the body returns a second sentinel value.
    local inner = 'local req = ' .. encoded .. '\n'
        .. 'local function run()\n' .. body .. '\nend\n'
        .. 'local ok, value = pcall(run)\n'
        .. 'if not ok then return net.lua2json({v=1,id=req.id,ok=false,'
        .. 'error="BODY_RUNTIME_ERROR",detail=tostring(value)}), "SENTINEL" end\n'
        .. 'return value, "SENTINEL"'
    local code = 'local _, value = a_do_script(' .. string.format('%q', inner) .. '); return value'
    local call_ok, value = pcall(net.dostring_in, 'mission', code)
    if not call_ok then
        log.write('ATC-State', log.ERROR, 'dostring_in error: ' .. tostring(value))
        return error_reply(id, 'DOSTRING_ERROR')
    end
    if type(value) ~= 'string' then
        log.write('ATC-State', log.ERROR, 'dostring_in returned ' .. type(value))
        return error_reply(id, 'NON_STRING_RETURN')
    end
    if value:sub(1, 1) ~= '{' then
        log.write('ATC-State', log.ERROR, 'dostring_in returned: ' .. value:sub(1, 300))
        return error_reply(id, 'NON_JSON_RETURN')
    end
    if #value > 1048576 then return error_reply(id, 'RESPONSE_TOO_LARGE') end
    return value
end

local function handle(line)
    local parsed, request = pcall(net.json2lua, line)
    if not parsed or type(request) ~= 'table' then return error_reply('', 'INVALID_JSON') end
    local id = request.id
    if type(id) ~= 'string' or #id < 1 or #id > 64 or not id:match('^[%w_-]+$') then
        return error_reply('', 'INVALID_ID')
    end
    if request.v ~= 1 then return error_reply(id, 'UNSUPPORTED_VERSION') end
    if type(request.op) ~= 'string' or #request.op < 1 or #request.op > 32
        or not request.op:match('^[%w_]+$') then return error_reply(id, 'INVALID_OP') end
    if request.op == 'ping' then
        return '{"v":1,"id":"' .. id .. '","ok":true,"result":"pong"}'
    end
    if request.op == 'diag' then
        return diag(id)
    end
    if request.op == 'eval' then
        local chunk, load_err = loadstring('return ' .. tostring(request.code))
        if not chunk then
            return '{"v":1,"id":"' .. id .. '","ok":false,"error":"EVAL_LOAD"}'
        end
        local ok, value = pcall(chunk)
        if not ok then
            return '{"v":1,"id":"' .. id .. '","ok":false,"error":"EVAL_ERROR","detail":"'
                .. json_escape(value) .. '"}'
        end
        return '{"v":1,"id":"' .. id .. '","ok":true,"result":"' .. json_escape(value) .. '"}'
    end
    return mission_call(id, request)
end

local function frame()
    if not client then
        client = listener:accept()
        if not client then return end
        client:settimeout(0)
        input, output, sent = '', nil, 1
    end
    if not output then
        local line, err, partial = client:receive('*l')
        input = input .. (line or partial or '')
        if #input > 4096 then
            output = error_reply('', 'REQUEST_TOO_LARGE') .. '\n'
        elseif line then
            output = handle(input) .. '\n'
        elseif err == 'closed' then
            close_client()
            return
        else
            return
        end
    end
    local next_byte, err, last_byte = client:send(output, sent)
    sent = (next_byte or last_byte or sent - 1) + 1
    if sent > #output or (err and err ~= 'timeout') then close_client() end
end

DCS.setUserCallbacks({
    onSimulationFrame = function()
        local ok, err = pcall(frame)
        if not ok then
            log.write('ATC-State', log.ERROR, 'bridge frame: ' .. tostring(err))
            close_client()
        end
    end,
    onSimulationStop = close_client,
})
log.write('ATC-State', log.INFO, 'JSON bridge listening on 10309')
