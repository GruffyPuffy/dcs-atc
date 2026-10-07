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

local function mission_call(id, expression)
    -- This DCS build drops a single a_do_script return value. A second value
    -- makes the first available in the hook; tested on DCS 2.9.29.27468.
    local inner = 'return ' .. expression .. ', "SENTINEL"'
    local code = 'local _, value = a_do_script(' .. string.format('%q', inner) .. '); return value'
    local call_ok, value, dcs_ok = pcall(net.dostring_in, 'mission', code)
    if not call_ok or not dcs_ok or type(value) ~= 'string' or value:sub(1, 1) ~= '{' then
        return error_reply(id, 'NO_MISSION_RETURN')
    end
    if #value > 1048576 then return error_reply(id, 'RESPONSE_TOO_LARGE') end
    return value
end

-- Convert JSON data into a bounded Lua table literal. No request text is ever
-- inserted as code. Future structured orders can pass through this hook.
local function literal(value, depth, budget)
    budget.count = budget.count + 1
    if budget.count > 512 or depth > 12 then error('REQUEST_COMPLEXITY') end
    local kind = type(value)
    if kind == 'string' then
        if #value > 2048 then error('STRING_TOO_LONG') end
        return string.format('%q', value)
    end
    if kind == 'number' then
        if value ~= value or value == math.huge or value == -math.huge then error('INVALID_NUMBER') end
        return string.format('%.17g', value)
    end
    if kind == 'boolean' then return tostring(value) end
    if kind ~= 'table' then error('INVALID_VALUE') end
    local parts = {}
    for key, item in pairs(value) do
        if (type(key) ~= 'string' and type(key) ~= 'number')
            or (type(key) == 'string' and #key > 64) then error('INVALID_KEY') end
        parts[#parts + 1] = '[' .. literal(key, depth + 1, budget) .. ']='
            .. literal(item, depth + 1, budget)
    end
    return '{' .. table.concat(parts, ',') .. '}'
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
    local safe, encoded = pcall(literal, request, 0, {count = 0})
    if not safe then return error_reply(id, 'INVALID_REQUEST_DATA') end
    return mission_call(id, 'AtcStateBridge.handle(' .. encoded .. ')')
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
