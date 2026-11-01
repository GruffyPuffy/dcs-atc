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

-- Mission-side body, executed via a_do_script inside net.dostring_in('mission').
-- `req` is injected as a Lua literal by mission_call. Returns a JSON string.
-- a_do_script runs in the real mission scripting environment (SSE globals like
-- coalition/Group/coord/net are available there); the bare net.dostring_in
-- 'mission' state does NOT have them. Works on any mission (e.g. TTI).
local MISSION_BODY = [==[
-- Mission-side body for the ATC state bridge.
--
-- Executed by dcs_state_hook.lua via a_do_script inside net.dostring_in('mission').
-- This runs in the REAL mission scripting environment, where the SSE globals
-- (coalition, Group, coord, net) exist. `req` is injected by the hook and holds
-- the parsed request table. Must return a JSON string (built with net.lua2json).
--
-- This file is read fresh on every request, so edits take effect without
-- restarting DCS (the hook falls back to its embedded copy if this file is
-- missing). Deploy with: ./scripts/dcs.sh bridge

local function unit_json(u)
    local p = u:getPosition()
    local lat, lon, alt = coord.LOtoLL(p.p)
    -- p.x is the unit's forward vector; DCS world is x=north, z=east, y=up.
    local heading = math.deg(math.atan2(p.x.z, p.x.x)) % 360
    -- Ground speed (m/s) from the velocity vector, for the low-and-fast check
    -- and the landing-rollout gate. getPosition() has no velocity field, so
    -- read it from getVelocity() (falls back to 0 if unavailable).
    local v = { x = 0, y = 0, z = 0 }
    local ok, vel = pcall(u.getVelocity, u)
    if ok and vel then v = vel end
    local speed = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
    return {
        name = u:getName(),
        type = u:getTypeName(),
        x = p.p.x, y = p.p.y, z = p.p.z,
        lat = lat, lon = lon, alt = alt,
        heading = heading,
        speed = speed,
        player = u:getPlayerName() or "",
    }
end

local function group_json(g, coa)
    local units = {}
    for _, u in ipairs(g:getUnits() or {}) do
        units[#units + 1] = unit_json(u)
    end
    return {
        name = g:getName(),
        coalition = coa,
        category = g:getCategory(),
        units = units,
    }
end

local function snapshot()
    local out = { groups = {}, airbases = {} }
    for _, coa in ipairs({0, 1, 2}) do
        local ok, groups = pcall(coalition.getGroups, coa)
        if ok and groups then
            for _, g in ipairs(groups) do
                out.groups[#out.groups + 1] = group_json(g, coa)
            end
        end
        local ok2, bases = pcall(coalition.getAirbases, coa)
        if ok2 and bases then
            for _, b in ipairs(bases) do
                local p = b:getPoint()
                local lat, lon, alt = coord.LOtoLL(p)
                out.airbases[#out.airbases + 1] = {
                    name = b:getName(),
                    coalition = coa,
                    x = p.x, y = p.y, z = p.z,
                    lat = lat, lon = lon, alt = alt,
                }
            end
        end
    end
    return out
end

local function envelope(ok, payload)
    if ok then
        return net.lua2json({ v = 1, id = req.id, ok = true, result = payload })
    end
    return net.lua2json({ v = 1, id = req.id, ok = false, error = payload })
end

-- Player-slot callsigns from the mission definition (env.mission). Each slot
-- has a callsign table {name=..., nr=...}; e.g. name="Colt", nr=1 -> "Colt11".
local function callsigns()
    local out = {}
    local function scan(coa_name, coa)
        if type(coa) ~= 'table' or type(coa.country) ~= 'table' then return end
        for _, country in ipairs(coa.country) do
            for _, cat in ipairs({'plane', 'helicopter'}) do
                local groups = country[cat] and country[cat].group
                if type(groups) == 'table' then
                    for _, g in ipairs(groups) do
                        for _, u in ipairs(g.units or {}) do
                            if u.skill == 'Client' or u.skill == 'Player' then
                                local cs = u.callsign
                                local name, nr = '', ''
                                if type(cs) == 'table' then
                                    name = tostring(cs.name or '')
                                    nr = tostring(cs.nr or '')
                                elseif type(cs) == 'string' then
                                    name = cs
                                end
                                out[#out + 1] = {
                                    name = name,
                                    nr = nr,
                                    full = name .. nr,
                                    type = tostring(u.type or ''),
                                    coalition = coa_name,
                                }
                            end
                        end
                    end
                end
            end
        end
    end
    scan('blue', env.mission.coalition.blue)
    scan('red', env.mission.coalition.red)
    return { callsigns = out }
end

local op = req.op
if op == 'status' then
    return envelope(true, snapshot())
elseif op == 'parking' then
    -- Parking spots for an airbase (by name). DCS exposes no taxi-route names,
    -- but each spot has a terminal index and distance to the runway, which we
    -- use to pick a taxi route per parking area.
    local out = { spots = {} }
    for _, b in ipairs(coalition.getAirbases(2)) do
        if b:getName() == req.airbase then
            local ok, parking = pcall(function() return b:getParking() end)
            if ok and parking then
                for _, p in ipairs(parking) do
                    local lat, lon, alt = coord.LOtoLL(p.vTerminalPos)
                    out.spots[#out.spots + 1] = {
                        term = p.Term_Index,
                        dist_rwy = p.fDistToRW,
                        lat = lat, lon = lon, alt = alt,
                    }
                end
            end
        end
    end
    return envelope(true, out)
elseif op == 'callsigns' then
    return envelope(true, callsigns())
elseif op == 'runways' then
    -- Runway ends for an airbase (by name): each has a name (e.g. "25"), the
    -- threshold position and the runway heading. Used to seed airspace.json.
    local out = { runways = {} }
    for _, b in ipairs(coalition.getAirbases(2)) do
        if b:getName() == req.airbase then
            local ok, rwys = pcall(function() return b:getRunways() end)
            if ok and rwys then
                for _, r in ipairs(rwys) do
                    local lat, lon, alt = coord.LOtoLL(r.position)
                    out.runways[#out.runways + 1] = {
                        name = tostring(r.Name or r.name or ''),
                        lat = lat, lon = lon, alt = alt,
                        course = r.course,
                        length = r.length,
                        width = r.width,
                    }
                end
            end
        end
    end
    return envelope(true, out)
elseif op == 'tower' then
    -- Dispatcher/tower position for an airbase (by name). DCS sometimes returns
    -- a Position3 ({p=vec3}) or a zero vec3 (Caucasus origin / Crimea); only a
    -- real local-coord point is accepted, then airbase centre.
    local function as_point(p)
        if type(p) ~= 'table' then return nil end
        if type(p.p) == 'table' then p = p.p end
        if type(p.x) ~= 'number' or type(p.z) ~= 'number' then return nil end
        if p.x == 0 and p.z == 0 then return nil end
        return p
    end
    for _, coa in ipairs({0, 1, 2}) do
        local okb, bases = pcall(coalition.getAirbases, coa)
        if okb and bases then
            for _, b in ipairs(bases) do
                if b:getName() == req.airbase then
                    local p = nil
                    local ok, disp = pcall(function() return b:getDispatcherTowerPos() end)
                    if ok then p = as_point(disp) end
                    if not p then
                        local ok2, tech = pcall(function() return b:getTechObjectPos() end)
                        if ok2 then p = as_point(tech) end
                    end
                    if not p then p = b:getPoint() end
                    local lat, lon, alt = coord.LOtoLL(p)
                    return envelope(true, { name = b:getName(), lat = lat, lon = lon, alt = alt })
                end
            end
        end
    end
    return envelope(false, 'NO_AIRBASE')
elseif op == 'bullseye' then
    -- Coalition bullseye ("main reference point") as set in the mission editor.
    -- Blue is coalition 2 (the same side the airbases are read from). The pilot
    -- reads bullseye off their HSI, so the trainer must use the *mission's*
    -- point — a made-up one would be unverifiable. Returns NO_BULLSEYE when the
    -- mission has none set (getMainRefPoint then gives the origin).
    local coa = tonumber(req.coalition) or 2
    local ok, p = pcall(coalition.getMainRefPoint, coa)
    if not ok or type(p) ~= 'table' then return envelope(false, 'NO_BULLSEYE') end
    if p.x == 0 and p.z == 0 then return envelope(false, 'NO_BULLSEYE') end
    local lat, lon, alt = coord.LOtoLL(p)
    return envelope(true, { coalition = coa, lat = lat, lon = lon, alt = alt })
elseif op == 'weather' then
    local w = env.mission.weather or {}
    local wind = w.wind or {}
    local ground = wind.atGround or {}
    local clouds = w.clouds or {}
    local vis = w.visibility or {}
    local season = w.season or {}
    -- Mission time of day in seconds since midnight (for the ATIS letter).
    local mission_time_s = nil
    if timer and timer.getAbsTime then
        mission_time_s = timer.getAbsTime()
    elseif env.mission.start_time then
        mission_time_s = env.mission.start_time
    end
    return envelope(true, {
        qnh_mmhg = w.qnh,
        wind_dir = ground.dir,
        wind_speed_ms = ground.speed,
        wind_dir_2000 = (wind.at2000 or {}).dir,
        wind_speed_2000_ms = (wind.at2000 or {}).speed,
        clouds_base_m = clouds.base,
        clouds_density = clouds.density,
        clouds_preset = clouds.preset,
        visibility_m = vis.distance,
        temperature_c = season.temperature,
        fog = w.enable_fog,
        dust = w.enable_dust,
        mission_time_s = mission_time_s,
    })
elseif op == 'set_route' then
    local g = Group.getByName(req.group_name)
    if not g then return envelope(false, 'NO_GROUP') end
    g:getController():setTask({ id = 'Mission', params = { route = req.route_data } })
    return envelope(true, { moved = req.group_name })
elseif op == 'set_task' then
    local g = Group.getByName(req.group_name)
    if not g then return envelope(false, 'NO_GROUP') end
    g:getController():setTask(req.task_data)
    return envelope(true, { tasked = req.group_name })
elseif op == 'message' then
    local text = tostring(req.text or '')
    if req.mission then
        -- Separate, targeted mission-message destination for the optional
        -- pilot transcript. Keep the existing chat path below unchanged.
        local target = tostring(req.player or '')
        if target == '' or text == '' then
            return envelope(false, 'MISSING_PLAYER_OR_TEXT')
        end
        if not trigger or not trigger.action or not trigger.action.outTextForUnit then
            return envelope(false, 'NO_UNIT_MESSAGE_API')
        end
        for _, coa in ipairs({0, 1, 2}) do
            local ok, groups = pcall(coalition.getGroups, coa)
            if ok and groups then
                for _, group in ipairs(groups) do
                    for _, unit in ipairs(group:getUnits() or {}) do
                        local ok_player, player = pcall(unit.getPlayerName, unit)
                        if ok_player and player == target then
                            local ok_id, unit_id = pcall(unit.getID, unit)
                            if ok_id and unit_id then
                                local ok_send = pcall(
                                    trigger.action.outTextForUnit,
                                    unit_id, text, tonumber(req.duration) or 8, false)
                                if ok_send then
                                    return envelope(true, { sent = 'mission', to = target })
                                end
                            end
                            return envelope(false, 'UNIT_MESSAGE_FAILED')
                        end
                    end
                end
            end
        end
        return envelope(false, 'PLAYER_NOT_FOUND')
    end
    -- Post text to a player's in-game chat/message log (no .miz edit). Used by
    -- the bot's --debug "comms log": it echoes every radio exchange (the pilot's
    -- STT transcript and the ATC reply) back to the asking player, so a trainer
    -- can read what was heard and answered. Targets one player by name when known
    -- (net.send_chat_to), else the whole mission (trigger.action.outText).
    if req.player and req.player ~= '' and net.get_player_list then
        for _, pid in ipairs(net.get_player_list()) do
            local info = net.get_player_info and net.get_player_info(pid) or {}
            if info and info.name == req.player then
                net.send_chat_to(text, pid)
                return envelope(true, { sent = 'player', to = req.player })
            end
        end
    end
    if trigger and trigger.action and trigger.action.outText then
        trigger.action.outText(text, tonumber(req.duration) or 8)
        return envelope(true, { sent = 'all' })
    end
    return envelope(false, 'NO_MESSAGE_API')
else
    return envelope(false, 'UNKNOWN_OP')
end
]==]

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

-- Optional external body file (same folder as this hook). Lets us iterate on the
-- mission-side logic without restarting DCS: edit dcs_state_body.lua and the next
-- request picks it up. Falls back to the embedded MISSION_BODY if io is missing
-- or the file is absent.
local BODY_CANDIDATES = {}
do
    -- Derive the hook's own directory from its source path (Wine: @C:\...\x.lua).
    local ok, info = pcall(debug.getinfo, 1, 'S')
    local src = ok and info and info.source
    if type(src) == 'string' and src:sub(1, 1) == '@' then
        local dir = src:sub(2):gsub('[^/\\]+$', '')
        BODY_CANDIDATES[#BODY_CANDIDATES + 1] = dir .. 'dcs_state_body.lua'
    end
    -- Fallback: derive the Saved Games root from package.path's first entry.
    if type(package) == 'table' and type(package.path) == 'string' then
        local root = package.path:match('^(.-)Scripts[/\\]%?%.lua')
        if root then
            BODY_CANDIDATES[#BODY_CANDIDATES + 1] = root .. 'Scripts\\Hooks\\dcs_state_body.lua'
        end
    end
end

local function read_body()
    if io then
        for _, path in ipairs(BODY_CANDIDATES) do
            local ok, f = pcall(io.open, path, 'r')
            if ok and f then
                local content = f:read('*a')
                f:close()
                if type(content) == 'string' and #content > 0 then return content end
            end
        end
    end
    return MISSION_BODY
end

local function mission_call(id, request)
    local safe, encoded = pcall(literal, request, 0, {count = 0})
    if not safe then return error_reply(id, 'INVALID_REQUEST_DATA') end
    -- The SSE API (coalition/Group/coord/net) only works in the mission state.
    -- The hook's own "gui" state has a stub coalition (no getGroups), so we must
    -- inject into the mission env with a_do_script. Quirk: a_do_script drops a
    -- single return value, so the body returns a second sentinel value.
    local inner = 'local req = ' .. encoded .. '\n'
        .. 'local function run()\n' .. read_body() .. '\nend\n'
        .. 'return run(), "SENTINEL"'
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
